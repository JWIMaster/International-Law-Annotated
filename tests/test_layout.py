"""Text repair and page-layout tests."""

from __future__ import annotations

import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.helpers import fixture, has_fixture

from ilatool import backends, layout, textutil
from ilatool.errors import Diagnostics
from ilatool.layout import LaidOutLine
from ilatool.pdfmodel import Rect


class TextRepairTests(unittest.TestCase):
    def test_ligature_codepoints_are_expanded(self):
        stats = Counter()
        out = textutil.repair_text("bene\ufb01t of \ufb01ne \ufb02our", stats)
        self.assertEqual(out, "benefit of fine flour")
        self.assertEqual(stats["ligature"], 3)

    def test_intra_word_quote_becomes_ff(self):
        # The macOS/browser print artifact: the ff ligature extracts as '"'.
        stats = Counter()
        self.assertEqual(textutil.repair_text('di"erent e"ect', stats), "different effect")
        self.assertEqual(stats["ligature"], 2)

    def test_real_quotes_are_left_alone(self):
        text = 'He said "different" twice, and 5"6\' tall.'
        self.assertEqual(textutil.repair_text(text), text)

    def test_odd_spaces_are_normalised(self):
        out = textutil.repair_text("Article\u202f1\u00a0applies\u200b.")
        self.assertEqual(out, "Article 1 applies.")

    def test_normalize_folds_the_same_artifact(self):
        # The old (broken) extraction and the repaired text must normalise
        # identically, or re-conversion would orphan every annotation.
        self.assertEqual(textutil.normalize_for_match('di"erent'),
                         textutil.normalize_for_match("different"))

    def test_join_lines_undoes_hyphenation(self):
        self.assertEqual(textutil.join_lines("inter-", "national law"),
                         "international law")
        self.assertEqual(textutil.join_lines("the end.", "Next sentence."),
                         "the end. Next sentence.")
        # A capital after the hyphen is a real compound, not hyphenation.
        self.assertEqual(textutil.join_lines("Anglo-", "American"), "Anglo- American")

    def test_page_number_and_toc_detection(self):
        for text in ("12", "- 7 -", "Page 3 of 12", "iv"):
            self.assertTrue(textutil.looks_like_page_number(text), text)
        self.assertFalse(textutil.looks_like_page_number("Article 12"))
        self.assertTrue(textutil.looks_like_toc_entry("Article 5 .......... 12"))
        self.assertTrue(textutil.looks_like_toc_entry("Final provisions   44"))
        # "Article 36" is a heading: a single space must not make it a
        # contents entry, or the tool would delete real headings.
        self.assertFalse(textutil.looks_like_toc_entry("Article 36"))
        self.assertFalse(textutil.looks_like_toc_entry("Article 5"))

    def test_browser_furniture(self):
        self.assertTrue(textutil.looks_like_browser_furniture("4/10/2025, 2:31 pm"))
        self.assertTrue(textutil.looks_like_browser_furniture("https://example.org/x"))
        self.assertFalse(textutil.looks_like_browser_furniture("See https://x.org for more"))


class GeometryTests(unittest.TestCase):
    def test_rect_relationships(self):
        a = Rect(0, 0, 10, 10)
        b = Rect(5, 5, 15, 15)
        self.assertAlmostEqual(a.overlap_area(b), 25.0)
        self.assertAlmostEqual(a.overlap_fraction(b), 0.25)
        self.assertEqual(a.union(b), Rect(0, 0, 15, 15))
        self.assertTrue(a.contains_rect(Rect(2, 2, 3, 3)))

    def test_rotation_round_trip(self):
        for rotation in (0, 90, 180, 270):
            display = backends.page_rect_to_display((10, 20, 30, 40), 100, 200, rotation)
            back = backends.display_rect_to_page(display.as_tuple(), 100, 200, rotation)
            self.assertAlmostEqual(back.x0, 10, places=6)
            self.assertAlmostEqual(back.y0, 20, places=6)
            self.assertAlmostEqual(back.x1, 30, places=6)
            self.assertAlmostEqual(back.y1, 40, places=6)

    def test_display_size_swaps_on_quarter_turns(self):
        self.assertEqual(backends.display_size(100, 200, 90), (200, 100))
        self.assertEqual(backends.display_size(100, 200, 0), (100, 200))


def _analyse(name):
    doc = backends.load_pdf(fixture(name))
    return doc, layout.analyse_document(doc, Diagnostics())


@unittest.skipUnless(has_fixture("twocolumn"), "fixtures missing")
class ColumnTests(unittest.TestCase):
    def test_two_column_page_is_detected(self):
        _, laid_out = _analyse("twocolumn")
        page = laid_out.pages[0]
        self.assertEqual(page.columns, 2)

    def test_reading_order_runs_down_each_column(self):
        _, laid_out = _analyse("twocolumn")
        texts = [l.text for l in laid_out.pages[0].lines]
        left = [i for i, t in enumerate(texts) if t.startswith("LEFT")]
        right = [i for i, t in enumerate(texts) if t.startswith("RIGHT")]
        self.assertTrue(left and right)
        self.assertLess(max(left), min(right),
                        "every left-column line must come before the right column")

    def test_single_column_page_is_not_split(self):
        _, laid_out = _analyse("simple")
        self.assertEqual(laid_out.pages[0].columns, 1)


@unittest.skipUnless(has_fixture("headers"), "fixtures missing")
class RunningHeaderTests(unittest.TestCase):
    def test_repeating_header_and_footer_are_removed(self):
        doc, laid_out = _analyse("headers")
        kept = [l.text for page in laid_out.pages for l in page.lines]
        self.assertNotIn("International Law Review", kept)
        self.assertFalse([t for t in kept if t.startswith("Page ")])
        self.assertIn("International Law Review", laid_out.furniture)

    def test_body_text_survives(self):
        _, laid_out = _analyse("headers")
        joined = " ".join(l.text for page in laid_out.pages for l in page.lines)
        self.assertIn("The development of international law", joined)
        self.assertIn("The result is a legal order", joined)

    def test_one_off_continuation_header_is_removed(self):
        _, laid_out = _analyse("paragraphs_paged")
        kept = [l.text for page in laid_out.pages for l in page.lines]
        self.assertNotIn("Article 9 (continued)", kept)

    def test_first_line_of_the_document_is_never_treated_as_furniture(self):
        _, laid_out = _analyse("simple")
        self.assertEqual(laid_out.pages[0].lines[0].text, "A Short Note on Treaties")


@unittest.skipUnless(has_fixture("rotated"), "fixtures missing")
class RotationTests(unittest.TestCase):
    def test_rotation_is_recorded_and_the_display_size_is_swapped(self):
        doc, laid_out = _analyse("rotated")
        self.assertEqual(doc.pages[0].rotation, 90)
        self.assertEqual(doc.pages[1].rotation, 270)
        # The reader sees a landscape page; the model keeps the unrotated
        # media box so that text stays horizontal and annotations line up.
        self.assertAlmostEqual(doc.pages[0].width, 841.89, delta=1.0)
        self.assertAlmostEqual(doc.pages[0].height, 595.28, delta=1.0)
        self.assertAlmostEqual(doc.pages[0].page_width, 595.28, delta=1.0)
        self.assertAlmostEqual(doc.pages[0].page_height, 841.89, delta=1.0)

    def test_text_and_annotations_share_one_coordinate_space(self):
        # The invariant that actually matters: whatever space the backend
        # reports in, the words and the marks over them agree.
        doc, laid_out = _analyse("rotated")
        for page_model, page in zip(doc.pages, laid_out.pages):
            for line in page.lines:
                self.assertTrue(
                    page_model.page_rect.expand(1.0).contains_rect(line.bbox),
                    f"{line.text!r} at {line.bbox} is outside the page {page_model.page_rect}")

    def test_annotation_lands_on_the_marked_text(self):
        doc, laid_out = _analyse("rotated")
        page = laid_out.pages[0]
        annots = doc.pages[0].annotations
        self.assertTrue(annots, "the fixture carries a highlight on page 1")
        rect = annots[0].rect
        covered = [l.text for l in page.lines if rect.overlap_fraction(l.bbox) > 0.3]
        self.assertTrue(covered, "the highlight should overlap the text it marks")
        self.assertIn("Rotated page one", " ".join(covered))


@unittest.skipUnless(has_fixture("image"), "fixtures missing")
class ImageTests(unittest.TestCase):
    def test_image_bboxes_are_recorded(self):
        doc, _ = _analyse("image")
        self.assertTrue(doc.pages[0].images)
        self.assertGreater(doc.pages[0].images[0].bbox.area, 0)

    def test_text_free_page_is_flagged_as_a_scan(self):
        doc = backends.load_pdf(fixture("image"))
        diag = Diagnostics()
        laid_out = layout.analyse_document(doc, diag)
        self.assertTrue(laid_out.pages[1].is_image_only)
        self.assertTrue(any("scan" in d.message for d in diag))


@unittest.skipUnless(has_fixture("vector"), "fixtures missing")
class VectorTests(unittest.TestCase):
    def test_vector_graphics_are_recorded_with_pymupdf(self):
        doc = backends.load_pdf(fixture("vector"))
        if doc.backend != "pymupdf":
            self.skipTest("drawing extraction needs the PyMuPDF backend")
        self.assertTrue(doc.pages[0].drawings)
        self.assertTrue(any(d.items for d in doc.pages[0].drawings))

    def test_caption_text_survives(self):
        _, laid_out = _analyse("vector")
        self.assertTrue(any("Figure 1" in l.text for l in laid_out.pages[0].lines))


@unittest.skipUnless(has_fixture("pagesizes"), "fixtures missing")
class PageSizeTests(unittest.TestCase):
    def test_mixed_page_sizes(self):
        doc = backends.load_pdf(fixture("pagesizes"))
        sizes = [(round(p.page_width), round(p.page_height)) for p in doc.pages]
        # poppler reports whole points, so allow a point of slack.
        for actual, expected in zip(sizes, [(595, 842), (612, 792), (842, 595)]):
            self.assertAlmostEqual(actual[0], expected[0], delta=2)
            self.assertAlmostEqual(actual[1], expected[1], delta=2)


@unittest.skipUnless(has_fixture("empty"), "fixtures missing")
class EmptyPageTests(unittest.TestCase):
    def test_empty_document_does_not_crash_the_layout_stage(self):
        doc = backends.load_pdf(fixture("empty"))
        diag = Diagnostics()
        laid_out = layout.analyse_document(doc, diag)
        self.assertEqual(len(laid_out.pages), 2)
        self.assertFalse(laid_out.pages[0].has_text)

    def test_minimal_document_keeps_its_one_character(self):
        doc = backends.load_pdf(fixture("minimal"))
        laid_out = layout.analyse_document(doc, Diagnostics())
        text = " ".join(l.text for l in laid_out.pages[0].lines)
        self.assertIn(".", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class BackendParityTests(unittest.TestCase):
    """The two PDF readers must agree about the important things.

    They are allowed to differ by a point or two on coordinates (poppler
    rounds), but the text, the page count, the rotations and the annotations
    have to line up, or switching backends would silently change the site.
    """

    @classmethod
    def setUpClass(cls):
        from ilatool import backends
        if not backends.backend_status()["pymupdf"]["available"]:
            raise unittest.SkipTest("PyMuPDF is not installed")

    def _both(self, name):
        from ilatool import backends
        path = fixture(name)
        return (backends.load_pdf(path, backend="pymupdf"),
                backends.load_pdf(path, backend="poppler"))

    def test_page_count_and_rotation_agree(self):
        for name in ("simple", "annotated", "rotated", "pagesizes", "headers"):
            if not has_fixture(name):
                continue
            with self.subTest(fixture=name):
                a, b = self._both(name)
                self.assertEqual(a.page_count, b.page_count)
                self.assertEqual([p.rotation for p in a.pages],
                                 [p.rotation for p in b.pages])

    def test_extracted_text_agrees(self):
        # Compared after layout, because the raw block order a backend hands
        # back is arbitrary -- deciding reading order is the layout stage's job.
        for name in ("simple", "paragraphs", "annotated", "twocolumn"):
            if not has_fixture(name):
                continue
            with self.subTest(fixture=name):
                a, b = self._both(name)
                la = layout.analyse_document(a, Diagnostics())
                lb = layout.analyse_document(b, Diagnostics())
                text_a = textutil.normalize_for_match(
                    " ".join(l.text for page in la.pages for l in page.lines))
                text_b = textutil.normalize_for_match(
                    " ".join(l.text for page in lb.pages for l in page.lines))
                self.assertEqual(text_a, text_b)

    def test_annotations_agree(self):
        if not has_fixture("annotated"):
            self.skipTest("fixture missing")
        a, b = self._both("annotated")
        def summarise(doc):
            return sorted((ann.page, ann.kind, ann.author, ann.contents)
                          for ann in doc.all_annotations())
        self.assertEqual(summarise(a), summarise(b))

    def test_annotation_geometry_agrees(self):
        # The two readers normalise a markup rectangle slightly differently
        # (PyMuPDF snaps to the glyph box, poppler uses the raw /Rect), so a
        # few points of slack is expected; a *large* difference would mean one
        # of them is in the wrong coordinate space.
        if not has_fixture("annotated"):
            self.skipTest("fixture missing")
        a, b = self._both("annotated")
        for page_a, page_b in zip(a.pages, b.pages):
            self.assertEqual(len(page_a.annotations), len(page_b.annotations))
            for ann_a, ann_b in zip(page_a.annotations, page_b.annotations):
                self.assertAlmostEqual(ann_a.rect.x0, ann_b.rect.x0, delta=6)
                self.assertAlmostEqual(ann_a.rect.y0, ann_b.rect.y0, delta=6)


@unittest.skipUnless(has_fixture("hiddentext"), "fixtures missing")
class HiddenTextTests(unittest.TestCase):
    """A searchable scan stores its OCR text invisibly (render mode 3).

    Dropping it turns a perfectly good document into "no text layer", which
    is exactly what happened to a real scanned judgment before the poppler
    backend asked pdftohtml for hidden text.
    """

    def test_both_backends_recover_invisible_text(self):
        doc = backends.load_pdf(fixture("hiddentext"))
        text = " ".join(l.text for l in doc.all_lines())
        self.assertIn("Treaty is still in force", text)
        self.assertIn("negotiate in good faith", text)

    def test_the_page_is_not_mistaken_for_a_scan(self):
        doc = backends.load_pdf(fixture("hiddentext"))
        laid_out = layout.analyse_document(doc, Diagnostics())
        self.assertTrue(laid_out.pages[0].has_text)

    def test_the_full_pipeline_produces_paragraphs(self):
        from ilatool import pipeline
        with TemporaryDirectory() as tmp:
            report = pipeline.run(pipeline.RunRequest(
                source=fixture("hiddentext"), out_source=Path(tmp) / "h.txt",
                out_html=Path(tmp) / "h.html", meta={"TITLE": "Hidden"}))
            self.assertTrue(report.ok, report.error)
            joined = " ".join(b.text for b in report.result.source.paragraphs)
            self.assertIn("Treaty is still in force", joined)


class FurnitureRuleTests(unittest.TestCase):
    """The running-header detector must not eat content.

    A treaty sets "Article 7" at the top of a page and repeats that shape on
    every page.  Folding digits away made all of them look like one running
    head, and the CISG lost 101 article headings before this was pinned down.
    """

    def test_short_labels_are_not_digit_folded(self):
        exact, foldable = layout.furniture_keys("Article 7")
        self.assertEqual(exact, "article 7")
        self.assertIsNone(foldable, "'article #' would match every other article")

    def test_a_real_running_head_is_still_foldable(self):
        _exact, foldable = layout.furniture_keys(
            "certain international obligations (ord. 30 IV 24) 561")
        self.assertIsNotNone(foldable)
        self.assertIn("#", foldable)

    def test_a_fixed_running_head_matches_exactly(self):
        exact, _foldable = layout.furniture_keys("International Law Review")
        self.assertEqual(exact, "international law review")

    def test_a_changing_counter_in_a_header_is_foldable(self):
        _exact, foldable = layout.furniture_keys("Annual Report 2024 of the Commission")
        self.assertIsNotNone(foldable)
        self.assertEqual(foldable, "annual report # of the commission")

    def test_a_bare_page_number_is_left_to_the_shape_rule(self):
        # "page # of #" has too few real words to fold safely, but
        # looks_like_page_number() recognises it directly.
        exact, foldable = layout.furniture_keys("Page 3 of 12")
        self.assertEqual(exact, "page 3 of 12")
        self.assertIsNone(foldable)
        self.assertTrue(textutil.looks_like_page_number("Page 3 of 12"))

    def _page(self, number, top_text, body_text, footer_text):
        from ilatool.layout import PageLayout, REGION_BODY
        lines = [
            LaidOutLine(page=number, text=top_text, bbox=Rect(60, 40, 300, 52),
                        size=10, indent=0, order=0),
            LaidOutLine(page=number, text=body_text, bbox=Rect(60, 120, 540, 132),
                        size=10, indent=0, order=1),
            LaidOutLine(page=number, text=footer_text, bbox=Rect(280, 800, 320, 812),
                        size=10, indent=0, order=2),
        ]
        return PageLayout(number=number, width=595.0, height=842.0, lines=lines)

    def test_article_headings_in_the_top_band_survive(self):
        pages = [self._page(n, f"Article {n}", "Body text of the article.",
                            f"Page {n} of 12") for n in range(1, 13)]
        furniture = layout.find_running_furniture(pages)
        for number in range(1, 13):
            dropped = furniture.get(number, set())
            self.assertNotIn(0, dropped, f"Article {number} must not be furniture")

    def test_page_numbers_in_the_footer_are_removed(self):
        pages = [self._page(n, "Body text at the top of the page.",
                            "Body text of the article.", f"Page {n} of 12")
                 for n in range(1, 13)]
        furniture = layout.find_running_furniture(pages)
        self.assertTrue(any(2 in furniture.get(n, set()) for n in range(1, 13)))


class LineMergeTests(unittest.TestCase):
    """Two runs on one baseline are a single line only when they are adjacent."""

    def _chunk(self, left, width, size=10.0):
        return {"left": left, "width": width, "size": size}

    def test_adjacent_fragments_merge(self):
        cluster = [self._chunk(100.0, 50.0)]
        self.assertTrue(backends._horizontally_close(cluster, self._chunk(153.0, 40.0), True))

    def test_a_column_gap_does_not_merge(self):
        cluster = [self._chunk(100.0, 80.0)]
        self.assertFalse(backends._horizontally_close(cluster, self._chunk(231.0, 200.0), True))


class ColumnRuleTests(unittest.TestCase):
    def test_a_gutter_needs_text_on_both_sides(self):
        from ilatool.layout import PageLayout, LaidOutLine
        # A single column with an indented block: every body line runs past
        # the candidate gap, so it must not be read as two columns.
        lines = []
        for index in range(12):
            lines.append(LaidOutLine(page=1, text="x" * 90, bbox=Rect(60, 100 + index * 14, 560, 112 + index * 14), size=10))
        for index in range(4):
            lines.append(LaidOutLine(page=1, text="y" * 30, bbox=Rect(271, 300 + index * 14, 400, 312 + index * 14), size=10))
        page = PageLayout(number=1, width=595.0, height=842.0, lines=lines)
        count, _bands = layout.detect_columns(lines, 595.0)
        self.assertEqual(count, 1)

    def test_two_genuinely_separate_columns_are_found(self):
        from ilatool.layout import PageLayout, LaidOutLine
        lines = []
        for index in range(8):
            lines.append(LaidOutLine(page=1, text="left column line", bbox=Rect(50, 100 + index * 14, 200, 112 + index * 14), size=10))
        for index in range(8):
            lines.append(LaidOutLine(page=1, text="right column line", bbox=Rect(315, 100 + index * 14, 480, 112 + index * 14), size=10))
        count, bands = layout.detect_columns(lines, 595.0)
        self.assertEqual(count, 2)
        self.assertEqual(len(bands), 2)


class BodyStartTests(unittest.TestCase):
    def test_cover_matter_before_the_numbered_body_is_detected(self):
        from ilatool.layout import PageLayout, LaidOutLine
        lines = []
        y = 0.0
        for text in ("COUR INTERNATIONALE DE JUSTICE", "RECUEIL DES ARRÊTS",
                     "ORDONNANCE DU 30 AVRIL 2024"):
            lines.append(LaidOutLine(page=1, text=text, bbox=Rect(150, y, 400, y + 12), size=10))
            y += 40
        for number in range(1, 9):
            lines.append(LaidOutLine(
                page=1, text=f"{number}. " + "The Court considers the matter at length. " * 6,
                bbox=Rect(100, y, 500, y + 12), size=10))
            y += 14
        page = PageLayout(number=1, width=595.0, height=1000.0, lines=lines)
        found = layout.detect_body_start([page])
        self.assertIsNotNone(found)
        self.assertEqual(found[2], 3, "the body starts at the first '1.' line")

    def test_a_document_without_a_numbered_body_is_untouched(self):
        from ilatool.layout import PageLayout, LaidOutLine
        lines = [LaidOutLine(page=1, text=f"Paragraph {i} of ordinary prose.",
                             bbox=Rect(100, i * 14, 500, i * 14 + 12), size=10)
                 for i in range(20)]
        page = PageLayout(number=1, width=595.0, height=1000.0, lines=lines)
        self.assertIsNone(layout.detect_body_start([page]))
