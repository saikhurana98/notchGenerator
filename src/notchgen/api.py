"""The web portal: upload a DXF, confirm the layer mapping, review, download."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

import ezdxf
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import pipeline
from .config import Config

MAX_UPLOAD_BYTES = 32 * 1024 * 1024
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


def _remember_name(directory: Path, name: str) -> None:
    (directory / "original-name").write_text(name, encoding="utf-8")


def _recall_name(directory: Path) -> str:
    try:
        return (directory / "original-name").read_text(encoding="utf-8").strip() or "part.dxf"
    except OSError:
        return "part.dxf"


def _session_dir(session_id: str, create: bool = False) -> Path:
    # Session ids are minted here as hex UUIDs, so anything else is a traversal attempt.
    try:
        uuid.UUID(hex=session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="malformed session id") from None
    path = DATA_DIR / session_id
    if create:
        path.mkdir(parents=True, exist_ok=True)
    elif not path.is_dir():
        raise HTTPException(status_code=404, detail="session expired or unknown")
    return path


class ProcessRequest(BaseModel):
    session_id: str
    mapping: dict[str, str]
    depth: float = Field(default=2.0, gt=0)
    depth_from: str = "bend-end"
    shape: str = "v"
    thickness: float | None = None
    stitch_tol: float | None = None
    snap_tol: float | None = None
    sliver_tol: float | None = None
    chord_tol: float | None = None
    bridge_tol: float | None = None
    angle_tol: float | None = None
    max_stub: float | None = None
    merge_overlapping: bool = False
    single_layer: bool = True


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/api/upload")
async def upload(file: UploadFile) -> JSONResponse:
    _sweep()
    name = Path(file.filename or "input.dxf").name
    if not name.lower().endswith(".dxf"):
        raise HTTPException(status_code=400, detail="please upload a .dxf file")
    payload = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413, detail=f"file larger than {MAX_UPLOAD_BYTES // 1024 // 1024} MB"
        )

    session_id = uuid.uuid4().hex
    directory = _session_dir(session_id, create=True)
    source = directory / "input.dxf"
    source.write_bytes(payload)
    _remember_name(directory, name)

    try:
        found = pipeline.inspect(str(source))
    except UNREADABLE as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"could not read that DXF: {exc}") from None

    return JSONResponse({"session_id": session_id, "filename": name, **found.as_dict()})


@app.post("/api/process")
def process(request: ProcessRequest) -> JSONResponse:
    _sweep()
    directory = _session_dir(request.session_id)
    source = directory / "input.dxf"
    if not source.exists():
        raise HTTPException(status_code=404, detail="session expired or unknown")

    mapping = {k: v for k, v in request.mapping.items() if v}
    cfg = Config.from_dict(request.model_dump(exclude={"session_id", "mapping"}))

    try:
        result = pipeline.process(str(source), mapping, cfg)
    except UNREADABLE as exc:
        raise HTTPException(status_code=400, detail=f"could not read that DXF: {exc}") from None

    output = directory / "notched.dxf"
    output.unlink(missing_ok=True)
    if result.ok:
        pipeline.save(result, str(output), single_layer=request.single_layer)

    body = result.as_dict()
    body["download_ready"] = output.exists()
    body["download_name"] = output_name(_recall_name(directory))
    body["config"] = cfg.as_dict()
    return JSONResponse(body)


# GET *and* HEAD: browsers probe a download with HEAD, and a 404 there leaves the transfer
# showing its full byte count but never finishing.
@app.api_route("/api/download/{session_id}", methods=["GET", "HEAD"])
def download(session_id: str) -> FileResponse:
    directory = _session_dir(session_id)
    output = directory / "notched.dxf"
    if not output.exists():
        raise HTTPException(status_code=404, detail="nothing has been generated for this session")
    return FileResponse(
        output,
        # A DXF is plain text; octet-stream is what keeps browsers from trying to display it.
        media_type="application/octet-stream",
        filename=output_name(_recall_name(directory)),
        headers={"Cache-Control": "no-store"},
    )


if WEB_DIR is not None:
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
