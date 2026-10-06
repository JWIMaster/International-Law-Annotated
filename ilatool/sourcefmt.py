"""The plain-text ``.txt source`` format: reading and writing.

The format is the one the tool has always used -- optional ``KEY: value``
front matter, a line containing only ``---``, then the body -- with two
additions that make annotations survive a re-conversion:

``IDMAP <old-id>=<key>``
    Front matter may carry any number of ``IDMAP`` lines mapping a legacy
    positional id (``para-12``) onto the content key of the paragraph that
    used to have it.  When a PDF is converted again, the new source keeps
    the old aliases (read from the file being replaced) so a note written
    against ``@para-12`` still attaches to the same sentence.

``{key}`` paragraph prefixes
    Long supported, but now emitted automatically for every paragraph the
    PDF converter produces, using a hash of the paragraph text.  A paragraph
    therefore keeps the same id as long as its text has not changed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .errors import InputError
from .structure import Block, HEADING_KEYWORD_RE, HEADING_WORDS, content_key
from . import textutil

SRC_MARKER_RE = re.compile(
    r"^(¶|§\d*|\d+\.|\(\d+\)|\([a-z]+\)|\([ivxlcdm]+\)|[•·▪‣◦])\s+(.*)$",
    re.IGNORECASE,
)
SRC_KEY_RE = re.compile(r"^\{([\w.-]+)\}\s*(.*)$")
SRC_HEADER_RE = re.compile(r"^##\s+(.*)$")
SRC_META_RE = re.compile(r"^([A-Z][A-Z_]*):\s*(.*)$")
SRC_IDMAP_RE = re.compile(r"^IDMAP\s+([\w.:-]+)\s*=\s*([\w.-]+)\s*$", re.IGNORECASE)

#: Front-matter keys the page template understands.
KNOWN_META = ("TITLE", "DESC", "HEADER", "HOME", "CLOSING")


@dataclass
class SourceDocument:
    path: Optional[Path] = None
    meta: Dict[str, str] = field(default_factory=dict)
    blocks: List[Block] = field(default_factory=list)
    aliases: Dict[str, str] = field(default_factory=dict)

    @property
    def paragraphs(self) -> List[Block]:
        return [b for b in self.blocks if b.kind == "para"]

    @property
    def ids(self) -> set:
        return {b.id for b in self.paragraphs}

    def paragraphs_by_id(self) -> Dict[str, str]:
        return {b.id: b.text for b in self.paragraphs}

    def resolve_ref(self, ref: str) -> Optional[str]:
        """Map an annotation reference onto a paragraph id, or None.

        Accepts ``para-<key>``, a bare ``<key>``, a bare positional number,
        and legacy positional ids via the alias map.
        """
        ref = str(ref).strip()
        if not ref:
            return None
        candidates = [ref]
        if ref.startswith("para-"):
            candidates.append(ref[5:])
        else:
            candidates.append(f"para-{ref}")
        ids = self.ids
        for candidate in candidates:
            if candidate in ids:
                return candidate
        # Legacy positional id -> content key.
        for candidate in candidates:
            key = self.aliases.get(candidate)
            if key:
                mapped = f"para-{key}"
                if mapped in ids:
                    return mapped
        return None


def _heading_level(text: str) -> int:
    m = HEADING_KEYWORD_RE.match(text.strip())
    if m:
        return HEADING_WORDS.get(m.group("word").lower().rstrip("."), 2)
    return 2


def parse_source(path: Path) -> SourceDocument:
    """Read a ``.txt source`` file.  Raises :class:`InputError` on nonsense."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"could not read '{path}'", cause=exc) from exc
    return parse_source_text(text, path)


def parse_source_text(text: str, path: Optional[Path] = None) -> SourceDocument:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    meta: Dict[str, str] = {}
    aliases: Dict[str, str] = {}
    i = 0
    seen_delimiter = False
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == "---":
            i += 1
            seen_delimiter = True
            break
        im = SRC_IDMAP_RE.match(stripped)
        if im:
            aliases[im.group(1)] = im.group(2)
            i += 1
            continue
        m = SRC_META_RE.match(stripped)
        if m:
            meta[m.group(1)] = m.group(2).strip()
        i += 1

    if not seen_delimiter:
        # Front matter is optional, but a file that *starts* with metadata and
        # never says where the body begins is a mistake (usually a missing
        # "---"), and silently treating "TITLE: x" as a paragraph hides it.
        first = next((l.strip() for l in lines if l.strip()), "")
        if SRC_META_RE.match(first) or SRC_IDMAP_RE.match(first):
            raise InputError(
                f"'{path or 'source'}' has front matter but no '---' delimiter",
                hint="Add a line containing only '---' between the metadata and "
                     "the body text.",
            )
        i = 0
        meta = {}

    blocks: List[Block] = []
    auto_n = 0
    seen_ids: set = set()
    order = 0

    for raw in lines[i:]:
        line = raw.strip()
        if not line:
            continue

        hm = SRC_HEADER_RE.match(line)
        if hm:
            title = hm.group(1).strip()
            if not title:
                continue
            order += 1
            blocks.append(Block(kind="heading", text=title, label="",
                                level=_heading_level(title), key=content_key("heading:" + title),
                                order=order))
            continue

        explicit_key = None
        km = SRC_KEY_RE.match(line)
        if km:
            explicit_key = km.group(1)
            line = km.group(2).strip()
            if not line:
                continue

        mm = SRC_MARKER_RE.match(line)
        if mm:
            label, content = mm.group(1), mm.group(2).strip()
        else:
            label, content = "¶", line

        auto_n += 1
        core = explicit_key if explicit_key else str(auto_n)
        para_id = f"para-{core}"
        if para_id in seen_ids:
            raise InputError(
                f"duplicate paragraph id '{para_id}' in '{path or 'source'}'",
                hint="Explicit {keys} must be unique; re-conversion keeps them "
                     "stable, so duplicate keys mean the file was edited by hand.",
            )
        seen_ids.add(para_id)
        order += 1
        blocks.append(Block(kind="para", text=content, label=label, key=core,
                            order=order))

    if not any(b.kind == "para" for b in blocks):
        raise InputError(
            f"no paragraphs found in '{path or 'source'}'",
            hint="The body starts after a line containing only '---'. Check that "
                 "the delimiter is present and that the body is not empty.",
        )

    return SourceDocument(path=path, meta=meta, blocks=blocks, aliases=aliases)


def render_source(meta: Dict[str, str], blocks: Iterable[Block],
                  aliases: Optional[Dict[str, str]] = None) -> str:
    """Serialise blocks back into the ``.txt source`` format."""
    lines: List[str] = []
    for key in KNOWN_META:
        value = meta.get(key)
        if value:
            lines.append(f"{key}: {value}")
    for old_id, key in sorted((aliases or {}).items()):
        lines.append(f"IDMAP {old_id}={key}")
    lines.append("---")
    lines.append("")
    for block in blocks:
        if block.kind == "heading":
            lines.append(f"## {block.text}")
        else:
            lines.append(f"{{{block.key}}} {block.label} {block.text}")
    lines.append("")
    return "\n".join(lines)


def read_previous_aliases(path: Path) -> Dict[str, str]:
    """Best-effort read of the IDMAP of an existing source file.

    Used when re-converting a PDF over an existing source so that notes
    written against the old positional ids keep working.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    aliases: Dict[str, str] = {}
    for line in text.split("\n")[:4000]:
        stripped = line.strip()
        if stripped == "---":
            break
        m = SRC_IDMAP_RE.match(stripped)
        if m:
            aliases[m.group(1)] = m.group(2)
    return aliases


def carry_over_aliases(previous: "SourceDocument",
                       new_blocks: Iterable[Block]) -> Dict[str, str]:
    """Map the *previous* source's paragraph ids onto the new document.

    Positional ids are only meaningful relative to the file they were written
    against, so re-converting a PDF must not simply renumber them: that would
    silently move every ``@para-9`` note to whatever now happens to be ninth.
    Instead each old paragraph is looked up by its **text** in the new
    document; only paragraphs whose text is unchanged get an alias.  Anything
    that changed keeps no alias and is reported as unmatched, which is the
    honest outcome.

    Aliases already recorded in the previous file are carried forward as long
    as the paragraph they point at still exists.
    """
    by_text: Dict[str, str] = {}
    ids: set = set()
    for block in new_blocks:
        if block.kind != "para":
            continue
        ids.add(block.id)
        by_text.setdefault(textutil.normalize_for_match(block.text), block.key)

    out: Dict[str, str] = {}
    for block in previous.paragraphs:
        key = by_text.get(textutil.normalize_for_match(block.text))
        if key:
            out[block.id] = key
    for old_id, key in previous.aliases.items():
        if f"para-{key}" in ids:
            out.setdefault(old_id, key)
            continue
        # The alias points at a paragraph that is no longer present; drop it
        # rather than leaving a dangling reference behind.
    return out
