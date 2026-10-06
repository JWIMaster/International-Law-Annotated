#!/usr/bin/env python3
"""Deterministic PDF fixture generator for the PDF-parsing regression suite.

Pure standard library.  Writes every fixture PDF into ``tests/fixtures/pdf/``
and a machine readable description of all of them into
``tests/fixtures/manifest.json``.

Run with::

    python3 tests/fixtures/make_fixtures.py

The generator is deterministic and idempotent: it uses no timestamps, no
randomness and no compression, so running it twice produces byte-identical
files.

The PDFs are written by hand (objects, xref table with byte offsets, trailer
and startxref).  Only the base-14 fonts Helvetica, Helvetica-Bold,
Times-Roman and Times-Italic are used, with WinAnsiEncoding, so pdftotext
extracts the literal ASCII text without any font embedding or ToUnicode CMap.

Coordinate convention: all geometry in the manifest is PDF user space, origin
at the bottom left of the *unrotated* MediaBox, in points.  Annotation ``/Rect``
values are recorded exactly as stored in the PDF (unrotated user space), even
for pages that carry ``/Rotate``.
"""

import hashlib
import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
PDF_DIR = HERE / "pdf"
MANIFEST_PATH = HERE / "manifest.json"

# ---------------------------------------------------------------------------
# Standard-14 font metrics (AFM widths, units/1000 em, ASCII 32..126)
# ---------------------------------------------------------------------------

_HELV = (
    [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278]
    + [556] * 10
    + [278, 278, 584, 584, 584, 556, 1015]
    + [667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
       667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611]
    + [278, 278, 278, 469, 556, 333]
    + [556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
       556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500]
    + [334, 260, 334, 584]
)

_HELV_BOLD = (
    [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278]
    + [556] * 10
    + [333, 333, 584, 584, 584, 611, 975]
    + [722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
       667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611]
    + [333, 278, 333, 584, 556, 333]
    + [556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
       611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500]
    + [389, 280, 389, 584]
)

_TIMES = (
    [250, 333, 408, 500, 500, 833, 778, 180, 333, 333, 500, 564, 250, 333, 250, 278]
    + [500] * 10
    + [278, 278, 564, 564, 564, 444, 921]
    + [722, 667, 667, 722, 611, 556, 722, 722, 333, 389, 722, 611, 889, 722, 722,
       556, 722, 667, 556, 611, 722, 722, 944, 722, 722, 611]
    + [333, 278, 333, 469, 500, 333]
    + [444, 500, 444, 500, 444, 333, 500, 500, 278, 278, 500, 278, 778, 500, 500,
       500, 500, 333, 389, 278, 500, 500, 722, 500, 500, 444]
    + [480, 200, 480, 541]
)

FONT_WIDTHS = {
    "F1": _HELV,
    "F2": _HELV_BOLD,
    "F3": _TIMES,
    "F4": _TIMES,  # Times-Italic, approximated with the Times-Roman widths
}

FONT_BASEFONT = {
    "F1": "Helvetica",
    "F2": "Helvetica-Bold",
    "F3": "Times-Roman",
    "F4": "Times-Italic",
}

# Convenience aliases used by the fixture definitions.
HELV = "F1"
HELV_BOLD = "F2"
TIMES = "F3"
TIMES_ITALIC = "F4"


def text_width(text, font="F1", size=11.0):
    """Width of *text* in points using the embedded standard-14 metrics."""
    table = FONT_WIDTHS[font]
    total = 0
    for ch in text:
        code = ord(ch)
        if 32 <= code <= 126:
            total += table[code - 32]
        else:
            total += 556
    return total / 1000.0 * size


# ---------------------------------------------------------------------------
# Low level PDF primitives
# ---------------------------------------------------------------------------

def fmt(value):
    """Format a number deterministically for a content stream / dictionary."""
    if isinstance(value, int):
        return str(value)
    if value != value:  # NaN guard, should never happen
        return "0"
    text = "%.4f" % value
    text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        text = "0"
    return text


def esc(text):
    """Escape a literal PDF string."""
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _rect(values):
    return "[" + " ".join(fmt(v) for v in values) + "]"


class Content:
    """Accumulates content-stream operators."""

    def __init__(self):
        self.parts = []

    def raw(self, operator):
        self.parts.append(operator)

    def text(self, x, y, text, font="F1", size=11.0, angle=0):
        """Emit one text-showing paragraph: ``BT /F1 12 Tf x y Td (t) Tj ET``."""
        body = "BT /%s %s Tf " % (font, fmt(size))
        if angle == 0:
            body += "%s %s Td" % (fmt(x), fmt(y))
        elif angle == 90:
            body += "0 1 -1 0 %s %s Tm" % (fmt(x), fmt(y))
        elif angle == 270:
            body += "0 -1 1 0 %s %s Tm" % (fmt(x), fmt(y))
        else:  # arbitrary rotation in degrees
            rad = math.radians(angle)
            c, s = round(math.cos(rad), 6), round(math.sin(rad), 6)
            body += "%s %s %s %s %s %s Tm" % (fmt(c), fmt(s), fmt(-s), fmt(c), fmt(x), fmt(y))
        body += " (%s) Tj ET" % esc(text)
        self.parts.append(body)

    def rect(self, x, y, w, h, fill=None, stroke=None, line_width=1.0):
        """Rectangle: fill gray, stroke gray, or both."""
        if fill is not None:
            self.parts.append("%s g" % fmt(fill))
        if stroke is not None:
            self.parts.append("%s G" % fmt(stroke))
        self.parts.append("%s w" % fmt(line_width))
        self.parts.append("%s %s %s %s re" % (fmt(x), fmt(y), fmt(w), fmt(h)))
        if fill is not None and stroke is not None:
            self.parts.append("B")
        elif fill is not None:
            self.parts.append("f")
        else:
            self.parts.append("S")

    def line(self, x1, y1, x2, y2, gray=0.0, line_width=1.0):
        self.parts.append("%s G" % fmt(gray))
        self.parts.append("%s w" % fmt(line_width))
        self.parts.append("%s %s m %s %s l S" % (fmt(x1), fmt(y1), fmt(x2), fmt(y2)))

    def polyline(self, points, gray=0.0, line_width=1.0, close=False):
        self.parts.append("%s G" % fmt(gray))
        self.parts.append("%s w" % fmt(line_width))
        self.parts.append("%s %s m" % (fmt(points[0][0]), fmt(points[0][1])))
        for px, py in points[1:]:
            self.parts.append("%s %s l" % (fmt(px), fmt(py)))
        if close:
            self.parts.append("h")
        self.parts.append("S")

    def filled_path(self, points, gray=0.5, stroke=None, line_width=1.0, close=True):
        if gray is not None:
            self.parts.append("%s g" % fmt(gray))
        if stroke is not None:
            self.parts.append("%s G" % fmt(stroke))
            self.parts.append("%s w" % fmt(line_width))
        self.parts.append("%s %s m" % (fmt(points[0][0]), fmt(points[0][1])))
        for px, py in points[1:]:
            self.parts.append("%s %s l" % (fmt(px), fmt(py)))
        if close:
            self.parts.append("h")
        self.parts.append("B" if stroke is not None else "f")

    def draw_image(self, name, x, y, w, h):
        self.parts.append("q %s 0 0 %s %s %s cm /%s Do Q"
                          % (fmt(w), fmt(h), fmt(x), fmt(y), name))

    def bytes(self):
        if not self.parts:
            return b""
        return ("\n".join(self.parts) + "\n").encode("latin-1")


class PDFBuilder:
    """Minimal hand-rolled PDF writer with a correct xref table."""

    def __init__(self):
        self._objs = {}
        self._next = 1

    def reserve(self):
        number = self._next
        self._next += 1
        return number

    def add(self, body):
        number = self.reserve()
        self._objs[number] = body
        return number

    def set(self, number, body):
        self._objs[number] = body

    def stream_obj(self, data, entries=""):
        head = "<< /Length %d" % len(data)
        if entries:
            head += " " + entries
        head += " >>\nstream\n"
        return head.encode("latin-1") + data + b"\nendstream"

    def build(self, root_number):
        out = bytearray()
        out += b"%PDF-1.7\n"
        out += b"%\xe2\xe3\xcf\xd3\n"
        offsets = {}
        for number in sorted(self._objs):
            offsets[number] = len(out)
            out += ("%d 0 obj\n" % number).encode("ascii")
            out += self._objs[number]
            out += b"\nendobj\n"
        xref_offset = len(out)
        size = self._next
        out += b"xref\n"
        out += ("0 %d\n" % size).encode("ascii")
        out += b"0000000000 65535 f \n"
        for number in range(1, size):
            if number in offsets:
                out += ("%010d 00000 n \n" % offsets[number]).encode("ascii")
            else:
                out += b"0000000000 65535 f \n"
        out += b"trailer\n"
        out += ("<< /Size %d /Root %d 0 R /ID [<0123456789abcdef0123456789abcdef>"
                " <0123456789abcdef0123456789abcdef>] >>\n" % (size, root_number)).encode("ascii")
        out += b"startxref\n"
        out += ("%d\n" % xref_offset).encode("ascii")
        out += b"%%EOF\n"
        return bytes(out)


# ---------------------------------------------------------------------------
# Document model
# ---------------------------------------------------------------------------

A4_W, A4_H = 595.276, 841.89
LETTER_W, LETTER_H = 612.0, 792.0


def make_image_rgb():
    """Deterministic 8x8 DeviceRGB raw image (192 bytes)."""
    data = bytearray()
    for row in range(8):
        for col in range(8):
            data += bytes([(col * 32) % 256, (row * 32) % 256, ((col + row) * 16) % 256])
    return bytes(data)


IMAGE_RGB = make_image_rgb()


class Page:
    def __init__(self, width, height, rotate=0):
        self.width = width
        self.height = height
        self.rotate = rotate
        self.content = Content()
        self.annots = []          # annotation spec dicts
        self.xobjects = {}        # resource name -> raw bytes
        self.xobject_meta = {}    # resource name -> dict
        self.text_lines = []
        self.has_vector = False

    def put_text(self, text, x, y, font="F1", size=11.0, angle=0):
        self.content.text(x, y, text, font=font, size=size, angle=angle)
        self.text_lines.append({
            "text": text,
            "x": round(float(x), 4),
            "y": round(float(y), 4),
            "font": font,
            "size": float(size),
            "angle": angle,
        })

    def add_image(self, name, x, y, w, h, raw=None, pixel_width=8, pixel_height=8,
                  colorspace="DeviceRGB"):
        raw = IMAGE_RGB if raw is None else raw
        self.xobjects[name] = raw
        self.xobject_meta[name] = {
            "name": name,
            "pixel_width": pixel_width,
            "pixel_height": pixel_height,
            "colorspace": colorspace,
        }
        self.content.draw_image(name, x, y, w, h)
        return {"x": float(x), "y": float(y), "width": float(w), "height": float(h),
                "pixel_width": pixel_width, "pixel_height": pixel_height,
                "colorspace": colorspace}

    def add_annot(self, spec):
        self.annots.append(spec)


class Fixture:
    def __init__(self, name, description, guards):
        self.name = name
        self.description = description
        self.guards = guards
        self.pages = []
        self.headings = []
        self.paragraphs = []
        self.images = []
        self.vectors = []
        self.annotations = []
        self.running_elements = []

    def new_page(self, width=A4_W, height=A4_H, rotate=0):
        page = Page(width, height, rotate)
        self.pages.append(page)
        return page

    def heading(self, page_index, text, x, y, size=13.0, font=HELV_BOLD, level=1):
        page = self.pages[page_index]
        page.put_text(text, x, y, font=font, size=size)
        self.headings.append({
            "text": text,
            "level": level,
            "page": page_index + 1,
            "font": font,
            "size": float(size),
            "x": round(float(x), 4),
            "y": round(float(y), 4),
        })

    def draw_lines(self, page_index, lines, x, y, font=HELV, size=11.0,
                   leading=14.0, hanging=0.0):
        page = self.pages[page_index]
        records = []
        cursor = float(y)
        for index, line in enumerate(lines):
            lx = float(x) + (hanging if index > 0 else 0.0)
            page.put_text(line, lx, cursor, font=font, size=size)
            records.append({
                "page": page_index + 1,
                "text": line,
                "x": round(lx, 4),
                "y": round(cursor, 4),
                "font": font,
                "size": float(size),
            })
            cursor -= leading
        return records

    def paragraph(self, page_index, lines, x, y, font=HELV, size=11.0,
                  leading=14.0, hanging=0.0, column=None):
        records = self.draw_lines(page_index, lines, x, y, font=font, size=size,
                                  leading=leading, hanging=hanging)
        self.record_paragraph(lines, records, column=column, font=font, size=size)
        return records

    def record_paragraph(self, lines, line_records, column=None, font=HELV, size=11.0):
        pages = sorted({rec["page"] for rec in line_records})
        self.paragraphs.append({
            "text": " ".join(line.strip() for line in lines),
            "pages": pages,
            "column": column,
            "font": font,
            "size": float(size),
            "lines": line_records,
        })

    def vector(self, page_index, kind, geometry):
        record = {"page": page_index + 1, "type": kind}
        record.update(geometry)
        self.vectors.append(record)
        self.pages[page_index].has_vector = True

    # -- manifest -----------------------------------------------------------
    def manifest(self):
        pages = []
        for index, page in enumerate(self.pages):
            pages.append({
                "page": index + 1,
                "width": page.width,
                "height": page.height,
                "rotation": page.rotate,
                "has_text": bool(page.text_lines),
                "has_images": bool(page.xobjects),
                "has_vector_graphics": bool(page.has_vector),
                "text_lines": page.text_lines,
            })
        return {
            "file": self.name,
            "description": self.description,
            "guards": self.guards,
            "page_count": len(self.pages),
            "pages": pages,
            "headings": self.headings,
            "paragraphs": self.paragraphs,
            "images": self.images,
            "vector_elements": self.vectors,
            "annotations": self.annotations,
            "running_elements": self.running_elements,
        }


# ---------------------------------------------------------------------------
# Annotation helpers
# ---------------------------------------------------------------------------

AUTHOR_A = "A. Researcher"
AUTHOR_B = "B. Editor"
AUTHOR_C = "C. Reviewer"


def line_quad(x1, y_baseline, x2, size):
    """Quad points for a single text line covering (baseline .. baseline+size)."""
    top = y_baseline + size * 0.92
    bottom = y_baseline - size * 0.28
    return [x1, top, x2, top, x1, bottom, x2, bottom]


def highlight_spec(rect, quad, contents, author, color=None):
    return {
        "subtype": "Highlight",
        "rect": [round(float(v), 4) for v in rect],
        "quad_points": [round(float(v), 4) for v in quad],
        "contents": contents,
        "author": author,
        "color": color if color is not None else [1, 0.85, 0.2],
        "covers": None,
        "refers_to": None,
    }


def text_note_spec(rect, contents, author, refers_to=None):
    return {
        "subtype": "Text",
        "rect": [round(float(v), 4) for v in rect],
        "contents": contents,
        "author": author,
        "color": [0.9, 0.8, 0.1],
        "covers": None,
        "refers_to": refers_to,
    }


def underline_spec(rect, quad, contents, author, subtype="Underline", color=None):
    return {
        "subtype": subtype,
        "rect": [round(float(v), 4) for v in rect],
        "quad_points": [round(float(v), 4) for v in quad],
        "contents": contents,
        "author": author,
        "color": color if color is not None else [0, 0, 1],
        "covers": None,
        "refers_to": None,
    }


def square_spec(rect, contents, author, color=None, refers_to=None):
    return {
        "subtype": "Square",
        "rect": [round(float(v), 4) for v in rect],
        "contents": contents,
        "author": author,
        "color": color if color is not None else [0, 0.6, 0],
        "covers": None,
        "refers_to": refers_to,
    }


def annot_record(page_index, spec):
    return {
        "page": page_index + 1,
        "subtype": spec["subtype"],
        "rect": spec["rect"],
        "author": spec.get("author"),
        "contents": spec.get("contents"),
        "color": spec.get("color"),
        "quad_points": spec.get("quad_points"),
        "covers": spec.get("covers"),
        "refers_to": spec.get("refers_to"),
    }


def annot_body(spec, page_number):
    body = "<< /Type /Annot /Subtype /%s /Rect %s /F 4 /P %d 0 R" % (
        spec["subtype"], _rect(spec["rect"]), page_number)
    if spec.get("quad_points"):
        body += " /QuadPoints %s" % _rect(spec["quad_points"])
    if spec.get("contents"):
        body += " /Contents (%s)" % esc(spec["contents"])
    if spec.get("author"):
        body += " /T (%s)" % esc(spec["author"])
    if spec.get("color"):
        body += " /C %s" % _rect(spec["color"])
    if spec["subtype"] == "Text":
        body += " /Name /Comment"
    if spec["subtype"] == "Square":
        body += " /BS << /W 2 >>"
    body += " >>"
    return body.encode("latin-1")


# ---------------------------------------------------------------------------
# Serialisation of a Fixture to PDF bytes
# ---------------------------------------------------------------------------

def build_pdf(fixture):
    pdf = PDFBuilder()
    font_numbers = {}
    for name, base in FONT_BASEFONT.items():
        font_numbers[name] = pdf.add(
            ("<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>"
             % base).encode("ascii"))

    pages_number = pdf.reserve()
    page_numbers = []

    for page in fixture.pages:
        page_number = pdf.reserve()
        page_numbers.append(page_number)

        xobject_numbers = {}
        for name, raw in page.xobjects.items():
            meta = page.xobject_meta[name]
            entries = ("/Type /XObject /Subtype /Image /Width %d /Height %d "
                       "/ColorSpace /%s /BitsPerComponent 8"
                       % (meta["pixel_width"], meta["pixel_height"], meta["colorspace"]))
            xobject_numbers[name] = pdf.add(pdf.stream_obj(raw, entries))

        annot_numbers = []
        for spec in page.annots:
            annot_numbers.append(pdf.add(annot_body(spec, page_number)))

        content_number = pdf.add(pdf.stream_obj(page.content.bytes()))

        resources = "<< /ProcSet [/PDF /Text /ImageB /ImageC /ImageI] /Font << "
        for name in sorted(font_numbers):
            resources += "/%s %d 0 R " % (name, font_numbers[name])
        resources += ">>"
        if xobject_numbers:
            resources += " /XObject << "
            for name in sorted(xobject_numbers):
                resources += "/%s %d 0 R " % (name, xobject_numbers[name])
            resources += ">>"
        resources += " >>"

        body = ("<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %s %s] /Resources %s "
                "/Contents %d 0 R" % (pages_number, fmt(page.width), fmt(page.height),
                                      resources, content_number))
        if page.rotate:
            body += " /Rotate %d" % page.rotate
        if annot_numbers:
            body += " /Annots [" + " ".join("%d 0 R" % n for n in annot_numbers) + "]"
        body += " >>"
        pdf.set(page_number, body.encode("ascii"))

    pdf.set(pages_number,
            ("<< /Type /Pages /Kids [%s] /Count %d >>"
             % (" ".join("%d 0 R" % n for n in page_numbers), len(page_numbers))
             ).encode("ascii"))
    catalog_number = pdf.add(
        ("<< /Type /Catalog /Pages %d 0 R >>" % pages_number).encode("ascii"))
    return pdf.build(catalog_number)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def fixture_simple():
    fx = Fixture(
        "simple.pdf",
        "One A4 page: a Helvetica-Bold 16 title followed by three short plain "
        "paragraphs with no numbering or markers.",
        "Baseline text extraction: fonts, sizes, one Tj per line, paragraph "
        "joining and page/paragraph attribution on the simplest possible input.")
    page = fx.new_page()
    fx.heading(0, "A Short Note on Treaties", 72, 750, size=16, level=1)
    fx.paragraph(0, [
        "The first paragraph makes a short and plain statement about treaties.",
    ], 72, 700)
    fx.paragraph(0, [
        "The second paragraph adds a little more detail about customary",
        "international law and the way that it slowly develops over time.",
    ], 72, 660)
    fx.paragraph(0, [
        "The third paragraph closes the page with three lines of ordinary",
        "running text that a parser should be able to join back into",
        "a single block without any trouble at all.",
    ], 72, 600)
    return fx


def fixture_paragraphs():
    fx = Fixture(
        "paragraphs.pdf",
        "One A4 page: a heading, three numbered paragraphs (\'1.\', \'2.\', \'3.\') "
        "with several lines each, and a lettered sub-list under paragraph 3.",
        "Numbered-paragraph detection, hanging-indent line joining, and "
        "distinguishing a sub-list from the numbered paragraphs.")
    fx.new_page()
    fx.heading(0, "Article 3", 72, 770, size=13, level=1)
    fx.paragraph(0, [
        "1. The present Convention shall apply to treaties between States.",
        "This paragraph is short but still occupies more than a single line",
        "so that line-joining behaviour can be checked.",
    ], 72, 725, hanging=14)
    fx.paragraph(0, [
        "2. For the purposes of the present Convention, a treaty means an",
        "international agreement concluded between States in written form",
        "and governed by international law, whether embodied in a single",
        "instrument or in two or more related instruments.",
    ], 72, 660, hanging=14)
    fx.paragraph(0, [
        "3. The provisions of this Article do not apply to agreements",
        "concluded between a State and an international organisation.",
    ], 72, 575, hanging=14)
    fx.paragraph(0, [
        "(a) unless the parties have so agreed in writing; and",
    ], 90, 535)
    fx.paragraph(0, [
        "(b) unless the agreement is deposited with the depositary.",
    ], 90, 510)
    return fx


def fixture_paragraphs_paged():
    fx = Fixture(
        "paragraphs_paged.pdf",
        "Two A4 pages. Paragraph 2 starts near the bottom of page 1 and its "
        "text continues at the top of page 2, with a running header on page 2 "
        "between the two halves.",
        "Cross-page paragraph continuation: a naive parser inserts the running "
        "header into the paragraph and splits it, so line/paragraph joining must "
        "recognise headers and mid-sentence continuations.")
    p1 = fx.new_page()
    fx.heading(0, "Article 9", 72, 780, size=13, level=1)
    fx.paragraph(0, [
        "1. A treaty shall be interpreted in good faith and in accordance",
        "with the ordinary meaning to be given to its terms.",
    ], 72, 745, hanging=14)
    line_records_a = fx.draw_lines(0, [
        "2. A treaty shall be interpreted in good faith in accordance with the",
        "ordinary meaning to be given to the terms of the treaty in their",
        "context and in the light of its object and purpose, and this",
        "example sentence is deliberately made long enough that it has to",
    ], 72, 690, hanging=14)
    # Page 2: running header sits between the two halves of paragraph 2.
    p2 = fx.new_page()
    p2.put_text("Article 9 (continued)", 72, 800, font=HELV_BOLD, size=10)
    fx.running_elements.append({
        "kind": "running_header",
        "text": "Article 9 (continued)",
        "page": 2,
        "font": HELV_BOLD,
        "size": 10.0,
        "x": 72.0,
        "y": 800.0,
    })
    line_records_b = fx.draw_lines(1, [
        "continue onto the following page.",
        "3. The context for the purpose of the interpretation of a treaty",
        "shall comprise, in addition to the text, its preamble and annexes.",
    ], 72, 760, hanging=14)
    fx.record_paragraph([
        "2. A treaty shall be interpreted in good faith in accordance with the",
        "ordinary meaning to be given to the terms of the treaty in their",
        "context and in the light of its object and purpose, and this",
        "example sentence is deliberately made long enough that it has to",
        "continue onto the following page.",
    ], line_records_a + line_records_b, font=HELV, size=11)
    return fx


def fixture_twocolumn():
    fx = Fixture(
        "twocolumn.pdf",
        "One A4 page with a full-width title and two clearly separated columns "
        "(left x=50..280, right x=315..545). The content stream emits every "
        "left-column line before any right-column line, but the y positions of "
        "the two columns interleave.",
        "Column detection: a naive y-descending sort interleaves the two columns "
        "into nonsense, so the parser must separate columns by x before ordering "
        "lines within each column.")
    page = fx.new_page()
    fx.heading(0, "Two Column Layout Test", 150, 775, size=14, level=1)

    left = [
        ([
            "LEFT alpha begins the first left",
            "column paragraph and continues.",
        ], 720.0),
        ([
            "LEFT beta opens the second left",
            "paragraph with more of its text.",
        ], 664.0),
        ([
            "LEFT gamma is the third and it",
            "closes the left column of text.",
        ], 608.0),
    ]
    right = [
        ([
            "RIGHT alpha begins the first right",
            "column paragraph and continues.",
        ], 713.0),
        ([
            "RIGHT beta opens the second right",
            "paragraph with more of its text.",
        ], 657.0),
        ([
            "RIGHT gamma is the third and it",
            "closes the right column of text.",
        ], 601.0),
    ]
    # Emission order: every left line first ...
    for lines, y in left:
        fx.paragraph(0, lines, 50, y, leading=14, column="left")
    # ... then every right line, with interleaved y positions.
    for lines, y in right:
        fx.paragraph(0, lines, 315, y, leading=14, column="right")
    return fx


def fixture_fontsizes():
    fx = Fixture(
        "fontsizes.pdf",
        "One A4 page mixing a 20pt Helvetica-Bold title, a 14pt Helvetica-Bold "
        "section heading, an 11pt Helvetica body paragraph, a 9pt footnote and "
        "one Times-Italic run.",
        "Font and size recovery: heading detection from size, body versus "
        "footnote classification, and italic run detection.")
    fx.new_page()
    fx.heading(0, "Type Size Title", 72, 760, size=20, font=HELV_BOLD, level=1)
    fx.heading(0, "Section 1. Type Sizes", 72, 720, size=14, font=HELV_BOLD, level=2)
    fx.paragraph(0, [
        "This body paragraph is set in eleven point Helvetica and it",
        "spans two short lines for the parser to join.",
    ], 72, 688, font=HELV, size=11)
    italic_line = ["This run is set in Times Italic at eleven point."]
    fx.paragraph(0, italic_line, 72, 645, font=TIMES_ITALIC, size=11)
    fx.paragraph(0, [
        "1. This footnote is set in nine point Helvetica.",
    ], 72, 605, font=HELV, size=9)
    return fx


def fixture_image():
    fx = Fixture(
        "image.pdf",
        "Two A4 pages. Page 1 has a paragraph plus an image XObject in the "
        "middle third of the page. Page 2 is a single image covering almost "
        "the whole page with no text at all (a scanned page).",
        "Image XObject discovery and image-only / scanned pages: text extraction "
        "returns nothing for page 2 and image coverage must be measured.")
    p1 = fx.new_page()
    fx.paragraph(0, [
        "The first page carries a short paragraph of text together",
        "with an image in the middle third of the page.",
    ], 72, 760)
    info = p1.add_image("Im1", 100, 320, 400, 200)
    info["page"] = 1
    fx.images.append(info)

    p2 = fx.new_page()
    info2 = p2.add_image("Im1", 20, 20, 555, 800)
    info2["page"] = 2
    fx.images.append(info2)
    return fx


def fixture_vector():
    fx = Fixture(
        "vector.pdf",
        "One A4 page containing a simple vector diagram: stroked and filled "
        "rectangles, connecting lines and a filled triangular path, with a "
        "caption paragraph underneath.",
        "Vector graphics detection (re/f/S/m/l) as distinct from text and "
        "images, plus caption placement below a figure.")
    page = fx.new_page()
    page.content.rect(72, 700, 120, 80, stroke=0.0, line_width=1.5)
    fx.vector(0, "rect", {"x": 72.0, "y": 700.0, "width": 120.0, "height": 80.0,
                          "style": "stroke", "stroke_gray": 0.0})
    page.content.rect(220, 700, 120, 80, fill=0.75, stroke=0.0, line_width=1.5)
    fx.vector(0, "rect", {"x": 220.0, "y": 700.0, "width": 120.0, "height": 80.0,
                          "style": "fill+stroke", "fill_gray": 0.75, "stroke_gray": 0.0})
    page.content.line(192, 740, 220, 740)
    fx.vector(0, "line", {"x1": 192.0, "y1": 740.0, "x2": 220.0, "y2": 740.0})
    page.content.line(340, 740, 420, 740)
    fx.vector(0, "line", {"x1": 340.0, "y1": 740.0, "x2": 420.0, "y2": 740.0})
    page.content.line(420, 740, 420, 640)
    fx.vector(0, "line", {"x1": 420.0, "y1": 740.0, "x2": 420.0, "y2": 640.0})
    page.content.polyline([(72, 620), (160, 620), (160, 560)], gray=0.0,
                          line_width=1.0, close=False)
    fx.vector(0, "polyline", {"points": [[72, 620], [160, 620], [160, 560]]})
    page.content.filled_path([(220, 560), (300, 620), (380, 560)], gray=0.4)
    fx.vector(0, "filled_path", {"points": [[220, 560], [300, 620], [380, 560]],
                                 "fill_gray": 0.4})
    fx.paragraph(0, [
        "Figure 1. A simple vector diagram drawn with rectangles,",
        "strokes and a filled path.",
    ], 72, 500)
    return fx


def fixture_rotated():
    fx = Fixture(
        "rotated.pdf",
        "Two A4 pages. Page 1 has /Rotate 90 and page 2 has /Rotate 270; each "
        "carries two lines of text drawn so that they read left-to-right after "
        "rotation. Page 1 also carries a Highlight annotation.",
        "Rotated-page text and annotation coordinate mapping: /Rect values are "
        "stored in unrotated user space but displayed rotated, so callers must "
        "map between the two frames.")
    p1 = fx.new_page(rotate=90)
    # +y content direction becomes left-to-right after a 90 degree CW display rotation
    p1.put_text("Rotated page one reads left to right.", 72, 72, font=HELV, size=12, angle=90)
    p1.put_text("A second line on the rotated page.", 100, 72, font=HELV, size=12, angle=90)
    line1 = "Rotated page one reads left to right."
    w1 = text_width(line1, HELV, 12)
    spec = highlight_spec(
        [72 - 12, 72, 72 + 12, 72 + w1 + 4],
        line_quad(72, 72, 72 + w1 + 4, 12),
        "Rotated annotation anchor.",
        AUTHOR_A,
        color=[1, 0.85, 0.2],
    )
    spec["covers"] = line1
    p1.add_annot(spec)
    fx.annotations.append(annot_record(0, spec))

    p2 = fx.new_page(rotate=270)
    # -y content direction becomes left-to-right after a 270 degree display rotation
    p2.put_text("Rotated page two reads left to right.", 500, 769.89, font=HELV,
                size=12, angle=270)
    p2.put_text("Its rotation is two hundred and seventy.", 470, 769.89, font=HELV,
                size=12, angle=270)
    return fx


def fixture_pagesizes():
    fx = Fixture(
        "pagesizes.pdf",
        "Three pages with different media boxes: A4 portrait, US Letter "
        "portrait and a true A4 landscape MediaBox with /Rotate 0.",
        "Page-size handling and landscape detection from the MediaBox rather "
        "than from /Rotate.")
    p1 = fx.new_page(A4_W, A4_H)
    fx.paragraph(0, ["Page one uses A4 portrait dimensions."], 72, 700)
    p2 = fx.new_page(LETTER_W, LETTER_H)
    fx.paragraph(1, ["Page two uses US Letter portrait dimensions."], 72, 700)
    p3 = fx.new_page(A4_H, A4_W)
    fx.paragraph(2, ["Page three uses A4 landscape dimensions."], 72, 450)
    return fx


def fixture_headers():
    fx = Fixture(
        "headers.pdf",
        "Four A4 pages. Every page carries the running header "
        "\'International Law Review\', the running footer \'Page N of 4\', and "
        "body text that flows continuously across page breaks. Page 2 also "
        "carries a standalone centred page number line \'- 2 -\'.",
        "Running header/footer removal and continuous body flow: the body "
        "paragraph is one logical block spanning four pages and each page "
        "after the first starts mid-sentence.")
    body_lines = [
        "The development of international law has always depended upon the patient",
        "accumulation of State practice and the considered reflection of scholars and",
        "tribunals alike. Treaties record the settled will of States, while custom grows",
        "from conduct that is both widespread and accepted as law. When a dispute",
        "arises, the parties look first to their own agreements, then to the general",
        "principles that the community of nations has come to treat as binding. In this",
        "way the legal order expands, not by sudden invention, but by the slow and",
        "careful extension of rules that have already proved their worth in practice.",
        "The same process can be seen in the work of codification, where settled rules",
        "are restated with greater precision and the gaps between them are filled by",
        "agreement. Each generation inherits a body of doctrine and adds to it,",
        "sometimes by treaty, sometimes by the quiet weight of consistent conduct.",
        "The result is a legal order that is neither static nor wholly new but always",
        "growing at the edges, and for the student of the subject the lesson is that",
        "no single instrument tells the whole story; the careful reader must attend",
        "to practice as closely as to the text itself.",
    ]
    per_page = 4
    all_records = []
    for page_index in range(4):
        page = fx.new_page()
        page.put_text("International Law Review", 72, 800, font=HELV_BOLD, size=10)
        fx.running_elements.append({
            "kind": "running_header", "text": "International Law Review",
            "page": page_index + 1, "font": HELV_BOLD, "size": 10.0,
            "x": 72.0, "y": 800.0,
        })
        page.put_text("Page %d of 4" % (page_index + 1), 72, 50, font=HELV, size=9)
        fx.running_elements.append({
            "kind": "running_footer", "text": "Page %d of 4" % (page_index + 1),
            "page": page_index + 1, "font": HELV, "size": 9.0,
            "x": 72.0, "y": 50.0,
        })
        if page_index == 1:
            page.put_text("- 2 -", 285, 100, font=HELV, size=10)
            fx.running_elements.append({
                "kind": "page_number", "text": "- 2 -", "page": 2,
                "font": HELV, "size": 10.0, "x": 285.0, "y": 100.0,
            })
        chunk = body_lines[page_index * per_page:(page_index + 1) * per_page]
        all_records.extend(fx.draw_lines(page_index, chunk, 72, 760, font=HELV,
                                         size=11, leading=14))
    fx.record_paragraph(body_lines, all_records, column=None, font=HELV, size=11)
    return fx


def fixture_empty():
    fx = Fixture(
        "empty.pdf",
        "Two A4 pages whose content streams are completely empty.",
        "Empty content streams: no text, no images, no annotations; parsers must "
        "not crash and must report two blank pages.")
    fx.new_page()
    fx.new_page()
    return fx


def fixture_minimal():
    fx = Fixture(
        "minimal.pdf",
        "One A4 page whose content stream draws nothing but a single tiny "
        "\'.\' character.",
        "Degenerate-but-valid input: a page with one punctuation glyph should be "
        "reported as having (almost) no text rather than as an error.")
    page = fx.new_page()
    page.put_text(".", 300, 400, font=HELV, size=12)
    fx.paragraph(0, ["."], 300, 400, font=HELV, size=12)
    return fx


def fixture_generic():
    """An ordinary, non-legal document.

    Everything in the parser has to work on documents that are not court
    orders: a short leaflet with a cover, a contents list, numbered steps and
    a quoted specification.  A rule that only behaves on long legal texts is
    not a rule.
    """
    fx = Fixture(
        "generic.pdf",
        "Two A4 pages of ordinary non-legal prose: a cover page with a "
        "contents list, then three numbered steps and a quoted specification, "
        "with a running header and footer.",
        "Generality guard: the parser must not assume legal structure. The "
        "back-matter rule in particular must not delete a short document that "
        "merely has a contents list on its first page.")
    cover = fx.new_page()
    cover.put_text("AUTUMN CATALOGUE 2024", 200, 700, font=HELV_BOLD, size=16)
    cover.put_text("Kitchen Tools and Utensils", 200, 670, font=HELV, size=12)
    cover.put_text("Contents", 72, 600, font=HELV_BOLD, size=11)
    for index, entry in enumerate(("Saucepans .......... 4", "Skillets .......... 9",
                                   "Utensils .......... 17",
                                   "Care instructions .......... 24")):
        cover.put_text(entry, 72, 580 - index * 14, font=HELV, size=11)

    body = fx.new_page()
    body.put_text("Autumn Catalogue 2024 - Kitchen Tools", 72, 800, font=HELV, size=9)
    y = 740.0
    groups = [
        ["1. Saucepans are made from three-ply stainless steel and they are",
         "suitable for all hob types including induction, and they carry a",
         "lifetime guarantee against manufacturing defects."],
        ["2. Skillets share the same construction but have a lower profile,",
         "which makes them better suited to searing and to reducing sauces",
         "quickly over a high heat."],
        ["3. Handles stay cool on the hob and are secured with a riveted",
         "fixing that can be tightened with a standard screwdriver."],
    ]
    for group in groups:
        for line in group:
            body.put_text(line, 72, y, font=HELV, size=11)
            fx.paragraph(1, [line], 72, y, font=HELV, size=11)
            y -= 16.0
        y -= 12.0
    for line in ['The manufacturer states: "(1) the warranty covers manufacturing',
                 'defects only; (2) it excludes damage from misuse; and (3) it',
                 'requires proof of purchase."']:
        body.put_text(line, 72, y, font=HELV, size=11)
        fx.paragraph(1, [line], 72, y, font=HELV, size=11)
        y -= 16.0
    body.put_text("Page 2 of 2", 72, 50, font=HELV, size=9)
    return fx


def fixture_hidden_text():
    """Text drawn with the invisible rendering mode -- an OCR text layer.

    This is what a searchable scanned judgment looks like: the page is one
    big image, and the text sits underneath it with rendering mode 3.
    ``pdftohtml`` only emits it when passed ``-hidden``, and forgetting that
    turned a fully searchable scanned document into an "empty" one.
    """
    fx = Fixture(
        "hiddentext.pdf",
        "One A4 page whose text is drawn with the invisible text rendering "
        "mode (3 Tr), as an OCR layer over a scanned image.",
        "Regression guard: invisible (OCR) text must still be extracted. Both "
        "backends must return the two lines; a parser that silently drops "
        "render-mode-3 text reports the page as having no text at all.")
    page = fx.new_page()
    page.add_image("Im1", 40, 60, 515, 700)
    page.content.raw("3 Tr")
    lines = [
        "The Court finds that the Treaty is still in force.",
        "Both Parties are under an obligation to negotiate in good faith.",
    ]
    y = 700.0
    for line in lines:
        # fx.paragraph() draws the line; calling put_text as well would draw
        # it twice, which is not what an OCR layer looks like.
        fx.paragraph(0, [line], 72, y, font=HELV, size=11)
        y -= 16.0
    page.content.raw("0 Tr")
    return fx


def fixture_annotated():
    fx = Fixture(
        "annotated.pdf",
        "Two A4 pages of numbered body paragraphs (six in total) carrying every "
        "required annotation subtype. Page 1: a Highlight over the middle of "
        "paragraph 2, a Text sticky note in the right margin beside paragraph 3, "
        "an Underline over paragraph 4 and a Square in the margin. Page 2: a "
        "StrikeOut over paragraph 5 and two overlapping annotations (Highlight "
        "plus Text note) over paragraph 6.",
        "Annotation parsing: subtype, /Rect, /QuadPoints, /Contents, /T author "
        "and colour; rect-to-text mapping; overlapping annotations; and "
        "input-order versus sorted-order of the /Annots array.")
    # ---- page 1 ----
    p1 = fx.new_page()
    fx.heading(0, "Article 14", 72, 780, size=13, level=1)

    line1 = "1. The first paragraph is short and occupies a single line of this page."
    fx.paragraph(0, [line1], 72, 745)

    p2_lines = [
        "2. The second paragraph is deliberately longer so that it has a middle line",
        "which a highlight annotation can be placed over without covering the whole",
        "of the paragraph, and it finishes here with a final line that remains",
        "entirely unmarked by any annotation at all.",
    ]
    recs = fx.paragraph(0, p2_lines, 72, 710, leading=14)

    p3_lines = [
        "3. The third paragraph sits below the highlighted one and it is used as",
        "the anchor for a sticky note placed in the right margin.",
    ]
    fx.paragraph(0, p3_lines, 72, 630, leading=14)

    p4_lines = [
        "4. The fourth paragraph is underlined in full so that an underline",
        "annotation can be checked against a known pair of sentences.",
    ]
    p4_recs = fx.paragraph(0, p4_lines, 72, 578, leading=14)

    middle_line = p2_lines[1]  # second line of paragraph 2
    mid_x2 = 72 + text_width(middle_line, HELV, 11) + 4
    highlight = highlight_spec(
        [72 - 2, recs[1]["y"] - 3, mid_x2, recs[1]["y"] + 12],
        line_quad(72 - 2, recs[1]["y"], mid_x2, 11),
        "Middle line of paragraph 2 - verify the quotation.",
        AUTHOR_A,
        color=[1, 0.85, 0.2],
    )
    highlight["covers"] = middle_line
    p1.add_annot(highlight)

    note = text_note_spec(
        [500, 616, 522, 638],
        "Sticky note beside paragraph 3 in the right margin.",
        AUTHOR_B,
        refers_to=p3_lines[0] + " " + p3_lines[1],
    )
    p1.add_annot(note)

    underline_x1, underline_x2 = 72 - 2, 72 + max(
        text_width(p4_lines[0], HELV, 11), text_width(p4_lines[1], HELV, 11)) + 4
    underline = underline_spec(
        [underline_x1, p4_recs[1]["y"] - 3, underline_x2, p4_recs[0]["y"] + 12],
        line_quad(underline_x1, p4_recs[0]["y"], underline_x2, 11)
        + line_quad(underline_x1, p4_recs[1]["y"], underline_x2, 11),
        "Paragraph 4 is the key holding of the decision.",
        AUTHOR_A,
        subtype="Underline",
        color=[0, 0, 1],
    )
    underline["covers"] = p4_lines[0] + " " + p4_lines[1]
    p1.add_annot(underline)

    square = square_spec(
        [500, 556, 540, 596],
        "Margin box placed beside paragraph 4.",
        AUTHOR_C,
        color=[0, 0.6, 0],
        refers_to=p4_lines[0] + " " + p4_lines[1],
    )
    p1.add_annot(square)

    # Deliberately non-sorted /Annots order (not by rect y): Square, Underline,
    # Highlight, Text.
    p1.annots = [square, underline, highlight, note]

    # ---- page 2 ----
    p2 = fx.new_page()
    p5_lines = [
        "5. The fifth paragraph is struck out because it is treated as",
        "having been superseded by a later agreement between the parties.",
    ]
    p5_recs = fx.paragraph(1, p5_lines, 72, 760, leading=14)

    p6_lines = [
        "6. The sixth paragraph carries two overlapping annotations, a highlight",
        "and a sticky note, whose rectangles are deliberately intended to",
        "intersect each other on this page.",
    ]
    p6_recs = fx.paragraph(1, p6_lines, 72, 700, leading=14)

    strike_x1, strike_x2 = 72 - 2, 72 + max(
        text_width(p5_lines[0], HELV, 11), text_width(p5_lines[1], HELV, 11)) + 4
    strike = underline_spec(
        [strike_x1, p5_recs[1]["y"] - 3, strike_x2, p5_recs[0]["y"] + 12],
        line_quad(strike_x1, p5_recs[0]["y"], strike_x2, 11)
        + line_quad(strike_x1, p5_recs[1]["y"], strike_x2, 11),
        "Paragraph 5 is superseded by the later agreement.",
        AUTHOR_B,
        subtype="StrikeOut",
        color=[1, 0, 0],
    )
    strike["covers"] = p5_lines[0] + " " + p5_lines[1]
    p2.add_annot(strike)

    p6_line1_x2 = 72 + text_width(p6_lines[0], HELV, 11) + 4
    overlap_highlight = highlight_spec(
        [72 - 2, p6_recs[0]["y"] - 3, p6_line1_x2, p6_recs[0]["y"] + 12],
        line_quad(72 - 2, p6_recs[0]["y"], p6_line1_x2, 11),
        "Highlight that overlaps the sticky note.",
        AUTHOR_A,
        color=[1, 0.85, 0.2],
    )
    overlap_highlight["covers"] = p6_lines[0]
    p2.add_annot(overlap_highlight)

    overlap_note = text_note_spec(
        [400, 694, 422, 716],
        "Sticky note whose rect overlaps the highlight.",
        AUTHOR_C,
        refers_to=p6_lines[0],
    )
    p2.add_annot(overlap_note)

    for page_index, page in enumerate(fx.pages):
        for spec in page.annots:
            fx.annotations.append(annot_record(page_index, spec))
    return fx


FIXTURE_BUILDERS = [
    fixture_simple,
    fixture_paragraphs,
    fixture_paragraphs_paged,
    fixture_twocolumn,
    fixture_fontsizes,
    fixture_image,
    fixture_vector,
    fixture_rotated,
    fixture_pagesizes,
    fixture_headers,
    fixture_empty,
    fixture_minimal,
    fixture_generic,
    fixture_hidden_text,
    fixture_annotated,
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    fixtures = []
    written = []

    for builder in FIXTURE_BUILDERS:
        fixture = builder()
        data = build_pdf(fixture)
        path = PDF_DIR / fixture.name
        path.write_bytes(data)
        entry = fixture.manifest()
        entry["bytes"] = len(data)
        entry["sha256"] = hashlib.sha256(data).hexdigest()
        # keep file before the digest fields in a stable position
        ordered = {
            "file": entry["file"],
            "description": entry["description"],
            "guards": entry["guards"],
            "page_count": entry["page_count"],
            "bytes": entry["bytes"],
            "sha256": entry["sha256"],
            "pages": entry["pages"],
            "headings": entry["headings"],
            "paragraphs": entry["paragraphs"],
            "images": entry["images"],
            "vector_elements": entry["vector_elements"],
            "annotations": entry["annotations"],
            "running_elements": entry["running_elements"],
        }
        fixtures.append(ordered)
        written.append((fixture.name, len(fixture.pages), len(data)))

    manifest = {
        "format": "ila-pdf-fixtures/1",
        "generator": "tests/fixtures/make_fixtures.py",
        "pdf_version": "1.7",
        "units": "points",
        "coordinate_system": (
            "PDF user space; origin bottom-left of the unrotated MediaBox. "
            "Annotation rects/quad points are recorded as stored in the PDF "
            "(unrotated), even when the page carries /Rotate."
        ),
        "field_notes": {
            "pages[].width/height": "unrotated MediaBox in points",
            "pages[].rotation": "the page /Rotate value in degrees (0, 90 or 270)",
            "paragraphs[].pages": "every page the paragraph appears on, so cross-page paragraphs have more than one entry",
            "paragraphs[].column": "'left'/'right' for twocolumn.pdf, else null",
            "paragraphs[].lines[].page": "page the individual physical line was drawn on",
            "annotations[].rect": "the raw /Rect array, unrotated user space",
            "annotations[].quad_points": "the raw /QuadPoints array where present, else null",
            "annotations[].covers": "exact body text enclosed by the rect; null for margin notes and the Square",
            "annotations[].refers_to": "anchor paragraph for margin notes that cover no body text; else null",
            "annotations[] order": "annotated.pdf page 1 stores its /Annots array deliberately out of vertical order",
            "running_elements": "running headers, footers and standalone page numbers that a body-text parser must discard",
        },
        "fonts": FONT_BASEFONT,
        "fixture_count": len(fixtures),
        "fixtures": fixtures,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    total = 0
    for name, pages, size in written:
        total += size
        print("%-22s pages=%d bytes=%d" % (name, pages, size))
    print("wrote %d fixtures (%d bytes) to %s" % (len(written), total, PDF_DIR))
    print("manifest: %s" % MANIFEST_PATH)


if __name__ == "__main__":
    main()
