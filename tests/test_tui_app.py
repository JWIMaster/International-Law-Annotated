"""Smoke tests for the real application object.

The rendering tests use hand-built views; these drive the actual app so that
a mistake in a screen's construction (a missing key, a bad format string) is
caught rather than silently shipped.
"""

from __future__ import annotations

import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ilatool import pipeline
from ilatool.errors import InputError, Stage, ToolError
from ilatool.tui import render, term
from tests.helpers import Recorder
from ilatool.tui.app import AppState, InteractiveApp, PlainApp, load_state


def make_app(tmp: Path, widths=(24, 44, 80, 140)):
    state = AppState(source=str(tmp / "a.txt"), annotations=str(tmp / "b.json"))
    app = InteractiveApp(state, tmp / "state.json", tmp)
    app.screen = Recorder()
    app._test_widths = widths
    return app


def render_at(app, width, height=24):
    app.view.message = app.message
    app.view.message_kind = app.message_kind
    app.view.scroll = app.scroll
    return render.render_scrollable(app.view, width, height)


class AppSmokeTests(unittest.TestCase):
    def test_every_screen_renders_within_the_terminal(self):
        with TemporaryDirectory() as tmp:
            app = make_app(Path(tmp))
            screens = []
            app.show_menu()
            screens.append(("menu", app.view))
            app.action_backends()
            screens.append(("backends", app.view))
            app.action_format_help()
            screens.append(("formats", app.view))
            app.show_error(ToolError("something broke", hint="try something else",
                                     detail="traceback line"))
            screens.append(("error", app.view))
            app.show_error(ToolError("no retry offered"))
            screens.append(("error-no-retry", app.view))
            app.show_text("Empty", [])
            screens.append(("empty", app.view))
            for name, view in screens:
                for width in (20, 24, 40, 80, 140, 200):
                    lines = render.render_scrollable(view, width, 24)
                    self.assertEqual(len(lines), 24, f"{name} at {width}")
                    for line in lines:
                        self.assertLessEqual(term.visible_width(line), width,
                                             f"{name} at {width}: {line!r}")

    def test_diagnostic_screen_survives_a_real_report(self):
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            source = tmpdir / "a.txt"
            source.write_text("TITLE: T\n---\n¶ One paragraph of text here.\n",
                              encoding="utf-8")
            report = pipeline.run(pipeline.RunRequest(
                source=source, out_html=tmpdir / "a.html",
                meta={"TITLE": "T"}))
            app = make_app(tmpdir)
            app.last_report = report
            app.action_diagnostics()
            for width in (24, 60, 120):
                for line in render.render_scrollable(app.view, width, 24):
                    self.assertLessEqual(term.visible_width(line), width)

    def test_long_paths_and_messages_do_not_break_the_layout(self):
        with TemporaryDirectory() as tmp:
            app = make_app(Path(tmp))
            app.state.source = "/" + "/".join(["a-very-long-directory-name"] * 8) + "/x.pdf"
            app.note("x" * 400, "error")
            app.show_menu()
            for width in (20, 30, 50, 100):
                for line in render_at(app, width):
                    self.assertLessEqual(term.visible_width(line), width)

    def test_activation_skips_disabled_items_and_reports_why(self):
        with TemporaryDirectory() as tmp:
            app = make_app(Path(tmp))
            app.state.source = ""
            app.state.annotations = ""
            app.show_menu()
            # "Generate" is disabled: selecting it must explain, not crash.
            app.view.selected = 2
            app.activate()
            self.assertIn("source", app.message.lower())
            self.assertEqual(app.mode, "menu")

    def test_escape_returns_to_the_menu(self):
        with TemporaryDirectory() as tmp:
            app = make_app(Path(tmp))
            app.action_backends()
            self.assertEqual(app.mode, "view")
            app.handle_key("esc")
            self.assertEqual(app.mode, "menu")

    def test_quit_from_the_menu(self):
        with TemporaryDirectory() as tmp:
            app = make_app(Path(tmp))
            app.show_menu()
            app.handle_key("q")
            self.assertTrue(app.quit)


class StateFileTests(unittest.TestCase):
    def test_state_is_written_and_read_back(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            app = make_app(Path(tmp))
            app.state.source = "/some/source.txt"
            app.state.add_to_all_html = True
            app.state_path = path
            app.save()
            again = load_state(path)
            self.assertEqual(again.source, "/some/source.txt")
            self.assertTrue(again.add_to_all_html)

    def test_a_corrupt_state_file_is_ignored(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(load_state(path).source, "")

    def test_a_missing_state_file_is_fine(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(load_state(Path(tmp) / "nope.json").source, "")


class PlainAppTests(unittest.TestCase):
    def test_menu_wraps_a_long_path_to_the_terminal(self):
        import contextlib
        with TemporaryDirectory() as tmp:
            app = PlainApp(AppState(source="/" + "x" * 300),
                           Path(tmp) / "s.json", Path(tmp))
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                app.show_menu()
            width, _ = term.terminal_size()
            for line in buffer.getvalue().splitlines():
                self.assertLessEqual(term.visible_width(line), width, repr(line))


class JobLifecycleTests(unittest.TestCase):
    def test_a_failed_job_becomes_an_error_view_with_a_retry(self):
        with TemporaryDirectory() as tmp:
            app = make_app(Path(tmp))
            app._result = {"report": pipeline.RunReport(
                error=InputError("could not read the file", hint="check the path"))}
            app._thread = None
            app._on_done = lambda report: None
            app._retry = lambda: None
            app._progress = {"stages": {}}
            app._finish_job()
            self.assertEqual(app.mode, "error")
            self.assertIn("could not read", " ".join(app.view.body))
            self.assertTrue(app.view.items[0].enabled)

    def test_a_successful_job_calls_back(self):
        with TemporaryDirectory() as tmp:
            app = make_app(Path(tmp))
            seen = {}
            app._result = {"report": pipeline.RunReport()}
            app._thread = None
            app._on_done = lambda report: seen.setdefault("called", True)
            app._retry = None
            app._finish_job()
            self.assertTrue(seen.get("called"))

    def test_an_unexpected_exception_is_not_swallowed(self):
        with TemporaryDirectory() as tmp:
            app = make_app(Path(tmp))
            app._result = {"error": RuntimeError("boom")}
            app._thread = None
            app._on_done = lambda report: None
            app._progress = {"stages": {}}
            app._finish_job()
            self.assertEqual(app.mode, "error")
            self.assertIsNotNone(app.last_report.error)
            self.assertEqual(app.last_report.error.stage, Stage.INTERNAL)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
