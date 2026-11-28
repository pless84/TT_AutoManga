"""Per-project locking for API handlers that load, modify and save a project."""
import functools

from fastapi import HTTPException

from ..storage.manager import InvalidProjectIdError, storage


def locked_project(handler):
    """Run a sync (threadpool) handler holding its project's lock, taken
    from the ``project_id`` path parameter or the body's ``id``/
    ``project_id``: its load-modify-save cannot interleave with another
    writer of the same project."""
    @functools.wraps(handler)
    def wrapper(*args, **kwargs):
        pid = kwargs.get("project_id")
        if pid is None:
            for v in kwargs.values():
                pid = getattr(v, "project_id", None) or getattr(v, "id", None)
                if pid:
                    break
        if not pid:
            return handler(*args, **kwargs)
        try:
            lk = storage.lock(pid)
        except InvalidProjectIdError as e:
            raise HTTPException(status_code=400, detail=str(e))
        with lk:
            return handler(*args, **kwargs)
    return wrapper
