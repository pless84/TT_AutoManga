"""Export encoding: JPEG PDF pages, ZIP formats, dpi scaling, title-page reuse."""
import io
import random
import zipfile

import pytest
from PIL import Image

from backend.render import collection_export as ce
from backend.render import export_service as es


def _noisy_page(tmp_path, w=300, h=450):
    """A page whose single image is photo-like noise (compresses badly as
    lossless, like real panel art)."""
    rnd = random.Random(1)
    art = Image.new("RGB", (w, h))
    art.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                 for _ in range(w * h)])
    resolve = lambda ref: art.copy() if ref == "art" else None
    page = {"id": "p", "width": 600, "height": 900, "elements": [
        {"id": "e", "type": "image", "src": "art", "x": 0, "y": 0,
         "width": 100, "height": 100, "rotation": 0, "zIndex": 1, "visible": True}]}
    return page, resolve


def test_jpeg_pdf_embeds_dct_and_is_much_smaller(tmp_path):
    page, resolve = _noisy_page(tmp_path)
    jpeg = es.export_pages_to_bytes([page], "pdf", 1.0, resolve)["pdf"]
    png = es.export_pages_to_bytes([page], "pdf", 1.0, resolve,
                                   opts=es.ExportOptions(image_format="png"))["pdf"]
    assert b"/DCTDecode" in jpeg and b"/DCTDecode" not in png
    assert len(jpeg) < len(png) / 2


def test_pdf_page_size_is_physical_not_pixels(tmp_path):
    page = {"id": "p", "width": 1650, "height": 2550, "elements": []}
    pdf = es.export_pages_to_bytes([page], "pdf", 0.1, None)["pdf"]
    import re
    box = re.search(rb"/MediaBox \[ 0 0 ([\d.]+) ([\d.]+) \]", pdf)
    w, h = float(box.group(1)), float(box.group(2))
    assert w == pytest.approx(6.625 * 72, abs=0.01)
    assert h == pytest.approx(w * 2550 / 1650, abs=0.01)


@pytest.mark.parametrize("fmt,magic", [("png", b"\x89PNG"), ("webp", b"RIFF"),
                                       ("jpeg", b"\xff\xd8")])
def test_zip_formats(fmt, magic):
    page = {"id": "p", "width": 60, "height": 90, "elements": []}
    out = es.export_pages_to_bytes([page, page], "zip", 1.0, None,
                                   opts=es.ExportOptions(zip_format=fmt))
    with zipfile.ZipFile(io.BytesIO(out["zip"])) as zf:
        ext = {"png": "png", "webp": "webp", "jpeg": "jpg"}[fmt]
        assert zf.namelist() == [f"page_001.{ext}", f"page_002.{ext}"]
        assert zf.read(f"page_001.{ext}").startswith(magic)


@pytest.mark.parametrize("kw", [{"image_format": "tiff"}, {"zip_format": "gif"},
                                {"quality": 0}, {"dpi": 10}, {"pdf_width_in": 0}])
def test_invalid_options_rejected(kw):
    with pytest.raises(ValueError):
        es.ExportOptions(**kw).validate()


def test_dpi_gives_every_page_the_same_pixel_width():
    opts = es.ExportOptions(dpi=300)
    small = {"width": 600, "height": 900, "elements": []}
    big = {"width": 1650, "height": 2550, "elements": []}
    for p in (small, big):
        assert round(p["width"] * opts.page_scale(p, 2.0)) == round(300 * 6.625)
    assert es.ExportOptions().page_scale(big, 2.0) == 2.0


def test_title_page_reuses_first_page_render(tmp_path, monkeypatch):
    calls = []
    real = ce.render_page
    monkeypatch.setattr(ce, "render_page",
                        lambda page, scale, resolve: (calls.append(page.get("id")),
                                                      real(page, scale, resolve))[1])
    pages = [{"id": f"p{i}", "width": 60, "height": 90, "elements": []} for i in range(3)]
    eps = [{"id": "e", "name": "S — One", "title": "One", "pages": pages}]
    res = ce.export_collection({"id": "c", "name": "S"}, eps, "zip", 1.0, None,
                               tmp_path, "collection_c_20260101_000000",
                               episode_title_pages=True)
    assert res["pages"] == 4
    # p0 rendered once (thumbnail + page), plus the generated title page
    assert sorted(c for c in calls if c != "p0" and c.startswith("p")) == ["p1", "p2"]
    assert calls.count("p0") == 1


def test_export_endpoint_rejects_bad_options():
    from fastapi.testclient import TestClient
    from backend.main import app
    c = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    r = c.post("/api/render/export", json={
        "pages": [{"width": 60, "height": 90, "elements": []}],
        "imageFormat": "tiff"})
    assert r.status_code == 400 and "imageFormat" in r.text
