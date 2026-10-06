"""Focused tests for the paragraph-splitting rules.

Each of these pins down a rule that was added because a real document came
out wrong without it.  They use hand-built lines so the rule under test is
the only thing that can make them pass or fail.
"""

from __future__ import annotations

import unittest

from tests.helpers import ROOT  # noqa: F401  (ensures the package path)

from ilatool import structure, textutil
from ilatool.errors import Diagnostics
from ilatool.layout import LaidOutLine
from ilatool.pdfmodel import Rect


def line(text, y, x=100.0, size=10.0, width=None, page=1, indent=0.0):
    width = width if width is not None else max(20.0, len(text) * size * 0.5)
    return LaidOutLine(page=page, text=text, bbox=Rect(x, y, x + width, y + size),
                       size=size, indent=indent, order=0)


def build(lines, body_size=10.0, spacing=11.0):
    """Run the block builder over one page of synthetic lines."""
    from ilatool.layout import PageLayout
    page = PageLayout(number=1, width=595.0, height=842.0, lines=list(lines))
    page.body_box = Rect(100.0, 0.0, 500.0, 800.0)
    for order, item in enumerate(page.lines):
        item.order = order
    options = structure._Options(split_recitals=False, drop_front_matter=False)
    options.hyphenated_terms = set()
    doc = structure.build_blocks([page], body_size, options, Diagnostics())
    return [b for b in doc.blocks]


class StructuralHeadingTests(unittest.TestCase):
    def test_short_article_labels_are_structural_headings(self):
        for text in ("Article 1", "Article 12", "PART II", "Chapter 3",
                     "Section 4.2", "ANNEX I"):
            self.assertTrue(structure.looks_like_structural_heading(text), text)

    def test_a_sentence_beginning_with_article_is_not(self):
        self.assertFalse(structure.looks_like_structural_heading(
            "Article 5 provides that the seller must deliver the goods within "
            "thirty days of the conclusion of the contract."))
        self.assertFalse(structure.looks_like_structural_heading(
            "Article 5,"))
        self.assertFalse(structure.looks_like_structural_heading("Ordinary text"))

    def test_a_heading_never_absorbs_the_body_text_after_it(self):
        # The CISG sets "Article 12" on its own line and the article text on
        # the next at the same indent -- which every other rule reads as a
        # continuation.  The heading must stay separate.
        lines = [
            line("Article 12", 100.0),
            line("Any provision of article 11 that allows a contract of sale to "
                 "be made in any form other than in writing does not apply.",
                 112.0),
        ]
        blocks = build(lines)
        self.assertEqual([b.kind for b in blocks], ["heading", "para"])
        self.assertEqual(blocks[0].text, "Article 12")
        self.assertTrue(blocks[1].text.startswith("Any provision"))

    def test_a_heading_is_never_a_single_trimmed_line_by_accident(self):
        lines = [line("Article 3", 100.0), line("Short body line.", 112.0)]
        blocks = build(lines)
        self.assertEqual(blocks[0].kind, "heading")
        self.assertEqual(blocks[1].kind, "para")


class QuotationTests(unittest.TestCase):
    def test_numbered_sub_items_inside_a_quotation_stay_in_one_paragraph(self):
        lines = [
            line("2. At the end of its Application, Nicaragua", 100.0),
            line("“respectfully requests the Court to declare that Germany:", 112.0),
            line("(1) has breached its obligations under the Convention", 124.0),
            line("by providing aid to Israel;", 136.0, indent=18.0),
            line("(2) has breached its obligations by withdrawing funding.”", 148.0),
            line("6. The Deputy-Registrar communicated the Application.", 160.0),
        ]
        blocks = build(lines)
        self.assertEqual(len(blocks), 2, [b.text[:40] for b in blocks])
        self.assertIn("(1)", blocks[0].text)
        self.assertIn("(2)", blocks[0].text)
        # The clause number becomes the paragraph's label, not part of its text.
        self.assertEqual(blocks[1].label, "6.")
        self.assertTrue(blocks[1].text.startswith("The Deputy-Registrar"))

    def test_sub_items_outside_a_quotation_are_separate_paragraphs(self):
        lines = [
            line("This Convention does not apply to sales:", 100.0),
            line("(a) of goods bought for personal use;", 112.0),
            line("(b) by auction;", 124.0),
        ]
        blocks = build(lines)
        self.assertEqual(len(blocks), 3, [b.text[:30] for b in blocks])

    def test_balance_counts_curly_quotes_only(self):
        self.assertEqual(structure.quote_balance("“open"), 1)
        self.assertEqual(structure.quote_balance("“open” closed"), 0)
        self.assertEqual(structure.quote_balance('"straight"'), 0)


class LeadInTests(unittest.TestCase):
    def test_a_line_opening_a_quotation_continues_its_lead_in(self):
        lines = [
            line("The measures are as follows:", 100.0),
            line("“(1) Germany shall immediately suspend its aid to Israel.", 122.0),
            line("(2) Germany shall resume its funding of UNRWA.", 134.0),
        ]
        blocks = build(lines)
        self.assertEqual(len(blocks), 1, [b.text[:40] for b in blocks])

    def test_a_colon_does_not_make_the_next_line_a_paragraph(self):
        lines = [
            line("The following representatives were present:", 100.0),
            line("On behalf of Nicaragua: HE Mr Argüello Gómez,", 122.0),
            line("Mr Daniel Müller,", 134.0, x=260.0, indent=120.0),
            line("On behalf of Germany: Ms von Uslar-Gleichen,", 146.0),
        ]
        blocks = build(lines)
        self.assertEqual(len(blocks), 1, [b.text[:40] for b in blocks])


class ClauseMarkerTests(unittest.TestCase):
    def test_a_clause_number_keeps_its_own_text(self):
        lines = [
            line("1. A treaty shall be interpreted in good faith and in accordance", 100.0),
            line("with the ordinary meaning to be given to its terms.", 116.0),
            line("2. A subsequent clause.", 132.0),
        ]
        blocks = build(lines)
        self.assertEqual(len(blocks), 2, [b.text[:40] for b in blocks])
        self.assertEqual(blocks[0].label, "1.")
        self.assertIn("ordinary meaning", blocks[0].text)

    def test_extra_leading_after_a_marker_is_not_a_break(self):
        # Court orders set the number on its own line with more space after it.
        lines = [
            line("2. At the end of its Application, Nicaragua", 100.0),
            line("respectfully requests the Court to declare that Germany", 116.0),
            line("has breached its obligations under the Convention.", 127.0),
        ]
        blocks = build(lines)
        self.assertEqual(len(blocks), 1, [b.text[:40] for b in blocks])


class HyphenationTests(unittest.TestCase):
    def test_a_line_break_hyphen_is_removed(self):
        self.assertEqual(textutil.join_lines("inter-", "national law"),
                         "international law")

    def test_a_real_compound_keeps_its_hyphen(self):
        known = {"self-determination"}
        self.assertEqual(
            textutil.join_lines("the right to self-", "determination of peoples",
                                known),
            "the right to self-determination of peoples")

    def test_hyphenated_join_builds_the_compound_name(self):
        # This is the *lookup key* for the known-compounds set, so it keeps
        # the hyphen either way.
        self.assertEqual(textutil.hyphenated_join("self-", "determination"),
                         "self-determination")
        self.assertEqual(textutil.hyphenated_join("inter-", "national"),
                         "inter-national")


class ParagraphIdTests(unittest.TestCase):
    def test_ids_are_content_addressed_and_stable(self):
        first = build([line("A paragraph of text about treaties.", 100.0)])
        second = build([line("A paragraph of text about treaties.", 140.0)])
        self.assertEqual(first[0].id, second[0].id)

    def test_a_heading_and_a_paragraph_never_share_an_id(self):
        blocks = build([line("Article 7", 100.0),
                        line("Article 7", 112.0),
                        line("The seventh article sets out the rule.", 124.0)])
        self.assertEqual(blocks[0].kind, "heading")
        self.assertEqual(blocks[1].kind, "heading")
        self.assertNotEqual(blocks[0].id, blocks[1].id)
        self.assertEqual(blocks[2].kind, "para")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class GeneralityTests(unittest.TestCase):
    """The rules must work on documents that are not court orders.

    Every heuristic here was derived from a legal instrument, so these use an
    ordinary leaflet: a short document with a contents list, numbered steps
    and a quoted specification.
    """

    @classmethod
    def setUpClass(cls):
        from tests.helpers import fixture, has_fixture
        if not has_fixture("generic"):
            raise unittest.SkipTest("the generic fixture is missing")
        from ilatool import backends, layout
        from ilatool.errors import Diagnostics as D
        doc = backends.load_pdf(fixture("generic"))
        laid_out = layout.analyse_document(doc, D())
        options = structure._Options(drop_front_matter=True)
        options.hyphenated_terms = laid_out.hyphenated_terms
        cls.document = structure.build_blocks(laid_out.pages, laid_out.body_size,
                                              options, D())
        cls.laid_out = laid_out

    def test_a_short_document_is_not_deleted_as_back_matter(self):
        # The contents list sits on page 1 of a two-page leaflet.  Treating it
        # as a trailer used to remove the entire body of the document.
        texts = [b.text for b in self.document.blocks]
        self.assertTrue(any("three-ply stainless steel" in t for t in texts),
                        f"the body was dropped: {texts}")

    def test_numbered_steps_become_separate_labelled_paragraphs(self):
        labelled = [b for b in self.document.paragraphs if b.label in ("1.", "2.", "3.")]
        self.assertEqual(len(labelled), 3, [b.label for b in self.document.paragraphs])
        self.assertIn("three-ply", labelled[0].text)
        self.assertIn("Skillets", labelled[1].text)

    def test_the_quoted_specification_is_its_own_paragraph(self):
        texts = [b.text for b in self.document.paragraphs]
        quoted = [t for t in texts if t.startswith("The manufacturer states")]
        self.assertEqual(len(quoted), 1, texts[-3:])
        self.assertIn("proof of purchase", quoted[0])

    def test_no_runaway_paragraph(self):
        lengths = [len(b.text) for b in self.document.paragraphs]
        self.assertLess(max(lengths), 1200)

    def test_the_running_header_and_footer_are_gone(self):
        joined = " ".join(b.text for b in self.document.blocks)
        self.assertNotIn("Page 2 of 2", joined)
        self.assertNotIn("Autumn Catalogue 2024 - Kitchen Tools", joined)
