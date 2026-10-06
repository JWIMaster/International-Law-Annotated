"""Reading Word comments out of a ``.docx``.

The annotations for this project are written as Word comments on the
instrument text.  That is a *much* better source than a hand-retyped note
file: the comment already carries its author, its timestamp, and -- because
Word records the range it is anchored to -- the exact text it is about.  So
the anchor can be matched to a paragraph the same way a pasted excerpt is,
with no human transcription step in between.

A ``.docx`` is a zip of XML parts, so this needs nothing beyond the standard
library.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List
from xml.etree import ElementTree as ET

from .annotations import Annotation, ORIGIN_DOCX
from .errors import AnnotationError

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@dataclass
class WordComment:
    comment_id: str
    author: str = ""
    initials: str = ""
    date: str = ""
    text: str = ""
    quote: str = ""
    paragraph_index: int = -1

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.comment_id,
            "author": self.author,
            "initials": self.initials,
            "date": self.date,
            "text": self.text,
            "quote": self.quote,
        }


def _element_text(element: ET.Element) -> str:
    """Concatenate the ``w:t`` runs under an element, honouring tabs/breaks."""
    parts: List[str] = []
    for node in element.iter():
        tag = node.tag
        if tag == f"{W}t":
            parts.append(node.text or "")
        elif tag == f"{W}tab":
            parts.append("\t")
        elif tag in (f"{W}br", f"{W}cr"):
            parts.append(" ")
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _normalise_annotations_xml(raw: bytes) -> bytes:
    return raw


def read_docx_comments(path: Path) -> List[WordComment]:
    """Extract every comment with the text range it is anchored to."""
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise AnnotationError(
            f"'{path.name}' is not a readable .docx file",
            hint="It may be a .doc saved with the wrong extension, or corrupt.",
            cause=exc,
        ) from exc

    names = set(archive.namelist())
    if "word/comments.xml" not in names:
        raise AnnotationError(
            f"'{path.name}' contains no Word comments",
            hint="Comments are stored as Word comments; text that was merely "
                 "highlighted, or annotations typed into the body, cannot be "
                 "recovered.",
        )

    comments: Dict[str, WordComment] = {}
    root = ET.fromstring(archive.read("word/comments.xml"))
    for node in root.iter(f"{W}comment"):
        cid = node.get(f"{W}id", "")
        if not cid:
            continue
        comments[cid] = WordComment(
            comment_id=cid,
            author=(node.get(f"{W}author") or "").strip(),
            initials=(node.get(f"{W}initials") or "").strip(),
            date=(node.get(f"{W}date") or "").strip(),
            text=_element_text(node),
        )

    if not comments:
        raise AnnotationError(
            f"'{path.name}' has a comments part but no comments in it",
            hint="Word stores comments separately from highlighted text; this "
                 "document has neither.",
        )

    # Match up the anchored ranges in the main document part.
    document_part = next((n for n in ("word/document.xml",) if n in names), None)
    if document_part:
        _collect_ranges(archive.read(document_part), comments)

    return [c for c in comments.values() if c.text]


def _collect_ranges(xml: bytes, comments: Dict[str, WordComment]) -> None:
    """Fill in ``quote`` for each comment from its anchored text range."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return

    buffers: Dict[str, List[str]] = {}
    active: List[str] = []
    paragraph_texts: List[str] = []

    # A single pre-order walk both follows document order (so a range's text
    # lands in its buffer) and lets us note paragraph boundaries.
    for node in root.iter():
        tag = node.tag
        if tag == f"{W}commentRangeStart":
            cid = node.get(f"{W}id", "")
            if cid:
                active.append(cid)
                buffers.setdefault(cid, [])
        elif tag == f"{W}commentRangeEnd":
            cid = node.get(f"{W}id", "")
            if cid in active:
                active.remove(cid)
        elif tag == f"{W}t" and active:
            text = node.text or ""
            for cid in active:
                buffers.setdefault(cid, []).append(text)
        elif tag == f"{W}p":
            paragraph_texts.append(_element_text(node))

    for cid, parts in buffers.items():
        comment = comments.get(cid)
        if comment is None:
            continue
        comment.quote = re.sub(r"\s+", " ", "".join(parts)).strip()

    # Comments with no range (or an empty one) fall back to the paragraph
    # holding their reference mark.
    if not any(c.quote for c in comments.values()):
        return
    for cid, comment in comments.items():
        if comment.quote:
            continue
        for index, text in enumerate(paragraph_texts):
            if text:
                comment.paragraph_index = index
                break


def read_docx_comments_verbose(path: Path) -> List[WordComment]:
    """Like :func:`read_docx_comments` but also reports unresolved comments."""
    return read_docx_comments(path)


def annotations_from_docx(path: Path) -> List[Annotation]:
    """Convert a ``.docx``'s comments into annotations ready for matching."""
    out: List[Annotation] = []
    for order, comment in enumerate(read_docx_comments(path), start=1):
        out.append(Annotation(
            text=comment.text,
            author=comment.author,
            quote=comment.quote,
            kind="comment",
            origin=ORIGIN_DOCX,
            created=comment.date,
            order=order,
            meta={"initials": comment.initials, "comment_id": comment.comment_id},
        ))
    return out


def docx_comment_count(path: Path) -> int:
    try:
        with zipfile.ZipFile(path) as archive:
            if "word/comments.xml" not in archive.namelist():
                return 0
            root = ET.fromstring(archive.read("word/comments.xml"))
            return sum(1 for _ in root.iter(f"{W}comment"))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# The document body, as a text source
# ---------------------------------------------------------------------------
#
# Five of this project's instruments exist only as Word files: the annotated
# .docx *is* the text, with the notes attached as comments.  Reading the body
# out of it means one command turns the material into a page, and Word's own
# heading styles are a far better heading signal than anything inferred from a
# PDF's geometry.

@dataclass
class DocxParagraph:
    text: str
    style: str = ""
    heading_level: int = 0     # 0 = ordinary text, 1..4 = heading

    def as_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "style": self.style,
                "heading_level": self.heading_level}


def _style_heading_levels(archive: zipfile.ZipFile) -> Dict[str, int]:
    """Map Word style *ids* to heading levels via the styles part."""
    try:
        root = ET.fromstring(archive.read("word/styles.xml"))
    except (KeyError, ET.ParseError):
        return {}
    levels: Dict[str, int] = {}
    for style in root.iter(f"{W}style"):
        style_id = style.get(f"{W}styleId") or ""
        name_node = style.find(f"{W}name")
        name = (name_node.get(f"{W}val") if name_node is not None else "") or style_id
        levels[style_id] = _heading_level_from_name(name)
    return levels


def _heading_level_from_name(name: str) -> int:
    """Word style name -> heading level (0 when it is ordinary text)."""
    folded = re.sub(r"[\s_-]+", "", (name or "").lower())
    if folded in ("title",):
        return 1
    if folded.startswith("subtitle"):
        return 2
    match = re.match(r"heading(\d+)", folded)
    if match:
        return max(1, min(4, int(match.group(1))))
    if folded in ("heading", "h1"):
        return 1
    return 0


def _paragraph_pieces(paragraph: ET.Element) -> List[str]:
    """The text of one ``w:p``, split at explicit line breaks.

    Some exporters put an entire document into a single ``w:p`` and separate
    the lines with ``w:br``; the Reparations opinion is one 105,000-character
    paragraph with 622 of them.  Splitting here is what makes it readable.
    """
    pieces: List[str] = []
    current: List[str] = []

    def flush() -> None:
        text = re.sub(r"\s+", " ", "".join(current)).strip()
        # Empty pieces are kept: an explicit line break with nothing between
        # is how these documents mark a paragraph boundary, and dropping the
        # blanks would leave the joiner no way to tell a new paragraph from a
        # continuation.
        pieces.append(text)
        current.clear()

    for node in paragraph.iter():
        tag = node.tag
        if tag == f"{W}t":
            current.append(node.text or "")
        elif tag == f"{W}tab":
            current.append(" ")
        elif tag in (f"{W}br", f"{W}cr"):
            flush()
        elif tag in (f"{W}delText", f"{W}instrText"):
            # Tracked deletions and field codes are not body text.
            current.append("")
    flush()
    return pieces


def read_docx_paragraphs(path: Path) -> List[DocxParagraph]:
    """Every paragraph of a ``.docx`` body, in document order."""
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise AnnotationError(
            f"'{path.name}' is not a readable .docx file", cause=exc) from exc
    if "word/document.xml" not in archive.namelist():
        raise AnnotationError(f"'{path.name}' has no word/document.xml")
    try:
        root = ET.fromstring(archive.read("word/document.xml"))
    except ET.ParseError as exc:
        raise AnnotationError(f"'{path.name}' has an unreadable document part",
                              cause=exc) from exc

    levels = _style_heading_levels(archive)
    out: List[DocxParagraph] = []
    for paragraph in root.iter(f"{W}p"):
        style_node = paragraph.find(f"{W}pPr/{W}pStyle")
        style_id = (style_node.get(f"{W}val") if style_node is not None else "") or ""
        level = levels.get(style_id)
        if level is None:
            level = _heading_level_from_name(style_id)
        for piece in _paragraph_pieces(paragraph):
            out.append(DocxParagraph(text=piece, style=style_id,
                                     heading_level=level))
    return out


def docx_paragraph_count(path: Path) -> int:
    try:
        return len(read_docx_paragraphs(path))
    except Exception:
        return 0
