"""Finding the parts of a website project.

The tool has always worked on loose files: give it a source and an
annotations file.  A *project* is the thing around them -- the folder that
holds the landing page, the generated texts, their notes, and the materials
they were built from.  This module turns a folder into that structure, by
reading what the project itself says rather than by guessing at a layout.

The conventions it knows are the ones this repository actually uses:

    <root>/index.html               the landing page
    <root>/texts/all.html           the card list (which texts exist)
    <root>/texts/<name>.html        a generated text
    <root>/texts/<name>-notes.js    its annotations
    <root>/texts/sources/<name>.txt the extracted source (paragraph ids)
    <root>/materials/...            the PDFs and Word files it came from

Nothing here is hardcoded to those names where the project can be asked
instead.  In particular the notes file is found through the ``<script
src="...notes...">`` tag in the page itself, because that is the file the
page will actually load -- which is how a page whose notes are called
``-notes.v7.js`` still works.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import allhtml
from .errors import InputError

#: Files that are part of the site's own furniture rather than a text.
_NOT_A_TEXT = {"all.html", "index.html"}

_NOTES_SRC_RE = re.compile(r"""<script[^>]+src=["']([^"']*notes[^"']*)["']""", re.I)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)
_H1_RE = re.compile(r"""<h1[^>]*id=["']page-title["'][^>]*>(.*?)</h1>""", re.I | re.S)
_DESC_RE = re.compile(r"""<meta[^>]+name=["']description["'][^>]*content=["']([^"']*)["']""", re.I)
_DESC_RE_ALT = re.compile(r"""<meta[^>]+content=["']([^"']*)["'][^>]*name=["']description["']""", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return _TAG_RE.sub("", fragment or "").strip()


@dataclass
class Document:
    """One generated text: its page, its notes, and where they came from."""

    name: str
    page: Path
    #: the notes file the page loads, if it could be determined
    notes: Optional[Path] = None
    #: the extracted source text, which carries the paragraph ids
    source: Optional[Path] = None
    #: the PDF or Word file the source was extracted from
    material: Optional[Path] = None
    #: every material that could be the source, best first
    material_candidates: List[Path] = field(default_factory=list)
    title: str = ""
    description: str = ""
    #: the card in all.html for this page, if there is one
    card: Optional[Dict[str, str]] = None
    #: non-fatal problems found while looking at this document
    issues: List[str] = field(default_factory=list)

    @property
    def has_notes(self) -> bool:
        return self.notes is not None and self.notes.exists()

    def notes_candidates(self) -> List[Path]:
        """Every plausible notes file for this page, best first."""
        found: List[Path] = []
        if self.notes:
            found.append(self.notes)
        stem = self.page.stem
        if self.page.parent.exists():
            for path in sorted(self.page.parent.glob(f"{stem}-notes*.js")):
                if path not in found:
                    found.append(path)
        return found


@dataclass
class Project:
    """A website project, as discovered on disk."""

    root: Path
    landing: Optional[Path] = None
    all_html: Optional[Path] = None
    texts_dir: Optional[Path] = None
    materials_dir: Optional[Path] = None
    documents: List[Document] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.root.name

    def document(self, name: str) -> Optional[Document]:
        for doc in self.documents:
            if doc.name == name or doc.page.name == name:
                return doc
        return None

    def summary(self) -> str:
        bits = [f"{len(self.documents)} text(s)"]
        if self.landing:
            bits.append("landing page")
        if self.all_html:
            bits.append("card list")
        return f"{self.name}: " + ", ".join(bits)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _find_landing(root: Path) -> Optional[Path]:
    for name in ("index.html", "index.htm"):
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def _find_texts_dir(root: Path) -> Optional[Path]:
    for name in ("texts", "documents", "pages"):
        candidate = root / name
        if candidate.is_dir():
            return candidate
    # A project that keeps its pages beside the landing page.
    if _find_landing(root) is not None:
        return root
    return None


def _find_all_html(root: Path, texts_dir: Optional[Path]) -> Optional[Path]:
    for base in (texts_dir, root):
        if base is None:
            continue
        candidate = base / "all.html"
        if candidate.is_file():
            return candidate
    return None


def _notes_from_page(page: Path) -> Optional[Path]:
    """The notes file the page itself says it loads."""
    try:
        head = page.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = _NOTES_SRC_RE.search(head)
    if not match:
        return None
    src = match.group(1).split("?")[0].strip()
    if not src or src.startswith(("http://", "https://", "//")):
        return None
    candidate = (page.parent / src).resolve()
    try:
        if candidate.is_file():
            return candidate
    except OSError:
        return None
    return None


def _page_meta(page: Path) -> Dict[str, str]:
    try:
        text = page.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    out: Dict[str, str] = {}
    title = _TITLE_RE.search(text) or _H1_RE.search(text)
    if title:
        out["title"] = _text(title.group(1))
    desc = _DESC_RE.search(text) or _DESC_RE_ALT.search(text)
    if desc:
        out["description"] = desc.group(1).strip()
    return out


def _find_source(texts_dir: Path, name: str) -> Optional[Path]:
    for base in (texts_dir / "sources", texts_dir):
        if not base.is_dir():
            continue
        for suffix in (".txt",):
            candidate = base / f"{name}{suffix}"
            if candidate.is_file():
                return candidate
    return None


#: Words too common to identify a document on their own.
_STOPWORDS = {
    "v", "vs", "the", "of", "and", "a", "an", "on", "in", "to", "for",
    "annotated", "plain", "text", "final", "draft", "copy", "comments",
}


def _tokens(text: str) -> set:
    parts = re.split(r"[^0-9a-z]+", text.lower())
    return {p for p in parts if p and p not in _STOPWORDS and len(p) > 1}


def find_materials(root: Path, name: str) -> List[Path]:
    """The PDF or Word file a text was built from.

    Matched on the words that actually distinguish one instrument from
    another, so ``nicaragua-germany`` finds ``Nicaragua v Germany.pdf``
    while ignoring the ``v`` and the ``Annotated`` in the folder.
    """
    materials = root / "materials"
    if not materials.is_dir():
        return []
    wanted = _tokens(name)
    if not wanted:
        return []
    scored: List[tuple] = []
    for path in sorted(materials.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".pdf", ".docx"):
            continue
        if path.name.startswith("~$"):        # Word lock files
            continue
        stem_tokens = _tokens(path.stem)
        if not stem_tokens:
            continue
        overlap = len(wanted & stem_tokens) / len(wanted)
        # The instrument's own name has to be in the file name, not merely
        # share a word with it.
        if overlap < 0.5:
            continue
        # Best overlap first, then the more complete name, then the shorter
        # path so the choice is at least stable between runs.
        score = (round(overlap, 3), len(stem_tokens & wanted), -len(str(path)))
        scored.append((score, path))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [path for _score, path in scored]


def _cards(all_html: Optional[Path]) -> List[Dict[str, str]]:
    if all_html is None or not all_html.is_file():
        return []
    try:
        return allhtml.find_all_html_cases(
            all_html.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return []


def _card_for(cards: Iterable[Dict[str, str]], page: Path,
              texts_dir: Optional[Path]) -> Optional[Dict[str, str]]:
    names = {page.name, page.stem}
    if texts_dir is not None:
        try:
            names.add(str(page.relative_to(texts_dir.parent)))
        except ValueError:
            pass
    for card in cards:
        href = (card.get("href") or "").split("?")[0]
        if not href:
            continue
        href_name = Path(href).name
        if href_name in names or Path(href).stem == page.stem:
            return card
    return None


def _iter_pages(texts_dir: Path, root: Path) -> List[Path]:
    pages = []
    for path in sorted(texts_dir.glob("*.html")):
        if path.name.lower() in _NOT_A_TEXT:
            continue
        if path.parent == root and path.name.lower() == "index.html":
            continue
        pages.append(path)
    return pages


def open_project(folder: Path) -> Project:
    """Discover a project rooted at ``folder``.

    Never raises for a folder that is merely incomplete: what could not be
    found is recorded in ``project.issues`` so the interface can say so
    plainly instead of failing.
    """
    root = Path(folder).expanduser()
    if not root.exists():
        raise InputError(f"there is no folder at '{root}'")
    if not root.is_dir():
        raise InputError(f"'{root}' is a file, not a project folder")
    root = root.resolve()

    project = Project(root=root)
    project.landing = _find_landing(root)
    project.texts_dir = _find_texts_dir(root)
    project.all_html = _find_all_html(root, project.texts_dir)
    materials = root / "materials"
    project.materials_dir = materials if materials.is_dir() else None

    if project.landing is None:
        project.issues.append(
            "no index.html at the top of the folder -- is this the site root?")
    if project.texts_dir is None:
        project.issues.append(
            "no texts/ folder and no index.html beside the pages, so there is "
            "nothing to list")
        return project
    if project.all_html is None:
        project.issues.append(
            "no all.html found -- the texts will not appear on the site index")

    cards = _cards(project.all_html)
    pages = _iter_pages(project.texts_dir, root)
    if not pages:
        project.issues.append(f"no .html pages in {project.texts_dir.name}/")

    for page in pages:
        doc = Document(name=page.stem, page=page)
        doc.notes = _notes_from_page(page)
        candidates = doc.notes_candidates()
        if doc.notes is None and len(candidates) == 1:
            doc.notes = candidates[0]
            doc.issues.append(
                f"{page.name} does not load a notes file; found "
                f"{candidates[0].name} next to it")
        elif doc.notes is None and len(candidates) > 1:
            doc.issues.append(
                "several notes files look plausible ("
                + ", ".join(p.name for p in candidates)
                + ") and the page does not say which it loads")
        elif doc.notes is None:
            doc.issues.append("no notes file for this page")
        if doc.notes is not None and not doc.notes.is_file():
            doc.issues.append(
                f"the page loads {doc.notes.name}, which is not there")
            doc.notes = None
        doc.source = _find_source(project.texts_dir, doc.name)
        candidates = find_materials(root, doc.name)
        doc.material = candidates[0] if candidates else None
        doc.material_candidates = candidates
        if len(candidates) > 1:
            doc.issues.append(
                f"{len(candidates)} materials could be the source of this text "
                f"({', '.join(p.name for p in candidates[:3])}); "
                f"using {candidates[0].name}")
        meta = _page_meta(page)
        doc.title = meta.get("title", "")
        doc.description = meta.get("description", "")
        doc.card = _card_for(cards, page, project.texts_dir)
        if doc.card is None and project.all_html is not None:
            doc.issues.append("no card in all.html, so the site index omits it")
        project.documents.append(doc)

    return project


def candidate_folders(start: Path, limit: int = 400) -> List[Path]:
    """Folders under ``start`` that look like a project root.

    Used by the open dialog's default location and by tests; deliberately
    shallow so it stays fast on a large tree.
    """
    start = Path(start).expanduser()
    out: List[Path] = []
    if _looks_like_project(start):
        out.append(start)
    try:
        for depth, base in enumerate([start, *start.iterdir()]):
            if depth == 0 or not base.is_dir():
                continue
            if base.name.startswith("."):
                continue
            if _looks_like_project(base):
                out.append(base)
            if len(out) >= limit:
                break
    except OSError:
        pass
    return out


def _looks_like_project(folder: Path) -> bool:
    if not folder.is_dir():
        return False
    if (folder / "index.html").is_file():
        return True
    return (folder / "texts" / "all.html").is_file()
