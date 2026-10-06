"""Keyboard handling tests.

Arrow keys used to quit the interface with "Bye.".  Three things combined to
cause it:

* the escape sequence was read with a 20 ms window, so a terminal that
  delivered ``ESC`` then ``[`` then ``B`` slightly apart produced a bare
  ``esc`` keypress instead of "down";
* ``ESC`` in the main menu quit the program;
* only ``ESC [ A`` was recognised, not the ``ESC O A`` form that terminals
  send in application-cursor mode, nor modified forms like ``ESC [ 1 ; 5 A``.

These tests pin all three down: the decoder is checked against the exact byte
sequences real terminals send, the application is checked not to exit, and a
real pty runs the real binary end to end.
"""

from __future__ import annotations

import os
import select
import signal
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.helpers import ROOT, Recorder

from ilatool.tui import term
from ilatool.tui.app import AppState, InteractiveApp


# Every one of these has been observed from a real terminal, an xterm, a
# tmux session or a mac Terminal window.
KEY_SEQUENCES = [
    (b"\x1b[A", "up"),
    (b"\x1b[B", "down"),
    (b"\x1b[C", "right"),
    (b"\x1b[D", "left"),
    (b"\x1bOA", "up"),          # application cursor keys (tmux, screen)
    (b"\x1bOB", "down"),
    (b"\x1bOC", "right"),
    (b"\x1bOD", "left"),
    (b"\x1b[1;5A", "up"),       # ctrl+up
    (b"\x1b[1;2B", "down"),     # shift+down
    (b"\x1b[H", "home"),
    (b"\x1b[F", "end"),
    (b"\x1bOH", "home"),
    (b"\x1bOF", "end"),
    (b"\x1b[1~", "home"),
    (b"\x1b[4~", "end"),
    (b"\x1b[5~", "pgup"),
    (b"\x1b[6~", "pgdn"),
    (b"\x1b[3~", "delete"),
    (b"\r", "enter"),
    (b"\n", "enter"),
    (b"q", "q"),
    (b"3", "3"),
    (b"\x03", "ctrl-c"),
    (b"\x7f", "backspace"),
    (b"\x08", "backspace"),
    (b"\t", "tab"),
]


class KeyDecoderTests(unittest.TestCase):
    def test_known_sequences(self):
        for data, expected in KEY_SEQUENCES:
            with self.subTest(sequence=data):
                parsed = term.parse_key_bytes(data)
                self.assertIsNotNone(parsed, f"{data!r} should decode")
                self.assertEqual(parsed[0], expected)
                self.assertEqual(parsed[1], len(data), "the whole sequence is consumed")

    def test_partial_sequences_ask_for_more(self):
        for data in (b"\x1b", b"\x1b[", b"\x1b[1", b"\x1b[1;", b"\x1b[1;5", b"\x1bO"):
            with self.subTest(sequence=data):
                self.assertIsNone(term.parse_key_bytes(data))

    def test_sequences_decode_as_they_arrive_byte_by_byte(self):
        # This is the shape of the original bug: the bytes turn up one at a
        # time, and only the complete sequence may be reported.
        for data, expected in KEY_SEQUENCES:
            if not data.startswith(b"\x1b"):
                continue
            with self.subTest(sequence=data):
                buffer = b""
                result = None
                for byte in data:
                    buffer += bytes([byte])
                    parsed = term.parse_key_bytes(buffer)
                    if parsed is not None:
                        result = parsed[0]
                        break
                self.assertEqual(result, expected)

    def test_escape_alone_is_only_reported_when_complete(self):
        self.assertIsNone(term.parse_key_bytes(b"\x1b"))

    def test_unknown_sequences_do_not_crash(self):
        for data in (b"\x1b[99X", b"\x1b[?25h", b"\x1b[?25l", b"\x1b[>0c"):
            with self.subTest(sequence=data):
                self.assertEqual(term.parse_key_bytes(data)[0], "unknown")

    def test_control_sequences_are_not_mistaken_for_keys(self):
        # ESC [ ? 25 h ends in the same letter as Home; it must not be one.
        self.assertNotEqual(term.parse_key_bytes(b"\x1b[?25h")[0], "home")

    def test_a_real_press_of_escape_is_not_mistaken_for_an_arrow(self):
        # ESC followed quickly by an unrelated letter is an arrow-like
        # sequence that never completes; it must still be one keypress.
        parsed = term.parse_key_bytes(b"\x1bZ")
        self.assertEqual(parsed, ("esc", 1))


class AppKeyHandlingTests(unittest.TestCase):
    def _app(self, tmp: Path) -> InteractiveApp:
        app = InteractiveApp(AppState(), Path(tmp) / "state.json", Path(tmp))
        app.screen = Recorder()
        return app

    def test_arrow_keys_move_the_selection_and_do_not_quit(self):
        with TemporaryDirectory() as tmp:
            app = self._app(Path(tmp))
            app.show_menu()
            app.handle_key("down")
            self.assertFalse(app.quit, "an arrow key must never exit")
            self.assertEqual(app.view.items[app.view.selected].key, "2")
            app.handle_key("up")
            self.assertEqual(app.view.items[app.view.selected].key, "1")
            self.assertFalse(app.quit)

    def test_navigation_wraps_around(self):
        with TemporaryDirectory() as tmp:
            app = self._app(Path(tmp))
            app.show_menu()
            app.handle_key("up")
            self.assertEqual(app.view.items[app.view.selected].key, "0")
            app.handle_key("down")
            self.assertEqual(app.view.items[app.view.selected].key, "1")

    def test_escape_in_the_menu_does_not_quit(self):
        with TemporaryDirectory() as tmp:
            app = self._app(Path(tmp))
            app.show_menu()
            app.handle_key("esc")
            self.assertFalse(app.quit)

    def test_escape_from_a_sub_screen_returns_to_the_menu(self):
        with TemporaryDirectory() as tmp:
            app = self._app(Path(tmp))
            app.show_menu()
            app.handle_key("down")
            app.handle_key("down")
            app.handle_key("enter")
            self.assertNotEqual(app.mode, "menu")
            app.handle_key("esc")
            self.assertEqual(app.mode, "menu")
            self.assertFalse(app.quit)

    def test_q_still_quits_from_the_menu(self):
        with TemporaryDirectory() as tmp:
            app = self._app(Path(tmp))
            app.show_menu()
            app.handle_key("q")
            self.assertTrue(app.quit)

    def test_unknown_keys_are_ignored(self):
        with TemporaryDirectory() as tmp:
            app = self._app(Path(tmp))
            app.show_menu()
            selection = app.view.selected
            for key in ("unknown", "f5", "insert", "center"):
                app.handle_key(key)
            self.assertFalse(app.quit)
            self.assertEqual(app.view.selected, selection)

    def test_a_long_run_of_arrow_keys_is_stable(self):
        with TemporaryDirectory() as tmp:
            app = self._app(Path(tmp))
            app.show_menu()
            for _ in range(50):
                app.handle_key("down")
                app.handle_key("up")
            self.assertFalse(app.quit)


class ScriptedInput:
    """A stand-in for :class:`~ilatool.tui.term.RawInput` with a fixed script."""

    def __init__(self, keys):
        self.keys = list(keys)
        self.reads = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read_key(self, timeout=None):
        key = self.keys.pop(0) if self.keys else "eof"
        self.reads.append(key)
        return key

    def read_key_timeout(self, timeout=None):
        return self.read_key(timeout)


class RunLoopTests(unittest.TestCase):
    """Drive the whole application loop with a scripted key sequence."""

    def _run(self, keys):
        import contextlib
        import io
        with TemporaryDirectory() as tmp:
            app = InteractiveApp(AppState(), Path(tmp) / "state.json", Path(tmp))
            app.screen = Recorder()
            script = ScriptedInput(keys)
            app.raw = script
            # The loop says goodbye on stdout; keep the test output clean.
            with contextlib.redirect_stdout(io.StringIO()):
                app.run()
            self.assertTrue(app.screen.frames, "the interface should have drawn")
            return app, script

    def test_navigating_with_arrows_then_quitting(self):
        app, script = self._run(["down", "down", "up", "enter", "esc", "q"])
        self.assertTrue(app.quit)
        self.assertEqual(script.reads[-1], "q")

    def test_a_bare_escape_never_ends_the_session(self):
        app, _ = self._run(["esc", "esc", "esc", "q"])
        self.assertTrue(app.quit)

    def test_unknown_key_names_are_harmless(self):
        app, _ = self._run(["unknown", "center", "f5", "insert", "q"])
        self.assertTrue(app.quit)

    def test_the_menu_is_actually_drawn(self):
        app, _ = self._run(["down", "up", "q"])
        text = app.screen.text()
        self.assertIn("Annotated Text Builder", text)
        self.assertIn("Quit", text)

    def test_quitting_with_ctrl_c(self):
        app, _ = self._run(["ctrl-c"])
        self.assertTrue(app.quit)


def frame_text(recorder) -> str:
    """Everything the recorder has drawn so far, with the colours stripped."""
    return "\n".join(term.strip_ansi(line)
                     for frame in recorder.frames for line in frame)


class ByteStreamAppTests(unittest.TestCase):
    """The whole application, driven by a real byte stream with real gaps.

    A pty is not always available (containers, some sandboxes), so the bytes
    are delivered through a pipe instead: the reader, the escape-sequence
    decoder and the run loop are all the real ones.  This is the test that
    would have caught "arrow keys quit the interface".
    """

    def _run(self, script, settle=0.6):
        import contextlib
        import io
        import threading
        from ilatool.tui import term as term_mod

        read_fd, write_fd = os.pipe()
        with TemporaryDirectory() as tmp:
            app = InteractiveApp(AppState(), Path(tmp) / "state.json", Path(tmp))
            app.screen = Recorder()
            reader = term_mod.RawInput.__new__(term_mod.RawInput)
            reader._fd = read_fd
            reader._saved = None
            app.raw = reader

            def write_script():
                # Wait until the interface has drawn once: by then the run
                # loop is blocked in read_key, so the gaps between bytes are
                # the ones this test controls -- not an artefact of startup.
                deadline = time.time() + 5.0
                while not app.screen.frames and time.time() < deadline:
                    time.sleep(0.01)
                time.sleep(settle)
                try:
                    for chunk, delay in script:
                        os.write(write_fd, chunk)
                        time.sleep(delay)
                except OSError:
                    pass
                finally:
                    os.close(write_fd)

            writer = threading.Thread(target=write_script, daemon=True)
            writer.start()
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    app.run()
            finally:
                writer.join(timeout=2)
                try:
                    os.close(read_fd)
                except OSError:
                    pass
            return app

    def test_arrow_keys_arriving_in_pieces_do_not_quit(self):
        # ESC, then '[', then 'B', with gaps in between -- exactly what a
        # terminal does.  The old 20 ms window turned the first byte into a
        # bare ESC, and ESC quit the program.
        app = self._run([
            (b"\x1b", 0.03),      # gaps well inside the escape window...
            (b"[", 0.03),
            (b"B", 0.25),          # ...but far above the old 20 ms one
            (b"\x1b[A", 0.25),
            (b"q", 0.25),
        ])
        self.assertTrue(app.quit)
        drawn = frame_text(app.screen)
        self.assertIn("\u25b8 2)", drawn,
                      "the Down key must have moved the cursor to item 2")
        self.assertIn("\u25b8 1)", drawn,
                      "the following Up key must have moved it back to item 1")

    def test_a_run_of_arrow_keys_keeps_the_interface_alive(self):
        app = self._run([(b"\x1b[B" * 5, 0.4), (b"q", 0.4)])
        self.assertTrue(app.quit)
        drawn = frame_text(app.screen)
        self.assertIn("Annotated Text Builder", drawn)
        self.assertIn("\u25b8 5)", drawn, "five Down presses should reach item 5")

    def test_escape_then_quit_still_quits(self):
        app = self._run([(b"\x1b", 0.4), (b"q", 0.4)])
        self.assertTrue(app.quit)
        self.assertIn("\u25b8 1)", frame_text(app.screen),
                      "a bare ESC must leave the selection where it was")


class SlowTerminalTests(unittest.TestCase):
    """Feed the reader through a pipe, one byte at a time, with real delays.

    This is the failure mode that made arrow keys quit: the terminal delivers
    ``ESC``, then ``[``, then ``B`` with gaps in between, and a reader with too
    short a window reports a bare ``esc`` for the first byte.
    """

    def _reader(self, read_fd: int) -> term.RawInput:
        reader = term.RawInput.__new__(term.RawInput)
        reader._fd = read_fd
        reader._saved = None
        return reader

    def _read_stream(self, chunks, gap: float):
        """Read one key while a thread feeds the bytes in with real gaps.

        The reader is already blocked on the first byte, so the delay between
        chunks is the delay the decoder actually experiences -- which is the
        whole point.
        """
        import threading
        read_fd, write_fd = os.pipe()
        try:
            reader = self._reader(read_fd)

            def writer():
                try:
                    for index, chunk in enumerate(chunks):
                        os.write(write_fd, chunk)
                        if index < len(chunks) - 1:
                            time.sleep(gap)
                except OSError:
                    pass
                finally:
                    try:
                        os.close(write_fd)
                    except OSError:
                        pass

            thread = threading.Thread(target=writer, daemon=True)
            thread.start()
            result = reader.read_key()
            thread.join(timeout=2)
            return result
        finally:
            os.close(read_fd)

    def test_arrow_key_split_across_reads_is_still_an_arrow(self):
        # 30 ms between bytes: too slow for the old 20 ms window, comfortably
        # inside the current one.
        self.assertEqual(self._read_stream([b"\x1b", b"[", b"B"], 0.03), "down")

    def test_application_cursor_keys_are_understood(self):
        self.assertEqual(self._read_stream([b"\x1b", b"O", b"D"], 0.03), "left")

    def test_a_lone_escape_is_reported_as_escape(self):
        read_fd, write_fd = os.pipe()
        try:
            reader = self._reader(read_fd)
            os.write(write_fd, b"\x1b")
            started = time.time()
            self.assertEqual(reader.read_key(), "esc")
            # It has to wait for the rest of a possible sequence first.
            self.assertGreaterEqual(time.time() - started, term.ESCAPE_TIMEOUT * 0.5)
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_two_arrow_keys_in_one_write_are_two_keypresses(self):
        read_fd, write_fd = os.pipe()
        try:
            reader = self._reader(read_fd)
            os.write(write_fd, b"\x1b[B\x1b[A")
            self.assertEqual(reader.read_key(), "down")
            self.assertEqual(reader.read_key(), "up")
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_a_timeout_returns_none_rather_than_a_key(self):
        read_fd, write_fd = os.pipe()
        try:
            reader = self._reader(read_fd)
            self.assertIsNone(reader.read_key_timeout(0.05))
        finally:
            os.close(read_fd)
            os.close(write_fd)


@unittest.skipUnless(hasattr(os, "openpty"), "needs a POSIX pty")
class RealTerminalTests(unittest.TestCase):
    """Run the real program in a real pty and press real arrow keys."""

    def _spawn(self, state_path: Path):
        """Start the real program attached to a pty.

        Returns ``(process, fd)``.  Some environments forbid allocating a
        pty at all; the caller skips in that case rather than failing.
        """
        import fcntl
        import struct
        import termios

        argv = [sys.executable, str(ROOT / "build_tool.py"),
                "menu", "--state", str(state_path)]
        env = dict(os.environ, TERM="xterm")

        try:
            import pty
            master, slave = pty.openpty()
        except (ImportError, OSError) as exc:
            raise unittest.SkipTest(f"cannot allocate a pty here: {exc}")

        import subprocess
        process = subprocess.Popen(
            argv, stdin=slave, stdout=slave, stderr=slave,
            cwd=str(ROOT), env=env, close_fds=True)
        os.close(slave)
        try:
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        except Exception:
            pass
        return process, master

    def _drain(self, fd, seconds: float) -> bytes:
        out = bytearray()
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.05)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            out.extend(chunk)
        return bytes(out)

    def _stop(self, process, fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            process.wait(timeout=2)
            return
        except Exception:
            pass
        try:
            process.kill()
            process.wait(timeout=2)
        except Exception:
            pass

    def test_arrow_keys_do_not_exit_the_interface(self):
        with TemporaryDirectory() as tmp:
            process, fd = self._spawn(Path(tmp) / "state.json")
            try:
                transcript = self._drain(fd, 1.5)
                self.assertIn(b"Annotated Text Builder", transcript,
                              "the interface should have started")

                # Exactly what a real terminal sends for the arrow keys.
                os.write(fd, b"\x1b[B")
                transcript += self._drain(fd, 0.8)
                os.write(fd, b"\x1b[A\x1b[B\x1b[B")
                transcript += self._drain(fd, 0.8)
                self.assertNotIn(b"Bye.", transcript,
                                 "arrow keys must not exit the interface")

                # ...and the interface is still responsive to a real quit.
                os.write(fd, b"q")
                transcript += self._drain(fd, 1.5)
                self.assertIn(b"Bye.", transcript)
            finally:
                self._stop(process, fd)

    def test_a_bare_escape_key_does_not_exit_the_interface(self):
        with TemporaryDirectory() as tmp:
            process, fd = self._spawn(Path(tmp) / "state.json")
            try:
                self._drain(fd, 1.5)
                os.write(fd, b"\x1b")
                transcript = self._drain(fd, 1.0)
                self.assertNotIn(b"Bye.", transcript)
                os.write(fd, b"q")
                transcript += self._drain(fd, 1.5)
                self.assertIn(b"Bye.", transcript)
            finally:
                self._stop(process, fd)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
