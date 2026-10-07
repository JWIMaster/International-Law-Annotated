"""The annotations of one text, loaded, edited and written back.

A ``notes.js`` is a JSON object mapping a paragraph id onto a list of notes:

    window.NOTES = {
      "para-k1f3e9030": [
        {"author": "J. Smith", "title": "...", "text": "...", "source": "..."}
      ]
    };

This module is the editable form of that file.  It keeps the project's own
format -- the same ``render_notes_js`` the builder uses writes it back, so
there is no second annotation format to keep in step -- and it adds the
things an editor needs which a build does not: an undo history, a changed
flag, and a refusal to overwrite a file that somebody else has edited.

A note has no id in this format.  Its identity is the paragraph it hangs on
plus its text; that is how the builder already de-duplicates notes arriving
from two sources (:func:`ilatool.annotations._is_same_note`), and it is what
merging uses here.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import annotations as ann_mod
from . import fileio
from . import textutil
from .errors import InputError
from .render import render_notes_js

#: The fields a note can carry, in the order the editor shows them.
NOTE_FIELDS = ("author", "title", "text", "source")

#: Fields that must be non-empty for a note to be worth saving.
REQUIRED_FIELDS = ("text",)


def clean_note(entry: Dict[str, str]) -> Dict[str, str]:
    """Drop empty fields, the way the generated file does."""
    out: Dict[str, str] = {}
    for name in NOTE_FIELDS:
        value = (entry.get(name) or "").strip()
        if value:
            out[name] = value
    return out


def same_note(a: Dict[str, str], b: Dict[str, str]) -> bool:
    """Are these two notes the same note?

    Same paragraph is the caller's business; this compares the content using
    the builder's own rule, so a copy-edit does not look like a new note and
    a series that differs only in a citation number stays two notes.
    """
    text_a = textutil.normalize_for_match(a.get("text", ""))
    text_b = textutil.normalize_for_match(b.get("text", ""))
    if not text_a or not text_b:
        return False
    if (a.get("author") or "").strip() != (b.get("author") or "").strip():
        return False
    return ann_mod._is_same_note(text_a, text_b)


@dataclass
class Change:
    """What one edit did, for the status line and for undo."""

    kind: str
    para: str
    summary: str


@dataclass
class NoteStore:
    """Every note of one text, with editing on top."""

    path: Path
    notes: Dict[str, List[Dict[str, str]]] = field(default_factory=dict)
    #: where the notes were when we loaded them, so external edits are noticed
    state: fileio.FileState = None            # type: ignore[assignment]
    dirty: bool = False
    #: paragraph text, when a source is available, for showing context
    paragraphs: Dict[str, str] = field(default_factory=dict)
    labels: Dict[str, str] = field(default_factory=dict)
    #: set when the file on disk could not be read, so it is never
    #: overwritten with the empty store that stands in for it
    unreadable: str = ""
    _undo: List[Dict[str, List[Dict[str, str]]]] = field(default_factory=list)
    _redo: List[Dict[str, List[Dict[str, str]]]] = field(default_factory=list)
    _limit: int = 100

    # -- loading and saving --------------------------------------------------

    @classmethod
    def load(cls, path: Path, paragraphs: Optional[Dict[str, str]] = None,
             labels: Optional[Dict[str, str]] = None) -> "NoteStore":
        path = Path(path)
        if not path.exists():
            # A text with no notes yet is a normal thing to open.
            return cls(path=path, notes={}, state=fileio.FileState(path=path),
                       paragraphs=dict(paragraphs or {}),
                       labels=dict(labels or {}))
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise InputError(f"could not read '{path.name}'", cause=exc) from exc
        notes = parse_notes_lenient(text, path)
        return cls(path=path, notes=notes, state=fileio.FileState.read(path),
                   paragraphs=dict(paragraphs or {}), labels=dict(labels or {}))

    @classmethod
    def unreadable_store(cls, path: Path, message: str,
                         paragraphs: Optional[Dict[str, str]] = None,
                         labels: Optional[Dict[str, str]] = None) -> "NoteStore":
        """A stand-in for a notes file that could not be parsed.

        The document stays open so the problem can be seen and fixed, but it
        holds no notes and refuses to save: replacing a file we could not read
        with an empty one would destroy annotations silently, which is the one
        thing this tool must never do.
        """
        return cls(path=Path(path), notes={}, state=fileio.FileState.read(Path(path)),
                   paragraphs=dict(paragraphs or {}), labels=dict(labels or {}),
                   unreadable=message)

    def reload(self) -> None:
        fresh = NoteStore.load(self.path, self.paragraphs, self.labels)
        self.notes = fresh.notes
        self.state = fresh.state
        self.dirty = False
        self._undo.clear()
        self._redo.clear()

    def render(self) -> str:
        return render_notes_js(self.notes)

    def external_change(self) -> Optional[str]:
        """A description of how the file differs from what we loaded, or None."""
        if not self.path.exists():
            if self.state is not None and self.state.exists:
                return f"{self.path.name} has been deleted since it was opened"
            return None
        current = fileio.FileState.read(self.path)
        if self.state is None or not self.state.exists:
            return f"{self.path.name} appeared on disk since it was opened"
        if current.digest != self.state.digest:
            return f"{self.path.name} has been changed by something else"
        return None

    def save(self, force: bool = False, backup: bool = False) -> Optional[Path]:
        """Write the notes back, refusing to clobber an outside edit."""
        if self.unreadable and not force:
            raise InputError(
                f"'{self.path.name}' could not be read, so it will not be "
                "written over",
                hint=f"{self.unreadable}\n\nFix the file, then reload the "
                     "project, or save over it deliberately.")
        if not force:
            clash = self.external_change()
            if clash:
                raise InputError(
                    clash,
                    hint="Reload the file, or save over it deliberately.")
        made = fileio.atomic_write(self.path, self.render(), backup=backup)
        self.state = fileio.FileState.read(self.path)
        self.dirty = False
        return made

    # -- reading -------------------------------------------------------------

    @property
    def count(self) -> int:
        return sum(len(v) for v in self.notes.values())

    def authors(self) -> List[str]:
        found = {(n.get("author") or "").strip()
                 for entries in self.notes.values() for n in entries}
        found.discard("")
        return sorted(found)

    def targets(self) -> List[str]:
        return list(self.notes.keys())

    def entries(self) -> List[Tuple[str, int, Dict[str, str]]]:
        """Every note as ``(paragraph id, index, note)`` in file order."""
        out: List[Tuple[str, int, Dict[str, str]]] = []
        for para, entries in self.notes.items():
            for index, note in enumerate(entries):
                out.append((para, index, note))
        return out

    def search(self, text: str = "", author: str = "", para: str = "",
               only_orphans: bool = False) -> List[Tuple[str, int, Dict[str, str]]]:
        needle = (text or "").strip().lower()
        rows = []
        for pid, index, note in self.entries():
            if author and (note.get("author") or "").strip() != author:
                continue
            if para and pid != para:
                continue
            if only_orphans and not self.is_orphan(pid):
                continue
            if needle:
                haystack = " ".join([
                    note.get("text", ""), note.get("title", ""),
                    note.get("author", ""), note.get("source", ""),
                    self.paragraphs.get(pid, ""), pid,
                ]).lower()
                if needle not in haystack:
                    continue
            rows.append((pid, index, note))
        return rows

    def paragraph_text(self, para: str) -> str:
        return self.paragraphs.get(para, "")

    def label_for(self, para: str) -> str:
        return self.labels.get(para, "")

    def is_orphan(self, para: str) -> bool:
        """Is this note attached to a paragraph the text no longer has?"""
        return bool(self.paragraphs) and para not in self.paragraphs

    # -- undo ----------------------------------------------------------------

    def _snapshot(self) -> None:
        self._undo.append(copy.deepcopy(self.notes))
        if len(self._undo) > self._limit:
            self._undo.pop(0)
        self._redo.clear()

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(copy.deepcopy(self.notes))
        self.notes = self._undo.pop()
        self.dirty = True
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(copy.deepcopy(self.notes))
        self.notes = self._redo.pop()
        self.dirty = True
        return True

    # -- editing -------------------------------------------------------------

    def add(self, para: str, note: Dict[str, str], index: Optional[int] = None) -> Change:
        para = (para or "").strip()
        if not para:
            raise InputError("a note needs a paragraph to attach to")
        entry = clean_note(note)
        missing = [f for f in REQUIRED_FIELDS if not entry.get(f)]
        if missing:
            raise InputError("a note needs " + " and ".join(missing))
        self._snapshot()
        bucket = self.notes.setdefault(para, [])
        if index is None or index >= len(bucket):
            bucket.append(entry)
        else:
            bucket.insert(max(0, index), entry)
        self.dirty = True
        return Change("add", para, f"added a note to {self._describe(para)}")

    def update(self, para: str, index: int, note: Dict[str, str]) -> Change:
        entry = clean_note(note)
        missing = [f for f in REQUIRED_FIELDS if not entry.get(f)]
        if missing:
            raise InputError("a note needs " + " and ".join(missing))
        bucket = self.notes.get(para)
        if bucket is None or not (0 <= index < len(bucket)):
            raise InputError("that note is no longer there")
        self._snapshot()
        bucket[index] = entry
        self.dirty = True
        return Change("edit", para, f"edited a note on {self._describe(para)}")

    def delete(self, para: str, index: int) -> Change:
        bucket = self.notes.get(para)
        if bucket is None or not (0 <= index < len(bucket)):
            raise InputError("that note is no longer there")
        self._snapshot()
        removed = bucket.pop(index)
        if not bucket:
            del self.notes[para]
        self.dirty = True
        return Change("delete", para,
                      f"removed “{_short(removed.get('text', ''))}” from "
                      f"{self._describe(para)}")

    def delete_many(self, rows: Sequence[Tuple[str, int, Dict[str, str]]]) -> Change:
        self._snapshot()
        # Highest index first so the earlier removals do not shift the later.
        ordered = sorted({(para, index) for para, index, _ in rows},
                         key=lambda item: (item[0], -item[1]))
        for para, index in ordered:
            bucket = self.notes.get(para)
            if bucket and 0 <= index < len(bucket):
                bucket.pop(index)
            if bucket is not None and not bucket:
                self.notes.pop(para, None)
        self.dirty = True
        return Change("delete", "", f"removed {len(ordered)} note(s)")

    def duplicate(self, para: str, index: int) -> Change:
        bucket = self.notes.get(para)
        if bucket is None or not (0 <= index < len(bucket)):
            raise InputError("that note is no longer there")
        self._snapshot()
        copy_of = copy.deepcopy(bucket[index])
        bucket.insert(index + 1, copy_of)
        self.dirty = True
        return Change("duplicate", para, f"duplicated a note on {self._describe(para)}")

    def move(self, para: str, index: int, delta: int) -> Change:
        bucket = self.notes.get(para)
        if bucket is None or not (0 <= index < len(bucket)):
            raise InputError("that note is no longer there")
        target = index + delta
        if not (0 <= target < len(bucket)):
            raise InputError("that note is already at the end")
        self._snapshot()
        bucket.insert(target, bucket.pop(index))
        self.dirty = True
        return Change("reorder", para, "reordered notes on " + self._describe(para))

    def move_to_para(self, para: str, index: int, new_para: str) -> Change:
        bucket = self.notes.get(para)
        if bucket is None or not (0 <= index < len(bucket)):
            raise InputError("that note is no longer there")
        new_para = (new_para or "").strip()
        if not new_para:
            raise InputError("a note needs a paragraph to attach to")
        self._snapshot()
        note = bucket.pop(index)
        if not bucket:
            del self.notes[para]
        self.notes.setdefault(new_para, []).append(note)
        self.dirty = True
        return Change("move", new_para,
                      f"moved a note to {self._describe(new_para)}")

    def merge_entries(self, para: str, notes: Iterable[Dict[str, str]]) -> int:
        """Append notes that are not already there.  Returns how many were added."""
        self._snapshot()
        bucket = self.notes.setdefault(para, [])
        added = 0
        for note in notes:
            entry = clean_note(note)
            if not entry.get("text"):
                continue
            if any(same_note(entry, existing) for existing in bucket):
                continue
            bucket.append(entry)
            added += 1
        if not bucket:
            self.notes.pop(para, None)
        if added:
            self.dirty = True
        else:
            self._undo.pop()
        return added

    def _describe(self, para: str) -> str:
        label = self.label_for(para)
        text = textutil.normalize_whitespace(self.paragraph_text(para))
        if label and label not in ("", "¶"):
            return f"{label} {_short(text, 34)}".strip()
        return _short(text, 40) or para


def text_export_escapes(store: "NoteStore") -> List[str]:
    """Notes whose text had to be protected to survive the text format.

    Reported so the person exporting knows why a note begins with an invisible
    character, and can use JSON instead if they would rather not have it.
    """
    out: List[str] = []
    for para, _index, note in store.entries():
        body = (note.get("text") or "").strip()
        first = body.partition("\n")[0].strip()
        if first and ann_mod.ANN_FIELD_RE.match(first):
            out.append(f"{para}: {first[:60]}")
    return out


def render_annotations_text(store: "NoteStore") -> str:
    """The plain-text annotation format, the one the tool has always used.

    Kept here rather than anywhere near the interface: it is a file format,
    and the command-line tool and the tests use it without Qt.
    """
    lines: List[str] = ["# Annotations exported by Annotation Studio",
                        f"# {store.count} note(s) on {len(store.targets())} paragraph(s)",
                        ""]
    for para, _index, note in store.entries():
        lines.append(f"@{para}")
        for field in ("author", "title", "source"):
            value = (note.get(field) or "").strip()
            if value:
                lines.append(f"{field.capitalize()}: {value}")
        body = (note.get("text") or "").strip()
        first, _, rest = body.partition("\n")
        if ann_mod.ANN_FIELD_RE.match(first.strip()):
            # This line would be read back as a field and the note lost.  One
            # invisible character stops that, and the parser takes it off
            # again, so the note survives the round trip.
            first = ann_mod.ANN_BODY_ESCAPE + first
            body = first + ("\n" + rest if rest else "")
        lines.append(body)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _short(text: str, limit: int = 46) -> str:
    text = textutil.normalize_whitespace(text or "")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def parse_notes_lenient(text: str, path: Optional[Path] = None
                        ) -> Dict[str, List[Dict[str, str]]]:
    """Read a notes file, keeping whatever can be read.

    A single malformed entry should not make a whole file unopenable, so this
    falls back to reading the notes one by one and reports what it could not
    take.  The strict parser is still used first: if the file is fine, the
    result is exactly the same.
    """
    try:
        return ann_mod.parse_notes_js(text)
    except Exception:
        pass
    import json
    import re
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise InputError(
            f"'{path.name if path else 'notes'}' does not look like a notes file",
            hint="Expected 'window.NOTES = {...}' or addNote(...) calls.")
    body = text[start:end + 1]
    try:
        data = json.loads(body, strict=False)
    except json.JSONDecodeError as exc:
        raise InputError(
            f"'{path.name if path else 'notes'}' could not be read as JSON",
            hint=f"Line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    out: Dict[str, List[Dict[str, str]]] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            entries = value if isinstance(value, list) else [value]
            cleaned = [clean_note(e) for e in entries
                       if isinstance(e, dict) and clean_note(e).get("text")]
            if cleaned:
                out[str(key)] = cleaned
    return out
