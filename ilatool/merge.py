"""Bringing annotations in from somewhere else.

Importing is a merge, not a replace.  The project's own rule decides whether
two notes are the same note (:func:`ilatool.notestore.same_note`, which is
the builder's ``_is_same_note`` rule applied to the same paragraph), and the
project's own matcher decides which paragraph an incoming note belongs to
(:func:`ilatool.annotations.attach`).  Nothing here invents a second way of
reading a note.

What this module adds is the part a build never needed: an account of what
*would* happen, so a person can look at it before anything is written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import annotations as ann_mod
from . import docx as docx_mod
from . import notestore
from . import sourcefmt
from . import backends
from .errors import InputError
from .notestore import NoteStore, clean_note, same_note
from .sourcefmt import SourceDocument

#: How the plan classifies one incoming note.
NEW = "new"
DUPLICATE = "duplicate"
CONFLICT = "conflict"

#: What to do with a conflict.
KEEP_EXISTING = "keep"
TAKE_INCOMING = "take"
KEEP_BOTH = "both"


@dataclass
class MergeItem:
    kind: str
    para: str
    note: Dict[str, str]
    existing: Optional[Dict[str, str]] = None
    reason: str = ""

    @property
    def label(self) -> str:
        return {"new": "New", "duplicate": "Duplicate",
                "conflict": "Conflict"}.get(self.kind, self.kind)


@dataclass
class MergePlan:
    """What an import would do, before it does any of it."""

    label: str
    items: List[MergeItem] = field(default_factory=list)
    #: annotations that could not be attached, with the reason
    unplaced: List[Tuple[str, str]] = field(default_factory=list)
    #: notes the incoming set itself repeated
    internal_duplicates: int = 0

    def of(self, kind: str) -> List[MergeItem]:
        return [item for item in self.items if item.kind == kind]

    @property
    def new(self) -> List[MergeItem]:
        return self.of(NEW)

    @property
    def duplicates(self) -> List[MergeItem]:
        return self.of(DUPLICATE)

    @property
    def conflicts(self) -> List[MergeItem]:
        return self.of(CONFLICT)

    def counts(self) -> Dict[str, int]:
        return {"new": len(self.new), "duplicate": len(self.duplicates),
                "conflict": len(self.conflicts), "unplaced": len(self.unplaced)}

    def summary(self) -> str:
        bits = [f"{len(self.new)} new"]
        if self.duplicates:
            bits.append(f"{len(self.duplicates)} already present")
        if self.conflicts:
            bits.append(f"{len(self.conflicts)} conflicting")
        if self.unplaced:
            bits.append(f"{len(self.unplaced)} unplaceable")
        return ", ".join(bits)

    def apply(self, store: NoteStore,
              decisions: Optional[Dict[int, str]] = None) -> Tuple[int, int, int]:
        """Carry out the plan.  Returns ``(added, replaced, skipped)``.

        ``decisions`` maps an item's position in :attr:`items` to one of
        :data:`KEEP_EXISTING`, :data:`TAKE_INCOMING` or :data:`KEEP_BOTH`,
        for the conflicts; anything unset keeps the existing note.
        """
        decisions = decisions or {}
        added = replaced = skipped = 0
        pending: Dict[str, List[Dict[str, str]]] = {}

        for index, item in enumerate(self.items):
            if item.kind == DUPLICATE:
                skipped += 1
                continue
            if item.kind == NEW:
                bucket = pending.setdefault(item.para, [])
                if not any(same_note(item.note, other) for other in bucket):
                    bucket.append(item.note)
                else:
                    skipped += 1
                continue

            choice = decisions.get(index, KEEP_EXISTING)
            if choice == KEEP_EXISTING:
                skipped += 1
            elif choice == TAKE_INCOMING:
                if self._replace(store, item):
                    replaced += 1
                else:
                    pending.setdefault(item.para, []).append(item.note)
                    added += 1
            else:                                   # KEEP_BOTH
                pending.setdefault(item.para, []).append(item.note)
                added += 1

        for para, notes in pending.items():
            added += store.merge_entries(para, notes)
        return added, replaced, skipped

    @staticmethod
    def _replace(store: NoteStore, item: MergeItem) -> bool:
        bucket = store.notes.get(item.para) or []
        for index, existing in enumerate(bucket):
            if item.existing is not None and not same_note(existing, item.existing):
                continue
            store.update(item.para, index, item.note)
            return True
        return False


@dataclass
class Incoming:
    """Annotations loaded from a file, and where they landed."""

    path: Path
    notes: Dict[str, List[Dict[str, str]]]
    unplaced: List[Tuple[str, str]]
    total: int
    label: str
    #: annotations that could not be resolved because there was no source text
    needs_source: bool = False

    @property
    def count(self) -> int:
        return sum(len(v) for v in self.notes.values())


def load_incoming(path: Path, source: Optional[SourceDocument] = None,
                  backend: str = "auto") -> Incoming:
    """Read an annotation file of any supported kind and place its notes.

    Placement needs the paragraph ids, which live in the text's source.  When
    there is no source, notes that name a paragraph outright can still be
    read; notes anchored to an excerpt cannot, and that is said plainly
    rather than guessed at.
    """
    path = Path(path)
    if not path.exists():
        raise InputError(f"there is no file at '{path}'")
    suffix = path.suffix.lower()
    if suffix == ".docx":
        items = docx_mod.annotations_from_docx(path)
    elif suffix == ".pdf":
        items = ann_mod.annotations_from_pdf(backends.load_pdf(path, backend=backend))
    else:
        items = ann_mod.load_annotations(path)
    return from_annotations(items, path, source)


def from_annotations(items: Sequence[ann_mod.Annotation], path: Path,
                     source: Optional[SourceDocument] = None) -> Incoming:
    total = len(items)
    label = path.name if path is not None else "annotations"
    if source is None:
        notes: Dict[str, List[Dict[str, str]]] = {}
        unresolved: List[Tuple[str, str]] = []
        for ann in items:
            para = (ann.para or "").strip()
            if para and not para.startswith("para-"):
                para = f"para-{para}"
            if para and ann.text:
                notes.setdefault(para, []).append(clean_note(ann.to_note()))
            else:
                unresolved.append((
                    (ann.text or ann.quote or "")[:70],
                    "there is no source text for this document, so an excerpt "
                    "cannot be matched to a paragraph"))
        return Incoming(path=path, notes=notes, unplaced=unresolved, total=total,
                        label=label, needs_source=bool(unresolved))

    result = ann_mod.attach(items, source)
    unplaced = [((a.text or a.quote or "")[:70], why) for a, why in result.unplaced]
    notes = {para: [clean_note(n) for n in entries]
             for para, entries in result.notes.items()}
    return Incoming(path=path, notes=notes, unplaced=unplaced, total=total,
                    label=label)


#: Two notes on the same paragraph, by the same author, whose text is alike
#: but not the same.  That is an edit of one note, not two notes.
SIMILAR_ENOUGH = 0.55


def plan_merge(store: NoteStore, incoming: Incoming) -> MergePlan:
    """Compare an incoming set against what the project already has."""
    plan = MergePlan(label=incoming.label)
    plan.unplaced = list(incoming.unplaced)
    seen_incoming: Dict[str, List[Dict[str, str]]] = {}

    for para, entries in incoming.notes.items():
        for note in entries:
            # The incoming set can repeat itself; that is not a conflict with
            # the project, so it is counted separately.
            so_far = seen_incoming.setdefault(para, [])
            if any(same_note(note, other) for other in so_far):
                plan.internal_duplicates += 1
                continue
            so_far.append(note)

            existing = store.notes.get(para) or []
            twin = next((e for e in existing if same_note(note, e)), None)
            if twin is not None:
                plan.items.append(MergeItem(
                    DUPLICATE, para, note, twin,
                    "this note is already on that paragraph"))
                continue

            clash = _closest_conflict(note, existing)
            if clash is not None:
                plan.items.append(MergeItem(
                    CONFLICT, para, note, clash,
                    _conflict_reason(note, clash)))
                continue

            plan.items.append(MergeItem(NEW, para, note, None,
                                        "not in this project yet"))
    return plan


def _closest_conflict(note: Dict[str, str],
                      existing: Iterable[Dict[str, str]]) -> Optional[Dict[str, str]]:
    for other in existing:
        if (note.get("author") or "").strip() != (other.get("author") or "").strip():
            continue
        text_a = _norm(note.get("text", ""))
        text_b = _norm(other.get("text", ""))
        if not text_a or not text_b:
            continue
        score = ann_mod._similarity(text_a, text_b)
        if SIMILAR_ENOUGH <= score < 1.0:
            return other
    return None


def _conflict_reason(note: Dict[str, str], other: Dict[str, str]) -> str:
    score = ann_mod._similarity(_norm(note.get("text", "")),
                                _norm(other.get("text", "")))
    return (f"{note.get('author') or 'the same author'} already has a note on "
            f"this paragraph that is {score:.0%} the same — probably an edit "
            f"of it")


def _norm(text: str) -> str:
    from . import textutil
    return textutil.normalize_for_match(text)


def merge_annotations(store: NoteStore, incoming: Incoming,
                      decisions: Optional[Dict[int, str]] = None
                      ) -> Tuple[MergePlan, int, int, int]:
    """Plan and carry out a merge in one step.  Used by tests and the CLI."""
    plan = plan_merge(store, incoming)
    added, replaced, skipped = plan.apply(store, decisions)
    return plan, added, replaced, skipped
