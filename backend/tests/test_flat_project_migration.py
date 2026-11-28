"""The pre-folder projects/project.json moves into projects/<id>/ once, at
server startup (backend/main.py lifespan), not on every project listing.

Run: python -m pytest backend/tests/test_flat_project_migration.py -q
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _flat(tmp_path, monkeypatch):
    from backend.storage import manager as mgr
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(mgr, "PROJECTS_DIR", projects)
    (projects / "project.json").write_text(json.dumps({"id": "old_one", "name": "Old"}))
    return mgr, projects


def test_listing_no_longer_migrates(tmp_path, monkeypatch):
    mgr, projects = _flat(tmp_path, monkeypatch)
    assert mgr.storage.list_projects() == []
    assert (projects / "project.json").exists()


def test_startup_migrates_the_flat_file(tmp_path, monkeypatch):
    mgr, projects = _flat(tmp_path, monkeypatch)
    import backend.main as main_mod
    monkeypatch.setattr(main_mod, "_maybe_cleanup_orphan_assets", lambda: None)
    monkeypatch.setattr(main_mod.comics_rewrite_batch, "resume", lambda: None)

    async def run():
        async with main_mod.lifespan(main_mod.app):
            pass
    asyncio.run(run())
    assert not (projects / "project.json").exists()
    assert [p["id"] for p in mgr.storage.list_projects()] == ["old_one"]
