"""Page-level analysis: turning raw boxes into a readable document.

The PDF gives us text fragments at coordinates.  This module decides what
they *mean* page by page:

* which lines are running headers/footers (mastheads, page numbers, the URL
  and date a browser stamps on a printed page) rather than content,
* which column each line belongs to, so a two-column page reads down the
  left column and then down the right instead of zig-zagging across,
* what the reading order is,
* and how each line is indented relative to the body of its column.

Nothing here decides what a *paragraph* is -- that is
:mod:`ilatool.structure`'s job.  This stage is purely geometric, and it is
the stage that historically produced the worst output, because a flat
``pdftotext`` dump has already thrown all of this information away by the
time anything else sees it.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import textutil
from .errors import Diagnostics, Stage
from .pdfmodel import Line, PageModel, PdfDocument, Rect


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

REGION_BODY = "body"
REGION_HEADER = "header"
REGION_FOOTER = "footer"


@dataclass
class LaidOutLine:
    page: int
    text: str
    bbox: Rect
    size: float = 0.0
    bold: bool = False
    italic: bool = False
    column: int = 0
    region: str = REGION_BODY
    indent: float = 0.0
    order: int = 0
    source: Optional[Line] = None

    @property
    def y0(self) -> float:
        return self.bbox.y0

    @property
    def x0(self) -> float:
        return self.bbox.x0

    def as_dict(self) -> Dict[str, object]:
        return {
            "page": self.page,
            "text": self.text,
            "bbox": self.bbox.as_dict(),
            "size": round(self.size, 2),
            "bold": self.bold,
            "italic": self.italic,
            "column": self.column,
            "region": self.region,
            "indent": round(self.indent, 2),
            "order": self.order,
        }


@dataclass
class PageLayout:
    number: int
    width: float
    height: float
    rotation: int = 0
    lines: List[LaidOutLine] = field(default_factory=list)
    columns: int = 1
    body_box: Rect = field(default_factory=Rect.empty)
    has_text: bool = False
    image_area_ratio: float = 0.0
    dropped: List[LaidOutLine] = field(default_factory=list)

    @property
    def is_image_only(self) -> bool:
        return self.image_area_ratio > 0.6 and not self.has_text

    def as_dict(self) -> Dict[str, object]:
        return {
            "number": self.number,
            "width": round(self.width, 2),
            "height": round(self.height, 2),
            "rotation": self.rotation,
            "columns": self.columns,
            "has_text": self.has_text,
            "image_area_ratio": round(self.image_area_ratio, 3),
            "body_box": self.body_box.as_dict(),
            "lines": [l.as_dict() for l in self.lines],
            "dropped": [l.as_dict() for l in self.dropped],
        }


#: A hyphenated word appearing *inside* a line, e.g. "self-determination".
#: If the same compound also turns up split across a line break, the hyphen
#: is part of the word and must not be removed.
HYPHENATED_TOKEN_RE = re.compile(r"(?<![\w-])([A-Za-z]{2,}-[A-Za-z]{2,})(?![\w-])")


@dataclass
class DocumentLayout:
    source: PdfDocument
    pages: List[PageLayout] = field(default_factory=list)
    body_size: float = 0.0
    #: text of running headers/footers we removed, for reporting
    furniture: List[str] = field(default_factory=list)
    repairs: Dict[str, int] = field(default_factory=dict)
    #: hyphenated compounds seen intact somewhere in the document, used to
    #: avoid "dehyphenating" a real compound at a line break
    hyphenated_terms: set = field(default_factory=set)

    @property
    def lines(self) -> List[LaidOutLine]:
        out: List[LaidOutLine] = []
        for page in self.pages:
            out.extend(page.lines)
        return out

    def as_dict(self) -> Dict[str, object]:
        return {
            "backend": self.source.backend,
            "page_count": self.source.page_count,
            "body_size": round(self.body_size, 2),
            "furniture": self.furniture,
            "repairs": self.repairs,
            "pages": [p.as_dict() for p in self.pages],
        }


# ---------------------------------------------------------------------------
# Line extraction and cleaning
# ---------------------------------------------------------------------------

def _lines_from_page(page: PageModel) -> List[Line]:
    out: List[Line] = []
    for block in page.blocks:
        for line in block.lines:
            if line.text.strip():
                out.append(line)
    return out


def _clean_lines(lines: Sequence[Line], stats: Counter) -> List[LaidOutLine]:
    out: List[LaidOutLine] = []
    seen: set = set()
    for line in lines:
        text = textutil.clean_line(line.text, stats)
        text = text.strip()
        if not text:
            continue
        # Some producers draw the same text twice at the same coordinates --
        # most often a visible layer plus a hidden one.  Keeping both would
        # double every paragraph on the page.
        key = (textutil.normalize_for_match(text),
               round(line.bbox.x0), round(line.bbox.y0),
               round(line.bbox.x1), round(line.bbox.y1))
        if key in seen:
            stats["duplicate_line"] += 1
            continue
        seen.add(key)
        out.append(LaidOutLine(
            page=line.page,
            text=text,
            bbox=line.bbox,
            size=line.size,
            bold=line.is_bold,
            italic=line.is_italic,
            source=line,
        ))
    return out


# ---------------------------------------------------------------------------
# Running headers / footers
# ---------------------------------------------------------------------------

#: How far into the page (fraction of height) the header band reaches, and
#: how close to the bottom the footer band starts.  Generous on purpose:
#: court publications put the running head well below the paper edge (an ICJ
#: fascicle puts it a fifth of the way down), and a band that is too tight
#: simply never sees the furniture at all.
HEADER_BAND = 0.22
FOOTER_BAND = 0.18

#: An isolated margin line is only treated as furniture when it is also
#: *narrow*; a full-width first line is a heading, not a running head.
MARGIN_LINE_MAX_WIDTH = 0.7


#: A line needs this many non-numeric words before its digits are allowed to
#: be treated as a changing counter.  Without the guard "Article 1" folds to
#: "article #", matches every other article heading in a treaty, and the whole
#: set is deleted as running furniture -- which is exactly what happened to
#: the CISG's 101 article headings.
FURNITURE_MIN_WORDS = 3


def _normalise_furniture(text: str) -> str:
    """Fold a header/footer so the same furniture on different pages matches.

    Page numbers change from page to page, so digits become ``#``; the title
    part stays so a running head is still distinguishable from body text.
    """
    folded = textutil.normalize_for_match(text)
    return re.sub(r"\b\d+\b", "#", folded)


def furniture_keys(text: str) -> Tuple[str, Optional[str]]:
    """The keys under which a line is counted as possible furniture.

    ``(exact, foldable)``.  ``exact`` is the plain normalised text, which is
    what catches a running head that never changes ("International Law
    Review").  ``foldable`` additionally replaces digits with ``#`` so that
    "certain international obligations (ord. 30 IV 24) 561" matches the same
    head on page 562 -- but only for lines with enough real words to be a
    header rather than a label like "Article 1".
    """
    exact = textutil.normalize_for_match(text)
    tokens = exact.split()
    non_numeric = sum(1 for token in tokens if not token.isdigit())
    if non_numeric >= FURNITURE_MIN_WORDS:
        return exact, _normalise_furniture(text)
    return exact, None


def _typical_spacing(pages: Sequence[PageLayout]) -> float:
    """The document's dominant line-to-line gap, in points."""
    gaps: Counter = Counter()
    for page in pages:
        lines = page.lines
        for prev, cur in zip(lines, lines[1:]):
            if prev.column != cur.column:
                continue
            gap = cur.bbox.y0 - prev.bbox.y0
            if 0 < gap < 200:
                gaps[round(gap)] += 1
    return float(gaps.most_common(1)[0][0]) if gaps else 0.0


def find_running_furniture(pages: Sequence[PageLayout],
                           min_pages: int = 2,
                           threshold: float = 0.5) -> Dict[int, set]:
    """Return, per page number, the set of line indices that are furniture.

    Three independent tests, because no single one catches every real case:

    1. *Repetition.*  The same normalised text in the same band on at least
       half the pages ("International Law Review").  Digits are folded so
       "Page 3 of 12" and "Page 4 of 12" count as the same line.
    2. *Shape.*  A margin line that looks like a page number, a bare URL or
       a browser print stamp.
    3. *Isolation.*  A line in the top/bottom band separated from the body by
       a gap much larger than the document's line spacing.  This is what
       catches a one-off running head such as "Article 9 (continued)", which
       appears on a single page and therefore never repeats.
    """
    if not pages:
        return {}
    spacing = _typical_spacing(pages)

    top_counter: Counter = Counter()
    bot_counter: Counter = Counter()
    for page in pages:
        if not page.height:
            continue
        for line in page.lines:
            if line.region not in (REGION_BODY,):
                continue
            in_top = line.y0 <= page.height * HEADER_BAND
            in_bottom = line.bbox.y1 >= page.height * (1.0 - FOOTER_BAND)
            if not (in_top or in_bottom):
                continue
            exact, foldable = furniture_keys(line.text)
            counter = top_counter if in_top else bot_counter
            counter[("exact", exact)] += 1
            if foldable is not None:
                counter[("folded", foldable)] += 1

    need = max(2, int(round(len(pages) * threshold)))
    boiler = {key for key, value in top_counter.items() if value >= need and key[1]}
    boiler |= {key for key, value in bot_counter.items() if value >= need and key[1]}

    result: Dict[int, set] = {}
    for page in pages:
        drop: set = set()
        if len(pages) < min_pages and not spacing:
            pass
        for i, line in enumerate(page.lines):
            exact, foldable = furniture_keys(line.text)
            if ("exact", exact) in boiler or (foldable is not None and ("folded", foldable) in boiler):
                drop.add(i)
                continue
            # Page numbers, browser print stamps and bare URLs are furniture
            # wherever they appear in the margins, even on a single page.
            if line.y0 <= page.height * HEADER_BAND or line.bbox.y1 >= page.height * (1.0 - FOOTER_BAND):
                if (textutil.looks_like_page_number(line.text)
                        or textutil.looks_like_browser_furniture(line.text)):
                    drop.add(i)

        # Isolated margin lines (one-off running heads / footers).
        lines = page.lines
        if spacing and len(lines) >= 3:
            narrow = page.width * MARGIN_LINE_MAX_WIDTH
            first = lines[0]
            # Never drop the very first line of the document: a title above a
            # large gap looks exactly like a running head geometrically.
            is_document_start = page is pages[0] and 0 not in drop
            if (not is_document_start and first.y0 <= page.height * HEADER_BAND
                    and first.bbox.width <= narrow
                    and first.bbox.y1 < lines[1].bbox.y0
                    and (lines[1].bbox.y0 - first.bbox.y1) > spacing * 1.6):
                drop.add(0)
            last = lines[-1]
            if (len(lines) - 1 not in drop and last.bbox.y1 >= page.height * (1.0 - FOOTER_BAND)
                    and last.bbox.width <= narrow
                    and lines[-2].bbox.y1 < last.bbox.y0
                    and (last.bbox.y0 - lines[-2].bbox.y1) > spacing * 1.6):
                drop.add(len(lines) - 1)

        if drop:
            result[page.number] = drop
    return result


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------

#: A gutter must be at least this wide (points) and this fraction of the page
#: width before we believe the page is multi-column.
MIN_GUTTER_POINTS = 10.0
MIN_GUTTER_FRACTION = 0.015
#: Each side of a candidate gutter needs at least this share of the
#: page's lines.  Low, because a column of short labels is still a
#: column; the crossing test is what rejects false splits.
MIN_COLUMN_FRACTION = 0.10


def detect_columns(lines: Sequence[LaidOutLine], page_width: float,
                   max_columns: int = 3) -> Tuple[int, List[Tuple[float, float]]]:
    """Work out how many text columns a page has.

    Uses a projection profile: rasterise every line's horizontal extent and
    look for vertical gaps no line crosses.  Full-width lines (a title that
    spans both columns) are excluded from the profile -- otherwise they plug
    the gutter and every page looks single-column -- but they are still
    assigned to a column afterwards.  Returns ``(count, bands)``.
    """
    body = [l for l in lines if l.region == REGION_BODY and l.text.strip()]
    if len(body) < 4 or page_width <= 0:
        return 1, []
    narrow = [l for l in body if l.bbox.width < page_width * 0.75]
    if len(narrow) < 4:
        return 1, []

    # Build a coverage histogram at 2pt resolution.
    step = 2.0
    bins = int(page_width / step) + 2
    covers = [0] * bins
    for line in narrow:
        start = max(0, int(line.bbox.x0 / step))
        end = min(bins - 1, int(line.bbox.x1 / step))
        for i in range(start, end + 1):
            covers[i] += 1

    min_gutter = max(MIN_GUTTER_POINTS, page_width * MIN_GUTTER_FRACTION)
    min_gutter_bins = max(1, int(min_gutter / step))

    # A gutter is a wide band that *few* lines cross, not necessarily one that
    # no line crosses: a full-width title crossing the gutter must not hide
    # it.  So look for low-coverage runs, and require solid text on both
    # sides so the ragged right edge of a single column is not mistaken for a
    # gutter.
    peak = max(covers) if covers else 0
    if peak <= 0:
        return 1, []
    low = max(1, int(peak * 0.25))
    solid = max(1, int(peak * 0.5))

    used = [i for i, c in enumerate(covers) if c > 0]
    if not used:
        return 1, []
    lo, hi = used[0], used[-1]

    gaps: List[Tuple[int, int]] = []
    run_start = None
    for i in range(lo, hi + 1):
        if covers[i] <= low:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None:
                if i - run_start >= min_gutter_bins:
                    gaps.append((run_start, i))
                run_start = None
    if run_start is not None and hi - run_start >= min_gutter_bins:
        gaps.append((run_start, hi))

    # A candidate gutter only counts when there is a real column on each
    # side of it: lines that finish before the gutter and lines that start
    # after it.  Checking coverage immediately beside the gap instead would
    # reject a narrow, ragged column (a list of country names) and accept the
    # ragged right edge of a single column.
    min_side = max(2, int(len(narrow) * MIN_COLUMN_FRACTION))
    valid: List[Tuple[int, int]] = []
    for a, b in gaps:
        if a <= lo or b > hi:
            continue
        # ``a`` and ``b`` are bin indices; compare in page coordinates.
        xa, xb = a * step, b * step
        left_side = [l for l in narrow if l.bbox.x1 <= xa + 1]
        right_side = [l for l in narrow if l.bbox.x0 >= xb - 1]
        if len(left_side) < min_side or len(right_side) < min_side:
            continue
        # Almost nothing may *cross* a real column boundary.  A full-width
        # title spanning both columns is fine (one line); a single-column page
        # whose body lines all run past the candidate gap is not, and that is
        # what stops an indented block from looking like a second column.
        crossing = [l for l in body if l.bbox.x0 < xa and l.bbox.x1 > xb]
        if len(crossing) > max(1, int(len(body) * 0.1)):
            continue
        valid.append((a, b))
    if not valid or max_columns < 2:
        return 1, []

    valid.sort(key=lambda g: g[1] - g[0], reverse=True)
    splits = sorted(a for a, _b in valid[: max_columns - 1])
    boundaries = [0] + [int(s * step) for s in splits] + [int((hi + 1) * step)]
    bands: List[Tuple[float, float]] = []
    for a, b in zip(boundaries, boundaries[1:]):
        band_lines = [l for l in narrow if l.bbox.x0 >= a - 1 and l.bbox.x0 < b]
        if len(band_lines) >= min_side:
            bands.append((float(a), float(b)))
    if len(bands) < 2:
        return 1, []
    return len(bands), bands


def assign_columns(lines: Sequence[LaidOutLine], bands: Sequence[Tuple[float, float]]) -> None:
    """Give each line a column index.

    Assignment uses the line's left edge, not its centre: a full-width title
    centred over a two-column page has its centre in the right column and
    would otherwise be sorted after the whole of the left one.
    """
    if len(bands) < 2:
        for line in lines:
            line.column = 0
        return
    for line in lines:
        start = line.bbox.x0
        centre = (line.bbox.x0 + line.bbox.x1) / 2.0
        best = 0
        best_dist = None
        for i, (a, b) in enumerate(bands):
            if a <= start < b:
                best = i
                break
            dist = min(abs(centre - a), abs(centre - b))
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best = i
        line.column = best


# ---------------------------------------------------------------------------
# Indentation
# ---------------------------------------------------------------------------

def compute_indents(pages: Sequence["PageLayout"]) -> None:
    """Set ``indent`` on every line, relative to its column's left margin.

    The margin is computed **across the whole document**, per column index,
    not per page.  Per page it is unstable -- a page made up of indented
    quoted clauses has a different "leftmost" line from a page of ordinary
    body text, so the same body indent would come out as 0 on one page and
    -14 on the next, and the paragraph detector would then see a phantom
    indent change at every page break.

    The margin is a low percentile rather than the exact minimum so that one
    stray fragment cannot shift every line on the page.
    """
    edges_by_column: Dict[int, List[int]] = defaultdict(list)
    for page in pages:
        for line in page.lines:
            if line.region == REGION_BODY:
                edges_by_column[line.column].append(round(line.bbox.x0))

    margins: Dict[int, int] = {}
    for column, edges in edges_by_column.items():
        if not edges:
            continue
        edges.sort()
        index = min(len(edges) - 1, max(0, int(len(edges) * 0.05)))
        margins[column] = edges[index]

    for page in pages:
        for line in page.lines:
            margin = margins.get(line.column)
            if margin is None:
                line.indent = 0.0
            else:
                line.indent = round(line.bbox.x0) - margin


# ---------------------------------------------------------------------------
# Where the body starts
# ---------------------------------------------------------------------------

#: "12. Text of the paragraph".  Parenthesised numbers are deliberately *not*
#: matched: those are sub-clauses ("(1) ... (2) ...") inside a paragraph, and
#: treating a run of them as the start of the document would delete everything
#: before it.
NUMBERED_PARA_RE = re.compile(r"^(?P<num>\d{1,3})[.)]\s+(?P<rest>\S.*)$")

#: Back matter may be at most this share of the document.  Generous enough
#: for a long fascicle contents page, small enough that a contents list on
#: page 1 of a two-page leaflet is not mistaken for one.
TAIL_MATTER_MAX_FRACTION = 0.2

#: A run has to be this convincing before anything is thrown away.
BODY_START_MIN_RUN = 5
BODY_START_MIN_AVG_LEN = 120
BODY_START_MAX_POSITION = 0.45


def detect_body_start(pages: Sequence[PageLayout]) -> Optional[Tuple[int, int, int]]:
    """Find where the numbered body of a document begins.

    Court publications open with cover pages, a bilingual title, and often a
    multi-page summary or index before paragraph 1.  Returns
    ``(page_number, index_within_page, flat_position)`` for the first line of
    the best run of consecutive numbered paragraphs, or None.

    The run is scored the way a reader would judge it: how many paragraph
    numbers appear in order (1, 2, 3, ...) and how substantial those
    paragraphs are.  A table of contents lists the numbers but has tiny
    "paragraphs", so it loses on both counts.
    """
    flat: List[Tuple[int, int, LaidOutLine]] = []
    for page in pages:
        for index, line in enumerate(page.lines):
            flat.append((page.number, index, line))
    if not flat:
        return None

    if len(flat) < BODY_START_MIN_RUN * 2:
        return None

    best: Optional[Tuple[int, int, int]] = None
    best_score: Tuple[int, float] = (-1, 0.0)
    limit = int(len(flat) * BODY_START_MAX_POSITION)

    for position in range(limit):
        _page_no, _index, line = flat[position]
        match = NUMBERED_PARA_RE.match(line.text.strip())
        if not match or int(match.group("num")) != 1:
            continue

        # Collect the numbered paragraph *starts* in order, then measure how
        # big each paragraph really is -- counting the continuation lines,
        # not just the line carrying the number, or every document would look
        # like a table of contents.
        starts: List[int] = []
        for later in range(position, min(len(flat), position + 400)):
            if NUMBERED_PARA_RE.match(flat[later][2].text.strip()):
                starts.append(later)

        expected = 1
        consecutive = 0
        lengths: List[int] = []
        for order, start in enumerate(starts):
            found = NUMBERED_PARA_RE.match(flat[start][2].text.strip())
            number = int(found.group("num"))
            if number != expected:
                continue
            end = starts[order + 1] if order + 1 < len(starts) else len(flat)
            size = sum(len(flat[i][2].text.strip()) for i in range(start, end))
            consecutive += 1
            lengths.append(size)
            expected += 1
            if consecutive >= BODY_START_MIN_RUN:
                break
        if consecutive < BODY_START_MIN_RUN:
            continue
        # Only the first few paragraphs are scored, so the average reflects
        # real prose rather than the whole document.
        average = sum(lengths) / max(1, len(lengths))
        score = (consecutive, average)
        if score > best_score:
            best_score = score
            best = (flat[position][0], flat[position][1], position)

    if best is None:
        return None
    if best_score[1] < BODY_START_MIN_AVG_LEN:
        return None
    return best


def detect_tail_matter(pages: Sequence[PageLayout],
                       window: int = 60) -> Optional[Tuple[int, int]]:
    """Find a trailing contents list, e.g. a publication's own index.

    Returns ``(page_number, index_within_page)`` of the first line of the
    run, or None.  Only the very end of the document is inspected, and the
    run has to look like a contents list, so ordinary closing paragraphs are
    never touched.
    """
    flat: List[Tuple[int, int, LaidOutLine]] = []
    for page in pages:
        for index, line in enumerate(page.lines):
            flat.append((page.number, index, line))
    if len(flat) < 12:
        return None
    start_scan = max(0, len(flat) - window)

    def acceptable(position: int) -> bool:
        """Back matter is a *tail*: a small part of the document.

        A short document whose first page carries a contents list must not
        have everything from that list onwards deleted -- the guard is a
        fraction of the whole document, not a line count, so it scales.
        """
        remaining = len(flat) - position
        return remaining <= max(4, int(len(flat) * TAIL_MATTER_MAX_FRACTION))

    if not acceptable(start_scan):
        return None

    # A publication usually labels its own index.  That is the most reliable
    # signal of all: "CONTENTS OF THE FASCICLE", "Table of Contents".
    for position in range(start_scan, len(flat)):
        text = flat[position][2].text.strip()
        if re.match(r"^(table\s+of\s+)?contents\b", text, re.IGNORECASE):
            return (flat[position][0], flat[position][1])

    run = 0
    best_start, best_len = None, 0
    for position in range(start_scan, len(flat)):
        if textutil.looks_like_toc_entry(flat[position][2].text):
            run += 1
            if run > best_len:
                best_len, best_start = run, position - run + 1
        else:
            run = 0
    if best_len >= 3 and best_start is not None and acceptable(best_start):
        return (flat[best_start][0], flat[best_start][1])
    return None


def drop_tail_matter(pages: Sequence[PageLayout],
                     start: Tuple[int, int],
                     diagnostics: Optional[Diagnostics] = None) -> int:
    """Remove everything from ``start`` to the end of the document."""
    start_page, start_index = start
    count = 0
    first = ""
    for page in pages:
        keep: List[LaidOutLine] = []
        for index, line in enumerate(page.lines):
            after = (page.number, index) >= (start_page, start_index)
            if after:
                line.region = REGION_FOOTER
                page.dropped.append(line)
                if not first:
                    first = line.text
                count += 1
            else:
                keep.append(line)
        page.lines = keep
    if count and diagnostics is not None:
        diagnostics.info(
            Stage.LAYOUT,
            f"skipped {count} trailing line(s) of back matter "
            f"(starting {first!r})",
            hint="Use --keep-front-matter to keep them.",
        )
    return count


def drop_before_body(pages: Sequence[PageLayout], body_start: Tuple[int, int, int],
                     diagnostics: Optional[Diagnostics] = None) -> int:
    """Move every line before the body start into ``page.dropped``."""
    position_limit = body_start[2]
    dropped: List[str] = []
    count = 0
    for page in pages:
        keep: List[LaidOutLine] = []
        for line in page.lines:
            if count < position_limit:
                line.region = REGION_HEADER
                page.dropped.append(line)
                if len(dropped) < 3:
                    dropped.append(line.text)
            else:
                keep.append(line)
            count += 1
        page.lines = keep
    if position_limit and diagnostics is not None:
        diagnostics.info(
            Stage.LAYOUT,
            f"skipped {position_limit} line(s) of cover matter before the "
            f"numbered text begins (starting {dropped[0]!r})",
            hint="Use --keep-front-matter to keep them.",
        )
    return position_limit


# ---------------------------------------------------------------------------
# Whole-document pass
# ---------------------------------------------------------------------------

def analyse_document(doc: PdfDocument, diagnostics: Optional[Diagnostics] = None,
                     drop_furniture: bool = True,
                     drop_front_matter: bool = True) -> DocumentLayout:
    """Run every page-level analysis and return the laid-out document."""
    diag = diagnostics if diagnostics is not None else Diagnostics()
    stats: Counter = Counter()
    layout = DocumentLayout(source=doc)

    pages: List[PageLayout] = []
    image_only_pages: List[int] = []
    for page in doc.pages:
        raw = _lines_from_page(page)
        cleaned = _clean_lines(raw, stats)
        area = page.page_width * page.page_height
        image_area = sum(i.bbox.area for i in page.images if not i.bbox.is_empty)
        layout_page = PageLayout(
            number=page.number,
            width=page.page_width,
            height=page.page_height,
            rotation=page.rotation,
            lines=cleaned,
            has_text=bool(cleaned),
            image_area_ratio=(image_area / area) if area else 0.0,
        )
        if page.is_rotated:
            diag.warn(
                Stage.LAYOUT,
                f"page {page.number} is rotated {page.rotation}°",
                hint="" if doc.backend == "pymupdf" else
                     "Coordinates on rotated pages are approximate with the "
                     "poppler backend; install PyMuPDF for exact geometry.",
                page=page.number,
            )
        if layout_page.is_image_only:
            image_only_pages.append(page.number)
        pages.append(layout_page)

    layout.pages = pages

    # Report scans once for the document rather than once per page: an
    # 80-page scan should produce one clear message, not eighty.
    if image_only_pages:
        if len(image_only_pages) == len(pages):
            diag.warn(
                Stage.LAYOUT,
                f"none of the {len(pages)} page(s) have a text layer -- this "
                "looks like a scanned PDF",
                hint="OCR it first (e.g. 'ocrmypdf in.pdf out.pdf'), then convert "
                     "the OCR'd copy.",
            )
        else:
            shown = ", ".join(str(n) for n in image_only_pages[:6])
            more = "" if len(image_only_pages) <= 6 else f" and {len(image_only_pages) - 6} more"
            diag.warn(
                Stage.LAYOUT,
                f"{len(image_only_pages)} page(s) have no text layer (scanned): "
                f"{shown}{more}",
                hint="Those pages will be empty in the generated page.",
            )

    # Running headers/footers need more than one page to be recognisable.
    if drop_furniture:
        furniture = find_running_furniture(pages)
        for page in pages:
            drop = furniture.get(page.number, set())
            if not drop:
                continue
            kept: List[LaidOutLine] = []
            for i, line in enumerate(page.lines):
                if i in drop:
                    line.region = REGION_HEADER if line.y0 <= page.height * HEADER_BAND else REGION_FOOTER
                    page.dropped.append(line)
                    if line.text not in layout.furniture:
                        layout.furniture.append(line.text)
                else:
                    kept.append(line)
            page.lines = kept

    # Cover matter, title pages and summaries before the numbered body, and
    # a publication's own contents list after it.
    if drop_front_matter:
        body_start = detect_body_start(pages)
        if body_start is not None and body_start[2] > 0:
            drop_before_body(pages, body_start, diag)
        tail_start = detect_tail_matter(pages)
        if tail_start is not None:
            drop_tail_matter(pages, tail_start, diag)

    # Columns and reading order, page by page.
    for page in pages:
        count, bands = detect_columns(page.lines, page.width)
        page.columns = count
        assign_columns(page.lines, bands)
        page.lines.sort(key=lambda l: (l.column, round(l.bbox.y0, 1), l.bbox.x0))
        for order, line in enumerate(page.lines):
            line.order = order
        body = [l for l in page.lines if l.region == REGION_BODY]
        if body:
            box = body[0].bbox
            for line in body[1:]:
                box = box.union(line.bbox)
            page.body_box = box

    # Indentation needs every page in hand before it can be normalised.
    compute_indents(pages)

    # Compounds that appear intact on a line ("self-determination") tell us
    # that the same word split over a line break is one word, not two.
    for page in pages:
        for line in page.lines:
            text = line.text.rstrip()
            for match in HYPHENATED_TOKEN_RE.finditer(text):
                if match.end() == len(text):
                    continue  # this one *is* the line-break hyphen
                layout.hyphenated_terms.add(match.group(1).lower())

    # Body font size: the size covering the most characters across all pages.
    size_weights: Counter = Counter()
    for page in pages:
        for line in page.lines:
            size_weights[round(line.size, 1)] += max(1, len(line.text))
    layout.body_size = size_weights.most_common(1)[0][0] if size_weights else 0.0
    layout.repairs = dict(stats)

    if stats.get("ligature"):
        diag.info(
            Stage.LAYOUT,
            f"repaired {stats['ligature']} ligature/encoding artifact(s) in the "
            "extracted text (e.g. 'di\"erent' -> 'different')",
        )
    return layout


# ---------------------------------------------------------------------------
# Front matter
# ---------------------------------------------------------------------------

#: A table-of-contents style label: "B. Obligations of the buyer",
#: "IV. Final provisions", "12. Entry into force".
TOC_LABEL_RE = re.compile(r"^(?:[A-Z]{1,3}\.|[IVXLCDM]{1,6}\.|\d{1,3}\.)\s+\S")
_TOC_TERMINAL_RE = re.compile(r"[.,;:!?]\s*$")

#: ICJ advisory opinions and judgments open with a multi-page "summary" or
#: index made of entries like
#:   "Adaptation obligations — Article 7, paragraph 9, of the Paris Agreement — ..."
#: which are not prose and should not become hundreds of paragraphs.  Two or
#: more spaced dashes, on many consecutive lines, is a reliable signal.
INDEX_ENTRY_RE = re.compile(r"\s[\u2014\u2013]\s")
INDEX_SEPARATORS = 2


@dataclass
class FrontMatter:
    """A run of leading lines judged to be a table of contents / cover page."""

    page: int = 0
    count: int = 0
    reason: str = ""
    text: List[str] = field(default_factory=list)


def detect_front_matter(pages: Sequence[PageLayout]) -> Optional[FrontMatter]:
    """Find a leading table-of-contents block, if there is one.

    Two independent signals, both requiring a *consecutive run* so that a
    genuine heading (always followed by body text) cannot be mistaken for
    one:

    * a run of lines with dot leaders and trailing page numbers, and
    * a run of short "A. Something" / "IV. Something" labels that are not
      sentences -- in a real document the body text after a heading breaks
      the run immediately, so four or more in a row is a contents list.

    The caller reports whatever is dropped; a wrong guess here silently
    deletes content, so the bar is deliberately high.
    """
    collected: List[LaidOutLine] = []
    for page in pages:
        for line in page.lines:
            collected.append(line)
    if len(collected) < 8:
        return None

    # A contents list lives at the front.  Scanning further is how a run of
    # four headings deep inside a treaty gets mistaken for one.
    window = collected[:120]

    # Signal 1: dot leaders.
    run = 0
    best_start, best_len = None, 0
    for i, line in enumerate(window):
        if textutil.looks_like_toc_entry(line.text):
            run += 1
            if run > best_len:
                best_len, best_start = run, i - run + 1
        else:
            run = 0
    if best_len >= 4 and best_start is not None:
        block = window[best_start:best_start + best_len]
        return FrontMatter(page=block[0].page, count=len(block),
                           reason="table of contents",
                           text=[l.text for l in block])

    # Signal 2: a run of bare contents labels.
    run = 0
    best_start, best_len = None, 0
    for i, line in enumerate(window):
        text = line.text.strip()
        if (TOC_LABEL_RE.match(text) and len(text) <= 90
                and not _TOC_TERMINAL_RE.search(text)):
            run += 1
            if run > best_len:
                best_len, best_start = run, i - run + 1
        else:
            run = 0
    if best_len >= 4 and best_start is not None:
        block = window[best_start:best_start + best_len]
        return FrontMatter(page=block[0].page, count=len(block),
                           reason="contents list",
                           text=[l.text for l in block])

    # Signal 3: a run of dash-separated index entries.
    run = 0
    best_start, best_len = None, 0
    for i, line in enumerate(window):
        if len(INDEX_ENTRY_RE.findall(line.text)) >= INDEX_SEPARATORS:
            run += 1
            if run > best_len:
                best_len, best_start = run, i - run + 1
        else:
            run = 0
    if best_len >= 6 and best_start is not None:
        block = window[best_start:best_start + best_len]
        return FrontMatter(page=block[0].page, count=len(block),
                           reason="summary/index of dash-separated entries",
                           text=[l.text for l in block])
    return None
