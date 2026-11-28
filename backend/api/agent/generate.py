"""Agent API: image generation (single + batch jobs), job status,
installed models and asset metadata. The work is
backend/services/generation.py."""
import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...core.swarmui_client import swarm_client
from ...jobs import create_job, job_snapshot, run_job
from ...services import generation
from ...storage.manager import storage, InvalidProjectIdError
from ._common import assets

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Image generation (single + batch)
# ---------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    project_id: Optional[str] = None
    panel_id: Optional[str] = None
    prompt: str
    negative_prompt: str = ""
    width: Optional[int] = None
    height: Optional[int] = None
    steps: Optional[int] = None
    cfg_scale: Optional[float] = None
    seed: Optional[int] = None
    remove_background: bool = False
    variants: int = 1                  # 1-4
    apply_variant: int = 0             # which variant (0-based) binds to the panel
    apply_to_panel: bool = True
    kind: str = "panel"                # panel | overlay | character
    # --- per-request overrides (fall back to global settings) ---
    model: Optional[str] = None
    loras: Optional[List[Dict[str, Any]]] = None
    prompt_format: Optional[str] = None   # danbooru | prose | None (meta only)
    # --- img2img ---
    init_image: Optional[str] = None      # asset URL / data URL
    init_creativity: Optional[float] = None
    mask_image: Optional[str] = None      # inpaint mask (with init_image);
                                          # semantics follow SwarmUI
    # --- automatic prompt injection (project style + character blocks) ---
    inject_characters: bool = True


@router.post("/generate")
async def agent_generate(req: GenerateRequest):
    logger.info("agent/generate prompt_len=%d variants=%d rm_bg=%s panel=%s",
                len(req.prompt), req.variants, req.remove_background,
                req.panel_id)
    try:
        return await generation.generate(req.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except generation.GenerationError as e:
        raise HTTPException(status_code=502, detail=str(e))


class GenerateBatchRequest(BaseModel):
    project_id: Optional[str] = None
    concurrency: int = 1
    jobs: List[GenerateRequest]


# Strong references to running batch jobs (asyncio GC protection)
_background_tasks = set()


@router.post("/generate-batch")
async def agent_generate_batch(req: GenerateBatchRequest):
    if not req.jobs:
        raise HTTPException(status_code=400, detail="No jobs provided")
    payloads = []
    for j in req.jobs:
        p = j.model_dump()
        if req.project_id and not p.get("project_id"):
            p["project_id"] = req.project_id
        payloads.append(p)
    # a mature job whose prompt mentions a minor is refused up front (400),
    # not queued to fail; generation.generate checks every job again when it runs
    from ...content_rating import RatingRefused, check_prompt, project_rating
    ratings: Dict[str, str] = {}
    for i, p in enumerate(payloads):
        pid = p.get("project_id") or ""
        if pid not in ratings:
            try:
                ratings[pid] = project_rating(storage.load_project(pid) if pid else None)
            except InvalidProjectIdError as e:
                raise HTTPException(status_code=400, detail=f"job {i}: {e}")
        try:
            check_prompt(p.get("prompt") or "", ratings[pid])
        except RatingRefused as e:
            raise HTTPException(status_code=400, detail=f"job {i}: {e}")
    job = create_job(payloads, req.concurrency)
    task = asyncio.create_task(run_job(job.id, generation.generate))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return JSONResponse(status_code=202,
                        content={"job_id": job.id, "total": job.total})


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    snap = job_snapshot(job_id)
    if not snap:
        raise HTTPException(status_code=404, detail="Job not found")
    return snap


@router.get("/models")
async def list_models():
    """Installed SwarmUI models with architecture metadata, for per-request
    model selection. See the prompt-format guide in AGENTS.md."""
    models = await swarm_client.list_models()
    if not models:
        return {"models": [], "error": "SwarmUI unreachable or no models found"}
    return {"models": models}


@router.get("/assets/{asset_id}")
async def get_asset(asset_id: str):
    """Generation-time metadata for one asset (prompt, seed, model, alpha
    method...) — the agent's memory when it needs to reproduce or tweak."""
    entry = assets().get(asset_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Asset not found")
    return {"asset": entry,
            "url": assets().url_for(asset_id),
            "file_exists": Path(entry.get("path", "")).is_file()}
