"""Post-build checks on the generated site.

"It generated a file" is not the same as "the file is right".  These checks
compare what the generator *intended* with what actually landed on disk, so
a build that silently lost every annotation -- or attached them to ids that
do not exist in the page -- fails loudly instead of looking successful.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import Diagnostics, Stage
from .sourcefmt import SourceDocument

OK = "ok"
WARN = "warn"
FAIL = "fail"

_PARA_DIV_RE = re.compile(r'id="(para-[^"]+)"')
_DATA_PARA_RE = re.compile(r'data-para="(para-[^"]+)"')
_NOTES_SRC_RE = re.compile(r'<script[^>]+src="([^"]*notes[^"]*)"', re.IGNORECASE)


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""

    def as_dict(self) -> Dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class ValidationReport:
    checks: List[Check] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(name, status, detail))

    @property
    def failures(self) -> List[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warnings(self) -> List[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def ok(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        n_ok = sum(1 for c in self.checks if c.status == OK)
        parts = [f"{n_ok} check{'s' if n_ok != 1 else ''} passed"]
        if self.warnings:
            parts.append(f"{len(self.warnings)} warning(s)")
        if self.failures:
            parts.append(f"{len(self.failures)} FAILED")
        return ", ".join(parts)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": self.summary(),
            "checks": [c.as_dict() for c in self.checks],
            "stats": self.stats,
        }


def validate_site(html_path: Path, notes_path: Path, source: SourceDocument,
                  notes: Dict[str, List[Dict[str, str]]],
                  diagnostics: Optional[Diagnostics] = None) -> ValidationReport:
    diag = diagnostics if diagnostics is not None else Diagnostics()
    report = ValidationReport()

    try:
        html_text = html_path.read_text(encoding="utf-8")
    except OSError as exc:
        report.add("page written", FAIL, str(exc))
        return report
    report.add("page written", OK, f"{html_path.name} ({len(html_text):,} bytes)")

    div_ids = _PARA_DIV_RE.findall(html_text)
    data_ids = _DATA_PARA_RE.findall(html_text)
    report.stats["paragraphs_in_page"] = len(div_ids)
    report.stats["annotated_paragraphs"] = len(notes)
    report.stats["notes"] = sum(len(v) for v in notes.values())

    expected = source.ids
    missing = sorted(expected - set(div_ids))
    if missing:
        report.add("every paragraph is on the page", FAIL,
                   f"{len(missing)} paragraph(s) are missing, e.g. {', '.join(missing[:5])}")
        diag.error(Stage.VALIDATE,
                   f"{len(missing)} paragraph(s) did not make it into the page",
                   hint="This is a generator bug; please report it.")
    else:
        report.add("every paragraph is on the page", OK, f"{len(div_ids)} paragraphs")

    duplicates = sorted({i for i in div_ids if div_ids.count(i) > 1})
    if duplicates:
        report.add("paragraph ids are unique", FAIL,
                   f"duplicated: {', '.join(duplicates[:5])}")
    else:
        report.add("paragraph ids are unique", OK)

    # Every note must point at something the page can show.
    orphan_notes = sorted(set(notes) - set(div_ids))
    if orphan_notes:
        report.add("every note has a paragraph", FAIL,
                   f"{len(orphan_notes)} note target(s) have no paragraph, "
                   f"e.g. {', '.join(orphan_notes[:5])}")
        diag.error(Stage.VALIDATE,
                   f"{len(orphan_notes)} note(s) point at a paragraph that is not on the page")
    else:
        report.add("every note has a paragraph", OK, f"{len(notes)} target(s)")

    if set(data_ids) != set(div_ids):
        report.add("every paragraph is clickable", FAIL,
                   "some paragraphs have no marker button")
    else:
        report.add("every paragraph is clickable", OK)

    # The page must load the notes file that was actually written.
    match = _NOTES_SRC_RE.search(html_text)
    if not match:
        report.add("page loads its notes file", FAIL,
                   "no notes.js <script> tag found in the page")
    else:
        referenced = match.group(1).split("?")[0]
        if referenced != notes_path.name:
            report.add("page loads its notes file", FAIL,
                       f"page references '{referenced}' but '{notes_path.name}' was written")
        else:
            report.add("page loads its notes file", OK, referenced)

    if not notes_path.exists():
        report.add("notes file written", FAIL, f"{notes_path} is missing")
    else:
        report.add("notes file written", OK,
                   f"{notes_path.name} ({notes_path.stat().st_size:,} bytes)")
        try:
            from . import annotations as ann_mod
            parsed = ann_mod.parse_notes_js(notes_path.read_text(encoding="utf-8"))
            written = sum(len(v) for v in parsed.values())
            expected_notes = sum(len(v) for v in notes.values())
            if written != expected_notes:
                report.add("notes file is complete", FAIL,
                           f"{written} note(s) written, {expected_notes} expected")
            else:
                report.add("notes file is complete", OK, f"{written} note(s)")
        except Exception as exc:  # pragma: no cover - defensive
            report.add("notes file is loadable", FAIL, str(exc))

    empty_notes = sum(1 for entries in notes.values()
                      for note in entries if not str(note.get("text", "")).strip())
    if empty_notes:
        report.add("every note has text", WARN,
                   f"{empty_notes} note(s) have no text (bare highlights)")
    else:
        report.add("every note has text", OK)

    if "</html>" not in html_text:
        report.add("page is complete", FAIL, "the closing </html> tag is missing")
    else:
        report.add("page is complete", OK)

    return report
