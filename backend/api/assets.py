import asyncio
import logging

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
from ..core.swarmui_client import swarm_client
from ..core.config import settings
from ..storage.asset_store import asset_store
from ..services import generation
from ..services.generation import bind_panel_image

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/assets", tags=["assets"])


class GenerateImageRequest(BaseModel):
    prompt: str
    negative_prompt: str = ""
    seed: Optional[int] = None
    panel_id: str
    project_id: str
    swarmui_address: Optional[str] = None
    remove_background: bool = False


class SwarmUISettingsRequest(BaseModel):
    model: Optional[str] = None
    preset: Optional[str] = None
    loras: Optional[List[Dict[str, Any]]] = None
    steps: Optional[int] = None
    cfg_scale: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    sampler_name: Optional[str] = None


class UploadRequest(BaseModel):
    project_id: str
    panel_id: str


@router.get("/file/{asset_id}")
async def get_asset_file(asset_id: str):
    path = asset_store.path_for(asset_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    suffix = path.suffix.lstrip(".").lower()
    media = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
             "webp": "image/webp"}.get(suffix, "application/octet-stream")
    return FileResponse(
        str(path),
        media_type=media,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.post("/file")
async def register_asset(
    file: UploadFile = File(...),
    kind: str = Form("upload"),
    project_id: Optional[str] = Form(None),
):
    """Register arbitrary image bytes as a reusable asset (e.g. agent-side
    post-processing output) without binding it to a panel."""
    contents = await file.read()
    ext = (file.content_type or "image/png").split("/")[-1]
    entry = await asyncio.to_thread(asset_store.save_bytes, contents, ext=ext,
                                    project_id=project_id or None, kind=kind)
    return JSONResponse({
        "success": True,
        "asset_id": entry["asset_id"],
        "url": asset_store.url_for(entry["asset_id"]),
    })


@router.post("/upload/{project_id}/{panel_id}")
async def upload_image(project_id: str, panel_id: str, file: UploadFile = File(...)):
    contents = await file.read()
    ext = (file.content_type or "image/png").split("/")[-1]
    entry = await asyncio.to_thread(asset_store.save_bytes, contents, ext=ext,
                                    project_id=project_id, kind="upload")
    url = asset_store.url_for(entry["asset_id"])
    await asyncio.to_thread(bind_panel_image, project_id, panel_id, url)
    return JSONResponse({
        "success": True,
        "image": url,
        "asset_id": entry["asset_id"],
    })


@router.post("/swarmui/generate")
async def generate_with_swarmui(req: GenerateImageRequest):
    override = None
    if req.swarmui_address:
        # one-shot override: http(s) on loopback / the LAN only (no SSRF to
        # arbitrary hosts); the configured address itself is always fine
        from ..core.swarmui_client import _normalize_address, check_override_address
        if _normalize_address(req.swarmui_address) == swarm_client.address:
            override = swarm_client.address
        else:
            try:
                override = await asyncio.to_thread(check_override_address, req.swarmui_address)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

    # the shared generation path (backend/services/generation.py) with the
    # global settings; the Assets tab's prompt already carries style and
    # character blocks, so only the panel characters' LoRAs, the IP-Adapter
    # sheet and the content rating are added. An unknown project just
    # paints the prompt as sent (not bound to any panel).
    payload = {"prompt": req.prompt, "negative_prompt": req.negative_prompt,
               "seed": req.seed, "project_id": req.project_id, "panel_id": req.panel_id,
               "remove_background": req.remove_background, "kind": "panel"}
    logger.info("[Generate] panel=%s prompt_len=%d seed=%s rm_bg=%s", req.panel_id,
                len(req.prompt), req.seed, req.remove_background)
    try:
        out = await generation.generate(payload, prompt_blocks=False, strict_project=False,
                                        address=override)
    except ValueError as e:   # RatingRefused: a mature prompt naming a minor
        raise HTTPException(status_code=400, detail=str(e))
    except generation.GenerationError as e:
        # the Assets tab shows `error` (HTTP 200, as SwarmUI failures always were)
        return {"success": False, "error": str(e).removeprefix("SwarmUI: "),
                "content_rating": e.rating}
    asset = out["assets"][0]
    result = {"success": True, "image": asset["url"], "asset_id": asset["asset_id"],
              "path": asset["url"], "content_rating": out["content_rating"]}
    if out.get("notice"):
        result["notice"] = out["notice"]
    return result


@router.get("/swarmui/models")
async def get_swarmui_models():
    try:
        models = await swarm_client.get_models()
        return {"models": models}
    except Exception as e:
        return {"models": [], "error": str(e)}


@router.get("/swarmui/loras")
async def get_swarmui_loras():
    """Installed LoRAs for the Characters tab: [{name, trigger, architecture}],
    the name as SwarmUI's <lora:> tag wants it (relative to Models/Lora, no
    extension), the trigger from the model's metadata or the name convention."""
    from ..prompting import default_trigger, lora_base_name
    try:
        files = await swarm_client.list_models(subtype="LoRA")
    except Exception as e:
        return {"loras": [], "error": str(e)}
    out = []
    for f in files:
        name = lora_base_name(f.get("name") or "")
        if not name:
            continue
        trigger = (f.get("trigger_phrase") or "").strip() or default_trigger(name)
        out.append({"name": name, "trigger": trigger, "architecture": f.get("architecture") or ""})
    return {"loras": out}


@router.get("/swarmui/test")
async def test_swarmui_connection():
    try:
        result = await swarm_client.check_connection()
        return result
    except Exception as e:
        return {"connected": False, "status": str(e)}


# SwarmUI's optional features, probed at most once a minute (the Characters
# tab asks on every mount; the backend list rarely changes)
_FEATURES_TTL = 60.0
_features_cache: Dict[str, Any] = {"at": 0.0, "address": None, "value": None}


@router.get("/swarmui/features")
async def get_swarmui_features():
    """``{"ipadapter": bool, "presets": [str], "preset": str}``: whether a
    running SwarmUI backend has the IP-Adapter nodes (character-sheet
    references), its "Use IP-Adapter" presets and the configured one."""
    import time
    now = time.monotonic()
    address = swarm_client.address
    cached = _features_cache["value"]
    if cached is None or _features_cache["address"] != address or now - _features_cache["at"] > _FEATURES_TTL:
        try:
            cached = await swarm_client.ipadapter_features()
        except Exception:
            cached = {"ipadapter": False, "presets": []}
        _features_cache.update(at=now, address=address, value=cached)
    return {**cached, "preset": settings.swarmui.ipadapterPreset}
