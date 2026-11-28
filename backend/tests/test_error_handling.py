"""Global exception handlers (backend/main.py): unmapped errors are a bare
500 with the traceback in the log; storage errors map to 400/404 anywhere.

Run: python -m pytest backend/tests/test_error_handling.py -q
"""
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.storage import manager as mgr
    (tmp_path / "projects").mkdir()
    monkeypatch.setattr(mgr, "PROJECTS_DIR", tmp_path / "projects")
    from backend.main import app
    return TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000),
                      raise_server_exceptions=False)


def test_unhandled_error_is_a_bare_500_and_logged(client, monkeypatch, caplog):
    from backend.storage.manager import storage

    def boom():
        raise RuntimeError("secret /Users/someone/data/projects detail")
    monkeypatch.setattr(storage, "list_projects", boom)
    with caplog.at_level(logging.ERROR, logger="backend.main"):
        for path in ("/api/projects/", "/api/projects/covers", "/api/agent/projects"):
            r = client.get(path)
            assert r.status_code == 500, path
            assert r.json() == {"detail": "internal error"}
            assert "secret" not in r.text
    logged = [rec for rec in caplog.records if rec.name == "backend.main"]
    assert logged and logged[0].exc_info and "secret" in str(logged[0].exc_info[1])


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/projects/load/bad.id"),
    ("DELETE", "/api/projects/delete/bad.id"),
    ("GET", "/api/agent/projects/bad.id"),
    ("PUT", "/api/agent/projects/bad.id/panels"),
    ("POST", "/api/projects/bad.id/migrate-assets"),
])
def test_invalid_project_id_is_400_everywhere(client, method, path):
    r = client.request(method, path, json={"panels": []})
    assert r.status_code == 400, r.text
    assert "project id" in r.json()["detail"].lower() or "invalid" in r.json()["detail"].lower()


def test_project_not_found_error_is_404(client, monkeypatch):
    from backend.storage.manager import ProjectNotFoundError, storage

    def missing():
        raise ProjectNotFoundError("gone")
    monkeypatch.setattr(storage, "list_projects", missing)
    r = client.get("/api/projects/")
    assert r.status_code == 404 and r.json() == {"detail": "Project not found"}


def test_upload_to_unknown_project_still_registers_the_asset(client, tmp_path, monkeypatch):
    from backend.storage import asset_store as astore_mod
    import backend.api.assets as assets_mod
    store = astore_mod.AssetStore(tmp_path / "assets", tmp_path / "assets" / "index.json")
    monkeypatch.setattr(astore_mod, "asset_store", store)
    monkeypatch.setattr(assets_mod, "asset_store", store)
    r = client.post("/api/assets/upload/nope/p1", files={"file": ("a.png", b"\x89PNG", "image/png")})
    assert r.status_code == 200 and r.json()["image"].startswith("/api/assets/file/")
