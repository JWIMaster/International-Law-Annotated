"""Parity with the page the project already publishes.

``texts/nicaragua-germany.html`` is the hand-checked output for the
Nicaragua v. Germany order.  Converting ``materials/nicaragua-germany:/
Nicaragua v Germany.pdf`` should reproduce the same paragraphs: same
boundaries, same wording, and the Word comments landing on the same content.

That page is the reference for "does the text formatting look right", so it
is also the regression test for it.  A handful of near-misses are tolerated
(spacing, one stray character) but the structure has to hold.
"""

from __future__ import annotations

import html
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.helpers import ROOT

from ilatool import pipeline, textutil

PDF = ROOT / "materials" / "nicaragua-germany:" / "Nicaragua v Germany.pdf"
COMMENTS = ROOT / "materials" / "nicaragua-germany:" / "Annotated_Nicaragua_v_Germany.docx"
REFERENCE = ROOT / "texts" / "nicaragua-germany.html"

#: How many of the reference paragraphs must come out word for word.  One of
#: them ("For these reasons,") is a stub the reference stops at, and OCR-grade
#: spacing differences are tolerated, so this is not the full count.
MIN_EXACT = 24

_PARA_RE = re.compile(r'<div id="(para-[^"]+)">.*?<p>(.*?)</p>', re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def reference_paragraphs():
    text = REFERENCE.read_text(encoding="utf-8")
    out = []
    for _pid, body in _PARA_RE.findall(text):
        out.append(html.unescape(_TAG_RE.sub("", body)).strip())
    return out


@unittest.skipUnless(PDF.exists(), "the Nicaragua PDF is not present")
@unittest.skipUnless(REFERENCE.exists(), "the reference page is not present")
class NicaraguaParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference = reference_paragraphs()
        cls._tmp = TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        cls.report = pipeline.run(pipeline.RunRequest(
            source=PDF,
            annotations=COMMENTS if COMMENTS.exists() else None,
            out_html=tmp / "nicaragua.html",
            out_notes=tmp / "nicaragua-notes.js",
            out_source=tmp / "nicaragua.txt",
            meta={"TITLE": "Nicaragua v Germany"},
        ))
        cls.source = cls.report.result.source if cls.report.result else None

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_conversion_succeeds_and_validates(self):
        self.assertTrue(self.report.ok, self.report.error)
        self.assertTrue(self.report.result.validation.ok,
                        self.report.result.validation.summary())

    def test_the_reference_paragraphs_come_out_word_for_word(self):
        mine = [b.text for b in self.source.blocks]
        exact = sum(
            1 for i in range(min(len(self.reference), len(mine)))
            if textutil.normalize_for_match(self.reference[i])
            == textutil.normalize_for_match(mine[i])
        )
        self.assertGreaterEqual(
            exact, MIN_EXACT,
            f"only {exact}/{len(self.reference)} paragraphs match the published "
            "page:\n" + "\n".join(
                f"  {i+1}: ref {self.reference[i][:60]!r}\n"
                f"      mine {mine[i][:60]!r}"
                for i in range(min(6, len(self.reference), len(mine)))
                if textutil.normalize_for_match(self.reference[i])
                != textutil.normalize_for_match(mine[i])
            ))

    def test_no_runaway_paragraphs(self):
        # A single paragraph of ten thousand characters means two paragraphs
        # were merged, which is as wrong as splitting one in two.
        lengths = [len(b.text) for b in self.source.paragraphs]
        self.assertLess(max(lengths), 4000)

    def test_the_cover_matter_is_not_in_the_body(self):
        joined = " ".join(b.text for b in self.source.blocks)
        self.assertNotIn("COUR INTERNATIONALE DE JUSTICE", joined)
        self.assertNotIn("RECUEIL DES ARRÊTS", joined)
        self.assertNotIn("running head content", joined)
        self.assertNotIn("(ord. 30 IV 24)", joined)

    def test_the_body_starts_at_paragraph_one(self):
        self.assertTrue(self.source.paragraphs[0].text.startswith("On 1 March 2024"))

    def test_paragraph_five_keeps_its_quoted_list_inline(self):
        # The reference has this entire request -- including the numbered
        # sub-items -- as one paragraph.
        para = next(b for b in self.source.paragraphs
                    if b.text.startswith("At the end of its Request"))
        self.assertIn("provisional measures", para.text)
        self.assertIn("(1)", para.text)
        self.assertIn("(4)", para.text)
        self.assertGreater(len(para.text), 1400)

    def test_paragraph_ten_keeps_the_counsel_list_inline(self):
        para = next(b for b in self.source.paragraphs
                    if b.text.startswith("At the public hearings"))
        self.assertIn("On behalf of Nicaragua", para.text)
        self.assertIn("On behalf of Germany", para.text)

    @unittest.skipUnless(COMMENTS.exists(), "the annotated docx is not present")
    def test_the_word_comments_land_on_the_right_paragraphs(self):
        notes = self.report.result.notes
        self.assertEqual(self.report.result.stats["notes"], 3)
        self.assertEqual(self.report.result.match.unplaced, [])

        by_text = {b.id: b.text for b in self.source.paragraphs}
        targets = [by_text[pid] for pid in notes]
        self.assertTrue(any("significant decrease since November 2023" in t
                            for t in targets),
                        "the arms-export note should sit on the paragraph about "
                        "the decrease in exports")
        self.assertTrue(any("remind all States of their international obligations" in t
                            for t in targets),
                        "the Arms Trade Treaty note should sit on the reminder "
                        "paragraph")
        self.assertTrue(any("reaffirms that the decision" in t for t in targets),
                        "the Monetary Gold note should sit on the reaffirmation "
                        "paragraph")

    def test_real_hyphenated_compounds_keep_their_hyphen(self):
        joined = " ".join(b.text for b in self.source.blocks)
        self.assertIn("self-determination", joined)
        self.assertNotIn("selfdetermination", joined)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
