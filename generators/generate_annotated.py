#!/usr/bin/env python3
"""
generate_annotated.py

Turn a plain-text legal instrument + a JSON set of annotations into an
"International Law Annotated"-style HTML page: hover/tap popovers on each
paragraph, an author filter, a note-density rail down the side, and a
"jump to first annotation" button. Matches the structure of the hand-built
Kyoto Protocol page.

------------------------------------------------------------------------
SOURCE TEXT FORMAT  (plain .txt file)
------------------------------------------------------------------------
Optional front matter, then a line containing only "---", then the body.

    TITLE: Kyoto Protocol to the United Nations Framework Convention on Climate Change
    DESC: Annotated Kyoto Protocol text with inline author-filtered notes.
    HEADER: Kyoto Protocol
    HOME: ../index.html
    CLOSING: Done at Kyoto this tenth day of December one thousand nine hundred and ninety-seven.
    ---
    ¶ The Parties to this Protocol,
    ¶ Being Parties to the United Nations Framework Convention on Climate Change...

    ## Article 1
    ¶ For the purposes of this Protocol, the definitions contained in Article 1 shall apply.
    1. "Conference of the Parties" means the Conference of the Parties to the Convention.
    2. "Convention" means the United Nations Framework Convention on Climate Change...

    ## Article 2
    1. Each Party included in Annex I shall:
    (a) Implement and/or further elaborate policies and measures...
    (i) Enhancement of energy efficiency in relevant sectors...

Body rules:
  * Blank lines are ignored (no need to hard-wrap paragraphs; one paragraph
    per line, however long).
  * A line starting with "## " becomes an <h3>Article heading.
  * Each remaining line becomes one annotatable paragraph. If it starts with
    one of these markers it is stripped off and shown as the paragraph's
    clickable label; otherwise the label defaults to "¶":
        ¶            bare pilcrow
        1.  2.  3.   numbered
        (a) (b)      lettered
        (i) (ii)     lower-case roman
  * Inline HTML (<em>, <strong>, <br>, etc.) inside a line is passed through
    untouched, so you can keep emphasis exactly as in the source instrument.
  * To give a paragraph a stable id (so annotations survive re-ordering /
    re-running the script), prefix the line with {key}:
        {def-cop} 1. "Conference of the Parties" means ...
    Its id becomes "para-def-cop". Paragraphs without an explicit key are
    numbered sequentially: "para-1", "para-2", ...

------------------------------------------------------------------------
ANNOTATIONS FORMAT  (JSON file)
------------------------------------------------------------------------
A list of note objects. "para" must match a paragraph id from the source
(with or without the "para-" prefix -- both "def-cop" and "para-def-cop"
work, as does the plain sequential number "9"):

    [
      {
        "para": "def-cop",
        "author": "J. Smith",
        "title": "Scope of \"Conference of the Parties\"",
        "text": "This definition folds the COP into the Protocol's own
                  institutional machinery rather than creating a new body.",
        "source": "Smith, Climate Law (2020) 45"
      },
      {
        "para": "16",
        "author": "A. Nguyen",
        "text": "Note the 'shall' -- this is treated as a binding, not
                  hortatory, obligation on Annex I parties."
      }
    ]

Only "para" and "text" are required; "author", "title" and "source" are
optional and simply omitted from the rendered note if left out.

------------------------------------------------------------------------
USAGE
------------------------------------------------------------------------
    python generate_annotated.py \
        --text kyoto_source.txt \
        --annotations kyoto_notes.json \
        --out-html texts/kyoto.html \
        --out-notes texts/kyoto-notes.js
"""

import argparse
import html
import json
import re
import sys
from pathlib import Path

MARKER_RE = re.compile(r'^(¶|\d+\.|\([a-z]+\)|\([ivxlcdm]+\))\s+(.*)$', re.IGNORECASE)
KEY_RE = re.compile(r'^\{([\w-]+)\}\s*(.*)$')
HEADER_RE = re.compile(r'^##\s+(.*)$')
META_RE = re.compile(r'^([A-Z_]+):\s*(.*)$')


# ---------------------------------------------------------------- parsing --

def parse_source(path: Path):
    lines = path.read_text(encoding='utf-8').splitlines()

    meta = {}
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == '---':
            i += 1
            break
        m = META_RE.match(stripped)
        if m:
            meta[m.group(1)] = m.group(2).strip()
        i += 1
    body_lines = lines[i:]

    paragraphs = []
    auto_n = 0
    seen_ids = set()

    for raw in body_lines:
        line = raw.strip()
        if not line:
            continue

        hm = HEADER_RE.match(line)
        if hm:
            paragraphs.append({'type': 'header', 'text': hm.group(1).strip()})
            continue

        explicit_key = None
        km = KEY_RE.match(line)
        if km:
            explicit_key = km.group(1)
            line = km.group(2).strip()
            if not line:
                continue

        mm = MARKER_RE.match(line)
        if mm:
            label, content = mm.group(1), mm.group(2)
        else:
            label, content = '¶', line

        auto_n += 1
        pid_core = explicit_key if explicit_key else str(auto_n)
        para_id = f'para-{pid_core}'
        if para_id in seen_ids:
            sys.exit(f"error: duplicate paragraph id '{para_id}' "
                     f"(explicit keys must be unique)")
        seen_ids.add(para_id)

        paragraphs.append({
            'type': 'para',
            'id': para_id,
            'label': label,
            'text': content,
        })

    if not any(p['type'] == 'para' for p in paragraphs):
        sys.exit("error: no paragraphs found -- check the '---' front-matter delimiter")

    return meta, paragraphs


def normalize_para_ref(ref: str) -> str:
    ref = str(ref).strip()
    return ref if ref.startswith('para-') else f'para-{ref}'


def load_annotations(path: Path, valid_ids):
    raw = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(raw, list):
        sys.exit("error: annotations JSON must be a list of note objects")

    notes = {}
    warnings = []
    for item in raw:
        ref = item.get('para') or item.get('id')
        if not ref:
            sys.exit(f"error: annotation missing 'para' field: {item}")
        para_id = normalize_para_ref(ref)
        if para_id not in valid_ids:
            warnings.append(para_id)
        entry = {k: item[k] for k in ('author', 'title', 'text', 'source') if item.get(k)}
        if not entry.get('text'):
            sys.exit(f"error: annotation for '{para_id}' has no 'text'")
        notes.setdefault(para_id, []).append(entry)

    if warnings:
        uniq = sorted(set(warnings))
        print(f"warning: {len(uniq)} annotation(s) reference paragraph ids "
              f"not found in the source text: {', '.join(uniq)}", file=sys.stderr)

    return notes


def esc(s: str) -> str:
    return html.escape(s, quote=False)


# --------------------------------------------------------------- rendering --

PARA_TEMPLATE = '''<div id="{pid}">
  <div class="grid grid-cols-[6ch_1fr] items-start gap-3">
    <button @click.prevent="copyLink('{pid}')" class="para-id text-center shrink-0 w-full mt-1 px-2 py-0.5 focus-ring transition-colors" style="border: 1px solid var(--rule); color: var(--ink-soft); background: var(--paper-card);" onmouseover="this.style.borderColor='var(--annot)';this.style.color='var(--annot)'" onmouseout="this.style.borderColor='var(--rule)';this.style.color='var(--ink-soft)'" data-para="{pid}" title="Copy link; hover/click for notes">{label}</button>
    <p>{text}</p>
  </div>
</div>
'''

HEADER_TEMPLATE = '<h3 class="mt-6 font-display font-semibold" style="color: var(--ink);"><strong>{text}</strong></h3>\n'


def render_body(paragraphs):
    out = []
    for p in paragraphs:
        if p['type'] == 'header':
            out.append(HEADER_TEMPLATE.format(text=esc(p['text'])))
        else:
            # paragraph text is passed through as-is so inline <em>/<strong>/<br>
            # tags from the source survive; only the label is escaped.
            out.append(PARA_TEMPLATE.format(pid=p['id'], label=esc(p['label']), text=p['text']))
    return ''.join(out)


def render_notes_js(notes: dict) -> str:
    body = json.dumps(notes, ensure_ascii=False, indent=2)
    return (
        f"window.NOTES = {body};\n"
        f"window.dispatchEvent(new Event('notes:ready'));\n"
    )


PAGE_TEMPLATE = r'''<!DOCTYPE html>
<html class="h-full bg-white" lang="en" x-data="icjApp()">
<head>
<meta charset="utf-8"/>
<meta content="width=device-width,initial-scale=1" name="viewport"/>
<title>__TITLE__</title>
<meta content="__DESC__" name="description"/>
<script src="https://cdn.tailwindcss.com"></script>
<script defer src="https://unpkg.com/alpinejs@3.x.x/dist/cdn.min.js"></script>
<script src="https://unpkg.com/@popperjs/core@2"></script>
<script src="https://unpkg.com/tippy.js@6"></script>
<link href="https://unpkg.com/tippy.js@6/animations/scale.css" rel="stylesheet"/>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,500;8..60,600;8..60,700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&family=Caveat:wght@600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --paper: #F7F5EF;
    --paper-card: #FDFCF9;
    --ink: #1C2233;
    --ink-soft: #565C6E;
    --ink-faint: #8B8F9C;
    --rule: #DDD6C4;
    --annot: #A3341F;
    --annot-soft: #C6684F;
    --seal: #8A6D1C;
  }

  body { background: var(--paper); color: var(--ink); }

  .font-display { font-family: "Source Serif 4", Georgia, serif; }
  .font-ui { font-family: "IBM Plex Sans", system-ui, sans-serif; }
  .font-mono { font-family: "IBM Plex Mono", ui-monospace, monospace; }
  .font-hand { font-family: "Caveat", cursive; }

  .prose { --tw-prose-body: var(--ink-soft); --tw-prose-headings: var(--ink); --tw-prose-bold: var(--ink); max-width: none; }
  .prose p { line-height: 1.75; }

  .focus-ring { outline: none; }
  .focus-ring:focus-visible { box-shadow: 0 0 0 3px rgba(163,52,31,.35); border-radius: 2px; }

  .seal-mark {
    display: inline-flex; align-items: center; justify-content: center;
    width: 1.9rem; height: 1.9rem; border-radius: 9999px; border: 1.5px solid var(--ink);
    flex-shrink: 0;
  }

  .para-id { font-variant-numeric: tabular-nums; font-family: "IBM Plex Mono", ui-monospace, monospace; }
  .note-card { max-width: 100%; background: none; border: none; padding: 0; box-shadow: none; margin-bottom: 0.5rem; }
  .note-card .note-author { font-family: "IBM Plex Mono", monospace; font-size: 11px; letter-spacing: .04em; color: var(--ink-faint); text-transform: uppercase; }
  .note-card .note-title { font-family: "Source Serif 4", serif; font-weight: 600; color: var(--ink); margin-top: .1rem; }
  .note-card .note-body { font-family: "IBM Plex Sans", sans-serif; font-size: 13.5px; line-height: 1.5; color: var(--ink-soft); margin-top: .25rem; }
  .note-card .note-source { font-family: "IBM Plex Mono", monospace; font-size: 11px; color: var(--ink-faint); margin-top: .5rem; }

  .tippy-box[data-theme~='light-border'] { background-color: var(--paper-card); border: 1px solid var(--rule); box-shadow: 0 8px 24px rgba(28,34,51,.10); border-radius: 4px; max-width: min(92vw, 420px); border-top: 2px solid var(--annot); }
  .tippy-box[data-theme~='light-border'] .tippy-content { max-height: 68vh; overflow: auto; }
  @media (max-width: 380px) {
    .tippy-box[data-theme~='light-border'] { max-width: 94vw; }
    .tippy-box[data-theme~='light-border'] .tippy-content { max-height: 64vh; }
  }
  .tippy-box[data-theme~='light-border'] > .tippy-arrow { display: none !important; }
  .tippy-box[data-theme~='light-border'] .tippy-content { overflow-wrap: anywhere; word-break: break-word; white-space: normal; }
  .tippy-box[data-theme~='light-border'] a { text-decoration: underline; word-break: break-all; color: var(--annot); }

  .hl { background: rgba(163, 52, 31, .09); }
  .dim { opacity: 0.35; transition: opacity 0.2s ease; }
  .sc { font-variant: small-caps; letter-spacing: .02em; }
  .legal-flow { line-height: 1.7; hyphens: auto; }
  .note-rail { width:6px; }
  .note-dot { width:12px; height:8px; }

  .btn-primary { background: var(--ink); color: var(--paper); font-family: "IBM Plex Sans", sans-serif; }
  .btn-primary:hover { background: #2A3145; }

  .author-chip { font-family: "IBM Plex Mono", monospace; border: 1px solid var(--rule); color: var(--ink-soft); background: var(--paper-card); transition: border-color .15s ease, color .15s ease, background .15s ease; }
  .author-chip:hover { border-color: var(--annot); color: var(--annot); }
  .author-chip.is-active { background: var(--ink); color: var(--paper); border-color: var(--ink); }
  .author-chip.is-active:hover { background: #2A3145; color: var(--paper); }
</style>
<script defer src="__NOTES_JS__"></script>
<script defer>
function icjApp() {
  return {
    selectedAuthors: new Set(),
    authors: [],
    linkify(text = '') {
      const esc = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      const url = /(https?:\/\/[^\s)]+)(?=[)\s]|$)/gi;
      return esc.replace(url, (m) => `<a href="${m}" target="_blank" rel="noopener noreferrer">${m}</a>`);
    },
    htmlFor(id) {
      const list = (window.NOTES && window.NOTES[id]) ? window.NOTES[id] : [];
      const hasFilter = this.selectedAuthors.size > 0;
      const filtered = hasFilter ? list.filter(n => this.selectedAuthors.has((n.author || '').trim())) : list;
      if (!filtered.length) return '<div class="text-sm text-neutral-500">No notes yet.</div>';
      return filtered.map(n => {
        let h = '';
        h += '<div class="note-card mb-2">';
        if (n.author) h += '<div class="text-[11px] tracking-wide text-neutral-600 mb-1">By ' + n.author + '</div>';
        if (n.title)  h += '<div class="font-semibold mt-0.5">' + n.title + '</div>';
        h += '<div class="text-sm leading-snug mt-1 text-neutral-700" data-note-body>' + this.linkify(n.text || '') + '</div>';
        if (n.source) h += '<div class="text-xs mt-2 text-neutral-500">Source: ' + n.source + '</div>';
        h += '</div>';
        return h;
      }).join('');
    },
    _tippies: [],
    init() {
      const computeAuthors = () => {
        window.AUTHORS = Array.from(new Set(
          Object.values(window.NOTES || {}).flat().map(n => (n.author||'').trim()).filter(Boolean)
        )).sort();
        this.authors = window.AUTHORS || [];
      };
      const run = () => {
        if (!window.NOTES || Object.keys(window.NOTES).length === 0) {
          window.addEventListener('notes:ready', () => { computeAuthors(); this.refreshPopovers(); this.applyHighlights(); this.handleHashOnLoad(); }, { once: true });
        } else {
          computeAuthors(); this.refreshPopovers(); this.applyHighlights(); this.handleHashOnLoad();
        }
      };
      run();
    },
    refreshPopovers() {
      this._tippies.forEach(t => t.destroy());
      this._tippies = [];
      const isMobile = window.matchMedia('(max-width: 640px)').matches;
      document.querySelectorAll('[data-para]').forEach(el => {
        const id = el.getAttribute('data-para');
        const instance = tippy(el, {
  allowHTML: true,
  interactive: true,
  theme: 'light-border',
  animation: 'scale',
  appendTo: () => document.body,
  placement: isMobile ? 'bottom' : 'right-start',
  trigger: isMobile ? 'click' : 'mouseenter focus',
  offset: isMobile ? [0, 8] : [6, 0],
  hideOnClick: true,
  content: () => this.htmlFor(id),
  popperOptions: {
    modifiers: [
      { name: 'preventOverflow', options: { padding: 8, altAxis: true } },
      { name: 'flip', options: { fallbackPlacements: ['bottom', 'top', 'right', 'left'] } }
    ]
  }
});
        this._tippies.push(instance);
      });
    },
    applyHighlights() {
      const allParas = Array.from(document.querySelectorAll('[id^="para-"],[data-dimmable="1"]'));
      allParas.forEach(w => w.classList.remove('hl', 'dim'));
      const allIds = allParas.map(el => el.id);
      if (this.selectedAuthors.size === 0) {
        allIds.forEach(id => {
          const notes = (window.NOTES && window.NOTES[id]) ? window.NOTES[id] : [];
          if (notes.length > 0) { const w = document.getElementById(id); if (w) w.classList.add('hl'); }
        });
        return;
      }
      allParas.forEach(w => w.classList.add('dim'));
      allIds.forEach(id => {
        const notes = (window.NOTES && window.NOTES[id]) ? window.NOTES[id] : [];
        const hasSelected = notes.some(n => this.selectedAuthors.has((n.author || '').trim()));
        if (hasSelected) { const w = document.getElementById(id); if (w) { w.classList.add('hl'); w.classList.remove('dim'); } }
      });
    },
    toggleAuthor(a) { a=(a||'').trim(); if (this.selectedAuthors.has(a)) this.selectedAuthors.delete(a); else this.selectedAuthors.add(a); this.applyHighlights(); this.refreshPopovers(); },
    isActive(a) { return this.selectedAuthors.has((a||'').trim()); },
    copyLink(id) {
      const url = new URL(window.location); url.hash = id; navigator.clipboard.writeText(url.toString());
      const el = document.querySelector(`[data-para='${id}']`);
      if (el) { el.classList.add('ring-2','ring-emerald-400'); setTimeout(()=>el.classList.remove('ring-2','ring-emerald-400'),800); }
    },
    handleHashOnLoad() { if (location.hash) { const id = location.hash.slice(1); const el = document.getElementById(id); if (el) { el.scrollIntoView({behavior:'smooth', block:'start'}); el.classList.add('bg-yellow-50'); setTimeout(()=>el.classList.remove('bg-yellow-50'),1200); } } }
  }
}

function scrollToIdWithOffset(id, behavior) {
  if (!behavior) behavior = 'smooth';
  var el = document.getElementById(id); if (!el) return;
  var header = document.querySelector('header.sticky') || document.getElementById('site-header') || document.querySelector('header[role="banner"]');
  var headerH = (header && header.offsetHeight ? header.offsetHeight : 0) + 8;
  var rect = el.getBoundingClientRect();
  var targetY = window.scrollY + rect.top - headerH;
  window.scrollTo({ top: Math.max(0, targetY), behavior: behavior });
}

function noteMap() {
  function getNotedElements() {
    if (!window.NOTES || !Object.keys(window.NOTES).length) return [];
    var out = [];
    Object.keys(window.NOTES).forEach(function(id) {
      var el = document.getElementById(id);
      var list = window.NOTES[id] || [];
      if (el && list.length) out.push(el);
    });
    return out;
  }

  return {
    markers: [],
    activePos: 0,
    init: function () {
      this.compute();
      this.onScroll();

      var self = this;
      window.addEventListener('resize', function () { self.compute(); });
      window.addEventListener('scroll', function () { self.onScroll(); }, { passive: true });
      window.addEventListener('noteMap:update', function () { self.compute(); });
      window.addEventListener('notes:ready', function () { self.compute(); self.onScroll(); });
    },
    compute: function () {
      var container = document.querySelector('main') || document.body;
      var total = container && container.scrollHeight ? container.scrollHeight : 1;
      var noted = getNotedElements();
      var arr = [];

      for (var i = 0; i < noted.length; i++) {
        var el = noted[i];
        var pos = Math.min(98, Math.max(2, (el.offsetTop / total) * 100));
        var count = (window.NOTES && window.NOTES[el.id]) ? window.NOTES[el.id].length : 1;

        arr.push({
          id: el.id,
          pos: pos,
          opacity: Math.min(1, 0.45 + count * 0.18),
          tooltip: '¶' + el.id.replace('para-', '') + ' • ' +
            count + ' note' + (count > 1 ? 's' : '')
        });
      }

      this.markers = arr;
    },
    onScroll: function () {
      var container = document.querySelector('main') || document.body;
      var total = container && container.scrollHeight ? container.scrollHeight : 1;
      var y = window.scrollY + window.innerHeight * 0.25;
      this.activePos = Math.min(98, Math.max(2, (y / total) * 100));
    },
    scrollTo: function (id) {
      scrollToIdWithOffset(id);
    }
  };
}

(function tagNotedParas(){
  function tag() {
    var paras = document.querySelectorAll('[id^="para-"]');
    for (var i=0;i<paras.length;i++){ var el=paras[i]; var list=(window.NOTES && window.NOTES[el.id])||[]; if (list && list.length) el.setAttribute('data-has-notes','1'); else el.removeAttribute('data-has-notes'); }
    try { window.dispatchEvent(new Event('noteMap:update')); } catch(e) {}
  }
  function readyNow(){ return !!(window.NOTES && Object.keys(window.NOTES).length); }
  if (!readyNow()) { window.addEventListener('notes:ready', tag, { once:true });
    var tries=0; var iv=setInterval(function(){ if (readyNow() || ++tries > 300) { clearInterval(iv); if (readyNow()) tag(); } }, 100);
  } else { tag(); }
})();
</script>
<style>:root { --sticky-offset: 80px; } html, body { scroll-padding-top: var(--sticky-offset); }</style>
</head>

<body class="min-h-full font-ui" style="background: var(--paper); color: var(--ink);" x-init="init()">
<header class="sticky top-0 z-40 backdrop-blur border-b" style="background: color-mix(in srgb, var(--paper) 92%, transparent); border-color: var(--rule);">
  <div class="mx-auto max-w-5xl px-4 py-3 flex items-center justify-between gap-4">
    <div class="flex items-center gap-3 min-w-0">
      <a href="__HOME__"
   class="seal-mark shrink-0"
   style="color: var(--ink);"
   aria-label="International Law Annotated">
  <span class="font-display" style="color: var(--annot); font-size: 1.05rem; line-height: 1;">¶</span>
</a>
      <div id="hdr-title"
           class="font-display font-medium tracking-tight text-[17px] flex-1 min-w-0 truncate">
        __HEADER_TITLE__
      </div>
    </div>

    <div id="hdr-authors" class="flex items-center gap-2 shrink-0">
      <span class="font-mono text-[11px] uppercase tracking-wide" style="color: var(--ink-faint);">Authors</span>
      <template x-for="a in authors" :key="a">
  <button
    class="author-chip px-2.5 py-1 text-xs focus-ring"
    :class="isActive(a) ? 'is-active' : ''"
    @click="toggleAuthor(a)">
    <span x-text="a"></span>
  </button>
</template>
    </div>
  </div>
</header>
<main class="mx-auto max-w-5xl px-4 py-8 prose">

  <div x-data="noteMap()" x-init="init()"
       data-note-rail
       class="hidden lg:block fixed z-30"
       style="top: var(--sticky-offset);">
    <div class="note-rail rounded-full relative" style="background: var(--rule); height: calc(100vh - var(--sticky-offset) - 24px);">
      <template x-for="m in markers" :key="m.id">
        <button
          class="note-dot rounded-full absolute left-1/2 -translate-x-1/2 transition"
          style="background: color-mix(in srgb, var(--seal) 70%, transparent); box-shadow: 0 0 0 2px var(--paper);"
          onmouseover="this.style.background='var(--annot)'" onmouseout="this.style.background='color-mix(in srgb, var(--seal) 70%, transparent)'"
          :style="`top:${m.pos}%; opacity:${m.opacity}; z-index:${10000 - (parseInt((m.id || '').replace(/[^0-9]/g, ''), 10) || 0)}`"
          @click="scrollTo(m.id)"
          :title="m.tooltip">
        </button>
      </template>
      <div class="absolute left-1/2 -translate-x-1/2 w-2 h-2 rounded-full z-[1200]"
           style="background: var(--annot);"
           :style="`top:${activePos}%`"></div>
    </div>
  </div>

<section class="mb-8 border-b pb-5" style="border-color: var(--rule);">
  <div class="font-mono text-[11px] uppercase tracking-[0.18em] mb-2" style="color: var(--annot);">§&nbsp; International Law Annotated</div>
  <h1 class="font-display font-semibold text-balance tracking-tight leading-[1.15] text-[clamp(1.6rem,3.5vw,2.6rem)] md:text-[clamp(1.9rem,2.6vw,2.9rem)]" id="page-title" style="color: var(--ink);">__TITLE__</h1>
</section>
<div class="flex items-center gap-2.5 font-mono text-[13px] px-4 py-3 mb-6" style="color: var(--ink-soft); background: var(--paper-card); border: 1px solid var(--rule); border-left: 3px solid var(--annot);">
  <span style="color: var(--annot);">¶</span>
  <span>Hover or tap paragraph markers to view annotations.</span>
</div>

__BODY__
__CLOSING__
</main>

<div class="fixed right-4 bottom-4 z-40" x-cloak x-data="jumpFirstNote({ headerSelector: '#site-header' })" x-init="init()">
  <button @click="go()" class="btn-primary px-4 py-2.5 text-sm font-medium shadow transition-colors focus-ring" x-show="show" x-transition.opacity.duration.300>
    Jump to first annotation
</button>
</div>
<script>
function jumpFirstNote(opts){
  opts = opts || {};
  function getHeaderOffset(){
    var header = document.querySelector(opts.headerSelector || 'header.sticky, #site-header, header[role="banner"]');
    var h = header && header.offsetHeight ? header.offsetHeight : 0;
    return h + 8;
  }
  return {
    show: false, firstId: null, firstTop: 0, _blockReshow: false, _raf: null,
    init: function(){
      var self = this;
      function ready(){
        var noted = document.querySelectorAll('[id^="para-"][data-has-notes="1"]');
        if (!noted.length) return;
        self.firstId = noted[0].id;
        var r = noted[0].getBoundingClientRect();
        self.firstTop = r.top + window.scrollY;
        self.updateShow();
        window.addEventListener('scroll', function(){ self.onScroll(); }, { passive: true });
        window.addEventListener('resize', function(){ self.recalc(); });
      }
      window.addEventListener('noteMap:update', ready, { once: true });
      if (document.querySelector('[id^="para-"][data-has-notes="1"]')) ready();
    },
    recalc: function(){
      if (!this.firstId) return;
      var el = document.getElementById(this.firstId); if (!el) return;
      var r = el.getBoundingClientRect();
      this.firstTop = r.top + window.scrollY; this.updateShow();
    },
    onScroll: function(){
      var self = this;
      if (this._raf) return;
      this._raf = requestAnimationFrame(function(){ self.updateShow(); self._raf = null; });
    },
    updateShow: function(){
      var aboveFirst = (window.scrollY + getHeaderOffset()) < (this.firstTop - 2);
      if (this._blockReshow && aboveFirst) { this.show = false; return; }
      this.show = aboveFirst;
    },
    go: function(){
      if (!this.firstId) return;
      this._blockReshow = true; this.show = false; scrollToIdWithOffset(this.firstId);
      var self = this;
      function check(){
        var atOrPast = (window.scrollY + getHeaderOffset()) >= (self.firstTop - 2);
        if (atOrPast) { self._blockReshow = false; window.removeEventListener('scroll', onScrollCheck); clearTimeout(timer); }
      }
      function onScrollCheck(){ requestAnimationFrame(check); }
      window.addEventListener('scroll', onScrollCheck, { passive: true });
      var timer = setTimeout(function(){ self._blockReshow = false; window.removeEventListener('scroll', onScrollCheck); }, 1600);
    }
  };
}
</script>

<script>
(function positionNoteRail() {
  var rail = null, placedOnce = false;

  function getRail() {
    if (!rail) rail = document.querySelector('[data-note-rail]');
    return rail;
  }

  function place() {
    var el = getRail();
    var main = document.querySelector('main') || document.body;
    if (!el || !main) return;
    var r = main.getBoundingClientRect();
    var gap = 20;
    var left = window.scrollX + r.left - gap - el.offsetWidth;
    var minLeft = window.scrollX + 12;
    el.style.left = Math.max(minLeft, left) + 'px';
    placedOnce = true;
  }

  function onReady(fn) {
    if (document.readyState === 'interactive' || document.readyState === 'complete') fn();
    else window.addEventListener('DOMContentLoaded', fn, { once: true });
  }

  onReady(place);
  window.addEventListener('load', place);
  window.addEventListener('resize', place);
  window.addEventListener('noteMap:update', place);

  if (document.fonts && document.fonts.ready) document.fonts.ready.then(place);

  let tries = 0, iv = setInterval(function () {
    if (placedOnce || ++tries > 20) return clearInterval(iv);
    place();
  }, 150);
})();
</script>

</body>
</html>
'''


def build_page(meta: dict, body_html: str, notes_js_filename: str) -> str:
    title = meta.get('TITLE', 'Annotated Text')
    desc = meta.get('DESC', '')
    header_title = meta.get('HEADER', title)
    home = meta.get('HOME', '../index.html')
    closing = meta.get('CLOSING', '')
    closing_html = f'<p class="mt-6">{esc(closing)}</p>' if closing else ''

    page = PAGE_TEMPLATE
    page = page.replace('__TITLE__', esc(title))
    page = page.replace('__DESC__', esc(desc))
    page = page.replace('__HEADER_TITLE__', esc(header_title))
    page = page.replace('__HOME__', home)
    page = page.replace('__NOTES_JS__', notes_js_filename)
    page = page.replace('__BODY__', body_html)
    page = page.replace('__CLOSING__', closing_html)
    return page


# -------------------------------------------------------------------- main --

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--text', required=True, type=Path, help='source text file')
    ap.add_argument('--annotations', required=True, type=Path, help='annotations JSON file')
    ap.add_argument('--out-html', required=True, type=Path, help='output HTML path')
    ap.add_argument('--out-notes', required=True, type=Path, help='output notes .js path')
    args = ap.parse_args()

    meta, paragraphs = parse_source(args.text)
    valid_ids = {p['id'] for p in paragraphs if p['type'] == 'para'}
    notes = load_annotations(args.annotations, valid_ids)

    notes_js_filename = args.out_notes.name
    body_html = render_body(paragraphs)
    page = build_page(meta, body_html, notes_js_filename)

    args.out_html.parent.mkdir(parents=True, exist_ok=True)
    args.out_notes.parent.mkdir(parents=True, exist_ok=True)
    args.out_html.write_text(page, encoding='utf-8')
    args.out_notes.write_text(render_notes_js(notes), encoding='utf-8')

    n_paras = sum(1 for p in paragraphs if p['type'] == 'para')
    n_annotated = sum(1 for pid in valid_ids if pid in notes)
    n_notes = sum(len(v) for v in notes.values())
    print(f"wrote {args.out_html}")
    print(f"wrote {args.out_notes}")
    print(f"{n_paras} paragraphs parsed, {n_annotated} annotated, {n_notes} notes total")


if __name__ == '__main__':
    main()
