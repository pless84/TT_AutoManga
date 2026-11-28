"""Agent API: projects, panels, the storyboard LLM call, generation
defaults (/settings) and project settings."""
import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...core.config import settings
from ...core.openai_client import client as llm_client
from ...services.generation import SHOT_TAGS
from ...storage.manager import storage
from ..project_lock import locked_project
from ._common import load_or_404

router = APIRouter()


# ---------------------------------------------------------------------------
# Projects & panels
# ---------------------------------------------------------------------------

class CreateProjectRequest(BaseModel):
    name: str
    panels: List[Dict[str, Any]] = []


def _ensure_panel_ids(panels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fill missing ids as panel_1..panel_n; never collide within the batch."""
    used = {p.get("id") for p in panels if p.get("id")}
    n = 0
    for p in panels:
        if not p.get("id"):
            n += 1
            while f"panel_{n}" in used:
                n += 1
            p["id"] = f"panel_{n}"
            used.add(p["id"])
    return panels


def _validate_panels(panels: List[Dict[str, Any]]) -> None:
    seen = set()
    for p in panels:
        pid = p.get("id")
        if pid:
            if pid in seen:
                raise HTTPException(status_code=400,
                                    detail=f"duplicate panel id: {pid}")
            seen.add(pid)
        if not (p.get("scene") or "").strip():
            raise HTTPException(status_code=400,
                                detail="every panel needs a non-empty scene")
        if p.get("shot") is not None and p["shot"] not in SHOT_TAGS:
            raise HTTPException(
                status_code=400,
                detail=f"shot must be one of {sorted(SHOT_TAGS)}")


def _project_stats(project: Dict[str, Any]) -> Dict[str, Any]:
    panels = project.get("panels", [])
    pages = project.get("composerPages", [])
    used_srcs = set()
    for page in pages:
        for el in page.get("elements", []):
            if el.get("type") in ("image", "page-image") and el.get("src"):
                used_srcs.add(el["src"])
    with_image = [p for p in panels if p.get("image")]
    return {
        "panels": len(panels),
        "panels_with_image": len(with_image),
        "pages": len(pages),
        "panels_missing_image": [p["id"] for p in panels if not p.get("image")],
        "panels_missing_page": [p["id"] for p in with_image
                                if p.get("image") not in used_srcs],
    }


@router.post("/projects")
def create_project(req: CreateProjectRequest):
    project = {"name": req.name or "untitled", "panels": req.panels,
               "settings": {}, "composerPages": []}
    _ensure_panel_ids(project["panels"])
    _validate_panels(project["panels"])
    saved = storage.save_project(project)
    return {"project": saved}


@router.get("/projects")
def list_projects():
    out = []
    for p in storage.list_projects():
        out.append({"id": p.get("id"), "name": p.get("name"),
                    "updatedAt": p.get("updatedAt"),
                    "stats": _project_stats(p)})
    return {"projects": out}


@router.get("/projects/{project_id}")
def get_project(project_id: str):
    project = load_or_404(project_id)
    return {"project": project, "stats": _project_stats(project)}


class PutPanelsRequest(BaseModel):
    panels: List[Dict[str, Any]]


@router.put("/projects/{project_id}/panels")
@locked_project
def put_panels(project_id: str, req: PutPanelsRequest):
    project = load_or_404(project_id)
    _ensure_panel_ids(req.panels)
    _validate_panels(req.panels)
    project["panels"] = req.panels
    saved = storage.save_project(project)
    return {"success": True, "panels": saved["panels"]}


@router.patch("/projects/{project_id}/panels/{panel_id}")
@locked_project
def patch_panel(project_id: str, panel_id: str, updates: Dict[str, Any]):
    project = load_or_404(project_id)
    updates = dict(updates)
    if "id" in updates and updates["id"] != panel_id:
        raise HTTPException(status_code=400, detail="cannot change panel id")
    updates.pop("id", None)
    if updates.get("shot") is not None and updates["shot"] not in SHOT_TAGS:
        raise HTTPException(
            status_code=400,
            detail=f"shot must be one of {sorted(SHOT_TAGS)}")
    if "scene" in updates and not (updates["scene"] or "").strip():
        raise HTTPException(status_code=400, detail="scene cannot be empty")
    for panel in project.get("panels", []):
        if panel.get("id") == panel_id:
            panel.update(updates)
            saved = storage.save_project(project)
            target = next(p for p in saved["panels"] if p["id"] == panel_id)
            return {"success": True, "panel": target}
    raise HTTPException(status_code=404, detail="Panel not found")


class StoryboardRequest(BaseModel):
    project_id: str
    text: str
    detail_level: int = 3
    replace: bool = True
    endpoint: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None


@router.post("/storyboard")
async def agent_storyboard(req: StoryboardRequest):
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty")
    await asyncio.to_thread(load_or_404, req.project_id)   # 404 before the LLM call

    # a request-local client; masked echoes are ignored and the stored key is
    # only sent to the stored endpoint (a foreign endpoint needs the
    # request's own api_key)
    llm = llm_client.for_request(req.endpoint, req.api_key, model=req.model)

    result = await llm.generate_storyboard(req.text, req.detail_level)
    if result.get("error"):
        raise HTTPException(status_code=502,
                            detail=f"LLM: {result['error']}")
    panels = result.get("panels") or []
    if not panels:
        raise HTTPException(status_code=502, detail="LLM returned no panels")

    def apply() -> List[Dict[str, Any]]:
        # load AFTER the (slow) LLM call, under the lock: edits made while
        # it ran are kept
        with storage.lock(req.project_id):
            project = load_or_404(req.project_id)
            new = list(panels)
            if not req.replace:
                new.extend(project.get("panels", []))
            _ensure_panel_ids(new)
            _validate_panels(new)
            project["panels"] = new
            return storage.save_project(project)["panels"]
    return {"panels": await asyncio.to_thread(apply)}


def _swarm_view() -> Dict[str, Any]:
    sw = settings.swarmui
    return {"address": sw.address,
            "currentModel": sw.currentModel,
            "currentSteps": sw.currentSteps,
            "currentCfgScale": sw.currentCfgScale,
            "currentWidth": sw.currentWidth,
            "currentHeight": sw.currentHeight,
            "currentSamplerName": sw.currentSamplerName,
            "promptSuffix": sw.promptSuffix,
            "currentLoras": [l.model_dump() for l in sw.currentLoras]}


@router.get("/settings")
async def get_agent_settings():
    """Effective generation defaults /generate falls back to."""
    return {"swarmui": _swarm_view()}


class AgentSettingsUpdate(BaseModel):
    currentModel: Optional[str] = None
    currentSteps: Optional[int] = None
    currentCfgScale: Optional[float] = None
    currentWidth: Optional[int] = None
    currentHeight: Optional[int] = None
    currentSamplerName: Optional[str] = None
    promptSuffix: Optional[str] = None
    currentLoras: Optional[List[Dict[str, Any]]] = None


_INT_RANGES = {"currentSteps": (1, 150),
               "currentWidth": (64, 4096),
               "currentHeight": (64, 4096)}


@router.put("/settings")
async def put_agent_settings(req: AgentSettingsUpdate):
    """Update generation defaults (same fields the UI Settings tab edits).
    Takes effect on the next /generate call and persists to settings.json."""
    data = req.model_dump(exclude_none=True)
    for field, (lo, hi) in _INT_RANGES.items():
        if field in data and not (lo <= data[field] <= hi):
            raise HTTPException(status_code=400,
                                detail=f"{field} must be {lo}-{hi}")
    if "currentWidth" in data and data["currentWidth"] % 8:
        raise HTTPException(status_code=400,
                            detail="currentWidth must be a multiple of 8")
    if "currentHeight" in data and data["currentHeight"] % 8:
        raise HTTPException(status_code=400,
                            detail="currentHeight must be a multiple of 8")
    if "currentCfgScale" in data and not (0 <= data["currentCfgScale"] <= 30):
        raise HTTPException(status_code=400,
                            detail="currentCfgScale must be 0-30")
    if "currentLoras" in data:
        for l in data["currentLoras"]:
            if not isinstance(l.get("name"), str) or not l["name"].strip():
                raise HTTPException(status_code=400,
                                    detail="each lora needs a non-empty name")
            w = l.get("weight", 1.0)
            if not isinstance(w, (int, float)) or not (0 <= w <= 2):
                raise HTTPException(status_code=400,
                                    detail="lora weight must be 0-2")

    sw = settings.swarmui
    for k, v in data.items():
        if k == "currentLoras":
            from ...core.config import LoraConfig
            setattr(sw, k, [LoraConfig(name=l["name"],
                                       weight=float(l.get("weight", 1.0)))
                            for l in v])
        else:
            setattr(sw, k, v)

    from ...core.config import save_settings
    save_settings(settings)
    return {"success": True, "swarmui": _swarm_view()}


class ProjectSettingsUpdate(BaseModel):
    style: Optional[str] = None
    characters: Optional[List[Dict[str, Any]]] = None   # [{name, prompt_block}]
    locations: Optional[List[Dict[str, Any]]] = None    # [{name, prompt_block}]
    # "sfw" | "mature" (adult fictional characters only); "" = unset (the
    # global settings.content.defaultRating applies)
    contentRating: Optional[str] = None
    bible: Optional[str] = None   # free-text story bible: worldview, recap,
                                  # established locations/props — for the
                                  # agent's own continuity across chapters


@router.put("/projects/{project_id}/settings")
@locked_project
def put_project_settings(project_id: str, req: ProjectSettingsUpdate):
    """Project-level style profile (appended to every panel prompt), character
    prompt blocks (injected for panels listing that character), and a location
    library (injected for panels naming that location)."""
    project = load_or_404(project_id)
    cfg = project.setdefault("settings", {})
    if req.style is not None:
        cfg["style"] = req.style.strip()
    if req.bible is not None:
        cfg["bible"] = req.bible.strip()
    if req.contentRating is not None:
        rating = req.contentRating.strip().lower()
        if rating not in ("", "sfw", "mature"):
            raise HTTPException(status_code=400,
                                detail='contentRating must be "sfw", "mature" or "" (unset)')
        if rating:
            cfg["contentRating"] = rating
        else:
            cfg.pop("contentRating", None)
    if req.characters is not None:
        for c in req.characters:
            if not isinstance(c.get("name"), str) or not c["name"].strip()                     or not isinstance(c.get("prompt_block"), str) or not c["prompt_block"].strip():
                raise HTTPException(status_code=400,
                                    detail="each character needs name and prompt_block")
        cfg["characters"] = req.characters
    if req.locations is not None:
        for loc in req.locations:
            if not isinstance(loc.get("name"), str) or not loc["name"].strip()                     or not isinstance(loc.get("prompt_block"), str) or not loc["prompt_block"].strip():
                raise HTTPException(status_code=400,
                                    detail="each location needs name and prompt_block")
        cfg["locations"] = req.locations
    saved = storage.save_project(project)
    return {"success": True, "settings": saved.get("settings", {})}


@router.get("/projects/{project_id}/characters")
def get_project_characters(project_id: str):
    """Effective character prompt blocks (project entries override the global
    character library) — what /generate would inject for each name."""
    project = load_or_404(project_id)
    from ...prompting import resolve_blocks
    return {"blocks": resolve_blocks(project)}
