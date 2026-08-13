"""Portal tests, driven through the ASGI app rather than a live server."""

from __future__ import annotations

import uuid

import ezdxf
import pytest
from fastapi.testclient import TestClient

from conftest import MAPPING


@pytest.fixture
def client(tmp_path, monkeypatch):
    from notchgen import api

    monkeypatch.setattr(api, "DATA_DIR", tmp_path / "sessions")
    return TestClient(api.app)


def upload_sample(client, sample_path):
    with open(sample_path, "rb") as handle:
        response = client.post(
            "/api/upload", files={"file": ("part.dxf", handle, "application/dxf")}
        )
    assert response.status_code == 200, response.text
    return response.json()


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_upload_reports_layers_and_a_suggested_mapping(client, sample_path):
    body = upload_sample(client, sample_path)
    assert body["suggested_mapping"] == MAPPING
    assert {layer["name"] for layer in body["layers"]} == set(MAPPING.values())
    assert {item["role"] for item in body["geometry"]} == set(MAPPING)
    assert body["bounds"]["min"][0] == pytest.approx(-29.985, abs=1e-3)


def test_process_then_download_yields_a_two_layer_dxf(client, sample_path, tmp_path):
    session = upload_sample(client, sample_path)["session_id"]
    response = client.post(
        "/api/process",
        json={"session_id": session, "mapping": MAPPING, "depth": 2.0, "thickness": 2.5},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] and body["download_ready"]
    assert len(body["notches"]) == 4
    assert body["area_before"] - body["area_after"] == pytest.approx(28.331, abs=0.01)
    assert {item["role"] for item in body["after"]} == {"outer", "notch", "interior"}
    assert any(d["code"] == "depth-under-thickness" for d in body["diagnostics"])

    got = client.get(f"/api/download/{session}")
    assert got.status_code == 200
    out = tmp_path / "downloaded.dxf"
    out.write_bytes(got.content)
    doc = ezdxf.readfile(out)
    assert {e.dxf.layer for e in doc.modelspace()} == {"OUTER_PROFILES", "INTERIOR_PROFILES"}


def test_a_failed_run_reports_errors_and_offers_no_download(client, sample_path):
    session = upload_sample(client, sample_path)["session_id"]
    body = client.post(
        "/api/process",
        json={"session_id": session, "mapping": MAPPING, "depth": 40.0},
    ).json()
    assert not body["ok"]
    assert not body["download_ready"]
    assert any(d["level"] == "error" for d in body["diagnostics"])
    assert client.get(f"/api/download/{session}").status_code == 404


def test_depth_must_be_positive(client, sample_path):
    session = upload_sample(client, sample_path)["session_id"]
    response = client.post(
        "/api/process", json={"session_id": session, "mapping": MAPPING, "depth": 0}
    )
    assert response.status_code == 422


def test_a_non_dxf_filename_is_refused(client):
    response = client.post("/api/upload", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert response.status_code == 400


def test_a_file_that_is_not_really_a_dxf_is_refused_cleanly(client):
    """ezdxf raises a plain OSError here, which must still surface as a 400."""
    response = client.post("/api/upload", files={"file": ("part.dxf", b"not a dxf", "image/vnd.dxf")})
    assert response.status_code == 400
    assert "could not read" in response.json()["detail"]


def test_an_oversized_upload_is_refused(client, monkeypatch):
    from notchgen import api

    monkeypatch.setattr(api, "MAX_UPLOAD_BYTES", 64)
    response = client.post("/api/upload", files={"file": ("big.dxf", b"x" * 500, "image/vnd.dxf")})
    assert response.status_code == 413


def test_a_malformed_session_id_is_refused(client):
    assert client.get("/api/download/../../etc/passwd").status_code == 404
    assert client.get("/api/download/not-a-uuid").status_code == 400


def test_an_unknown_session_is_a_404(client):
    assert client.get(f"/api/download/{uuid.uuid4().hex}").status_code == 404
    response = client.post(
        "/api/process", json={"session_id": uuid.uuid4().hex, "mapping": MAPPING}
    )
    assert response.status_code == 404


def test_expired_sessions_are_swept(client, sample_path, monkeypatch):
    from notchgen import api

    session = upload_sample(client, sample_path)["session_id"]
    monkeypatch.setattr(api, "SESSION_TTL_SECONDS", -1)
    assert client.get(f"/api/download/{session}").status_code == 404
