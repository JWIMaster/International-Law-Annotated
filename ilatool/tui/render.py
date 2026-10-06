"""Pure screen rendering.

Every function here takes a description of what should be on screen and
returns a list of lines.  Nothing touches the terminal, which is what makes
the interface testable: the test suite renders every view at widths from 20
to 200 columns and asserts that no line is ever wider than the terminal and
nothing is silently cut off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from . import term as T

#: Below this width the bordered layout is dropped for a plain one.
NARROW_WIDTH = 44


@dataclass
class MenuItem:
    key: str
    label: str
    hint: str = ""
    enabled: bool = True
    note: str = ""
    danger: bool = False


@dataclass
class StageStatus:
    label: str
    state: str = "pending"      # pending | running | done | failed | skipped
    detail: str = ""
    seconds: float = 0.0

    @property
    def symbol(self) -> str:
        return {
            "pending": "·",
            "running": "▶",
            "done": "✔",
            "failed": "✖",
            "skipped": "–",
        }.get(self.state, "·")


@dataclass
class View:
    kind: str = "menu"           # menu | text | progress | result | prompt
    title: str = "Annotated Text Builder"
    subtitle: str = ""
    status: List[Tuple[str, str]] = field(default_factory=list)
    items: List[MenuItem] = field(default_factory=list)
    selected: int = 0
    body: List[str] = field(default_factory=list)
    footer: str = ""
    message: str = ""
    message_kind: str = "info"   # info | ok | warn | error
    scroll: int = 0
    stages: List[StageStatus] = field(default_factory=list)
    spinner: str = ""
    elapsed: float = 0.0
    prompt: str = ""
    prompt_value: str = ""
    progress: Optional[float] = None
    dense: bool = False

    def selectable(self) -> List[int]:
        return [i for i, item in enumerate(self.items) if item.enabled]

    def move(self, delta: int) -> None:
        options = self.selectable()
        if not options:
            return
        if self.selected not in options:
            self.selected = options[0]
            return
        position = options.index(self.selected)
        self.selected = options[(position + delta) % len(options)]


_MESSAGE_STYLE = {
    "info": T.accent,
    "ok": T.ok,
    "warn": T.warn,
    "error": T.error,
}


def _header(view: View, width: int) -> List[str]:
    lines: List[str] = []
    lines.append(T.title(T.fit(view.title, width)))
    if view.subtitle:
        for piece in T.wrap(view.subtitle, width):
            lines.append(T.dim(piece))
    return lines


def _status_block(view: View, width: int) -> List[str]:
    if not view.status:
        return []
    label_width = min(18, max(6, max(T.visible_width(k) for k, _ in view.status)))
    lines: List[str] = []
    for label, value in view.status:
        head = T.fit(label, label_width)
        pad = " " * (label_width - T.visible_width(head))
        room = max(4, width - label_width - 2)
        wrapped = T.wrap(value or "—", room)
        if len(wrapped) == 1:
            lines.append(T.dim(head) + pad + "  " + wrapped[0])
        else:
            lines.append(T.dim(head) + pad + "  " + wrapped[0])
            for extra in wrapped[1:]:
                lines.append(" " * (label_width + 2) + T.dim(extra))
    return lines


def _message_block(view: View, width: int) -> List[str]:
    if not view.message:
        return []
    style = _MESSAGE_STYLE.get(view.message_kind, T.accent)
    prefix = {"info": "i", "ok": "✔", "warn": "!", "error": "✖"}.get(view.message_kind, "i")
    out: List[str] = []
    for index, line in enumerate(T.wrap(view.message, width - 2)):
        marker = prefix + " " if index == 0 else "  "
        out.append(style(marker + line))
    return out


def _menu_body(view: View, width: int, dense: bool = False) -> List[str]:
    lines: List[str] = []
    if dense:
        lines.append("")
        for index, item in enumerate(view.items):
            marker = ">" if index == view.selected else " "
            text = f"{marker} {item.key}) {item.label}"
            if not item.enabled:
                lines.append(T.dim(T.fit(text, width)))
            elif index == view.selected:
                lines.append(T.bold(T.fit(text, width)))
            else:
                lines.append(T.fit(text, width))
    else:
        for index, item in enumerate(view.items):
            cursor = "▸" if index == view.selected else " "
            label = f"{cursor} {item.key}) {item.label}"
            note = item.note
            if not item.enabled:
                line = T.dim(label)
                if note:
                    line += " " + T.dim(note)
            elif index == view.selected:
                line = T.bold(T.accent(label))
                if note:
                    line += "  " + T.dim(note)
            else:
                line = label
                if note:
                    line += "  " + T.dim(note)
            lines.append(T.fit(line, width))
            if item.hint and index == view.selected:
                for wrapped in T.wrap(item.hint, max(4, width - 4)):
                    lines.append("    " + T.dim(wrapped))
    return lines


def _progress_body(view: View, width: int) -> List[str]:
    lines: List[str] = []
    for stage in view.stages:
        if stage.state == "running":
            symbol = view.spinner or "▶"
            colour = T.accent
        elif stage.state == "done":
            symbol, colour = "✔", T.ok
        elif stage.state == "failed":
            symbol, colour = "✖", T.error
        elif stage.state == "skipped":
            symbol, colour = "–", T.dim
        else:
            symbol, colour = "·", T.dim
        timing = f"{stage.seconds:5.1f}s" if stage.seconds else ""
        detail = stage.detail
        room = width - 4 - len(timing)
        if detail:
            head = T.fit(f"{symbol} {stage.label}", max(10, room // 2))
            rest = T.fit(detail, max(0, room - T.visible_width(head) - 2))
            line = colour(f"{head}  {T.dim(rest)}")
        else:
            line = colour(f"{symbol} {stage.label}")
        lines.append(T.fit(line, width))
    if view.progress is not None:
        lines.append("")
        lines.append(_bar(view.progress, width))
    if view.elapsed:
        lines.append("")
        lines.append(T.dim(f"  elapsed {view.elapsed:5.1f}s"))
    return lines


def _bar(fraction: float, width: int) -> str:
    fraction = max(0.0, min(1.0, fraction))
    room = max(4, width - 8)
    filled = int(room * fraction)
    return "  " + T.accent("█" * filled) + T.dim("░" * (room - filled)) + f" {fraction:4.0%}"


def _wrap_lines(lines: Sequence[str], width: int) -> List[str]:
    out: List[str] = []
    for line in lines:
        if T.visible_width(line) <= width:
            out.append(line)
            continue
        for piece in T.wrap(T.strip_ansi(line), width):
            out.append(piece)
    return out


def _paged(body: List[str], height: int, scroll: int) -> List[str]:
    if height <= 0:
        return []
    if len(body) <= height:
        return body
    start = max(0, min(scroll, len(body) - height))
    return body[start:start + height]


def render_view(view: View, width: int, height: int) -> List[str]:
    """Render ``view`` into exactly ``height`` lines of at most ``width``."""
    width = max(20, width)
    height = max(6, height)
    dense = view.dense or width < NARROW_WIDTH

    header = _header(view, width) if not dense else [T.bold(T.fit(view.title, width))]
    status = _status_block(view, width)
    message = _message_block(view, width)

    if view.kind == "progress":
        body = _progress_body(view, width)
    elif view.kind == "menu":
        body = _menu_body(view, width, dense)
    else:
        body = _wrap_lines(view.body, width)

    footer: List[str] = []
    if view.prompt:
        prompt_lines = T.wrap(f"{view.prompt}: {view.prompt_value}▏", width)
        footer.extend(prompt_lines)
    elif view.footer:
        footer.extend(T.wrap(view.footer, width))

    fixed = len(header) + len(status) + len(message) + len(footer)
    avail = height - fixed
    if avail < 1:
        # Too much chrome for this window: drop the status block first, then
        # the header, so the *content* is what survives.
        status = []
        fixed = len(header) + len(message) + len(footer)
        avail = height - fixed
    if avail < 1:
        header = []
        fixed = len(message) + len(footer)
        avail = height - fixed
    if avail < 1:
        message = []
        footer = footer[:1]
        avail = max(1, height - len(header) - len(footer))

    body = _paged(body, avail, view.scroll)

    lines = header + status
    if message:
        lines.extend(message)
    lines.extend(body)
    lines.extend(footer)
    # Right-align a scroll indicator onto the last content line when needed.
    out = [T.fit(line, width) for line in lines[:height]]
    while len(out) < height:
        out.append("")
    return out


def render_scrollable(view: View, width: int, height: int) -> List[str]:
    """Like :func:`render_view` but adds a position indicator."""
    lines = render_view(view, width, height)
    total = len(view.body) if view.kind != "menu" else len(view.items)
    if total > height:
        position = f"{min(view.scroll + 1, total)}–{min(view.scroll + height, total)}/{total}"
        if lines:
            lines[-1] = T.columns(lines[-1], T.dim(position), width)
    return lines
