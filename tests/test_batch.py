"""Uploading several files at once, and getting them all back as one download.

A session is a batch; a single-file upload is a batch of one. The single-file response
shape is covered in test_api.py — what is checked here is that more than one file works,
and that the two shapes agree with each other.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import ezdxf
import pytest
from fastapi.testclient import TestClient

from conftest import MAPPING


@pytest.fixture
def client(tmp_path, monkeypatch):
    from notchgen import api

    monkeypatch.setattr(api, "DATA_DIR", tmp_path / "sessions")
    return TestClient(api.app)


def part(tmp_path: Path, name: str, width: float = 40.0) -> Path:
    """A plate with one bend, so each file in a batch is genuinely a different part."""
    from conftest import BEND, EXTENT, OUTER

    doc = ezdxf.new("R2000")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    outline = [(0.0, 0.0), (width, 0.0), (width, 20.0), (0.0, 20.0)]
    for i in range(4):
        msp.add_line(outline[i], outline[(i + 1) % 4], dxfattribs={"layer": OUTER})
    mid = width / 2
    msp.add_line((mid, 0.0), (mid, 20.0), dxfattribs={"layer": BEND})
    msp.add_line((mid - 3, 0.0), (mid - 3, 20.0), dxfattribs={"layer": EXTENT})
    msp.add_line((mid + 3, 20.0), (mid + 3, 0.0), dxfattribs={"layer": EXTENT})
    path = tmp_path / name
    doc.saveas(path)
    return path


def send(client, paths, session=None):
    files = [("file", (p.name, p.read_bytes(), "image/vnd.dxf")) for p in paths]
    url = "/api/upload" + (f"?session={session}" if session else "")
    response = client.post(url, files=files)
    assert response.status_code == 200, response.text
    return response.json()


# -- uploading a batch -------------------------------------------------------------


def test_a_batch_reports_every_file(client, tmp_path):
    paths = [part(tmp_path, f"p{i}.dxf", 40 + 10 * i) for i in range(3)]
    body = send(client, paths)
    assert [f["filename"] for f in body["files"]] == ["p0.dxf", "p1.dxf", "p2.dxf"]
    assert all(f["ok"] for f in body["files"])
    # The single-file keys still describe the first file.
    assert body["filename"] == "p0.dxf"
    assert body["geometry"]


def test_only_the_first_file_carries_its_geometry(client, tmp_path):
    """Geometry is the bulk of the payload, so the rest is fetched when it is looked at."""
    paths = [part(tmp_path, f"p{i}.dxf") for i in range(3)]
    body = send(client, paths)
    assert body["files"][0]["geometry"]
    assert body["files"][1]["geometry"] is None
    assert body["files"][2]["geometry"] is None

    later = client.get(f"/api/geometry/{body['session_id']}/{body['files'][2]['file_id']}")
    assert later.status_code == 200
    assert later.json()["geometry"]
    assert later.json()["bounds"]["max"][0] == pytest.approx(40.0)


def test_geometry_carries_the_layer_and_covers_unmapped_layers(client, tmp_path):
    """The portal recolours a remap client-side, so it needs every layer, tagged."""
    path = part(tmp_path, "p.dxf")
    doc = ezdxf.readfile(path)
    # Two spares: auto-detection claims the larger one as the interior profiles and leaves
    # the other with no role at all, which is the case this test is about.
    doc.modelspace().add_line((0, 30), (10, 30), dxfattribs={"layer": "SCRATCH_A"})
    doc.modelspace().add_line((0, 32), (10, 32), dxfattribs={"layer": "SCRATCH_A"})
    doc.modelspace().add_line((0, 34), (10, 34), dxfattribs={"layer": "SCRATCH_B"})
    doc.saveas(path)

    body = send(client, [path])
    assert all("layer" in item for item in body["geometry"])
    tagged = {item["layer"] for item in body["geometry"]}
    assert {"SCRATCH_A", "SCRATCH_B"} <= tagged

    unmapped = {i["layer"] for i in body["geometry"] if i["role"] == ""}
    assert "SCRATCH_B" in unmapped
    assert body["suggested_mapping"].get("interior") == "SCRATCH_A"


def test_one_unreadable_file_does_not_sink_the_batch(client, tmp_path):
    good = part(tmp_path, "good.dxf")
    bad = tmp_path / "bad.dxf"
    bad.write_bytes(b"not a dxf at all")

    body = send(client, [bad, good])
    assert [f["ok"] for f in body["files"]] == [False, True]
    assert "could not read" in body["files"][0]["error"]
    # The top-level fields describe the first *readable* file.
    assert body["filename"] == "good.dxf"
    assert body["geometry"]


def test_a_batch_of_nothing_readable_is_still_a_400(client, tmp_path):
    one = tmp_path / "a.dxf"
    two = tmp_path / "b.dxf"
    one.write_bytes(b"nope")
    two.write_bytes(b"also nope")
    files = [("file", (p.name, p.read_bytes(), "image/vnd.dxf")) for p in (one, two)]
    assert client.post("/api/upload", files=files).status_code == 400


def test_too_many_files_at_once_is_refused(client, tmp_path, monkeypatch):
    from notchgen import api

    monkeypatch.setattr(api, "MAX_BATCH_FILES", 2)
    paths = [part(tmp_path, f"p{i}.dxf") for i in range(3)]
    files = [("file", (p.name, p.read_bytes(), "image/vnd.dxf")) for p in paths]
    assert client.post("/api/upload", files=files).status_code == 413


def test_more_files_can_be_added_to_an_open_session(client, tmp_path):
    first = send(client, [part(tmp_path, "a.dxf")])
    session = first["session_id"]
    second = send(client, [part(tmp_path, "b.dxf", 60)], session=session)

    assert second["session_id"] == session
    assert [f["filename"] for f in second["files"]] == ["b.dxf"]
    # Both are in the batch now, so processing covers both.
    done = client.post("/api/process", json={"session_id": session, "mapping": MAPPING}).json()
    assert [f["filename"] for f in done["files"]] == ["a.dxf", "b.dxf"]


def test_adding_to_a_session_that_has_gone_is_a_404(client, tmp_path):
    import uuid

    paths = [part(tmp_path, "a.dxf")]
    files = [("file", (p.name, p.read_bytes(), "image/vnd.dxf")) for p in paths]
    got = client.post(f"/api/upload?session={uuid.uuid4().hex}", files=files)
    assert got.status_code == 404


# -- processing a batch ------------------------------------------------------------


def test_every_file_in_the_batch_is_notched(client, tmp_path):
    paths = [part(tmp_path, f"p{i}.dxf", 40 + 10 * i) for i in range(3)]
    body = send(client, paths)
    done = client.post(
        "/api/process", json={"session_id": body["session_id"], "mapping": MAPPING}
    ).json()

    assert done["succeeded"] == 3 and done["failed"] == 0
    assert all(f["ok"] and f["download_ready"] for f in done["files"])
    assert [len(f["notches"]) for f in done["files"]] == [2, 2, 2]
    assert done["download_name"] == "notched parts (3).zip"


def test_a_per_file_mapping_beats_the_shared_one(client, tmp_path):
    paths = [part(tmp_path, "a.dxf"), part(tmp_path, "b.dxf")]
    body = send(client, paths)
    second = body["files"][1]["file_id"]

    done = client.post(
        "/api/process",
        json={
            "session_id": body["session_id"],
            "mapping": MAPPING,
            # Bend and extent the wrong way round, for this one file only.
            "mappings": {second: {**MAPPING, "bend": MAPPING["extent"], "extent": MAPPING["bend"]}},
        },
    ).json()
    assert done["files"][0]["ok"]
    assert not done["files"][1]["ok"]
    assert done["succeeded"] == 1 and done["failed"] == 1


def test_a_subset_can_be_processed(client, tmp_path):
    paths = [part(tmp_path, f"p{i}.dxf") for i in range(3)]
    body = send(client, paths)
    only = body["files"][1]["file_id"]
    done = client.post(
        "/api/process",
        json={"session_id": body["session_id"], "mapping": MAPPING, "file_ids": [only]},
    ).json()
    assert [f["file_id"] for f in done["files"]] == [only]


def test_with_no_mapping_each_file_falls_back_to_its_own_detection(client, tmp_path):
    """The API has to be usable without the portal filling in the layer names."""
    paths = [part(tmp_path, f"p{i}.dxf") for i in range(2)]
    body = send(client, paths)
    done = client.post("/api/process", json={"session_id": body["session_id"]}).json()
    assert done["succeeded"] == 2


def test_a_failure_in_one_file_leaves_the_others_downloadable(client, tmp_path):
    paths = [part(tmp_path, "good.dxf"), part(tmp_path, "bad.dxf")]
    body = send(client, paths)
    bad = body["files"][1]["file_id"]
    done = client.post(
        "/api/process",
        json={
            "session_id": body["session_id"],
            "mapping": MAPPING,
            # A depth far larger than the part cannot produce a safe notch.
            "mappings": {bad: MAPPING},
            "file_ids": [f["file_id"] for f in body["files"]],
            "depth": 2.0,
        },
    ).json()
    assert done["succeeded"] == 2

    hopeless = client.post(
        "/api/process",
        json={"session_id": body["session_id"], "mapping": MAPPING, "depth": 40.0},
    ).json()
    assert hopeless["succeeded"] == 0
    assert client.get(f"/api/download/{body['session_id']}").status_code == 404


# -- downloading a batch -----------------------------------------------------------


def test_the_batch_download_is_a_zip_of_every_result(client, tmp_path):
    paths = [part(tmp_path, f"p{i}.dxf", 40 + 10 * i) for i in range(3)]
    body = send(client, paths)
    client.post("/api/process", json={"session_id": body["session_id"], "mapping": MAPPING})

    got = client.get(f"/api/download/{body['session_id']}")
    assert got.status_code == 200
    assert got.headers["content-type"] == "application/zip"

    archive = zipfile.ZipFile(io.BytesIO(got.content))
    assert sorted(archive.namelist()) == [
        "p0 with notch.dxf",
        "p1 with notch.dxf",
        "p2 with notch.dxf",
    ]
    # Every entry is a real DXF, not an empty placeholder.
    for name in archive.namelist():
        assert b"SECTION" in archive.read(name)[:4096]


def test_two_uploads_sharing_a_name_do_not_collide_in_the_zip(client, tmp_path):
    a = part(tmp_path, "same.dxf")
    other = tmp_path / "second"
    other.mkdir()
    b = part(other, "same.dxf", 60)

    body = send(client, [a, b])
    client.post("/api/process", json={"session_id": body["session_id"], "mapping": MAPPING})
    got = client.get(f"/api/download/{body['session_id']}")
    names = zipfile.ZipFile(io.BytesIO(got.content)).namelist()
    assert sorted(names) == ["same with notch (2).dxf", "same with notch.dxf"]


def test_a_batch_of_one_downloads_as_a_plain_dxf(client, tmp_path):
    body = send(client, [part(tmp_path, "solo.dxf")])
    client.post("/api/process", json={"session_id": body["session_id"], "mapping": MAPPING})
    got = client.get(f"/api/download/{body['session_id']}")
    assert got.headers["content-type"] == "application/octet-stream"
    assert b"SECTION" in got.content[:4096]


def test_one_file_of_a_batch_can_be_downloaded_on_its_own(client, tmp_path):
    paths = [part(tmp_path, f"p{i}.dxf") for i in range(2)]
    body = send(client, paths)
    client.post("/api/process", json={"session_id": body["session_id"], "mapping": MAPPING})

    second = body["files"][1]["file_id"]
    got = client.get(f"/api/download/{body['session_id']}/{second}")
    assert got.status_code == 200
    assert b"SECTION" in got.content[:4096]
    assert client.head(f"/api/download/{body['session_id']}/{second}").status_code == 200


def test_a_rerun_rebuilds_the_zip(client, tmp_path):
    """A stale bundle would hand back the previous run's geometry."""
    paths = [part(tmp_path, f"p{i}.dxf") for i in range(2)]
    body = send(client, paths)
    session = body["session_id"]

    client.post("/api/process", json={"session_id": session, "mapping": MAPPING, "depth": 2.0})
    shallow = client.get(f"/api/download/{session}").content
    client.post("/api/process", json={"session_id": session, "mapping": MAPPING, "depth": 4.0})
    deeper = client.get(f"/api/download/{session}").content
    assert shallow != deeper


def test_a_made_up_file_id_is_refused(client, tmp_path):
    import uuid

    body = send(client, [part(tmp_path, "a.dxf")])
    session = body["session_id"]
    assert client.get(f"/api/download/{session}/{uuid.uuid4().hex}").status_code == 404
    assert client.get(f"/api/download/{session}/not-a-uuid").status_code == 400
    assert client.get(f"/api/geometry/{session}/not-a-uuid").status_code == 400


def test_the_version_route_says_what_is_deployed(client):
    body = client.get("/api/version").json()
    assert body["version"] == "dev"
    assert body["max_files"] >= 1


def test_the_portal_files_are_always_revalidated(client):
    """A deploy reuses these names, so a browser must ask before reusing what it holds."""
    page = client.get("/")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-cache"
    for asset in ("/style.css", "/app.js"):
        assert client.get(asset).headers["cache-control"] == "no-cache"
