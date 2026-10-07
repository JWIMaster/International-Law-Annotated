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
    /* The annotation pane's width.  The page reserves exactly this much when
       the pane is open, so the two are defined in one place.  It narrows a
       little on smaller screens so the reading column can keep its full
       width for as long as possible. */
    --pane-w: clamp(21rem, 27vw, 25rem);
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
  .note-card { max-width: 100%; background: none; border: none; padding: 0; box-shadow: none; margin-bottom: 1.1rem; }
  .note-card .note-title { font-family: "Source Serif 4", serif; font-weight: 600; color: var(--ink); }
  .note-card .note-body { font-family: "IBM Plex Sans", sans-serif; font-size: 13.5px; line-height: 1.6; color: var(--ink-soft); margin-top: .3rem; }
  .note-card .note-source { font-family: "IBM Plex Mono", monospace; font-size: 11px; color: var(--ink-faint); margin-top: .55rem; }
  /* The author sits at the foot of the note as a signature, so the note
     itself starts at the top of the card and has somewhere to breathe. */
  .note-card .note-byline { display: flex; align-items: baseline; justify-content: flex-end; gap: .45rem;
                            margin-top: .6rem; padding-top: .4rem; border-top: 1px dotted var(--rule); }
  .note-card .note-byline::before { content: ""; }
  .note-card .note-author { font-family: "IBM Plex Mono", monospace; font-size: 10.5px; letter-spacing: .09em;
                            color: var(--ink-faint); text-transform: uppercase; }

  /* --- the annotation pane -------------------------------------------------
     Clicking a paragraph with notes opens them beside the text, Genius-style.
     The page makes room for the pane rather than the pane floating over it:
     the reading column keeps its width and slides over, so nothing is covered
     and nothing has to shrink to fit. */
  [x-cloak] { display: none !important; }
  .pane-scrim { position: fixed; inset: 0; z-index: 45; background: rgba(28,34,51,.12); }
  .note-pane {
    position: fixed; top: 0; right: 0; bottom: 0; z-index: 50;
    display: flex; flex-direction: column;
    width: var(--pane-w);
    background: var(--paper-card);
    border-left: 1px solid var(--rule);
    transform: translateX(100%); visibility: hidden;
    transition: transform .22s ease, visibility .22s ease;
  }
  .note-pane.is-open { transform: none; visibility: visible; }
  @media (max-width: 640px) { .note-pane { width: 100vw; } }   /* a phone: full screen */

  /* Room for the pane: padding on the root shrinks the box the header and
     the article are centred in, so the column slides left instead of being
     narrowed and re-centred. */
  html { overflow-anchor: none; }
  @media (min-width: 1024px) {
    html { transition: padding-right .22s ease; }
    html.pane-open { padding-right: var(--pane-w); }
    .pane-scrim { display: none !important; }        /* the page moves; it is not covered */
  }
  .jump-wrap { transition: right .22s ease; }
  @media (min-width: 1024px) {
    html.pane-open .jump-wrap { right: calc(var(--pane-w) + 1rem); }
  }
  .pane-head { display: flex; align-items: center; justify-content: space-between; gap: .5rem;
               padding: .6rem .75rem; border-bottom: 1px solid var(--rule); }
  .pane-kicker { font-family: "IBM Plex Mono", monospace; font-size: 11px; letter-spacing: .08em;
                 text-transform: uppercase; color: var(--annot); }
  .pane-tools { display: flex; align-items: center; gap: .25rem; }
  .pane-btn { display: inline-flex; align-items: center; justify-content: center;
              width: 1.85rem; height: 1.85rem; border-radius: 3px; border: 1px solid var(--rule);
              background: var(--paper); color: var(--ink-soft);
              font-family: "IBM Plex Mono", monospace; font-size: 13px; line-height: 1; }
  .pane-btn:hover:not(:disabled) { border-color: var(--annot); color: var(--annot); }
  .pane-btn:disabled { opacity: .35; cursor: default; }
  .pane-quote { margin: 0; padding: .8rem .85rem; border-bottom: 1px dashed var(--rule);
                max-height: 30vh; overflow: auto;
                font-family: "Source Serif 4", Georgia, serif; font-size: 14px; line-height: 1.55;
                color: var(--ink); }
  .pane-quote::before { content: "\201C"; color: var(--annot); }
  .pane-quote::after { content: "\201D"; color: var(--annot); }
  .pane-body { flex: 1; overflow: auto; padding: .9rem .85rem 3rem; }
  .pane-body .note-card { border-left: 2px solid var(--rule); padding-left: .7rem; margin-bottom: 1.1rem; }
  .pane-body .note-card:last-child { margin-bottom: 0; }
  .pane-body a { text-decoration: underline; color: var(--annot); overflow-wrap: anywhere; }
  .pane-body .note-count { font-family: "IBM Plex Mono", monospace; font-size: 11px;
                           color: var(--ink-faint); }
  @media (prefers-reduced-motion: reduce) {
    .note-pane, html, .jump-wrap { transition: none; }
  }

  /* The paragraph being annotated. */
  [id^="para-"].is-selected { background: rgba(163,52,31,.13); box-shadow: inset 3px 0 0 var(--annot); }

  .hl { background: rgba(163, 52, 31, .09); }
  .dim { opacity: 0.35; transition: opacity 0.2s ease; }
  .sc { font-variant: small-caps; letter-spacing: .02em; }
  .legal-flow { line-height: 1.7; hyphens: auto; }

  /* --- paragraphs with notes --------------------------------------------- */
  [id^="para-"][data-has-notes="1"] { cursor: pointer; }
  [id^="para-"][data-has-notes="1"]:hover { background: rgba(163, 52, 31, .055); }

  /* --- the marker rail ----------------------------------------------------
     Sits in the left gutter beside the text column, so it never covers it;
     it is hidden once the gutter is too narrow to hold it. */
  .note-rail-wrap { position: fixed; top: var(--sticky-offset); z-index: 30;
                    left: 0; transition: left .22s ease, opacity .15s ease; }
  .note-rail { position: relative; width: 6px; border-radius: 9999px; background: var(--rule); }
  .note-dot { position: absolute; left: 50%; width: 12px; height: 9px; margin-left: -6px;
              border-radius: 9999px; padding: 0; border: 0;
              background: color-mix(in srgb, var(--seal) 70%, transparent);
              box-shadow: 0 0 0 2px var(--paper); z-index: 1; }
  .note-dot:hover, .note-dot:focus-visible { background: var(--annot); z-index: 3; }
  .note-dot.is-current { background: var(--annot); z-index: 2; }
  .note-cursor { position: absolute; left: 50%; width: 8px; height: 8px; margin-left: -4px;
                 border-radius: 9999px; background: var(--annot); z-index: 4;
                 pointer-events: none; transition: top .1s linear; }

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
    init() {
      const computeAuthors = () => {
        window.AUTHORS = Array.from(new Set(
          Object.values(window.NOTES || {}).flat().map(n => (n.author||'').trim()).filter(Boolean)
        )).sort();
        this.authors = window.AUTHORS || [];
      };
      const run = () => {
        if (!window.NOTES || Object.keys(window.NOTES).length === 0) {
          window.addEventListener('notes:ready', () => { computeAuthors(); this.applyHighlights(); if (this.paneOpen) this.renderPane(); this.handleHashOnLoad(); }, { once: true });
        } else {
          computeAuthors(); this.applyHighlights(); this.handleHashOnLoad();
        }
      };
      run();

      // One delegated listener: clicking *anywhere* on a paragraph that has
      // notes opens them.  The paragraph rows are plain generated markup, so
      // delegation is what keeps this working on every page.
      document.addEventListener('click', (ev) => {
        if (ev.target.closest('.note-pane')) return;          // clicks inside the pane
        if (ev.defaultPrevented) return;
        const row = ev.target.closest('[id^="para-"][data-has-notes="1"]');
        if (!row) return;
        ev.preventDefault();
        this.openNote(row.id);
      });
      window.addEventListener('keydown', (ev) => {
        if (ev.key === 'Escape' && this.paneOpen) { this.closePane(); }
        else if (this.paneOpen && (ev.key === 'ArrowDown' || ev.key === 'j') && ev.altKey) { this.step(1); }
        else if (this.paneOpen && (ev.key === 'ArrowUp' || ev.key === 'k') && ev.altKey) { this.step(-1); }
      });
      window.addEventListener('pane:open', (ev) => {
        if (ev.detail && ev.detail.id) this.openNote(ev.detail.id);
      });
    },
    // ---- the annotation pane ---------------------------------------------
    // The pane's contents are plain properties filled in by renderPane(),
    // not getters: Alpine binds x-html once and does not re-run an expression
    // whose value is produced by a getter, so a getter left the pane showing
    // its first, empty state.
    paneOpen: false,
    paneId: '',
    paneKicker: '',
    paneQuote: '',
    paneHtml: '',
    copied: false,
    notedIds() {
      const ids = [];
      document.querySelectorAll('[id^="para-"][data-has-notes="1"]').forEach(el => ids.push(el.id));
      return ids;
    },
    notesFor(id) {
      const all = (window.NOTES && window.NOTES[id]) ? window.NOTES[id] : [];
      if (this.selectedAuthors.size === 0) return all;
      return all.filter(n => this.selectedAuthors.has((n.author || '').trim()));
    },
    notesHtml(list) {
      if (!list.length) return '<div class="note-count">No annotations from the selected authors.</div>';
      return list.map(n => {
        let h = '<div class="note-card">';
        if (n.title) h += '<div class="note-title">' + this.esc(n.title) + '</div>';
        h += '<div class="note-body" data-note-body>' + this.linkify(n.text || '') + '</div>';
        if (n.source) h += '<div class="note-source">Source: ' + this.esc(n.source) + '</div>';
        // The author goes last, as a signature.  At the top it pushed the
        // note itself down and left every card looking cramped.
        if (n.author) h += '<div class="note-byline"><span class="note-author">' + this.esc(n.author) + '</span></div>';
        return h + '</div>';
      }).join('');
    },
    renderPane() {
      const id = this.paneId;
      if (!id) { this.paneKicker = this.paneQuote = this.paneHtml = ''; return; }
      const ids = this.notedIds();
      const at = ids.indexOf(id);
      const list = this.notesFor(id);
      this.paneKicker = at >= 0
        ? 'Annotation ' + (at + 1) + ' of ' + ids.length
        : (list.length === 1 ? '1 annotation' : list.length + ' annotations');
      const el = document.getElementById(id);
      const p = el ? el.querySelector('p') : null;
      this.paneQuote = p ? p.innerHTML : '';
      this.paneHtml = this.notesHtml(list);
    },
    setPaneOpen(open) {
      this.paneOpen = open;
      document.documentElement.classList.toggle('pane-open', open);
      // Making room for the pane moves every paragraph, so the rail has to
      // measure again once the layout has settled.
      setTimeout(() => {
        try { window.dispatchEvent(new Event('noteMap:update')); } catch (e) {}
      }, 260);
    },
    // Opening the pane moves the text.  Keeping the clicked paragraph exactly
    // where it was on screen is what stops the page appearing to jump: the
    // reader's eye stays on the sentence they clicked.
    keepInPlace(el, wasAt) {
      if (!el || wasAt == null) return;
      let cancelled = false;
      const stop = () => { cancelled = true; };
      window.addEventListener('wheel', stop, { once: true, passive: true });
      window.addEventListener('touchmove', stop, { once: true, passive: true });
      const settle = () => {
        if (cancelled) return;
        const now = el.getBoundingClientRect().top;
        const drift = now - wasAt;
        if (Math.abs(drift) > 0.5) window.scrollBy(0, drift);
      };
      requestAnimationFrame(settle);
      setTimeout(settle, 260);          // once the transition has finished
    },
    openNote(id) {
      const el = document.getElementById(id);
      const wasAt = el ? el.getBoundingClientRect().top : null;
      this.paneId = id;
      this.setPaneOpen(true);
      this.renderPane();
      this.markSelected();
      this.keepInPlace(el, wasAt);
      this.$nextTick(() => { const b = this.$refs.paneBody; if (b) b.scrollTop = 0; });
    },
    closePane() { this.setPaneOpen(false); this.markSelected(); },
    markSelected() {
      document.querySelectorAll('.is-selected').forEach(el => el.classList.remove('is-selected'));
      if (this.paneOpen && this.paneId) {
        const el = document.getElementById(this.paneId);
        if (el) el.classList.add('is-selected');
      }
    },
    step(delta) {
      const ids = this.notedIds();
      if (!ids.length) return;
      let i = ids.indexOf(this.paneId);
      if (i < 0) i = delta > 0 ? -1 : 0;
      const id = ids[(i + delta + ids.length) % ids.length];
      this.openNote(id);
      scrollToIdWithOffset(id);
    },
    copyPaneLink() {
      if (!this.paneId) return;
      const url = new URL(window.location); url.hash = this.paneId;
      navigator.clipboard.writeText(url.toString());
      this.copied = true;
      setTimeout(() => { this.copied = false; }, 1200);
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
    toggleAuthor(a) { a=(a||'').trim(); if (this.selectedAuthors.has(a)) this.selectedAuthors.delete(a); else this.selectedAuthors.add(a); this.applyHighlights(); if (this.paneOpen) this.renderPane(); },
    isActive(a) { return this.selectedAuthors.has((a||'').trim()); },
    handleHashOnLoad() {
      if (!location.hash) return;
      const id = location.hash.slice(1);
      const el = document.getElementById(id);
      if (!el) return;
      el.scrollIntoView({behavior:'smooth', block:'start'});
      el.classList.add('bg-yellow-50');
      setTimeout(()=>el.classList.remove('bg-yellow-50'),1200);
      // A deep link to an annotated paragraph should open its annotations,
      // not just scroll to them.
      const notes = (window.NOTES && window.NOTES[id]) || [];
      if (notes.length) this.openNote(id);
    }
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
    railVisible: true,
    init: function () {
      var self = this;
      this.compute();
      this.place();
      this.onScroll();

      window.addEventListener('resize', function () { self.compute(); self.place(); self.onScroll(); });
      window.addEventListener('scroll', function () { self.onScroll(); }, { passive: true });
      window.addEventListener('noteMap:update', function () { self.compute(); self.place(); self.onScroll(); });
      window.addEventListener('notes:ready', function () { self.compute(); self.place(); self.onScroll(); });
      // Web fonts change the text height, which moves every paragraph.
      if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { self.compute(); self.place(); self.onScroll(); });
      window.addEventListener('load', function () { self.compute(); self.place(); self.onScroll(); });
    },
    // Everything here is measured in one coordinate space: absolute document
    // pixels.  The old version divided a paragraph's offsetTop (relative to
    // its offsetParent) by main.scrollHeight (a different box altogether), so
    // on a long document the markers drifted away from their paragraphs.
    compute: function () {
      var rail = this.rail();
      var noted = getNotedElements();
      if (!this.railVisible && rail) { this.markers = []; return; }
      var docHeight = Math.max(1, document.documentElement.scrollHeight);
      var arr = [];

      for (var i = 0; i < noted.length; i++) {
        var el = noted[i];
        var rect = el.getBoundingClientRect();
        var centre = rect.top + window.scrollY + Math.min(rect.height, 200) / 2;
        var count = (window.NOTES && window.NOTES[el.id]) ? window.NOTES[el.id].length : 1;

        arr.push({
          id: el.id,
          y: centre,
          pos: Math.min(100, Math.max(0, (centre / docHeight) * 100)),
          count: count,
          opacity: Math.min(1, 0.5 + count * 0.16),
          label: this.labelFor(el),
          tooltip: this.labelFor(el) + ' — ' + count + (count > 1 ? ' notes' : ' note')
        });
      }

      this.markers = this.spread(arr, rail);
    },
    rail: function () {
      return document.querySelector('.note-rail');
    },
    wrap: function () {
      return document.querySelector('[data-note-rail]');
    },
    // Put the rail in the gutter to the left of the article, and hide it when
    // the article has moved so far over that the gutter is gone.  Reading the
    // article's own box is what makes this survive the pane opening.
    place: function () {
      var wrap = this.wrap();
      var main = document.querySelector('main');
      if (!wrap || !main) return;
      var box = main.getBoundingClientRect();
      var gap = 18;
      var left = box.left - wrap.offsetWidth - gap;
      var room = left >= 6;
      wrap.style.left = Math.max(6, left) + 'px';
      wrap.style.opacity = room ? '1' : '0';
      wrap.style.pointerEvents = room ? 'auto' : 'none';
      this.railVisible = room;
    },
    // A dot is 12x9 on a 6px rail.  Without this, neighbouring notes pile up
    // and the ones underneath cannot be seen or clicked.
    spread: function (arr, rail) {
      if (!arr.length) return arr;
      var height = rail ? (rail.offsetHeight || 0) : 0;
      var i;
      if (height <= 0) {
        // The rail is hidden at this width (narrow viewport).  Every marker
        // still needs a position: leaving `top` undefined writes
        // "top:undefinedpx" into the style attribute.
        for (i = 0; i < arr.length; i++) arr[i].top = arr[i].pos;
        return arr;
      }
      var pad = 6;
      var limit = Math.max(pad, height - pad);
      var usable = Math.max(1, limit - pad);
      arr.sort(function (a, b) { return a.y - b.y; });
      var gap = arr.length > 1
        ? Math.min(10, Math.max(2.5, (usable - 4) / (arr.length - 1)))
        : 0;

      for (i = 0; i < arr.length; i++) {
        arr[i].top = pad + (arr[i].pos / 100) * usable;
      }
      // Push down whatever collides with its predecessor...
      for (i = 1; i < arr.length; i++) {
        if (arr[i].top < arr[i - 1].top + gap) arr[i].top = arr[i - 1].top + gap;
      }
      // ...then pull back whatever that pushed off the end.
      arr[arr.length - 1].top = Math.min(arr[arr.length - 1].top, limit);
      for (i = arr.length - 2; i >= 0; i--) {
        if (arr[i].top > arr[i + 1].top - gap) arr[i].top = arr[i + 1].top - gap;
      }
      for (i = 0; i < arr.length; i++) arr[i].top = Math.max(pad, arr[i].top);
      return arr;
    },
    labelFor: function (el) {
      var label = (el.getAttribute('data-label') || '').trim();
      var p = el.querySelector('p');
      var text = p ? (p.textContent || '').replace(/\s+/g, ' ').trim() : '';
      if (text.length > 52) text = text.slice(0, 52).replace(/\s\S*$/, '') + '…';
      var head = label && label !== '¶' ? label : '';
      return (head ? head + ' · ' : '') + text;
    },
    onScroll: function () {
      var rail = this.rail();
      if (!rail) return;
      var railTop = rail.getBoundingClientRect().top + window.scrollY;
      var height = rail.offsetHeight || 0;
      var y = window.scrollY + window.innerHeight * 0.32;
      this.activePos = Math.min(height - 6, Math.max(6, y - railTop));
    },
    scrollTo: function (id) {
      scrollToIdWithOffset(id);
      // The pane lives in the page-level Alpine component; announce the click
      // rather than reaching into another component's internals.
      try { window.dispatchEvent(new CustomEvent('pane:open', { detail: { id: id } })); } catch (e) {}
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
  <div class="shell mx-auto max-w-5xl px-4 py-3 flex items-center justify-between gap-4">
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
<main class="shell mx-auto max-w-5xl px-4 py-8 prose">

  <div x-data="noteMap()" x-init="init()"
       data-note-rail
       class="note-rail-wrap"
       aria-hidden="true">
    <div class="note-rail" style="height: calc(100vh - var(--sticky-offset) - 24px);">
      <template x-for="m in markers" :key="m.id">
        <button type="button"
          class="note-dot focus-ring"
          :class="paneId === m.id && paneOpen ? 'is-current' : ''"
          :style="`top:${m.top}px; opacity:${m.opacity};`"
          @click="scrollTo(m.id)"
          :title="m.tooltip"
          :aria-label="m.tooltip">
        </button>
      </template>
      <div class="note-cursor" :style="`top:${activePos}px`"></div>
    </div>
  </div>

<section class="mb-8 border-b pb-5" style="border-color: var(--rule);">
  <div class="font-mono text-[11px] uppercase tracking-[0.18em] mb-2" style="color: var(--annot);">§&nbsp; International Law Annotated</div>
  <h1 class="font-display font-semibold text-balance tracking-tight leading-[1.15] text-[clamp(1.6rem,3.5vw,2.6rem)] md:text-[clamp(1.9rem,2.6vw,2.9rem)]" id="page-title" style="color: var(--ink);">__TITLE__</h1>
</section>
<div class="flex items-center gap-2.5 font-mono text-[13px] px-4 py-3 mb-6" style="color: var(--ink-soft); background: var(--paper-card); border: 1px solid var(--rule); border-left: 3px solid var(--annot);">
  <span style="color: var(--annot);">¶</span>
  <span>Click a highlighted paragraph to read its annotations.</span>
</div>

__BODY__
__CLOSING__
</main>

<!-- Annotations open here, on the right, Genius-style. Click a paragraph to
     open the pane, another to switch, or click away / press Esc to dismiss. -->
<div class="pane-scrim" x-cloak x-show="paneOpen" x-transition.opacity.duration.150ms @click="closePane()"></div>
<aside class="note-pane" :class="paneOpen ? 'is-open' : ''" x-cloak
       role="complementary" aria-label="Annotations">
  <div class="pane-head">
    <div class="pane-kicker" x-text="paneKicker"></div>
    <div class="pane-tools">
      <button type="button" class="pane-btn focus-ring" @click="step(-1)"
              title="Previous annotated paragraph (Alt+↑)" aria-label="Previous annotated paragraph">↑</button>
      <button type="button" class="pane-btn focus-ring" @click="step(1)"
              title="Next annotated paragraph (Alt+↓)" aria-label="Next annotated paragraph">↓</button>
      <button type="button" class="pane-btn focus-ring" @click="copyPaneLink()"
              :title="copied ? 'Link copied' : 'Copy a link to this paragraph'"
              aria-label="Copy a link to this paragraph"><span x-text="copied ? '✓' : '⧉'"></span></button>
      <button type="button" class="pane-btn focus-ring" @click="closePane()"
              title="Close (Esc)" aria-label="Close annotations">✕</button>
    </div>
  </div>
  <blockquote class="pane-quote" x-html="paneQuote"></blockquote>
  <div class="pane-body" x-ref="paneBody" x-html="paneHtml"></div>
</aside>

<div class="jump-wrap fixed right-4 bottom-4 z-40" x-cloak x-data="jumpFirstNote({ headerSelector: '#site-header' })" x-init="init()">
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

</body>
</html>
'''
