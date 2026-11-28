"""Agent API: page assembly (build, plan, cover, order), templates, SFX
presets and element-level CRUD."""
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...assembly import build_page
from ...storage.manager import storage
from ..project_lock import locked_project
from ._common import find_page, load_or_404

router = APIRouter()


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------

class BuildPageRequest(BaseModel):
    template: dict
    name: Optional[str] = None
    panel_ids: List[str] = []
    reading: str = "rtl"            # rtl | ltr
    bubbles: dict = {}              # {mode: auto|none, fontSize, vertical}
    dynamic: bool = False           # slant/deform frames for a cinematic feel
    dry_run: bool = False


@router.post("/projects/{project_id}/pages")
@locked_project
def build_page_endpoint(project_id: str, req: BuildPageRequest):
    project = load_or_404(project_id)
    try:
        page, warnings = build_page(req.model_dump(exclude={"dry_run"}),
                                    project)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    saved = False
    if not req.dry_run:
        pages = project.setdefault("composerPages", [])
        page["order"] = len(pages)
        pages.append(page)
        storage.save_project(project)
        saved = True
    return {"page": page, "warnings": warnings, "saved": saved}


@router.get("/projects/{project_id}/pages/{page_id}")
def get_page(project_id: str, page_id: str):
    project = load_or_404(project_id)
    return {"page": find_page(project, page_id)}


@router.delete("/projects/{project_id}/pages/{page_id}")
@locked_project
def delete_page(project_id: str, page_id: str):
    project = load_or_404(project_id)
    pages = project.get("composerPages", [])
    for i, page in enumerate(pages):
        if page.get("id") == page_id:
            pages.pop(i)
            for j, q in enumerate(pages):   # keep order contiguous
                q["order"] = j
            storage.save_project(project)
            return {"success": True, "pages_remaining": len(pages)}
    raise HTTPException(status_code=404, detail="Page not found")


# exact slot-count match preferred, next larger accepted (extra slots stay
# empty placeholders and render nothing)
_PLAN_TEMPLATES = {1: "hero-splash", 2: "manga-2h", 3: "manga-3", 4: "manga-4",
                   5: "dyn-5-mix", 6: "manga-6", 8: "manga-8"}


def _plan_template(count: int) -> str:
    if count in _PLAN_TEMPLATES:
        return _PLAN_TEMPLATES[count]
    larger = [n for n in _PLAN_TEMPLATES if n > count]
    return _PLAN_TEMPLATES[min(larger)] if larger else "manga-8"


class PlanPagesRequest(BaseModel):
    panels_per_page: int = 4         # 1-8
    splash_panel_ids: List[str] = [] # forced solo splash pages
    only_with_image: bool = True     # skip panels that have no art yet
    reading: str = "rtl"
    bubbles: dict = {}
    dynamic: bool = False
    apply: bool = False              # execute the plan with build_page


@router.post("/projects/{project_id}/plan-pages")
@locked_project
def plan_pages(project_id: str, req: PlanPagesRequest):
    """Heuristic pagination: chunk panels into pages (page = the narrative
    unit of manga), splash panels get a solo page, template picked by slot
    count. Pure planning unless apply=true."""
    project = load_or_404(project_id)
    ppp = max(1, min(8, req.panels_per_page))
    splash = set(req.splash_panel_ids)

    chosen = [p for p in project.get("panels", [])
              if not req.only_with_image or p.get("image")]
    groups: List[List[str]] = []
    group: List[str] = []
    for p in chosen:
        pid = p.get("id")
        if p.get("shot") == "splash" or pid in splash:
            if group:
                groups.append(group)
                group = []
            groups.append([pid])
        else:
            group.append(pid)
            if len(group) >= ppp:
                groups.append(group)
                group = []
    if group:
        groups.append(group)

    plan = [{"index": i + 1, "name": f"p{i + 1}",
             "panel_ids": g,
             "template": {"kind": "builtin", "id": _plan_template(len(g))}}
            for i, g in enumerate(groups)]

    pages_added = 0
    if req.apply and plan:
        for entry in plan:
            page, _warnings = build_page(
                {"template": entry["template"], "name": entry["name"],
                 "panel_ids": entry["panel_ids"], "reading": req.reading,
                 "bubbles": req.bubbles, "dynamic": req.dynamic},
                project)
            pages = project.setdefault("composerPages", [])
            page["order"] = len(pages)
            pages.append(page)
            entry["page_id"] = page["id"]
            pages_added += 1
        storage.save_project(project)
    return {"plan": plan, "applied": req.apply, "pages_added": pages_added}


class CoverRequest(BaseModel):
    title: str
    subtitle: Optional[str] = None
    asset_url: Optional[str] = None    # /api/assets/file/... or data: URL
    panel_id: Optional[str] = None     # reuse an existing panel's image
    title_color: str = "#ffffff"
    font_size: int = 64


@router.post("/projects/{project_id}/cover")
@locked_project
def build_cover(project_id: str, req: CoverRequest):
    """Assemble a cover page (full-bleed art + outlined title) and insert it
    as page 1. Generate the art first via /generate kind=\"cover\"."""
    if not req.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    project = load_or_404(project_id)
    src = None
    if req.asset_url:
        if not (req.asset_url.startswith("/api/assets/file/")
                or req.asset_url.startswith("data:")):
            raise HTTPException(status_code=400,
                                detail="asset_url must be an asset URL or data: URL")
        src = req.asset_url
    elif req.panel_id:
        panel = next((p for p in project.get("panels", [])
                      if p.get("id") == req.panel_id), None)
        if panel is None:
            raise HTTPException(status_code=404, detail="Panel not found")
        src = panel.get("image")
        if not src:
            raise HTTPException(status_code=400,
                                detail="panel has no image to use as cover art")
    else:
        raise HTTPException(status_code=400,
                            detail="asset_url or panel_id is required")

    els: List[Dict[str, Any]] = [{
        "id": uuid.uuid4().hex, "type": "image", "src": src,
        "x": 0, "y": 0, "width": 100, "height": 100, "rotation": 0,
        "zIndex": 0, "locked": False, "visible": True, "frameBorderWidth": 0,
    }, {
        "id": uuid.uuid4().hex, "type": "text", "content": req.title.strip(),
        "x": 8, "y": 6, "width": 84, "height": 16, "rotation": 0,
        "zIndex": 1, "locked": False, "visible": True,
        "fontSize": req.font_size, "fontFamily": "", "color": req.title_color,
        "direction": "horizontal", "fontWeight": "bold", "textAlign": "center",
        "strokeWidth": 6, "strokeColor": "#000000",
    }]
    if req.subtitle and req.subtitle.strip():
        els.append({
            "id": uuid.uuid4().hex, "type": "text",
            "content": req.subtitle.strip(),
            "x": 12, "y": 23, "width": 76, "height": 8, "rotation": 0,
            "zIndex": 2, "locked": False, "visible": True,
            "fontSize": max(12, int(req.font_size * 0.4)), "fontFamily": "",
            "color": req.title_color, "direction": "horizontal",
            "fontWeight": "normal", "textAlign": "center",
            "strokeWidth": 3, "strokeColor": "#000000",
        })
    page = {"id": uuid.uuid4().hex, "name": "cover", "templateId": "cover",
            "width": 600, "height": 900, "order": 0, "elements": els}
    pages = project.setdefault("composerPages", [])
    for p in pages:
        p["order"] = p.get("order", 0) + 1
    pages.insert(0, page)
    storage.save_project(project)
    return {"page": page}


# curated onomatopoeia for the SFX text tool: Chinese, Japanese and English
# words (`lang`), each meaning in English and Chinese; the UI offers English
# words in English and all of them in Chinese
_SFX_PRESETS = [
    {"word": "轰", "lang": "zh", "meaning": "explosion, engine", "meaningZh": "爆炸、引擎"},
    {"word": "砰", "lang": "zh", "meaning": "impact", "meaningZh": "撞击"},
    {"word": "咔嚓", "lang": "zh", "meaning": "snap, shutter", "meaningZh": "断裂、快门"},
    {"word": "咚", "lang": "zh", "meaning": "heavy blow, heartbeat", "meaningZh": "重击、心跳"},
    {"word": "唰", "lang": "zh", "meaning": "swoosh past", "meaningZh": "快速掠过"},
    {"word": "啪", "lang": "zh", "meaning": "slap", "meaningZh": "拍击"},
    {"word": "哗啦", "lang": "zh", "meaning": "splash, scatter", "meaningZh": "水流、散落"},
    {"word": "扑通", "lang": "zh", "meaning": "plop into water", "meaningZh": "落水"},
    {"word": "滴答", "lang": "zh", "meaning": "tick-tock", "meaningZh": "钟表"},
    {"word": "嘶", "lang": "zh", "meaning": "rip, hiss", "meaningZh": "撕裂、嘶鸣"},
    {"word": "ざっ", "lang": "ja", "meaning": "slash", "meaningZh": "斩击"},
    {"word": "どん", "lang": "ja", "meaning": "boom", "meaningZh": "轰响"},
    {"word": "ごごご", "lang": "ja", "meaning": "rising rumble", "meaningZh": "轰鸣渐进"},
    {"word": "ドキッ", "lang": "ja", "meaning": "heart skips", "meaningZh": "心跳"},
    {"word": "BOOM", "lang": "en", "meaning": "explosion", "meaningZh": "爆炸"},
    {"word": "WHOOSH", "lang": "en", "meaning": "fast movement", "meaningZh": "快速移动"},
    {"word": "CRACK", "lang": "en", "meaning": "impact", "meaningZh": "撞击"},
    {"word": "THUD", "lang": "en", "meaning": "heavy hit", "meaningZh": "重击"},
    {"word": "BANG", "lang": "en", "meaning": "gunshot, slammed door", "meaningZh": "枪声、摔门"},
    {"word": "CRASH", "lang": "en", "meaning": "breaking, collision", "meaningZh": "碰撞、破碎"},
    {"word": "SLAM", "lang": "en", "meaning": "door slammed", "meaningZh": "摔门"},
    {"word": "KNOCK", "lang": "en", "meaning": "knock on a door", "meaningZh": "敲门"},
    {"word": "DING", "lang": "en", "meaning": "bell, notification", "meaningZh": "铃声、提示音"},
    {"word": "RING", "lang": "en", "meaning": "phone ringing", "meaningZh": "电话铃"},
    {"word": "BUZZ", "lang": "en", "meaning": "phone vibrating", "meaningZh": "手机震动"},
    {"word": "CLICK", "lang": "en", "meaning": "switch, camera, lock", "meaningZh": "开关、快门、锁"},
    {"word": "SPLASH", "lang": "en", "meaning": "water", "meaningZh": "水花"},
    {"word": "THUMP", "lang": "en", "meaning": "heartbeat, footstep", "meaningZh": "心跳、脚步"},
    {"word": "POW", "lang": "en", "meaning": "punch", "meaningZh": "拳击"},
    {"word": "ZAP", "lang": "en", "meaning": "electric shock", "meaningZh": "电击"},
    {"word": "CREAK", "lang": "en", "meaning": "door, floorboard", "meaningZh": "吱呀声"},
    {"word": "SNAP", "lang": "en", "meaning": "breaking, fingers", "meaningZh": "折断、响指"},
    {"word": "GASP", "lang": "en", "meaning": "sharp breath", "meaningZh": "倒吸一口气"},
    {"word": "SIGH", "lang": "en", "meaning": "sigh", "meaningZh": "叹气"},
]


@router.get("/sfx-presets")
async def sfx_presets():
    return {"presets": _SFX_PRESETS,
            "default_style": {"fontSize": 48, "rotation": -8,
                              "strokeWidth": 4, "strokeColor": "#000000",
                              "color": "#ffffff", "fontWeight": "bold",
                              "textAlign": "center"}}


class ReorderPagesRequest(BaseModel):
    page_ids: List[str]    # full desired order; must be a permutation


@router.put("/projects/{project_id}/pages/order")
@locked_project
def reorder_pages(project_id: str, req: ReorderPagesRequest):
    project = load_or_404(project_id)
    pages = project.setdefault("composerPages", [])
    existing = [p.get("id") for p in pages]
    if sorted(req.page_ids) != sorted(existing):
        raise HTTPException(
            status_code=400,
            detail="page_ids must be a permutation of the existing page ids")
    by_id = {p["id"]: p for p in pages}
    ordered = [by_id[pid] for pid in req.page_ids]
    for i, p in enumerate(ordered):
        p["order"] = i
    project["composerPages"] = ordered
    storage.save_project(project)
    return {"success": True,
            "pages": [{"id": p["id"], "order": p["order"]} for p in ordered]}


# ---------------------------------------------------------------------------
# Element-level CRUD
# ---------------------------------------------------------------------------

_ALLOWED_TYPES = {"image-slot", "image", "page-image", "text", "bubble"}


def _validate_element_payload(el: Dict[str, Any], partial: bool = False) -> None:
    if not partial or "type" in el:
        if el.get("type") not in _ALLOWED_TYPES:
            raise HTTPException(status_code=400,
                                detail=f"type must be one of {sorted(_ALLOWED_TYPES)}")
    for key in ("x", "y", "width", "height"):
        if key in el:
            v = el[key]
            if not isinstance(v, (int, float)) or not (0 <= v <= 100):
                raise HTTPException(status_code=400,
                                    detail=f"{key} must be a number in 0-100")
    if "width" in el and el["width"] <= 0:
        raise HTTPException(status_code=400, detail="width must be > 0")
    if "height" in el and el["height"] <= 0:
        raise HTTPException(status_code=400, detail="height must be > 0")
    if "strokeWidth" in el and el["strokeWidth"] is not None:
        v = el["strokeWidth"]
        if not isinstance(v, (int, float)) or not (0 <= v <= 20):
            raise HTTPException(status_code=400,
                                detail="strokeWidth must be a number in 0-20")
    if "strokeColor" in el and el["strokeColor"] is not None:
        if not isinstance(el["strokeColor"], str):
            raise HTTPException(status_code=400,
                                detail="strokeColor must be a string")
    # optional bubble lettering fields (null clears them)
    if el.get("tailTip") is not None:
        from ...render.bubbles import TIP_MAX, TIP_MIN
        tip = el["tailTip"]
        if not isinstance(tip, dict) or not all(
                isinstance(tip.get(k), (int, float)) and not isinstance(tip.get(k), bool)
                and TIP_MIN <= tip[k] <= TIP_MAX for k in ("x", "y")):
            raise HTTPException(status_code=400,
                                detail=f"tailTip needs numeric x/y (box fractions, "
                                       f"{TIP_MIN:g}..{TIP_MAX:g})")
    if el.get("textAlign") is not None \
            and el["textAlign"] not in ("left", "center", "right", "justify"):
        raise HTTPException(status_code=400,
                            detail="textAlign must be left, center or right")
    if el.get("markup") is not None and el["markup"] != "stars":
        raise HTTPException(status_code=400, detail='markup must be "stars"')
    if "corners" in el and el["corners"] is not None:
        c = el["corners"]
        if not isinstance(c, dict) or set(c.keys()) != {"tl", "tr", "br", "bl"}:
            raise HTTPException(status_code=400,
                                detail="corners needs tl/tr/br/bl")
        for k, pt in c.items():
            if not isinstance(pt, dict) \
                    or not isinstance(pt.get("x"), (int, float)) \
                    or not isinstance(pt.get("y"), (int, float)):
                raise HTTPException(status_code=400,
                                    detail=f"corners.{k} needs numeric x/y")


@router.post("/projects/{project_id}/pages/{page_id}/elements")
@locked_project
def add_element(project_id: str, page_id: str, el: Dict[str, Any]):
    project = load_or_404(project_id)
    page = find_page(project, page_id)
    el = dict(el)
    _validate_element_payload(el)
    el.setdefault("id", uuid.uuid4().hex)
    if any(e.get("id") == el["id"] for e in page["elements"]):
        raise HTTPException(status_code=400, detail="element id already exists")
    el.setdefault("rotation", 0)
    el.setdefault("locked", False)
    el.setdefault("visible", True)
    if "zIndex" not in el:
        el["zIndex"] = max((e.get("zIndex", 0) for e in page["elements"]),
                           default=0) + 1
    if el["type"] == "page-image":
        el.setdefault("frameBorderWidth", 0)   # breakout overlays have no border
    page["elements"].append(el)
    storage.save_project(project)
    return {"success": True, "element": el}


@router.patch("/projects/{project_id}/pages/{page_id}/elements/{element_id}")
@locked_project
def patch_element(project_id: str, page_id: str, element_id: str,
                        updates: Dict[str, Any]):
    project = load_or_404(project_id)
    page = find_page(project, page_id)
    updates = dict(updates)
    if "id" in updates and updates["id"] != element_id:
        raise HTTPException(status_code=400, detail="cannot change element id")
    if "type" in updates:
        raise HTTPException(status_code=400,
                            detail="cannot change element type; delete and re-add")
    updates.pop("id", None)
    _validate_element_payload(updates, partial=True)
    for el in page["elements"]:
        if el.get("id") == element_id:
            el.update(updates)
            storage.save_project(project)
            return {"success": True, "element": el}
    raise HTTPException(status_code=404, detail="Element not found")


@router.delete("/projects/{project_id}/pages/{page_id}/elements/{element_id}")
@locked_project
def delete_element(project_id: str, page_id: str, element_id: str):
    project = load_or_404(project_id)
    page = find_page(project, page_id)
    for el in page["elements"]:
        if el.get("id") == element_id:
            page["elements"].remove(el)
            storage.save_project(project)
            return {"success": True}
    raise HTTPException(status_code=404, detail="Element not found")


@router.get("/templates")
async def list_templates():
    """All build-page templates (builtin + dynamic presets), so agents can
    discover slot counts instead of relying on the AGENTS.md list."""
    from ...composer_builtins import TEMPLATES, DYNAMIC_TEMPLATES, WEBTOON_TEMPLATES

    def brief(t: dict, dynamic: bool) -> dict:
        return {"id": t["id"], "name": t["name"],
                "nameZh": t.get("nameZh", ""),
                "width": t.get("width", 600), "height": t.get("height", 900),
                "slots": sum(1 for e in t["elements"]
                             if e.get("type") == "image-slot"),
                "dynamic": dynamic}

    return {
        "templates": [brief(t, False) for t in TEMPLATES]
                      + [brief(t, False) for t in WEBTOON_TEMPLATES]
                      + [brief(t, True) for t in DYNAMIC_TEMPLATES],
        "other_kinds": {
            "rows": '{"kind":"rows","rows":[2,3]} — n columns per row',
            "custom": '{"kind":"custom","elements":[...],"width":600,"height":900}',
            "user": '{"kind":"user","id":...} — list via GET /api/composer-templates',
        },
    }
