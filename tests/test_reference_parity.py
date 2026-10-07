"""Parity with the page the project used to publish.

The hand-checked page for the Nicaragua v. Germany order is kept frozen at
``tests/fixtures/reference/``.  Converting ``materials/nicaragua-germany:/
Nicaragua v Germany.pdf`` should reproduce the same paragraphs: same
boundaries, same wording, and the Word comments landing on the same content.

It is frozen rather than read from ``texts/`` because ``texts/`` now holds
*this tool's* output: comparing a build against itself would assert nothing.
The fixture is the last page a person checked by hand, so it is the standard
for "does the text formatting look right".  A handful of near-misses are
tolerated (spacing, one stray character) but the structure has to hold.
"""

from __future__ import annotations

import html
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.helpers import ROOT

from ilatool import annotations as ann_mod, migrate, pipeline, textutil

PDF = ROOT / "materials" / "nicaragua-germany:" / "Nicaragua v Germany.pdf"
COMMENTS = ROOT / "materials" / "nicaragua-germany:" / "Annotated_Nicaragua_v_Germany.docx"
#: The notes that were published with that page, kept beside it.
REFERENCE_NOTES = (ROOT / "tests" / "fixtures" / "reference"
                   / "nicaragua-germany-notes.reference.js")
REFERENCE = ROOT / "tests" / "fixtures" / "reference" / "nicaragua-germany.reference.html"

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


@unittest.skipUnless(PDF.exists(), "the Nicaragua PDF is not present")
@unittest.skipUnless(REFERENCE_NOTES.exists(), "the published notes are not present")
class ReferenceNoteMigrationTests(unittest.TestCase):
    """Notes published against the old page must survive a rebuild.

    The old page keyed its notes by position (``para-18``); a rebuilt page
    keys them by paragraph text.  Carrying them over by *renumbering* would
    silently move every note to whatever is eighteenth now, so the migration
    goes through the paragraph each note was actually attached to.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        cls.published = ann_mod.parse_notes_js(REFERENCE_NOTES.read_text(encoding="utf-8"))
        cls.report = pipeline.run(pipeline.RunRequest(
            source=PDF,
            annotations=[REFERENCE_NOTES] + ([COMMENTS] if COMMENTS.exists() else []),
            out_html=tmp / "migrated.html",
            out_notes=tmp / "migrated-notes.js",
            out_source=tmp / "migrated.txt",
            meta={"TITLE": "Nicaragua v Germany"},
            build=pipeline.BuildOptions(migrate_from=REFERENCE),
        ))
        cls.migrated = (ann_mod.parse_notes_js((tmp / "migrated-notes.js").read_text())
                        if cls.report.ok else {})

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _key(self, note):
        return (note.get("author", "").strip(),
                textutil.normalize_for_match(note.get("text", "")))

    def test_the_build_succeeds_with_nothing_left_unplaced(self):
        self.assertTrue(self.report.ok, self.report.error)
        self.assertEqual(self.report.result.match.unplaced, [])

    def test_every_published_note_is_still_on_the_page(self):
        before = {self._key(n) for v in self.published.values() for n in v}
        after = {self._key(n) for v in self.migrated.values() for n in v}
        missing = sorted(before - after)
        self.assertEqual(missing, [], f"{len(missing)} published note(s) were lost")

    def test_no_note_was_renumbered_onto_a_different_paragraph(self):
        """Each migrated note sits on text the old page also associated it with."""
        reference_text = {pid: migrate.paragraph_texts(
            REFERENCE.read_text(encoding="utf-8")).get(pid, "")
            for pid in self.published}
        by_id = {b.id: b for b in self.report.result.source.paragraphs}
        for old_id, entries in self.published.items():
            old_text = textutil.normalize_for_match(reference_text.get(old_id, ""))
            if not old_text:
                continue
            for note in entries:
                key = self._key(note)
                target = next((pid for pid, notes in self.migrated.items()
                               if any(self._key(n) == key for n in notes)), None)
                self.assertIsNotNone(target, f"{key[1][:40]!r} lost its paragraph")
                new_text = textutil.normalize_for_match(by_id[target].text)
                self.assertTrue(
                    old_text in new_text or new_text in old_text,
                    f"note moved to unrelated text: {new_text[:60]!r} "
                    f"was {old_text[:60]!r}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
