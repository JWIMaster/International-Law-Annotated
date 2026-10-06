"""Every published text, regenerated from its material.

This is the check that matters in the end: the site's own pages, rebuilt from
the files in ``materials/``, with the formatting inequalities that make a
page readable asserted mechanically.  It is fast (the whole corpus converts
in a few seconds) and it skips any text whose material is not present.

The published page is used as a *content* reference, not a formatting one --
the older pages were produced by several different pipelines -- so the
similarity bar is deliberately loose while the formatting rules are strict.
"""

from __future__ import annotations

import html
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.helpers import ROOT

from ilatool import pipeline, textutil

#: published page -> (material, annotations) relative to materials/
CORPUS = [
    ("cisg", "CISG/CISG_english.pdf", "CISG/CISG_annotated.docx"),
    ("kyoto", "UNFCCC/Kyoto Protocol Annotated.docx", None),
    ("paris", "UNFCCC/Paris Annotations.docx", None),
    ("unfccc", "UNFCCC/UNFCCC Annotations.docx", None),
    ("reparations", "Reparations for Injury AO/Reparations-AO-Annotated.docx", None),
    ("opt-advisory-opinion", "OPT-advisory-opinion:/OPT Advisory Opinion.pdf",
     "OPT-advisory-opinion:/Annotated_OPT_Advisory_Opinion.docx"),
    ("nicaragua-germany", "nicaragua-germany:/Nicaragua v Germany.pdf",
     "nicaragua-germany:/Annotated_Nicaragua_v_Germany.docx"),
]

JUNK_RE = re.compile(
    r"^(?:page\s+\d|\d{1,4}\s+of\s+\d{1,4}|[-–—]?\s*\d{1,4}\s*[-–—]?|"
    r"https?://\S+|running head content)$", re.IGNORECASE)

#: A paragraph that opens with a lower-case letter is almost always the tail
#: of the sentence before it, i.e. a paragraph break in the wrong place.
MAX_LOWERCASE_STARTS = 2
MAX_PARAGRAPH_CHARS = 4000
MIN_SIMILARITY = 0.40


_ENDS_RE = re.compile(r"[.,;:!?][\"'\u201d\u2019)\]]*$")


def _broken_continuations(paragraphs):
    """Paragraphs that are the tail of the sentence before them.

    Two things make a lower-case opening legitimate: the paragraph's marker
    was moved into its label ("(a) when the States are..."), or it is a list
    entry that happens to start lower-case ("for the State of Palestine:").
    What is never legitimate is a paragraph that opens lower-case when the
    paragraph before it stopped without any punctuation at all -- that is one
    sentence with a break through the middle of it.
    """
    broken = []
    for index, block in enumerate(paragraphs):
        if not block.text[:1].islower() or block.label not in ("\u00b6", ""):
            continue
        if index == 0:
            continue
        previous = paragraphs[index - 1].text.strip()
        if previous and not _ENDS_RE.search(previous):
            broken.append(block.text)
    return broken


def _published(name: str):
    page = ROOT / "texts" / f"{name}.html"
    if not page.exists():
        return None
    text = page.read_text(encoding="utf-8", errors="ignore")
    return [html.unescape(re.sub(r"<[^>]+>", "", body)).strip()
            for _pid, body in re.findall(
                r'<div id="(para-[^"]+)">.*?<p>(.*?)</p>', text, re.DOTALL)]


class CorpusFormattingTests(unittest.TestCase):
    """One conversion per text, shared by all the assertions below."""

    @classmethod
    def setUpClass(cls):
        cls.results = {}
        # The outputs have to outlive setUpClass: the tests below re-read them.
        cls._tmp = TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        for name, material, annotations in CORPUS:
            path = ROOT / "materials" / material
            if not path.exists():
                continue
            report = pipeline.run(pipeline.RunRequest(
                source=path,
                annotations=(ROOT / "materials" / annotations) if annotations else None,
                out_html=tmp / f"{name}.html",
                out_notes=tmp / f"{name}-notes.js",
                out_source=tmp / f"{name}.txt",
                meta={"TITLE": name}))
            cls.results[name] = (report, _published(name))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _cases(self):
        if not self.results:
            self.skipTest("no materials present")
        return sorted(self.results.items())

    # -- the build itself ----------------------------------------------------

    def test_every_text_converts_and_validates(self):
        for name, (report, _published_paras) in self._cases():
            with self.subTest(text=name):
                self.assertIsNone(report.error, report.error)
                self.assertIsNotNone(report.result)
                self.assertTrue(report.result.validation.ok,
                                f"{name}: {report.result.validation.summary()}")

    def test_every_text_produces_paragraphs(self):
        for name, (report, _published_paras) in self._cases():
            with self.subTest(text=name):
                # Headings are not asserted: a document with none is a valid
                # document (the Nicaragua order has no headings at all).
                self.assertGreater(report.result.stats["paragraphs"], 10, name)

    # -- formatting ----------------------------------------------------------

    def test_no_paragraph_starts_mid_sentence(self):
        for name, (report, _published_paras) in self._cases():
            with self.subTest(text=name):
                broken = _broken_continuations(report.result.source.paragraphs)
                self.assertLessEqual(
                    len(broken), MAX_LOWERCASE_STARTS,
                    f"{name}: {len(broken)} paragraph(s) begin mid-sentence, "
                    f"e.g. {broken[:3]}")

    def test_no_paragraph_is_a_runaway_merge(self):
        for name, (report, _published_paras) in self._cases():
            with self.subTest(text=name):
                lengths = [len(b.text) for b in report.result.source.paragraphs]
                self.assertLess(max(lengths), MAX_PARAGRAPH_CHARS,
                                f"{name}: longest paragraph is {max(lengths)} chars")

    def test_no_paragraph_is_page_furniture(self):
        for name, (report, _published_paras) in self._cases():
            with self.subTest(text=name):
                junk = [b.text for b in report.result.source.paragraphs
                        if JUNK_RE.match(b.text.strip())]
                self.assertEqual(junk, [], f"{name}: furniture left in the body")

    def test_no_paragraph_is_just_punctuation(self):
        for name, (report, _published_paras) in self._cases():
            with self.subTest(text=name):
                empty = [b.text for b in report.result.source.paragraphs
                         if not any(ch.isalnum() for ch in b.text)]
                self.assertEqual(empty, [], name)

    # -- content -------------------------------------------------------------

    def test_the_published_text_is_preserved(self):
        for name, (report, published) in self._cases():
            if not published:
                continue
            with self.subTest(text=name):
                mine = [b.text for b in report.result.source.paragraphs]
                import difflib
                ratio = difflib.SequenceMatcher(
                    None,
                    textutil.normalize_for_match(" ".join(published)),
                    textutil.normalize_for_match(" ".join(mine))).ratio()
                self.assertGreater(
                    ratio, MIN_SIMILARITY,
                    f"{name}: only {ratio:.2f} of the published text survives")

    def test_notes_attach_where_the_material_has_comments(self):
        for name, (report, _published_paras) in self._cases():
            with self.subTest(text=name):
                stats = report.result.stats
                if not report.result.match.attached and not report.result.notes:
                    # No comments in this material: nothing to attach.
                    self.assertEqual(stats["annotated"], 0, name)
                    continue
                self.assertGreater(stats["annotated"], 0, name)
                unattached = stats["unplaced"]
                total = stats["annotated"] + unattached
                self.assertGreaterEqual(
                    stats["annotated"], total * 0.6,
                    f"{name}: only {stats['annotated']} of {total} notes attached")

    def test_the_generated_files_exist_and_are_well_formed(self):
        for name, (report, _published_paras) in self._cases():
            with self.subTest(text=name):
                html_path = Path(report.result.html_path)
                notes_path = Path(report.result.notes_path)
                self.assertTrue(html_path.exists(), name)
                self.assertTrue(notes_path.exists(), name)
                page = html_path.read_text(encoding="utf-8")
                self.assertIn("</html>", page)
                self.assertEqual(page.count('data-para="'), page.count('id="para-'),
                                 f"{name}: markers and paragraphs disagree")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
