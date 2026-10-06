"""Rendering, validation and the end-to-end pipeline."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.helpers import build_docx, fixture, has_fixture

from ilatool import pipeline, render, sourcefmt, structure, validate

SOURCE_TEXT = """TITLE: A Treaty
DESC: An annotated treaty
HEADER: Treaty
HOME: ../index.html
CLOSING: Done at Example.
---
## Article 1
¶ The first paragraph states the rule and it is <em>escaped</em> & safe.
¶ The second paragraph uses "quotes" and an ampersand & a < bracket.
{kkey2} ¶ A third paragraph with an explicit key.
"""


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.source = sourcefmt.parse_source_text(SOURCE_TEXT)

    def test_paragraph_text_is_escaped(self):
        body = render.render_body(self.source.blocks)
        self.assertNotIn("<em>", body)
        self.assertIn("&lt;em&gt;", body)
        self.assertIn("&amp;", body)
        self.assertIn("&lt; bracket", body)

    def test_every_paragraph_gets_an_element_and_a_marker(self):
        body = render.render_body(self.source.blocks)
        for block in self.source.paragraphs:
            self.assertIn(f'id="{block.id}"', body)
            self.assertIn(f'data-para="{block.id}"', body)

    def test_meta_is_escaped_in_attributes(self):
        page = render.build_page(
            {"TITLE": 'A "quoted" title', "DESC": 'desc with "quotes"',
             "HEADER": "H", "HOME": 'x" onmouseover="alert(1)'},
            "", "notes.js")
        self.assertNotIn('content="desc with "quotes""', page)
        self.assertIn("&quot;", page)

    def test_notes_js_is_stable_and_loadable(self):
        notes = {"para-a": [{"author": "A", "title": "", "text": "x", "source": ""}]}
        first = render.render_notes_js(notes)
        second = render.render_notes_js(notes)
        self.assertEqual(first, second)
        from ilatool import annotations as ann_mod
        self.assertEqual(ann_mod.parse_notes_js(first)["para-a"][0]["text"], "x")

    def test_empty_note_fields_are_dropped(self):
        notes = {"para-a": [{"author": "", "title": "", "text": "x", "source": ""}]}
        from ilatool import annotations as ann_mod
        payload = render.render_notes_js(notes)
        parsed = ann_mod.parse_notes_js(payload)
        self.assertEqual(list(parsed["para-a"][0].keys()), ["text"])


class ValidationTests(unittest.TestCase):
    def _write(self, tmp, page_html, notes_js):
        html_path = Path(tmp) / "t.html"
        notes_path = Path(tmp) / "t-notes.js"
        html_path.write_text(page_html, encoding="utf-8")
        notes_path.write_text(notes_js, encoding="utf-8")
        return html_path, notes_path

    def test_a_good_build_passes(self):
        source = sourcefmt.parse_source_text(SOURCE_TEXT)
        notes = {source.paragraphs[0].id: [{"author": "A", "text": "note"}]}
        page = render.render_page(source.meta, source.blocks, notes, "t-notes.js")
        with TemporaryDirectory() as tmp:
            html_path, notes_path = self._write(tmp, page.html, page.notes_js)
            report = validate.validate_site(html_path, notes_path, source, notes)
            self.assertTrue(report.ok, report.summary())

    def test_a_missing_paragraph_is_a_failure(self):
        source = sourcefmt.parse_source_text(SOURCE_TEXT)
        page = render.render_page(source.meta, source.blocks, {}, "t-notes.js")
        html = page.html.replace(f'id="{source.paragraphs[0].id}"', 'id="gone"')
        with TemporaryDirectory() as tmp:
            html_path, notes_path = self._write(tmp, html, page.notes_js)
            report = validate.validate_site(html_path, notes_path, source, {})
            self.assertFalse(report.ok)
            self.assertTrue(any("missing" in c.detail for c in report.failures))

    def test_an_orphan_note_is_a_failure(self):
        source = sourcefmt.parse_source_text(SOURCE_TEXT)
        notes = {"para-not-there": [{"author": "A", "text": "note"}]}
        page = render.render_page(source.meta, source.blocks, notes, "t-notes.js")
        with TemporaryDirectory() as tmp:
            html_path, notes_path = self._write(tmp, page.html, page.notes_js)
            report = validate.validate_site(html_path, notes_path, source, notes)
            self.assertFalse(report.ok)
            self.assertTrue(any("note" in c.name for c in report.failures))

    def test_a_page_pointing_at_the_wrong_notes_file_is_a_failure(self):
        source = sourcefmt.parse_source_text(SOURCE_TEXT)
        page = render.render_page(source.meta, source.blocks, {}, "t-notes.js")
        with TemporaryDirectory() as tmp:
            html_path, notes_path = self._write(
                tmp, page.html.replace("t-notes.js", "other-notes.js"), page.notes_js)
            report = validate.validate_site(html_path, notes_path, source, {})
            self.assertFalse(report.ok)


class PipelineTests(unittest.TestCase):
    def _source(self, tmp: Path) -> Path:
        path = Path(tmp) / "treaty.txt"
        path.write_text(SOURCE_TEXT, encoding="utf-8")
        return path

    def test_build_from_source_and_json_annotations(self):
        with TemporaryDirectory() as tmp:
            source_path = self._source(tmp)
            source = sourcefmt.parse_source(path=source_path)
            ann_path = Path(tmp) / "notes.json"
            ann_path.write_text(json.dumps([
                {"quote": "The second paragraph uses", "author": "A. One",
                 "text": "A quoted note."},
            ]), encoding="utf-8")
            out_html = Path(tmp) / "treaty.html"
            report = pipeline.run(pipeline.RunRequest(
                source=source_path, annotations=ann_path, out_html=out_html,
                meta=source.meta))
            self.assertTrue(report.ok, report.error)
            self.assertEqual(report.result.stats["notes"], 1)
            self.assertTrue(out_html.exists())
            self.assertTrue(out_html.with_name("treaty-notes.js").exists())
            self.assertTrue(report.diagnostics_path.exists())

    def test_missing_annotation_target_is_reported_not_hidden(self):
        with TemporaryDirectory() as tmp:
            source_path = self._source(tmp)
            ann_path = Path(tmp) / "notes.json"
            ann_path.write_text(json.dumps([
                {"para": "para-nope", "text": "nowhere to go"}]), encoding="utf-8")
            report = pipeline.run(pipeline.RunRequest(
                source=source_path, annotations=ann_path,
                out_html=Path(tmp) / "t.html"))
            self.assertEqual(report.result.stats["notes"], 0)
            self.assertEqual(len(report.result.match.unplaced), 1)
            self.assertTrue(any("could not be attached" in d.message
                                for d in report.diagnostics))

    def test_a_broken_source_fails_with_a_useful_error(self):
        with TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.txt"
            bad.write_text("TITLE: x\nno delimiter here\n", encoding="utf-8")
            report = pipeline.run(pipeline.RunRequest(source=bad))
            self.assertFalse(report.ok)
            self.assertIsNotNone(report.error)
            self.assertIn("delimiter", report.error.message + report.error.hint)

    def test_a_missing_file_fails_cleanly(self):
        report = pipeline.run(pipeline.RunRequest(source=Path("/nope/missing.pdf")))
        self.assertFalse(report.ok)
        self.assertIsNotNone(report.error)

    def test_docx_comments_end_to_end(self):
        with TemporaryDirectory() as tmp:
            source_path = self._source(tmp)
            docx_path = Path(tmp) / "instrument.docx"
            build_docx(
                docx_path,
                paragraphs=["The second paragraph uses \"quotes\" and an ampersand "
                            "& a < bracket."],
                comments=[("Dr Smith", "A comment from Word", 0,
                           "quotes\" and an ampersand")],
            )
            out_html = Path(tmp) / "treaty.html"
            report = pipeline.run(pipeline.RunRequest(
                source=source_path, annotations=docx_path, out_html=out_html))
            self.assertTrue(report.ok, report.error)
            self.assertEqual(report.result.stats["notes"], 1)

    @unittest.skipUnless(has_fixture("annotated"), "fixtures missing")
    def test_pdf_markup_annotations_end_to_end(self):
        with TemporaryDirectory() as tmp:
            source_path = Path(tmp) / "annotated.txt"
            convert = pipeline.convert_pdf(
                fixture("annotated"), {"TITLE": "Annotated"}, pipeline.ConvertOptions())
            pipeline.write_source(convert, source_path, write_json=True)
            self.assertTrue(source_path.exists())
            self.assertTrue(source_path.with_suffix(".layout.json").exists())

            out_html = Path(tmp) / "annotated.html"
            report = pipeline.run(pipeline.RunRequest(
                source=fixture("annotated"), annotations=fixture("annotated"),
                out_source=source_path, out_html=out_html,
                meta={"TITLE": "Annotated"}))
            self.assertTrue(report.ok, report.error)
            stats = report.result.stats
            self.assertGreaterEqual(stats["paragraphs"], 6)
            self.assertGreaterEqual(stats["notes"], 3, "annotations should attach")

    @unittest.skipUnless(has_fixture("twocolumn"), "fixtures missing")
    def test_two_column_pdf_reads_correctly_end_to_end(self):
        with TemporaryDirectory() as tmp:
            out_html = Path(tmp) / "two.html"
            report = pipeline.run(pipeline.RunRequest(
                source=fixture("twocolumn"), out_source=Path(tmp) / "two.txt",
                out_html=out_html, meta={"TITLE": "Two"}))
            self.assertTrue(report.ok, report.error)
            texts = [b.text for b in report.result.source.paragraphs]
            left = [i for i, t in enumerate(texts) if t.startswith("LEFT")]
            right = [i for i, t in enumerate(texts) if t.startswith("RIGHT")]
            self.assertTrue(left and right)
            self.assertLess(max(left), min(right))

    def test_ligature_artifacts_never_reach_the_page(self):
        # The broken-font artifact is repaired during extraction; this checks
        # the whole path from extracted text to the written HTML.
        from collections import Counter
        from ilatool import textutil
        stats = Counter()
        repaired = textutil.repair_text('di"erent and e"ect', stats)
        self.assertEqual(repaired, "different and effect")
        body = render.render_body([structure.Block(kind="para", text=repaired, key="k")])
        self.assertNotIn('"erent', body)
        self.assertIn("different", body)


@unittest.skipUnless(has_fixture("headers"), "fixtures missing")
class RealDocumentTests(unittest.TestCase):
    def test_headers_document_keeps_only_the_body(self):
        with TemporaryDirectory() as tmp:
            report = pipeline.run(pipeline.RunRequest(
                source=fixture("headers"), out_source=Path(tmp) / "h.txt",
                out_html=Path(tmp) / "h.html", meta={"TITLE": "Headers"}))
            self.assertTrue(report.ok, report.error)
            joined = " ".join(b.text for b in report.result.source.paragraphs)
            self.assertIn("The development of international law", joined)
            self.assertNotIn("International Law Review", joined)
            self.assertNotIn("Page 2 of 4", joined)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
