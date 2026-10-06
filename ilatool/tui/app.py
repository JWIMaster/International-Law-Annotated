"""The interactive terminal interface.

Design notes, because the previous interface's problems all came from
missing them:

* **Nothing is printed directly.**  A "view" is a data structure; the screen
  is redrawn whole from it on every event and on every resize.  That removes
  the entire class of bugs where a spinner, a log line and a panel fight over
  the same rows.
* **Long operations run on a worker thread** and report through the
  pipeline's progress callback, so the interface keeps painting, says which
  stage is running, and can be cancelled with Ctrl-C without leaving a
  half-written file behind.
* **Failures are shown as failures**, with the stage, the explanation and a
  suggested next step, plus retry or back.  Technical detail goes to the
  build report rather than being sprayed over the screen.
* **Everything degrades**: a terminal narrower than 44 columns switches to a
  plain layout, and a non-interactive stdin/stdout falls back to a
  line-oriented mode so the tool still works in a script or a pipe.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .. import allhtml, annotations as ann_mod, backends, docx as docx_mod
from .. import pipeline, sourcefmt
from ..errors import BuildCancelled, InputError, InternalError, Stage, ToolError
from . import render, term as T
from .render import MenuItem, StageStatus, View

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

STAGE_ORDER = [
    Stage.INPUT, Stage.LOAD, Stage.PARSE, Stage.LAYOUT, Stage.STRUCTURE,
    Stage.ANNOTATIONS, Stage.MATCHING, Stage.RENDER, Stage.WRITE, Stage.VALIDATE,
]

MODE_MENU = "menu"
MODE_VIEW = "view"
MODE_BUSY = "busy"
MODE_ERROR = "error"


@dataclass
class AppState:
    source: str = ""
    annotations: str = ""
    out_html: str = ""
    out_notes: str = ""
    meta: Dict[str, str] = field(default_factory=dict)
    add_to_all_html: bool = False
    annotations_is_js: bool = False

    def to_json(self) -> Dict[str, Any]:
        return {
            "source_txt": self.source,
            "annotations": self.annotations,
            "out_html": self.out_html,
            "out_notes": self.out_notes,
            "meta": self.meta,
            "add_to_all_html": self.add_to_all_html,
            "annotations_is_js": self.annotations_is_js,
        }

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "AppState":
        return cls(
            source=data.get("source_txt", "") or "",
            annotations=data.get("annotations", "") or "",
            out_html=data.get("out_html", "") or "",
            out_notes=data.get("out_notes", "") or "",
            meta=dict(data.get("meta", {}) or {}),
            add_to_all_html=bool(data.get("add_to_all_html", False)),
            annotations_is_js=bool(data.get("annotations_is_js", False)),
        )


class InteractiveApp:
    """Full-screen, keyboard-driven interface."""

    def __init__(self, state: AppState, state_path: Path, work_dir: Path) -> None:
        self.state = state
        self.state_path = state_path
        self.work_dir = work_dir
        self.screen = T.Screen()
        self.raw = T.RawInput()
        self.view = View()
        self.mode = MODE_MENU
        self.quit = False
        self.scroll = 0
        self.spinner_index = 0
        self.message = ""
        self.message_kind = "info"
        self.last_report: Optional[pipeline.RunReport] = None
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._result: Dict[str, Any] = {}
        self._progress: Dict[str, Any] = {}
        self._on_done: Optional[Callable[[pipeline.RunReport], None]] = None
        self._retry: Optional[Callable[[], None]] = None
        self._on_choose: Dict[str, Callable[[], None]] = {}
        self._resize = False
        T.on_resize(self._on_resize)

    # -- plumbing -------------------------------------------------------------

    def _on_resize(self) -> None:
        self._resize = True

    def save(self) -> None:
        try:
            self.state_path.write_text(json.dumps(self.state.to_json(), indent=2),
                                       encoding="utf-8")
        except OSError:
            pass

    def note(self, message: str, kind: str = "info") -> None:
        self.message = message
        self.message_kind = kind

    def draw(self) -> None:
        width, height = T.terminal_size()
        self.view.message = self.message
        self.view.message_kind = self.message_kind
        self.view.scroll = self.scroll
        lines = render.render_scrollable(self.view, width, height)
        self.screen.draw(lines, width, height)

    def run(self) -> None:
        self.screen.enter()
        try:
            with self.raw:
                self.show_menu()
                while not self.quit:
                    self.draw()
                    if self.mode == MODE_BUSY:
                        key = self.raw.read_key_timeout(0.08)
                        self.spinner_index = (self.spinner_index + 1) % len(SPINNER)
                        self._poll_job()
                        if key == "ctrl-c":
                            self._cancel.set()
                            self.note("Cancelling — stopping at the next safe point…", "warn")
                        continue
                    key = self.raw.read_key()
                    if key == "eof":
                        self.quit = True
                        break
                    self.handle_key(key)
        except KeyboardInterrupt:
            if self._thread and self._thread.is_alive():
                self._cancel.set()
        finally:
            self.screen.leave()
            print(T.dim("Bye."))

    def handle_key(self, key: str) -> None:
        if self._resize:
            self._resize = False
        if key == "ctrl-c":
            if self.mode == MODE_MENU:
                self.quit = True
            else:
                self.show_menu()
            return
        if key in ("esc", "q") and self.mode != MODE_MENU:
            self.show_menu()
            return
        # In the menu, only "q" (or Ctrl-C) quits.  Escape deliberately does
        # nothing: a terminal that dribbles an arrow key's bytes out slowly
        # can still deliver a bare ESC, and that must never end the session.
        if key == "q" and self.mode == MODE_MENU:
            self.quit = True
            return
        if key == "esc" and self.mode == MODE_MENU:
            self.message = ""
            return
        if key in ("up", "k"):
            self.view.move(-1)
            return
        if key in ("down", "j"):
            self.view.move(1)
            return
        if key == "pgup":
            self.scroll = max(0, self.scroll - 10)
            return
        if key == "pgdn":
            self.scroll += 10
            return
        if key == "home":
            self.scroll = 0
            return
        if key == "end":
            self.scroll = 10 ** 6
            return
        if key in ("enter", " "):
            self.activate()
            return
        if len(key) == 1 and key.isdigit():
            for index, item in enumerate(self.view.items):
                if item.key == key and item.enabled:
                    self.view.selected = index
                    self.activate()
                    return

    def activate(self) -> None:
        items = self.view.items
        if self.mode == MODE_ERROR:
            if self.view.selected == 0 and self._retry:
                retry, self._retry = self._retry, None
                retry()
            else:
                self.show_menu()
            return
        if not items or self.view.selected >= len(items):
            return
        item = items[self.view.selected]
        if not item.enabled:
            self.note(item.note or "That step is not available.", "warn")
            return
        action = self._on_choose.pop(item.key, None) if self._on_choose else None
        if action is not None:
            action()
            return
        if self.mode == MODE_VIEW:
            if item.key == "b":
                self.show_menu()
            elif item.key == "7":
                self.action_diagnostics()
            elif item.key == "a":
                self.action_all_html()
            elif item.key == "r" and self._retry:
                retry, self._retry = self._retry, None
                retry()
            return
        self.dispatch(item.key)

    def dispatch(self, key: str) -> None:
        handlers = {
            "1": self.action_source,
            "2": self.action_annotations,
            "3": self.action_generate,
            "4": self.action_format_help,
            "5": self.action_toggle_all_html,
            "6": self.action_backends,
            "7": self.action_diagnostics,
            "0": self.action_quit,
        }
        handler = handlers.get(key)
        if handler:
            handler()

    # -- screens --------------------------------------------------------------

    def _present(self, path: str) -> str:
        if not path:
            return "not set"
        return path if Path(path).exists() else f"{path}  (missing)"

    def show_menu(self) -> None:
        state = self.state
        source_ok = bool(state.source) and Path(state.source).exists()
        ann_ok = bool(state.annotations) and Path(state.annotations).exists()
        can_generate = source_ok and ann_ok
        items = [
            MenuItem(key="1", label="Source text", note=self._present(state.source),
                     hint="Convert a PDF, or point at an existing source .txt"),
            MenuItem(key="2", label="Annotations", note=self._present(state.annotations),
                     hint="A note file (text/JSON/CSV), a .docx's comments, a PDF's own "
                          "markup, or an existing notes.js"),
            MenuItem(key="3", label="Generate the annotated page", enabled=can_generate,
                     note="" if can_generate else "set the source and annotations first",
                     hint="Build the page and its notes.js, then check them"),
            MenuItem(key="4", label="Show the annotation formats"),
            MenuItem(key="5", label="Add a card to texts/all.html",
                     note="on" if state.add_to_all_html else "off",
                     hint="Toggle: update all.html automatically after generating"),
            MenuItem(key="6", label="PDF backend status"),
            MenuItem(key="7", label="Last build report", enabled=bool(self.last_report),
                     hint="Everything that happened, including warnings"),
            MenuItem(key="0", label="Quit"),
        ]
        self.mode = MODE_MENU
        self.scroll = 0
        self.view = View(
            kind="menu",
            title="Annotated Text Builder",
            subtitle="PDF or text \u2192 structured paragraphs \u2192 annotated website",
            status=[("Source", self._present(state.source)),
                    ("Annotations", self._present(state.annotations)),
                    ("Output", state.out_html or "texts/<name>.html")],
            items=items,
            footer="↑/↓ move · Enter select · 1-7 shortcuts · q quit",
        )
        self.view.selected = 0

    def show_text(self, title: str, body: List[str], footer: str = "↑/↓ scroll · Esc back",
                  back_key: str = "b",
                  on_choose: Optional[Dict[str, Callable[[], None]]] = None,
                  items: Optional[List[MenuItem]] = None) -> None:
        self.mode = MODE_VIEW
        self.scroll = 0
        self._on_choose = dict(on_choose or {})
        self.view = View(kind="text", title=title, body=body,
                         items=items or [MenuItem(key=back_key, label="Back")],
                         footer=footer)
        self.view.selected = 0

    def action_quit(self) -> None:
        self.quit = True

    # -- informational screens ------------------------------------------------

    def action_backends(self) -> None:
        status = backends.backend_status()
        body = [T.bold("PDF readers available to this tool"), ""]
        for name, info in status.items():
            mark = "✔" if info["available"] else "✖"
            line = f"{mark} {name:10s} {info['detail']}"
            body.append(T.ok(line) if info["available"] else T.warn(line))
        body.append("")
        body.extend(T.wrap(
            "PyMuPDF gives exact coordinates, native annotations and correct "
            "handling of rotated pages. The poppler fallback works anywhere "
            "but approximates geometry on rotated pages.", 72))
        self.show_text("PDF backends", body)

    def action_format_help(self) -> None:
        body = ann_mod.ANN_FORMAT_HELP.strip("\n").split("\n")
        self.show_text("Annotation formats", body)

    def action_diagnostics(self) -> None:
        report = self.last_report
        if not report:
            self.note("No build has been run yet.", "warn")
            return
        body: List[str] = []
        if report.error:
            body.append(T.error(f"Failed: {report.error.message}"))
            if report.error.hint:
                body.extend(T.wrap(report.error.hint, 72))
            body.append("")
        body.append(T.bold("Stages"))
        for label, seconds in report.stages:
            body.append(f"  {label:<34} {seconds:6.2f}s")
        if report.result and report.result.validation:
            body.append("")
            body.append(T.bold("Checks"))
            for check in report.result.validation.checks:
                mark = {"ok": "✔", "warn": "!", "fail": "✖"}.get(check.status, "·")
                colour = {"ok": T.ok, "warn": T.warn, "fail": T.error}.get(check.status, T.dim)
                body.append(colour(f"  {mark} {check.name}") +
                            (T.dim(f"  {check.detail}") if check.detail else ""))
        body.append("")
        body.append(T.bold("Notices"))
        for diag in report.diagnostics:
            colour = {"info": T.dim, "warning": T.warn, "error": T.error}[diag.severity]
            body.append(colour(f"  [{diag.severity}] {diag.message}"))
            if diag.hint:
                body.extend(T.wrap(diag.hint, 68, subsequent_indent="      ")[:2])
        if report.result and report.result.match.unplaced:
            body.append("")
            body.append(T.bold("Annotations that could not be attached"))
            for ann, why in report.result.match.unplaced[:25]:
                label = ann.para or (ann.quote[:44] + "…" if ann.quote else "(no target)")
                body.append(T.warn(f"  {label}"))
                body.append(T.dim(f"      {why}"))
        if report.diagnostics_path:
            body.append("")
            body.append(T.dim(f"Full report: {report.diagnostics_path}"))
        self.show_text("Last build report", body)

    def action_toggle_all_html(self) -> None:
        self.state.add_to_all_html = not self.state.add_to_all_html
        self.save()
        self.note("Cards will be added to texts/all.html after each build."
                  if self.state.add_to_all_html else
                  "Automatic all.html updates are off.", "ok")
        self.show_menu()

    # -- source ---------------------------------------------------------------

    def action_source(self) -> None:
        value = self.ask("Path to a PDF, or an existing source .txt")
        if value is None:
            return
        path = Path(os.path.expanduser(value.strip().strip("'\"")))
        if not path.exists():
            self.note(f"No file at {path}", "error")
            return
        if path.suffix.lower() == ".pdf":
            self.source_pdf_flow(path)
            return
        try:
            source = sourcefmt.parse_source(path)
        except ToolError as exc:
            self.show_error(exc)
            return
        self.state.source = str(path)
        self.state.meta = dict(source.meta)
        self.save()
        self.show_menu()
        self.note(f"Using {path.name}: {len(source.paragraphs)} paragraph(s)", "ok")

    def source_pdf_flow(self, pdf_path: Path) -> None:
        try:
            details = backends.probe_pdf(pdf_path)
        except Exception as exc:  # pragma: no cover - defensive
            details = {}
        meta = dict(self.state.meta)
        default_title = (meta.get("TITLE") or pdf_path.stem.replace("_", " ").title())
        title = self.ask("Document title", default_title)
        if title is None:
            return
        header = self.ask("Short header text", meta.get("HEADER") or title)
        if header is None:
            return
        desc = self.ask("Description (optional)", meta.get("DESC", ""))
        if desc is None:
            return
        home = self.ask("Home link", meta.get("HOME", "../index.html"))
        if home is None:
            return
        default_source = pdf_path.with_name(pdf_path.stem + ".txt")
        out = self.ask("Write the source text to", str(default_source))
        if out is None:
            return
        out_source = Path(os.path.expanduser(out))
        meta = {"TITLE": title, "HEADER": header, "DESC": desc, "HOME": home}

        body = [
            f"PDF           {pdf_path}",
            f"Pages         {details.get('page_count', '?')}",
            f"Rotation      {details.get('rotations') or 'none'}",
            f"PDF markup    {details.get('annotation_count', 0)} annotation(s)",
            "",
            f"Source text   {out_source}",
        ]
        if out_source.exists():
            body.append(T.warn("This file exists: notes written against its "
                               "paragraph ids will be carried over."))

        def go() -> None:
            request = pipeline.RunRequest(
                source=pdf_path, annotations=None, out_source=out_source, meta=meta,
                build_site=False,          # converting must not emit a page
                convert=pipeline.ConvertOptions(
                    keep_aliases_from=out_source if out_source.exists() else None),
                build=pipeline.BuildOptions(validate=False, write_diagnostics=False),
            )
            self.run_job(f"Converting {pdf_path.name}", request, self._after_convert,
                         retry=lambda: self.source_pdf_flow(pdf_path))

        self.show_text(
            "Convert PDF — review", body,
            footer="↑/↓ choose · Enter select · Esc cancel",
            items=[MenuItem(key="c", label="Convert this PDF"),
                   MenuItem(key="b", label="Cancel")],
            on_choose={"c": go})

    def _after_convert(self, report: pipeline.RunReport) -> None:
        conversion = report.conversion
        if conversion is None:
            return
        self.state.source = str(report.source_path)
        self.state.meta = dict(conversion.source.meta)
        self.save()
        stats = conversion.stats
        body = [
            T.ok(f"Wrote {report.source_path}"),
            "",
            f"  pages             {stats['pages']}",
            f"  paragraphs        {stats['paragraphs']}",
            f"  headings          {stats['headings']}",
            f"  running headers   {stats['furniture_removed']} removed",
            f"  front matter      {stats['front_matter_dropped']} line(s) skipped",
            f"  text repairs      {stats['text_repairs'] or 'none needed'}",
        ]
        if report.json_path:
            body.append(T.dim(f"  layout sidecar    {report.json_path}"))
        body.append("")
        body.append(T.dim("Open the source text to fix paragraph breaks or add {key} tags."))
        for diag in report.diagnostics:
            if diag.severity != "info":
                body.append(T.warn(f"! {diag.message}"))
        self.show_text("Converted", body, footer="Esc back to the menu")

    # -- annotations ----------------------------------------------------------

    def action_annotations(self) -> None:
        value = self.ask("Path to annotations (.txt/.json/.csv/.docx/.pdf/.js)")
        if value is None:
            return
        path = Path(os.path.expanduser(value.strip().strip("'\"")))
        if not path.exists():
            self.note(f"No file at {path}", "error")
            return
        self.state.annotations = str(path)
        self.state.annotations_is_js = path.suffix.lower() == ".js"
        self.save()
        self.preview_annotations(path)

    def preview_annotations(self, path: Path) -> None:
        try:
            if path.suffix.lower() == ".docx":
                items = docx_mod.annotations_from_docx(path)
            elif path.suffix.lower() == ".pdf":
                items = ann_mod.annotations_from_pdf(backends.load_pdf(path))
            else:
                items = ann_mod.load_annotations(path)
        except ToolError as exc:
            self.show_error(exc)
            return

        body = [T.bold(f"{len(items)} annotation(s) in {path.name}"), ""]
        for ann in items[:80]:
            author = (ann.author or "unknown")[:18]
            anchor = ann.para or (f"p{ann.page}" if ann.page else "") or "excerpt"
            snippet = (ann.text or ann.quote or "").replace("\n", " ")
            body.append(f"  {author:<18} {anchor:<9} {T.fit(snippet, 60)}")
        if len(items) > 80:
            body.append(T.dim(f"  … and {len(items) - 80} more"))

        source_path = Path(self.state.source) if self.state.source else None
        if source_path and source_path.exists():
            try:
                source = sourcefmt.parse_source(source_path)
                result = ann_mod.attach(items, source)
                body.append("")
                body.append(T.ok(f"  {len(result.attached)} will attach to a paragraph")
                            if result.attached else T.warn("  none attach to a paragraph yet"))
                if result.unplaced:
                    body.append(T.warn(f"  {len(result.unplaced)} unplaced:"))
                    for ann, why in result.unplaced[:10]:
                        label = ann.para or (ann.quote[:36] + "…" if ann.quote else "?")
                        body.append(T.dim(f"    {label}: {why}"))
            except ToolError as exc:
                body.append(T.warn(f"  (could not check against the source: {exc.message})"))
        self.show_text("Annotations", body)

    # -- generate -------------------------------------------------------------

    def action_generate(self) -> None:
        if not (self.state.source and self.state.annotations):
            self.note("Set the source text and annotations first.", "warn")
            return
        source_path = Path(self.state.source)
        texts_dir = self.work_dir / "texts"
        default_html = (Path(self.state.out_html) if self.state.out_html
                        else texts_dir / (source_path.stem + ".html"))
        value = self.ask("Output HTML path", str(default_html))
        if value is None:
            return
        out_html = Path(os.path.expanduser(value.strip()))
        out_notes = out_html.with_name(out_html.stem + "-notes.js")
        if out_html.exists() or out_notes.exists():
            if not self.ask_yes_no(f"{out_html.name} already exists. Overwrite?", False):
                self.show_menu()
                self.note("Cancelled — nothing was overwritten.")
                return
        self.state.out_html = str(out_html)
        self.state.out_notes = str(out_notes)
        self.save()

        def go() -> None:
            request = pipeline.RunRequest(
                source=source_path, annotations=Path(self.state.annotations),
                out_html=out_html, out_notes=out_notes, meta=dict(self.state.meta))
            self.run_job("Building the page", request, self._after_build,
                         retry=self.action_generate)

        go()

    def _after_build(self, report: pipeline.RunReport) -> None:
        self.last_report = report
        result = report.result
        if result is None:
            return
        stats = result.stats
        body = [
            T.ok(f"Wrote {result.html_path}"),
            T.ok(f"Wrote {result.notes_path}"),
            "",
            f"  paragraphs           {stats['paragraphs']}",
            f"  headings             {stats['headings']}",
            f"  paragraphs annotated {stats['annotated']}",
            f"  notes                {stats['notes']}",
            f"  authors              {stats['authors']}",
        ]
        if stats["unplaced"]:
            body.append(T.warn(f"  unplaced annotations {stats['unplaced']}"))
        if result.validation:
            body.append("")
            body.append(T.bold("Checks"))
            for check in result.validation.checks:
                mark = {"ok": "✔", "warn": "!", "fail": "✖"}.get(check.status, "·")
                colour = {"ok": T.ok, "warn": T.warn, "fail": T.error}.get(check.status, T.dim)
                body.append(colour(f"  {mark} {check.name}") +
                            (T.dim(f"  {check.detail}") if check.detail else ""))
        notices = [d for d in report.diagnostics if d.severity != "info"]
        if notices:
            body.append("")
            for diag in notices[:8]:
                body.append(T.warn(f"! {diag.message}"))
        if report.diagnostics_path:
            body.append("")
            body.append(T.dim(f"Report: {report.diagnostics_path}"))

        items = [MenuItem(key="a", label="Add or update the all.html card"),
                 MenuItem(key="7", label="Show the full report"),
                 MenuItem(key="b", label="Back to the menu")]
        self.mode = MODE_VIEW
        self.scroll = 0
        self._on_choose = {}
        self.view = View(kind="result", title="Built", body=body, items=items,
                         footer="Enter select · Esc back")
        self.view.selected = 0

    def action_all_html(self) -> None:
        report = self.last_report
        result = report.result if report else None
        if result is None or result.html_path is None:
            self.note("Nothing has been built yet.", "warn")
            return
        texts_dir = Path(result.html_path).parent
        all_html_path = texts_dir / "all.html"
        try:
            text = all_html_path.read_text(encoding="utf-8")
        except OSError as exc:
            self.show_error(InputError(f"could not read {all_html_path}", cause=exc))
            return
        href = f"./{Path(result.html_path).name}"
        title = (result.source.meta.get("HEADER") or result.source.meta.get("TITLE")
                 or Path(result.html_path).stem)
        desc = result.source.meta.get("DESC", "")
        try:
            existing = next((c for c in allhtml.find_all_html_cases(text)
                             if c["href"] == href), None)
            if existing:
                text = allhtml.replace_case_in_all_html(text, href, title,
                                                        existing["subtitle"], desc)
            else:
                text = allhtml.add_case_to_all_html(text, title, "", desc, href)
            all_html_path.write_text(text, encoding="utf-8")
        except (OSError, ValueError) as exc:
            self.show_error(InputError(f"could not update {all_html_path.name}", cause=exc))
            return
        self.note(f"Updated {all_html_path.name} with a card for “{title}”.", "ok")

    # -- prompts --------------------------------------------------------------

    def ask(self, label: str, default: str = "") -> Optional[str]:
        buffer = default
        while True:
            self.view.prompt = label
            self.view.prompt_value = buffer
            self.draw()
            key = self.raw.read_key()
            if key == "enter":
                self.view.prompt = ""
                return buffer
            if key in ("esc", "ctrl-c", "eof"):
                self.view.prompt = ""
                return None
            if key == "backspace":
                buffer = buffer[:-1]
            elif key == "ctrl-u":
                buffer = ""
            elif len(key) == 1 and key.isprintable():
                buffer += key

    def ask_yes_no(self, question: str, default: bool = False) -> bool:
        suffix = " [Y/n]" if default else " [y/N]"
        answer = self.ask(question + suffix, "")
        if answer is None:
            return False
        answer = answer.strip().lower()
        if not answer:
            return default
        return answer.startswith("y")

    # -- jobs -----------------------------------------------------------------

    def run_job(self, label: str, request: pipeline.RunRequest,
                on_done: Callable[[pipeline.RunReport], None],
                retry: Optional[Callable[[], None]] = None) -> None:
        self._progress = {
            "stage": None,
            "fraction": None,
            "message": "starting",
            "stages": {stage: StageStatus(label=stage.label) for stage in STAGE_ORDER},
            "started": time.time(),
        }
        self._result = {}
        self._cancel.clear()
        self._on_done = on_done
        self._retry = retry
        self.mode = MODE_BUSY
        self.message = ""
        self.message_kind = "info"
        self.view = View(kind="progress", title=label,
                         subtitle="starting", footer="Ctrl-C cancels")
        self._started = time.time()

        def progress(stage: Stage, fraction: Optional[float], message: str) -> None:
            if self._cancel.is_set():
                raise BuildCancelled()
            entry = self._progress
            previous_stage = entry.get("stage")
            if previous_stage is not None and previous_stage != stage:
                previous = entry["stages"].get(previous_stage)
                if previous is not None and previous.state == "running":
                    previous.state = "done"
                    previous.seconds = time.time() - entry.get("stage_started", time.time())
                entry["stage_started"] = time.time()
            entry["stage"] = stage
            entry["fraction"] = fraction
            entry["message"] = message
            entry.setdefault("stage_started", time.time())
            status = entry["stages"].get(stage)
            if status is not None:
                status.state = "running"
                status.detail = message

        def worker() -> None:
            try:
                self._result["report"] = pipeline.run(request, progress)
            except BaseException as exc:  # pragma: no cover - safety net
                self._result["error"] = exc
            finally:
                self._result["finished"] = True

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()

    def _poll_job(self) -> None:
        entry = self._progress
        self.view.stages = [entry["stages"][s] for s in STAGE_ORDER if s in entry["stages"]]
        self.view.spinner = SPINNER[self.spinner_index]
        self.view.elapsed = time.time() - self._started
        self.view.progress = entry.get("fraction")
        self.view.subtitle = str(entry.get("message") or "")
        if self._thread is not None and not self._thread.is_alive():
            self._finish_job()

    def _finish_job(self) -> None:
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.0)
        report = self._result.get("report")
        exc = self._result.get("error")
        if report is None and exc is not None:
            wrapped = exc if isinstance(exc, ToolError) else InternalError.wrap(exc, "running the build")
            report = pipeline.RunReport(error=wrapped)
            report.diagnostics.error(wrapped.stage, wrapped.message)
        if report is None:
            report = pipeline.RunReport(error=InternalError("the build produced no result"))
        self.mode = MODE_VIEW
        if report.error is not None:
            self.last_report = report
            self.show_error(report.error, retry=self._retry)
            return
        callback, self._on_done = self._on_done, None
        if callback is not None:
            callback(report)

    def show_error(self, exc: ToolError, retry: Optional[Callable[[], None]] = None) -> None:
        body = [T.error(f"{exc.title} failed"), ""]
        body.extend(T.wrap(exc.message, 72))
        if exc.hint:
            body.append("")
            body.append(T.bold("What to do"))
            body.extend(T.wrap(exc.hint, 70, subsequent_indent="  "))
        if exc.detail:
            body.append("")
            body.append(T.dim("Technical detail"))
            for line in str(exc.detail).strip().splitlines()[:14]:
                body.append(T.dim("  " + line[:100]))
        items = [MenuItem(key="r", label="Try again", enabled=retry is not None),
                 MenuItem(key="b", label="Back to the menu")]
        self.mode = MODE_ERROR
        self.scroll = 0
        self._retry = retry
        self.view = View(kind="result", title="Something went wrong", body=body,
                         items=items, footer="↑/↓ choose · Enter select · Esc back")
        self.view.selected = 0 if retry else 1


# ---------------------------------------------------------------------------
# Non-interactive fallback
# ---------------------------------------------------------------------------

class PlainApp:
    """Line-oriented interface for pipes, logs and dumb terminals.

    Same capabilities, no full-screen drawing: each prompt is a single line
    of output, and every message is wrapped to the terminal width so a
    narrow window still reads sensibly.
    """

    def __init__(self, state: AppState, state_path: Path, work_dir: Path) -> None:
        self.state = state
        self.state_path = state_path
        self.work_dir = work_dir
        self.last_report: Optional[pipeline.RunReport] = None

    def _out(self, text: str = "") -> None:
        width, _ = T.terminal_size()
        for line in str(text).split("\n"):
            if T.visible_width(line) <= width:
                print(line)
            else:
                for piece in T.wrap(T.strip_ansi(line), width):
                    print(piece)

    def save(self) -> None:
        try:
            self.state_path.write_text(json.dumps(self.state.to_json(), indent=2),
                                       encoding="utf-8")
        except OSError:
            pass

    def run(self) -> None:
        while True:
            self.show_menu()
            try:
                choice = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if choice == "0":
                self._out(T.dim("Bye."))
                return
            if choice == "1":
                self.prompt_source()
            elif choice == "2":
                self.prompt_annotations()
            elif choice == "3":
                self.generate()
            elif choice == "4":
                self._out(ann_mod.ANN_FORMAT_HELP)
            elif choice == "5":
                self.state.add_to_all_html = not self.state.add_to_all_html
                self.save()
            elif choice == "6":
                for name, info in backends.backend_status().items():
                    self._out(f"  {name:10s} {'available' if info['available'] else 'unavailable'}"
                              f"  {info['detail']}")
            elif choice == "7":
                if self.last_report:
                    for diag in self.last_report.diagnostics:
                        self._out(f"  [{diag.severity}] {diag.message}")
                else:
                    self._out("No build has been run yet.")
            else:
                self._out(T.error("Not a valid option."))

    def show_menu(self) -> None:
        width, _ = T.terminal_size()
        lines = [
            "",
            T.title("Annotated Text Builder"),
            T.dim("-" * min(width, 64)),
            f" 1. Source text : {self.state.source or '(not set)'}",
            f" 2. Annotations : {self.state.annotations or '(not set)'}",
            f" 3. Generate    : {self.state.out_html or '(not set yet)'}",
            " 4. Formats     : show the annotation formats",
            f" 5. all.html    : {'ON' if self.state.add_to_all_html else 'off'}",
            " 6. Backends    : which PDF readers are available",
            f" 7. Last report : {len(self.last_report.diagnostics) if self.last_report else 0} notice(s)",
            " 0. Quit",
        ]
        # Everything goes through _out so a long path wraps instead of
        # running off the edge of a narrow terminal.
        self._out("\n".join(lines))

    def prompt_source(self) -> None:
        raw = input("Path to a PDF or source .txt (blank to cancel): ").strip()
        if not raw:
            return
        path = Path(os.path.expanduser(raw.strip("'\"")))
        if not path.exists():
            self._out(T.error(f"No file at {path}"))
            return
        if path.suffix.lower() == ".pdf":
            title = input("Title: ").strip() or path.stem.replace("_", " ").title()
            out = input(f"Source text path [{path.with_name(path.stem + '.txt')}]: ").strip()
            out_path = Path(out) if out else path.with_name(path.stem + ".txt")
            request = pipeline.RunRequest(
                source=path, out_source=out_path, build_site=False,
                meta={"TITLE": title, "HEADER": title, "HOME": "../index.html"},
                convert=pipeline.ConvertOptions(
                    keep_aliases_from=out_path if out_path.exists() else None),
                build=pipeline.BuildOptions(validate=False, write_diagnostics=False))
            report = pipeline.run(request, self.progress)
            if report.error:
                self._out(T.error(f"{report.error.title}: {report.error.message}"))
                if report.error.hint:
                    self._out(T.dim(report.error.hint))
                return
            self.state.source = str(report.source_path)
            self.state.meta = dict(report.conversion.source.meta)
            self.save()
            self._out(T.ok(f"Wrote {report.source_path}"))
            for diag in report.diagnostics:
                self._out(f"  [{diag.severity}] {diag.message}")
        else:
            try:
                source = sourcefmt.parse_source(path)
            except ToolError as exc:
                self._out(T.error(exc.message))
                return
            self.state.source = str(path)
            self.state.meta = dict(source.meta)
            self.save()
            self._out(T.ok(f"Using {path.name} ({len(source.paragraphs)} paragraphs)"))

    def prompt_annotations(self) -> None:
        raw = input("Path to annotations (blank to cancel): ").strip()
        if not raw:
            return
        path = Path(os.path.expanduser(raw.strip("'\"")))
        if not path.exists():
            self._out(T.error(f"No file at {path}"))
            return
        self.state.annotations = str(path)
        self.state.annotations_is_js = path.suffix.lower() == ".js"
        self.save()
        try:
            if path.suffix.lower() == ".docx":
                items = docx_mod.annotations_from_docx(path)
            elif path.suffix.lower() == ".pdf":
                items = ann_mod.annotations_from_pdf(backends.load_pdf(path))
            else:
                items = ann_mod.load_annotations(path)
        except ToolError as exc:
            self._out(T.error(exc.message))
            return
        self._out(T.ok(f"{len(items)} annotation(s)"))
        for ann in items[:20]:
            self._out(f"  {ann.author or 'unknown':<18} {(ann.text or ann.quote)[:60]}")

    def generate(self) -> None:
        if not (self.state.source and self.state.annotations):
            self._out(T.error("Set the source and annotations first."))
            return
        source_path = Path(self.state.source)
        default = self.work_dir / "texts" / (source_path.stem + ".html")
        raw = input(f"Output HTML path [{default}]: ").strip()
        out_html = Path(os.path.expanduser(raw)) if raw else default
        out_notes = out_html.with_name(out_html.stem + "-notes.js")
        if out_html.exists():
            answer = input(f"{out_html.name} exists. Overwrite? [y/N]: ").strip().lower()
            if not answer.startswith("y"):
                self._out("Cancelled.")
                return
        request = pipeline.RunRequest(
            source=source_path, annotations=Path(self.state.annotations),
            out_html=out_html, out_notes=out_notes, meta=dict(self.state.meta))
        report = pipeline.run(request, self.progress)
        self.last_report = report
        self.state.out_html = str(out_html)
        self.state.out_notes = str(out_notes)
        self.save()
        if report.error:
            self._out(T.error(f"{report.error.title}: {report.error.message}"))
            if report.error.hint:
                self._out(T.dim(report.error.hint))
            return
        result = report.result
        self._out(T.ok(f"Wrote {result.html_path}"))
        self._out(T.ok(f"Wrote {result.notes_path}"))
        if result.validation:
            self._out(result.validation.summary())
            for check in result.validation.checks:
                if check.status != "ok":
                    self._out(f"  [{check.status}] {check.name}: {check.detail}")
        for diag in report.diagnostics:
            if diag.severity != "info":
                self._out(f"  [{diag.severity}] {diag.message}")

    def progress(self, stage: Stage, fraction: Optional[float], message: str) -> None:
        width, _ = T.terminal_size()
        pct = "" if fraction is None else f"{fraction:4.0%}"
        line = f"  {stage.value:<11} {pct:>5}  {message}"
        print("\r" + T.fit(line, width - 1), end="", flush=True)
        if fraction is None or fraction >= 0.99:
            print()


def load_state(path: Path) -> AppState:
    if not path.exists():
        return AppState()
    try:
        return AppState.from_json(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return AppState()


def run_tui(work_dir: Path, state_path: Path) -> int:
    state = load_state(state_path)
    if T.is_interactive():
        InteractiveApp(state, state_path, work_dir).run()
    else:
        PlainApp(state, state_path, work_dir).run()
    return 0
