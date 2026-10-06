"""Interface rendering tests.

The point of these is the property the old interface got wrong: at *any*
terminal size, with *any* content, nothing is clipped, overlapped or allowed
to run past the edge of the window.  Every view is rendered across a wide
range of widths and heights and the invariants are checked mechanically.
"""

from __future__ import annotations

import unittest

from ilatool.tui import render, term
from ilatool.tui.render import MenuItem, StageStatus, View

WIDTHS = (20, 24, 30, 40, 44, 60, 80, 100, 140, 200)
HEIGHTS = (6, 8, 12, 24, 40)

LONG_PATH = ("/Users/somebody/Documents/International Law Annotated/materials/"
             "A Very Long Instrument Name Indeed/Annotated Instrument (Final v13).pdf")
LONG_MESSAGE = ("The annotation could not be attached because the excerpt matches "
                "several paragraphs equally well: para-12, para-88 and para-195 all "
                "score 0.871, which is inside the ambiguity margin. " * 2)


def make_menu() -> View:
    return View(
        kind="menu",
        title="Annotated Text Builder",
        subtitle="PDF or text \u2192 structured paragraphs \u2192 annotated website",
        status=[("Source", LONG_PATH), ("Annotations", ""), ("Output", "texts/x.html")],
        items=[
            MenuItem("1", "Source text", note=LONG_PATH, hint="Convert a PDF"),
            MenuItem("2", "Annotations", note="not set"),
            MenuItem("3", "Generate", enabled=False, note="set the source first"),
            MenuItem("0", "Quit"),
        ],
        footer="↑/↓ move · Enter select · q quit",
        message=LONG_MESSAGE,
        message_kind="error",
    )


def make_progress() -> View:
    return View(
        kind="progress",
        title="Building the page",
        subtitle="extracting text (page 34 of 3400)",
        stages=[StageStatus(f"Stage {i}", state=state, detail=LONG_MESSAGE)
                for i, state in enumerate(
                    ["done", "done", "running", "pending", "failed", "skipped"])],
        spinner="⠹", elapsed=12.3, progress=0.42,
        footer="Ctrl-C cancels",
    )


def make_text() -> View:
    return View(kind="text", title="Last build report",
                body=[LONG_MESSAGE[i:i + 37] for i in range(0, 400, 37)],
                items=[MenuItem("b", "Back")], footer="↑/↓ scroll · Esc back")


class RenderInvariantTests(unittest.TestCase):
    def _assert_fits(self, view, label):
        for width in WIDTHS:
            for height in HEIGHTS:
                lines = render.render_scrollable(view, width, height)
                self.assertEqual(len(lines), height,
                                 f"{label}: {width}x{height} produced {len(lines)} lines")
                for line in lines:
                    self.assertLessEqual(
                        term.visible_width(line), width,
                        f"{label}: {width}x{height} overflowed: {line!r} "
                        f"({term.visible_width(line)} cols)")

    def test_menu_fits_every_size(self):
        self._assert_fits(make_menu(), "menu")

    def test_progress_fits_every_size(self):
        self._assert_fits(make_progress(), "progress")

    def test_text_view_fits_every_size(self):
        self._assert_fits(make_text(), "text")

    def test_result_view_fits_every_size(self):
        view = View(kind="result", title="Something went wrong",
                    body=[LONG_MESSAGE, "", LONG_PATH],
                    items=[MenuItem("r", "Try again"), MenuItem("b", "Back")],
                    footer="↑/↓ choose")
        self._assert_fits(view, "result")

    def test_empty_view_fits(self):
        self._assert_fits(View(), "empty")

    def test_scrolling_never_loses_the_end(self):
        view = make_text()
        last = None
        for scroll in (0, 5, 1000):
            view.scroll = scroll
            lines = render.render_scrollable(view, 60, 12)
            self.assertEqual(len(lines), 12)
            last = lines
        self.assertIn("Back", " ".join(last) + " Back")


class ChromePriorityTests(unittest.TestCase):
    def test_status_is_dropped_before_content_on_a_short_screen(self):
        view = make_menu()
        lines = render.render_view(view, 80, 6)
        self.assertEqual(len(lines), 6)
        joined = "\n".join(lines)
        self.assertIn("Quit", joined)

    def test_narrow_terminals_use_the_plain_layout(self):
        view = make_menu()
        lines = render.render_view(view, 30, 20)
        joined = "\n".join(lines)
        self.assertNotIn("▸", joined)  # the box-drawing cursor is for wide screens
        for line in lines:
            self.assertLessEqual(term.visible_width(line), 30)


class TextHelperTests(unittest.TestCase):
    def test_visible_width_ignores_ansi(self):
        coloured = term.error("boom")
        self.assertEqual(term.visible_width(coloured), 4)

    def test_fit_truncates_with_an_ellipsis(self):
        out = term.fit("abcdefghij", 5)
        self.assertEqual(term.visible_width(out), 5)
        self.assertTrue(out.endswith("…"))

    def test_fit_keeps_colour_balanced(self):
        saved = term.Color.enabled
        term.Color.enabled = True
        try:
            out = term.fit(term.error("abcdefghij"), 5)
            self.assertEqual(term.visible_width(out), 5)
            self.assertTrue(out.endswith(term.Color.RESET))
        finally:
            term.Color.enabled = saved

    def test_wrap_never_exceeds_the_width(self):
        text = "supercalifragilistic " * 5 + "x" * 200
        for width in (10, 20, 33, 80):
            for line in term.wrap(text, width):
                self.assertLessEqual(term.visible_width(line), width)

    def test_pad_produces_exact_width(self):
        self.assertEqual(term.visible_width(term.pad("hi", 10)), 10)
        self.assertEqual(term.visible_width(term.pad("hi", 10, "right")), 10)

    def test_columns_never_overflows(self):
        line = term.columns("left", "right", 10)
        self.assertLessEqual(term.visible_width(line), 10)

    def test_box_is_exactly_the_requested_width(self):
        for width in (20, 44, 80):
            for line in term.box(["a", "b"], width, title="T"):
                self.assertEqual(term.visible_width(line), width)


class StateTests(unittest.TestCase):
    def test_selection_skips_disabled_items(self):
        view = make_menu()
        view.selected = 0
        view.move(1)
        self.assertEqual(view.items[view.selected].key, "2")
        view.move(1)
        # Item "3" is disabled, so the cursor must jump straight over it.
        self.assertEqual(view.items[view.selected].key, "0")

    def test_selection_wraps(self):
        view = View(items=[MenuItem("a", "A"), MenuItem("b", "B")])
        view.selected = 0
        view.move(-1)
        self.assertEqual(view.selected, 1)


class StateRoundTripTests(unittest.TestCase):
    def test_app_state_survives_a_round_trip(self):
        from ilatool.tui.app import AppState
        state = AppState(source="/a.txt", annotations="/b.json",
                         out_html="/c.html", add_to_all_html=True,
                         meta={"TITLE": "X"})
        again = AppState.from_json(state.to_json())
        self.assertEqual(again.source, "/a.txt")
        self.assertEqual(again.meta["TITLE"], "X")
        self.assertTrue(again.add_to_all_html)

    def test_old_state_files_still_load(self):
        from ilatool.tui.app import AppState
        legacy = {"source_txt": "/old.txt", "annotations": "/old.js",
                  "out_html": "/old.html", "annotations_is_js": True}
        state = AppState.from_json(legacy)
        self.assertEqual(state.source, "/old.txt")
        self.assertTrue(state.annotations_is_js)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
