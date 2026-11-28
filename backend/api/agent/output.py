"""Agent API: page preview, local upscale and project export."""
import asyncio
import base64
import io
import logging
import time
from typing import Any, Dict

import httpx
from fastapi import APIRouter, HTTPException, Response
from PIL import Image
from pydantic import BaseModel

from ...render import render_page
from ...render.export_service import export_pages
from ...services.generation import ref_to_dataurl
from ._common import assets, exports_dir, find_page, load_or_404, resolve

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Preview & export
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/pages/{page_id}/preview.png")
async def page_preview(project_id: str, page_id: str, scale: float = 2.0):
    project = await asyncio.to_thread(load_or_404, project_id)
    page = find_page(project, page_id)
    scale = min(8.0, max(0.5, scale))
    # CPU-bound PIL work: keep the event loop (and every other request) free
    png = await asyncio.to_thread(_render_png, page, scale)
    return Response(content=png, media_type="image/png")


def _render_png(page: Dict[str, Any], scale: float) -> bytes:
    img = render_page(page, scale, resolve)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Local upscale (optional onnxruntime; no SwarmUI needed)
# ---------------------------------------------------------------------------

class UpscaleRequest(BaseModel):
    asset_url: str                     # /api/assets/file/... or data: URL
    model: str = "realesrgan-anime-6b"
    tile: int = 512                    # tile edge in px; memory/speed knob


@router.get("/inference-models")
async def list_inference_models():
    """Local ONNX upscale models + whether onnxruntime is installed."""
    from ...inference import INFERENCE_MODELS, available
    return {"available": available(),
            "models": [{"id": mid, "scale": m["scale"],
                        "description": m["description"]}
                       for mid, m in INFERENCE_MODELS.items()]}


@router.post("/upscale")
async def upscale_asset(req: UpscaleRequest):
    """Upscale an existing asset with a local ONNX model (x4) and register
    the result as a new asset (kind="upscale")."""
    from ...inference import InferenceUnavailable, ModelError, upscale_bytes
    src = await asyncio.to_thread(ref_to_dataurl, req.asset_url)
    if not src:
        raise HTTPException(status_code=400,
                            detail="asset_url must be an asset URL or data: URL")
    try:
        raw = base64.b64decode(src.split(",", 1)[1])
        out = await asyncio.to_thread(upscale_bytes, raw, req.model, req.tile)
    except InferenceUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ModelError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except httpx.HTTPError as e:   # model download transport failure
        raise HTTPException(status_code=502, detail=f"model download failed: {e}")
    except (ValueError, OSError) as e:   # UnidentifiedImageError is an OSError
        raise HTTPException(status_code=400, detail=f"invalid image: {e}")

    img = Image.open(io.BytesIO(out))
    from ...inference import INFERENCE_MODELS
    factor = INFERENCE_MODELS[req.model]["scale"]
    entry = await asyncio.to_thread(
        assets().save_bytes, out, ext="png", kind="upscale",
        meta={"source_asset": req.asset_url, "model": req.model,
              "scale": factor, "width": img.size[0], "height": img.size[1]})
    logger.info("agent/upscale model=%s %dx%d -> %dx%d", req.model,
                img.size[0] // factor, img.size[1] // factor,
                img.size[0], img.size[1])
    return {"asset_id": entry["asset_id"],
            "url": assets().url_for(entry["asset_id"]),
            "width": img.size[0], "height": img.size[1],
            "model": req.model}


class AgentExportRequest(BaseModel):
    project_id: str
    format: str = "both"       # pdf | zip | both
    scale: float = 2.0
    page_numbers: bool = False # stamp page numbers (cover stays unnumbered)


@router.post("/export")
async def agent_export(req: AgentExportRequest):
    project = await asyncio.to_thread(load_or_404, req.project_id)
    pages = project.get("composerPages") or []
    if not pages:
        raise HTTPException(status_code=400,
                            detail="project has no composer pages; build pages first")
    if req.format not in ("pdf", "zip", "both"):
        raise HTTPException(status_code=400, detail="format must be pdf|zip|both")
    fmt = req.format
    base_name = f"manga_{req.project_id[:8]}_{time.strftime('%Y%m%d_%H%M%S')}"
    # minutes of PIL/reportlab work for a long project: run it in a thread
    result = await asyncio.to_thread(
        export_pages, pages, fmt, min(8.0, max(0.5, req.scale)),
        resolve, exports_dir(), base_name,
        page_numbers=req.page_numbers)
    logger.info("agent/export project=%s fmt=%s base=%s", req.project_id, fmt,
                base_name)
    return result
