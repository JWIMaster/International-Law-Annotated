"""The layer between the Qt interface and the working code.

Everything the window does to a project goes through :class:`ProjectSession`.
It owns the open project, the loaded annotation stores, the undo history, the
file watchers and the log; the widgets only ask it to do things and listen to
its signals.  Nothing here imports a widget, so the same object could drive a
command-line front end -- which matters, because the CLI still exists.

Long jobs (building a page, converting a PDF) run on :class:`JobThread` so the
window keeps repainting, and report progress through Qt signals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import (QObject, QSettings, QThread, QTimer, Signal)

from .. import allhtml, annotations as ann_mod, errors as err_mod, fileio
from .. import merge as merge_mod
from .. import notestore, pipeline, project as project_mod, sourcefmt, validate
from ..errors import ToolError


# ---------------------------------------------------------------------------
# Background work
# ---------------------------------------------------------------------------

class JobThread(QThread):
    """Runs one long call off the GUI thread."""

    progressed = Signal(str, float, str)      # stage, fraction, message
    finished_ok = Signal(object)              # the call's return value
    failed = Signal(str, str)                 # message, hint

    def __init__(self, label: str, work, parent=None) -> None:
        super().__init__(parent)
        self.label = label
        self._work = work

    def run(self) -> None:                     # pragma: no cover - thread body
        try:
            result = self._work(self._progress)
        except ToolError as exc:
            self.failed.emit(exc.message, exc.hint or "")
        except Exception as exc:               # noqa: BLE001 - reported to the user
            self.failed.emit(f"{type(exc).__name__}: {exc}", "")
        else:
            self.finished_ok.emit(result)

    def _progress(self, stage, fraction, message) -> None:
        name = getattr(stage, "value", None) or str(stage)
        self.progressed.emit(str(name), float(fraction or 0.0), str(message or ""))


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------

@dataclass
class LogLine:
    level: str
    message: str
    detail: str = ""


class ProjectSession(QObject):
    """The open project and everything done to it."""

    projectOpened = Signal(object)             # project.Project
    projectClosed = Signal()
    documentSelected = Signal(object)          # project.Document or None
    notesChanged = Signal(object)              # notestore.NoteStore
    documentsChanged = Signal()                # something on disk moved
    logged = Signal(str, str, str)             # level, message, detail
    busy = Signal(bool, str)                   # running, label
    progress = Signal(str, float, str)
    externalChange = Signal(str, str)          # document name, description

    MAX_RECENT = 12

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.project: Optional[project_mod.Project] = None
        self.documents: Dict[str, project_mod.Document] = {}
        self.stores: Dict[str, notestore.NoteStore] = {}
        self.sources: Dict[str, sourcefmt.SourceDocument] = {}
        self.current: Optional[project_mod.Document] = None
        self.settings = QSettings("InternationalLawAnnotated", "Studio")
        self.log: List[LogLine] = []
        self._watcher_paths: Dict[Path, str] = {}
        self._job: Optional[JobThread] = None
        self._pending_texts: Dict[str, str] = {}

        # Polling rather than QFileSystemWatcher alone: editors write by
        # replacing the file, which drops the watch on some platforms.
        self._timer = QTimer(self)
        self._timer.setInterval(1500)
        self._timer.timeout.connect(self.check_external_changes)

    # -- logging -------------------------------------------------------------

    def note(self, level: str, message: str, detail: str = "") -> None:
        self.log.append(LogLine(level, message, detail))
        if len(self.log) > 2000:
            del self.log[:500]
        self.logged.emit(level, message, detail)

    def info(self, message: str, detail: str = "") -> None:
        self.note("info", message, detail)

    def warn(self, message: str, detail: str = "") -> None:
        self.note("warn", message, detail)

    def error(self, message: str, detail: str = "") -> None:
        self.note("error", message, detail)

    # -- recent projects -----------------------------------------------------

    def recent_projects(self) -> List[str]:
        value = self.settings.value("recent", [])
        if isinstance(value, str):
            value = [value]
        return [str(v) for v in (value or []) if Path(str(v)).is_dir()]

    def remember_project(self, path: Path) -> None:
        items = [str(path)] + [p for p in self.recent_projects() if p != str(path)]
        self.settings.setValue("recent", items[: self.MAX_RECENT])

    def last_folder(self) -> str:
        return str(self.settings.value("lastFolder", str(Path.home())))

    def remember_folder(self, path: Path) -> None:
        self.settings.setValue("lastFolder", str(path))

    # -- opening -------------------------------------------------------------

    def open_project(self, folder: Path) -> bool:
        self.close_project()
        self.busy.emit(True, f"Reading {Path(folder).name}…")
        try:
            found = project_mod.open_project(Path(folder))
        except ToolError as exc:
            self.error(exc.message, exc.hint or "")
            self.busy.emit(False, "")
            return False
        finally:
            self.progress.emit("", 0.0, "")

        self.project = found
        self.documents = {doc.name: doc for doc in found.documents}
        self.remember_project(found.root)
        self.remember_folder(found.root)
        self.info(f"Opened {found.root}", found.summary())
        for issue in found.issues:
            self.warn(issue)
        for doc in found.documents:
            for issue in doc.issues:
                self.warn(f"{doc.name}: {issue}")
        self.projectOpened.emit(found)
        self.busy.emit(False, "")
        self._timer.start()
        return True

    def close_project(self) -> None:
        self._timer.stop()
        self.project = None
        self.documents = {}
        self.stores = {}
        self.sources = {}
        self.current = None
        self._watcher_paths.clear()
        self.projectClosed.emit()

    def reload_project(self) -> None:
        if self.project is None:
            return
        unsaved = [name for name, store in self.stores.items() if store.dirty]
        if unsaved:
            self.warn("Discarded unsaved changes while reloading: "
                      + ", ".join(sorted(unsaved)))
        root = self.project.root
        self.open_project(root)

    # -- documents -----------------------------------------------------------

    def source_for(self, doc: project_mod.Document) -> Optional[sourcefmt.SourceDocument]:
        """The parsed source text, which is where paragraph ids come from."""
        if doc.name in self.sources:
            return self.sources[doc.name]
        if doc.source is None or not Path(doc.source).exists():
            return None
        try:
            parsed = sourcefmt.parse_source(Path(doc.source))
        except ToolError as exc:
            self.warn(f"{doc.name}: the source text could not be read",
                      f"{exc.message}\n{exc.hint or ''}".strip())
            return None
        self.sources[doc.name] = parsed
        return parsed

    def store_for(self, doc: project_mod.Document, reload: bool = False) -> notestore.NoteStore:
        """The annotations of one text, loaded on first use."""
        if doc.name in self.stores and not reload:
            return self.stores[doc.name]
        source = self.source_for(doc)
        paragraphs = {b.id: b.text for b in source.paragraphs} if source else {}
        labels = {b.id: (b.label or "") for b in source.paragraphs} if source else {}
        path = doc.notes or (doc.page.parent / f"{doc.name}-notes.js")
        try:
            store = notestore.NoteStore.load(Path(path), paragraphs, labels)
        except ToolError as exc:
            # A notes file that cannot be parsed must not take the whole
            # application down, and must not be quietly replaced either.
            store = notestore.NoteStore.unreadable_store(
                Path(path), f"{exc.message}{(' — ' + exc.hint) if exc.hint else ''}",
                paragraphs, labels)
            self.error(f"{doc.name}: {exc.message}", exc.hint or "")
        self.stores[doc.name] = store
        self._watch(Path(path), doc.name)
        if not store.unreadable:
            self.info(f"{doc.name}: {store.count} annotation(s) loaded from "
                      f"{Path(path).name}")
        return store

    def select_document(self, doc: Optional[project_mod.Document]) -> None:
        self.current = doc
        if doc is not None:
            self.store_for(doc)
        self.documentSelected.emit(doc)

    def dirty_documents(self) -> List[str]:
        return sorted(name for name, store in self.stores.items() if store.dirty)

    def save_current(self, backup: bool = True) -> bool:
        if self.current is None:
            return False
        return self.save_document(self.current, backup)

    def save_document(self, doc: project_mod.Document, backup: bool = True,
                      force: bool = False) -> bool:
        store = self.store_for(doc)
        try:
            made = store.save(backup=backup, force=force)
        except ToolError as exc:
            self.error(exc.message, exc.hint or "")
            return False
        self.info(f"Saved {Path(store.path).name}"
                  + (f" (backup: {made.name})" if made else ""))
        self.notesChanged.emit(store)
        return True

    def save_all(self, backup: bool = True) -> int:
        saved = 0
        for name in self.dirty_documents():
            doc = self.documents.get(name)
            if doc is not None and self.save_document(doc, backup):
                saved += 1
        return saved

    # -- editing helpers -----------------------------------------------------

    def store(self) -> Optional[notestore.NoteStore]:
        if self.current is None:
            return None
        return self.store_for(self.current)

    def after_edit(self, message: str, level: str = "info") -> None:
        store = self.store()
        self.note(level, message)
        if store is not None:
            self.notesChanged.emit(store)

    # -- watching ------------------------------------------------------------

    def _watch(self, path: Path, name: str) -> None:
        try:
            self._watcher_paths[Path(path)] = name
        except TypeError:
            pass

    def check_external_changes(self) -> None:
        changed: List[str] = []
        for name, store in self.stores.items():
            if store.dirty:
                # Our own unsaved edits are the more interesting fact; do not
                # nag about the file as well.
                continue
            clash = store.external_change()
            if clash:
                changed.append(name)
                self.externalChange.emit(name, clash)
        if changed:
            self.documentsChanged.emit()

    def reload_document(self, doc: project_mod.Document) -> bool:
        store = self.store_for(doc)
        try:
            store.reload()
        except ToolError as exc:
            self.error(exc.message, exc.hint or "")
            return False
        self.info(f"Reloaded {Path(store.path).name} from disk")
        self.notesChanged.emit(store)
        return True

    # -- long jobs -----------------------------------------------------------

    def running(self) -> bool:
        return self._job is not None and self._job.isRunning()

    def start_job(self, label: str, work, on_done, on_failed=None) -> bool:
        if self.running():
            self.warn("Something is already running; wait for it to finish.")
            return False
        job = JobThread(label, work, self)
        job.progressed.connect(self.progress)
        job.finished_ok.connect(lambda value: self._job_done(job, label, value, on_done, None))
        job.failed.connect(lambda msg, hint: self._job_done(job, label, None, on_done,
                                                            (msg, hint)))
        self._job = job
        self.busy.emit(True, label)
        job.start()
        return True

    def _job_done(self, job, label, value, on_done, failure) -> None:
        self._job = None
        self.busy.emit(False, "")
        self.progress.emit("", 0.0, "")
        if failure is not None:
            message, hint = failure
            self.error(f"{label} failed: {message}", hint)
            if on_done is not None:
                on_done(None, (message, hint))
        else:
            self.info(f"{label} finished")
            if on_done is not None:
                on_done(value, None)
        job.deleteLater()

    # -- building ------------------------------------------------------------

    def build_document(self, doc: project_mod.Document, on_done,
                       migrate: bool = True, use_material: bool = False):
        """Regenerate one text's page and notes from its source or material."""
        source_path = Path(doc.source) if doc.source else Path(doc.page)
        if not source_path.exists():
            self.error(f"{doc.name}: no source text to build from",
                       "Convert the PDF or Word file first, or build from the "
                       "materials folder.")
            return False
        notes_path = Path(doc.notes) if doc.notes else \
            doc.page.parent / f"{doc.name}-notes.js"
        meta = self._meta_for(doc)

        def work(progress):
            return pipeline.run(pipeline.RunRequest(
                source=source_path,
                annotations=notes_path if notes_path.exists() else None,
                out_html=Path(doc.page),
                out_notes=notes_path,
                out_source=source_path,
                meta=meta,
                build=pipeline.BuildOptions(
                    migrate_from=Path(doc.page) if migrate else None)),
                progress)

        return self.start_job(f"Building {doc.name}", work,
                              lambda value, err: self._after_build(doc, value, err, on_done))

    def _meta_for(self, doc: project_mod.Document) -> Dict[str, str]:
        meta: Dict[str, str] = {"TITLE": doc.title or doc.name,
                                "HEADER": doc.title or doc.name}
        if doc.description:
            meta["DESC"] = doc.description
        home = "../index.html"
        if self.project is not None and self.project.landing is not None:
            try:
                rel = Path(doc.page).parent.relative_to(
                    self.project.landing.parent)
                depth = len(rel.parts)
                home = "../" * max(1, depth) + self.project.landing.name
            except ValueError:
                pass
        meta["HOME"] = home
        return meta

    def _after_build(self, doc, report, failure, on_done) -> None:
        if report is None:
            if on_done:
                on_done(None, failure)
            return
        if getattr(report, "error", None):
            self.error(f"{doc.name}: build failed", report.error.message)
        elif report.result is not None:
            summary = report.result.validation.summary()
            self.info(f"{doc.name}: built ({summary})")
            for check in report.result.validation.checks:
                if check.status != validate.OK:
                    self.warn(f"{doc.name}: {check.name} — {check.detail}")
        if on_done:
            on_done(report, None)

    def convert_document(self, material: Path, name: str, on_done):
        """PDF or Word -> source text, so a new text can be started."""
        if self.project is None:
            self.error("Open a project first")
            return False
        out_source = self.project.root / "texts" / "sources" / f"{name}.txt"
        meta = {"TITLE": name, "HEADER": name, "HOME": "../index.html"}

        def work(progress):
            return pipeline.run(pipeline.RunRequest(
                source=Path(material), annotations=None,
                out_html=self.project.root / "texts" / f"{name}.html",
                out_notes=self.project.root / "texts" / f"{name}-notes.js",
                out_source=out_source, meta=meta,
                convert=pipeline.ConvertOptions(force=True),
                build=pipeline.BuildOptions(write_diagnostics=False)),
                progress)

        return self.start_job(f"Converting {Path(material).name}", work,
                              lambda value, err: self._after_convert(name, value, err, on_done))

    def _after_convert(self, name, report, failure, on_done) -> None:
        if report is None:
            if on_done:
                on_done(None, failure)
            return
        if getattr(report, "error", None):
            self.error(f"{name}: conversion failed", report.error.message)
        else:
            self.info(f"{name}: converted from {Path(report.source_path).name}")
        if on_done:
            on_done(report, None)
        self.reload_project()

    # -- importing and exporting --------------------------------------------

    def import_annotations(self, doc: project_mod.Document, path: Path) -> Optional[merge_mod.MergePlan]:
        store = self.store_for(doc)
        source = self.source_for(doc)
        try:
            incoming = merge_mod.load_incoming(Path(path), source)
        except ToolError as exc:
            self.error(exc.message, exc.hint or "")
            return None
        plan = merge_mod.plan_merge(store, incoming)
        self.info(f"Compared {Path(path).name} with {doc.name}: {plan.summary()}")
        for text, why in plan.unplaced:
            self.warn(f"Could not place “{text}”", why)
        return plan

    def export_annotations(self, doc: project_mod.Document, path: Path,
                           fmt: str = "txt") -> bool:
        store = self.store_for(doc)
        path = Path(path)
        try:
            if fmt == "json":
                import json
                payload = [
                    dict(note, para=para, label=store.label_for(para),
                         paragraph=store.paragraph_text(para))
                    for para, _index, note in store.entries()
                ]
                text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
            elif fmt == "js":
                text = store.render()
            else:
                text = render_annotations_text(store)
                escapes = notestore.text_export_escapes(store)
                if escapes:
                    self.warn(
                        f"{len(escapes)} note(s) would have been read back as "
                        f"fields, so their first line was protected with an "
                        f"invisible character: " + "; ".join(escapes[:3]),
                        "Use the JSON or notes.js format for a copy with "
                        "nothing added.")
            fileio.atomic_write(path, text)
        except ToolError as exc:
            self.error(exc.message, exc.hint or "")
            return False
        self.info(f"Exported {store.count} annotation(s) to {path.name}")
        return True

    # -- cards ---------------------------------------------------------------

    def sync_card(self, doc: project_mod.Document) -> bool:
        """Add or update this text's card in all.html."""
        if self.project is None or self.project.all_html is None:
            self.error("There is no all.html to update")
            return False
        path = Path(self.project.all_html)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            self.error(f"Could not read {path.name}", str(exc))
            return False
        title = doc.title or doc.name.replace("-", " ").title()
        subtitle = self._subtitle_for(doc)
        desc = doc.description or f"Annotated {title} with inline author-filtered notes."
        href = f"./{doc.page.name}"
        if doc.card is not None:
            updated = allhtml.replace_case_in_all_html(text, doc.card["href"],
                                                       title, subtitle, desc)
        else:
            updated = allhtml.add_case_to_all_html(text, title, subtitle, desc, href)
        try:
            fileio.atomic_write(path, updated, backup=True)
        except ToolError as exc:
            self.error(exc.message, exc.hint or "")
            return False
        self.info(f"Updated the card for {doc.name} in all.html")
        return True

    def _subtitle_for(self, doc: project_mod.Document) -> str:
        source = self.source_for(doc)
        if source is not None:
            for key in ("SUBTITLE", "HEADER"):
                if source.meta.get(key):
                    return source.meta[key]
        if self.project is not None and self.project.materials_dir is not None \
                and doc.material is not None:
            try:
                return doc.material.parent.relative_to(
                    self.project.materials_dir).as_posix().replace(":", "")
            except ValueError:
                pass
        return ""


# Re-exported for the interface; the implementation is with the format.
render_annotations_text = notestore.render_annotations_text
