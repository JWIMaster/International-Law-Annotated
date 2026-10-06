"""The structured representation of a PDF.

Everything the rest of the tool knows about a PDF goes through these types.
Two rules matter:

1.  **Coordinates are in "page space".**  Origin at the top-left of the
    unrotated media box, y growing downwards, units are PDF points, and the
    page's ``/Rotate`` deliberately *not* applied.  Keeping rotation out of
    the stored coordinates means text always runs left-to-right and an
    annotation rectangle lands exactly on the words it marks.  Each page
    knows its own ``rotation`` and can convert to display space with
    :meth:`PageModel.to_display` when a viewer-facing coordinate is needed.

2.  **Nothing is thrown away at load time.**  Word boxes, font sizes, images,
    vector drawings, links and native PDF annotations all survive into the
    model, because the later stages need geometry to decide reading order and
    to attach annotations to the right sentence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    # -- constructors ---------------------------------------------------------

    @classmethod
    def from_any(cls, value: Any) -> "Rect":
        """Accepts a Rect, a 4-sequence, or an object with x0/y0/x1/y1."""
        if isinstance(value, Rect):
            return value
        if hasattr(value, "x0"):
            return cls(float(value.x0), float(value.y0), float(value.x1), float(value.y1))
        seq = list(value)
        if len(seq) != 4:
            raise ValueError(f"cannot make a Rect from {value!r}")
        return cls(*(float(v) for v in seq))

    @classmethod
    def empty(cls) -> "Rect":
        return cls(0.0, 0.0, 0.0, 0.0)

    # -- basic properties -----------------------------------------------------

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> Tuple[float, float]:
        return ((self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0)

    @property
    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    # -- relationships --------------------------------------------------------

    def union(self, other: "Rect") -> "Rect":
        return Rect(min(self.x0, other.x0), min(self.y0, other.y0),
                    max(self.x1, other.x1), max(self.y1, other.y1))

    def intersection(self, other: "Rect") -> Optional["Rect"]:
        x0, y0 = max(self.x0, other.x0), max(self.y0, other.y0)
        x1, y1 = min(self.x1, other.x1), min(self.y1, other.y1)
        if x1 <= x0 or y1 <= y0:
            return None
        return Rect(x0, y0, x1, y1)

    def overlap_area(self, other: "Rect") -> float:
        inter = self.intersection(other)
        return inter.area if inter else 0.0

    def overlap_fraction(self, other: "Rect") -> float:
        """Fraction of *this* rectangle covered by ``other``."""
        if self.area <= 0:
            return 0.0
        return self.overlap_area(other) / self.area

    def contains_point(self, x: float, y: float) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1

    def contains_rect(self, other: "Rect") -> bool:
        return (self.x0 <= other.x0 and self.y0 <= other.y0
                and self.x1 >= other.x1 and self.y1 >= other.y1)

    def expand(self, dx: float, dy: Optional[float] = None) -> "Rect":
        dy = dx if dy is None else dy
        return Rect(self.x0 - dx, self.y0 - dy, self.x1 + dx, self.y1 + dy)

    def horizontal_gap(self, other: "Rect") -> float:
        """Signed horizontal distance between the two boxes (negative means
        they overlap horizontally)."""
        if other.x0 >= self.x1:
            return other.x0 - self.x1
        if self.x0 >= other.x1:
            return self.x0 - other.x1
        return -min(self.x1, other.x1) + max(self.x0, other.x0)

    def vertical_gap(self, other: "Rect") -> float:
        if other.y0 >= self.y1:
            return other.y0 - self.y1
        if self.y0 >= other.y1:
            return self.y0 - other.y1
        return -min(self.y1, other.y1) + max(self.y0, other.y0)

    def as_tuple(self) -> Tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)

    def as_dict(self) -> Dict[str, float]:
        """Rounded, for JSON output (keeps generated files readable)."""
        return {
            "x0": round(self.x0, 2),
            "y0": round(self.y0, 2),
            "x1": round(self.x1, 2),
            "y1": round(self.y1, 2),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Rect({self.x0:.1f},{self.y0:.1f},{self.x1:.1f},{self.y1:.1f})"


# ---------------------------------------------------------------------------
# Text content
# ---------------------------------------------------------------------------

# Span flag bits, kept aligned with PyMuPDF's so the backend mapping is
# obvious, but the rest of the tool only ever reads the booleans below.
FLAG_SUPERSCRIPT = 1
FLAG_ITALIC = 2
FLAG_SERIF = 4
FLAG_MONOSPACE = 8
FLAG_BOLD = 16


@dataclass
class Span:
    text: str
    bbox: Rect
    font: str = ""
    size: float = 0.0
    flags: int = 0
    color: int = 0

    @property
    def bold(self) -> bool:
        return bool(self.flags & FLAG_BOLD) or "bold" in self.font.lower()

    @property
    def italic(self) -> bool:
        return bool(self.flags & FLAG_ITALIC) or "italic" in self.font.lower() or "oblique" in self.font.lower()

    @property
    def monospace(self) -> bool:
        return bool(self.flags & FLAG_MONOSPACE) or "mono" in self.font.lower() or "courier" in self.font.lower()

    @property
    def serif(self) -> bool:
        name = self.font.lower()
        if any(k in name for k in ("times", "serif", "georgia", "garamond", "roman")):
            return True
        return bool(self.flags & FLAG_SERIF) and not self.monospace

    def as_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "bbox": self.bbox.as_dict(),
            "font": self.font,
            "size": round(self.size, 2),
            "bold": self.bold,
            "italic": self.italic,
        }


@dataclass
class Line:
    """One visual line of text."""

    bbox: Rect
    spans: List[Span] = field(default_factory=list)
    page: int = 0

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)

    @property
    def size(self) -> float:
        """Dominant font size: the size covering the most characters."""
        if not self.spans:
            return 0.0
        weights: Dict[float, int] = {}
        for span in self.spans:
            key = round(span.size, 1)
            weights[key] = weights.get(key, 0) + max(1, len(span.text))
        return max(weights.items(), key=lambda kv: kv[1])[0]

    @property
    def font(self) -> str:
        if not self.spans:
            return ""
        return max(self.spans, key=lambda s: len(s.text)).font

    @property
    def bold_fraction(self) -> float:
        total = sum(len(s.text) for s in self.spans) or 1
        bold = sum(len(s.text) for s in self.spans if s.bold)
        return bold / total

    @property
    def italic_fraction(self) -> float:
        total = sum(len(s.text) for s in self.spans) or 1
        italic = sum(len(s.text) for s in self.spans if s.italic)
        return italic / total

    @property
    def is_bold(self) -> bool:
        return self.bold_fraction >= 0.6

    @property
    def is_italic(self) -> bool:
        return self.italic_fraction >= 0.6

    @property
    def x0(self) -> float:
        return self.bbox.x0

    @property
    def y0(self) -> float:
        return self.bbox.y0

    @property
    def stripped(self) -> str:
        return self.text.strip()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "bbox": self.bbox.as_dict(),
            "text": self.text,
            "size": round(self.size, 2),
            "bold": self.is_bold,
            "italic": self.is_italic,
            "spans": [s.as_dict() for s in self.spans],
        }


@dataclass
class Block:
    """A group of lines the PDF itself considered a block (or that the
    layout stage grouped together)."""

    bbox: Rect
    lines: List[Line] = field(default_factory=list)
    number: int = 0
    page: int = 0

    @property
    def text(self) -> str:
        return "\n".join(l.text for l in self.lines)

    @property
    def size(self) -> float:
        if not self.lines:
            return 0.0
        weights: Dict[float, int] = {}
        for line in self.lines:
            key = round(line.size, 1)
            weights[key] = weights.get(key, 0) + max(1, len(line.text))
        return max(weights.items(), key=lambda kv: kv[1])[0]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "bbox": self.bbox.as_dict(),
            "lines": [l.as_dict() for l in self.lines],
        }


@dataclass
class ImageInfo:
    bbox: Rect
    width: int = 0
    height: int = 0
    colorspace: str = ""
    page: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {"bbox": self.bbox.as_dict(), "width": self.width,
                "height": self.height, "colorspace": self.colorspace}


@dataclass
class DrawingInfo:
    """A vector graphic.  We keep only what is useful downstream: where it
    is, and roughly what it is made of."""

    bbox: Rect
    items: int = 0
    fill: bool = False
    stroke: bool = False
    page: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {"bbox": self.bbox.as_dict(), "items": self.items,
                "fill": self.fill, "stroke": self.stroke}


@dataclass
class LinkInfo:
    bbox: Rect
    uri: str = ""
    target_page: Optional[int] = None
    page: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {"bbox": self.bbox.as_dict(), "uri": self.uri,
                "target_page": self.target_page}


# ---------------------------------------------------------------------------
# Native PDF annotations
# ---------------------------------------------------------------------------

#: Annotation kinds we name explicitly; anything else is passed through as
#: its lower-cased PDF subtype so nothing is ever silently ignored.
ANNOTATION_KINDS = {
    "text": "sticky note",
    "freetext": "text box",
    "highlight": "highlight",
    "underline": "underline",
    "strikeout": "strikethrough",
    "squiggly": "squiggly underline",
    "square": "rectangle",
    "circle": "ellipse",
    "polygon": "polygon",
    "line": "line",
    "ink": "freehand drawing",
    "stamp": "stamp",
    "caret": "caret",
    "fileattachment": "file attachment",
    "sound": "sound",
    "popup": "popup",
    "link": "link",
    "redact": "redaction",
}

#: Kinds that mark up existing text (their geometry points at words).
MARKUP_KINDS = {"highlight", "underline", "strikeout", "squiggly"}

#: Kinds whose contents are a note *about* the marked-up area.
NOTE_KINDS = {"text", "freetext", "square", "circle", "polygon", "line",
              "ink", "stamp", "caret", "fileattachment", "sound"}


@dataclass
class PdfAnnotation:
    page: int
    kind: str
    rect: Rect
    contents: str = ""
    author: str = ""
    subject: str = ""
    color: str = ""
    opacity: float = 1.0
    icon: str = ""
    created: str = ""
    modified: str = ""
    #: For markup annotations, the individual quads (a highlight over three
    #: lines has three).  Empty when the PDF only gave a bounding box.
    quads: List[Rect] = field(default_factory=list)
    #: True when the annotation was added by this tool or is otherwise
    #: generated rather than authored (links, widgets).
    generated: bool = False

    @property
    def is_markup(self) -> bool:
        return self.kind in MARKUP_KINDS

    @property
    def is_note(self) -> bool:
        return self.kind in NOTE_KINDS

    @property
    def kind_label(self) -> str:
        return ANNOTATION_KINDS.get(self.kind, self.kind.replace("_", " "))

    @property
    def has_text(self) -> bool:
        return bool(self.contents.strip())

    @property
    def regions(self) -> List[Rect]:
        return self.quads or [self.rect]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "page": self.page,
            "kind": self.kind,
            "rect": self.rect.as_dict(),
            "quads": [q.as_dict() for q in self.quads],
            "contents": self.contents,
            "author": self.author,
            "subject": self.subject,
            "color": self.color,
            "opacity": self.opacity,
            "icon": self.icon,
            "created": self.created,
            "modified": self.modified,
        }


# ---------------------------------------------------------------------------
# Pages and documents
# ---------------------------------------------------------------------------

@dataclass
class PageModel:
    """One page.

    ``width``/``height`` are the **display** dimensions (with ``/Rotate``
    applied) -- what a reader sees.  ``page_width``/``page_height`` are the
    unrotated dimensions.  Every stored coordinate (lines, spans, images,
    annotations) is in *page space*: unrotated, origin at the media box's
    top-left, y growing downwards.  In page space text always reads
    left-to-right and an annotation rectangle sits exactly on top of the
    words it marks, which is what the later stages need.  Use
    :meth:`to_display` when you need to talk about the rotated page.
    """

    number: int  # 1-based
    width: float
    height: float
    rotation: int = 0
    page_width: float = 0.0
    page_height: float = 0.0
    blocks: List[Block] = field(default_factory=list)
    images: List[ImageInfo] = field(default_factory=list)
    drawings: List[DrawingInfo] = field(default_factory=list)
    links: List[LinkInfo] = field(default_factory=list)
    annotations: List[PdfAnnotation] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.page_width:
            self.page_width = self.width
        if not self.page_height:
            self.page_height = self.height

    @property
    def rect(self) -> Rect:
        """Display-space page rectangle."""
        return Rect(0.0, 0.0, self.width, self.height)

    @property
    def page_rect(self) -> Rect:
        """Page-space (unrotated) rectangle."""
        return Rect(0.0, 0.0, self.page_width, self.page_height)

    @property
    def is_rotated(self) -> bool:
        return self.rotation % 360 != 0

    def to_display(self, rect: Rect) -> Rect:
        """Convert a page-space rectangle into display space."""
        if not self.is_rotated:
            return rect
        rot = self.rotation % 360
        corners = [(rect.x0, rect.y0), (rect.x1, rect.y0),
                   (rect.x0, rect.y1), (rect.x1, rect.y1)]
        out = []
        for x, y in corners:
            if rot == 90:
                out.append((self.page_height - y, x))
            elif rot == 180:
                out.append((self.page_width - x, self.page_height - y))
            elif rot == 270:
                out.append((y, self.page_width - x))
            else:
                out.append((x, y))
        xs = [p[0] for p in out]
        ys = [p[1] for p in out]
        return Rect(min(xs), min(ys), max(xs), max(ys))

    @property
    def lines(self) -> List[Line]:
        out: List[Line] = []
        for block in self.blocks:
            out.extend(block.lines)
        return out

    @property
    def text(self) -> str:
        return "\n".join(l.text for l in self.lines)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "number": self.number,
            "width": round(self.width, 2),
            "height": round(self.height, 2),
            "page_width": round(self.page_width, 2),
            "page_height": round(self.page_height, 2),
            "rotation": self.rotation,
            "blocks": [b.as_dict() for b in self.blocks],
            "images": [i.as_dict() for i in self.images],
            "drawings": [d.as_dict() for d in self.drawings],
            "links": [l.as_dict() for l in self.links],
            "annotations": [a.as_dict() for a in self.annotations],
        }


@dataclass
class PdfDocument:
    path: str
    pages: List[PageModel] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    backend: str = ""
    encrypted: bool = False

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def title(self) -> str:
        return str(self.metadata.get("title") or "").strip()

    @property
    def author(self) -> str:
        return str(self.metadata.get("author") or "").strip()

    def all_annotations(self) -> List[PdfAnnotation]:
        out: List[PdfAnnotation] = []
        for page in self.pages:
            out.extend(page.annotations)
        return out

    def all_lines(self) -> Iterable[Line]:
        for page in self.pages:
            yield from page.lines

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "backend": self.backend,
            "metadata": self.metadata,
            "page_count": self.page_count,
            "pages": [p.as_dict() for p in self.pages],
        }


# ---------------------------------------------------------------------------
# Small geometric helpers used by the layout stage
# ---------------------------------------------------------------------------

def median(values: Sequence[float]) -> float:
    vals = sorted(v for v in values if not math.isnan(v))
    if not vals:
        return 0.0
    mid = len(vals) // 2
    if len(vals) % 2:
        return float(vals[mid])
    return (vals[mid - 1] + vals[mid]) / 2.0


def cluster_1d(values: Sequence[float], tolerance: float) -> List[List[int]]:
    """Group indices of ``values`` into clusters no further apart than
    ``tolerance``.  Returns a list of index lists, ordered by value."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    clusters: List[List[int]] = []
    current: List[int] = []
    last = None
    for idx in order:
        value = values[idx]
        if last is None or value - last <= tolerance:
            current.append(idx)
        else:
            clusters.append(current)
            current = [idx]
        last = value
    if current:
        clusters.append(current)
    return clusters


def reading_order(lines: Sequence[Line], tolerance: float = 3.0) -> List[Line]:
    """Sort lines into human reading order, for a *single column* of text.

    A plain sort by y then x is wrong for the common case where two lines
    share a y-coordinate but belong to different blocks (e.g. a paragraph
    and a marginal note).  Within a y-band we sort by x, which is right for
    left-to-right scripts; the multi-column handling lives in
    :mod:`ilatool.layout`.
    """
    result: List[Line] = []
    for band in cluster_1d([l.bbox.y0 for l in lines], tolerance):
        band_lines = sorted((lines[i] for i in band), key=lambda l: (l.bbox.x0, l.bbox.y0))
        result.extend(band_lines)
    return result
