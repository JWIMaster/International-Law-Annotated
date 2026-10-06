"""Moving existing notes onto a rebuilt page.

The published pages key their notes by positional ids (``para-9``).  A
rebuilt page derives its ids from the paragraph text, so those keys no longer
resolve -- and simply renumbering them would silently move every note to
whatever is ninth now.

What this module does instead is use the *published page itself* as the
authority for what each old id meant: it reads the paragraph text that sat
under ``para-9`` and finds that same paragraph in the new document.  Notes
therefore follow their sentence, and anything that cannot be placed is
reported rather than guessed at.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import annotations as ann_mod
from . import textutil
from .errors import Diagnostics, InputError, Stage
from .sourcefmt import SourceDocument

#: id="para-9" followed by its <p>…</p>
_PARA_RE = re.compile(r'<div id="(?P<id>[^"]+)">.*?<p>(?P<body>.*?)</p>', re.DOTALL)
_H3_RE = re.compile(r"<h3[^>]*>(?P<body>.*?)</h3>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return html.unescape(_TAG_RE.sub("", fragment)).strip()


@dataclass
class MigrationReport:
    exact: int = 0
    fuzzy: int = 0
    #: old id -> the text it held, for paragraphs that vanished entirely
    unmatched: List[Tuple[str, str]] = field(default_factory=list)
    #: old id -> the neighbouring paragraph its notes were moved to
    neighbouring: List[Tuple[str, str]] = field(default_factory=list)
    from_page: Optional[Path] = None

    @property
    def total(self) -> int:
        return self.exact + self.fuzzy + len(self.neighbouring)

    def summary(self) -> str:
        parts = [f"{self.exact} paragraph(s) matched exactly"]
        if self.fuzzy:
            parts.append(f"{self.fuzzy} matched closely")
        if self.neighbouring:
            parts.append(f"{len(self.neighbouring)} followed a removed paragraph")
        if self.unmatched:
            parts.append(f"{len(self.unmatched)} could not be placed at all")
        return ", ".join(parts)


def paragraph_texts(page_html: str) -> Dict[str, str]:
    """Every id -> its visible text, for a published page."""
    out: Dict[str, str] = {}
    for match in _PARA_RE.finditer(page_html):
        out[match.group("id")] = _text(match.group("body"))
    return out


def heading_texts(page_html: str) -> List[str]:
    return [_text(m.group("body")) for m in _H3_RE.finditer(page_html)]


def aliases_from_page(page_path: Path, source: SourceDocument,
                      diagnostics: Optional[Diagnostics] = None
                      ) -> Tuple[Dict[str, str], MigrationReport]:
    """Map the old page's paragraph ids onto the new document's keys.

    Returns ``(aliases, report)`` where ``aliases`` can be merged straight
    into :attr:`SourceDocument.aliases`, which is what the note matcher
    consults when an id does not resolve directly.
    """
    diag = diagnostics if diagnostics is not None else Diagnostics()
    report = MigrationReport(from_page=page_path)
    if not page_path.exists():
        raise InputError(f"no page to migrate from at '{page_path}'")
    try:
        page_html = page_path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        raise InputError(f"could not read '{page_path}'", cause=exc) from exc

    old = paragraph_texts(page_html)
    if not old:
        diag.warn(Stage.MATCHING,
                  f"'{page_path.name}' has no annotated paragraphs to migrate from")
        return {}, report

    paragraphs = source.paragraphs
    by_text: Dict[str, str] = {}
    for block in paragraphs:
        by_text.setdefault(textutil.normalize_for_match(block.text), block.key)

    # Where each old paragraph sat on the page, and where each new paragraph
    # sits in the document.  When a phrase is too generic to identify a
    # paragraph on its own -- "For these reasons," occurs three times in an
    # ICJ opinion -- the relative position is the tie-breaker: the documents
    # are largely the same document, so the same sentences stay in roughly
    # the same place.
    old_order = list(old.items())
    old_position = {pid: i / max(1, len(old_order) - 1)
                    for i, (pid, _text) in enumerate(old_order)}
    new_norm = [textutil.normalize_for_match(b.text) for b in paragraphs]
    keys = [b.key for b in paragraphs]
    total_new = max(1, len(paragraphs) - 1)

    def closest_by_position(candidates: List[int]) -> int:
        position = old_position.get(old_id, 0.0)
        return min(candidates, key=lambda i: abs(i / total_new - position))

    aliases: Dict[str, str] = {}
    placed_at: Dict[int, int] = {}   # old index -> new index
    unresolved: List[Tuple[int, str, str]] = []
    for old_index, (old_id, old_text) in enumerate(old_order):
        if old_id in source.ids:
            # The id still exists: nothing to migrate.
            continue
        needle = textutil.normalize_for_match(old_text)
        key = by_text.get(needle)
        if key:
            aliases[old_id] = key
            placed_at[old_index] = keys.index(key)
            report.exact += 1
            continue
        if not needle:
            unresolved.append((old_index, old_id, old_text))
            continue

        # Containment, then close similarity; both ranked by position when
        # more than one paragraph qualifies.
        containing = [i for i, text in enumerate(new_norm)
                      if text and (needle in text or text in needle)]
        if containing:
            index = containing[0] if len(containing) == 1 else closest_by_position(containing)
            aliases[old_id] = keys[index]
            placed_at[old_index] = index
            report.fuzzy += 1
            continue

        scored = [i for i, text in enumerate(new_norm)
                  if text and ann_mod._similarity(needle, text) >= ann_mod.QUOTE_MATCH_THRESHOLD]
        if scored:
            index = scored[0] if len(scored) == 1 else closest_by_position(scored)
            aliases[old_id] = keys[index]
            placed_at[old_index] = index
            report.fuzzy += 1
            continue
        unresolved.append((old_index, old_id, old_text))

    # A paragraph can disappear without its notes becoming meaningless: the
    # old page kept browser-print debris (a bare URL) as a paragraph, and a
    # note pinned to it was really a note about the text beside it.  Such
    # notes follow the nearest paragraph that did survive.  Every one of them
    # is listed in the report so it can be checked.
    for old_index, old_id, old_text in unresolved:
        neighbours = [index for index in placed_at if index != old_index]
        if not neighbours:
            report.unmatched.append((old_id, old_text[:70]))
            continue
        nearest = min(neighbours, key=lambda index: (abs(index - old_index), index))
        target_index = placed_at[nearest]
        aliases[old_id] = keys[target_index]
        report.neighbouring.append((old_id, old_text[:70]))

    if report.total:
        diag.info(Stage.MATCHING,
                  f"carried {report.total} paragraph id(s) over from "
                  f"'{page_path.name}' ({report.summary()})")
    if report.unmatched:
        shown = ", ".join(old_id for old_id, _ in report.unmatched[:8])
        diag.warn(
            Stage.MATCHING,
            f"{len(report.unmatched)} paragraph(s) from '{page_path.name}' are not "
            f"in the new text, so notes on them cannot be carried over "
            f"({shown})",
            hint="Those paragraphs were changed or removed by the new parse; "
                 "check the notes listed in the build report.",
        )
    return aliases, report
