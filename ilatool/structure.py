"""Turning laid-out lines into sections, headings and paragraphs.

The input is a stream of positioned lines; the output is the thing the
website is actually built from.  The rules here are deliberately explicit
and testable, because "why did my paragraph get merged with the heading?"
is the single most common complaint about PDF-to-text tools:

* a line gets its own paragraph when it starts with a list/structure marker
  (``1.``, ``(a)``, ``(iv)``, ``•``, ``§``, ``¶``), when the vertical gap
  after the previous line is bigger than the local line spacing, when the
  indent changes, when the font size changes, or when the previous line
  ends with a sentence-ending punctuation mark;
* a line is a heading when it matches a structural keyword, or when it is
  shorter than a threshold and set in a larger or bolder face than the body;
* lines that belong together are joined, undoing end-of-line hyphenation.

Paragraph ids are content addressed (a short hash of the normalised text)
when the source does not carry an explicit ``{key}``.  That means
re-converting a PDF does not silently orphan every annotation written
against the previous conversion -- the same paragraph keeps the same id as
long as its text is unchanged.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import textutil
from .errors import Diagnostics, Stage, StructureError
from .layout import LaidOutLine, PageLayout, detect_front_matter
from .pdfmodel import Rect


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

#: Structural headings: "ARTICLE 7", "Part IV", "CHAPTER 2", "Section 3.1"...
HEADING_KEYWORD_RE = re.compile(
    r"^(?P<word>article|art\.?|part|chapter|chap\.?|section|sec\.?|annex|"
    r"appendix|schedule|title|preamble|preamble|recitals?|rule|regulation|"
    r"paragraph|sub-?paragraph)\s+"
    r"(?P<num>[IVXLCDM]+|\d+(?:\.\d+)*|[A-Z])\b[.:)\s]*(?P<rest>.*)$",
    re.IGNORECASE,
)

#: A heading that is just a numeral, e.g. "I." or "1." on a line of its own.
ROMAN_HEADING_RE = re.compile(r"^(?P<num>[IVXLCDM]{1,7})\.\s+(?P<rest>.+)$")
LETTER_HEADING_RE = re.compile(r"^(?P<num>[A-Z])\.\s+(?P<rest>.{0,70})$")

#: Paragraph markers, in the order they are tried.  Each becomes the
#: paragraph's visible label in the generated page.
MARKER_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("pilcrow", re.compile(r"^¶\s*(?P<rest>.*)$")),
    ("section", re.compile(r"^§+\s*(?P<num>\d+(?:\.\d+)*)?\s*[.)]?\s*(?P<rest>.*)$")),
    ("paren_num", re.compile(r"^\((?P<num>\d{1,3})\)\s*(?P<rest>.*)$")),
    ("num", re.compile(r"^(?P<num>\d{1,3})[.)]\s+(?P<rest>.*)$")),
    ("paren_roman", re.compile(r"^\((?P<num>[ivxlcdm]{1,6})\)\s*(?P<rest>.*)$", re.IGNORECASE)),
    ("paren_letter", re.compile(r"^\((?P<num>[a-z]{1,3})\)\s*(?P<rest>.*)$")),
    ("bullet", re.compile(r"^[•·▪‣◦*\-–—]\s+(?P<rest>.*)$")),
]

#: A marker on a line of its own, with the clause starting on the next line.
BARE_MARKER_RE = re.compile(
    r"^(?:\(\d{1,3}\)|\d{1,3}[.)]|\([a-z]{1,3}\)|\([ivxlcdm]{1,6}\)|[•·▪‣◦]|¶|§+)$",
    re.IGNORECASE,
)

SENTENCE_END_RE = re.compile(r"[.;:!?]['\"”’)\]]*$")

#: Any punctuation that closes off a phrase or clause.  Used to tell a
#: hanging list continuation from a paragraph break.
_ENDS_ANYTHING_RE = re.compile(r"[.,;:!?]['\"\u201d\u2019)\]]*$")

#: Punctuation that genuinely finishes a sentence.  A colon or a comma is a
#: lead-in -- "the following provisional measures: “(1) Germany shall ..." --
#: and the text after it belongs to the same paragraph, so those two are
#: deliberately absent.
SENTENCE_STOP_RE = re.compile(r"[.;!?]['\"”’)\]]*$")

#: Curly quotation marks, the only ones unambiguous enough to balance.  A
#: straight double quote is used for both opening and closing in plain text,
#: so counting it would be guesswork.
QUOTE_OPEN_CHARS = ("\u201c", "\u201e", "\u00ab")
QUOTE_CLOSE_CHARS = ("\u201d", "\u00bb")

#: Refuse to keep merging past this many characters: an unbalanced quotation
#: mark must not swallow the rest of the document.
QUOTE_MERGE_LIMIT = 8000

#: Words that almost always introduce a new recital in a treaty preamble.
RECITAL_CUES = [
    "Being", "Recognizing", "Recognising", "Recalling", "Further recalling",
    "Noting", "Taking note of", "Taking into account", "Considering",
    "Desiring", "Desirous", "Reaffirming", "Bearing in mind", "Convinced",
    "Determined", "Concerned", "Emphasizing", "Emphasising", "Underlining",
    "Acknowledging", "Mindful", "Guided by", "Welcoming", "Alarmed by",
    "Aware that", "Affirming", "Conscious", "Stressing", "Have agreed",
    "In pursuit of", "Pursuant to", "Bearing in mind that",
]
RECITAL_SPLIT_RE = re.compile(
    r"(?<=[,;])\s+(?=(?:" + "|".join(re.escape(c) for c in RECITAL_CUES) + r")\b)"
)

#: A *line* that opens a recital.  Combined with "the previous line ended in a
#: comma or semicolon" this is a very reliable paragraph boundary in treaties,
#: and far safer than splitting finished paragraphs on the same words.
RECITAL_START_RE = re.compile(
    r"^(?:" + "|".join(re.escape(c) for c in RECITAL_CUES) + r")\b"
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class Block:
    """One heading or paragraph in the structured document."""

    kind: str            # 'para' | 'heading'
    text: str
    label: str = "¶"
    level: int = 0
    key: str = ""
    page: int = 0
    bbox: Optional[Rect] = None
    section: str = ""
    order: int = 0
    #: index of this block's first line within its page, used to order
    #: annotations that sit next to it
    first_line_order: int = -1
    lines: List[LaidOutLine] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"para-{self.key}"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "label": self.label,
            "text": self.text,
            "level": self.level,
            "page": self.page,
            "bbox": self.bbox.as_dict() if self.bbox else None,
            "section": self.section,
            "order": self.order,
        }


@dataclass
class StructuredDocument:
    blocks: List[Block] = field(default_factory=list)
    body_size: float = 0.0
    aliases: Dict[str, str] = field(default_factory=dict)
    dropped_front_matter: List[str] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    @property
    def paragraphs(self) -> List[Block]:
        return [b for b in self.blocks if b.kind == "para"]

    @property
    def headings(self) -> List[Block]:
        return [b for b in self.blocks if b.kind == "heading"]


# ---------------------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------------------

def content_key(text: str) -> str:
    """A short, stable, human-typeable id derived from the paragraph text."""
    folded = textutil.normalize_for_match(text)[:600]
    return "k" + hashlib.sha1(folded.encode("utf-8")).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Marker handling
# ---------------------------------------------------------------------------

def split_marker(text: str) -> Tuple[str, str]:
    """Split a leading list/clause marker off a line.

    Returns ``(label, remainder)``.  ``label`` is what will be shown in the
    page (``"1."``, ``"(a)"``, ``"¶"``); ``remainder`` is the clause text.
    """
    for kind, pattern in MARKER_PATTERNS:
        m = pattern.match(text)
        if not m:
            continue
        rest = (m.groupdict().get("rest") or "").strip()
        num = m.groupdict().get("num")
        if kind == "num":
            label = f"{num}."
        elif kind == "paren_num":
            label = f"({num})"
        elif kind == "paren_letter":
            label = f"({num})"
        elif kind == "paren_roman":
            label = f"({num.lower()})"
        elif kind == "section":
            label = f"§{num}" if num else "§"
        elif kind == "bullet":
            label = "•"
        else:
            label = "¶"
        # A bare marker means the clause text starts on the next line; the
        # caller handles that by treating the label as pending.
        return label, rest
    return "¶", text


# ---------------------------------------------------------------------------
# Heading classification
# ---------------------------------------------------------------------------

HEADING_WORDS = {
    "article": 2, "art": 2, "section": 2, "sec": 2, "rule": 2, "regulation": 2,
    "part": 1, "chapter": 1, "chap": 1, "title": 1, "annex": 1, "appendix": 1,
    "schedule": 1, "preamble": 1, "recitals": 1,
}


def _is_flush_left_or_centred(line: LaidOutLine, body_box: Rect) -> bool:
    """Headings sit at the left margin or are centred -- not indented.

    Short lines that are indented are almost always the ragged last line of a
    paragraph, which must not be promoted to a heading.  Centred court-style
    headings are the important exception.
    """
    if abs(line.indent) <= 2:
        return True
    if body_box.width <= 0:
        return False
    left_gap = line.bbox.x0 - body_box.x0
    right_gap = body_box.x1 - line.bbox.x1
    return abs(left_gap - right_gap) < max(6.0, body_box.width * 0.12)


def classify_heading(line: LaidOutLine, body_size: float, body_box: Rect) -> Optional[Tuple[int, str]]:
    """Return ``(level, title)`` when the line is a heading, else None.

    Levels: 1 = Part/Chapter, 2 = Article/Section, 3 = a smaller run-in
    heading such as a roman numeral or a bold caption.
    """
    text = line.text.strip()
    if not text or len(text) > 200:
        return None

    m = HEADING_KEYWORD_RE.match(text)
    if m and line.size >= body_size * 0.95:
        word = m.group("word").lower().rstrip(".")
        level = HEADING_WORDS.get(word, 2)
        rest = (m.group("rest") or "").strip(" .:")
        if rest:
            # "Article 7 Definitions" -> keep the whole thing as the title.
            return level, text
        return level, text

    # "I. Scope" style headings: short, at the start of a line, larger/bolder.
    if len(text) <= 80 and not SENTENCE_END_RE.search(text):
        rm = ROMAN_HEADING_RE.match(text)
        if rm and (line.bold or line.size > body_size * 1.05):
            return 3, text
        lm = LETTER_HEADING_RE.match(text)
        if lm and line.bold and line.size > body_size * 1.02:
            return 3, text

    # A short, emphasised line that is visually set apart from the body.
    larger = line.size >= body_size * 1.18
    bold_short = line.bold and line.size >= body_size * 0.98
    # A heading is a label, not a sentence: punctuation in the middle or at
    # the end of a short line usually means it is body text that happens to
    # be short ("Have agreed as follows:", "The States Parties to this
    # Convention,").
    label_like = not re.search(r"[,;:!?]\s*$", text) and text.count(",") == 0
    if len(text) <= 90 and (larger or bold_short) and label_like:
        if _is_flush_left_or_centred(line, body_box):
            level = 2 if larger else 3
            return level, text

    # All-capitals lines.  Court documents label their sections "PRELIMINARY
    # OBJECTIONS" or "INTERNATIONAL COURT OF JUSTICE", and the poppler
    # backend does not always surface the bold flag that would catch them.
    letters = [ch for ch in text if ch.isalpha()]
    if (len(text) <= 90 and len(letters) >= 4 and label_like
            and sum(1 for ch in letters if ch.isupper()) / len(letters) >= 0.85):
        if _is_flush_left_or_centred(line, body_box):
            return 2, text
    return None


# ---------------------------------------------------------------------------
# Paragraph assembly
# ---------------------------------------------------------------------------

@dataclass
class _Options:
    split_recitals: bool = True
    drop_front_matter: bool = True
    skip_title: str = ""
    min_paragraph_chars: int = 0
    #: hyphenated compounds seen intact elsewhere in the document
    hyphenated_terms: set = field(default_factory=set)


def _line_spacing(lines: Sequence[LaidOutLine]) -> float:
    """Typical vertical distance between consecutive lines in a column.

    Uses the *most common* gap rather than the median: a title page or a
    table of contents has many over-large gaps, and a median over those
    inflates the "normal" spacing until real paragraph breaks stop being
    detected.  The mode is dominated by the running text.
    """
    gaps: Counter = Counter()
    for prev, cur in zip(lines, lines[1:]):
        if prev.column != cur.column or prev.page != cur.page:
            continue
        gap = cur.bbox.y0 - prev.bbox.y0
        if 0 < gap < 200:
            gaps[round(gap, 0)] += 1
    if not gaps:
        return 0.0
    return float(gaps.most_common(1)[0][0])


def _document_spacing(pages: Sequence[PageLayout]) -> float:
    """The document-wide dominant line spacing.

    Per-page figures are unreliable on sparse pages (a title page, a page
    holding a single table), so the paragraph-break test uses this when it is
    available and falls back to the page value only when the document has
    nothing to say.
    """
    gaps: Counter = Counter()
    for page in pages:
        lines = [l for l in page.lines if l.region == "body"]
        for prev, cur in zip(lines, lines[1:]):
            if prev.column != cur.column:
                continue
            gap = cur.bbox.y0 - prev.bbox.y0
            if 0 < gap < 200:
                gaps[round(gap, 0)] += 1
    if not gaps:
        return 0.0
    # Ties go to the smaller gap: under-estimating spacing splits paragraphs
    # more eagerly, which is the safer failure for a reader.
    best = min(gaps.items(), key=lambda kv: (-kv[1], kv[0]))
    return float(best[0])


def _is_new_paragraph(prev: LaidOutLine, cur: LaidOutLine, spacing: float,
                      body_size: float, prev_is_marker_line: bool = False) -> bool:
    if prev is None:
        return True
    if prev.column != cur.column:
        return True
    if prev.page != cur.page:
        # A paragraph can run over a page break.  Continue it unless the last
        # line on the previous page clearly finished a sentence.
        return bool(SENTENCE_END_RE.search(prev.text.strip()))
    if abs(cur.size - prev.size) > max(0.6, body_size * 0.08):
        return True
    if cur.bold and not prev.bold and cur.size >= body_size:
        return True
    # A line that opens a quotation continues the lead-in that introduced it
    # ("...the following measures: “(1) ..."), even across a wider gap.
    if (cur.text.lstrip()[:1] in QUOTE_OPEN_CHARS
            and not SENTENCE_STOP_RE.search(prev.text.strip())):
        return False

    gap = cur.bbox.y0 - prev.bbox.y0
    # Extra leading only means "new paragraph" when the previous line actually
    # finished a sentence.  Court documents set a clause number on its own
    # line with more space after it, and set paragraph breaks with a
    # first-line indent and *no* extra leading at all.
    if spacing and gap > spacing * 1.4 and SENTENCE_STOP_RE.search(prev.text.strip()):
        return True
    if not prev_is_marker_line:
        # The line right after a clause marker is a continuation by
        # definition; its box starts further right only because the marker
        # itself hangs into the margin.
        #
        # Only an *increase* in indent starts a paragraph.  A decrease is the
        # normal second line of a first-line-indented paragraph (or the
        # continuation of an outdented index entry), and treating it as a
        # break used to split every such paragraph in two.
        if cur.indent > prev.indent + 2.0:
            # An indent only means "new paragraph" when the line before it
            # actually finished something.  A line that runs on without any
            # punctuation -- "for the State of Palestine: HE Mr Riad Malki,
            # Minister for Foreign Affairs and" -- is a hanging list entry,
            # and its indented continuation is the rest of that sentence.
            #
            # The size of the jump decides how much punctuation is enough.  A
            # first-line indent is modest, and a comma at the end of the line
            # before it still separates paragraphs.  A jump of a hundred
            # points is a different layout region -- a right-aligned block of
            # counsel names -- and only a finished *sentence* starts a
            # paragraph there.
            increase = cur.indent - prev.indent
            previous = prev.text.strip()
            if increase <= max(24.0, body_size * 3.0):
                return bool(_ENDS_ANYTHING_RE.search(previous))
            return bool(SENTENCE_STOP_RE.search(previous))
    if prev.text.rstrip().endswith((",", ";")) and RECITAL_START_RE.match(cur.text):
        return True
    if gap <= 0:
        # Same baseline: this happens when a fragment was split; keep together.
        return False
    return False


def looks_like_structural_heading(text: str) -> bool:
    """Is this line a heading beyond reasonable doubt?

    "Article 1" on a line of its own is a heading even when the surrounding
    spacing suggests a continuation.  A sentence that merely *starts* with
    "Article 5 provides that ..." is not, which is why the line has to be
    short.
    """
    stripped = text.strip()
    if not stripped or len(stripped) > 60:
        return False
    if not HEADING_KEYWORD_RE.match(stripped):
        return False
    return not re.search(r"[,;]$", stripped)


def _starts_with_marker(text: str) -> bool:
    return any(p.match(text) for _, p in MARKER_PATTERNS)


def quote_balance(text: str) -> int:
    """How many quotation marks are still open after ``text``."""
    opened = sum(text.count(ch) for ch in QUOTE_OPEN_CHARS)
    closed = sum(text.count(ch) for ch in QUOTE_CLOSE_CHARS)
    return opened - closed


def build_blocks(pages: Sequence[PageLayout], body_size: float,
                 options: _Options, diagnostics: Diagnostics,
                 alias_map: Optional[Dict[str, str]] = None) -> StructuredDocument:
    doc = StructuredDocument(body_size=body_size)
    aliases: Dict[str, str] = dict(alias_map or {})
    section_trail: List[str] = []
    seen_keys: Counter = Counter()
    positional = 0
    order = 0

    skip_norm = textutil.normalize_for_match(options.skip_title) if options.skip_title else ""
    pending_label: Optional[str] = None
    buffer: List[LaidOutLine] = []
    buffer_label = "¶"
    buffer_heading_level: Optional[int] = None
    buffer_heading_text = ""
    skip_title_used = False
    #: Whether the line just added began with a clause marker.  A marker line
    #: is followed by *indented* continuations, so comparing indents across it
    #: would split every clause from its own text.
    last_line_had_marker = False
    #: Net unclosed quotation marks in the block being built.
    buffer_quote = 0

    def current_text() -> str:
        text = ""
        for line in buffer:
            text = textutil.join_lines(text, line.text, options.hyphenated_terms)
        return text.strip()

    def inside_quotation() -> bool:
        """Are we in the middle of quoted material?

        Court orders quote long lists: "(1) ... shall ...; (2) ... shall ...".
        Those markers are *not* paragraph breaks -- the whole quotation is one
        paragraph -- so while a quotation is open the heuristics stand down.
        """
        return buffer_quote > 0 and len(current_text()) < QUOTE_MERGE_LIMIT

    def start(kind: str, label: str, line: LaidOutLine) -> None:
        nonlocal buffer, buffer_label, buffer_heading_level, buffer_heading_text
        buffer = [line]
        buffer_label = label
        buffer_heading_level = None
        buffer_heading_text = ""

    def flush() -> None:
        nonlocal buffer, positional, order, pending_label
        nonlocal buffer_heading_level, buffer_heading_text
        nonlocal buffer_quote, last_line_had_marker
        if not buffer:
            pending_label = None
            return
        if buffer_heading_level is not None:
            # A heading that absorbed extra lines must keep them: using only
            # the first line silently deleted text ("Article 1" vanished from
            # the CISG because it followed "CHAPTER 1" into the same buffer).
            text = buffer_heading_text if len(buffer) == 1 else current_text()
            if text:
                order += 1
                key = content_key("heading:" + text)
                seen_keys[key] += 1
                if seen_keys[key] > 1:
                    key = f"{key}-{seen_keys[key]}"
                bbox = buffer[0].bbox
                for line in buffer[1:]:
                    bbox = bbox.union(line.bbox)
                section_trail[:] = section_trail[: buffer_heading_level - 1]
                section_trail.append(text)
                doc.blocks.append(Block(
                    kind="heading", text=text, label="", level=buffer_heading_level,
                    key=key, page=buffer[0].page, bbox=bbox,
                    section=" / ".join(section_trail), order=order,
                    first_line_order=buffer[0].order, lines=list(buffer),
                ))
        else:
            text = current_text()
            if text and not any(ch.isalnum() for ch in text):
                # A stray bullet, dagger or horizontal rule is punctuation we
                # split off a marker line, not a paragraph.
                text = ""
            if text:
                order += 1
                positional += 1
                key = content_key(text)
                seen_keys[key] += 1
                if seen_keys[key] > 1:
                    key = f"{key}-{seen_keys[key]}"
                # Keep the legacy positional id resolvable, so annotations
                # written against an older conversion still attach.
                aliases.setdefault(f"para-{positional}", key)
                bbox = buffer[0].bbox
                for line in buffer[1:]:
                    bbox = bbox.union(line.bbox)
                doc.blocks.append(Block(
                    kind="para", text=text, label=buffer_label, key=key,
                    page=buffer[0].page, bbox=bbox, section=" / ".join(section_trail),
                    order=order, first_line_order=buffer[0].order, lines=list(buffer),
                ))
        buffer = []
        buffer_heading_level = None
        buffer_heading_text = ""
        pending_label = None
        nonlocal buffer_quote, last_line_had_marker
        buffer_quote = 0
        last_line_had_marker = False

    # -- front matter ---------------------------------------------------------

    front = detect_front_matter(pages) if options.drop_front_matter else None
    front_keys: set = set()
    if front:
        for text in front.text:
            front_keys.add(textutil.normalize_for_match(text))
        doc.dropped_front_matter = list(front.text)
        diagnostics.warn(
            Stage.STRUCTURE,
            f"skipped {front.count} leading line(s) that look like a "
            f"{front.reason}",
            hint="Check the generated source text if this removed real content.",
        )

    prev: Optional[LaidOutLine] = None
    fallback_spacing = _document_spacing(pages)
    for page in pages:
        lines = [l for l in page.lines if l.region == "body"]
        spacing = fallback_spacing or _line_spacing(lines)
        for line in lines:
            norm = textutil.normalize_for_match(line.text)

            if front_keys and norm in front_keys and not doc.blocks:
                prev = line
                continue

            if options.skip_title and not skip_title_used and not doc.blocks:
                if norm == skip_norm:
                    skip_title_used = True
                    prev = line
                    continue

            # A marker alone on its line: remember it and wait for the text.
            if BARE_MARKER_RE.match(line.text) and len(line.text) <= 8:
                flush()
                pending_label = split_marker(line.text)[0]
                prev = line
                continue

            quoted = inside_quotation()
            starts_new = (
                pending_label is not None
                or not buffer
                # A heading is a whole block.  Body text that follows one must
                # not be absorbed into it -- the CISG sets "Article 12" on its
                # own line, and the article's text follows at the same indent
                # with ordinary leading, which every other heuristic reads as
                # a continuation.
                or buffer_heading_level is not None
                or (not quoted and (
                    _starts_with_marker(line.text)
                    or looks_like_structural_heading(line.text)
                    or _is_new_paragraph(prev, line, spacing, body_size,
                                         prev_is_marker_line=last_line_had_marker)))
            )
            if not starts_new:
                buffer.append(line)
                buffer_quote += quote_balance(line.text)
                last_line_had_marker = bool(_starts_with_marker(line.text))
                prev = line
                continue

            # The line opens a new block: heading or paragraph?  Only lines
            # that start a block are ever tested -- otherwise the second line
            # of a paragraph that happens to be short and large would be
            # promoted to a heading and tear the paragraph in half.
            flush()
            heading = None if pending_label is not None else classify_heading(
                line, body_size, page.body_box)
            if heading is not None:
                buffer = [line]
                buffer_heading_level, buffer_heading_text = heading
                buffer_label = ""
                buffer_quote = quote_balance(line.text)
                last_line_had_marker = False
            else:
                label, remainder = split_marker(line.text)
                stripped_marker = remainder != line.text
                if pending_label is not None:
                    label = pending_label
                    stripped_marker = True
                if stripped_marker:
                    line = LaidOutLine(
                        page=line.page, text=remainder, bbox=line.bbox,
                        size=line.size, bold=line.bold, italic=line.italic,
                        column=line.column, region=line.region,
                        indent=line.indent, order=line.order, source=line.source,
                    )
                buffer = [line]
                buffer_label = label
                buffer_heading_level = None
                buffer_heading_text = ""
                buffer_quote = quote_balance(line.text)
                last_line_had_marker = stripped_marker
            pending_label = None
            prev = line

    flush()

    # Recital splitting: treaty preambles are usually one long run of clauses
    # separated by ", Recognising that..." with no blank line in the PDF.
    if options.split_recitals:
        doc.blocks = _split_recitals(doc.blocks, diagnostics)

    for i, block in enumerate(doc.blocks):
        block.order = i + 1

    doc.aliases = aliases
    doc.stats = {
        "blocks": len(doc.blocks),
        "paragraphs": len(doc.paragraphs),
        "headings": len(doc.headings),
    }
    if not doc.paragraphs:
        raise StructureError(
            "no paragraphs could be identified in this document",
            hint="Every line was classified as a heading or dropped. Try "
                 "re-running with --keep-front-matter, or inspect the PDF's "
                 "text layer.",
        )
    return doc


def _split_recitals(blocks: Sequence[Block], diagnostics: Diagnostics) -> List[Block]:
    """Split a fused preamble into separate recital paragraphs.

    Only the opening blocks are considered.  A PDF whose preamble has no line
    break between clauses comes through as one enormous paragraph; splitting
    on the recital cue words fixes that.  Limiting it to the start of the
    document keeps ordinary prose, which uses the same words mid-sentence,
    intact.
    """
    out: List[Block] = []
    splits = 0
    examined = 0
    for block in blocks:
        if (examined < 8 and block.kind == "para" and block.label == "¶"
                and len(block.text) > 120):
            examined += 1
            parts = [p.strip() for p in RECITAL_SPLIT_RE.split(block.text) if p.strip()]
            if len(parts) > 1:
                splits += len(parts) - 1
                for part in parts:
                    out.append(Block(
                        kind="para", text=part, label="¶", key=content_key(part),
                        page=block.page, bbox=block.bbox, section=block.section,
                        lines=list(block.lines),
                    ))
                continue
        out.append(block)
    if splits:
        diagnostics.info(
            Stage.STRUCTURE,
            f"split {splits} preamble recital(s) into separate paragraphs",
        )
    # Re-key to avoid duplicate ids introduced by the split.
    seen: Counter = Counter()
    for block in out:
        if block.kind != "para":
            continue
        seen[block.key] += 1
        if seen[block.key] > 1:
            block.key = f"{block.key}-{seen[block.key]}"
    return out
