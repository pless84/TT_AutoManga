"""Agent orchestration API (/api/agent/*).

The machine-facing half of the pipeline, contract in AGENTS.md:

- ``projects``: project/panel CRUD, the storyboard LLM call, generation
  defaults (/settings) and project settings;
- ``generate``: image generation (single + batch jobs), jobs, models, asset
  metadata (the work itself: backend/services/generation.py);
- ``pages``: page assembly, plan, cover, order, templates, SFX presets and
  element-level editing;
- ``output``: server-side preview, local upscale and export.

Shared helpers live in ``_common``. Every handler that loads, modifies and
saves a project is a ``@locked_project`` threadpool ``def``.
"""
from fastapi import APIRouter

# the singletons the modules use, for callers that patch their attributes
from ...core.config import settings
from ...core.openai_client import client as llm_client
from ...core.swarmui_client import swarm_client
from . import generate, output, pages, projects

__all__ = ["router", "settings", "llm_client", "swarm_client"]

router = APIRouter(prefix="/api/agent", tags=["agent"])
for _module in (projects, generate, pages, output):
    router.include_router(_module.router)
