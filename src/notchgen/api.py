"""The web portal: upload one or more DXFs, confirm the layer mapping, review, download.

A session holds a *batch*: one or more uploaded files, each with its own layer mapping and
its own result. A single-file upload is simply a batch of one, and every response carries
the first file's fields at the top level, so the single-file shape is unchanged.

Geometry is the bulky part of a response, so only the first file's travels with the upload.
The rest is fetched per file as the user switches to it, and cached in the browser.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

import ezdxf
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import pipeline
from .config import Config

MAX_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_BATCH_FILES = 25
SESSION_TTL_SECONDS = 6 * 3600

# ezdxf signals an unreadable file with a plain OSError, not with DXFError, so both have
# to be caught to turn "that isn't a DXF" into a 400 rather than a 500.
UNREADABLE = (ezdxf.DXFError, OSError, UnicodeDecodeError, ValueError)

DATA_DIR = Path(os.environ.get("NOTCHGEN_DATA", str(Path(tempfile.gettempdir()) / "notchgen-sessions")))


def _find_web_dir() -> Path | None:
    """Locate the static frontend, whether running from a checkout or an installed package."""
    candidates = [
        os.environ.get("NOTCHGEN_WEB"),
        Path(__file__).resolve().parents[2] / "web",  # src/notchgen/api.py -> repo root
        Path("/app/web"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return None


WEB_DIR = _find_web_dir()

app = FastAPI(title="notchgen", description="Bend-relief notches for flat-pattern DXF files")


def _sweep() -> None:
    """Drop sessions older than the TTL. Cheap enough to run on every request."""
    if not DATA_DIR.exists():
        return
    cutoff = time.time() - SESSION_TTL_SECONDS
    for entry in DATA_DIR.iterdir():
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


def output_name(original: str) -> str:
    """`part.dxf` becomes `part with notch.dxf`, so the download is recognisable."""
    stem = Path(original).stem or "part"
    return f"{stem} with notch.dxf"


def bundle_name(count: int) -> str:
    return f"notched parts ({count}).zip"


# -- session and file plumbing ----------------------------------------------------


def _checked_id(value: str) -> str:
    """Ids are minted here as hex UUIDs, so anything else is a traversal attempt."""
    try:
        uuid.UUID(hex=value)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail="malformed id") from None
    return value


def _session_dir(session_id: str, create: bool = False) -> Path:
    path = DATA_DIR / _checked_id(session_id)
    if create:
        (path / "files").mkdir(parents=True, exist_ok=True)
    elif not path.is_dir():
        raise HTTPException(status_code=404, detail="session expired or unknown")
    else:
        # Keep a session the user is still working in from being swept out from under them.
        try:
            os.utime(path)
        except OSError:
            pass
    return path


def _manifest(directory: Path) -> list[dict]:
    """The batch's files, in upload order."""
    try:
        data = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _write_manifest(directory: Path, entries: list[dict]) -> None:
    (directory / "manifest.json").write_text(json.dumps(entries), encoding="utf-8")


def _file_dir(directory: Path, file_id: str) -> Path:
    path = directory / "files" / _checked_id(file_id)
    if not path.is_dir():
        raise HTTPException(status_code=404, detail="no such file in this session")
    return path


def _entry(directory: Path, file_id: str) -> dict:
    for item in _manifest(directory):
        if item.get("id") == file_id:
            return item
    raise HTTPException(status_code=404, detail="no such file in this session")


def _readable(directory: Path) -> list[dict]:
    """Manifest entries whose upload actually parsed as a DXF."""
    return [e for e in _manifest(directory) if e.get("ok")]


def _outputs(directory: Path) -> list[tuple[dict, Path]]:
    """(entry, path) for every file in the batch that currently has a result on disk."""
    out = []
    for entry in _manifest(directory):
        candidate = directory / "files" / entry["id"] / "notched.dxf"
        if candidate.exists():
            out.append((entry, candidate))
    return out


# -- requests ---------------------------------------------------------------------

LayerMapping = dict[str, "str | list[str] | None"]


class ProcessRequest(BaseModel):
    session_id: str
    mapping: LayerMapping = Field(default_factory=dict)
    """Applied to every file that has no entry in `mappings`."""

    mappings: dict[str, LayerMapping] = Field(default_factory=dict)
    """Per-file overrides, keyed by file id."""

    file_ids: list[str] | None = None
    """Which files to process. All of them when omitted."""

    depth: float = Field(default=2.0, gt=0)
    depth_from: str = "bend-end"
    shape: str = "v"
    thickness: float | None = None
    bend_zone: float | None = Field(default=None, gt=0)
    stitch_tol: float | None = None
    snap_tol: float | None = None
    sliver_tol: float | None = None
    chord_tol: float | None = None
    bridge_tol: float | None = None
    angle_tol: float | None = None
    max_stub: float | None = None
    merge_overlapping: bool = False
    single_layer: bool = True

    def config(self) -> Config:
        return Config.from_dict(
            self.model_dump(exclude={"session_id", "mapping", "mappings", "file_ids"})
        )


# -- routes -----------------------------------------------------------------------


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/api/version")
def version() -> dict:
    """What is actually deployed, so the portal can show which build it is talking to."""
    return {
        "version": os.environ.get("NOTCHGEN_VERSION", "dev"),
        "revision": os.environ.get("NOTCHGEN_REVISION", "unknown"),
        "max_files": MAX_BATCH_FILES,
        "max_bytes": MAX_UPLOAD_BYTES,
    }


@app.post("/api/upload")
async def upload(file: list[UploadFile], session: str | None = None) -> JSONResponse:
    """Take one or more DXFs. In a batch, a file that will not parse is reported, not fatal.

    With `session`, the files are added to that batch instead of starting a new one, which
    is what the portal's "add files" button does.
    """
    _sweep()
    if not file:
        raise HTTPException(status_code=400, detail="no file uploaded")

    existing: list[dict] = []
    if session:
        session_id = session
        directory = _session_dir(session_id)
        (directory / "files").mkdir(parents=True, exist_ok=True)
        existing = _manifest(directory)
    else:
        session_id = uuid.uuid4().hex
        directory = _session_dir(session_id, create=True)
    if len(existing) + len(file) > MAX_BATCH_FILES:
        raise HTTPException(status_code=413, detail=f"at most {MAX_BATCH_FILES} files at a time")

    entries: list[dict] = []
    readable: dict[str, dict] = {}

    try:
        for index, upload_file in enumerate(file):
            name = Path(upload_file.filename or "input.dxf").name
            file_id = uuid.uuid4().hex
            entry: dict = {"id": file_id, "filename": name, "ok": False}

            if not name.lower().endswith(".dxf"):
                entry["error"] = "please upload a .dxf file"
                entries.append(entry)
                continue

            payload = await upload_file.read(MAX_UPLOAD_BYTES + 1)
            if len(payload) > MAX_UPLOAD_BYTES:
                entry["error"] = f"file larger than {MAX_UPLOAD_BYTES // 1024 // 1024} MB"
                entry["status"] = 413
                entries.append(entry)
                continue

            where = directory / "files" / file_id
            where.mkdir(parents=True, exist_ok=True)
            (where / "input.dxf").write_bytes(payload)

            try:
                # Reading a DXF is seconds of CPU on a big part. Off the event loop it goes,
                # or one upload stalls every other request on the server.
                found = await run_in_threadpool(pipeline.inspect, str(where / "input.dxf"))
            except UNREADABLE as exc:
                shutil.rmtree(where, ignore_errors=True)
                entry["error"] = f"could not read that DXF: {exc}"
                entries.append(entry)
                continue

            entry["ok"] = True
            entries.append(entry)
            body = found.as_dict()
            readable[file_id] = {
                "file_id": file_id,
                "filename": name,
                "ok": True,
                "layers": body["layers"],
                "suggested_mapping": body["suggested_mapping"],
                "bounds": body["bounds"],
                "diagnostics": body["diagnostics"],
                # Only the file shown first brings its geometry; the rest fetch it on demand.
                "geometry": body["geometry"] if index == 0 else None,
            }

        if not readable:
            failed = entries[0]
            if not existing:
                shutil.rmtree(directory, ignore_errors=True)
            raise HTTPException(
                status_code=failed.get("status", 400),
                detail=failed.get("error", "could not read that DXF"),
            )
        _write_manifest(directory, [*existing, *entries])
    except HTTPException:
        raise
    except Exception:
        if not existing:
            shutil.rmtree(directory, ignore_errors=True)
        raise

    files = [
        readable.get(
            e["id"],
            {
                "file_id": e["id"],
                "filename": e["filename"],
                "ok": False,
                "error": e.get("error", "could not read that DXF"),
            },
        )
        for e in entries
    ]
    first = next(f for f in files if f["ok"])
    if first["geometry"] is None:
        # The first upload was unreadable, so the first *readable* one carries the geometry.
        found = await run_in_threadpool(geometry_of, directory, first["file_id"])
        first["geometry"] = found["geometry"]

    return JSONResponse(
        {
            "session_id": session_id,
            "files": files,
            # The single-file shape, unchanged: the first readable file's own fields.
            "filename": first["filename"],
            "layers": first["layers"],
            "suggested_mapping": first["suggested_mapping"],
            "geometry": first["geometry"],
            "bounds": first["bounds"],
            "diagnostics": first["diagnostics"],
        }
    )


def geometry_of(directory: Path, file_id: str, chord_tol: float = 1e-3) -> dict:
    source = _file_dir(directory, file_id) / "input.dxf"
    if not source.exists():
        raise HTTPException(status_code=404, detail="no such file in this session")
    try:
        found = pipeline.inspect(str(source), chord_tol)
    except UNREADABLE as exc:
        raise HTTPException(status_code=400, detail=f"could not read that DXF: {exc}") from None
    body = found.as_dict()
    return {"file_id": file_id, "geometry": body["geometry"], "bounds": body["bounds"]}


@app.get("/api/geometry/{session_id}/{file_id}")
def geometry(session_id: str, file_id: str, chord_tol: float = 1e-3) -> JSONResponse:
    """One file's drawable geometry, fetched when the user switches to it."""
    _sweep()
    return JSONResponse(geometry_of(_session_dir(session_id), file_id, chord_tol))


@app.post("/api/process")
def process(request: ProcessRequest) -> JSONResponse:
    _sweep()
    directory = _session_dir(request.session_id)
    entries = _readable(directory)
    if not entries:
        raise HTTPException(status_code=404, detail="session expired or unknown")

    if request.file_ids is not None:
        wanted = {_checked_id(f) for f in request.file_ids}
        entries = [e for e in entries if e["id"] in wanted]
        if not entries:
            raise HTTPException(status_code=404, detail="no such file in this session")

    # A new run invalidates any bundle zipped from the previous one.
    (directory / "bundle.zip").unlink(missing_ok=True)
    cfg = request.config()
    results: list[dict] = []

    for entry in entries:
        file_id = entry["id"]
        where = directory / "files" / file_id
        source, output = where / "input.dxf", where / "notched.dxf"
        output.unlink(missing_ok=True)
        if not source.exists():
            continue

        chosen = request.mappings.get(file_id) or request.mapping
        mapping = {k: v for k, v in (chosen or {}).items() if v}

        try:
            if not mapping:
                # Usable without the portal: fall back to this file's own auto-detection.
                mapping = {k: v for k, v in pipeline.inspect(str(source)).suggested.items() if v}
            result = pipeline.process(str(source), mapping, cfg)
        except UNREADABLE as exc:
            if len(entries) == 1:
                raise HTTPException(
                    status_code=400, detail=f"could not read that DXF: {exc}"
                ) from None
            results.append(_unreadable_result(entry, exc))
            continue

        if result.ok:
            pipeline.save(result, str(output), single_layer=request.single_layer)

        body = result.as_dict()
        body.update(
            {
                "file_id": file_id,
                "filename": entry["filename"],
                "mapping": mapping,
                "download_ready": output.exists(),
                "download_name": output_name(entry["filename"]),
            }
        )
        results.append(body)

    ready = [r for r in results if r.get("download_ready")]
    first = results[0] if results else {}
    payload: dict = {
        # The single-file shape, unchanged: the first processed file's own fields.
        **{k: v for k, v in first.items() if k not in ("file_id", "filename", "mapping")},
        "files": results,
        "succeeded": len(ready),
        "failed": len(results) - len(ready),
        "config": cfg.as_dict(),
    }
    if len(ready) > 1:
        # More than one result means the batch download is a zip of all of them.
        payload["download_ready"] = True
        payload["download_name"] = bundle_name(len(ready))
    payload.setdefault("ok", False)
    payload.setdefault("download_ready", False)
    payload.setdefault("download_name", "")
    return JSONResponse(payload)


def _unreadable_result(entry: dict, exc: Exception) -> dict:
    return {
        "file_id": entry["id"],
        "filename": entry["filename"],
        "ok": False,
        "notches": [],
        "download_ready": False,
        "diagnostics": [
            {
                "level": "error",
                "code": "unreadable",
                "message": f"could not read that DXF: {exc}",
                "context": {},
            }
        ],
    }


def _build_bundle(directory: Path, outputs: list[tuple[dict, Path]]) -> Path:
    """Zip every result in the batch, each under its own recognisable name."""
    bundle = directory / "bundle.zip"
    used: dict[str, int] = {}
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry, path in outputs:
            name = output_name(entry["filename"])
            seen = used.get(name, 0)
            used[name] = seen + 1
            if seen:
                # Two uploads sharing a filename would otherwise collide inside the zip.
                stem, _, suffix = name.rpartition(".")
                name = f"{stem} ({seen + 1}).{suffix}"
            archive.write(path, arcname=name)
    return bundle


# GET *and* HEAD: browsers probe a download with HEAD, and a 404 there leaves the transfer
# showing its full byte count but never finishing.
@app.api_route("/api/download/{session_id}", methods=["GET", "HEAD"])
def download(session_id: str) -> FileResponse:
    """One DXF when the batch produced one result, a zip of all of them when it produced more."""
    directory = _session_dir(session_id)
    outputs = _outputs(directory)
    if not outputs:
        raise HTTPException(status_code=404, detail="nothing has been generated for this session")
    if len(outputs) == 1:
        entry, path = outputs[0]
        return _as_download(path, output_name(entry["filename"]), "application/octet-stream")
    bundle = directory / "bundle.zip"
    if not bundle.exists():
        bundle = _build_bundle(directory, outputs)
    return _as_download(bundle, bundle_name(len(outputs)), "application/zip")


@app.api_route("/api/download/{session_id}/{file_id}", methods=["GET", "HEAD"])
def download_one(session_id: str, file_id: str) -> FileResponse:
    directory = _session_dir(session_id)
    path = _file_dir(directory, file_id) / "notched.dxf"
    if not path.exists():
        raise HTTPException(status_code=404, detail="nothing has been generated for this file")
    name = output_name(_entry(directory, file_id)["filename"])
    return _as_download(path, name, "application/octet-stream")


def _as_download(path: Path, filename: str, media_type: str) -> FileResponse:
    return FileResponse(
        path,
        # A DXF is plain text; octet-stream is what keeps browsers from trying to display it.
        media_type=media_type,
        filename=filename,
        headers={"Cache-Control": "no-store"},
    )


if WEB_DIR is not None:
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
