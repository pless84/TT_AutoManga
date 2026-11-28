"""Project storage under concurrency: per-project locks, version counter,
listing cache, handlers off the event loop, background orphan cleanup."""
import threading
import time

import pytest


@pytest.fixture()
def mgr(tmp_path, monkeypatch):
    from backend.storage import manager as mgr
    (tmp_path / "projects").mkdir()
    monkeypatch.setattr(mgr, "PROJECTS_DIR", tmp_path / "projects")
    return mgr


@pytest.fixture()
def client(mgr):
    from fastapi.testclient import TestClient
    from backend.main import app
    return TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))


def _new(mgr, **extra):
    return mgr.storage.save_project({"name": "c", "panels": [], "composerPages": [],
                                     "settings": {}, **extra})["id"]


def test_edit_project_serializes_writers(mgr):
    pid = _new(mgr, counter=0)

    def bump(n):
        for _ in range(n):
            with mgr.storage.edit_project(pid) as p:
                p["counter"] += 1

    threads = [threading.Thread(target=bump, args=(25,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    p = mgr.storage.load_project(pid)
    assert p["counter"] == 100
    assert p["version"] == 101          # create + 100 edits


def test_edit_project_discards_on_error_and_404s(mgr):
    pid = _new(mgr, counter=0)
    with pytest.raises(RuntimeError):
        with mgr.storage.edit_project(pid) as p:
            p["counter"] = 99
            raise RuntimeError("boom")
    assert mgr.storage.load_project(pid)["counter"] == 0
    with pytest.raises(mgr.ProjectNotFoundError):
        with mgr.storage.edit_project("nope"):
            pass


def test_stale_version_refused(mgr):
    pid = _new(mgr)
    p = mgr.storage.load_project(pid)
    assert p["version"] == 1
    mgr.storage.save_project(dict(p), expected_version=1)          # ok -> 2
    with pytest.raises(mgr.StaleVersionError) as e:
        mgr.storage.save_project(dict(p), expected_version=1)
    assert e.value.current == 2


def test_list_projects_cache_follows_changes(mgr):
    a = _new(mgr)
    b = _new(mgr)
    assert {p["id"] for p in mgr.storage.list_projects()} == {a, b}
    with mgr.storage.edit_project(a) as p:
        p["name"] = "renamed"
    names = {p["id"]: p["name"] for p in mgr.storage.list_projects()}
    assert names[a] == "renamed"
    mgr.storage.delete_project(b)
    assert [p["id"] for p in mgr.storage.list_projects()] == [a]


def test_parallel_panel_patches_are_all_kept(client):
    """Handlers now run in the threadpool; without the per-project lock two
    PATCHes would load the same file and the later save would drop the other."""
    pid = client.post("/api/agent/projects", json={"name": "p"}).json()["project"]["id"]
    n = 16
    client.put(f"/api/agent/projects/{pid}/panels", json={"panels": [
        {"scene": f"s{i}"} for i in range(n)]})
    errors = []

    def patch(i):
        r = client.patch(f"/api/agent/projects/{pid}/panels/panel_{i + 1}",
                         json={"dialogue": f"line {i}"})
        if r.status_code != 200:
            errors.append(r.text)

    threads = [threading.Thread(target=patch, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    panels = client.get(f"/api/agent/projects/{pid}").json()["project"]["panels"]
    assert [p.get("dialogue") for p in panels] == [f"line {i}" for i in range(n)]


def test_storyboard_keeps_edits_made_during_llm_call(client, mgr, monkeypatch):
    from backend.api import agent
    pid = client.post("/api/agent/projects", json={"name": "s"}).json()["project"]["id"]
    client.put(f"/api/agent/projects/{pid}/panels", json={"panels": [{"scene": "old"}]})

    class FakeLLM:
        async def generate_storyboard(self, text, level):
            # another writer edits the project while the LLM "thinks"
            with mgr.storage.edit_project(pid) as p:
                p["panels"][0]["dialogue"] = "edited meanwhile"
            return {"panels": [{"scene": "new"}]}

    monkeypatch.setattr(agent.projects.llm_client, "for_request", lambda *a, **k: FakeLLM())
    r = client.post("/api/agent/storyboard", json={
        "project_id": pid, "text": "x", "replace": False})
    assert r.status_code == 200, r.text
    panels = mgr.storage.load_project(pid)["panels"]
    assert [p["scene"] for p in panels] == ["new", "old"]
    assert panels[1]["dialogue"] == "edited meanwhile"


def test_orphan_cleanup_runs_at_most_daily(tmp_path, monkeypatch):
    from backend import main
    calls = []
    monkeypatch.setattr(main, "_cleanup_orphan_assets", lambda: calls.append(1))
    monkeypatch.setattr(main, "ORPHAN_CLEANUP_STAMP", tmp_path / "stamp")
    main._maybe_cleanup_orphan_assets()
    main._maybe_cleanup_orphan_assets()
    assert calls == [1]
    old = time.time() - main.ORPHAN_CLEANUP_EVERY_S - 10
    import os
    os.utime(tmp_path / "stamp", (old, old))
    main._maybe_cleanup_orphan_assets()
    assert calls == [1, 1]
