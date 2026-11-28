"""Helpers shared by the agent API modules."""
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import HTTPException
from PIL import Image

from ...render.resolve import make_resolver
from ...storage import asset_store as _astore
from ...storage.manager import storage


def assets():
    """The asset store singleton, looked up per call (it can be swapped:
    tests, AUTOMANGA_DATA_DIR)."""
    return _astore.asset_store


def resolve(url: str) -> Optional[Image.Image]:
    """Image resolver for the renderer, over the current asset store."""
    return make_resolver(assets())(url)


def exports_dir() -> Path:
    """data/exports, shared with /api/export (and its download route)."""
    from .. import export
    return export.EXPORTS_DIR


def load_or_404(project_id: str) -> Dict[str, Any]:
    """The project (an unsafe id raises InvalidProjectIdError: a 400)."""
    project = storage.load_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def find_page(project: Dict[str, Any], page_id: str) -> Dict[str, Any]:
    for page in project.get("composerPages", []):
        if page.get("id") == page_id:
            return page
    raise HTTPException(status_code=404, detail="Page not found")
