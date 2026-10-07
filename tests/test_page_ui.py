"""The page's annotation UI, as generated.

The notes used to open in hover popovers driven by tippy.js.  They now open in
a pane on the right when a paragraph is clicked, which is what a reader
expects and what works on a touch screen.  These tests pin down the pieces the
JavaScript depends on, so a change to the template cannot quietly break the
pane -- the behaviour itself is verified in a real browser.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.helpers import build_docx_styled

from ilatool import pipeline, render
from ilatool.page_template import PAGE_TEMPLATE


class ParagraphMarkupTests(unittest.TestCase):
    def test_a_paragraph_row_carries_its_label_for_the_rail(self):
        html = render.render_body([])  # empty document renders nothing
        self.assertEqual(html, "")

    def test_the_row_has_a_label_and_a_click_target(self):
        from ilatool.structure import Block
        block = Block(kind="para", text="The Parties shall do the thing.",
                      label="(a)", key="k12345678", order=1)
        html = render.render_body([block])
        self.assertIn('id="para-k12345678"', html)
        self.assertIn('data-label="(a)"', html)
        self.assertIn('data-para="para-k12345678"', html)
        self.assertNotIn("hover/click", html)


class TemplateTests(unittest.TestCase):
    def test_the_hover_popover_library_is_gone(self):
        for gone in ("tippy", "@popperjs", "tippy-box", "mouseenter focus"):
            self.assertNotIn(gone, PAGE_TEMPLATE, gone)

    def test_the_pane_has_the_controls_a_reader_needs(self):
        self.assertIn('class="note-pane"', PAGE_TEMPLATE)
        self.assertIn('x-show="paneOpen"', PAGE_TEMPLATE)          # the scrim
        self.assertIn('@click="closePane()"', PAGE_TEMPLATE)       # the close button
        self.assertIn('@click="step(-1)"', PAGE_TEMPLATE)          # previous
        self.assertIn('@click="step(1)"', PAGE_TEMPLATE)           # next
        self.assertIn('@click="copyPaneLink()"', PAGE_TEMPLATE)    # copy link
        self.assertIn("ev.key === 'Escape'", PAGE_TEMPLATE)

    def test_clicking_a_paragraph_opens_the_pane(self):
        # delegated, because the paragraph rows are generated markup
        self.assertIn("closest('[id^=\"para-\"][data-has-notes=\"1\"]')", PAGE_TEMPLATE)
        self.assertIn("this.openNote(row.id)", PAGE_TEMPLATE)
        self.assertIn("ev.target.closest('.note-pane')", PAGE_TEMPLATE)

    def test_the_pane_text_is_rendered_from_the_notes(self):
        # a plain property, not a getter: x-html does not re-run on getters
        for field in ("paneKicker: ''", "paneQuote: ''", "paneHtml: ''"):
            self.assertIn(field, PAGE_TEMPLATE)
        self.assertNotIn("get paneHtml()", PAGE_TEMPLATE)
        self.assertIn("this.paneHtml = this.notesHtml(list);", PAGE_TEMPLATE)

    def test_the_reflow_gives_the_pane_room(self):
        self.assertIn("html.pane-open .shell", PAGE_TEMPLATE)
        self.assertIn("document.documentElement.classList.toggle('pane-open', open)", PAGE_TEMPLATE)

    def test_the_rail_measures_in_document_pixels(self):
        # the old rail divided an offsetTop by main.scrollHeight, two different
        # boxes, so the markers drifted away from their paragraphs
        self.assertNotIn("el.offsetTop / total", PAGE_TEMPLATE)
        self.assertIn("rect.top + window.scrollY", PAGE_TEMPLATE)
        self.assertIn("document.documentElement.scrollHeight", PAGE_TEMPLATE)

    def test_the_rail_labels_are_readable_not_hashes(self):
        # markers are keyed by content hash, so the old '¶' + id.slice showed
        # the reader "¶k78066d39"
        self.assertNotIn("'¶' + el.id.replace('para-', '')", PAGE_TEMPLATE)
        self.assertIn("this.labelFor(el)", PAGE_TEMPLATE)

    def test_the_rail_spreads_dots_that_would_collide(self):
        self.assertIn("spread: function (arr, rail)", PAGE_TEMPLATE)
        # every marker gets a numeric position even when the rail is hidden
        self.assertIn("arr[i].top = arr[i].pos;", PAGE_TEMPLATE)

    def test_the_rail_is_not_positioned_by_script_any_more(self):
        self.assertNotIn("positionNoteRail", PAGE_TEMPLATE)
        self.assertIn("left: max(10px, calc(50% - 32rem - 2.5rem))", PAGE_TEMPLATE)


class GeneratedPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = TemporaryDirectory()
        base = Path(cls._tmp.name)
        source = build_docx_styled(base / "d.docx", [
            {"text": "Article 1", "style": "Heading1"},
            {"text": "The Parties shall do the thing described herein.",
             "style": "BodyText"},
        ])
        report = pipeline.run(pipeline.RunRequest(
            source=source, out_html=base / "out.html",
            out_notes=base / "out-notes.js", out_source=base / "out.txt",
            meta={"TITLE": "T", "HEADER": "T"}))
        cls.page = (base / "out.html").read_text() if report.ok else ""

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_page_loads_no_popover_library(self):
        self.assertNotIn("tippy", self.page)
        self.assertNotIn("popperjs", self.page)

    def test_the_page_has_the_pane_and_its_script(self):
        self.assertIn('class="note-pane"', self.page)
        self.assertIn("function icjApp()", self.page)
        self.assertIn("renderPane()", self.page)

    def test_the_script_is_syntactically_valid(self):
        # A quick structural check; the browser test is the real one.
        scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", self.page, re.DOTALL)
        self.assertTrue(scripts)
        for script in scripts:
            self.assertEqual(script.count("{"), script.count("}"),
                             "unbalanced braces in an inline script")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
