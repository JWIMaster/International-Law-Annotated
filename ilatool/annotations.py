"""Annotations: the structured model, every input format, and matching.

An annotation is not "some text a user typed next to the document".  It is a
record with a source, an author, a target, and -- when it came from the PDF or
a Word file -- a page and a bounding box.  Everything downstream (the page,
the validation report, the TUI preview) reads this one model.

Supported inputs
----------------
``text``      the tool's long-standing plain-text block format (``@id`` or a
              quoted excerpt, then ``Author:``/``Title:``/``Source:``)
``json``      a list of ``{"para"|"quote", "author", "title", "text",
              "source"}`` objects
``csv``/``tsv``  the column layout the per-instrument prep folders use
              (``id``/``quote`` plus ``comment_author``/``comment_text``...)
``notes.js``  an existing generated notes file, used as-is
``pdf``       markup annotations that live *inside* the PDF (highlights,
              sticky notes, underlines, ...)
``docx``      Word comments, which is where this project's annotations
              actually live

Matching
--------
Explicit ids are resolved first, including through the source file's legacy
``IDMAP`` aliases.  Quoted excerpts are matched with a combination of
containment, character-level similarity and word coverage, and a match is
only accepted when it is clearly better than the runner-up -- a note attached
to the wrong sentence is worse than a note that needs a human to place it.
"""

from __future__ import annotations

import csv
import difflib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import textutil
from .errors import AnnotationError, Diagnostics, Stage
from .layout import LaidOutLine, PageLayout
from .pdfmodel import PdfDocument, Rect
from .sourcefmt import SourceDocument
from .structure import Block

#: How close a pasted excerpt must be to a paragraph to count as a match.
QUOTE_MATCH_THRESHOLD = 0.86

#: How much better the winner must be than the runner-up.
QUOTE_MATCH_MARGIN = 0.025

#: Origin labels, surfaced in the TUI and the diagnostics file.
ORIGIN_TEXT = "text"
ORIGIN_JSON = "json"
ORIGIN_CSV = "csv"
ORIGIN_NOTES_JS = "notes.js"
ORIGIN_PDF = "pdf"
ORIGIN_DOCX = "docx"


@dataclass
class Annotation:
    """One note, before it has been attached to a paragraph."""

    text: str = ""
    author: str = ""
    title: str = ""
    source: str = ""
    #: explicit target: a paragraph id, or a quoted excerpt to match
    para: str = ""
    quote: str = ""
    kind: str = "note"
    origin: str = ORIGIN_TEXT
    page: int = 0
    bbox: Optional[Rect] = None
    created: str = ""
    #: Where the annotation sat in its source document, 0..1, or -1 if unknown.
    #: Used only to break ties between equally good matches.
    position: float = -1.0
    #: annotation order in the source file / annotation array
    order: int = 0
    meta: Dict[str, Any] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (self.text or "").strip()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "author": self.author,
            "title": self.title,
            "text": self.text,
            "source": self.source,
            "para": self.para,
            "quote": self.quote,
            "kind": self.kind,
            "origin": self.origin,
            "page": self.page,
            "bbox": self.bbox.as_dict() if self.bbox else None,
            "created": self.created,
            "position": self.position,
            "order": self.order,
        }

    def to_note(self) -> Dict[str, str]:
        """The shape the generated ``notes.js`` uses."""
        note = {"author": self.author, "title": self.title,
                "text": self.text, "source": self.source}
        return {k: v for k, v in note.items() if v}


#: Two notes are "the same note" when one is almost entirely present in the
#: other as a single unbroken run.  That distinguishes a copy-edit ("Cybrus"
#: corrected to "Cyprus" somewhere in a 950-character note: the run is ~99% of
#: the text) from a *series* of similar notes ("See Opinion No 14 …", "… No 15
#: …": whole-text similarity is 97%, but the longest shared run is under half
#: the text, because the difference falls in the middle).
SAME_NOTE_RUN_RATIO = 0.9
SAME_NOTE_MIN_CHARS = 60


def _is_same_note(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) < SAME_NOTE_MIN_CHARS or len(b) < SAME_NOTE_MIN_CHARS:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    matcher = difflib.SequenceMatcher(None, shorter, longer, autojunk=False)
    run = matcher.find_longest_match(0, len(shorter), 0, len(longer)).size
    return run >= SAME_NOTE_RUN_RATIO * len(longer)


def _already_placed(ann: Annotation, attached: Sequence[Tuple[str, str]]) -> Optional[str]:
    """Is this same note already on the page via a different source?"""
    text_norm = textutil.normalize_for_match(ann.text)
    if not text_norm:
        return None
    for earlier_text, target in attached:
        if _is_same_note(text_norm, earlier_text):
            return target
    return None


@dataclass
class MatchResult:
    notes: Dict[str, List[Dict[str, str]]] = field(default_factory=dict)
    attached: List[Annotation] = field(default_factory=list)
    #: (annotation, reason) pairs for everything that could not be placed
    unplaced: List[Tuple[Annotation, str]] = field(default_factory=list)
    dropped_ids: List[str] = field(default_factory=list)
    duplicates: int = 0
    #: notes that could not be placed themselves but were already on the page
    #: through another source
    absorbed: int = 0

    @property
    def note_count(self) -> int:
        return sum(len(v) for v in self.notes.values())

    @property
    def annotated_ids(self) -> set:
        return set(self.notes)


# ---------------------------------------------------------------------------
# Plain-text format
# ---------------------------------------------------------------------------

ANN_BLOCK_START_RE = re.compile(r"^@([\w.:-]+)\s*$")
ANN_QUOTE_FENCE_RE = re.compile(r'^"{3,}\s*$')
ANN_FIELD_RE = re.compile(r"^(Author|Title|Source|Page|Quote|Para|Id):\s*(.*)$",
                          re.IGNORECASE)

#: A block may also start with "Para:"/"Id:" or "Quote:" without fences.
ANN_INLINE_QUOTE_RE = re.compile(r'^Quote:\s*(.*)$', re.IGNORECASE)


def parse_annotations_text(text: str) -> List[Annotation]:
    """Parse the documented plain-text annotation format."""
    lines = text.replace("\r\n", "\n").split("\n")
    items: List[Annotation] = []
    cur: Optional[Annotation] = None
    body: List[str] = []
    body_started = False
    in_quote = False
    quote_lines: List[str] = []
    order = 0

    def flush() -> None:
        nonlocal cur, body, body_started
        if cur is not None:
            cur.text = " ".join(l.strip() for l in body if l.strip()).strip()
            if not cur.is_empty():
                items.append(cur)
        cur, body, body_started = None, [], False

    for raw in lines:
        stripped = raw.strip()

        if in_quote:
            if ANN_QUOTE_FENCE_RE.match(stripped):
                in_quote = False
                cur.quote = " ".join(l.strip() for l in quote_lines if l.strip())
                quote_lines = []
            else:
                quote_lines.append(raw)
            continue

        if ANN_QUOTE_FENCE_RE.match(stripped):
            flush()
            order += 1
            cur = Annotation(origin=ORIGIN_TEXT, order=order)
            in_quote = True
            quote_lines = []
            continue

        bm = ANN_BLOCK_START_RE.match(stripped)
        if bm:
            flush()
            order += 1
            cur = Annotation(para=bm.group(1), origin=ORIGIN_TEXT, order=order)
            continue

        if cur is None:
            continue
        if not stripped:
            continue

        fm = ANN_FIELD_RE.match(stripped)
        if fm and not body_started:
            key = fm.group(1).lower()
            value = fm.group(2).strip()
            if key == "author":
                cur.author = value
            elif key == "title":
                cur.title = value
            elif key == "source":
                cur.source = value
            elif key == "page":
                try:
                    cur.page = int(value)
                except ValueError:
                    pass
            elif key in ("quote",):
                cur.quote = value
            elif key in ("para", "id"):
                cur.para = value
            continue

        body_started = True
        body.append(raw)

    flush()
    return items


# ---------------------------------------------------------------------------
# JSON / CSV
# ---------------------------------------------------------------------------

def parse_annotations_json(text: str) -> List[Annotation]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AnnotationError(
            "the annotations file is not valid JSON",
            detail=str(exc),
            hint="Check for a trailing comma or an unquoted key.",
        ) from exc
    if isinstance(raw, dict):
        # A notes.js-style mapping of id -> [note, ...] is also accepted.
        items: List[Annotation] = []
        order = 0
        for para_id, entries in raw.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                order += 1
                items.append(_annotation_from_dict(entry, PARA_ID_KEY, para_id, order))
        return items
    if not isinstance(raw, list):
        raise AnnotationError(
            "the annotations JSON must be a list of note objects (or a mapping "
            "of paragraph id to notes)",
        )
    items = []
    for order, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise AnnotationError(
                f"annotation #{order} is a {type(entry).__name__}, not an object",
            )
        items.append(_annotation_from_dict(entry, None, "", order))
    return items


PARA_ID_KEY = "para"


def _annotation_from_dict(entry: Dict[str, Any], fixed_key: Optional[str],
                          fixed_value: str, order: int) -> Annotation:
    def pick(*names: str) -> str:
        for name in names:
            value = entry.get(name)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    ann = Annotation(
        text=pick("text", "comment_text", "note", "body"),
        author=pick("author", "comment_author", "by"),
        title=pick("title", "comment_title", "heading"),
        source=pick("source", "comment_source"),
        para=pick("para", "id", "paragraph", "target") or (fixed_value if fixed_key else ""),
        quote=pick("quote", "excerpt", "match"),
        origin=ORIGIN_JSON,
        order=order,
    )
    if entry.get("page"):
        try:
            ann.page = int(entry["page"])
        except (TypeError, ValueError):
            pass
    if entry.get("kind"):
        ann.kind = str(entry["kind"])
    ann.meta = {k: v for k, v in entry.items()
                if k not in {"text", "author", "title", "source", "para", "id",
                             "quote", "page", "kind"}}
    return ann


CSV_ID_KEYS = ("id", "para", "paragraph")
CSV_QUOTE_KEYS = ("quote", "excerpt", "text_quote", "anchor")
CSV_TEXT_KEYS = ("comment_text", "note", "text", "comment", "body")
CSV_AUTHOR_KEYS = ("comment_author", "author", "by")
CSV_TITLE_KEYS = ("comment_title", "title")
CSV_SOURCE_KEYS = ("comment_source", "source")


def parse_annotations_csv(text: str, delimiter: str = ",") -> List[Annotation]:
    reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
    if not reader.fieldnames:
        raise AnnotationError("the annotations CSV has no header row")
    lower = {(name or "").strip().lower(): name for name in reader.fieldnames}

    def column(keys: Sequence[str]) -> Optional[str]:
        for key in keys:
            if key in lower:
                return lower[key]
        return None

    id_col = column(CSV_ID_KEYS)
    quote_col = column(CSV_QUOTE_KEYS)
    text_col = column(CSV_TEXT_KEYS)
    if text_col is None:
        raise AnnotationError(
            "the annotations CSV has no note text column",
            hint="Expected one of: " + ", ".join(CSV_TEXT_KEYS),
        )
    author_col = column(CSV_AUTHOR_KEYS)
    title_col = column(CSV_TITLE_KEYS)
    source_col = column(CSV_SOURCE_KEYS)

    items: List[Annotation] = []
    for order, row in enumerate(reader, start=1):
        text_value = (row.get(text_col) or "").strip()
        author = (row.get(author_col) or "").strip() if author_col else ""
        if not text_value:
            continue
        quote = (row.get(quote_col) or "").strip() if quote_col else ""
        para = (row.get(id_col) or "").strip() if id_col else ""
        # A CSV from the prep folders uses the same column for the paragraph
        # text; stripping the marker keeps ids and quotes unambiguous.
        items.append(Annotation(
            text=text_value,
            author=author,
            title=(row.get(title_col) or "").strip() if title_col else "",
            source=(row.get(source_col) or "").strip() if source_col else "",
            para=para if not quote else "",
            quote=quote,
            origin=ORIGIN_CSV,
            order=order,
        ))
    return items


# ---------------------------------------------------------------------------
# notes.js
# ---------------------------------------------------------------------------

_NOTES_JS_RE = re.compile(r"window\.NOTES\s*=\s*(?=\{)")
_ADD_NOTE_RE = re.compile(r"\baddNote\s*\(")


def _balanced_object(text: str, start: int) -> Optional[str]:
    """Extract the ``{...}`` starting at ``start``, ignoring braces in strings."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    quote = ""
    escaped = False
    for index in range(start, len(text)):
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_string = False
            continue
        if ch in ("'", '"'):
            in_string, quote = True, ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _string_arguments(text: str, start: int) -> List[str]:
    """Read the string literals in the call whose ``(`` is at ``start``."""
    args: List[str] = []
    index = start + 1
    depth = 1
    while index < len(text) and depth:
        ch = text[index]
        if ch in ("'", '"'):
            quote = ch
            index += 1
            buffer: List[str] = []
            while index < len(text):
                ch = text[index]
                if ch == "\\" and index + 1 < len(text):
                    buffer.append(text[index:index + 2])
                    index += 2
                    continue
                if ch == quote:
                    break
                buffer.append(ch)
                index += 1
            try:
                args.append(json.loads(f'"{buffer and "".join(buffer) or ""}"'))
            except Exception:
                args.append("".join(buffer))
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        index += 1
    return args


def parse_notes_js(text: str) -> Dict[str, List[Dict[str, str]]]:
    """Read a generated ``notes.js`` back into a plain dict.

    Two shapes are accepted, because both are in use in this project:

    * ``window.NOTES = { "para-1": [ {...} ] };`` -- what this tool writes;
    * ``addNote("para-1", "Author", "text")`` calls -- how the hand-maintained
      pages were written, and the shape a contributor is most likely to edit.

    A file that is neither is rejected rather than guessed at.
    """
    out: Dict[str, List[Dict[str, str]]] = {}
    # An empty notes file is legitimate: a text can genuinely have none.  Only
    # a file with no recognisable structure at all is an error.
    recognised = False

    match = _NOTES_JS_RE.search(text)
    if match:
        payload = _balanced_object(text, match.end())
        recognised = payload is not None
        if payload:
            data = None
            for kwargs in ({}, {"strict": False}):
                try:
                    data = json.loads(payload, **kwargs)
                    break
                except json.JSONDecodeError:
                    continue
            if isinstance(data, dict):
                for key, value in data.items():
                    if isinstance(value, list):
                        entries = [v for v in value if isinstance(v, dict)]
                    elif isinstance(value, dict):
                        entries = [value]
                    else:
                        continue
                    out.setdefault(str(key), []).extend(entries)

    for call in _ADD_NOTE_RE.finditer(text):
        args = _string_arguments(text, call.end() - 1)
        if not args:
            continue
        recognised = True
        para = args[0].strip()
        if not para:
            continue
        entry: Dict[str, str] = {}
        for name, value in zip(("author", "text", "title", "source"), args[1:]):
            if value:
                entry[name] = value
        if entry.get("text") or entry.get("author"):
            out.setdefault(para, []).append(entry)

    if not recognised:
        raise AnnotationError(
            "this does not look like a notes.js file",
            hint="Expected 'window.NOTES = {...}' or addNote(...) calls.",
        )
    return out


def load_annotations(path: Path) -> List[Annotation]:
    """Load any annotations file, dispatching on its extension and content."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise AnnotationError(f"could not read '{path.name}'", cause=exc) from exc
    except UnicodeDecodeError as exc:
        raise AnnotationError(
            f"'{path.name}' is not valid UTF-8 text",
            hint="Re-save it as UTF-8, or export it as CSV/JSON.",
            cause=exc,
        ) from exc

    suffix = path.suffix.lower()
    stripped = text.lstrip()
    if suffix == ".json" or stripped.startswith("["):
        return parse_annotations_json(text)
    if suffix in (".csv", ".tsv"):
        return parse_annotations_csv(text, "\t" if suffix == ".tsv" else ",")
    if suffix == ".js":
        return annotations_from_notes(parse_notes_js(text))
    if stripped.startswith("{"):
        return parse_annotations_json(text)
    return parse_annotations_text(text)


def annotations_from_notes(notes: Dict[str, List[Dict[str, str]]]) -> List[Annotation]:
    items: List[Annotation] = []
    order = 0
    for para_id, entries in notes.items():
        for entry in entries:
            order += 1
            items.append(Annotation(
                text=str(entry.get("text", "")),
                author=str(entry.get("author", "")),
                title=str(entry.get("title", "")),
                source=str(entry.get("source", "")),
                para=para_id,
                origin=ORIGIN_NOTES_JS,
                order=order,
            ))
    return items


# ---------------------------------------------------------------------------
# Annotations that live inside the PDF
# ---------------------------------------------------------------------------

def _page_lines(pages: Sequence[PageLayout], number: int) -> List[LaidOutLine]:
    for page in pages:
        if page.number == number:
            return page.lines
    return []


def text_under_rect(lines: Sequence[LaidOutLine], rect: Rect,
                    min_overlap: float = 0.25) -> str:
    """The text a markup annotation covers.

    Uses the annotation's own rectangles where available, so a highlight over
    three lines yields all three, in reading order.
    """
    parts: List[str] = []
    for line in lines:
        frac = line.bbox.overlap_fraction(rect)
        if frac >= min_overlap:
            parts.append(line.text)
    return textutil.normalize_whitespace(" ".join(parts))


def nearest_line(lines: Sequence[LaidOutLine], rect: Rect) -> Optional[LaidOutLine]:
    """The line closest to a rectangle that does not overlap it.

    A sticky note sits in the margin, so the useful question is "what is it
    next to?", answered by vertical overlap first and distance second.
    """
    best = None
    best_key = None
    for line in lines:
        if line.bbox.overlap_fraction(rect) > 0.2:
            return line
        vertical = rect.vertical_gap(line.bbox)
        horizontal = rect.horizontal_gap(line.bbox)
        key = (0 if vertical < 0 else 1, max(0.0, vertical), max(0.0, horizontal))
        if best_key is None or key < best_key:
            best_key = key
            best = line
    return best


def annotations_from_pdf(doc: PdfDocument,
                         pages: Optional[Sequence[PageLayout]] = None,
                         include_empty: bool = False) -> List[Annotation]:
    """Turn the PDF's own markup annotations into :class:`Annotation` objects.

    Highlight/underline/strikeout annotations carry no text of their own, so
    the text they cover is recovered from the layout and used as the anchor
    for matching.  Sticky notes and shapes instead carry a note plus a
    position, and are matched to the nearest paragraph.
    """
    out: List[Annotation] = []
    order = 0
    for page in doc.pages:
        lines = _page_lines(pages, page.number) if pages else []
        for annot in page.annotations:
            if annot.generated or annot.kind in ("link", "popup", "widget"):
                continue
            if not include_empty and not annot.has_text and not annot.is_markup:
                continue
            order += 1
            quote = ""
            if annot.is_markup and lines:
                fragments = []
                for region in annot.regions:
                    covered = text_under_rect(lines, region)
                    if covered:
                        fragments.append(covered)
                quote = " ".join(fragments).strip()
                if not quote:
                    line = nearest_line(lines, annot.rect)
                    quote = line.text if line else ""
            elif not annot.is_markup and lines:
                line = nearest_line(lines, annot.rect)
                if line is not None:
                    quote = line.text
            out.append(Annotation(
                text=annot.contents,
                author=annot.author,
                title=annot.subject,
                quote=quote,
                kind=annot.kind,
                origin=ORIGIN_PDF,
                page=page.number,
                bbox=annot.rect,
                created=annot.created,
                order=order,
            ))
    return out


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Score:
    score: float
    run: int          # length of the longest common run, in characters
    para_ratio: float  # that run measured against the paragraph


def _score_pair(quote_norm: str, para_norm: str) -> _Score:
    """Score how well a quote matches a paragraph.

    ``SequenceMatcher.ratio`` punishes a long paragraph for a short quote and
    vice versa, so it is combined with the longest common run measured
    against *both* lengths.  The individual terms are kept as well, because
    "most of this paragraph appears inside the quote" is a different, and
    equally valid, signal from "this paragraph contains the quote".
    """
    if not quote_norm or not para_norm:
        return _Score(0.0, 0, 0.0)
    matcher = difflib.SequenceMatcher(None, quote_norm, para_norm, autojunk=False)
    ratio = matcher.ratio()
    match = matcher.find_longest_match(0, len(quote_norm), 0, len(para_norm))
    quote_ratio = match.size / max(1, len(quote_norm))
    para_ratio = match.size / max(1, len(para_norm))
    return _Score(max(ratio, 0.35 * ratio + 0.65 * max(quote_ratio, para_ratio)),
                  match.size, para_ratio)


def _similarity(quote_norm: str, para_norm: str) -> float:
    """Backwards-compatible wrapper returning just the score."""
    return _score_pair(quote_norm, para_norm).score


#: A paragraph only counts as "inside" the quote when it is substantial.
#: Without a floor, the heading "BUYER" is contained in every long quote that
#: mentions buyers and would win the match.  Forty characters and six words
#: is comfortably below any real clause and comfortably above any label.
CONTAINMENT_MIN_CHARS = 40
CONTAINMENT_MIN_WORDS = 6

#: A quote may also be anchored over *part* of a paragraph.  When a long run
#: of the paragraph appears in the quote, that is a match even though neither
#: string contains the other.
PARTIAL_RUN_CHARS = 60
PARTIAL_PARA_RATIO = 0.7
PARTIAL_SCORE = 0.88


def _token_overlap(quote_tokens: set, para_tokens: set) -> float:
    if not quote_tokens:
        return 0.0
    return len(quote_tokens & para_tokens) / len(quote_tokens)


def match_quote(quote: str, paragraphs: Sequence[Block],
                page_hint: int = 0) -> Tuple[Optional[str], str, float, List[Tuple[str, float]]]:
    """Match a pasted excerpt to a paragraph.

    Returns ``(paragraph_id_or_None, status, confidence, runner_ups)`` where
    status is ``ok``, ``ambiguous`` or ``no_match``.

    Three cases, in order of strength:

    * the quote sits *inside* one paragraph -- the normal case;
    * the quote *spans* several paragraphs (a Word comment anchored across a
      whole article) -- attach it to the first paragraph it covers, which is
      where the annotation starts, rather than calling it ambiguous;
    * a fuzzy match above the threshold -- ambiguous if the runner-up is
      nearly as good.
    """
    quote_norm = textutil.normalize_for_match(quote)
    if not quote_norm:
        return None, "no_match", 0.0, []

    quote_tokens = set(quote_norm.split())
    use_prefilter = len(quote_tokens) >= 5

    inside: List[Tuple[str, float]] = []
    spanning: List[Tuple[str, float]] = []
    scored: List[Tuple[str, float]] = []

    for block in paragraphs:
        para_norm = textutil.normalize_for_match(block.text)
        if not para_norm:
            continue
        overlap = 1.0
        if use_prefilter:
            overlap = _token_overlap(quote_tokens, set(para_norm.split()))
            # Cheap rejection first: a paragraph sharing almost none of the
            # quote's words cannot be the right one, and this keeps a
            # 300-paragraph document matching in milliseconds instead of tens
            # of seconds.  The bar is deliberately low here because a comment
            # anchored over a whole article shares only a little of its
            # vocabulary with any single paragraph inside it.
            if overlap < 0.12:
                continue
        # A page number from a PDF annotation is a strong prior: it nudges
        # the right page up and near-misses on other pages down, which is
        # what breaks a tie between two near-identical paragraphs.
        bonus = 0.0
        if page_hint and block.page:
            bonus = 0.03 if block.page == page_hint else -0.02
        if quote_norm in para_norm:
            inside.append((block.id, 1.0 + bonus))
            continue
        if (para_norm in quote_norm
                and len(para_norm) >= CONTAINMENT_MIN_CHARS
                and len(para_norm.split()) >= CONTAINMENT_MIN_WORDS):
            spanning.append((block.id, 1.0 + bonus))
            continue
        # Character-level similarity is the expensive part; only run it for
        # paragraphs that already look like a real candidate.
        if use_prefilter and overlap < 0.45:
            continue
        result = _score_pair(quote_norm, para_norm)
        score = result.score + bonus
        if score >= QUOTE_MATCH_THRESHOLD:
            scored.append((block.id, score))
        elif (result.run >= PARTIAL_RUN_CHARS
              and result.para_ratio >= PARTIAL_PARA_RATIO):
            # The annotation was anchored over part of a paragraph: most of
            # the paragraph is present in the quote, but neither contains the
            # other, so the score alone falls short.  A run this long cannot
            # be a coincidence, so accept it slightly below the threshold.
            scored.append((block.id, max(score, PARTIAL_SCORE)))

    if inside:
        inside.sort(key=lambda kv: kv[1], reverse=True)
        if len(inside) > 1 and (inside[0][1] - inside[1][1]) < QUOTE_MATCH_MARGIN:
            return None, "ambiguous", inside[0][1], inside[:4]
        return inside[0][0], "ok", inside[0][1], inside[:4]

    if spanning:
        # Ties keep document order, so the note lands where the anchor starts.
        ranked = sorted(spanning, key=lambda kv: kv[1], reverse=True)
        return ranked[0][0], "ok", ranked[0][1], ranked[:4]

    if not scored:
        return None, "no_match", 0.0, []

    scored.sort(key=lambda kv: kv[1], reverse=True)
    best_id, best_score = scored[0]
    if len(scored) > 1 and (best_score - scored[1][1]) < QUOTE_MATCH_MARGIN:
        return None, "ambiguous", best_score, scored[:4]
    return best_id, "ok", best_score, scored[:4]


def match_across_paragraphs(quote: str, paragraphs: Sequence[Block],
                            page_hint: int = 0) -> Tuple[Optional[str], float]:
    """Handle a quote that spans a paragraph boundary.

    Contributors often paste two sentences that the PDF split into separate
    paragraphs.  Only adjacent pairs that already share most of the quote's
    words are scored, which keeps this cheap on a long document.
    """
    quote_norm = textutil.normalize_for_match(quote)
    if not quote_norm:
        return None, 0.0
    quote_tokens = set(quote_norm.split())
    prefilter = len(quote_tokens) >= 5
    best: Tuple[Optional[str], float] = (None, 0.0)
    for first, second in zip(paragraphs, paragraphs[1:]):
        joined_norm = textutil.normalize_for_match(first.text + " " + second.text)
        if prefilter and _token_overlap(quote_tokens, set(joined_norm.split())) < 0.6:
            continue
        score = _similarity(quote_norm, joined_norm)
        if page_hint and first.page == page_hint:
            score += 0.03
        if score > best[1]:
            best = (first.id, score)
    if best[1] >= QUOTE_MATCH_THRESHOLD:
        return best
    return None, 0.0


def paragraph_after_heading(quote: str, source: SourceDocument) -> Optional[str]:
    """The paragraph a heading-anchored note belongs to.

    A Word comment anchored to "Article 27" or "Annex A" marks up the heading,
    and headings are not annotatable on the page.  Such a note belongs to the
    text the heading introduces, which is where a reader would expect to find
    it.  The heading must match closely -- this is a fallback, not a guess.
    """
    quote_norm = textutil.normalize_for_match(quote)
    if not quote_norm:
        return None
    for index, block in enumerate(source.blocks):
        if block.kind != "heading":
            continue
        heading_norm = textutil.normalize_for_match(block.text)
        if not heading_norm:
            continue
        matched = (quote_norm == heading_norm
                   or quote_norm in heading_norm
                   or heading_norm in quote_norm
                   or _similarity(quote_norm, heading_norm) >= 0.9)
        if not matched:
            continue
        for later in source.blocks[index + 1:]:
            if later.kind == "para":
                return later.id
        return None
    return None


def _closest_by_position(runners: Sequence[Tuple[str, float]],
                         paragraphs: Sequence[Block],
                         position: float) -> Optional[str]:
    """Pick the candidate nearest a position in the document.

    A Word comment anchored to a single word ("review", "developed") matches
    dozens of paragraphs.  Its position in the document is what tells them
    apart, and the position is exact -- it is where the author put the
    comment, not an inference.
    """
    total = max(1, len(paragraphs) - 1)
    index_of = {block.id: i for i, block in enumerate(paragraphs)}
    candidates = [(abs(index_of[pid] / total - position), pid)
                  for pid, _score in runners if pid in index_of]
    if not candidates:
        return None
    return min(candidates)[1]


def attach(annotations: Sequence[Annotation], source: SourceDocument,
           diagnostics: Optional[Diagnostics] = None) -> MatchResult:
    """Resolve every annotation onto a paragraph id.

    Nothing is ever attached on a guess: an ambiguous or unmatched quote goes
    into ``unplaced`` with the reason, for the caller (TUI or report) to show.
    """
    diag = diagnostics if diagnostics is not None else Diagnostics()
    result = MatchResult()
    paragraphs = source.paragraphs
    paragraphs_by_id = {b.id: b for b in paragraphs}
    seen: Counter = Counter()
    attached_texts: List[Tuple[str, str]] = []

    for ann in annotations:
        if ann.is_empty() and not ann.quote:
            continue

        target: Optional[str] = None
        why = ""

        if ann.para:
            target = source.resolve_ref(ann.para)
            if target is None:
                why = f"no paragraph with id '{ann.para}'"
                result.dropped_ids.append(ann.para)
        elif ann.quote:
            target, status, score, runners = match_quote(ann.quote, paragraphs, ann.page)
            if status == "ambiguous" and ann.position >= 0 and runners:
                chosen = _closest_by_position(runners, paragraphs, ann.position)
                if chosen is not None:
                    target, status = chosen, "ok"
            if status == "ambiguous":
                target = None
                why = ("the excerpt matches several paragraphs equally well "
                       f"({', '.join(i for i, _ in runners[:3])})")
            elif status == "no_match":
                target, score = match_across_paragraphs(ann.quote, paragraphs, ann.page)
                if target is None:
                    why = "no paragraph is a close enough match for the excerpt"

        if target is None and ann.quote:
            # A note anchored to a heading attaches to the text it introduces.
            heading_target = paragraph_after_heading(ann.quote, source)
            if heading_target is not None:
                target = heading_target
                ann.meta["anchored_to"] = "heading"
                diag.info(
                    Stage.MATCHING,
                    f"attached a note anchored to a heading ({ann.quote.strip()[:40]!r}) "
                    "to the text it introduces",
                )

        if target is None and not why:
            why = "the annotation has neither an id nor an excerpt to match"

        if target is None:
            # The same note may have arrived from a second source whose anchor
            # *did* resolve -- the author's Word comment and the copy already
            # published, say.  If so the note is on the page already; it just
            # reached it by the other route.  Losing it because this copy's
            # anchor failed would be a silent disappearance.
            absorbed = _already_placed(ann, attached_texts)
            if absorbed is not None:
                result.absorbed += 1
                continue
            result.unplaced.append((ann, why))
            continue

        note = ann.to_note()
        if not note.get("text") and ann.kind in ("highlight", "underline", "strikeout", "squiggly"):
            # A bare markup annotation still deserves to show up as a mark.
            note["text"] = ""
        key = (target, json.dumps(note, sort_keys=True, ensure_ascii=False))
        if seen[key]:
            result.duplicates += 1
            continue

        # The same note often exists in two places: the author's Word comment
        # and the copy already published on the page.  Keep the first source's
        # version, so "Word comments first" means the comment's own anchor
        # wins, and the published copy is not shown twice.
        text_norm = textutil.normalize_for_match(note.get("text", ""))
        if text_norm and len(text_norm) > 40:
            duplicate_of = None
            for earlier_text, earlier_origin in attached_texts:
                if earlier_origin == ann.origin:
                    continue
                if _is_same_note(text_norm, earlier_text):
                    duplicate_of = earlier_text
                    break
            if duplicate_of is not None:
                result.duplicates += 1
                continue
            attached_texts.append((text_norm, ann.origin))

        attached_texts.append((textutil.normalize_for_match(note.get("text", "")), target))
        seen[key] += 1
        result.notes.setdefault(target, []).append(note)
        ann.para = target
        result.attached.append(ann)

    # Keep notes in a stable, meaningful order: the order they were written.
    for para_id, notes in result.notes.items():
        result.notes[para_id] = notes

    if result.unplaced:
        ids = [a.para for a, _ in result.unplaced if a.para]
        if ids:
            diag.warn(
                Stage.MATCHING,
                f"{len(result.unplaced)} annotation(s) could not be attached",
                hint="Ids that match nothing usually mean the source text was "
                     "edited after the notes were written.",
            )
    return result


# ---------------------------------------------------------------------------
# Help shown in the interface
# ---------------------------------------------------------------------------

ANN_FORMAT_HELP = """
Annotation formats
==================================================================

The tool accepts five kinds of annotation file.  It looks at the extension
and at the first character, so you rarely have to say which is which.

1. Plain text  (.txt)
------------------------------------------------------------------
One block per note.  A block starts EITHER with a line "@<id>", where
<id> matches a paragraph id from the source text, OR with an excerpt
fenced above and below by a line of three double quotes.

    @k1f3e9030
    Author: J. Smith
    Title: Institutional continuity
    Source: Smith, Climate Law (2020) 45
    The Protocol folds the COP into its own machinery.

    \"\"\"
    Each Party included in Annex I shall ensure that its aggregate
    anthropogenic carbon dioxide equivalent emissions do not exceed
    its assigned amount.
    \"\"\"
    Author: A. Nguyen
    The word "shall" here imposes a binding obligation.

Inside a block, "Author:", "Title:" and "Source:" are optional and must
come before the note text.  Repeat "@same-id" for a second note on the
same paragraph.

2. JSON  (.json)
------------------------------------------------------------------
    [
      {"para": "k1f3e9030", "author": "J. Smith", "text": "..."},
      {"quote": "Each Party included in Annex I shall ensure...",
       "author": "A. Nguyen", "text": "..."}
    ]

3. CSV / TSV
------------------------------------------------------------------
The columns used by the prep-folder workflow: an id or quote column plus
comment_author / comment_title / comment_text / comment_source.

4. Word comments  (.docx)
------------------------------------------------------------------
Word comments already carry their author, date and anchored text, so the
note is matched to the paragraph automatically.  This is the least
error-prone source when the annotations were written in Word.

5. A PDF's own markup  (.pdf)
------------------------------------------------------------------
Highlights, underlines and sticky notes are read straight out of the PDF,
including their page, position and author.  Install PyMuPDF for the most
accurate geometry.

Where an excerpt cannot be matched to exactly one paragraph, the note is
reported as unplaced rather than attached to a guess -- a note on the
wrong sentence is worse than a note you have to place by hand.
==================================================================
"""
