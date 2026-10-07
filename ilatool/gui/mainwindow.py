"""The application window.

Structure of the interface, top to bottom and left to right::

    +-- menu bar: File  Edit  Text  View  Help
    +-- toolbar: open, reload, save, add, import, export, build
    +-- splitter
    |     left   the project: its structure, then each text and its files
    |     right  one page at a time --
    |              Overview       what was discovered, and what was not
    |              Annotations    the notes of the selected text
    |              Build          regenerate a page and read the report
    |              Log            everything the session has said
    +-- status bar: what is happening, and whether anything is unsaved

Every action that changes a file goes through :class:`ProjectSession`; this
module only arranges widgets and asks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import (QAction, QActionGroup, QColor, QFont, QIcon, QKeySequence,
                           QTextCharFormat, QTextCursor)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                               QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
                               QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QSplitter, QStackedWidget, QStatusBar,
                               QTableView, QTableWidget, QTableWidgetItem, QTextBrowser,
                               QTextEdit, QToolBar, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from .. import annotations as ann_mod, errors as err_mod, merge as merge_mod
from .. import project as project_mod, textutil, validate
from ..errors import ToolError
from .dialogs import AnnotationEditor, ExportDialog, MergeDialog
from .models import (AnnotationFilterModel, AnnotationTableModel,
                     INDEX_ROLE, NOTE_ROLE, PARA_ROLE)
from .services import ProjectSession

PAGE_OVERVIEW, PAGE_NOTES, PAGE_BUILD, PAGE_LOG = range(4)


class MainWindow(QMainWindow):
    def __init__(self, session: Optional[ProjectSession] = None, start: Optional[Path] = None) -> None:
        super().__init__()
        self.session = session or ProjectSession(self)
        self.setWindowTitle("Annotation Studio")
        self.resize(1360, 880)
        self.setMinimumSize(QSize(940, 600))
        self._build_ui()
        self._connect()
        if start is not None:
            self.open_path(Path(start))
        else:
            self._show_empty_state()

    # ------------------------------------------------------------------ build

    def _build_ui(self) -> None:
        self._build_actions()
        self._build_menus()
        self._build_toolbar()

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_sidebar())
        splitter.addWidget(self._build_pages())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([330, 1030])
        self.splitter = splitter
        self.setCentralWidget(splitter)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status_label = QLabel("Open a project folder to begin.")
        self.status.addWidget(self.status_label, 1)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setVisible(False)
        self.status.addPermanentWidget(self.progress)
        self.dirty_label = QLabel("")
        self.status.addPermanentWidget(self.dirty_label)

    def _build_actions(self) -> None:
        def act(text: str, shortcut: str = "", tip: str = "", slot=None) -> QAction:
            action = QAction(text, self)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            if tip:
                action.setStatusTip(tip)
            if slot is not None:
                action.triggered.connect(slot)
            return action

        self.act_open = act("&Open Project…", "Ctrl+O",
                            "Choose the folder that holds index.html", self.on_open)
        self.act_open_other = act("Open &Folder…", "Ctrl+Shift+O",
                                  "Open a folder without a recent list", self.on_open)
        self.act_reload = act("&Reload Project", "Ctrl+Shift+R",
                              "Read every file again from disk", self.on_reload)
        self.act_close = act("&Close Project", "", "Close the project", self.on_close)
        self.act_save = act("&Save Annotations", "Ctrl+S",
                            "Write this text's annotations back", self.on_save)
        self.act_save_all = act("Save &All", "Ctrl+Alt+S",
                                "Write every changed annotations file", self.on_save_all)
        self.act_import = act("&Import Annotations…", "Ctrl+I",
                              "Merge another annotation file into this text",
                              self.on_import)
        self.act_export = act("&Export Annotations…", "Ctrl+E",
                              "Write these annotations out in any format",
                              self.on_export)
        self.act_convert = act("&Convert Material…", "Ctrl+Shift+I",
                               "Turn a PDF or Word file into a source text",
                               self.on_convert)
        self.act_quit = act("&Quit", "Ctrl+Q", "Leave", self.close)

        self.act_undo = act("&Undo", "Ctrl+Z", "Undo the last change", self.on_undo)
        self.act_redo = act("&Redo", "Ctrl+Shift+Z", "Redo", self.on_redo)
        self.act_add = act("&New Annotation…", "Ctrl+N",
                           "Add a note to this text", self.on_add)
        self.act_edit = act("&Edit Annotation…", "Return",
                            "Edit the selected note", self.on_edit)
        self.act_duplicate = act("&Duplicate", "Ctrl+D",
                                 "Copy the selected note", self.on_duplicate)
        self.act_delete = act("De&lete", "Delete",
                              "Remove the selected note", self.on_delete)
        self.act_move_up = act("Move &Up", "Ctrl+Up", "Reorder within the paragraph",
                               lambda: self.on_move(-1))
        self.act_move_down = act("Move &Down", "Ctrl+Down", "Reorder within the paragraph",
                                 lambda: self.on_move(1))
        self.act_find = act("&Find", "Ctrl+F", "Search every annotation",
                            self.on_find)

        self.act_sync_card = act("Update Site &Card", "",
                                 "Add or update this text's card in all.html",
                                 self.on_sync_card)
        self.act_build = act("&Build This Text", "Ctrl+B",
                             "Regenerate the page from its source and notes",
                             self.on_build)
        self.act_build_all = act("Build &All Texts", "Ctrl+Shift+B",
                                 "Regenerate every text in the project",
                                 self.on_build_all)
        self.act_open_html = act("Open the &Page in a Browser", "Ctrl+Return",
                                 "Show the generated page", self.on_open_page)
        self.act_open_folder = act("Reveal in &File Manager", "", "",
                                   self.on_reveal)

        self.act_overview = act("&Overview", "Ctrl+1", "What was discovered", 
                                lambda: self.show_page(PAGE_OVERVIEW))
        self.act_notes = act("&Annotations", "Ctrl+2", "The notes of this text",
                             lambda: self.show_page(PAGE_NOTES))
        self.act_build_page = act("&Build", "Ctrl+3", "Build and read the report",
                                  lambda: self.show_page(PAGE_BUILD))
        self.act_log = act("&Log", "Ctrl+4", "Everything this session has said",
                           lambda: self.show_page(PAGE_LOG))
        for action in (self.act_overview, self.act_notes, self.act_build_page, self.act_log):
            action.setCheckable(True)
        self.act_overview.setChecked(True)

        self.act_formats = act("Annotation &Formats", "F1",
                               "The formats this project accepts", self.on_formats)
        self.act_about = act("&About", "", "", self.on_about)

    def _build_menus(self) -> None:
        bar = self.menuBar()
        file_menu = bar.addMenu("&File")
        file_menu.addAction(self.act_open)
        self.recent_menu = file_menu.addMenu("Open &Recent")
        file_menu.addSeparator()
        file_menu.addAction(self.act_reload)
        file_menu.addAction(self.act_close)
        file_menu.addSeparator()
        file_menu.addAction(self.act_save)
        file_menu.addAction(self.act_save_all)
        file_menu.addSeparator()
        file_menu.addAction(self.act_import)
        file_menu.addAction(self.act_export)
        file_menu.addAction(self.act_convert)
        file_menu.addSeparator()
        file_menu.addAction(self.act_quit)

        edit_menu = bar.addMenu("&Edit")
        for action in (self.act_undo, self.act_redo):
            edit_menu.addAction(action)
        edit_menu.addSeparator()
        for action in (self.act_add, self.act_edit, self.act_duplicate,
                       self.act_delete):
            edit_menu.addAction(action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_move_up)
        edit_menu.addAction(self.act_move_down)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_find)

        text_menu = bar.addMenu("&Text")
        text_menu.addAction(self.act_build)
        text_menu.addAction(self.act_build_all)
        text_menu.addSeparator()
        text_menu.addAction(self.act_sync_card)
        text_menu.addSeparator()
        text_menu.addAction(self.act_open_html)
        text_menu.addAction(self.act_open_folder)

        view_menu = bar.addMenu("&View")
        for action in (self.act_overview, self.act_notes, self.act_build_page,
                       self.act_log):
            view_menu.addAction(action)

        help_menu = bar.addMenu("&Help")
        help_menu.addAction(self.act_formats)
        help_menu.addAction(self.act_about)

    def _build_toolbar(self) -> None:
        bar = QToolBar("Main")
        bar.setMovable(False)
        bar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(bar)
        for action in (self.act_open, self.act_reload, self.act_save):
            bar.addAction(action)
        bar.addSeparator()
        for action in (self.act_add, self.act_import, self.act_export):
            bar.addAction(action)
        bar.addSeparator()
        bar.addAction(self.act_build)
        self.toolbar = bar
        self._fill_recent()

    def _build_sidebar(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 4, 8)
        layout.setSpacing(6)

        self.project_label = QLabel("No project open")
        font = QFont()
        font.setBold(True)
        self.project_label.setFont(font)
        self.project_label.setWordWrap(True)
        layout.addWidget(self.project_label)

        self.structure = QTreeWidget()
        self.structure.setHeaderHidden(True)
        self.structure.setAlternatingRowColors(False)
        self.structure.setSelectionMode(QAbstractItemView.SingleSelection)
        self.structure.itemSelectionChanged.connect(self._tree_selection)
        self.structure.setContextMenuPolicy(Qt.CustomContextMenu)
        self.structure.customContextMenuRequested.connect(self._tree_menu)
        self.structure.itemDoubleClicked.connect(lambda *_: self.on_open_page())
        layout.addWidget(self.structure, 1)

        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText("Filter the texts…")
        self.filter_box.textChanged.connect(self._filter_tree)
        layout.addWidget(self.filter_box)
        return panel

    def _build_pages(self) -> QWidget:
        self.pages = QStackedWidget()
        self.pages.addWidget(self._page_overview())
        self.pages.addWidget(self._page_notes())
        self.pages.addWidget(self._page_build())
        self.pages.addWidget(self._page_log())
        return self.pages

    # -- overview ------------------------------------------------------------

    def _page_overview(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        self.overview = QTextBrowser()
        self.overview.setOpenExternalLinks(True)
        layout.addWidget(self.overview, 1)
        row = QHBoxLayout()
        self.overview_issues_only = QCheckBox("Show only the text that has problems")
        row.addWidget(self.overview_issues_only)
        row.addStretch(1)
        layout.addLayout(row)
        return page

    # -- annotations ---------------------------------------------------------

    def _page_notes(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        self.doc_label = QLabel("Select a text")
        font = QFont()
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        self.doc_label.setFont(font)
        header.addWidget(self.doc_label)
        header.addStretch(1)
        self.count_label = QLabel("")
        header.addWidget(self.count_label)
        layout.addLayout(header)

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search notes, authors, paragraph text…  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter_changed)
        filters.addWidget(self.search, 1)
        self.author_filter = QComboBox()
        self.author_filter.setMinimumWidth(170)
        self.author_filter.currentIndexChanged.connect(self._filter_changed)
        filters.addWidget(QLabel("Author"))
        filters.addWidget(self.author_filter)
        self.orphan_only = QCheckBox("Only orphaned")
        self.orphan_only.setToolTip(
            "Notes attached to a paragraph the current source text no longer has")
        self.orphan_only.toggled.connect(self._filter_changed)
        filters.addWidget(self.orphan_only)
        layout.addLayout(filters)

        self.table = QTableView()
        self.model = AnnotationTableModel(None, self)
        self.proxy = AnnotationFilterModel(self)
        self.proxy.setSourceModel(self.model)
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        self.table.doubleClicked.connect(lambda *_: self.on_edit())
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.Interactive)
        header_view.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 260)
        self.table.setColumnWidth(1, 130)
        self.table.setColumnWidth(2, 150)
        self.table.setColumnWidth(4, 160)
        self.table.setColumnWidth(5, 80)
        layout.addWidget(self.table, 1)

        self.detail = QTextBrowser()
        self.detail.setMaximumHeight(190)
        self.detail.setHtml("<p style='color:#8B8F9C'>Select an annotation to see "
                            "the whole note and the paragraph it belongs to.</p>")
        layout.addWidget(self.detail)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._show_detail())

        empty = QLabel()
        empty.setAlignment(Qt.AlignCenter)
        empty.setWordWrap(True)
        empty.setStyleSheet("color: #8B8F9C; padding: 40px 24px;")
        self.empty_notes = empty
        layout.addWidget(empty, 1)
        empty.setVisible(False)

        banner = QLabel()
        banner.setWordWrap(True)
        banner.setStyleSheet(
            "background: #FBF3E4; border: 1px solid #E0CFA8; border-left: 3px solid "
            "#A3341F; padding: 8px; color: #5A4A1E;")
        banner.setVisible(False)
        layout.insertWidget(2, banner)
        self.banner = banner
        return page

    # -- build ---------------------------------------------------------------

    def _page_build(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        self.build_label = QLabel("Select a text to build")
        font = QFont()
        font.setBold(True)
        self.build_label.setFont(font)
        layout.addWidget(self.build_label)

        row = QHBoxLayout()
        self.build_button = QPushButton("Build this text")
        self.build_button.clicked.connect(self.on_build)
        row.addWidget(self.build_button)
        self.build_all_button = QPushButton("Build every text")
        self.build_all_button.clicked.connect(self.on_build_all)
        row.addWidget(self.build_all_button)
        self.build_migrate = QCheckBox("Carry paragraph ids over from the current page")
        self.build_migrate.setChecked(True)
        self.build_migrate.setToolTip(
            "Keeps notes attached when the source text is regenerated")
        row.addWidget(self.build_migrate)
        row.addStretch(1)
        layout.addLayout(row)

        self.build_report = QTextBrowser()
        layout.addWidget(self.build_report, 1)
        return page

    # -- log -----------------------------------------------------------------

    def _page_log(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Menlo, Consolas, monospace", 11))
        layout.addWidget(self.log_view, 1)
        row = QHBoxLayout()
        self.log_detail = QCheckBox("Include technical detail")
        self.log_detail.setChecked(True)
        row.addWidget(self.log_detail)
        row.addStretch(1)
        copy = QPushButton("Copy everything")
        copy.clicked.connect(self.on_copy_log)
        row.addWidget(copy)
        layout.addLayout(row)
        return page

    # ------------------------------------------------------------------ wiring

    def _connect(self) -> None:
        s = self.session
        s.projectOpened.connect(self._on_project_opened)
        s.projectClosed.connect(self._on_project_closed)
        s.documentSelected.connect(self._on_document_selected)
        s.notesChanged.connect(self._on_notes_changed)
        s.documentsChanged.connect(self._refresh_dirty)
        s.logged.connect(self._on_logged)
        s.busy.connect(self._on_busy)
        s.progress.connect(self._on_progress)
        s.externalChange.connect(self._on_external_change)

    # ------------------------------------------------------------------ actions

    def open_path(self, folder: Path) -> None:
        if not self.session.open_project(Path(folder)):
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("Could not open that folder")
            box.setText(f"{folder} could not be opened as a project.")
            box.setInformativeText("See the Log page for the details.")
            box.exec()

    def on_open(self) -> None:
        start = self.session.last_folder()
        folder = QFileDialog.getExistingDirectory(
            self, "Choose the project folder (the one with index.html)", start,
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks)
        if folder:
            self.open_path(Path(folder))

    def on_close(self) -> None:
        if not self._confirm_discard():
            return
        self.session.close_project()

    def on_reload(self) -> None:
        if not self._confirm_discard("Reloading will discard unsaved changes."):
            return
        self.session.reload_project()

    def on_save(self) -> bool:
        if not self._require_document():
            return False
        return self.session.save_current()

    def on_save_all(self) -> None:
        count = self.session.save_all()
        self.status_label.setText(f"Saved {count} file(s)." if count else "Nothing to save.")

    def on_undo(self) -> None:
        store = self.session.store()
        if store is None:
            return
        if store.undo():
            self.session.info("Undid the last change")
            self.session.notesChanged.emit(store)
        else:
            self.status_label.setText("Nothing to undo.")

    def on_redo(self) -> None:
        store = self.session.store()
        if store is None:
            return
        if store.redo():
            self.session.info("Redid the last change")
            self.session.notesChanged.emit(store)
        else:
            self.status_label.setText("Nothing to redo.")

    def on_add(self) -> None:
        store = self.session.store()
        if store is None or not self._require_document():
            return
        para = self._selected_para() or ""
        dialog = AnnotationEditor(store, para, None, self)
        if dialog.exec() != AnnotationEditor.Accepted:
            return
        try:
            change = store.add(dialog.selected_para, dialog.selected_note)
        except ToolError as exc:
            self._report(exc)
            return
        self.session.after_edit(change.summary)
        self._select_row(change.para, len(store.notes.get(change.para, [])) - 1)

    def on_edit(self) -> None:
        store = self.session.store()
        row = self._current_row()
        if store is None or row is None:
            return
        para, index, note = row
        dialog = AnnotationEditor(store, para, note, self)
        if dialog.exec() != AnnotationEditor.Accepted:
            return
        try:
            change = store.update(para, index, dialog.selected_note)
        except ToolError as exc:
            self._report(exc)
            return
        if dialog.selected_para != para:
            change = store.move_to_para(para, index, dialog.selected_para)
        self.session.after_edit(change.summary)
        self._select_row(dialog.selected_para, index)

    def on_duplicate(self) -> None:
        store = self.session.store()
        row = self._current_row()
        if store is None or row is None:
            return
        try:
            change = store.duplicate(row[0], row[1])
        except ToolError as exc:
            self._report(exc)
            return
        self.session.after_edit(change.summary)
        self._select_row(row[0], row[1] + 1)

    def on_delete(self) -> None:
        store = self.session.store()
        rows = self._current_rows()
        if store is None or not rows:
            return
        if len(rows) == 1:
            para, index, note = rows[0]
            question = "Remove this annotation?\n\n"
            if note.get("author"):
                question += f"{note['author']}\n"
            question += textutil.truncate_end(
                textutil.normalize_whitespace(note.get("text", "")), 300)
            question += f"\n\nFrom: {store._describe(para)}"
        else:
            question = (f"Remove {len(rows)} annotations?\n\n"
                        + "\n".join("• " + textutil.truncate_end(
                            textutil.normalize_whitespace(n.get("text", "")), 60)
                            for _p, _i, n in rows[:8])
                        + ("\n…" if len(rows) > 8 else ""))
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Delete annotations")
        box.setText(question)
        box.setInformativeText("This can be undone with Ctrl+Z until you save.")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return
        try:
            change = store.delete_many(rows)
        except ToolError as exc:
            self._report(exc)
            return
        self.session.after_edit(change.summary)

    def on_move(self, delta: int) -> None:
        store = self.session.store()
        row = self._current_row()
        if store is None or row is None:
            return
        try:
            change = store.move(row[0], row[1], delta)
        except ToolError as exc:
            self.status_label.setText(exc.message)
            return
        self.session.after_edit(change.summary)
        self._select_row(row[0], row[1] + delta)

    def on_find(self) -> None:
        self.show_page(PAGE_NOTES)
        self.search.setFocus()
        self.search.selectAll()

    def on_import(self) -> None:
        doc = self.session.current
        if doc is None or not self._require_document():
            return
        start = str(doc.page.parent)
        path, _filter = QFileDialog.getOpenFileName(
            self, "Import annotations",
            start,
            "Annotations (*.txt *.json *.csv *.tsv *.js *.docx *.pdf);;All files (*)")
        if not path:
            return
        plan = self.session.import_annotations(doc, Path(path))
        if plan is None:
            return
        if not plan.items and not plan.unplaced:
            QMessageBox.information(
                self, "Nothing to import",
                f"{Path(path).name} contains no annotations that this project "
                "does not already have.")
            return
        store = self.session.store_for(doc)
        dialog = MergeDialog(plan, store, doc.name, self)
        if dialog.exec() != MergeDialog.Accepted:
            self.status_label.setText("Import cancelled; nothing was changed.")
            return
        added, replaced, skipped = plan.apply(store, dialog.decisions)
        self.session.after_edit(
            f"Imported from {Path(path).name}: {added} added, {replaced} replaced, "
            f"{skipped} left as they were")
        if added or replaced:
            self.status_label.setText(
                f"Imported {added + replaced} annotation(s). "
                "Save (Ctrl+S) to write them to the project.")

    def on_export(self) -> None:
        doc = self.session.current
        if doc is None or not self._require_document():
            return
        store = self.session.store_for(doc)
        dialog = ExportDialog(store, self)
        if dialog.exec() != ExportDialog.Accepted:
            return
        fmt = dialog.format.currentData()
        suffix = {"txt": ".txt", "json": ".json", "js": "-notes.js"}[fmt]
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export annotations",
            str(doc.page.parent / f"{doc.name}-annotations{suffix}"),
            "All files (*)")
        if not path:
            return
        if self.session.export_annotations(doc, Path(path), fmt):
            self.status_label.setText(f"Exported to {Path(path).name}")

    def on_convert(self) -> None:
        if self.session.project is None:
            QMessageBox.information(self, "No project",
                                    "Open a project folder first.")
            return
        start = str(self.session.project.materials_dir or self.session.project.root)
        path, _filter = QFileDialog.getOpenFileName(
            self, "Choose the PDF or Word file to convert", start,
            "Documents (*.pdf *.docx);;All files (*)")
        if not path:
            return
        name = Path(path).stem.lower().replace(" ", "-")
        name, ok = _prompt_text(self, "Name this text",
                                "The file name for the generated page and notes:",
                                name)
        if not ok or not name:
            return
        self.session.convert_document(Path(path), name, self._after_convert)

    def _after_convert(self, report, failure) -> None:
        if failure is not None:
            self._report_failure("Conversion failed", failure)
            return
        self.status_label.setText("Converted. The project has been re-scanned.")

    def on_build(self) -> None:
        doc = self.session.current
        if doc is None or not self._require_document():
            return
        self.show_page(PAGE_BUILD)
        if doc.source is None:
            self.build_report.setHtml(
                "<p>This text has no <b>source text</b>, so it cannot be built. "
                "Use <i>File ▸ Convert Material…</i> to make one from the PDF or "
                "Word file first.</p>")
            return
        self.build_report.setHtml(f"<p>Building <b>{doc.name}</b>…</p>")
        self.session.build_document(doc, self._after_build,
                                    migrate=self.build_migrate.isChecked())

    def on_build_all(self) -> None:
        if self.session.project is None:
            return
        pending = [d for d in self.session.project.documents if d.source is not None]
        if not pending:
            QMessageBox.information(self, "Nothing to build",
                                    "No text in this project has a source text yet.")
            return
        self.show_page(PAGE_BUILD)
        self._build_queue = list(pending)
        self._build_results: List[str] = []
        self.build_report.setHtml(f"<p>Building {len(pending)} text(s)…</p>")
        self._build_next()

    def _build_next(self) -> None:
        if not self._build_queue:
            self.build_report.setHtml(
                "<h3>Build finished</h3><ul>"
                + "".join(f"<li>{line}</li>" for line in self._build_results)
                + "</ul>")
            return
        doc = self._build_queue.pop(0)
        self.session.build_document(
            doc, lambda report, failure, doc=doc: self._after_one_build(doc, report, failure),
            migrate=self.build_migrate.isChecked())

    def _after_one_build(self, doc, report, failure) -> None:
        if failure is not None:
            self._build_results.append(f"<b>{doc.name}</b>: failed — {failure[0]}")
        elif report is not None and getattr(report, "error", None):
            self._build_results.append(
                f"<b>{doc.name}</b>: failed — {report.error.message}")
        elif report is not None and report.result is not None:
            self._build_results.append(
                f"<b>{doc.name}</b>: {report.result.validation.summary()}")
        self._build_next()

    def _after_build(self, report, failure) -> None:
        if failure is not None:
            self._report_failure("Build failed", failure)
            return
        if report is None:
            return
        if getattr(report, "error", None):
            self.build_report.setHtml(
                f"<h3>Build failed</h3><p>{report.error.message}</p>"
                f"<p style='color:#8B8F9C'>{report.error.hint or ''}</p>")
            return
        result = report.result
        rows = ["<h3>Build finished</h3>",
                f"<p>{result.validation.summary()}</p>", "<ul>"]
        for check in result.validation.checks:
            colour = {"ok": "#2F6B3A", "warn": "#8A6D1C", "fail": "#A3341F"}.get(
                check.status, "#565C6E")
            mark = {"ok": "✔", "warn": "!", "fail": "✖"}.get(check.status, "·")
            rows.append(f"<li><span style='color:{colour}'>{mark}</span> "
                        f"<b>{check.name}</b> — {check.detail}</li>")
        rows.append("</ul>")
        stats = result.stats
        rows.append("<h4>Numbers</h4><ul>"
                    + "".join(f"<li>{key}: {value}</li>" for key, value in stats.items())
                    + "</ul>")
        self.build_report.setHtml("".join(rows))
        self.session.reload_document(self.session.current)

    def on_sync_card(self) -> None:
        doc = self.session.current
        if doc is None or not self._require_document():
            return
        if self.session.sync_card(doc):
            QMessageBox.information(
                self, "Site card updated",
                f"all.html now links to {doc.page.name}.")
            self.session.reload_project()

    def on_open_page(self) -> None:
        doc = self.session.current
        if doc is None:
            return
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(doc.page))))

    def on_reveal(self) -> None:
        doc = self.session.current
        target = Path(doc.page) if doc is not None else (
            self.session.project.root if self.session.project else None)
        if target is None:
            return
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.parent
                                                        if target.is_file() else target)))

    def on_formats(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Annotation formats")
        box.setTextFormat(Qt.PlainText)
        box.setText(ann_mod.ANN_FORMAT_HELP.strip())
        box.setDetailedText(
            "The editor writes the same notes.js the builder reads, so nothing "
            "is lost by editing here instead of in a text editor.")
        box.exec()

    def on_about(self) -> None:
        QMessageBox.about(
            self, "Annotation Studio",
            "<h3>Annotation Studio</h3>"
            "<p>Managing the annotations of an International Law Annotated "
            "project.</p>"
            "<p>Annotations are read and written in the project's own "
            "<code>notes.js</code> format, by the same code the command-line "
            "tool uses.</p>")

    def on_copy_log(self) -> None:
        QApplication.clipboard().setText(self.log_view.toPlainText())
        self.status_label.setText("Log copied to the clipboard.")

    # ------------------------------------------------------------------ pages

    def show_page(self, page: int) -> None:
        self.pages.setCurrentIndex(page)
        for index, action in enumerate((self.act_overview, self.act_notes,
                                        self.act_build_page, self.act_log)):
            action.setChecked(index == page)

    # ------------------------------------------------------------------ state

    def _show_empty_state(self) -> None:
        self.overview.setHtml(
            "<h2>No project open</h2>"
            "<p>Choose <b>File ▸ Open Project…</b> and pick the folder that "
            "holds <code>index.html</code>. Everything else — the texts, their "
            "annotations, the sources and the materials — is found from there."
            "</p>"
            "<h3>What it looks for</h3>"
            "<ul><li><code>index.html</code> — the landing page</li>"
            "<li><code>texts/all.html</code> — the card list</li>"
            "<li><code>texts/&lt;name&gt;.html</code> — a generated text</li>"
            "<li><code>texts/&lt;name&gt;-notes.js</code> — its annotations, "
            "found through the page's own script tag</li>"
            "<li><code>texts/sources/&lt;name&gt;.txt</code> — the paragraph ids"
            "</li><li><code>materials/</code> — the PDFs and Word files</li></ul>")
        self.show_page(PAGE_OVERVIEW)

    def _on_project_opened(self, project) -> None:
        self.project_label.setText(str(project.root))
        self.setWindowTitle(f"Annotation Studio — {project.name}")
        self._fill_tree()
        self._fill_overview()
        self._update_actions()
        self.show_page(PAGE_OVERVIEW)
        self.status_label.setText(project.summary())
        if project.documents:
            self._select_document(project.documents[0])
        self._fill_recent()

    def _on_project_closed(self) -> None:
        self.project_label.setText("No project open")
        self.setWindowTitle("Annotation Studio")
        self.structure.clear()
        self._show_empty_state()
        self._update_actions()

    def _fill_tree(self) -> None:
        project = self.session.project
        self.structure.clear()
        if project is None:
            return
        root_item = QTreeWidgetItem([project.name])
        root_item.setData(0, Qt.UserRole, ("root", ""))
        root_item.setExpanded(True)
        self.structure.addTopLevelItem(root_item)

        structure = QTreeWidgetItem(["Project files"])
        structure.setData(0, Qt.UserRole, ("group", ""))
        for label, path in (("Landing page", project.landing),
                            ("Card list (all.html)", project.all_html),
                            ("Materials folder", project.materials_dir),
                            ("Sources folder", (project.texts_dir / "sources")
                             if project.texts_dir else None)):
            if path is None:
                item = QTreeWidgetItem([f"{label} — not found"])
                item.setForeground(0, QColor("#A3341F"))
                item.setData(0, Qt.UserRole, ("missing", label))
            else:
                item = QTreeWidgetItem([f"{label} — {Path(path).name}"])
                item.setToolTip(0, str(path))
                item.setData(0, Qt.UserRole, ("file", str(path)))
            structure.addChild(item)
        for issue in project.issues:
            item = QTreeWidgetItem([issue])
            item.setForeground(0, QColor("#8A6D1C"))
            item.setData(0, Qt.UserRole, ("issue", ""))
            structure.addChild(item)
        root_item.addChild(structure)
        structure.setExpanded(True)

        texts = QTreeWidgetItem([f"Texts ({len(project.documents)})"])
        texts.setData(0, Qt.UserRole, ("group", ""))
        for doc in project.documents:
            item = QTreeWidgetItem([doc.title or doc.name])
            item.setData(0, Qt.UserRole, ("doc", doc.name))
            item.setToolTip(0, str(doc.page))
            if doc.issues:
                item.setForeground(0, QColor("#8A6D1C"))
                item.setToolTip(0, str(doc.page) + "\n\n" + "\n".join(doc.issues))
            item.addChild(_leaf("Page", doc.page))
            item.addChild(_leaf("Annotations", doc.notes))
            item.addChild(_leaf("Source text", doc.source))
            item.addChild(_leaf("Material", doc.material))
            texts.addChild(item)
        root_item.addChild(texts)
        texts.setExpanded(True)

    def _filter_tree(self, text: str) -> None:
        needle = (text or "").strip().lower()
        for i in range(self.structure.topLevelItemCount()):
            root = self.structure.topLevelItem(i)
            for j in range(root.childCount()):
                group = root.child(j)
                if group.data(0, Qt.UserRole)[0] != "group":
                    continue
                for k in range(group.childCount()):
                    item = group.child(k)
                    hidden = bool(needle) and needle not in item.text(0).lower()
                    item.setHidden(hidden)

    def _tree_selection(self) -> None:
        items = self.structure.selectedItems()
        if not items:
            return
        kind, value = items[0].data(0, Qt.UserRole) or ("", "")
        if kind == "doc":
            doc = self.session.documents.get(value)
            if doc is not None:
                self._select_document(doc)

    def _tree_menu(self, position) -> None:
        items = self.structure.selectedItems()
        menu = QMenu(self)
        if items:
            kind, value = items[0].data(0, Qt.UserRole) or ("", "")
            if kind == "doc":
                doc = self.session.documents.get(value)
                if doc is not None:
                    menu.addAction(self.act_build)
                    menu.addAction(self.act_sync_card)
                    menu.addSeparator()
                    menu.addAction(self.act_import)
                    menu.addAction(self.act_export)
                    menu.addSeparator()
                    menu.addAction(self.act_open_html)
                    menu.addAction(self.act_open_folder)
            elif kind == "file":
                reveal = QAction("Reveal in file manager", self)
                reveal.triggered.connect(lambda: self._reveal(Path(value)))
                menu.addAction(reveal)
        if menu.isEmpty():
            menu.addAction(self.act_open)
            menu.addAction(self.act_reload)
        menu.exec(self.structure.viewport().mapToGlobal(position))

    def _reveal(self, path: Path) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(
            str(path if path.is_dir() else path.parent)))

    def _select_document(self, doc) -> None:
        self.session.select_document(doc)
        for i in range(self.structure.topLevelItemCount()):
            root = self.structure.topLevelItem(i)
            for j in range(root.childCount()):
                group = root.child(j)
                for k in range(group.childCount()):
                    item = group.child(k)
                    kind, value = item.data(0, Qt.UserRole) or ("", "")
                    if kind == "doc" and value == doc.name:
                        self.structure.setCurrentItem(item)
                        return

    def _on_document_selected(self, doc) -> None:
        if doc is None:
            self.model.set_store(None)
            self.doc_label.setText("Select a text")
            return
        store = self.session.store_for(doc)
        self.doc_label.setText(doc.title or doc.name)
        self.model.set_store(store)
        self._show_banner(store)
        self._fill_author_filter(store)
        self.summary_for(store)
        self.detail.clear()
        self.show_page(PAGE_NOTES if self.pages.currentIndex() != PAGE_BUILD
                       else PAGE_BUILD)
        self.build_label.setText(f"Build “{doc.title or doc.name}”")
        self.build_report.clear()
        self._update_actions()
        self._refresh_counts()

    def _show_banner(self, store) -> None:
        if store is not None and store.unreadable:
            self.banner.setText(
                f"<b>{Path(store.path).name} could not be read, so no annotations "
                f"are shown and this file will not be written over.</b><br>"
                f"{_esc(store.unreadable)}<br>"
                "Fix the file, then use File ▸ Reload Project.")
            self.banner.setVisible(True)
        else:
            self.banner.setVisible(False)

    def _fill_author_filter(self, store) -> None:
        current = self.author_filter.currentData()
        self.author_filter.blockSignals(True)
        self.author_filter.clear()
        self.author_filter.addItem("Everyone", "")
        for author in store.authors():
            self.author_filter.addItem(author, author)
        index = self.author_filter.findData(current)
        self.author_filter.setCurrentIndex(max(0, index))
        self.author_filter.blockSignals(False)

    def summary_for(self, store) -> None:
        """Nothing to say here: the count is on screen, and the log should hold
        things that happened rather than things that are."""
        return

    def _on_notes_changed(self, store) -> None:
        self.model.refresh()
        # A note by a new author must appear in the filter straight away,
        # not only after the text is re-selected.
        self._fill_author_filter(store)
        self._show_banner(store)
        self._refresh_counts()
        self._refresh_dirty()
        self._update_actions()
        self._show_detail()

    def _refresh_counts(self) -> None:
        store = self.session.store()
        if store is None:
            self.count_label.setText("")
            return
        orphans = sum(1 for para in store.targets() if store.is_orphan(para))
        text = f"{store.count} annotation(s) on {len(store.targets())} paragraph(s)"
        if orphans:
            text += f" · {orphans} orphaned"
        self.count_label.setText(text)
        empty = store.count == 0
        self.empty_notes.setVisible(empty)
        self.table.setVisible(not empty)
        if empty:
            if store.unreadable:
                self.empty_notes.setText(
                    "This text's annotations could not be read, so there is "
                    "nothing to show. Fix the file above, then reload.")
            elif not store.paragraphs:
                self.empty_notes.setText(
                    "<b>No annotations yet.</b><br><br>Press <b>Ctrl+N</b> to write "
                    "the first one.<br><br>"
                    "<span style='color:#A3341F'>This text has no source text, so "
                    "notes cannot be anchored to a paragraph yet.</span>")
            else:
                self.empty_notes.setText(
                    "<b>No annotations yet.</b><br><br>Press <b>Ctrl+N</b> to write "
                    "the first one.")
        self.detail.setVisible(store.count > 0)

    def _refresh_dirty(self) -> None:
        dirty = self.session.dirty_documents()
        if dirty:
            self.dirty_label.setText(f"● unsaved: {', '.join(dirty)}")
            self.dirty_label.setStyleSheet("color: #A3341F;")
        else:
            self.dirty_label.setText("saved")
            self.dirty_label.setStyleSheet("color: #8B8F9C;")
        self._update_actions()

    def _update_actions(self) -> None:
        has_project = self.session.project is not None
        has_doc = self.session.current is not None
        store = self.session.store() if has_doc else None
        for action in (self.act_reload, self.act_close):
            action.setEnabled(has_project)
        broken = bool(store and store.unreadable)
        for action in (self.act_save, self.act_save_all, self.act_add,
                       self.act_edit, self.act_duplicate, self.act_delete,
                       self.act_import, self.act_export, self.act_build,
                       self.act_sync_card, self.act_open_html):
            action.setEnabled(has_doc and not broken)
        self.act_move_up.setEnabled(bool(store))
        self.act_move_down.setEnabled(bool(store))
        self.act_undo.setEnabled(bool(store) and not broken and store.can_undo())
        self.act_redo.setEnabled(bool(store) and not broken and store.can_redo())
        self.act_build_all.setEnabled(has_project)
        self.act_convert.setEnabled(has_project)

    # -- overview ------------------------------------------------------------

    def _fill_overview(self) -> None:
        project = self.session.project
        if project is None:
            return
        only_issues = self.overview_issues_only.isChecked()
        rows = ["<h2>Project overview</h2>",
                f"<p style='color:#565C6E'>{project.root}</p>"]
        if project.issues:
            rows.append("<h3 style='color:#8A6D1C'>Problems</h3><ul>")
            rows.extend(f"<li>{issue}</li>" for issue in project.issues)
            rows.append("</ul>")
        rows.append("<h3>Texts</h3>")
        rows.append("<table cellpadding='6' style='border-collapse:collapse'>"
                    "<tr style='text-align:left;color:#565C6E'>"
                    "<th>Text</th><th>Annotations</th><th>Page</th>"
                    "<th>Source</th><th>Material</th><th>Card</th></tr>")
        for doc in project.documents:
            # Load every text's notes so the table says how many there are,
            # and so a file that cannot be read shows up here rather than
            # only when that text happens to be selected.
            try:
                store = self.session.store_for(doc)
            except Exception:                      # noqa: BLE001 - shown as a dash
                store = None
            count = store.count if store is not None else "—"
            if store is not None and store.unreadable:
                count = "<span style='color:#A3341F'>unreadable</span>"
            if only_issues and not doc.issues:
                continue
            marks = []
            for value in (doc.page, doc.source, doc.material, doc.card):
                marks.append("<span style='color:#2F6B3A'>✔</span>" if value is not None
                             else "<span style='color:#A3341F'>✖</span>")
            note = ""
            if doc.issues:
                note = ("<br><span style='color:#8A6D1C;font-size:11px'>"
                        + "<br>".join(doc.issues) + "</span>")
            rows.append(
                f"<tr><td><b>{doc.title or doc.name}</b>{note}</td>"
                f"<td>{count}</td><td>{marks[0]}</td><td>{marks[1]}</td>"
                f"<td>{marks[2]}</td><td>{marks[3]}</td></tr>")
        rows.append("</table>")
        self.overview.setHtml("".join(rows))

    # -- filtering and selection --------------------------------------------

    def _filter_changed(self) -> None:
        self.proxy.set_text(self.search.text())
        self.proxy.set_author(self.author_filter.currentData() or "")
        self.proxy.set_only_orphans(self.orphan_only.isChecked())
        visible = self.proxy.rowCount()
        total = self.model.rowCount()
        if visible != total:
            self.count_label.setText(f"{visible} of {total} shown")

    def _current_row(self):
        rows = self._current_rows()
        return rows[0] if rows else None

    def _current_rows(self):
        rows = []
        selection = self.table.selectionModel()
        if selection is None:
            return rows
        for index in selection.selectedRows():
            source = self.proxy.mapToSource(index)
            pid = self.model.data(source, PARA_ROLE)
            note_index = self.model.data(source, INDEX_ROLE)
            note = self.model.data(source, NOTE_ROLE)
            if pid is not None:
                rows.append((pid, note_index, note))
        return rows

    def _selected_para(self) -> Optional[str]:
        row = self._current_row()
        return row[0] if row else None

    def _select_row(self, para: str, note_index: int) -> None:
        source_row = self.model.row_for(para, note_index)
        if source_row < 0:
            return
        index = self.proxy.mapFromSource(self.model.index(source_row, 0))
        if index.isValid():
            self.table.selectRow(index.row())
            self.table.scrollTo(index)

    def _show_detail(self) -> None:
        store = self.session.store()
        row = self._current_row()
        if store is None or row is None:
            self.detail.setHtml(
                "<p style='color:#8B8F9C'>Select an annotation to see the whole "
                "note and the paragraph it belongs to.</p>")
            return
        para, index, note = row
        label = store.label_for(para)
        paragraph = store.paragraph_text(para)
        parts = [f"<h3 style='margin:0'>{label} {para}</h3>"]
        if paragraph:
            parts.append(f"<blockquote style='color:#565C6E;border-left:3px solid "
                         f"#DDD6C4;padding-left:8px;margin:6px 0'>"
                         f"{_esc(paragraph)}</blockquote>")
        if store.is_orphan(para):
            parts.append("<p style='color:#A3341F'>This paragraph is not in the "
                         "current source text, so the page will not show it.</p>")
        parts.append(f"<p><b>{_esc(note.get('author') or 'Unknown')}</b>"
                     + (f" — {_esc(note.get('title'))}" if note.get('title') else "")
                     + "</p>")
        parts.append(f"<p>{_esc(note.get('text', ''))}</p>")
        if note.get("source"):
            parts.append(f"<p style='color:#8B8F9C'>Source: {_esc(note['source'])}</p>")
        self.detail.setHtml("".join(parts))

    def _table_menu(self, position) -> None:
        if self.session.current is None:
            return
        menu = QMenu(self)
        menu.addAction(self.act_add)
        if self._current_row() is not None:
            menu.addAction(self.act_edit)
            menu.addAction(self.act_duplicate)
            menu.addSeparator()
            menu.addAction(self.act_move_up)
            menu.addAction(self.act_move_down)
            menu.addSeparator()
            menu.addAction(self.act_delete)
            menu.addSeparator()
            copy = QAction("Copy the note text", self)
            copy.triggered.connect(self._copy_selected_text)
            menu.addAction(copy)
        menu.exec(self.table.viewport().mapToGlobal(position))

    def _copy_selected_text(self) -> None:
        rows = self._current_rows()
        if rows:
            QApplication.clipboard().setText(rows[0][2].get("text", ""))

    # -- log -----------------------------------------------------------------

    def _on_logged(self, level: str, message: str, detail: str) -> None:
        colour = {"info": "#565C6E", "warn": "#8A6D1C", "error": "#A3341F"}.get(
            level, "#565C6E")
        mark = {"info": "·", "warn": "!", "error": "✖"}.get(level, "·")
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(colour))
        cursor.insertText(f"{mark} {message}\n", fmt)
        if detail and self.log_detail.isChecked():
            quiet = QTextCharFormat()
            quiet.setForeground(QColor("#8B8F9C"))
            for line in str(detail).splitlines():
                cursor.insertText(f"    {line}\n", quiet)
        self.log_view.setTextCursor(cursor)
        self.log_view.ensureCursorVisible()

    def _on_busy(self, running: bool, label: str) -> None:
        self.progress.setVisible(running)
        if running:
            self.progress.setRange(0, 0)
            self.status_label.setText(label)
        else:
            self.progress.setRange(0, 100)
        for action in (self.act_open, self.act_reload, self.act_build,
                       self.act_build_all, self.act_convert, self.act_import,
                       self.act_export):
            action.setEnabled(not running and self._action_would_be_enabled(action))

    def _action_would_be_enabled(self, action) -> bool:
        if action in (self.act_open,):
            return True
        if self.session.project is None:
            return False
        if action in (self.act_reload, self.act_build_all, self.act_convert):
            return True
        return self.session.current is not None

    def _on_progress(self, stage: str, fraction: float, message: str) -> None:
        if fraction:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(fraction * 100))
        if message:
            self.status_label.setText(message if not stage else f"{stage}: {message}")

    def _on_external_change(self, name: str, description: str) -> None:
        store = self.session.stores.get(name)
        if store is None or self.session.current is None:
            return
        if name != self.session.current.name:
            self.session.warn(f"{name}: {description}")
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Changed outside the application")
        box.setText(description)
        box.setInformativeText(
            "Reload it to see the change here, or keep working and save over it.")
        reload_button = box.addButton("Reload from disk", QMessageBox.AcceptRole)
        box.addButton("Leave it", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is reload_button:
            self.session.reload_document(self.session.current)

    # -- misc ----------------------------------------------------------------

    def _fill_recent(self) -> None:
        self.recent_menu.clear()
        recent = self.session.recent_projects()
        if not recent:
            empty = self.recent_menu.addAction("(nothing yet)")
            empty.setEnabled(False)
            return
        for path in recent:
            action = self.recent_menu.addAction(path)
            action.triggered.connect(lambda _=False, p=path: self.open_path(Path(p)))

    def _require_document(self) -> bool:
        if self.session.current is None:
            self.status_label.setText("Select a text in the project first.")
            return False
        return True

    def _confirm_discard(self, message: str = "") -> bool:
        dirty = self.session.dirty_documents()
        if not dirty:
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Unsaved changes")
        box.setText(f"{', '.join(dirty)} has unsaved changes.")
        box.setInformativeText(message or "Save before continuing?")
        box.setStandardButtons(QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Save)
        choice = box.exec()
        if choice == QMessageBox.Cancel:
            return False
        if choice == QMessageBox.Save:
            return self.session.save_all() >= 0
        return True

    def _report(self, exc: ToolError) -> None:
        self.session.error(exc.message, exc.hint or "")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("That did not work")
        box.setText(exc.message)
        if exc.hint:
            box.setInformativeText(exc.hint)
        box.exec()

    def _report_failure(self, title: str, failure) -> None:
        message, hint = failure
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle(title)
        box.setText(message)
        if hint:
            box.setInformativeText(hint)
        box.exec()

    def closeEvent(self, event) -> None:
        if not self._confirm_discard("Save before closing?"):
            event.ignore()
            return
        event.accept()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _leaf(label: str, path: Optional[Path]) -> QTreeWidgetItem:
    if path is None:
        item = QTreeWidgetItem([f"{label}: not found"])
        item.setForeground(0, QColor("#8B8F9C"))
        item.setData(0, Qt.UserRole, ("missing", label))
        return item
    item = QTreeWidgetItem([f"{label}: {Path(path).name}"])
    item.setToolTip(0, str(path))
    item.setData(0, Qt.UserRole, ("file", str(path)))
    return item


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _prompt_text(parent, title: str, label: str, default: str) -> Tuple[str, bool]:
    from PySide6.QtWidgets import QInputDialog
    text, ok = QInputDialog.getText(parent, title, label, QLineEdit.Normal, default)
    return text.strip(), ok
