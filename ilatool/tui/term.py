"""Terminal primitives: size, colour, keys, and width-aware text helpers.

Everything here is deliberately small and dependency-free.  The important
part is :func:`visible_width` / :func:`wrap` / :func:`fit`: the old
interface simply ``print()``-ed strings and let the terminal wrap them, which
is why long paths, long error messages and narrow windows produced ragged,
overlapping output.  Every panel in this tool is laid out from an explicit
width instead.
"""

from __future__ import annotations

import os
import shutil
import signal
import sys
import unicodedata
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple
# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------

class Color:
    """ANSI colours, disabled automatically when output is not a terminal."""

    enabled = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    INVERSE = "\033[7m"

    @classmethod
    def wrap(cls, text: str, *codes: str) -> str:
        if not cls.enabled or not codes:
            return text
        return "".join(codes) + text + cls.RESET

    @classmethod
    def disable(cls) -> None:
        cls.enabled = False


def bold(text: str) -> str:
    return Color.wrap(text, Color.BOLD)


def dim(text: str) -> str:
    return Color.wrap(text, Color.DIM)


def title(text: str) -> str:
    return Color.wrap(text, Color.BOLD, Color.CYAN)


def ok(text: str) -> str:
    return Color.wrap(text, Color.GREEN)


def warn(text: str) -> str:
    return Color.wrap(text, Color.YELLOW)


def error(text: str) -> str:
    return Color.wrap(text, Color.BOLD, Color.RED)


def path_text(text: str) -> str:
    return Color.wrap(text, Color.MAGENTA)


def accent(text: str) -> str:
    return Color.wrap(text, Color.BLUE)


_ESCAPE_RE = __import__("re").compile(r"\033\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return _ESCAPE_RE.sub("", text)


# ---------------------------------------------------------------------------
# Width handling
# ---------------------------------------------------------------------------

def char_width(ch: str) -> int:
    """Display width of one character (East Asian wide characters count 2)."""
    if ch == "\t":
        return 4
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def visible_width(text: str) -> int:
    """Width of ``text`` as it will appear, ignoring ANSI colour codes."""
    return sum(char_width(ch) for ch in strip_ansi(text))


def fit(text: str, width: int, ellipsis: str = "…") -> str:
    """Truncate to ``width`` display columns, counting ANSI codes as zero.

    Colours are preserved (the reset code is re-appended) so a truncated
    coloured string does not leak its colour into the rest of the line.
    """
    if width <= 0:
        return ""
    if visible_width(text) <= width:
        return text

    plain = strip_ansi(text)
    out: List[str] = []
    used = 0
    budget = width - char_width(ellipsis)
    for ch in plain:
        w = char_width(ch)
        if used + w > budget:
            break
        out.append(ch)
        used += w
    result = "".join(out) + ellipsis
    if text != plain and Color.enabled:
        result += Color.RESET
    return result


def pad(text: str, width: int, align: str = "left") -> str:
    """Pad ``text`` to exactly ``width`` display columns."""
    gap = width - visible_width(text)
    if gap <= 0:
        return fit(text, width)
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def wrap(text: str, width: int, *, subsequent_indent: str = "") -> List[str]:
    """Word-wrap to ``width`` display columns, never exceeding it.

    Long unbreakable tokens (a URL, a deep file path) are hard-split, because
    letting the terminal wrap them is exactly what makes output look broken.
    """
    if width <= 1:
        return [text[: max(0, width)]]
    lines: List[str] = []
    for paragraph in str(text).split("\n"):
        words = paragraph.split(" ")
        current = ""
        indent = ""
        for word in words:
            while visible_width(word) > width - visible_width(indent):
                if current:
                    lines.append(current)
                    current = ""
                    indent = subsequent_indent
                room = width - visible_width(indent)
                head = _split_at(word, room)
                lines.append(indent + head)
                word = word[len(head):]
                indent = subsequent_indent
            candidate = word if not current else current + " " + word
            if visible_width(candidate) <= width - visible_width(indent):
                current = candidate
            else:
                if current:
                    lines.append(current)
                indent = subsequent_indent
                current = word
        lines.append(current if current else "")
    return lines


def _split_at(text: str, width: int) -> str:
    out: List[str] = []
    used = 0
    for ch in text:
        w = char_width(ch)
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out) or text[:1]


def wrap_block(text: str, width: int, initial_indent: str = "",
               subsequent_indent: str = "") -> List[str]:
    lines = wrap(text, width - max(visible_width(initial_indent),
                                   visible_width(subsequent_indent)))
    if not lines:
        return [initial_indent]
    out = [initial_indent + lines[0]]
    out.extend(subsequent_indent + line for line in lines[1:])
    return out


def truncate_path(text: str, width: int) -> str:
    """Shorten a path from the middle, where the least information is."""
    from ..textutil import truncate_middle
    return truncate_middle(text, width)


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

SINGLE = "single"
DOUBLE = "double"
ROUND = "round"

_BOX = {
    SINGLE: ("┌", "┐", "└", "┘", "─", "│"),
    DOUBLE: ("╔", "╗", "╚", "╝", "═", "║"),
    ROUND: ("╭", "╮", "╰", "╯", "─", "│"),
}


@dataclass
class Panel:
    lines: List[str]


def box(lines: Sequence[str], width: int, title: str = "", style: str = ROUND,
        min_width: int = 8) -> List[str]:
    """Draw a bordered panel whose every line is exactly ``width`` wide."""
    if width < min_width:
        return [fit(line, width) for line in lines]
    tl, tr, bl, br, hz, vt = _BOX.get(style, _BOX[ROUND])
    inner = width - 4
    out: List[str] = []
    if title:
        label = f" {title} "
        head = tl + hz + label
        head = head + hz * max(0, width - 3 - visible_width(label))
        head += tr
        out.append(fit(head, width))
    else:
        out.append(tl + hz * (width - 2) + tr)
    for line in lines:
        out.append(vt + " " + pad(fit(line, inner), inner) + " " + vt)
    out.append(bl + hz * (width - 2) + br)
    return [fit(line, width) for line in out]


def columns(left: str, right: str, width: int, gap: int = 2) -> str:
    """A line with left-aligned and right-aligned content."""
    room = width - gap
    lw = min(visible_width(left), max(0, room - 1))
    left = fit(left, lw) if visible_width(left) > lw else left
    right_fit = fit(right, max(0, room - visible_width(left)))
    filler = " " * max(1, width - visible_width(left) - visible_width(right_fit))
    return fit(left + filler + right_fit, width)


# ---------------------------------------------------------------------------
# Terminal control
# ---------------------------------------------------------------------------

def terminal_size(fallback: Tuple[int, int] = (80, 24)) -> Tuple[int, int]:
    size = shutil.get_terminal_size(fallback=fallback)
    return max(20, size.columns), max(6, size.lines)


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


#: How long to wait for the rest of an escape sequence once ESC has arrived.
#: Too short and an arrow key degrades into a bare "esc" -- which used to quit
#: the application.  Terminals dribble the bytes out one at a time and there
#: is latency over ssh, so this is deliberately generous; the cost is only
#: paid after a genuine ESC keypress.
ESCAPE_TIMEOUT = 0.10

#: CSI/SS3 final bytes that name a key when they follow ``ESC [`` or ``ESC O``.
_CSI_LETTERS = {
    "A": "up", "B": "down", "C": "right", "D": "left",
    "E": "center", "H": "home", "F": "end",
    "P": "f1", "Q": "f2", "R": "f3", "S": "f4", "Z": "backtab",
}

#: The same keys expressed as ``ESC [ <number> ~``.
_CSI_TILDES = {
    "1": "home", "2": "insert", "3": "delete", "4": "end",
    "5": "pgup", "6": "pgdn", "7": "home", "8": "end",
    "11": "f1", "12": "f2", "13": "f3", "14": "f4", "15": "f5",
    "17": "f6", "18": "f7", "19": "f8", "20": "f9", "21": "f10",
    "23": "f11", "24": "f12",
}


def _name_for_csi(params: bytes, final: bytes) -> str:
    """Name a CSI sequence such as ``ESC [ A`` or ``ESC [ 1 ; 5 A``.

    Parameter bytes other than digits and semicolons mean this is a control
    sequence rather than a key -- ``ESC [ ? 25 h`` is "show cursor", and
    reading its final ``h`` as Home would be wrong.
    """
    text = params.decode("latin-1")
    if text and any(ch not in "0123456789;" for ch in text):
        return "unknown"
    if final == b"~":
        return _CSI_TILDES.get(text.split(";")[0], "unknown")
    letter = final.decode("latin-1")
    return _CSI_LETTERS.get(letter.upper(), "unknown")


def parse_key_bytes(data: bytes) -> Optional[Tuple[str, int]]:
    """Decode one keypress from the front of ``data``.

    Returns ``(key_name, bytes_consumed)``, or ``None`` when ``data`` is a
    partial escape sequence and more bytes are needed to decide.  This is a
    pure function so the decoder can be tested against the exact byte
    sequences real terminals send, without needing a terminal.
    """
    if not data:
        return None
    byte = data[0:1]
    if byte != b"\x1b":
        if byte in (b"\r", b"\n"):
            return ("enter", 1)
        if byte == b"\x03":
            return ("ctrl-c", 1)
        if byte in (b"\x7f", b"\x08"):
            return ("backspace", 1)
        if byte == b"\t":
            return ("tab", 1)
        if byte == b"\x04":
            return ("eof", 1)
        if byte[0] < 0x20:
            return (f"ctrl-{chr(byte[0] + 96)}", 1)
        return (byte.decode("utf-8", "replace"), 1)

    if len(data) == 1:
        return None  # either a bare ESC or the start of a sequence

    second = data[1:2]
    if second == b"[":
        # CSI: parameter bytes 0x30-0x3F, then intermediates 0x20-0x2F, then a
        # final byte 0x40-0x7E.
        index = 2
        while index < len(data) and 0x30 <= data[index] <= 0x3F:
            index += 1
        while index < len(data) and 0x20 <= data[index] <= 0x2F:
            index += 1
        if index >= len(data):
            return None
        final = data[index:index + 1]
        if not (0x40 <= final[0] <= 0x7E):
            return ("unknown", index + 1)
        return (_name_for_csi(data[2:index], final), index + 1)

    if second == b"O":
        # SS3, used by terminals in "application cursor keys" mode.
        if len(data) < 3:
            return None
        return (_CSI_LETTERS.get(chr(data[2]).upper(), "unknown"), 3)

    return ("esc", 1)


class RawInput:
    """Read single keypresses (including arrow keys) in cbreak mode."""

    def __init__(self) -> None:
        self._fd = sys.stdin.fileno()
        self._saved = None

    def __enter__(self) -> "RawInput":
        try:
            import termios
            self._saved = termios.tcgetattr(self._fd)
            new = termios.tcgetattr(self._fd)
            new[3] = new[3] & ~(termios.ICANON | termios.ECHO)
            new[6][termios.VMIN] = 1
            new[6][termios.VTIME] = 0
            termios.tcsetattr(self._fd, termios.TCSADRAIN, new)
        except Exception:
            self._saved = None
        return self

    def __exit__(self, *exc) -> None:
        if self._saved is not None:
            try:
                import termios
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            except Exception:
                pass

    # -- reading --------------------------------------------------------------

    def _read_byte(self, timeout: Optional[float]) -> bytes:
        import select
        try:
            ready, _, _ = select.select([self._fd], [], [], timeout)
        except (OSError, ValueError):
            return b""
        if not ready:
            return b""
        try:
            return os.read(self._fd, 1)
        except OSError:
            return b""

    def read_key(self, timeout: Optional[float] = None) -> Optional[str]:
        """Return a symbolic key name, or None if ``timeout`` elapsed first.

        An escape sequence is read byte by byte until it is complete, allowing
        :data:`ESCAPE_TIMEOUT` between bytes.  Only when nothing follows the
        initial ESC within that window is it reported as a real ``esc``
        keypress -- getting that wrong is what made arrow keys quit the app.
        """
        first = self._read_byte(timeout)
        if not first:
            return None if timeout is not None else "eof"

        buffer = bytearray(first)
        parsed = parse_key_bytes(bytes(buffer))
        if first == b"\x1b" and parsed is None:
            while True:
                nxt = self._read_byte(ESCAPE_TIMEOUT)
                if not nxt:
                    break
                buffer += nxt
                parsed = parse_key_bytes(bytes(buffer))
                if parsed is not None:
                    break
        if parsed is None:
            return "esc"
        return parsed[0]

    def read_key_timeout(self, timeout: float) -> Optional[str]:
        """Like :meth:`read_key` but returns None when nothing arrives."""
        return self.read_key(timeout)


class Screen:
    """Alternate-screen renderer: redraws whole frames, never scrolls."""

    def __init__(self, stream=None) -> None:
        self.stream = stream or sys.stdout
        self._entered = False

    def enter(self) -> None:
        if not self._entered:
            self.stream.write("\033[?1049h\033[?25l")  # alt screen, hide cursor
            self.stream.flush()
            self._entered = True

    def leave(self) -> None:
        if self._entered:
            self.stream.write("\033[?25h\033[?1049l")
            self.stream.flush()
            self._entered = False

    def draw(self, lines: Sequence[str], width: int, height: int) -> None:
        frame = []
        for index in range(height):
            line = lines[index] if index < len(lines) else ""
            frame.append("\033[K" + fit(line, width))
        body = "\033[H" + "\n".join(frame)
        self.stream.write(body)
        self.stream.flush()


def on_resize(callback) -> None:
    """Call ``callback`` when the terminal is resized (POSIX only)."""
    if not hasattr(signal, "SIGWINCH"):
        return
    try:
        signal.signal(signal.SIGWINCH, lambda *_: callback())
    except (ValueError, OSError):
        pass
