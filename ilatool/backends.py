"""PDF readers.

Two backends, one interface:

``pymupdf``
    Preferred.  Gives word-level boxes, per-span font information, images,
    vector drawings, native annotations and correct coordinates for rotated
    pages, all in one pass.

``poppler``
    Fallback using the ``pdftohtml``/``pdftotext``/``pdfinfo`` command line
    tools, which are present on almost every machine that can open a PDF.
    Text and geometry come from ``pdftohtml -xml``; annotations and page
    rotation come from :mod:`ilatool.pdfobj`, which reads the PDF structure
    directly.

Both produce the same :class:`~ilatool.pdfmodel.PdfDocument`, so no later
stage knows or cares which one ran.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import pdfobj
from .errors import PdfParseError, UnsupportedDocumentError
from .pdfmodel import (
    Block,
    DrawingInfo,
    ImageInfo,
    Line,
    LinkInfo,
    PageModel,
    PdfAnnotation,
    PdfDocument,
    Rect,
    Span,
)

ProgressFn = Optional[Callable[[int, int, str], None]]

#: Largest page we will happily process (points).  Anything bigger is almost
#: certainly a poster plot, not a document.
MAX_PAGE_DIMENSION = 20000.0


# ---------------------------------------------------------------------------
# Rotation maths
# ---------------------------------------------------------------------------
#
# There are three coordinate spaces in play and mixing them up is the single
# easiest way to attach a note to the wrong sentence:
#
#   *PDF user space*      origin bottom-left, y grows upwards, /Rotate ignored.
#                         This is what /Rect and /QuadPoints use.
#   *page space*          origin top-left of the (unrotated) media box, y grows
#                         downwards, /Rotate still ignored.  Text reads
#                         left-to-right here, so this is the space the model
#                         stores: lines stay horizontal and annotation boxes
#                         line up with the words under them.
#   *display space*       page space with /Rotate applied -- what a reader
#                         actually sees.  Only used for reporting and previews.
#
# PDF libraries disagree about which of these they hand back, which is why
# both backends funnel through the helpers below.

def page_rect_to_display(rect: Sequence[float], page_width: float,
                         page_height: float, rotate: int) -> Rect:
    """page space (unrotated, top-down) -> display space."""
    x0, y0, x1, y1 = (float(v) for v in rect)
    rot = int(rotate) % 360
    corners = [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
    points = [_page_point_to_display(x, y, page_width, page_height, rot)
              for x, y in corners]
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return Rect(min(xs), min(ys), max(xs), max(ys))


def _page_point_to_display(x: float, y: float, width: float,
                           height: float, rot: int) -> Tuple[float, float]:
    if rot == 90:
        return (height - y, x)
    if rot == 180:
        return (width - x, height - y)
    if rot == 270:
        return (y, width - x)
    return (x, y)


def display_rect_to_page(rect: Sequence[float], page_width: float,
                         page_height: float, rotate: int) -> Rect:
    """display space -> page space (unrotated, top-down)."""
    x0, y0, x1, y1 = (float(v) for v in rect)
    rot = int(rotate) % 360
    corners = [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
    points = []
    for x, y in corners:
        if rot == 90:
            points.append((y, page_height - x))
        elif rot == 180:
            points.append((page_width - x, page_height - y))
        elif rot == 270:
            points.append((page_width - y, x))
        else:
            points.append((x, y))
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return Rect(min(xs), min(ys), max(xs), max(ys))


def user_rect_to_page(rect: Sequence[float], mediabox: Sequence[float]) -> Rect:
    """PDF user space (y up) -> page space (y down)."""
    bx0, by0, bx1, by1 = (float(v) for v in mediabox)
    x0, y0, x1, y1 = (float(v) for v in rect)
    return Rect(x0 - bx0, by1 - y1, x1 - bx0, by1 - y0)


def display_size(page_width: float, page_height: float, rotate: int) -> Tuple[float, float]:
    if int(rotate) % 180 == 90:
        return (page_height, page_width)
    return (page_width, page_height)



# ---------------------------------------------------------------------------
# Backend discovery
# ---------------------------------------------------------------------------

def _import_pymupdf():
    try:  # PyMuPDF >= 1.24 spells the module 'pymupdf'
        import pymupdf  # type: ignore
        return pymupdf
    except Exception:
        pass
    try:  # older releases only ship 'fitz'
        import fitz  # type: ignore
        return fitz
    except Exception:
        return None


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def backend_status() -> Dict[str, Dict[str, Any]]:
    """Describe which backends are usable, for the TUI's status panel."""
    status: Dict[str, Dict[str, Any]] = {}
    mod = _import_pymupdf()
    if mod is not None:
        version = getattr(mod, "version", None)
        status["pymupdf"] = {
            "available": True,
            "detail": f"PyMuPDF {version[0] if version else ''}".strip(),
            "best": True,
        }
    else:
        status["pymupdf"] = {
            "available": False,
            "detail": "not installed (pip install pymupdf for best results)",
            "best": True,
        }
    tools = {name: _which(name) for name in ("pdftohtml", "pdftotext", "pdfinfo")}
    missing = [n for n, p in tools.items() if not p]
    status["poppler"] = {
        "available": not missing,
        "detail": "poppler-utils present" if not missing else f"missing: {', '.join(missing)}",
        "best": False,
    }
    return status


def choose_backend(preferred: str = "auto") -> str:
    status = backend_status()
    if preferred in ("pymupdf", "poppler"):
        if not status[preferred]["available"]:
            raise UnsupportedDocumentError(
                f"the '{preferred}' PDF backend is not available",
                hint=status[preferred]["detail"],
            )
        return preferred
    if status["pymupdf"]["available"]:
        return "pymupdf"
    if status["poppler"]["available"]:
        return "poppler"
    raise UnsupportedDocumentError(
        "no PDF backend is available",
        hint="Install PyMuPDF ('pip install pymupdf') or poppler-utils "
             "('brew install poppler' / 'apt install poppler-utils').",
    )


def load_pdf(path: Path, *, backend: str = "auto", progress: ProgressFn = None,
             password: str = "") -> PdfDocument:
    """Load ``path`` with the best available backend."""
    name = choose_backend(backend)
    if name == "pymupdf":
        return PyMuPDFBackend().load(path, progress=progress, password=password)
    return PopplerBackend().load(path, progress=progress, password=password)


# ---------------------------------------------------------------------------
# PyMuPDF backend
# ---------------------------------------------------------------------------

_COLOR_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


def _color_to_hex(value: Any) -> str:
    """Normalise the many colour shapes PDF libraries use to '#rrggbb'."""
    if value is None:
        return ""
    if isinstance(value, str):
        m = _COLOR_HEX.match(value.strip())
        return f"#{m.group(1).lower()}" if m else ""
    if isinstance(value, (list, tuple)):
        parts = [float(v) for v in value if isinstance(v, (int, float))]
        if len(parts) == 3:
            return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(p * 255)))) for p in parts)
        if len(parts) == 4:
            return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(p * 255)))) for p in parts[:3])
        if len(parts) == 1:
            g = max(0, min(255, int(round(parts[0] * 255))))
            return f"#{g:02x}{g:02x}{g:02x}"
    if isinstance(value, (int, float)):
        v = int(value)
        return f"#{v:06x}"
    return ""


class PyMuPDFBackend:
    name = "pymupdf"

    def load(self, path: Path, *, progress: ProgressFn = None,
             password: str = "") -> PdfDocument:
        mod = _import_pymupdf()
        if mod is None:
            raise UnsupportedDocumentError("PyMuPDF is not installed")
        try:
            doc = mod.open(str(path))
        except Exception as exc:  # pragma: no cover - depends on the file
            raise PdfParseError(
                f"could not open '{path.name}' as a PDF",
                hint="The file may be truncated, password protected or not a PDF.",
                cause=exc,
            ) from exc

        try:
            if getattr(doc, "needs_pass", False):
                if password and doc.authenticate(password):
                    pass
                else:
                    raise UnsupportedDocumentError(
                        f"'{path.name}' is password protected",
                        hint="Supply the password with --password, or remove the "
                             "protection first (e.g. with 'qpdf --decrypt').",
                    )
            meta = dict(getattr(doc, "metadata", {}) or {})
            pages: List[PageModel] = []
            total = doc.page_count
            for index in range(total):
                if progress:
                    progress(index, total, f"page {index + 1} of {total}")
                pages.append(self._read_page(mod, doc[index], index + 1))
            return PdfDocument(
                path=str(path),
                pages=pages,
                metadata=meta,
                backend=self.name,
                encrypted=bool(getattr(doc, "is_encrypted", False)),
            )
        finally:
            try:
                doc.close()
            except Exception:
                pass

    # -- one page -------------------------------------------------------------

    def _read_page(self, mod, page, number: int) -> PageModel:
        display = page.rect
        box = getattr(page, "cropbox", None)
        if box is None or box.is_empty:
            box = getattr(page, "mediabox", display)
        page_width, page_height = float(box.width), float(box.height)
        if page_width <= 0 or page_height <= 0:
            page_width, page_height = float(display.width), float(display.height)
        if page_width > MAX_PAGE_DIMENSION or page_height > MAX_PAGE_DIMENSION:
            raise UnsupportedDocumentError(
                f"page {number} is {page_width:.0f}x{page_height:.0f} points",
                hint="Pages this large are not documents; check the PDF.",
            )
        rotation = int(getattr(page, "rotation", 0) or 0) % 360
        disp_w, disp_h = display_size(page_width, page_height, rotation)
        model = PageModel(number=number, width=disp_w, height=disp_h,
                          rotation=rotation, page_width=page_width,
                          page_height=page_height)

        try:
            # PyMuPDF hands back *unrotated* coordinates, which is exactly the
            # page space the model stores, so no conversion is needed here.
            raw = page.get_text("dict")
        except Exception as exc:
            raise PdfParseError(f"could not read text from page {number}", cause=exc) from exc

        block_number = 0
        for raw_block in raw.get("blocks", []):
            btype = raw_block.get("type", 0)
            bbox = Rect.from_any(raw_block.get("bbox", (0, 0, 0, 0)))
            if btype == 1:
                model.images.append(ImageInfo(
                    bbox=bbox,
                    width=int(raw_block.get("width", 0) or 0),
                    height=int(raw_block.get("height", 0) or 0),
                    page=number,
                ))
                continue
            lines: List[Line] = []
            for raw_line in raw_block.get("lines", []):
                spans: List[Span] = []
                for raw_span in raw_line.get("spans", []):
                    text = raw_span.get("text", "")
                    if not text:
                        continue
                    spans.append(Span(
                        text=text,
                        bbox=Rect.from_any(raw_span.get("bbox", (0, 0, 0, 0))),
                        font=str(raw_span.get("font", "")),
                        size=float(raw_span.get("size", 0.0) or 0.0),
                        flags=int(raw_span.get("flags", 0) or 0),
                        color=int(raw_span.get("color", 0) or 0),
                    ))
                if not spans:
                    continue
                line_bbox = spans[0].bbox
                for span in spans[1:]:
                    line_bbox = line_bbox.union(span.bbox)
                lines.append(Line(bbox=line_bbox, spans=spans, page=number))
            if lines:
                block_number += 1
                model.blocks.append(Block(bbox=bbox, lines=lines,
                                          number=block_number, page=number))

        model.drawings = self._read_drawings(page, number)
        model.links = self._read_links(mod, page, number)
        model.annotations = self._read_annotations(page, number)
        return model

    def _read_drawings(self, page, number: int) -> List[DrawingInfo]:
        try:
            raw = page.get_drawings()
        except Exception:
            return []
        out: List[DrawingInfo] = []
        for item in raw:
            bbox = item.get("rect")
            if bbox is None:
                continue
            out.append(DrawingInfo(
                bbox=Rect.from_any(bbox),
                items=len(item.get("items", []) or []),
                fill=bool(item.get("fill")),
                stroke=bool(item.get("color")),
                page=number,
            ))
        return out

    def _read_links(self, mod, page, number: int) -> List[LinkInfo]:
        try:
            raw = page.get_links()
        except Exception:
            return []
        out: List[LinkInfo] = []
        for item in raw:
            bbox = item.get("from")
            if bbox is None:
                continue
            target = None
            if item.get("kind") == getattr(mod, "LINK_GOTO", 1):
                target = int(item.get("page", -1)) + 1 or None
            out.append(LinkInfo(
                bbox=Rect.from_any(bbox),
                uri=str(item.get("uri") or ""),
                target_page=target,
                page=number,
            ))
        return out

    def _read_annotations(self, page, number: int) -> List[PdfAnnotation]:
        try:
            annots = list(page.annots() or [])
        except Exception:
            return []
        out: List[PdfAnnotation] = []
        for annot in annots:
            try:
                out.append(self._annotation(annot=annot, number=number))
            except Exception:
                continue
        return [a for a in out if a is not None]

    def _annotation(self, annot, number: int) -> Optional[PdfAnnotation]:
        type_info = getattr(annot, "type", (0, "Unknown"))
        kind = str(type_info[1] if isinstance(type_info, (tuple, list)) else type_info).lower()
        if kind in ("popup", "link", "widget"):
            return None
        info = dict(getattr(annot, "info", {}) or {})
        rect = Rect.from_any(annot.rect)
        quads: List[Rect] = []
        vertices = getattr(annot, "vertices", None)
        if vertices:
            pts = [p for p in vertices]
            for i in range(0, len(pts) - 3, 4):
                quad = pts[i:i + 4]
                xs = [float(p[0]) for p in quad]
                ys = [float(p[1]) for p in quad]
                quads.append(Rect(min(xs), min(ys), max(xs), max(ys)))
        colors = dict(getattr(annot, "colors", {}) or {})
        color = _color_to_hex(colors.get("stroke")) or _color_to_hex(colors.get("fill"))
        return PdfAnnotation(
            page=number,
            kind=kind or "unknown",
            rect=rect,
            contents=str(info.get("content") or ""),
            author=str(info.get("title") or ""),
            subject=str(info.get("subject") or ""),
            color=color,
            opacity=float(getattr(annot, "opacity", 1.0) or 1.0),
            icon=str(info.get("name") or ""),
            created=str(info.get("creationDate") or ""),
            modified=str(info.get("modDate") or ""),
            quads=quads,
        )


# ---------------------------------------------------------------------------
# Poppler backend
# ---------------------------------------------------------------------------

_PAGE_RE = re.compile(r"<page\b([^>]*)>")
_FONT_RE = re.compile(r"<fontspec\b([^>]*)/?>")
_TEXT_RE = re.compile(r"<text\b([^>]*)>(.*?)</text>", re.DOTALL)
_IMAGE_RE = re.compile(r"<image\b([^>]*)/?>")
_ANCHOR_RE = re.compile(r'<a\s+href="([^"]*)"[^>]*>', re.IGNORECASE)
_ATTR_RE = re.compile(r'([A-Za-z_][-A-Za-z0-9_]*)\s*=\s*"([^"]*)"')


def _attrs(text: str) -> Dict[str, str]:
    return {m.group(1): m.group(2) for m in _ATTR_RE.finditer(text)}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class PopplerBackend:
    name = "poppler"

    def __init__(self) -> None:
        # Set per load(); the XML pass needs the page tree to find /Annots.
        self._structure: Optional[pdfobj.PdfFile] = None

    def load(self, path: Path, *, progress: ProgressFn = None,
             password: str = "") -> PdfDocument:
        if not _which("pdftohtml"):
            raise UnsupportedDocumentError(
                "poppler-utils is not installed",
                hint="Install it ('brew install poppler' or 'apt install poppler-utils'), "
                     "or install PyMuPDF ('pip install pymupdf').",
            )

        structure = None
        try:
            structure = pdfobj.PdfFile.from_path(path)
        except Exception:
            structure = None
        self._structure = structure
        if structure is not None and structure.encrypted and not password:
            raise UnsupportedDocumentError(
                f"'{path.name}' is password protected",
                hint="Remove the protection first, or use the PyMuPDF backend with "
                     "--password.",
            )

        xml = self._run_pdftohtml(path, progress)
        pages = self._parse_xml(xml, path, structure, progress)
        if not pages:
            raise PdfParseError(
                f"no pages could be read from '{path.name}'",
                hint="The file may be corrupt.",
            )
        meta: Dict[str, Any] = {}
        info = self._run_pdfinfo(path)
        for key in ("Title", "Author", "Subject", "Creator", "Producer"):
            value = info.get(key.lower())
            if value:
                meta[key.lower()] = value
        return PdfDocument(path=str(path), pages=pages, metadata=meta,
                           backend=self.name,
                           encrypted=bool(structure and structure.encrypted))

    # -- process helpers ------------------------------------------------------

    def _run_pdftohtml(self, path: Path, progress: ProgressFn) -> str:
        if progress:
            progress(0, 0, "running pdftohtml")
        # ``-hidden`` matters more than it looks: an OCR'd scan stores its
        # text with rendering mode 3 (invisible), so without it a perfectly
        # searchable scanned judgment extracts as an empty document.
        cmd = [
            "pdftohtml", "-xml", "-stdout", "-q", "-noframes",
            "-noroundcoord", "-zoom", "1.0", "-hidden",
            str(Path(path).resolve()),
        ]
        with tempfile.TemporaryDirectory(prefix="ilatool-poppler-") as tmp:
            try:
                result = subprocess.run(
                    cmd, capture_output=True, cwd=tmp, timeout=600,
                )
            except FileNotFoundError as exc:
                raise UnsupportedDocumentError("poppler-utils is not installed", cause=exc) from exc
            except subprocess.TimeoutExpired as exc:
                raise PdfParseError(
                    "pdftohtml timed out after 10 minutes",
                    hint="The PDF may be enormous or malformed.",
                    cause=exc,
                ) from exc
            if result.returncode != 0 and not result.stdout.strip():
                detail = (result.stderr or b"").decode("utf-8", "replace").strip()
                raise PdfParseError(
                    f"pdftohtml could not read '{path.name}'",
                    detail=detail,
                    hint="If the file is password protected, remove the protection first.",
                )
            return result.stdout.decode("utf-8", "replace")

    def _run_pdfinfo(self, path: Path) -> Dict[str, str]:
        if not _which("pdfinfo"):
            return {}
        try:
            result = subprocess.run(["pdfinfo", str(path)], capture_output=True, timeout=60)
        except Exception:
            return {}
        info: Dict[str, str] = {}
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                info[key.strip().lower()] = value.strip()
        return info

    # -- XML parsing ----------------------------------------------------------

    def _parse_xml(self, xml: str, path: Path, structure,
                   progress: ProgressFn) -> List[PageModel]:
        page_dicts: List[Dict[str, Any]] = []
        if structure is not None:
            try:
                page_dicts = structure.catalog_pages()
            except Exception:
                page_dicts = []

        pages: List[PageModel] = []
        # Split the document on <page ...> so page-level state stays local.
        chunks = re.split(r"(?=<page\b)", xml)
        for chunk in chunks:
            m = _PAGE_RE.search(chunk)
            if not m:
                continue
            attrs = _attrs(m.group(1))
            number = int(_f(attrs.get("number"), len(pages) + 1))
            xml_width = _f(attrs.get("width"))
            xml_height = _f(attrs.get("height"))
            if xml_width <= 0 or xml_height <= 0:
                xml_width, xml_height = 612.0, 792.0
            if progress:
                progress(number - 1, len(chunks) - 1 or number,
                         f"page {number}")

            fonts: Dict[str, Dict[str, Any]] = {}
            for fm in _FONT_RE.finditer(chunk):
                fa = _attrs(fm.group(1))
                fonts[fa.get("id", "")] = {
                    "size": _f(fa.get("size"), 10.0),
                    "family": fa.get("family", ""),
                    "color": fa.get("color", "#000000"),
                }

            chunks_found: List[Dict[str, Any]] = []
            for tm in _TEXT_RE.finditer(chunk):
                ta = _attrs(tm.group(1))
                body = tm.group(2)
                hrefs = [h for h in _ANCHOR_RE.findall(body)]
                text = re.sub(r"<[^>]+>", "", body)
                text = html.unescape(text)
                if not text.strip() and not hrefs:
                    continue
                font = fonts.get(ta.get("font", ""), {})
                chunks_found.append({
                    "top": _f(ta.get("top")),
                    "left": _f(ta.get("left")),
                    "width": _f(ta.get("width")),
                    "height": _f(ta.get("height")),
                    "text": text,
                    "size": _f(font.get("size"), 10.0),
                    "family": str(font.get("family", "")),
                    "href": hrefs[0] if hrefs else "",
                })

            page_dict = page_dicts[number - 1] if (page_dicts and number - 1 < len(page_dicts)) else None
            rotation = pdfobj.page_rotation(page_dict) if page_dict else 0
            # pdftohtml swaps the axes for rotated pages, so the XML width and
            # height are the *display* size; recover the page-space size.
            if rotation % 180 == 90:
                page_width, page_height = xml_height, xml_width
            else:
                page_width, page_height = xml_width, xml_height
            disp_w, disp_h = display_size(page_width, page_height, rotation)

            page = PageModel(number=number, width=disp_w, height=disp_h,
                             rotation=rotation, page_width=page_width,
                             page_height=page_height)
            page.blocks = self._chunks_to_blocks(chunks_found, number, rotation,
                                                 page_width, page_height,
                                                 disp_w, disp_h)
            if page_dict is not None:
                page.annotations = self._annotations_from_structure(page_dict, number)

            for im in _IMAGE_RE.finditer(chunk):
                ia = _attrs(im.group(1))
                display_box = Rect(_f(ia.get("left")), _f(ia.get("top")),
                                   _f(ia.get("left")) + _f(ia.get("width")),
                                   _f(ia.get("top")) + _f(ia.get("height")))
                page.images.append(ImageInfo(
                    bbox=display_rect_to_page(display_box.as_tuple(), page_width,
                                              page_height, rotation),
                    page=number,
                ))
            for entry in chunks_found:
                if entry["href"]:
                    display_box = Rect(entry["left"], entry["top"],
                                       entry["left"] + max(entry["width"], 1.0),
                                       entry["top"] + max(entry["height"], 1.0))
                    page.links.append(LinkInfo(
                        bbox=display_rect_to_page(display_box.as_tuple(), page_width,
                                                  page_height, rotation),
                        uri=entry["href"],
                        page=number,
                    ))
            pages.append(page)
        return pages


    def _chunks_to_blocks(self, chunks: Sequence[Dict[str, Any]], number: int,
                          rotation: int = 0, page_width: float = 0.0,
                          page_height: float = 0.0,
                          disp_w: float = 0.0, disp_h: float = 0.0) -> List[Block]:
        """Group pdftohtml's text chunks into lines and map them to page space.

        pdftohtml reports a chunk's box in *display* coordinates (the page as
        the reader sees it), so everything here is built in display space and
        converted once at the end.  Text that is horizontal on screen clusters
        by ``top``; text that runs vertically (a rotated page whose content was
        never rotated to compensate) clusters by ``left`` and pdftohtml gives it
        a zero width, so the run length has to be estimated.
        """
        if not chunks:
            return []

        # Drop anything that lands entirely outside the page.  Producers
        # leave stray text objects off-page (a hidden duplicate, a leftover
        # running head) and pdftohtml reports them at absurd coordinates such
        # as left="-278"; merged into a real line they produce garbage like
        # "certain international obligations (Art. 30 IV 24) Article I by, ...".
        def on_page(chunk: Dict[str, Any]) -> bool:
            left, top = chunk["left"], chunk["top"]
            size = chunk["size"] or 10.0
            width = max(chunk["width"], size if chunk["width"] <= 0.01 else 0.0)
            right = left + width
            bottom = top + max(chunk["height"], size)
            # Nothing legitimate *starts* left of or above the page.  A text
            # object that does is a hidden duplicate or a leftover running
            # head, and gluing it to the line at the same height produces
            # sentences that were never in the document.
            if left < -2.0 or top < -2.0:
                return False
            if disp_w and (right < 0.0 or left > disp_w):
                return False
            if disp_h and (bottom < 0.0 or top > disp_h):
                return False
            return True

        chunks = [c for c in chunks if on_page(c)]
        if not chunks:
            return []

        def display_box(chunk: Dict[str, Any]) -> Rect:
            height = chunk["height"] or chunk["size"] or 10.0
            width = chunk["width"]
            if width > 0.01:
                return Rect(chunk["left"], chunk["top"],
                            chunk["left"] + width, chunk["top"] + height)
            # Vertical run: length estimated, thickness = reported height.
            length = max(chunk["size"] * 0.5, 1.0) * max(1, len(chunk["text"].strip()))
            return Rect(chunk["left"], chunk["top"],
                        chunk["left"] + height, chunk["top"] + length)

        groups: Dict[bool, List[Dict[str, Any]]] = {True: [], False: []}
        for chunk in chunks:
            groups[chunk["width"] > 0.01].append(chunk)

        blocks: List[Block] = []
        index = 0
        for horizontal, group in groups.items():
            if not group:
                continue
            heights = [c["height"] for c in group if c["height"] > 0]
            tol = max(2.0, (sorted(heights)[len(heights) // 2] * 0.4) if heights else 2.0)

            key_cluster = (lambda c: c["top"]) if horizontal else (lambda c: c["left"])
            key_order = (lambda c: c["left"]) if horizontal else (lambda c: c["top"])

            ordered = sorted(group, key=lambda c: (key_cluster(c), key_order(c)))
            clusters: List[List[Dict[str, Any]]] = []
            for chunk in ordered:
                if not clusters:
                    clusters.append([chunk])
                    continue
                last = clusters[-1]
                reference = min(key_cluster(c) for c in last)
                if (abs(key_cluster(chunk) - reference) <= tol
                        and _horizontally_close(last, chunk, horizontal)):
                    last.append(chunk)
                else:
                    clusters.append([chunk])

            for cluster in clusters:
                cluster.sort(key=key_order)
                spans: List[Span] = []
                cursor: Optional[float] = None
                for chunk in cluster:
                    text = chunk["text"]
                    if cursor is not None and key_order(chunk) - cursor > 0.25 * max(1.0, chunk["size"] * 0.3):
                        if not text.startswith(" "):
                            text = " " + text
                    box = display_box(chunk)
                    cursor = (box.x1 if horizontal else box.y1)
                    spans.append(Span(
                        text=text,
                        bbox=display_rect_to_page(box.as_tuple(), page_width,
                                                  page_height, rotation),
                        font=chunk["family"],
                        size=chunk["size"],
                        flags=_flags_from_family(chunk["family"]),
                    ))
                if not spans:
                    continue
                index += 1
                bbox = spans[0].bbox
                for span in spans[1:]:
                    bbox = bbox.union(span.bbox)
                blocks.append(Block(bbox=bbox,
                                    lines=[Line(bbox=bbox, spans=spans, page=number)],
                                    number=index, page=number))
        blocks.sort(key=lambda b: (round(b.bbox.y0, 1), b.bbox.x0))
        return blocks

    def _annotations_from_structure(self, page_dict: Dict[str, Any], number: int) -> List[PdfAnnotation]:
        doc = self._structure
        if doc is None:
            return []
        annots = doc.resolve(page_dict.get("Annots"))
        if not isinstance(annots, list):
            return []
        mediabox = pdfobj.page_mediabox(page_dict)
        rotate = pdfobj.page_rotation(page_dict)
        out: List[PdfAnnotation] = []
        for item in annots:
            data = doc.resolve(item)
            if not isinstance(data, dict):
                continue
            subtype = str(doc.resolve(data.get("Subtype")) or "").lower()
            if subtype in ("popup", "link", "widget"):
                continue
            rect_values = pdfobj.number_list(doc.resolve(data.get("Rect")))
            if len(rect_values) != 4:
                continue
            rect = user_rect_to_page(rect_values, mediabox)
            quads: List[Rect] = []
            quad_values = pdfobj.number_list(doc.resolve(data.get("QuadPoints")))
            for i in range(0, len(quad_values) - 7, 8):
                xs = quad_values[i:i + 8:2]
                ys = quad_values[i + 1:i + 8:2]
                quads.append(user_rect_to_page(
                    [min(xs), min(ys), max(xs), max(ys)], mediabox))
            color = data.get("C")
            color_hex = ""
            if isinstance(color, list):
                parts = [float(v) for v in color if isinstance(v, (int, float))]
                if len(parts) >= 3:
                    color_hex = "#%02x%02x%02x" % tuple(
                        max(0, min(255, int(round(p * 255)))) for p in parts[:3])
            out.append(PdfAnnotation(
                page=number,
                kind=subtype or "unknown",
                rect=rect,
                contents=pdfobj.text_of(doc.resolve(data.get("Contents"))),
                author=pdfobj.text_of(doc.resolve(data.get("T"))),
                subject=pdfobj.text_of(doc.resolve(data.get("Subj"))),
                color=color_hex,
                opacity=float(data.get("CA") or 1.0) if isinstance(data.get("CA"), (int, float)) else 1.0,
                icon=pdfobj.text_of(doc.resolve(data.get("Name"))),
                created=pdfobj.text_of(doc.resolve(data.get("CreationDate"))),
                modified=pdfobj.text_of(doc.resolve(data.get("M"))),
                quads=quads,
            ))
        return out


def _horizontally_close(cluster: Sequence[Dict[str, Any]],
                        chunk: Dict[str, Any], horizontal: bool) -> bool:
    """Is a fragment part of the same visual line as the cluster so far?

    Two fragments at the same height but far apart horizontally are different
    text objects -- a section number in the margin, a stamp, a sidebar -- and
    gluing them together produces a sentence that never existed.
    """
    if not horizontal:
        return True
    size = max(1.0, float(chunk.get("size") or 10.0))
    # Two runs on the same baseline are one line only when the gap between
    # them is a word-space-sized gap.  A generous threshold silently glues a
    # narrow left column onto the wide right column beside it ("for the
    # Republic of" + "HE Mr John Silk, ..."), producing sentences that were
    # never in the document and hiding the column boundary from the layout
    # stage.
    gap_allowed = max(15.0, size * 2.0)
    left = min(c["left"] for c in cluster)
    right = max(c["left"] + max(c["width"], 0.0) for c in cluster)
    start = chunk["left"]
    end = start + max(chunk["width"], 0.0)
    if end < left:
        return (left - end) <= gap_allowed
    if start > right:
        return (start - right) <= gap_allowed
    return True


def _flags_from_family(family: str) -> int:
    name = (family or "").lower()
    flags = 0
    if "bold" in name or ",bold" in name:
        flags |= 16
    if "italic" in name or "oblique" in name:
        flags |= 2
    if "mono" in name or "courier" in name:
        flags |= 8
    if "times" in name or "serif" in name or "roman" in name:
        flags |= 4
    return flags


# ---------------------------------------------------------------------------
# File-level diagnostics used by the loader/TUI
# ---------------------------------------------------------------------------

def quick_text_probe(path: Path, samples: int = 8) -> Tuple[int, int]:
    """Cheaply check whether a PDF has a text layer at all.

    Returns ``(pages_sampled, pages_with_text)``.  Reading a scanned PDF page
    by page is slow because every page is a large image, so this samples a
    handful of pages spread through the document with ``pdftotext`` -- which
    ignores images entirely -- and lets the caller fail fast instead of
    grinding through a hundred scans before saying "there is no text here".
    """
    if not _which("pdftotext"):
        return (0, 0)

    page_count = 0
    info = _which("pdfinfo")
    if info:
        try:
            result = subprocess.run(["pdfinfo", str(path)], capture_output=True, timeout=60)
            for line in result.stdout.decode("utf-8", "replace").splitlines():
                if line.lower().startswith("pages:"):
                    page_count = int(line.split(":", 1)[1].strip() or 0)
                    break
        except Exception:
            page_count = 0
    if page_count <= 0:
        try:
            page_count = len(pdfobj.PdfFile.from_path(path).catalog_pages())
        except Exception:
            return (0, 0)

    if page_count <= samples:
        indices = list(range(1, page_count + 1))
    else:
        step = page_count / float(samples)
        indices = sorted({max(1, min(page_count, int(round(i * step)) + 1))
                          for i in range(samples)})

    with_text = 0
    for number in indices:
        try:
            result = subprocess.run(
                ["pdftotext", "-f", str(number), "-l", str(number), str(path), "-"],
                capture_output=True, timeout=60)
        except Exception:
            continue
        if result.stdout.decode("utf-8", "replace").strip():
            with_text += 1
    return (len(indices), with_text)


def probe_pdf(path: Path) -> Dict[str, Any]:
    """Cheap, backend-agnostic facts about a PDF, for the status panel."""
    info: Dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if not path.exists():
        return info
    info["size_bytes"] = path.stat().st_size
    if _which("pdfinfo"):
        try:
            result = subprocess.run(["pdfinfo", str(path)], capture_output=True, timeout=60)
            parsed: Dict[str, str] = {}
            for line in result.stdout.decode("utf-8", "replace").splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    parsed[key.strip().lower()] = value.strip()
            info["pdfinfo"] = parsed
        except Exception:
            pass
    try:
        doc = pdfobj.PdfFile.from_path(path)
        pages = doc.catalog_pages()
        info["page_count"] = len(pages)
        info["encrypted"] = doc.encrypted
        info["rotations"] = sorted({pdfobj.page_rotation(p) for p in pages})
        sizes = {pdfobj.page_mediabox(p) for p in pages}
        info["page_sizes"] = [list(s) for s in sizes]
        info["annotation_count"] = sum(
            len(doc.resolve(p.get("Annots")) or [])
            for p in pages
            if isinstance(doc.resolve(p.get("Annots")), list)
        )
    except Exception as exc:
        info["structure_error"] = str(exc)
    return info
