"""Annotation parsing, matching, and the Word/PDF annotation sources."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.helpers import build_docx, fixture, has_fixture

from ilatool import annotations as ann_mod, backends, docx, sourcefmt, structure
from ilatool.errors import AnnotationError, Diagnostics

SOURCE_TEXT = """TITLE: Test
---
¶ The first paragraph makes a short and plain statement about treaties.
¶ The second paragraph adds a little more detail about customary international law.
¶ A completely different closing paragraph mentions neither of the above.
"""


def source():
    return sourcefmt.parse_source_text(SOURCE_TEXT)


class PlainTextFormatTests(unittest.TestCase):
    def test_id_block_with_fields(self):
        text = ("@para-k1\nAuthor: A. One\nTitle: T\nSource: S\n"
                "The note body\nspans lines.\n")
        items = ann_mod.parse_annotations_text(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].para, "para-k1")
        self.assertEqual(items[0].author, "A. One")
        self.assertEqual(items[0].text, "The note body spans lines.")

    def test_quoted_excerpt_block(self):
        text = ('"""\nThe second paragraph adds a little more detail\n"""\n'
                "Author: B. Two\nA note about it.\n")
        items = ann_mod.parse_annotations_text(text)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].quote.startswith("The second paragraph"))
        self.assertEqual(items[0].author, "B. Two")

    def test_fields_after_the_body_are_part_of_the_body(self):
        text = "@x\nA note.\nAuthor: not a field here\n"
        items = ann_mod.parse_annotations_text(text)
        self.assertEqual(items[0].author, "")
        self.assertIn("Author: not a field here", items[0].text)


class JsonAndCsvTests(unittest.TestCase):
    def test_json_list(self):
        items = ann_mod.parse_annotations_json(json.dumps([
            {"para": "k1", "author": "A", "text": "one"},
            {"quote": "excerpt", "text": "two"},
        ]))
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].para, "k1")
        self.assertEqual(items[1].quote, "excerpt")

    def test_json_object_form_is_accepted(self):
        items = ann_mod.parse_annotations_json(json.dumps({"para-k1": [{"text": "hi"}]}))
        self.assertEqual(items[0].para, "para-k1")

    def test_bad_json_is_rejected_clearly(self):
        with self.assertRaises(AnnotationError):
            ann_mod.parse_annotations_json("[{,]")

    def test_csv_from_the_prep_folder_layout(self):
        text = ("id,row_type,label,text_md,comment_author,comment_title,"
                "comment_text,comment_source\n"
                "para-001,para,1.,Some text,A. One,,A note here,Smith 2020\n"
                "para-002,para,2.,More text,,,,\n")
        items = ann_mod.parse_annotations_csv(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].para, "para-001")
        self.assertEqual(items[0].author, "A. One")
        self.assertEqual(items[0].source, "Smith 2020")

    def test_csv_without_a_text_column_is_rejected(self):
        with self.assertRaises(AnnotationError):
            ann_mod.parse_annotations_csv("id,other\n1,2\n")


class NotesJsTests(unittest.TestCase):
    def test_round_trip_through_the_generated_format(self):
        from ilatool import render
        notes = {"para-k1": [{"author": "A", "title": "", "text": "hello", "source": ""}]}
        text = render.render_notes_js(notes)
        parsed = ann_mod.parse_notes_js(text)
        self.assertEqual(parsed["para-k1"][0]["text"], "hello")

    def test_rubbish_is_rejected(self):
        with self.assertRaises(AnnotationError):
            ann_mod.parse_notes_js("this is not a notes file")


class LoadingTests(unittest.TestCase):
    def test_extension_dispatch(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "notes.json"
            path.write_text(json.dumps([{"para": "k1", "text": "x"}]), encoding="utf-8")
            self.assertEqual(len(ann_mod.load_annotations(path)), 1)
            path2 = Path(tmp) / "notes.txt"
            path2.write_text("@k1\nA note.\n", encoding="utf-8")
            self.assertEqual(len(ann_mod.load_annotations(path2)), 1)


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.source = source()
        self.paragraphs = self.source.paragraphs

    def test_exact_excerpt_matches_one_paragraph(self):
        target, status, score, _ = ann_mod.match_quote(
            "The second paragraph adds a little more detail about customary "
            "international law.", self.paragraphs)
        self.assertEqual(status, "ok")
        self.assertGreater(score, 0.9)
        self.assertEqual(target, self.paragraphs[1].id)

    def test_small_typos_are_tolerated(self):
        target, status, _, _ = ann_mod.match_quote(
            "The second paragraph adds a litlte more detail about customary "
            "international law.", self.paragraphs)
        self.assertEqual(status, "ok")
        self.assertIsNotNone(target)

    def test_unrelated_excerpt_does_not_match(self):
        target, status, _, _ = ann_mod.match_quote(
            "Quantum chromodynamics describes the strong interaction.", self.paragraphs)
        self.assertEqual(status, "no_match")
        self.assertIsNone(target)

    def test_identical_paragraphs_are_ambiguous_rather_than_guessed(self):
        text = ("---\n¶ The same sentence appears here.\n"
                "¶ The same sentence appears here.\n")
        duplicated = sourcefmt.parse_source_text(text)
        # Duplicate ids are not allowed in a source file, so build the blocks
        # by hand to exercise the ambiguity path.
        blocks = [
            structure.Block(kind="para", text="The same sentence appears here.", key="a"),
            structure.Block(kind="para", text="The same sentence appears here.", key="b"),
        ]
        target, status, _, runners = ann_mod.match_quote(
            "The same sentence appears here.", blocks)
        self.assertEqual(status, "ambiguous")
        self.assertIsNone(target)
        self.assertEqual(len(runners), 2)

    def test_attach_reports_unplaced_instead_of_guessing(self):
        real_id = self.paragraphs[0].id
        items = [
            ann_mod.Annotation(text="good", quote="The second paragraph adds a little more detail"),
            ann_mod.Annotation(text="bad", quote="nothing like this appears anywhere"),
            ann_mod.Annotation(text="by id", para=real_id),
            ann_mod.Annotation(text="dangling", para="para-does-not-exist"),
        ]
        result = ann_mod.attach(items, self.source, Diagnostics())
        self.assertEqual(result.note_count, 2)
        self.assertEqual(len(result.unplaced), 2)
        reasons = " ".join(why for _, why in result.unplaced)
        self.assertIn("no paragraph with id", reasons)

    def test_duplicate_notes_are_collapsed(self):
        real_id = self.paragraphs[0].id
        items = [ann_mod.Annotation(text="same", para=real_id),
                 ann_mod.Annotation(text="same", para=real_id)]
        result = ann_mod.attach(items, self.source, Diagnostics())
        self.assertEqual(result.note_count, 1)
        self.assertEqual(result.duplicates, 1)

    def test_quote_spanning_several_paragraphs_attaches_to_the_first(self):
        # What a Word comment anchored over a whole article looks like.
        blocks = [
            structure.Block(kind="para", key="a", text="Article 5 The first clause sets out the rule."),
            structure.Block(kind="para", key="b", text="The second clause adds a qualification to it."),
            structure.Block(kind="para", key="c", text="The third clause explains how it is applied."),
            structure.Block(kind="para", key="d", text="Something completely unrelated follows later."),
        ]
        quote = ("Article 5 The first clause sets out the rule. The second clause "
                 "adds a qualification to it. The third clause explains how it is "
                 "applied.")
        target, status, _, _ = ann_mod.match_quote(quote, blocks)
        self.assertEqual(status, "ok")
        self.assertEqual(target, "para-a")

    def test_a_short_heading_inside_a_long_quote_does_not_win(self):
        blocks = [
            structure.Block(kind="para", key="head", text="BUYER"),
            structure.Block(kind="para", key="body", page=1, text=(
                "The buyer must pay the price for the goods and take delivery of "
                "them as required by the contract and this Convention.")),
        ]
        quote = ("Article 53 The buyer must pay the price for the goods and take "
                 "delivery of them as required by the contract and this Convention.")
        target, status, _, _ = ann_mod.match_quote(quote, blocks)
        self.assertEqual(target, "para-body")

    def test_an_anchor_over_part_of_a_paragraph_still_matches(self):
        paragraph = ("A party who fails to pay the price or any other sum that is "
                     "in arrears is liable to pay interest on it, without prejudice "
                     "to any claim for damages recoverable under article 74.")
        blocks = [
            structure.Block(kind="para", key="x", text="An unrelated opening clause."),
            structure.Block(kind="para", key="y", text=paragraph),
            structure.Block(kind="para", key="z", text="Something else entirely."),
        ]
        # The comment covers the first sentence only, then runs into the next
        # article, so neither string contains the other.
        quote = ("Article 78 A party who fails to pay the price or any other sum "
                 "that is in arrears is liable to pay interest on it, without "
                 "prejudice to any claim for damages recoverable under article 74. "
                 "Article 79 A party is not liable for a failure to perform any of "
                 "his obligations if he proves that the failure was due to an "
                 "impediment beyond his control.")
        target, status, _, _ = ann_mod.match_quote(quote, blocks)
        self.assertEqual(status, "ok")
        self.assertEqual(target, "para-y")

    def test_matching_a_long_document_is_fast(self):
        # The naive version took over thirty seconds on a 260-paragraph
        # document; a regression here makes the tool feel broken.
        import time
        blocks = [
            structure.Block(kind="para", key=f"k{i}", page=i // 10, text=(
                f"Clause {i} provides that the parties shall act in good faith with "
                f"respect to the matters described in the preceding provisions and "
                f"shall refrain from any measure that would defeat the object of "
                f"clause {i} of the present instrument."))
            for i in range(300)
        ]
        quotes = [f"Clause {i} provides that the parties shall act in good faith with "
                  f"respect to the matters described in the preceding provisions"
                  for i in range(0, 300, 15)]
        started = time.time()
        placed = 0
        for quote in quotes:
            target, status, _, _ = ann_mod.match_quote(quote, blocks)
            placed += 1 if target else 0
        elapsed = time.time() - started
        self.assertEqual(placed, len(quotes))
        self.assertLess(elapsed, 3.0, f"matching took {elapsed:.1f}s")

    def test_page_hint_breaks_a_close_call(self):
        blocks = [
            structure.Block(kind="para", text="Alpha beta gamma delta.", key="a", page=1),
            structure.Block(kind="para", text="Alpha beta gamma delta!", key="b", page=7),
        ]
        target, status, _, _ = ann_mod.match_quote("Alpha beta gamma delta.", blocks, page_hint=7)
        self.assertEqual(status, "ok")
        self.assertEqual(target, "para-b")


@unittest.skipUnless(has_fixture("annotated"), "fixtures missing")
class PdfAnnotationTests(unittest.TestCase):
    def setUp(self):
        self.doc = backends.load_pdf(fixture("annotated"))

    def test_every_subtype_is_read(self):
        kinds = {a.kind for a in self.doc.all_annotations()}
        for expected in ("highlight", "underline", "strikeout", "text", "square"):
            self.assertIn(expected, kinds)

    def test_author_and_contents_survive(self):
        annots = self.doc.pages[0].annotations
        highlight = next(a for a in annots if a.kind == "highlight")
        self.assertEqual(highlight.author, "A. Researcher")
        self.assertTrue(highlight.contents)

    def test_markup_annotations_carry_quad_points(self):
        highlight = next(a for a in self.doc.pages[0].annotations if a.kind == "highlight")
        self.assertTrue(highlight.quads or highlight.rect.width > 0)

    def test_overlapping_annotations_are_both_kept(self):
        page2 = self.doc.pages[1].annotations
        self.assertGreaterEqual(len(page2), 3)
        rects = [a.rect for a in page2]
        overlaps = sum(1 for i, a in enumerate(rects)
                       for b in rects[i + 1:] if a.overlap_area(b) > 0)
        self.assertGreaterEqual(overlaps, 1, "the fixture has overlapping annotations")

    def test_marked_up_text_is_recovered(self):
        from ilatool.layout import analyse_document
        laid_out = analyse_document(self.doc, Diagnostics())
        items = ann_mod.annotations_from_pdf(self.doc, laid_out.pages)
        highlights = [i for i in items if i.kind == "highlight"]
        self.assertTrue(highlights)
        self.assertTrue(any(i.quote for i in highlights),
                        "the text under a highlight should be recovered as its anchor")


class DocxCommentTests(unittest.TestCase):
    def test_comments_carry_author_text_and_anchor(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "instrument.docx"
            build_docx(
                path,
                paragraphs=[
                    "The first paragraph makes a short and plain statement about treaties.",
                    "The second paragraph adds a little more detail about customary law.",
                ],
                comments=[
                    ("Dr Smith", "This phrasing is drawn from the 1969 Convention", 1,
                     "adds a little more detail"),
                ],
            )
            comments = docx.read_docx_comments(path)
            self.assertEqual(len(comments), 1)
            self.assertEqual(comments[0].author, "Dr Smith")
            self.assertIn("1969 Convention", comments[0].text)
            self.assertIn("adds a little more detail", comments[0].quote)

    def test_comments_become_attachable_annotations(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "instrument.docx"
            build_docx(
                path,
                paragraphs=["The first paragraph makes a short and plain statement "
                            "about treaties."],
                comments=[("Dr Smith", "A note", 0, "short and plain statement")],
            )
            items = docx.annotations_from_docx(path)
            self.assertEqual(items[0].origin, "docx")
            src = sourcefmt.parse_source_text(
                "---\n¶ The first paragraph makes a short and plain statement "
                "about treaties.\n")
            result = ann_mod.attach(items, src, Diagnostics())
            self.assertEqual(result.note_count, 1)

    def test_a_docx_without_comments_is_reported_clearly(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "plain.docx"
            build_docx(path, ["Just text."], [])
            with self.assertRaises(AnnotationError):
                docx.read_docx_comments(path)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
