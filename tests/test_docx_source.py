"""The Word document path: body text, heading styles, and paragraph joins.

Five of this project's instruments exist only as ``.docx``, so the Word file
is the text source.  Word spreads one logical paragraph over several ``w:p``
elements, and taking them at face value produces paragraphs that begin
mid-sentence -- which is the defect these tests pin down.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.helpers import build_docx_styled

from ilatool import docx, pipeline, sourcefmt


def convert(case, paragraphs, meta=None):
    """Convert a synthetic document, cleaning up when the test finishes."""
    tmp = TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    base = Path(tmp.name)
    path = build_docx_styled(base / "instrument.docx", paragraphs)
    report = pipeline.run(pipeline.RunRequest(
        source=path, out_html=base / "out.html",
        out_notes=base / "out-notes.js", out_source=base / "out.txt",
        meta=meta or {"TITLE": "Test"}))
    return report


class ParagraphReaderTests(unittest.TestCase):
    def test_headings_come_from_word_styles(self):
        with TemporaryDirectory() as tmp:
            path = build_docx_styled(Path(tmp) / "d.docx", [
                {"text": "Article 1", "style": "Heading1"},
                {"text": "The first article says something.", "style": "BodyText"},
                {"text": "Definitions", "style": "Heading2"},
            ])
            paragraphs = docx.read_docx_paragraphs(path)
            self.assertEqual([p.heading_level for p in paragraphs], [1, 0, 2])
            self.assertEqual(paragraphs[0].text, "Article 1")

    def test_line_breaks_split_a_single_paragraph_into_lines(self):
        with TemporaryDirectory() as tmp:
            path = build_docx_styled(Path(tmp) / "d.docx", [
                {"text": "first line"},
                {"text": "second line", "break_before": True},
                {"text": "third line", "break_before": True},
            ])
            paragraphs = docx.read_docx_paragraphs(path)
            # Empty pieces are kept deliberately: they are how these files
            # mark a paragraph boundary, and the joiner needs to see them.
            self.assertEqual([p.text for p in paragraphs if p.text],
                             ["first line", "second line", "third line"])


class ContinuationTests(unittest.TestCase):
    def test_a_paragraph_split_mid_sentence_is_joined(self):
        report = convert(self, [
            {"text": "The Conference of the Parties serving as the meeting", "style": "ListParagraph"},
            {"text": "of the Parties shall keep the matter under review.", "style": "BodyText"},
        ])
        paragraphs = report.result.source.paragraphs
        self.assertEqual(len(paragraphs), 1, [b.text for b in paragraphs])
        self.assertIn("meeting of the Parties shall keep", paragraphs[0].text)

    def test_a_finished_sentence_is_not_joined(self):
        report = convert(self, [
            {"text": "The first paragraph stands alone.", "style": "BodyText"},
            {"text": "The second paragraph also stands alone.", "style": "BodyText"},
        ])
        self.assertEqual(len(report.result.source.paragraphs), 2)

    def test_a_recital_ending_in_a_comma_is_not_joined(self):
        # Preamble recitals end with a comma and the next begins with a
        # capital; joining them fuses the whole preamble into one blob.
        report = convert(self, [
            {"text": "The Parties to this Convention,", "style": "BodyText"},
            {"text": "Acknowledging that change in the climate is a concern,", "style": "BodyText"},
            {"text": "Have agreed as follows:", "style": "BodyText"},
        ])
        self.assertEqual(len(report.result.source.paragraphs), 3,
                         [b.text for b in report.result.source.paragraphs])

    def test_a_blank_line_does_not_break_a_mid_sentence_continuation(self):
        report = convert(self, [
            {"text": "The obligations of Parties are set out in the", "style": "BodyText"},
            {"text": "  ", "style": "BodyText"},
            {"text": "following provisions of this Article.", "style": "BodyText"},
        ])
        paragraphs = report.result.source.paragraphs
        self.assertEqual(len(paragraphs), 1, [b.text for b in paragraphs])

    def test_a_hyphenated_word_is_rejoined(self):
        report = convert(self, [
            {"text": "The obligation is inter-", "style": "BodyText"},
            {"text": "national in character.", "style": "BodyText"},
        ])
        self.assertIn("international in character",
                      report.result.source.paragraphs[0].text)

    def test_a_clause_marker_becomes_the_label_not_the_text(self):
        report = convert(self, [
            {"text": "(a) of goods bought for personal use;", "style": "ListParagraph"},
            {"text": "(b) by auction;", "style": "ListParagraph"},
        ])
        paragraphs = report.result.source.paragraphs
        self.assertEqual([b.label for b in paragraphs], ["(a)", "(b)"])
        self.assertTrue(paragraphs[0].text.startswith("of goods bought"))


class BuildTests(unittest.TestCase):
    def test_the_generated_source_round_trips(self):
        report = convert(self, [
            {"text": "Article 1", "style": "Heading1"},
            {"text": "A paragraph of body text.", "style": "BodyText"},
        ])
        source = sourcefmt.parse_source(Path(report.source_path))
        self.assertEqual([b.kind for b in source.blocks], ["heading", "para"])

    def test_a_document_with_no_comments_still_builds(self):
        report = convert(self, [{"text": "Body text with no comments at all.",
                           "style": "BodyText"}])
        self.assertTrue(report.ok, report.error)
        self.assertEqual(report.result.stats["notes"], 0)

    def test_an_empty_document_is_reported_clearly(self):
        report = convert(self, [{"text": "", "style": "BodyText"}])
        self.assertFalse(report.ok)
        self.assertIn("no paragraphs", report.error.message.lower())

    def test_heading_styles_are_used_even_without_structural_keywords(self):
        report = convert(self, [
            {"text": "Introduction", "style": "Heading1"},
            {"text": "Ordinary body text follows here, at some length so that "
                     "it is clearly a paragraph.", "style": "BodyText"},
        ])
        self.assertEqual(report.result.stats["headings"], 1)
        self.assertEqual(report.conversion.stats["styled_headings"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
