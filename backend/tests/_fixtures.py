"""Shared test fixtures (in-memory PNG data URLs, a long-lived TestClient loop)."""
import base64
import contextlib
import io
from PIL import Image, ImageDraw


def to_dataurl(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def solid_png(color, size=(400, 400)) -> str:
    return to_dataurl(Image.new("RGB", size, color))


def solid_rgba_png(color, size=(400, 400)) -> str:
    return to_dataurl(Image.new("RGBA", size, color))


def transparent_circle_png(size=(200, 200)) -> str:
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([20, 20, size[0] - 20, size[1] - 20], fill=(255, 0, 0, 160))
    return to_dataurl(img)


@contextlib.contextmanager
def live_loop(client):
    """Keep one event loop running across TestClient requests (without the
    app lifespan), as under uvicorn: background tasks such as batch
    generation jobs, which await worker threads, keep running between
    requests instead of dying with each request's loop."""
    import anyio.from_thread
    with anyio.from_thread.start_blocking_portal() as portal:
        client.portal = portal
        try:
            yield client
        finally:
            client.portal = None
