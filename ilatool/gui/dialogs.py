"""The annotation editor and the merge preview.

Both are ordinary Qt dialogs.  They do not touch files: the editor returns a
note and the merge dialog returns a set of decisions, and the window applies
them through the session, which is what keeps the saving and undo rules in one
place.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QTextOption
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit,
                               QPushButton, QSplitter, QStackedWidget, QTableWidget,
                               QTableWidgetItem, QTextBrowser, QTextEdit,
                               QVBoxLayout, QWidget)

from .. import merge as merge_mod, notestore, textutil
from ..notestore import NoteStore


class AnnotationEditor(QDialog):
    """Add or edit one note, with every field the format supports."""

    def __init__(self, store: NoteStore, para: str = "", note: Optional[Dict[str, str]] = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit annotation" if note else "New annotation")
        self.setModal(True)
        self.resize(720, 560)
        self.store = store
        self._para = para
        self._note = dict(note or {})

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self.target = QComboBox()
        self.target.setEditable(True)
        self.target.setInsertPolicy(QComboBox.NoInsert)
        self._fill_targets(store, para)
        self.target.currentIndexChanged.connect(self._target_changed)
        form.addRow("Paragraph", self.target)

        self.target_preview = QLabel()
        self.target_preview.setWordWrap(True)
        self.target_preview.setStyleSheet("color: #565C6E;")
        self.target_preview.setMinimumHeight(44)
        self.target_preview.setAlignment(Qt.AlignTop)
        form.addRow("", self.target_preview)

        self.author = QLineEdit(self._note.get("author", ""))
        self.author.setPlaceholderText("Who wrote this note?")
        form.addRow("Author", self.author)

        self.title = QLineEdit(self._note.get("title", ""))
        self.title.setPlaceholderText("Optional short heading")
        form.addRow("Title", self.title)

        self.source = QLineEdit(self._note.get("source", ""))
        self.source.setPlaceholderText("Optional citation or link")
        form.addRow("Source", self.source)

        layout.addLayout(form)

        note_label = QLabel("Note")
        layout.addWidget(note_label)
        self.text = QPlainTextEdit(self._note.get("text", ""))
        self.text.setPlaceholderText("The annotation itself.")
        self.text.setTabChangesFocus(True)
        layout.addWidget(self.text, 1)

        self.error = QLabel()
        self.error.setStyleSheet("color: #A3341F;")
        self.error.setWordWrap(True)
        layout.addWidget(self.error)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText(
            "Save changes" if note else "Add annotation")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.text.setFocus()
        self._target_changed()

    # -- helpers -------------------------------------------------------------

    def _fill_targets(self, store: NoteStore, para: str) -> None:
        self.target.addItem("— choose a paragraph —", "")
        for block_id, text in store.paragraphs.items():
            label = store.label_for(block_id)
            head = f"{label} " if label and label != "¶" else ""
            snippet = textutil.truncate_end(
                textutil.normalize_whitespace(text), 72) or "(empty)"
            self.target.addItem(f"{head}{snippet}", block_id)
        # A paragraph the store only knows as an id (an orphan) can still be
        # typed in by hand.
        if para and para not in store.paragraphs:
            self.target.addItem(f"{para} (not in the current source)", para)
        index = self.target.findData(para)
        self.target.setCurrentIndex(index if index >= 0 else 0)

    def _current_para(self) -> str:
        data = self.target.currentData()
        if data:
            return str(data)
        return (self.target.currentText() or "").strip()

    def _target_changed(self) -> None:
        para = self._current_para()
        body = self.store.paragraph_text(para)
        if body:
            self.target_preview.setText(textutil.truncate_end(body, 420))
        elif para:
            self.target_preview.setText(
                "This paragraph id is not in the current source text. "
                "The note will be saved, but the page will not show it until "
                "the source contains it.")
        else:
            self.target_preview.setText("")

    # -- validation ----------------------------------------------------------

    def result_note(self) -> Dict[str, str]:
        return notestore.clean_note({
            "author": self.author.text(),
            "title": self.title.text(),
            "source": self.source.text(),
            "text": self.text.toPlainText(),
        })

    def _accept(self) -> None:
        para = self._current_para()
        note = self.result_note()
        problems: List[str] = []
        if not para:
            problems.append("Choose the paragraph this note belongs to.")
        if not note.get("text"):
            problems.append("A note needs some text.")
        if len(note.get("text", "")) > 20000:
            problems.append("That note is unusually long (over 20,000 characters).")
        if problems:
            self.error.setText(" • ".join(problems))
            return
        self.selected_para = para
        self.selected_note = note
        self.accept()


class ExportDialog(QDialog):
    """Pick what to export and in which of the project's formats."""

    def __init__(self, store: NoteStore, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export annotations")
        self.store = store
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<b>{store.count}</b> annotation(s) on "
                                f"<b>{len(store.targets())}</b> paragraph(s)."))
        form = QFormLayout()
        self.format = QComboBox()
        self.format.addItem("Plain text (.txt) — the tool's own format", "txt")
        self.format.addItem("JSON (.json) — one object per note", "json")
        self.format.addItem("notes.js (.js) — exactly what the page loads", "js")
        form.addRow("Format", self.format)
        self.only_authors = QComboBox()
        self.only_authors.addItem("Everyone", "")
        for author in store.authors():
            self.only_authors.addItem(author, author)
        form.addRow("Author", self.only_authors)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Export…")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class MergeDialog(QDialog):
    """Show exactly what an import would do, and let the user decide."""

    NEW, DUP, CONFLICT = range(3)

    def __init__(self, plan: merge_mod.MergePlan, store: NoteStore,
                 document_name: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Import into {document_name}")
        self.setModal(True)
        self.resize(980, 640)
        self.plan = plan
        self.store = store
        self.decisions: Dict[int, str] = {}

        layout = QVBoxLayout(self)
        header = QLabel(
            f"<b>{plan.label}</b> — {plan.summary()}"
            + (f" · {plan.internal_duplicates} repeated inside the file"
               if plan.internal_duplicates else ""))
        header.setWordWrap(True)
        layout.addWidget(header)

        body = QSplitter(Qt.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        for label, count, kind in (
                ("New annotations", len(plan.new), self.NEW),
                ("Already in this project", len(plan.duplicates), self.DUP),
                ("Conflicts to resolve", len(plan.conflicts), self.CONFLICT)):
            item = QListWidgetItem(f"{label}  ({count})")
            item.setData(Qt.UserRole, kind)
            self.list.addItem(item)
            item.setFlags(item.flags() | Qt.ItemIsEnabled)
        if plan.unplaced:
            item = QListWidgetItem(f"Could not be placed  ({len(plan.unplaced)})")
            item.setData(Qt.UserRole, 3)
            self.list.addItem(item)
        self.list.currentRowChanged.connect(self._show)
        left_layout.addWidget(self.list)
        body.addWidget(left)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["", "Paragraph", "Author", "Note"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._detail)
        body.addWidget(self.table)
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)
        body.setSizes([240, 740])
        layout.addWidget(body, 1)

        self.detail = QTextBrowser()
        self.detail.setMinimumHeight(140)
        layout.addWidget(self.detail)

        row = QHBoxLayout()
        self.decision = QComboBox()
        self.decision.addItem("Keep the existing note", merge_mod.KEEP_EXISTING)
        self.decision.addItem("Replace it with the imported note", merge_mod.TAKE_INCOMING)
        self.decision.addItem("Keep both", merge_mod.KEEP_BOTH)
        self.decision.currentIndexChanged.connect(self._decide)
        row.addWidget(QLabel("For the selected conflict:"))
        row.addWidget(self.decision)
        row.addStretch(1)
        self.apply_all = QPushButton("Apply this choice to every conflict")
        self.apply_all.clicked.connect(self._apply_to_all)
        row.addWidget(self.apply_all)
        layout.addLayout(row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok = buttons.button(QDialogButtonBox.Ok)
        self.ok.setText("Apply merge")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if plan.items:
            self.list.setCurrentRow(0)
        self._update_ok()

    # -- display -------------------------------------------------------------

    def _current_kind(self) -> int:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else -1

    def _show(self) -> None:
        kind = self._current_kind()
        self.table.setRowCount(0)
        if kind == 3:
            for text, why in self.plan.unplaced:
                row = self.table.rowCount()
                self.table.insertRow(row)
                self.table.setItem(row, 0, QTableWidgetItem("!"))
                self.table.setItem(row, 1, QTableWidgetItem(text))
                self.table.setItem(row, 2, QTableWidgetItem(""))
                self.table.setItem(row, 3, QTableWidgetItem(why))
            return
        items = {self.NEW: self.plan.new, self.DUP: self.plan.duplicates,
                 self.CONFLICT: self.plan.conflicts}.get(kind, [])
        for item in items:
            row = self.table.rowCount()
            self.table.insertRow(row)
            marker = {merge_mod.NEW: "+", merge_mod.DUPLICATE: "=",
                      merge_mod.CONFLICT: "!"}.get(item.kind, "?")
            note = item.note
            label = self.store.label_for(item.para)
            snippet = textutil.truncate_end(
                textutil.normalize_whitespace(self.store.paragraph_text(item.para)), 50)
            cell = QTableWidgetItem(marker)
            cell.setData(Qt.UserRole, self.plan.items.index(item))
            self.table.setItem(row, 0, cell)
            self.table.setItem(row, 1, QTableWidgetItem(f"{label} {snippet}".strip()))
            self.table.setItem(row, 2, QTableWidgetItem(note.get("author", "—")))
            self.table.setItem(row, 3, QTableWidgetItem(
                textutil.truncate_end(textutil.normalize_whitespace(note.get("text", "")), 120)))
        self._detail()

    def _detail(self) -> None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            self.detail.setHtml(
                "<p style='color:#8B8F9C'>Select a row to see the whole note and "
                "the paragraph it belongs to.</p>")
            return
        row = rows[0].row()
        marker = self.table.item(row, 0)
        if marker is None or marker.data(Qt.UserRole) is None:
            self.detail.setHtml("<p style='color:#8B8F9C'>No further detail.</p>")
            return
        item = self.plan.items[marker.data(Qt.UserRole)]
        parts = [f"<h3 style='margin:0 0 4px 0'>{item.label}</h3>",
                 f"<p style='color:#565C6E;margin:0 0 8px 0'>{item.reason}</p>",
                 f"<p style='margin:0 0 4px 0'><b>Paragraph</b> "
                 f"<span style='color:#8B8F9C'>{item.para}</span></p>",
                 f"<blockquote style='color:#565C6E;border-left:3px solid #DDD6C4;"
                 f"padding-left:8px'>{_escape(self.store.paragraph_text(item.para))}</blockquote>"]
        if item.existing is not None:
            parts.append("<p><b>Already in the project</b></p>")
            parts.append(_note_html(item.existing))
        parts.append("<p><b>Being imported</b></p>")
        parts.append(_note_html(item.note))
        self.detail.setHtml("".join(parts))

    # -- decisions -----------------------------------------------------------

    def _decide(self) -> None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return
        marker = self.table.item(rows[0].row(), 0)
        if marker is None or marker.data(Qt.UserRole) is None:
            return
        index = marker.data(Qt.UserRole)
        if self.plan.items[index].kind != merge_mod.CONFLICT:
            return
        self.decisions[index] = self.decision.currentData()
        self._mark(rows[0].row(), self.decision.currentData())
        self._update_ok()

    def _apply_to_all(self) -> None:
        choice = self.decision.currentData()
        for index, item in enumerate(self.plan.items):
            if item.kind == merge_mod.CONFLICT:
                self.decisions[index] = choice
        self._show()
        self._update_ok()

    def _mark(self, row: int, choice: str) -> None:
        marker = self.table.item(row, 0)
        if marker is None:
            return
        marker.setText({merge_mod.KEEP_EXISTING: "!",
                        merge_mod.TAKE_INCOMING: "→",
                        merge_mod.KEEP_BOTH: "≠"}.get(choice, "!"))

    def _update_ok(self) -> None:
        unresolved = [i for i, item in enumerate(self.plan.items)
                      if item.kind == merge_mod.CONFLICT and i not in self.decisions]
        total = len(self.plan.new) + len(self.plan.duplicates) + len(self.plan.conflicts)
        if unresolved:
            self.ok.setEnabled(False)
            self.ok.setText(f"Resolve {len(unresolved)} conflict(s) to continue")
        else:
            self.ok.setEnabled(True)
            self.ok.setText(f"Apply merge ({len(self.plan.new)} to add)" if total
                            else "Nothing to do")


def _escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _note_html(note: Dict[str, str]) -> str:
    bits = []
    if note.get("author"):
        bits.append(f"<p style='margin:0'><b>{_escape(note['author'])}</b>")
    if note.get("title"):
        bits.append(f" — {_escape(note['title'])}")
    if note.get("author") or note.get("title"):
        bits.append("</p>")
    bits.append(f"<p style='margin:4px 0'>{_escape(note.get('text', ''))}</p>")
    if note.get("source"):
        bits.append(f"<p style='color:#8B8F9C;margin:0'>Source: {_escape(note['source'])}</p>")
    return "".join(bits)
