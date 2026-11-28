"""GET /api/comics/scripts caches each script's summary by the mtimes it
depends on (the script, out/<name>/panels, the automanga.json files).

Run: python -m pytest backend/tests/test_comics_listing.py -q
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

import backend.api.comics as cm  # noqa: E402
from backend.main import app  # noqa: E402


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "Comics"
    (root / "scripts").mkdir(parents=True)
    (root / "automanga.js").write_text("", encoding="utf-8")
    (root / "scripts" / "a.comic").write_text("title: First\nPAGE 1\nPANEL 1\n", encoding="utf-8")
    monkeypatch.setattr(cm, "COMICS_DIR", root)
    monkeypatch.setattr(cm, "_summary_cache", {})
    calls = []
    real = cm._summary
    monkeypatch.setattr(cm, "_summary", lambda p: calls.append(p.stem) or real(p))
    return root, calls


def _list(c):
    r = c.get("/api/comics/scripts")
    assert r.status_code == 200, r.text
    return {s["name"]: s for s in r.json()["scripts"]}


def _bump(path: Path):
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_listing_reuses_summaries_until_something_changes(repo):
    root, calls = repo
    c = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    assert _list(c)["a"]["title"] == "First" and calls == ["a"]
    _list(c)
    assert calls == ["a"]                       # cached

    script = root / "scripts" / "a.comic"
    script.write_text("title: Renamed\nPAGE 1\nPANEL 1\nPANEL 2\n", encoding="utf-8")
    _bump(script)
    s = _list(c)["a"]
    assert s["title"] == "Renamed" and s["panels"] == 2 and calls == ["a", "a"]

    panels = root / "out" / "a" / "panels"
    panels.mkdir(parents=True)
    (panels / "p01-01.png").write_bytes(b"x")
    assert _list(c)["a"]["painted"] == 1 and len(calls) == 3
    (panels / "p01-02.png").write_bytes(b"x")
    _bump(panels)
    assert _list(c)["a"]["painted"] == 2 and len(calls) == 4

    (root / "out" / "a" / "automanga.json").write_text(json.dumps({"project_id": "pid1"}))
    assert _list(c)["a"]["project_id"] == "pid1" and len(calls) == 5
    _list(c)
    assert len(calls) == 5


def test_deleted_scripts_leave_the_cache(repo):
    root, calls = repo
    c = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    (root / "scripts" / "b.comic").write_text("title: B\n", encoding="utf-8")
    assert set(_list(c)) == {"a", "b"}
    (root / "scripts" / "b.comic").unlink()
    assert set(_list(c)) == {"a"}
    assert set(cm._summary_cache) == {str(root / "scripts" / "a.comic")}
