"""Per-project locking for API handlers that load, modify and save a project."""
import functools

from ..storage.manager import storage


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
        with storage.lock(pid):   # an unsafe id: InvalidProjectIdError, a 400
            return handler(*args, **kwargs)
    return wrapper
