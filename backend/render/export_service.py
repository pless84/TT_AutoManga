"""Shared page-export service.

Two entry points over the same renderer:
- export_pages_to_bytes: in-memory PDF/ZIP (used by POST /api/render/export)
- export_pages: writes files under data/exports and returns download URLs
  (used by POST /api/agent/export)
Collection export (collection_export.py) feeds write_export as well.

PDF pages are ExportOptions.pdf_width_in wide (height by aspect); page images
are JPEG by default (see ExportOptions).
"""
import io
import logging
import re
import shutil
import time
import zipfile
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, List, Optional, Tuple
from PIL import Image
from . import render_page

# Maximum viewer compatibility: embed images without an ASCII85 layer. Some
# PDF viewers render A85+Flate filter arrays as blank pages.
from reportlab import rl_config as _rl_config
_rl_config.useA85 = 0

logger = logging.getLogger(__name__)

# data/exports pruning: every export writes <base>.pdf/.zip (+ <base>_pages/);
# on each new export, ones older than this go, and of the rest at most
# EXPORT_KEEP_PER_PROJECT newest per project stay.
EXPORT_MAX_AGE_DAYS = 7
EXPORT_KEEP_PER_PROJECT = 20

# <kind>[_<project id>]_<timestamp or 8 hex>: manga_<id8>_20261117_073326
# (agent export), manga_<project id>_ab12cd34 (/api/export/pdf|images),
# composer_ab12cd34 (/api/export/composer-*), collection_<collection id8>_
# 20261117_073326 (collection export). Anything else is left alone.
_EXPORT_STEM_RE = re.compile(
    r"(?P<kind>[a-z]+)(?:_(?P<pid>[\w-]+?))?_(?:\d{8}_\d{6}|[0-9a-f]{8})")


def export_group(stem: str) -> Optional[str]:
    """The project an export stem belongs to ("manga:67537cf6", "composer:"),
    None for a name no export writes. Agent exports carry only the first 8
    characters of the project id, so the full-id ones are grouped by those."""
    m = _EXPORT_STEM_RE.fullmatch(stem)
    if not m:
        return None
    return f"{m.group('kind')}:{(m.group('pid') or '')[:8]}"


def _export_stem(p: Path) -> Optional[str]:
    if p.is_symlink():
        return None
    if p.is_dir() and p.name.endswith("_pages"):
        return p.name[:-len("_pages")]
    if p.is_file() and p.suffix.lower() in (".pdf", ".zip"):
        return p.stem
    return None


def prune_exports(out_dir: Path, keep: Iterable[str] = (),
                  max_age_days: float = EXPORT_MAX_AGE_DAYS,
                  keep_per_project: int = EXPORT_KEEP_PER_PROJECT,
                  now: Optional[float] = None) -> List[str]:
    """Delete exports (PDF, ZIP and _pages/ folder of one stem together) older
    than max_age_days, and beyond the keep_per_project newest of each
    project. Stems in ``keep`` (the export just written) always stay. Never
    raises: a failed prune must not fail the export. Returns deleted names."""
    deleted: List[str] = []
    try:
        if not out_dir.is_dir():
            return deleted
        now = time.time() if now is None else now
        keep = set(keep)
        stems: dict = {}   # stem -> [paths]
        for p in out_dir.iterdir():
            stem = _export_stem(p)
            if stem is not None and export_group(stem) is not None:
                stems.setdefault(stem, []).append(p)
        groups: dict = {}  # group -> [(mtime, stem)]
        for stem, paths in stems.items():
            mtime = max(p.stat().st_mtime for p in paths)
            groups.setdefault(export_group(stem), []).append((mtime, stem))
        cutoff = now - max_age_days * 86400
        for entries in groups.values():
            entries.sort(reverse=True)
            for i, (mtime, stem) in enumerate(entries):
                if stem in keep or (i < keep_per_project and mtime >= cutoff):
                    continue
                for p in stems[stem]:
                    try:
                        if p.is_dir():
                            shutil.rmtree(p)
                        else:
                            p.unlink()
                        deleted.append(p.name)
                    except OSError as e:
                        logger.warning("prune exports: could not delete %s: %s", p.name, e)
        if deleted:
            logger.info("pruned %d old export file(s)/folder(s)", len(deleted))
    except OSError as e:
        logger.warning("prune exports failed: %s", e)
    return deleted


def _stamp_page_number(img: Image.Image, index: int, scale: float) -> None:
    """Number a page bottom-center; page 1 (the cover) stays unnumbered."""
    if index == 0:
        return
    stamp_label(img, str(index + 1), scale)


def stamp_label(img: Image.Image, label: str, scale: float) -> None:
    """Draw a page number (any label) bottom-center."""
    from PIL import ImageDraw
    from .fonts import get_font
    d = ImageDraw.Draw(img)
    fs = max(10, int(14 * scale))
    font = get_font(fs)
    w = font.getlength(label)
    d.text(((img.width - w) / 2, img.height - fs * 1.8), label,
           font=font, fill=(102, 102, 102, 255))


def _stamp_page_numbers(images: List[Image.Image], scale: float) -> None:
    for i, img in enumerate(images):
        _stamp_page_number(img, i, scale)


# ------------------------------------------------------------------ options

IMAGE_FORMATS = ("jpeg", "png")          # PDF page images
ZIP_FORMATS = ("png", "webp", "jpeg")    # ZIP page files
DEFAULT_PDF_WIDTH_IN = 6.625             # US comic trim width


@dataclass
class ExportOptions:
    """How exported pages are encoded.

    image_format: PDF page images. "jpeg" (default) embeds the JPEG bytes as
      they are (DCTDecode), about 5-10x smaller than "png" (lossless Flate).
    quality: JPEG quality 1-100 (PDF pages, and ZIP files when zip_format is
      jpeg).
    zip_format: ZIP page files: "png" (default, lossless), "webp" (lossless,
      ~30% smaller) or "jpeg".
    dpi: when set, every page is rendered at the scale that gives this many
      pixels per inch at pdf_width_in, whatever its own pixel width (a 600 px
      and a 1650 px page come out the same size); overrides scale.
    pdf_width_in: physical PDF page width in inches; height keeps the page's
      aspect ratio.
    """
    image_format: str = "jpeg"
    quality: int = 85
    zip_format: str = "png"
    dpi: Optional[float] = None
    pdf_width_in: float = DEFAULT_PDF_WIDTH_IN

    def validate(self) -> "ExportOptions":
        if self.image_format not in IMAGE_FORMATS:
            raise ValueError(f"imageFormat must be one of {'|'.join(IMAGE_FORMATS)}")
        if self.zip_format not in ZIP_FORMATS:
            raise ValueError(f"zipFormat must be one of {'|'.join(ZIP_FORMATS)}")
        if not 1 <= int(self.quality) <= 100:
            raise ValueError("quality must be 1-100")
        if self.dpi is not None and not 36 <= float(self.dpi) <= 1200:
            raise ValueError("dpi must be 36-1200")
        if not 1 <= float(self.pdf_width_in) <= 40:
            raise ValueError("pdfWidthIn must be 1-40")
        return self

    def page_scale(self, page: dict, scale: float) -> float:
        """Render scale for one page: from dpi when set, else ``scale``."""
        if self.dpi is None:
            return scale
        pw = float(page.get("width") or 600)
        return max(0.05, min(8.0, float(self.dpi) * self.pdf_width_in / pw))


def _encode(img: Image.Image, fmt: str, quality: int) -> bytes:
    b = io.BytesIO()
    if fmt == "jpeg":
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(b, format="JPEG", quality=int(quality), optimize=True,
                 subsampling="4:2:0")
    elif fmt == "webp":
        img.save(b, format="WEBP", lossless=True, method=4)
    else:
        img.save(b, format="PNG")
    return b.getvalue()


_EXT = {"jpeg": "jpg", "png": "png", "webp": "webp"}


class PdfWriter:
    """Incremental PDF: each page is embedded as soon as it is rendered.

    Pages are pdf_width_in wide (height by aspect). JPEG page images are
    embedded as-is (DCTDecode); PNG ones are decoded and Flate-compressed."""

    def __init__(self, target, opts: Optional[ExportOptions] = None):
        from reportlab.pdfgen import canvas as rl_canvas
        self._rl_canvas = rl_canvas
        self.target = target
        self.opts = opts or ExportOptions()
        self.c = None

    def add(self, img: Image.Image, page: dict,
            encoded: Optional[bytes] = None) -> None:
        from reportlab.lib.utils import ImageReader
        pw = float(page.get("width") or 600)
        ph = float(page.get("height") or 900)
        w = self.opts.pdf_width_in * 72
        h = w * ph / pw
        if self.c is None:
            self.c = self._rl_canvas.Canvas(self.target, pagesize=(w, h))
        else:
            self.c.setPageSize((w, h))
        if self.opts.image_format == "jpeg":
            data = encoded or _encode(img, "jpeg", self.opts.quality)
            src = ImageReader(io.BytesIO(data))
        else:
            src = ImageReader(img)
        self.c.drawImage(src, 0, 0, width=w, height=h)
        self.c.showPage()

    def close(self) -> None:
        if self.c is not None:
            self.c.save()


class ExportSink:
    """Takes rendered pages one at a time and writes them to a PDF and/or a
    ZIP (targets: paths or file objects). Each page is encoded once per
    format; when the PDF and the ZIP use the same format the bytes are
    shared. ZIP entries are stored, not deflated (already compressed)."""

    def __init__(self, pdf_target=None, zip_target=None,
                 opts: Optional[ExportOptions] = None, digits: int = 3):
        self.opts = (opts or ExportOptions()).validate()
        self.digits = digits
        self.pdf = PdfWriter(pdf_target, self.opts) if pdf_target is not None else None
        self.zf = (zipfile.ZipFile(zip_target, "w", zipfile.ZIP_STORED)
                   if zip_target is not None else None)
        self.count = 0

    def add(self, img: Image.Image, page: dict) -> None:
        o = self.opts
        pdf_bytes = None
        if self.pdf is not None and o.image_format == "jpeg":
            pdf_bytes = _encode(img, "jpeg", o.quality)
        if self.zf is not None:
            if pdf_bytes is not None and o.zip_format == "jpeg":
                data = pdf_bytes
            else:
                data = _encode(img, o.zip_format, o.quality)
            name = f"page_{self.count + 1:0{self.digits}d}.{_EXT[o.zip_format]}"
            self.zf.writestr(name, data)
        if self.pdf is not None:
            self.pdf.add(img, page, pdf_bytes)
        self.count += 1

    def close(self) -> None:
        try:
            if self.zf is not None:
                self.zf.close()
        finally:
            if self.pdf is not None:
                self.pdf.close()


def _formats(fmt: str) -> Tuple[bool, bool]:
    if fmt not in ("pdf", "zip", "both"):
        raise ValueError(f"unknown export format: {fmt}")
    return fmt in ("pdf", "both"), fmt in ("zip", "both")


def write_export(rendered: Iterable[Tuple[dict, Image.Image]], fmt: str,
                 out_dir: Path, base_name: str,
                 opts: Optional[ExportOptions] = None, digits: int = 3,
                 progress: Optional[Callable[[int], None]] = None) -> dict:
    """Write <base_name>.pdf / .zip under out_dir from (page, image) pairs.
    Both are written to hidden .part files and renamed only when complete,
    so a failed export never leaves a truncated file under the final name.
    Returns {pages, pdf?: {path, url}, zip?: {path, url}}."""
    want_pdf, want_zip = _formats(fmt)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path, zip_path = out_dir / f"{base_name}.pdf", out_dir / f"{base_name}.zip"
    pdf_part = out_dir / f".{base_name}.pdf.part"
    zip_part = out_dir / f".{base_name}.zip.part"
    try:
        sink = ExportSink(str(pdf_part) if want_pdf else None,
                          str(zip_part) if want_zip else None, opts, digits)
        try:
            for page, img in rendered:
                sink.add(img, page)
                del img   # drop the page before rendering the next one
                if progress:
                    progress(sink.count)
        finally:
            sink.close()
        result: dict = {"pages": sink.count}
        for want, part, path, key in ((want_pdf, pdf_part, pdf_path, "pdf"),
                                      (want_zip, zip_part, zip_path, "zip")):
            if want:
                part.replace(path)
                result[key] = {"path": str(path),
                               "url": f"/api/export/download/{path.name}"}
    finally:
        for p in (pdf_part, zip_part):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
    prune_exports(out_dir, keep={base_name})
    return result


def _render_all(pages: List[dict], scale: float, resolve, page_numbers: bool,
                opts: ExportOptions) -> Iterator[Tuple[dict, Image.Image]]:
    """Render one page at a time: a long export must never hold every page
    image in memory at once."""
    for i, page in enumerate(pages):
        s = opts.page_scale(page, scale)
        img = render_page(page, s, resolve)
        if page_numbers:
            _stamp_page_number(img, i, s)
        yield page, img


def export_pages_to_bytes(pages: List[dict], fmt: str, scale: float,
                          resolve, page_numbers: bool = False,
                          opts: Optional[ExportOptions] = None) -> dict:
    """fmt ∈ pdf|zip|both → {"pdf": bytes|None, "zip": bytes|None}."""
    want_pdf, want_zip = _formats(fmt)
    opts = (opts or ExportOptions()).validate()
    pdf_buf = io.BytesIO() if want_pdf else None
    zip_buf = io.BytesIO() if want_zip else None
    sink = ExportSink(pdf_buf, zip_buf, opts)
    try:
        for page, img in _render_all(pages, scale, resolve, page_numbers, opts):
            sink.add(img, page)
            del img
    finally:
        sink.close()
    return {"pdf": pdf_buf.getvalue() if pdf_buf else None,
            "zip": zip_buf.getvalue() if zip_buf else None}


def export_pages(pages: List[dict], fmt: str, scale: float, resolve,
                 out_dir: Path, base_name: str,
                 page_numbers: bool = False,
                 opts: Optional[ExportOptions] = None) -> dict:
    """Render + write <base_name>.pdf/.zip; returns paths and
    /api/export/download URLs ({pages, pdf?, zip?})."""
    _formats(fmt)
    opts = (opts or ExportOptions()).validate()
    return write_export(_render_all(pages, scale, resolve, page_numbers, opts),
                        fmt, out_dir, base_name, opts)
