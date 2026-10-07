"""Qt models over the annotation store.

The store is the source of truth; these are views of it.  Keeping the table
model thin means every edit goes through :class:`ilatool.notestore.NoteStore`,
so undo, the changed flag and the atomic save all keep working no matter
which widget started the edit.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QSortFilterProxyModel,
                            Qt)
from PySide6.QtGui import QColor, QFont

from .. import notestore, textutil

COLUMNS = ("Paragraph", "Author", "Title", "Note", "Source", "State")

#: Roles used to carry the raw note through the proxy.
PARA_ROLE = Qt.UserRole + 1
INDEX_ROLE = Qt.UserRole + 2
NOTE_ROLE = Qt.UserRole + 3


class AnnotationTableModel(QAbstractTableModel):
    """Every annotation of one text, in file order."""

    def __init__(self, store: Optional[notestore.NoteStore] = None, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self._rows: List[Tuple[str, int, Dict[str, str]]] = []
        self.refresh()

    def set_store(self, store: Optional[notestore.NoteStore]) -> None:
        self.beginResetModel()
        self.store = store
        self._rows = store.entries() if store is not None else []
        self.endResetModel()

    def refresh(self) -> None:
        self.beginResetModel()
        self._rows = self.store.entries() if self.store is not None else []
        self.endResetModel()

    # -- Qt plumbing ---------------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        para, note_index, note = self._rows[index.row()]
        column = index.column()

        if role in (Qt.DisplayRole, Qt.ToolTipRole):
            return self._text_for(para, note, column, role == Qt.ToolTipRole)
        if role == PARA_ROLE:
            return para
        if role == INDEX_ROLE:
            return note_index
        if role == NOTE_ROLE:
            return note
        if role == Qt.ForegroundRole and self._is_orphan(para):
            return QColor("#A3341F")
        if role == Qt.FontRole and column == 0:
            font = QFont()
            font.setBold(True)
            return font
        if role == Qt.TextAlignmentRole and column == len(COLUMNS) - 1:
            return int(Qt.AlignCenter)
        return None

    # -- contents ------------------------------------------------------------

    def _is_orphan(self, para: str) -> bool:
        return bool(self.store) and self.store.is_orphan(para)

    def _text_for(self, para: str, note: Dict[str, str], column: int,
                  tooltip: bool) -> str:
        store = self.store
        label = store.label_for(para) if store else ""
        paragraph = store.paragraph_text(para) if store else ""
        if column == 0:
            head = label if label and label not in ("¶",) else ""
            snippet = textutil.truncate_end(textutil.normalize_whitespace(paragraph), 70)
            if tooltip:
                return f"{para}\n\n{paragraph}"
            return f"{head} {snippet}".strip() or para
        if column == 1:
            return note.get("author", "") or "—"
        if column == 2:
            return textutil.truncate_end(note.get("title", ""), 40)
        if column == 3:
            body = textutil.normalize_whitespace(note.get("text", ""))
            return body if tooltip else textutil.truncate_end(body, 110)
        if column == 4:
            return textutil.truncate_end(note.get("source", ""), 40) if not tooltip \
                else note.get("source", "")
        if column == 5:
            if self._is_orphan(para):
                return "orphaned"
            return "placed"
        return ""

    # -- lookup --------------------------------------------------------------

    def row_for(self, para: str, note_index: int) -> int:
        for row, (pid, index, _note) in enumerate(self._rows):
            if pid == para and index == note_index:
                return row
        return -1

    def rows_for_paras(self, paras) -> List[int]:
        wanted = set(paras)
        return [row for row, (pid, _i, _n) in enumerate(self._rows) if pid in wanted]

    def selected_rows(self) -> List[Tuple[str, int, Dict[str, str]]]:
        return [self._rows[row] for row in range(len(self._rows))]


class AnnotationFilterModel(QSortFilterProxyModel):
    """Search across every field, plus author and state filters."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setDynamicSortFilter(True)
        self.text = ""
        self.author = ""
        self.only_orphans = False
        self.only_notes_with_source = False

    # Qt 6 renamed invalidateFilter(); invalidateRowsFilter is the supported
    # call and exists in every Qt this application runs on.
    def set_text(self, value: str) -> None:
        self.text = (value or "").strip().lower()
        self.invalidateRowsFilter()

    def set_author(self, value: str) -> None:
        self.author = (value or "").strip()
        self.invalidateRowsFilter()

    def set_only_orphans(self, value: bool) -> None:
        self.only_orphans = bool(value)
        self.invalidateRowsFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if model is None:
            return True
        index = model.index(source_row, 0, source_parent)
        para = model.data(index, PARA_ROLE)
        note = model.data(index, NOTE_ROLE) or {}
        if self.author and (note.get("author") or "").strip() != self.author:
            return False
        if self.only_orphans and not model._is_orphan(para):
            return False
        if self.text:
            haystack = " ".join([
                note.get("text", ""), note.get("title", ""), note.get("author", ""),
                note.get("source", ""), model._text_for(para, note, 0, False), para,
            ]).lower()
            if self.text not in haystack:
                return False
        return True
