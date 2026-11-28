"""Collection export: every episode of a collection in one PDF and/or ZIP.

Output order: an optional generated collection cover, then per episode an
optional generated title page (collection name, the episode's subtitle, a
thumbnail of its first page) followed by the episode's own pages. Pages are
rendered ONE at a time and streamed into the PDF and ZIP (export_service.
write_export), so memory stays at about one page image (two around a title
page, whose thumbnail reuses the episode's first page) whatever the
collection's length.

Generated pages are ordinary page dicts drawn by the shared renderer and
fonts (titles in font family "title" when data/fonts has one), sized like the
first episode's first page; their font sizes and borders are given for a
600 px page and scaled by width / 600 (Comics imports are 1650 px wide). The
cover shows the chosen page's largest picture without its lettering, or the
whole page when it has no picture. Pre-rendered images (the title page
thumbnail, the cover of a page without a picture) reach the renderer through
a resolver wrapper under THUMB_PREFIX refs.
"""
import logging
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from PIL import Image

from . import render_page as _render_page
from .export_service import ExportOptions, stamp_label, write_export
from ..storage.collections import split_title

logger = logging.getLogger(__name__)

THUMB_PREFIX = "collection-thumb:"
DEFAULT_W, DEFAULT_H = 600, 900

# tests swap this for a slow / counting renderer
render_page = _render_page


def plan_episodes(collection: Dict[str, Any],
                  load_project: Callable[[str], Optional[Dict[str, Any]]]
                  ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Episodes to export, in collection order: [{id, name, title, pages}].
    Missing projects and episodes without pages are skipped with a warning."""
    episodes: List[Dict[str, Any]] = []
    warnings: List[str] = []
    cname = collection.get("name") or ""
    for pid in collection.get("projectIds") or []:
        try:
            project = load_project(pid)
        except Exception as e:   # unreadable / invalid id
            project = None
            logger.warning("collection %s: cannot load project %s: %s",
                           collection.get("id"), pid, e)
        if not project:
            msg = f"project {pid} not found; skipped"
            logger.warning("collection %s: %s", collection.get("id"), msg)
            warnings.append(msg)
            continue
        pages = project.get("composerPages") or []
        name = project.get("name") or pid
        if not pages:
            msg = f"project {name!r} has no pages; skipped"
            logger.warning("collection %s: %s", collection.get("id"), msg)
            warnings.append(msg)
            continue
        base, sub = split_title(name)
        title = sub if sub and base == cname else name
        episodes.append({"id": pid, "name": name, "title": title, "pages": pages})
    return episodes, warnings


def page_total(episodes: List[Dict[str, Any]], cover: bool,
               episode_title_pages: bool) -> int:
    return (1 if cover and episodes else 0) + sum(
        len(e["pages"]) + (1 if episode_title_pages else 0) for e in episodes)


def _text(content: str, y: float, h: float, size: float, *, family: str = "",
          bold: bool = False, color: str = "#111111", stroke: float = 0,
          x: float = 8, w: float = 84) -> Dict[str, Any]:
    return {"id": uuid.uuid4().hex, "type": "text", "content": content,
            "x": x, "y": y, "width": w, "height": h, "rotation": 0,
            "zIndex": 2, "visible": True, "fontSize": size,
            "fontFamily": family, "color": color, "direction": "horizontal",
            "fontWeight": "bold" if bold else "normal", "textAlign": "center",
            "strokeWidth": stroke, "strokeColor": "#000000"}


def _image(ref: str, x: float, y: float, w: float, h: float,
           border: float) -> Dict[str, Any]:
    return {"id": uuid.uuid4().hex, "type": "image", "src": ref,
            "x": x, "y": y, "width": w, "height": h, "rotation": 0,
            "zIndex": 1, "visible": True, "frameBorderWidth": border}


def _page(name: str, w: int, h: int, elements: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"id": uuid.uuid4().hex, "name": name, "width": w, "height": h,
            "elements": elements}


def _render_scaled(page: Dict[str, Any], target_w_px: float, resolve) -> Image.Image:
    """Render a source page so its width is about target_w_px."""
    pw = float(page.get("width") or DEFAULT_W)
    return render_page(page, max(0.05, min(8.0, target_w_px / pw)), resolve)


def _cover_art(page: Dict[str, Any]) -> Optional[str]:
    """src of the page's largest panel picture, so the cover shows the art
    without the page's balloons and captions; None when it has none."""
    best, area = None, 0.0
    for el in page.get("elements") or []:
        if el.get("type") in ("image", "page-image") and el.get("src") \
                and el.get("visible", True):
            a = float(el.get("width") or 0) * float(el.get("height") or 0)
            if a > area:
                best, area = el["src"], a
    return best


def _cover_page(collection: Dict[str, Any], episodes: List[Dict[str, Any]],
                W: int, H: int, scale: float, resolve) -> Tuple[Dict[str, Any], Dict[str, Image.Image]]:
    art_page = None
    chosen = collection.get("cover") or {}
    ep = next((e for e in episodes if e["id"] == chosen.get("projectId")), None)
    if ep is not None:
        art_page = next((p for p in ep["pages"] if p.get("id") == chosen.get("pageId")),
                        ep["pages"][0])
    if art_page is None:
        art_page = episodes[0]["pages"][0]
    k = W / DEFAULT_W
    images: Dict[str, Image.Image] = {}
    ref = _cover_art(art_page)
    if ref is None:   # no picture on the page: the whole rendered page
        ref = THUMB_PREFIX + "cover"
        images[ref] = _render_scaled(art_page, W * scale, resolve)
    els = [_image(ref, 0, 0, 100, 100, 0),
           _text(collection.get("name") or "", 6, 16, 56 * k, family="title",
                 bold=True, color="#ffffff", stroke=6 * k)]
    desc = (collection.get("description") or "").strip()
    if desc:
        els.append(_text(desc, 23, 8, 22 * k, color="#ffffff", stroke=3 * k, x=12, w=76))
    return _page("cover", W, H, els), images


def _title_page(collection: Dict[str, Any], ep: Dict[str, Any], W: int, H: int,
                scale: float, resolve,
                first_img: Optional[Image.Image] = None
                ) -> Tuple[Dict[str, Any], Dict[str, Image.Image]]:
    """first_img: the episode's first page already rendered at full size; the
    thumbnail then reuses it (the renderer scales it into its box) instead of
    rendering the page a second time."""
    first = ep["pages"][0]
    fw = float(first.get("width") or DEFAULT_W)
    fh = float(first.get("height") or DEFAULT_H)
    # thumbnail box: 50% of the page width, aspect kept, at most 55% high
    bw = 50.0
    bh = bw * (fh / fw) * (W / H)
    if bh > 55.0:
        bh = 55.0
        bw = bh * (fw / fh) * (H / W)
    ref = THUMB_PREFIX + "thumb"
    thumb = first_img if first_img is not None else \
        _render_scaled(first, bw / 100 * W * scale, resolve)
    k = W / DEFAULT_W
    els = [_text(collection.get("name") or "", 7, 7, 26 * k, family="title",
                 color="#555555"),
           _text(ep["title"], 15, 14, 44 * k, family="title", bold=True),
           _image(ref, (100 - bw) / 2, 34, bw, bh, 1.5 * k)]
    return _page(f"title: {ep['name']}", W, H, els), {ref: thumb}


def _with_images(resolve, images: Dict[str, Image.Image]):
    def r(url: str):
        if isinstance(url, str) and url.startswith(THUMB_PREFIX):
            return images.get(url)
        return resolve(url) if resolve else None
    return r


def _generated(page: Dict[str, Any], images: Dict[str, Image.Image], scale: float,
               resolve) -> Tuple[Dict[str, Any], Image.Image]:
    return page, render_page(page, scale, _with_images(resolve, images))


def _iter_output(collection, episodes, scale, resolve, cover: bool,
                 episode_title_pages: bool,
                 opts: Optional[ExportOptions] = None
                 ) -> Iterator[Tuple[Dict[str, Any], Image.Image, bool, float]]:
    """(page, rendered image, is_cover, render scale) one at a time."""
    opts = opts or ExportOptions()
    first = episodes[0]["pages"][0]
    W = int(first.get("width") or DEFAULT_W)
    H = int(first.get("height") or DEFAULT_H)
    gs = opts.page_scale({"width": W}, scale)   # generated pages
    if cover:
        yield _generated(*_cover_page(collection, episodes, W, H, gs, resolve),
                         gs, resolve) + (True, gs)
    for ep in episodes:
        first_img = None
        if episode_title_pages:
            fp = ep["pages"][0]
            # rendered once: the title page's thumbnail and the page itself
            first_img = render_page(fp, opts.page_scale(fp, scale), resolve)
            yield _generated(*_title_page(collection, ep, W, H, gs, resolve, first_img),
                             gs, resolve) + (False, gs)
        for j, page in enumerate(ep["pages"]):
            s = opts.page_scale(page, scale)
            if j == 0 and first_img is not None:
                img, first_img = first_img, None
                yield page, img, False, s
            else:
                yield page, render_page(page, s, resolve), False, s
            img = None


def export_collection(collection: Dict[str, Any], episodes: List[Dict[str, Any]],
                      fmt: str, scale: float, resolve, out_dir: Path,
                      base_name: str, *, page_numbers: bool = False,
                      episode_title_pages: bool = True, cover: bool = False,
                      progress: Optional[Callable[[int, int], None]] = None,
                      opts: Optional[ExportOptions] = None) -> dict:
    """Write <base_name>.pdf / .zip under out_dir; returns
    {pages, pdf?: {path, url}, zip?: {path, url}}. Page numbers run across
    the whole collection; the generated cover is page 1 and unnumbered."""
    if fmt not in ("pdf", "zip", "both"):
        raise ValueError(f"unknown export format: {fmt}")
    if not episodes:
        raise ValueError("collection has no episodes with pages to export")
    opts = (opts or ExportOptions()).validate()
    total = page_total(episodes, cover, episode_title_pages)

    def numbered():
        for i, (page, img, is_cover, s) in enumerate(_iter_output(
                collection, episodes, scale, resolve, cover, episode_title_pages, opts)):
            if page_numbers and not is_cover:
                stamp_label(img, str(i + 1), s)
            yield page, img
            img = None

    return write_export(numbered(), fmt, out_dir, base_name, opts,
                        digits=max(3, len(str(total))),
                        progress=(lambda done: progress(done, total)) if progress else None)
