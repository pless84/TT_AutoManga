import hashlib
import json
import logging
import re
import shutil
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, List, Dict, Any, Iterator, Tuple
from ..core.config import PROJECTS_DIR, atomic_write_json

logger = logging.getLogger(__name__)

# Client-supplied ids become directory names under data/projects — only
# [A-Za-z0-9_-] is allowed so "../" and friends can never escape it.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def pages_base(project: Dict[str, Any]) -> str:
    """Version token of a project's composer pages (optimistic concurrency
    for the Composer autosave): sha1 of the canonical JSON of composerPages."""
    pages = project.get("composerPages", []) if project else []
    blob = json.dumps(pages, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


class InvalidProjectIdError(ValueError):
    """Raised when a project id contains characters unsafe for a path."""


def validate_project_id(project_id: str) -> str:
    if not isinstance(project_id, str) or not _SAFE_ID_RE.fullmatch(project_id):
        raise InvalidProjectIdError(
            f"invalid project id {project_id!r}: only letters, digits, "
            "'_' and '-' are allowed")
    return project_id


class ProjectNotFoundError(LookupError):
    """edit_project() on a project that does not exist."""


class StaleVersionError(RuntimeError):
    """A save carried an older ``version`` than the stored project."""

    def __init__(self, current: int):
        super().__init__(f"project changed elsewhere (now version {current})")
        self.current = current


# One re-entrant lock per project id: every load-modify-save of a project
# holds it, so two writers (agent PATCH, batch generation, Composer autosave)
# can never interleave and silently drop each other's change. Handlers that
# touch project files run in the threadpool (plain `def`), never holding the
# lock across an await.
_locks: Dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()

# list_projects cache: project dir name -> ((mtime_ns, size), parsed JSON)
_list_cache: Dict[str, Tuple[Tuple[int, int], Dict[str, Any]]] = {}
_list_cache_guard = threading.Lock()
# project id -> ((mtime_ns, size), version) of the file this process wrote
_version_cache: Dict[str, Tuple[Tuple[int, int], int]] = {}


class StorageManager:
    @staticmethod
    def lock(project_id: str) -> threading.RLock:
        validate_project_id(project_id)
        with _locks_guard:
            lk = _locks.get(project_id)
            if lk is None:
                lk = _locks[project_id] = threading.RLock()
            return lk

    @staticmethod
    @contextmanager
    def edit_project(project_id: str) -> Iterator[Dict[str, Any]]:
        """``with storage.edit_project(pid) as project: ...`` loads the
        project under its lock and saves it when the block ends normally
        (an exception inside the block discards the change). Raises
        ProjectNotFoundError / InvalidProjectIdError."""
        with StorageManager.lock(project_id):
            project = StorageManager.load_project(project_id)
            if project is None:
                raise ProjectNotFoundError(project_id)
            yield project
            StorageManager.save_project(project)

    @staticmethod
    def _migrate_flat_project_file() -> None:
        flat_file = PROJECTS_DIR / "project.json"
        if flat_file.exists():
            try:
                with open(flat_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                new_id = data.get("id") or str(uuid.uuid4())
                if not _SAFE_ID_RE.fullmatch(str(new_id)):
                    new_id = str(uuid.uuid4())
                data["id"] = new_id
                proj_dir = PROJECTS_DIR / new_id
                proj_dir.mkdir(exist_ok=True)
                atomic_write_json(proj_dir / "project.json", data, indent=2)
                flat_file.unlink()
            except Exception as e:
                logger.warning("flat project migration failed: %s", e)

    @staticmethod
    def list_projects() -> List[Dict[str, Any]]:
        """Every project, newest first. Parsed files are cached by mtime and
        size, so a listing only re-reads projects that changed. The returned
        dicts are shared with the cache: treat them as read-only (load_project
        returns a private copy for editing)."""
        StorageManager._migrate_flat_project_file()
        projects = []
        seen = set()
        for proj_dir in PROJECTS_DIR.iterdir():
            if not proj_dir.is_dir():
                continue
            meta_file = proj_dir / "project.json"
            try:
                st = meta_file.stat()
            except FileNotFoundError:
                continue
            key = (st.st_mtime_ns, st.st_size)
            seen.add(proj_dir.name)
            with _list_cache_guard:
                hit = _list_cache.get(proj_dir.name)
            if hit is not None and hit[0] == key:
                projects.append(hit[1])
                continue
            # one damaged project must not hide all the others
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                logger.warning("skipping unreadable %s: %s", meta_file, e)
                continue
            if not isinstance(data, dict):
                logger.warning("skipping %s: not a JSON object", meta_file)
                continue
            with _list_cache_guard:
                _list_cache[proj_dir.name] = (key, data)
            projects.append(data)
        with _list_cache_guard:
            for name in [n for n in _list_cache if n not in seen]:
                del _list_cache[name]
        return sorted(projects, key=lambda x: x.get("updatedAt", ""), reverse=True)

    @staticmethod
    def _normalize_image_refs(project: Dict[str, Any]) -> None:
        """Rewrite embedded data-URL images as asset files, referenced by URL."""
        from .asset_store import asset_store
        pid = project.get("id")

        def norm(v, kind):
            if isinstance(v, str) and v.startswith("data:"):
                entry = asset_store.save_dataurl(v, project_id=pid, kind=kind)
                return asset_store.url_for(entry["asset_id"])
            return v

        for panel in project.get("panels", []):
            if panel.get("image"):
                panel["image"] = norm(panel["image"], "panel")
        for page in project.get("composerPages", []):
            for el in page.get("elements", []):
                if el.get("src"):
                    el["src"] = norm(el["src"], "composer")
                if el.get("originalSrc"):
                    el["originalSrc"] = norm(el["originalSrc"], "composer")

    @staticmethod
    def save_project(project_data: Dict[str, Any],
                     expected_version: Optional[int] = None) -> Dict[str, Any]:
        """Write the project and bump its ``version`` (a save counter).
        expected_version: refuse with StaleVersionError when the stored
        project's version differs (optimistic concurrency for clients that
        send the whole project)."""
        project_id = project_data.get("id") or str(uuid.uuid4())
        validate_project_id(project_id)
        project_data["id"] = project_id

        with StorageManager.lock(project_id):
            proj_dir = PROJECTS_DIR / project_id
            proj_dir.mkdir(exist_ok=True)
            current = StorageManager._stored_version(proj_dir / "project.json")
            if expected_version is not None and expected_version != current:
                raise StaleVersionError(current)

            from datetime import datetime, timezone
            project_data["updatedAt"] = datetime.now(timezone.utc).isoformat()
            project_data["version"] = current + 1

            StorageManager._normalize_image_refs(project_data)

            meta_file = proj_dir / "project.json"
            atomic_write_json(meta_file, project_data, indent=2)
            st = meta_file.stat()
            with _list_cache_guard:
                _version_cache[project_id] = ((st.st_mtime_ns, st.st_size),
                                              project_data["version"])

        return project_data

    @staticmethod
    def _stored_version(meta_file: Path) -> int:
        """Version of the project file on disk (0 when absent/unversioned).
        Taken from the list cache when the file is unchanged since, so a
        save does not parse the whole project a second time."""
        try:
            st = meta_file.stat()
        except FileNotFoundError:
            return 0
        key = (st.st_mtime_ns, st.st_size)
        with _list_cache_guard:
            known = _version_cache.get(meta_file.parent.name)
            hit = _list_cache.get(meta_file.parent.name)
        if known is not None and known[0] == key:
            return known[1]
        if hit is not None and hit[0] == key:
            data = hit[1]
        else:
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                return 0
        v = data.get("version") if isinstance(data, dict) else None
        return v if isinstance(v, int) and v >= 0 else 0

    @staticmethod
    def load_project(project_id: str) -> Optional[Dict[str, Any]]:
        validate_project_id(project_id)
        proj_dir = PROJECTS_DIR / project_id
        meta_file = proj_dir / "project.json"

        with StorageManager.lock(project_id):
            if meta_file.exists():
                with open(meta_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        return None

    @staticmethod
    def delete_project(project_id: str) -> bool:
        validate_project_id(project_id)
        proj_dir = PROJECTS_DIR / project_id
        with StorageManager.lock(project_id):
            if not proj_dir.exists():
                return False
            shutil.rmtree(proj_dir)
        # a deleted project is no longer an episode of any collection
        from .collections import remove_project
        remove_project(project_id)
        return True


storage = StorageManager()
