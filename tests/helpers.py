"""Shared helpers for the test suite."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "pdf"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def fixture(name: str) -> Path:
    path = FIXTURES / (name if name.endswith(".pdf") else name + ".pdf")
    if not path.exists():
        raise unittest_skip(f"fixture {path.name} is missing")
    return path


def has_fixture(name: str) -> bool:
    return (FIXTURES / (name if name.endswith(".pdf") else name + ".pdf")).exists()


def unittest_skip(reason: str):
    import unittest
    raise unittest.SkipTest(reason)


# ---------------------------------------------------------------------------
# A tiny .docx builder, so the Word-comment path has a fixture without
# checking a binary into the repository.
# ---------------------------------------------------------------------------

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>
</Types>"""

RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>
</Relationships>"""


def build_docx(path: Path, paragraphs, comments) -> Path:
    """Write a minimal but valid .docx.

    ``paragraphs`` is a list of strings.  ``comments`` is a list of
    ``(author, text, paragraph_index, anchored_text_or_None)``.
    """
    doc_parts = [f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                 f'<w:document xmlns:w="{W_NS}"><w:body>']
    for index, text in enumerate(paragraphs):
        doc_parts.append("<w:p>")
        for cid, (author, ctext, pindex, anchor) in enumerate(comments):
            if pindex != index or anchor is None:
                continue
            doc_parts.append(f'<w:commentRangeStart w:id="{cid}"/>')
        doc_parts.append(f"<w:r><w:t>{_escape(text)}</w:t></w:r>")
        for cid, (author, ctext, pindex, anchor) in enumerate(comments):
            if pindex != index:
                continue
            if anchor is not None:
                doc_parts.append(f'<w:commentRangeEnd w:id="{cid}"/>')
            doc_parts.append(
                f'<w:r><w:commentReference w:id="{cid}"/></w:r>')
        doc_parts.append("</w:p>")
    doc_parts.append("</w:body></w:document>")
    document_xml = "".join(doc_parts)

    comment_parts = [f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                     f'<w:comments xmlns:w="{W_NS}">']
    for cid, (author, ctext, pindex, anchor) in enumerate(comments):
        comment_parts.append(
            f'<w:comment w:id="{cid}" w:author="{_escape(author)}" '
            f'w:initials="XX" w:date="2024-01-0{cid + 1}T00:00:00Z">'
            f'<w:p><w:r><w:t>{_escape(ctext)}</w:t></w:r></w:p></w:comment>')
    comment_parts.append("</w:comments>")
    comments_xml = "".join(comment_parts)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", RELS)
        archive.writestr("word/_rels/document.xml.rels", DOC_RELS)
        archive.writestr("word/document.xml", document_xml)
        if comments:
            archive.writestr("word/comments.xml", comments_xml)
    return path


class Recorder:
    """A :class:`~ilatool.tui.term.Screen` that keeps the frames it was given.

    Capturing the rendered frame lets a test assert on what the user would
    actually see, without needing a terminal.
    """

    def __init__(self):
        import io
        from ilatool.tui import term
        self._screen = term.Screen(stream=io.StringIO())
        self.frames = []
        self.entered = False

    def enter(self):
        self.entered = True

    def leave(self):
        self.entered = False

    def draw(self, lines, width, height):
        self.frames.append(list(lines))

    @property
    def last(self):
        return self.frames[-1] if self.frames else []

    def text(self) -> str:
        from ilatool.tui import term
        return "\n".join(term.strip_ansi(line) for line in self.last)


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{ns}">
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>
<w:style w:type="paragraph" w:styleId="BodyText"><w:name w:val="Body Text"/></w:style>
<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/></w:style>
</w:styles>""".replace("{ns}", W_NS)


def build_docx_styled(path: Path, paragraphs) -> Path:
    """Write a .docx with paragraph **styles** and explicit line breaks.

    ``paragraphs`` is a list of dicts with ``text``, an optional ``style``
    (``Heading1``, ``BodyText``, ...) and an optional ``break_before`` that
    emits a ``w:br`` -- the way Word files break one paragraph across several
    ``w:p`` elements.
    """
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             f'<w:document xmlns:w="{W_NS}"><w:body>']
    for item in paragraphs:
        text = item.get("text", "")
        style = item.get("style", "")
        parts.append("<w:p>")
        if style:
            parts.append(f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>')
        parts.append("<w:r>")
        if item.get("break_before"):
            parts.append("<w:br/>")
        parts.append(f"<w:t>{_escape(text)}</w:t>")
        parts.append("</w:r>")
        parts.append("</w:p>")
    parts.append("</w:body></w:document>")

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", RELS)
        archive.writestr("word/_rels/document.xml.rels", DOC_RELS)
        archive.writestr("word/document.xml", "".join(parts))
        archive.writestr("word/styles.xml", STYLES_XML)
    return path
