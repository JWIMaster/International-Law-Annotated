"""The generated page template.

Kept as one literal so the design and the generator stay in step; the only
substitutions are the ``__NAME__`` placeholders at the bottom of the file.
Paragraph text and note fields are escaped before they get here -- the
original inline version interpolated PDF text straight into the markup,
which broke the page as soon as a document contained a ``<`` or an ``&``.
"""

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
      const safe = this.esc(text);
      const url = /(https?:\/\/[^\s)]+)(?=[)\s]|$)/gi;
      return safe.replace(url, (m) => `<a href="${m}" target="_blank" rel="noopener noreferrer">${m}</a>`);
    },
    esc(text = '') {
      return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    },
    htmlFor(id) {
      const list = (window.NOTES && window.NOTES[id]) ? window.NOTES[id] : [];
      const hasFilter = this.selectedAuthors.size > 0;
      const filtered = hasFilter ? list.filter(n => this.selectedAuthors.has((n.author || '').trim())) : list;
      if (!filtered.length) return '<div class="text-sm text-neutral-500">No notes yet.</div>';
      return filtered.map(n => {
        let h = '';
        h += '<div class="note-card mb-2">';
        if (n.author) h += '<div class="text-[11px] tracking-wide text-neutral-600 mb-1">By ' + this.esc(n.author) + '</div>';
        if (n.title)  h += '<div class="font-semibold mt-0.5">' + this.esc(n.title) + '</div>';
        h += '<div class="text-sm leading-snug mt-1 text-neutral-700" data-note-body>' + this.linkify(n.text || '') + '</div>';
        if (n.source) h += '<div class="text-xs mt-2 text-neutral-500">Source: ' + this.esc(n.source) + '</div>';
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
