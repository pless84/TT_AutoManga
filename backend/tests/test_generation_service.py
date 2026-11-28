"""One generation path (backend/services/generation.py) behind both
/api/agent/generate and the Assets tab's /api/assets/swarmui/generate.

Run: python -m pytest backend/tests/test_generation_service.py -q
"""
import json
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.tests.test_e2e_agent import client  # noqa: E402,F401


def _asset_path(url):
    from backend.storage import asset_store as astore_mod
    return astore_mod.asset_store.path_for(url.rsplit("/", 1)[-1])


def _project(c):
    pid = c.post("/api/agent/projects", json={"name": "svc"}).json()["project"]["id"]
    c.put(f"/api/agent/projects/{pid}/panels",
          json={"panels": [{"id": "p1", "scene": "rooftop", "characters": []}]})
    return pid


def test_assets_generate_embeds_png_recipe_and_binds(client):
    pid = _project(client)
    r = client.post("/api/assets/swarmui/generate", json={
        "project_id": pid, "panel_id": "p1", "prompt": "rooftop at dusk",
        "negative_prompt": "blurry", "seed": 7})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] and body["image"].startswith("/api/assets/file/")
    assert body["asset_id"] == body["image"].rsplit("/", 1)[-1] and body["path"] == body["image"]
    assert body["content_rating"] == "sfw"
    img = Image.open(_asset_path(body["image"]))
    meta = json.loads(img.info["automanga"])
    assert meta["prompt"] == "rooftop at dusk" and meta["seed"] == 7
    assert meta["negative_prompt"].startswith("blurry, nsfw")
    panel = client.get(f"/api/agent/projects/{pid}").json()["project"]["panels"][0]
    assert panel["image"] == body["image"]


def test_assets_generate_uses_alpha_fallback(client):
    """remove_background on the Assets tab gets the same local fallback as
    the agent path when SwarmUI hands back an opaque image."""
    pid = _project(client)
    import backend.api.assets as assets_mod
    from backend.tests._fixtures import solid_png
    from unittest.mock import patch
    opaque = AsyncMock(return_value={"success": True, "image": solid_png((255, 255, 255))})
    with patch.object(assets_mod.swarm_client, "generate_image", opaque):
        r = client.post("/api/assets/swarmui/generate", json={
            "project_id": pid, "panel_id": "p1", "prompt": "x", "remove_background": True})
    assert r.status_code == 200, r.text
    from backend.storage import asset_store as astore_mod
    entry = astore_mod.asset_store.get(r.json()["asset_id"])
    assert entry["alpha_method"] in ("rembg", "threshold")


def test_assets_generate_unknown_project_still_returns_an_asset(client):
    r = client.post("/api/assets/swarmui/generate", json={
        "project_id": "nope", "panel_id": "x", "prompt": "p"})
    assert r.status_code == 200, r.text
    assert r.json()["image"].startswith("/api/assets/file/")


def test_assets_generate_swarm_failure_is_success_false(client):
    import backend.api.assets as assets_mod
    from unittest.mock import patch
    failing = AsyncMock(return_value={"success": False, "error": "boom"})
    with patch.object(assets_mod.swarm_client, "generate_image", failing):
        r = client.post("/api/assets/swarmui/generate", json={
            "project_id": "nope", "panel_id": "x", "prompt": "p"})
    assert r.status_code == 200
    assert r.json() == {"success": False, "error": "boom", "content_rating": "sfw"}
    with patch.object(assets_mod.swarm_client, "generate_image", failing):
        r = client.post("/api/agent/generate", json={"prompt": "p"})
    assert r.status_code == 502 and r.json()["detail"] == "SwarmUI: boom"


def test_generate_does_not_log_prompts_at_info(client, caplog):
    pid = _project(client)
    with caplog.at_level(logging.INFO):
        client.post("/api/assets/swarmui/generate", json={
            "project_id": pid, "panel_id": "p1", "prompt": "secret plot twist"})
        client.post("/api/agent/generate", json={"project_id": pid, "prompt": "secret plot twist"})
    assert not [rec for rec in caplog.records
                if rec.levelno >= logging.INFO and "secret plot twist" in rec.getMessage()]


def test_agent_generate_refuses_missing_panel_before_painting(client):
    pid = _project(client)
    import backend.api.agent as agent_mod
    from unittest.mock import patch
    spy = AsyncMock()
    with patch.object(agent_mod.swarm_client, "generate_image", spy):
        r = client.post("/api/agent/generate", json={"project_id": pid, "panel_id": "ghost",
                                                     "prompt": "x"})
    assert r.status_code == 400 and "ghost" in r.json()["detail"]
    spy.assert_not_called()
