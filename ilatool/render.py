"""Generating the page: paragraph markup, ``notes.js`` and the full HTML.

The visual design lives in :mod:`ilatool.page_template`; this module is only
responsible for getting data into it safely.  Two long-standing bugs are
fixed here:

* paragraph text used to be interpolated into the markup **unescaped**, so a
  document containing ``<`` or ``&`` produced broken HTML.  Everything now
  goes through :func:`esc`.
* the notes file was written without a stable ordering, so regenerating a
  page could reshuffle the note order.  Notes now keep the order in which
  they were written.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence

from .errors import RenderError
from .page_template import PAGE_TEMPLATE
from .structure import Block

PARA_TEMPLATE = '''<div id="{pid}">
  <div class="grid grid-cols-[6ch_1fr] items-start gap-3">
    <button @click.prevent="copyLink('{pid}')" class="para-id text-center shrink-0 w-full mt-1 px-2 py-0.5 focus-ring transition-colors" style="border: 1px solid var(--rule); color: var(--ink-soft); background: var(--paper-card);" onmouseover="this.style.borderColor='var(--annot)';this.style.color='var(--annot)'" onmouseout="this.style.borderColor='var(--rule)';this.style.color='var(--ink-soft)'" data-para="{pid}" title="Copy link; hover/click for notes">{label}</button>
    <p>{text}</p>
  </div>
</div>
'''

HEADER_TEMPLATE = ('<h3 class="mt-6 font-display font-semibold" data-level="{level}" '
                   'style="color: var(--ink);"><strong>{text}</strong></h3>\n')

_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def esc(text: Any) -> str:
    """Escape text for HTML *text* content."""
    return html.escape(str(text if text is not None else ""), quote=False)


def esc_attr(text: Any) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def safe_id(value: str) -> str:
    return _ID_SAFE_RE.sub("-", value or "")


# ---------------------------------------------------------------------------
# Body
# ---------------------------------------------------------------------------

def render_body(blocks: Sequence[Block]) -> str:
    out: List[str] = []
    for block in blocks:
        if block.kind == "heading":
            out.append(HEADER_TEMPLATE.format(level=block.level or 2,
                                              text=esc(block.text)))
        else:
            out.append(PARA_TEMPLATE.format(
                pid=esc_attr(block.id),
                label=esc(block.label or "¶"),
                text=esc(block.text),
            ))
    return "".join(out)


# ---------------------------------------------------------------------------
# notes.js
# ---------------------------------------------------------------------------

def render_notes_js(notes: Dict[str, List[Dict[str, str]]]) -> str:
    """Serialise the notes exactly the way the page expects them.

    Empty fields are dropped rather than written as ``""``, so the generated
    file stays small and a rebuild diff shows only real changes.
    """
    ordered: Dict[str, List[Dict[str, str]]] = {}
    for para_id, entries in notes.items():
        ordered[para_id] = [
            {k: v for k, v in note.items() if v not in ("", None)}
            for note in entries
        ]
    try:
        body = json.dumps(ordered, ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as exc:
        raise RenderError("the notes could not be serialised", cause=exc) from exc
    return f"window.NOTES = {body};\nwindow.dispatchEvent(new Event('notes:ready'));\n"


def notes_hash(notes_js_text: str) -> str:
    return hashlib.sha1(notes_js_text.encode("utf-8")).hexdigest()[:10]


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@dataclass
class RenderedPage:
    html: str
    notes_js: str
    notes_filename: str


def build_page(meta: Dict[str, str], body_html: str, notes_js_filename: str,
               closing_html: str = "") -> str:
    title = meta.get("TITLE") or "Annotated Text"
    desc = meta.get("DESC", "")
    header_title = meta.get("HEADER") or title
    home = meta.get("HOME") or "../index.html"
    if not closing_html:
        closing = meta.get("CLOSING", "")
        closing_html = f'<p class="mt-6">{esc(closing)}</p>' if closing else ""

    page = PAGE_TEMPLATE
    page = page.replace("__TITLE__", esc(title))
    page = page.replace("__DESC__", esc_attr(desc))
    page = page.replace("__HEADER_TITLE__", esc(header_title))
    page = page.replace("__HOME__", esc_attr(home))
    page = page.replace("__NOTES_JS__", esc_attr(notes_js_filename))
    page = page.replace("__BODY__", body_html)
    page = page.replace("__CLOSING__", closing_html)
    return page


def render_page(meta: Dict[str, str], blocks: Sequence[Block],
                notes: Dict[str, List[Dict[str, str]]],
                notes_filename: str) -> RenderedPage:
    body = render_body(blocks)
    notes_js = render_notes_js(notes)
    versioned = f"{notes_filename}?v={notes_hash(notes_js)}"
    page = build_page(meta, body, versioned)
    return RenderedPage(html=page, notes_js=notes_js, notes_filename=notes_filename)
