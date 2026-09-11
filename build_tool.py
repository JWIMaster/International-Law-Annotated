#!/usr/bin/env python3
"""
annotate.py -- one-file, menu-driven tool for building "International Law
Annotated"-style pages: hover/tap paragraph annotations, an author filter,
a note-density rail, and a "jump to first annotation" button.

Run it with no arguments to get an interactive menu:

    python annotate.py

It walks you through three steps, remembering your progress (in a small
.annotate_state.json file in the current directory) so you can quit and
come back later, or re-run after editing a file by hand:

    1) Set the source text -- either convert a PDF, or point at an
       existing source .txt you already have (e.g. one you edited after
       a previous conversion).
    2) Set the annotations -- a plain-text file in the format described
       under option 4 in the menu, a JSON file, or an existing notes.js
       file to use as-is (skips matching entirely).
    3) Generate the final HTML + notes.js -- unlocked once both 1 and 2
       are set. Defaults to writing into a "texts" folder alongside
       index.html, one directory up from this script. If a file already
       exists at the target path, you'll be asked to cancel, overwrite,
       or rename.

    Option 5 toggles an extra step (off by default): after generating,
    add or update this text's card in texts/all.html automatically.

Nothing here talks to the network or needs installing anything beyond
Python 3 and poppler-utils' `pdftotext`/`pdfinfo` (already present in
most environments that can open PDFs at all).
"""

import argparse
import difflib
import hashlib
import html
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

STATE_FILE = Path('.annotate_state.json')

# Directory the script itself lives in -- used to anchor default output
# paths (see action_generate) regardless of the current working directory.
SCRIPT_DIR = Path(__file__).resolve().parent


# ========================================================================
#  Terminal colors
# ========================================================================
# A minimal ANSI helper -- no dependency needed for a handful of colors.
# Disabled automatically when stdout isn't a real terminal (e.g. piped to
# a file or captured by another program) or when NO_COLOR is set, per the
# https://no-color.org convention, so output stays clean in those cases.

class _Color:
    _enabled = sys.stdout.isatty() and not __import__('os').environ.get('NO_COLOR')

    RESET = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    RED = '\033[31m'
    GREEN = '\033[32m'
    YELLOW = '\033[33m'
    BLUE = '\033[34m'
    MAGENTA = '\033[35m'
    CYAN = '\033[36m'

    @classmethod
    def wrap(cls, text, *codes):
        if not cls._enabled:
            return text
        return ''.join(codes) + text + cls.RESET


def c_title(text):
    return _Color.wrap(text, _Color.BOLD, _Color.CYAN)


def c_ok(text):
    return _Color.wrap(text, _Color.GREEN)


def c_warn(text):
    return _Color.wrap(text, _Color.YELLOW)


def c_err(text):
    return _Color.wrap(text, _Color.BOLD, _Color.RED)


def c_path(text):
    return _Color.wrap(text, _Color.MAGENTA)


def c_dim(text):
    return _Color.wrap(text, _Color.DIM)


def c_menu(text):
    return _Color.wrap(text, _Color.BLUE)


# ========================================================================
#  PART 1 -- PDF -> plain-text "source" format
# ========================================================================
#
# SOURCE TEXT FORMAT  (plain .txt)
# ---------------------------------------------------------------------
# Optional front matter, then a line containing only "---", then the body.
#
#     TITLE: Kyoto Protocol to the United Nations Framework Convention on Climate Change
#     DESC: Annotated Kyoto Protocol text with inline author-filtered notes.
#     HEADER: Kyoto Protocol
#     HOME: ../index.html
#     CLOSING: Done at Kyoto this tenth day of December one thousand nine hundred and ninety-seven.
#     ---
#     ¶ The Parties to this Protocol,
#     ¶ Being Parties to the United Nations Framework Convention on Climate Change...
#
#     ## Article 1
#     ¶ For the purposes of this Protocol, the definitions contained in Article 1 shall apply.
#     1. "Conference of the Parties" means the Conference of the Parties to the Convention.
#
# Body rules:
#   * Blank lines are ignored.
#   * A line starting with "## " becomes an Article heading.
#   * Each remaining line becomes one annotatable paragraph. A leading
#     marker (¶, "1.", "(a)", "(i)") is stripped off and shown as the
#     paragraph's clickable label; otherwise the label defaults to "¶".
#   * Prefix a line with {key} to give it a stable id ("para-key") that
#     survives re-ordering / re-conversion. Without one, paragraphs are
#     auto-numbered: para-1, para-2, ...

PDF_HEADING_RE = re.compile(
    r'^(Article|Part|Chapter|Section|Annex|Title)\s+[IVXLCDM0-9]+[A-Za-z]*\b.{0,60}$',
    re.IGNORECASE,
)

PDF_MARKER_RE = re.compile(
    r'^(?:'
    r'(?P<num>\d+)[.\)]'
    r'|\((?P<pnum>\d+)\)'
    r'|\((?P<letter>[a-z]{1,3})\)'
    r'|\((?P<roman>[ivxlcdm]{1,6})\)'
    r'|(?P<pilcrow>¶)'
    r')(?:\s+(?P<rest>.*))?$',
    re.IGNORECASE,
)

PDF_PAGE_NUM_LINE_RE = re.compile(
    r'^\s*[-\u2013\u2014]?\s*\d+\s*[-\u2013\u2014]?\s*$|^\s*page\s+\d+(\s+of\s+\d+)?\s*$',
    re.IGNORECASE,
)

# Recital preambles ("The Parties to this Protocol, / Recognizing that..., /
# Have agreed as follows:") are near-universal in treaty drafting, but PDFs
# often have no blank line between clauses, so pdftotext fuses the whole
# preamble into one paragraph. These cue words almost always start a new
# recital, so they're used to re-split it.
PDF_RECITAL_CUES = [
    'Being', 'Recognizing', 'Recognising', 'Recalling', 'Further recalling',
    'Noting', 'Taking note', 'Taking into account', 'Considering', 'Desiring',
    'Desirous', 'Reaffirming', 'Bearing in mind', 'Convinced', 'Determined',
    'Concerned', 'Emphasizing', 'Emphasising', 'Underlining', 'Acknowledging',
    'Mindful', 'Guided by', 'Welcoming', 'Alarmed by', 'Aware that',
    'Affirming', 'Conscious', 'Stressing', 'Have agreed', 'In pursuit of',
    'Pursuant to',
]
PDF_RECITAL_CUE_RE = re.compile(
    r',\s+(?=(?:' + '|'.join(re.escape(c) for c in PDF_RECITAL_CUES) + r')\b)'
)


def pdf_split_recitals(text: str):
    parts = [p.strip() for p in PDF_RECITAL_CUE_RE.split(text) if p.strip()]
    if len(parts) < 2:
        return [text]
    for i in range(len(parts) - 1):
        if not parts[i].endswith((',', ';', ':', '.')):
            parts[i] += ','
    return parts


def pdf_extract_raw_text(pdf_path: Path, layout: bool = False) -> str:
    cmd = ['pdftotext']
    if layout:
        cmd.append('-layout')
    cmd += [str(pdf_path), '-']
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"pdftotext failed:\n{result.stderr}")
    if not result.stdout.strip():
        raise RuntimeError(
            "no text extracted -- this PDF may be scanned/raster-only "
            "(no text layer). OCR it first, then re-run."
        )
    return result.stdout


def pdf_strip_running_headers_footers(raw_text: str) -> str:
    """Drop lines repeating at the top/bottom of most pages (mastheads,
    page numbers), and trim blank lines right at page boundaries so a
    paragraph wrapping across a page break isn't split into two."""
    pages = raw_text.split('\x0c')
    if len(pages) < 3:
        return raw_text

    def norm(line):
        return re.sub(r'\d+', '#', line.strip())

    page_lines = [p.split('\n') for p in pages]
    top_counter, bot_counter = Counter(), Counter()
    for lines in page_lines:
        non_blank = [i for i, l in enumerate(lines) if l.strip()]
        if not non_blank:
            continue
        top_counter[norm(lines[non_blank[0]])] += 1
        bot_counter[norm(lines[non_blank[-1]])] += 1

    threshold = max(2, round(len(pages) * 0.5))
    top_boiler = {k for k, v in top_counter.items() if v >= threshold and 0 < len(k) < 90}
    bot_boiler = {k for k, v in bot_counter.items() if v >= threshold and 0 < len(k) < 90}

    out_page_lines = []
    for lines in page_lines:
        non_blank = [i for i, l in enumerate(lines) if l.strip()]
        drop = set()
        if non_blank and norm(lines[non_blank[0]]) in top_boiler:
            drop.add(non_blank[0])
        if non_blank and norm(lines[non_blank[-1]]) in bot_boiler:
            drop.add(non_blank[-1])
        for i in non_blank[:2] + non_blank[-2:]:
            if PDF_PAGE_NUM_LINE_RE.match(lines[i]):
                drop.add(i)
        kept = [l for i, l in enumerate(lines) if i not in drop]
        while kept and not kept[0].strip():
            kept.pop(0)
        while kept and not kept[-1].strip():
            kept.pop()
        out_page_lines.append(kept)

    return '\n'.join('\n'.join(lines) for lines in out_page_lines)


def pdf_dehyphenate_join(a: str, b: str) -> str:
    if re.search(r'[A-Za-z]-$', a):
        return a[:-1] + b
    return a + ' ' + b


def normalize_text(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', s.lower()).strip()


# A marker that shows up *after* some other text on the same physical line
# (e.g. "...as follows: (2) Each Party shall...") is invisible to
# PDF_MARKER_RE, which only checks the start of a line -- pdftotext often
# runs numbered sub-clauses together like this when a PDF's line breaks
# don't line up with paragraph breaks. MID_LINE_MARKER_RE finds those and
# pdf_presplit_inline_markers() breaks the line apart at each one, so every
# marker ends up at the start of its own line where the normal grouping
# logic (PDF_MARKER_RE, above) already knows how to handle it.
MID_LINE_MARKER_RE = re.compile(
    r'(?<=[.;:)\u2014\u2013,])\s+(?='
    r'(?:\d+[.\)]\s+\S)'
    r'|(?:\(\d+\)\s+\S)'
    r'|(?:\([a-z]{1,3}\)\s+\S)'
    r'|(?:\([ivxlcdm]{1,6}\)\s+\S)'
    r')',
    re.IGNORECASE,
)


def pdf_presplit_inline_markers(lines):
    """Break lines containing a mid-line "(2)"/"3."/"(iv)" marker into
    separate lines, one marker per line, WITHOUT touching a marker that's
    already alone at the start of its line."""
    out = []
    for line in lines:
        if not line:
            out.append(line)
            continue
        pieces = MID_LINE_MARKER_RE.split(line)
        out.extend(p.strip() for p in pieces if p.strip())
    return out


def pdf_group_paragraphs(raw_text: str, skip_title: str = None, split_recitals_enabled: bool = True):
    lines = [l.strip() for l in raw_text.replace('\x0c', '\n').split('\n')]
    lines = pdf_presplit_inline_markers(lines)
    skip_norm = normalize_text(skip_title) if skip_title else None

    paragraphs = []
    buf_text = None
    buf_label = None
    buf_empty_marker = False  # buffer holds only a bare marker, no text yet

    def flush():
        nonlocal buf_text, buf_label, buf_empty_marker
        if buf_text:
            paragraphs.append({'type': 'para', 'label': buf_label, 'text': buf_text.strip()})
        buf_text, buf_label, buf_empty_marker = None, None, False

    for line in lines:
        if not line:
            # A bare marker line (e.g. "(2)" alone, with the clause text
            # following on the next non-blank line -- common when a PDF
            # renderer breaks right after a numbered/lettered marker) must
            # not be flushed away by the blank line that can follow it;
            # otherwise the marker becomes an empty paragraph and the real
            # text that follows gets orphaned onto whatever comes after.
            if buf_empty_marker:
                continue
            flush()
            continue

        if skip_norm and normalize_text(line) == skip_norm:
            continue

        if PDF_HEADING_RE.match(line):
            flush()
            paragraphs.append({'type': 'header', 'text': line})
            continue

        mm = PDF_MARKER_RE.match(line)
        if mm:
            flush()
            if mm.group('num') is not None:
                buf_label = f"{mm.group('num')}."
            elif mm.group('pnum') is not None:
                buf_label = f"({mm.group('pnum')})"
            elif mm.group('letter') is not None:
                buf_label = f"({mm.group('letter')})"
            elif mm.group('roman') is not None:
                buf_label = f"({mm.group('roman')})"
            else:
                buf_label = '¶'
            buf_text = mm.group('rest') or ''
            buf_empty_marker = not buf_text.strip()
            continue

        if buf_text is None:
            buf_label = '¶'
            buf_text = line
        elif buf_empty_marker:
            # First real text arriving after a bare marker line -- this is
            # the marker's own clause, not a continuation to dehyphenate.
            buf_text = line
        else:
            buf_text = pdf_dehyphenate_join(buf_text, line)
        buf_empty_marker = False

    flush()

    if split_recitals_enabled:
        expanded = []
        in_leading_run = True
        for p in paragraphs:
            if in_leading_run and p['type'] == 'para' and p['label'] == '¶':
                for clause in pdf_split_recitals(p['text']):
                    expanded.append({'type': 'para', 'label': '¶', 'text': clause})
            else:
                in_leading_run = False
                expanded.append(p)
        paragraphs = expanded

    return paragraphs


def pdf_looks_suspicious(paragraphs):
    flags = []
    for i, p in enumerate(paragraphs):
        if p['type'] != 'para':
            continue
        t = p['text']
        if len(t) < 3:
            flags.append((i, t, 'very short'))
        elif re.search(r'[A-Za-z]{2}-[A-Za-z]{2}', t) and t.count('-') > len(t) / 20:
            flags.append((i, t[:60], 'possible leftover hyphenation'))
    return flags


def pdf_render_source(meta: dict, paragraphs) -> str:
    lines = []
    for key in ('TITLE', 'DESC', 'HEADER', 'HOME', 'CLOSING'):
        if meta.get(key):
            lines.append(f"{key}: {meta[key]}")
    lines.append('---')
    lines.append('')
    for p in paragraphs:
        if p['type'] == 'header':
            lines.append(f"## {p['text']}")
        else:
            lines.append(f"{p['label']} {p['text']}")
    lines.append('')
    return '\n'.join(lines)


def pdf_guess_title(pdf_path: Path) -> str:
    try:
        result = subprocess.run(['pdfinfo', str(pdf_path)], capture_output=True, text=True)
        m = re.search(r'^Title:\s*(.+)$', result.stdout, re.MULTILINE)
        if m and m.group(1).strip():
            return m.group(1).strip()
    except FileNotFoundError:
        pass
    return pdf_path.stem.replace('_', ' ').replace('-', ' ').title()


def convert_pdf_to_source(pdf_path: Path, meta: dict, layout: bool = False):
    """Returns (rendered_source_text, paragraphs, warnings_list)."""
    raw = pdf_extract_raw_text(pdf_path, layout=layout)
    raw = pdf_strip_running_headers_footers(raw)
    paragraphs = pdf_group_paragraphs(raw, skip_title=meta.get('TITLE'))
    if not any(p['type'] == 'para' for p in paragraphs):
        raise RuntimeError(
            "no paragraphs detected -- try layout-preserving extraction, "
            "or check the PDF actually has a text layer."
        )
    flags = pdf_looks_suspicious(paragraphs)
    return pdf_render_source(meta, paragraphs), paragraphs, flags


# ========================================================================
#  PART 2 -- annotations: plain-text format + JSON, both accepted
# ========================================================================
#
# ANNOTATION TEXT FORMAT  (suggested default -- see menu option 4)
# ---------------------------------------------------------------------
# One block per note. A block starts EITHER with:
#
#   * a line of the form "@<id>", where <id> matches a paragraph id from
#     the source text (with or without the "para-" prefix, or the bare
#     sequential number) -- for when you already know the internal id, or
#
#   * a quoted excerpt from the document, wrapped in a line of three
#     double-quotes above and below it -- for contributors who don't know
#     (and shouldn't need to know) any internal ids. Paste the full
#     sentence or paragraph you're annotating, as it appears in the
#     document; the tool matches it to the right paragraph automatically.
#     Small typos/whitespace differences are tolerated, but paste the
#     whole sentence (not a short fragment) so the match is unambiguous.
#
# Inside a block, optional "Author:", "Title:", and "Source:" lines come
# next; everything after that, up to the next block, is the note text
# (reflowed -- wrap it across as many lines as you like).
#
#     @def-cop
#     Author: J. Smith
#     Title: Institutional continuity
#     Source: Smith, Climate Law (2020) 45
#     The Protocol folds the COP into its own machinery rather than
#     creating a rival body.
#
#     """
#     Each Party included in Annex I shall ensure that its aggregate
#     anthropogenic carbon dioxide equivalent emissions do not exceed
#     its assigned amount.
#     """
#     Author: A. Nguyen
#     The word "shall" here is generally read as imposing a binding
#     obligation on Annex I parties.
#
# Repeat "@same-id" (or paste the same quote again) for a second note on
# the same paragraph.
#
# JSON FORMAT (also accepted -- a list of note objects)
# ---------------------------------------------------------------------
#     [
#       {"para": "def-cop", "author": "J. Smith", "title": "...",
#        "text": "...", "source": "..."},
#       {"quote": "Each Party included in Annex I shall ensure...",
#        "author": "A. Nguyen", "text": "..."}
#     ]
#
# Give either "para" (an id) or "quote" (an excerpt to match), plus "text".

ANN_BLOCK_START_RE = re.compile(r'^@([\w-]+)\s*$')
ANN_QUOTE_FENCE_RE = re.compile(r'^"{3,}\s*$')
ANN_FIELD_RE = re.compile(r'^(Author|Title|Source):\s*(.*)$', re.IGNORECASE)

# How close a pasted excerpt must be to a paragraph's text to count as a
# match. Comparisons run on normalized text (lowercased, punctuation and
# whitespace collapsed), so this only has to absorb things like retyped
# quotation marks or a missed word, not formatting noise.
QUOTE_MATCH_THRESHOLD = 0.85


def parse_annotations_text(path: Path):
    lines = path.read_text(encoding='utf-8').splitlines()
    items = []
    cur = None
    body_lines = []
    body_started = False
    in_quote = False
    quote_lines = []

    def flush():
        nonlocal cur, body_lines, body_started
        if cur is not None:
            text = ' '.join(l.strip() for l in body_lines if l.strip())
            cur['text'] = text.strip()
            if cur.get('text'):
                items.append(cur)
        cur, body_lines, body_started = None, [], False

    for raw in lines:
        stripped = raw.strip()

        if in_quote:
            if ANN_QUOTE_FENCE_RE.match(stripped):
                in_quote = False
                cur['quote'] = ' '.join(l.strip() for l in quote_lines if l.strip())
                quote_lines = []
            else:
                quote_lines.append(raw)
            continue

        if ANN_QUOTE_FENCE_RE.match(stripped):
            flush()
            cur = {}
            in_quote = True
            quote_lines = []
            continue

        bm = ANN_BLOCK_START_RE.match(stripped)
        if bm:
            flush()
            cur = {'para': bm.group(1)}
            continue
        if cur is None:
            continue
        if not stripped:
            continue
        fm = ANN_FIELD_RE.match(stripped)
        if fm and not body_started:
            cur[fm.group(1).lower()] = fm.group(2).strip()
            continue
        body_started = True
        body_lines.append(raw)

    flush()
    return items


def normalize_para_ref(ref: str) -> str:
    ref = str(ref).strip()
    return ref if ref.startswith('para-') else f'para-{ref}'


def match_quote_to_paragraph(quote: str, paragraphs_by_id: dict):
    """Match a pasted excerpt to a paragraph id by normalized text
    similarity. paragraphs_by_id maps para id -> paragraph text.

    Returns (matched_id_or_None, status) where status is one of:
      'ok'        -- a single confident match
      'ambiguous' -- two or more paragraphs matched closely enough that
                     picking one would be a guess
      'no_match'  -- nothing cleared the similarity threshold
    """
    norm_quote = normalize_text(quote)
    if not norm_quote:
        return None, 'no_match'

    scored = []
    for pid, ptext in paragraphs_by_id.items():
        norm_para = normalize_text(ptext)
        if not norm_para:
            continue
        # A quote that's fully contained in the paragraph (or vice versa,
        # for a contributor who pasted a slightly longer chunk spanning
        # into neighboring text) is treated as a confident match outright.
        if norm_quote in norm_para or norm_para in norm_quote:
            scored.append((pid, 1.0))
            continue
        ratio = difflib.SequenceMatcher(None, norm_quote, norm_para).ratio()
        if ratio >= QUOTE_MATCH_THRESHOLD:
            scored.append((pid, ratio))

    if not scored:
        return None, 'no_match'

    scored.sort(key=lambda x: x[1], reverse=True)
    if len(scored) > 1 and (scored[0][1] - scored[1][1]) < 0.03:
        return None, 'ambiguous'
    return scored[0][0], 'ok'


def build_notes(items, valid_ids=None, paragraphs_by_id=None):
    """Returns (notes, warnings, dropped_count, unmatched_quotes).

    Notes whose paragraph id doesn't match anything in valid_ids are
    reported in `warnings` but are NOT written into the output -- a note
    attached to an id with no matching <div id="..."> in the HTML has
    nowhere to attach to and will never be shown, so silently keeping it
    around just hides the problem instead of fixing it.

    Notes given as a quoted excerpt (no explicit "para"/"id") are resolved
    against `paragraphs_by_id` (id -> paragraph text). Quotes that can't be
    matched confidently are reported in `unmatched_quotes` -- as with
    mismatched ids, they are dropped rather than guessed at, because a
    wrong guess would attach someone's note to the wrong sentence.
    """
    notes = {}
    warnings = []
    unmatched_quotes = []
    dropped = 0
    for item in items:
        entry = {k: item[k] for k in ('author', 'title', 'text', 'source') if item.get(k)}
        if not entry.get('text'):
            continue

        ref = item.get('para') or item.get('id')
        quote = item.get('quote')

        if ref:
            para_id = normalize_para_ref(ref)
            mismatched = valid_ids is not None and para_id not in valid_ids
            if mismatched:
                warnings.append(para_id)
                dropped += 1
                continue
            notes.setdefault(para_id, []).append(entry)
            continue

        if quote:
            if not paragraphs_by_id:
                unmatched_quotes.append((quote, 'no_match'))
                dropped += 1
                continue
            para_id, status = match_quote_to_paragraph(quote, paragraphs_by_id)
            if status != 'ok':
                unmatched_quotes.append((quote, status))
                dropped += 1
                continue
            notes.setdefault(para_id, []).append(entry)
            continue

        # Neither a "para"/"id" nor a "quote" -- nothing to attach to.
        continue

    return notes, warnings, dropped, unmatched_quotes


def load_annotations_any(path: Path, valid_ids=None, paragraphs_by_id=None):
    """Detects plain-text vs JSON and returns
    (notes_dict, warnings_list, dropped_count, unmatched_quotes)."""
    text = path.read_text(encoding='utf-8')
    stripped = text.strip()
    if path.suffix.lower() == '.json' or stripped.startswith('['):
        raw = json.loads(text)
        if not isinstance(raw, list):
            raise ValueError("annotations JSON must be a list of note objects")
        items = raw
    else:
        items = parse_annotations_text(path)
    if not items:
        raise ValueError("no notes found in this file")
    return build_notes(items, valid_ids, paragraphs_by_id)


# ========================================================================
#  PART 3 -- source .txt + notes -> HTML + notes.js
# ========================================================================

SRC_MARKER_RE = re.compile(r'^(¶|\d+\.|\(\d+\)|\([a-z]+\)|\([ivxlcdm]+\))\s+(.*)$', re.IGNORECASE)
SRC_KEY_RE = re.compile(r'^\{([\w-]+)\}\s*(.*)$')
SRC_HEADER_RE = re.compile(r'^##\s+(.*)$')
SRC_META_RE = re.compile(r'^([A-Z_]+):\s*(.*)$')


def parse_source(path: Path):
    lines = path.read_text(encoding='utf-8').splitlines()

    meta = {}
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == '---':
            i += 1
            break
        m = SRC_META_RE.match(stripped)
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

        hm = SRC_HEADER_RE.match(line)
        if hm:
            paragraphs.append({'type': 'header', 'text': hm.group(1).strip()})
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
            label, content = mm.group(1), mm.group(2)
        else:
            label, content = '¶', line

        auto_n += 1
        pid_core = explicit_key if explicit_key else str(auto_n)
        para_id = f'para-{pid_core}'
        if para_id in seen_ids:
            raise ValueError(f"duplicate paragraph id '{para_id}' (explicit keys must be unique)")
        seen_ids.add(para_id)

        paragraphs.append({'type': 'para', 'id': para_id, 'label': label, 'text': content})

    if not any(p['type'] == 'para' for p in paragraphs):
        raise ValueError("no paragraphs found -- check the '---' front-matter delimiter")

    return meta, paragraphs


def esc(s: str) -> str:
    return html.escape(s, quote=False)


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
            out.append(PARA_TEMPLATE.format(pid=p['id'], label=esc(p['label']), text=p['text']))
    return ''.join(out)


def render_notes_js(notes: dict) -> str:
    body = json.dumps(notes, ensure_ascii=False, indent=2)
    return f"window.NOTES = {body};\nwindow.dispatchEvent(new Event('notes:ready'));\n"


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


# ========================================================================
#  PART 3b -- adding a text as a card to texts/all.html
# ========================================================================
#
# all.html holds its list of texts as a plain JS array of object literals
# (Alpine's `cases: [ ... ]`), right after a "// Add further text entries
# here" marker comment. To add a new card without a full HTML/JS parser,
# this finds that marker and splices in one more `{ title, subtitle, desc,
# href }` object in the same style as the existing entries.

ALL_HTML_MARKER = '// Add further text entries here'
ALL_HTML_CASE_RE = re.compile(
    r"""\{\s*
        title:\s*'(?P<title>(?:[^'\\]|\\.)*)'\s*,\s*
        subtitle:\s*'(?P<subtitle>(?:[^'\\]|\\.)*)'\s*,\s*
        desc:\s*'(?P<desc>(?:[^'\\]|\\.)*)'\s*,\s*
        href:\s*'(?P<href>(?:[^'\\]|\\.)*)'\s*
    \}""",
    re.VERBOSE | re.DOTALL,
)


def js_str_escape(s: str) -> str:
    return s.replace('\\', '\\\\').replace("'", "\\'").replace('\n', ' ')


def find_all_html_cases(all_html_text: str):
    """Returns a list of dicts (title/subtitle/desc/href) for every card
    currently in all.html's `cases` array."""
    return [m.groupdict() for m in ALL_HTML_CASE_RE.finditer(all_html_text)]


def add_case_to_all_html(all_html_text: str, title: str, subtitle: str, desc: str, href: str) -> str:
    """Returns updated all.html text with one more case object spliced in
    right after the marker comment, matching the existing entries' style."""
    if ALL_HTML_MARKER not in all_html_text:
        raise ValueError(
            f"couldn't find the marker comment ('{ALL_HTML_MARKER}') in all.html -- "
            "has the file's structure changed?"
        )
    entry = (
        "\n{\n"
        f"  title: '{js_str_escape(title)}',\n"
        f"  subtitle: '{js_str_escape(subtitle)}',\n"
        f"  desc: '{js_str_escape(desc)}',\n"
        f"  href: '{js_str_escape(href)}'\n"
        "},"
    )
    return all_html_text.replace(ALL_HTML_MARKER, ALL_HTML_MARKER + entry, 1)


def replace_case_in_all_html(all_html_text: str, href: str, title: str, subtitle: str, desc: str) -> str:
    """Replace an existing case object (matched by href) in-place, keeping
    its position in the array rather than appending a duplicate."""
    def repl(m):
        if m.group('href') != href:
            return m.group(0)
        return (
            "{\n"
            f"  title: '{js_str_escape(title)}',\n"
            f"  subtitle: '{js_str_escape(subtitle)}',\n"
            f"  desc: '{js_str_escape(desc)}',\n"
            f"  href: '{js_str_escape(href)}'\n"
            "}"
        )
    return ALL_HTML_CASE_RE.sub(repl, all_html_text, count=0)


# ========================================================================
#  PART 4 -- interactive menu
# ========================================================================

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding='utf-8')
    except OSError:
        pass


def prompt(msg, default=None):
    if default:
        raw = input(f"{msg} [{default}]: ").strip()
        return raw or default
    return input(f"{msg}: ").strip()


def clean_path_str(raw: str) -> str:
    """Tidy up a pasted/typed path before it hits Path().

    input() reads the raw line with no shell involved, so two very common
    habits silently break Path.exists() and are otherwise easy to miss:
      * surrounding quotes ("... .pdf" or '... .pdf') typed or pasted in
      * backslash-escaped spaces (My\\ File.pdf) left over from dragging a
        file into a terminal that does shell-style quoting
    Both leave literal characters in the string that aren't part of the
    real filename, so the file "doesn't exist" even though it does.
    """
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()
    if '\\' in s:
        s = re.sub(r'\\(.)', r'\1', s)
    return s


def resolve_path(raw: str) -> Path:
    return Path(clean_path_str(raw)).expanduser()


def prompt_yes_no(msg, default=False):
    d = 'y/N' if not default else 'Y/n'
    raw = input(f"{msg} [{d}]: ").strip().lower()
    if not raw:
        return default
    return raw.startswith('y')


def resolve_path_collision(path: Path, what: str = "file"):
    """If `path` already exists, ask the user to cancel, overwrite, or
    rename. Returns the Path to actually write to, or None if cancelled.

    Renaming asks for a new name and re-checks it for collisions too, so
    the user can't accidentally rename straight into another collision.
    """
    while path.exists():
        print(c_warn(f"\nA {what} already exists at:\n    {path}"))
        print(c_menu("  [c] Cancel   [o] Overwrite   [r] Rename"))
        choice = input("> ").strip().lower()
        if choice in ('c', 'cancel', ''):
            return None
        if choice in ('o', 'overwrite'):
            return path
        if choice in ('r', 'rename'):
            new_str = prompt(f"New name for the {what}", path.name)
            if not new_str:
                continue
            path = path.with_name(new_str)
            continue
        print(c_err("Not a valid option -- enter c, o, or r."))
    return path


ANNOTATION_FORMAT_HELP = """
------------------------------------------------------------------
 Annotation text format
------------------------------------------------------------------
One block per note. A block starts EITHER way:

  (a) a line "@<id>", where <id> matches a paragraph id from the
      source text -- with or without the "para-" prefix, or the bare
      sequential number if you didn't give that paragraph a {key} in
      the source file. Use this if you already know the id.

  (b) a quoted excerpt, fenced above and below by a line of three
      double-quotes ('\"\"\"'). Paste the full sentence or paragraph
      you're annotating, exactly as it reads in the document -- the
      tool will find the matching paragraph automatically. This is
      the format to hand to a contributor who doesn't know (and
      shouldn't need to know) any internal ids -- e.g. give them a
      little form: "paste the sentence you're annotating, your name,
      and your note," and their answers slot straight into this.

Inside a block, optional "Author:", "Title:", and "Source:" lines
come next. Everything after that, up to the next block, is the
note text -- wrap it across as many lines as you like.

    @def-cop
    Author: J. Smith
    Title: Institutional continuity
    Source: Smith, Climate Law (2020) 45
    The Protocol folds the COP into its own machinery rather than
    creating a rival body.

    \"\"\"
    Each Party included in Annex I shall ensure that its aggregate
    anthropogenic carbon dioxide equivalent emissions do not exceed
    its assigned amount.
    \"\"\"
    Author: A. Nguyen
    The word "shall" here is generally read as imposing a binding
    obligation on Annex I parties.

Repeat "@same-id" (or paste the same quote again) for a second note
on the same paragraph.

Paste the WHOLE sentence or paragraph, not a short fragment -- a
short snippet can match more than one place in the document, and
when that happens the note is reported as unmatched and dropped
rather than guessed at, so nothing gets silently attached to the
wrong passage.

A .json file (a list of {"para" or "quote", "author", "title",
"text", "source"} objects) is also accepted -- it's auto-detected
by the .json extension or by the file starting with "[".
------------------------------------------------------------------
"""


def print_status(state):
    print()
    print(c_title("=" * 64))
    print(c_title(" Annotated Text Builder"))
    print(c_title("=" * 64))
    print(f" 1. Source text : {c_path(state.get('source_txt')) if state.get('source_txt') else c_dim('(not set)')}")
    ann_label = state.get('annotations')
    if ann_label and state.get('annotations_is_js'):
        ann_label = f"{ann_label} {c_dim('(notes.js, used as-is)')}"
    elif ann_label:
        ann_label = c_path(ann_label)
    else:
        ann_label = c_dim('(not set)')
    print(f" 2. Annotations : {ann_label}")
    out_html = state.get('out_html')
    print(f" 3. Output      : {c_path(out_html) if out_html else c_dim('(not generated yet)')}")
    add_all = state.get('add_to_all_html', False)
    toggle_label = c_ok('ON') if add_all else c_dim('off')
    print(f" 4. Add to all.html after generating : {toggle_label}")
    print(c_dim("-" * 64))


def action_set_source(state):
    print()
    path_str = prompt("Path to a PDF to convert, or an existing source .txt (blank to cancel)")
    if not path_str:
        return
    path = resolve_path(path_str)
    if not path.exists():
        print(c_err(f"\nCouldn't find a file at:\n    {path}"))
        print(c_dim("(resolved from the input you gave -- check for a typo, a wrong"))
        print(c_dim(" working directory, or stray quotes/escaped spaces left over"))
        print(c_dim(" from dragging the file into the terminal)"))
        return

    if path.suffix.lower() == '.pdf':
        guessed = pdf_guess_title(path)
        title = prompt("Title", guessed)
        header = prompt("Header (short title-bar text)", title)
        desc = prompt("Description", "")
        home = prompt("Home link", "../index.html")
        closing = prompt("Closing statement (optional)", "")
        layout = prompt_yes_no(
            "Use layout-preserving extraction? (try this only if the default output looks jumbled)",
            False,
        )
        default_out = path.with_name(path.stem + "_source.txt")
        out_str = prompt("Output .txt path", str(default_out))
        out_path = Path(out_str).expanduser()

        meta = {'TITLE': title, 'DESC': desc, 'HEADER': header, 'HOME': home, 'CLOSING': closing}
        try:
            source_text, paragraphs, flags = convert_pdf_to_source(path, meta, layout=layout)
        except RuntimeError as e:
            print(c_err(f"\nCouldn't convert this PDF: {e}"))
            return
        except FileNotFoundError:
            print(c_err(
                "\nCouldn't find 'pdftotext' -- this script relies on poppler-utils "
                "for PDF extraction. Install it (e.g. 'apt install poppler-utils' "
                "or 'brew install poppler') and try again."
            ))
            return

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(source_text, encoding='utf-8')

        n_headers = sum(1 for p in paragraphs if p['type'] == 'header')
        n_paras = sum(1 for p in paragraphs if p['type'] == 'para')

        print(c_ok(f"\nWrote {out_path}"))
        print(f"{n_headers} heading(s), {n_paras} paragraph(s) parsed.")
        if flags:
            print(c_warn(f"{len(flags)} paragraph(s) worth a manual check:"))
            for _, snippet, reason in flags[:10]:
                print(c_warn(f"  - [{reason}] \"{snippet}\""))
            if len(flags) > 10:
                print(c_dim(f"  ... and {len(flags) - 10} more"))

        print(c_dim("\nOpen this file now if you want to fix paragraph breaks, tidy the"))
        print(c_dim("preamble, or add {key} tags to paragraphs you plan to annotate."))
        print(c_dim("This path is already remembered -- come back whenever you're ready."))

        state['source_txt'] = str(out_path)
    else:
        try:
            parse_source(path)
        except ValueError as e:
            print(c_err(f"\n'{path}' doesn't look like a valid source file: {e}"))
            return
        state['source_txt'] = str(path)
        print(c_ok(f"\nUsing '{path}' as the source text."))

    save_state(state)


def action_set_annotations(state):
    print()
    path_str = prompt(
        "Path to your annotations file -- text format, JSON, or an existing "
        "notes.js to use as-is (blank to cancel)"
    )
    if not path_str:
        return
    path = resolve_path(path_str)
    if not path.exists():
        print(c_err(f"\nCouldn't find a file at:\n    {path}"))
        print(c_dim("(resolved from the input you gave -- check for a typo, a wrong"))
        print(c_dim(" working directory, or stray quotes/escaped spaces left over"))
        print(c_dim(" from dragging the file into the terminal)"))
        return

    if path.suffix.lower() == '.js':
        # A ready-made notes.js -- skip the whole annotations pipeline and
        # just use this file's content verbatim at generation time. This is
        # for "I already have a notes.js from a previous build and don't
        # want to touch it" -- no re-matching, no re-parsing.
        text = path.read_text(encoding='utf-8')
        if 'window.NOTES' not in text:
            print(c_err(f"\n'{path}' doesn't look like a notes.js file (no 'window.NOTES' found)."))
            print(c_dim("If this is meant to be a plain-text or JSON annotations file, rename"))
            print(c_dim("it away from .js and set it again."))
            return
        state['annotations'] = str(path)
        state['annotations_is_js'] = True
        save_state(state)
        print(c_ok(f"\nUsing '{path}' as notes.js directly -- it will be copied through as-is"))
        print(c_ok("at generation time, with no re-matching against the source text."))
        return

    valid_ids = None
    paragraphs_by_id = None
    if state.get('source_txt'):
        src = Path(state['source_txt'])
        if src.exists():
            try:
                _, paragraphs = parse_source(src)
                valid_ids = {p['id'] for p in paragraphs if p['type'] == 'para'}
                paragraphs_by_id = {p['id']: p['text'] for p in paragraphs if p['type'] == 'para'}
            except ValueError:
                pass

    try:
        notes, warnings, dropped, unmatched_quotes = load_annotations_any(
            path, valid_ids, paragraphs_by_id
        )
    except (ValueError, json.JSONDecodeError) as e:
        print(c_err(f"\nCouldn't parse '{path}': {e}"))
        return

    n_notes = sum(len(v) for v in notes.values())
    print(c_ok(f"\nParsed {n_notes} note(s) across {len(notes)} paragraph id(s)."))
    if warnings:
        uniq = sorted(set(warnings))
        print(c_warn(f"\n*** {len(warnings)} note(s) will be DROPPED -- their id(s) don't match any"))
        print(c_warn(f"*** paragraph in the current source text, so they have nothing to"))
        print(c_warn(f"*** attach to and would never show up on the page:"))
        print(c_warn("      " + ", ".join(uniq)))
        if state.get('source_txt'):
            print(c_dim("If the source text was edited (paragraphs added/removed/reordered)"))
            print(c_dim("after these ids were written, auto-numbered ids (para-1, para-2, ...)"))
            print(c_dim("will have shifted under them. Add a stable {key} to that paragraph in"))
            print(c_dim("the source .txt and reference it as @key instead to avoid this."))
    if unmatched_quotes:
        print(c_warn(f"\n*** {len(unmatched_quotes)} quoted note(s) will be DROPPED -- couldn't match"))
        print(c_warn(f"*** the excerpt to exactly one paragraph in the source text:"))
        for quote, status in unmatched_quotes[:10]:
            reason = "no close match found" if status == 'no_match' else "matched more than one paragraph"
            snippet = quote if len(quote) <= 70 else quote[:67] + '...'
            print(c_warn(f"      - [{reason}] \"{snippet}\""))
        if len(unmatched_quotes) > 10:
            print(c_dim(f"      ... and {len(unmatched_quotes) - 10} more"))
        print(c_dim("Paste the full sentence/paragraph exactly as it appears in the"))
        print(c_dim("document, or use @id instead if you know the paragraph's id."))

    state['annotations'] = str(path)
    state['annotations_is_js'] = False
    save_state(state)


def action_generate(state):
    print()
    source_path = Path(state['source_txt'])
    ann_path = Path(state['annotations'])
    if not source_path.exists():
        print(c_err(f"'{source_path}' no longer exists -- set the source text again (option 1)."))
        return
    if not ann_path.exists():
        print(c_err(f"'{ann_path}' no longer exists -- set the annotations again (option 2)."))
        return

    # Default output location: a "texts" folder alongside index.html, in
    # the same directory this script lives in --
    #   project/
    #     index.html
    #     annotate.py        <- SCRIPT_DIR
    #     texts/             <- SCRIPT_DIR / 'texts'
    #       all.html
    #       <stem>.html       <- default_html
    #       <stem>-notes.js   <- default_notes
    texts_dir = SCRIPT_DIR / 'texts'
    default_html = texts_dir / (source_path.stem + '.html')
    out_html = resolve_path(prompt("Output HTML path", str(default_html)))

    out_html = resolve_path_collision(out_html, "text")
    if out_html is None:
        print(c_dim("\nCancelled."))
        return

    default_notes = out_html.with_name(out_html.stem + '-notes.js')
    out_notes = resolve_path(prompt("Output notes .js path", str(default_notes)))
    if out_notes != default_notes or out_notes.exists():
        # Only re-check for a collision if it wasn't already resolved by
        # renaming the html (which renames notes.js to match by default,
        # above) -- or if the user typed a different notes.js path by hand.
        out_notes = resolve_path_collision(out_notes, "notes.js file")
        if out_notes is None:
            print(c_dim("\nCancelled."))
            return

    try:
        meta, paragraphs = parse_source(source_path)
    except ValueError as e:
        print(c_err(f"\nCouldn't parse the source text: {e}"))
        return

    valid_ids = {p['id'] for p in paragraphs if p['type'] == 'para'}
    paragraphs_by_id = {p['id']: p['text'] for p in paragraphs if p['type'] == 'para'}

    warnings, unmatched_quotes = [], []
    if state.get('annotations_is_js'):
        # Ready-made notes.js -- copy through as-is, no matching pipeline.
        notes_js_text = ann_path.read_text(encoding='utf-8')
        notes = {}  # only used below for the annotated/notes-count summary
    else:
        try:
            notes, warnings, dropped, unmatched_quotes = load_annotations_any(
                ann_path, valid_ids, paragraphs_by_id
            )
        except (ValueError, json.JSONDecodeError) as e:
            print(c_err(f"\nCouldn't parse the annotations: {e}"))
            return

        if warnings:
            uniq = sorted(set(warnings))
            print(c_warn(f"\n*** {len(warnings)} note(s) DROPPED -- their id(s) don't match any paragraph"))
            print(c_warn(f"*** in the source text, so they won't appear on the page:"))
            print(c_warn("      " + ", ".join(uniq)))
        if unmatched_quotes:
            print(c_warn(f"\n*** {len(unmatched_quotes)} quoted note(s) DROPPED -- couldn't match the excerpt"))
            print(c_warn(f"*** to exactly one paragraph in the source text:"))
            for quote, status in unmatched_quotes[:10]:
                reason = "no close match found" if status == 'no_match' else "matched more than one paragraph"
                snippet = quote if len(quote) <= 70 else quote[:67] + '...'
                print(c_warn(f"      - [{reason}] \"{snippet}\""))
            if len(unmatched_quotes) > 10:
                print(c_dim(f"      ... and {len(unmatched_quotes) - 10} more"))

        notes_js_text = render_notes_js(notes)

    body_html = render_body(paragraphs)
    # Cache-bust notes.js with a content hash, so a browser tab you already
    # had open picks up the new notes instead of serving a stale cached
    # copy of the previous version at the same filename.
    notes_hash = hashlib.sha1(notes_js_text.encode('utf-8')).hexdigest()[:10]
    page = build_page(meta, body_html, f"{out_notes.name}?v={notes_hash}")

    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_notes.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(page, encoding='utf-8')
    out_notes.write_text(notes_js_text, encoding='utf-8')

    n_paras = sum(1 for p in paragraphs if p['type'] == 'para')
    n_annotated = sum(1 for pid in valid_ids if pid in notes)
    n_notes_total = sum(len(v) for v in notes.values())
    n_dropped_total = len(warnings) + len(unmatched_quotes)

    print(c_ok(f"\nWrote {out_html}"))
    print(c_ok(f"Wrote {out_notes}"))
    if state.get('annotations_is_js'):
        print(c_dim("(notes.js copied through as-is -- no annotation matching was run)"))
    else:
        summary = f"{n_paras} paragraph(s), {n_annotated} annotated, {n_notes_total} note(s) total"
        if n_dropped_total:
            print(c_warn(summary + f", {n_dropped_total} dropped."))
        else:
            print(summary + ".")

    state['out_html'] = str(out_html)
    state['out_notes'] = str(out_notes)
    save_state(state)

    if state.get('add_to_all_html'):
        action_add_to_all_html(state, meta, out_html, texts_dir)


def action_add_to_all_html(state, meta: dict, out_html: Path, texts_dir: Path):
    """Adds/updates a card for the just-generated text in texts/all.html.
    Called from action_generate only when the toggle is enabled."""
    all_html_path = texts_dir / 'all.html'
    print()
    if not all_html_path.exists():
        print(c_warn(f"'add to all.html' is on, but no all.html was found at:\n    {all_html_path}"))
        print(c_dim("Skipping -- add the card by hand, or set the texts folder up first."))
        return

    try:
        all_html_text = all_html_path.read_text(encoding='utf-8')
    except OSError as e:
        print(c_err(f"Couldn't read '{all_html_path}': {e}"))
        return

    title = meta.get('HEADER') or meta.get('TITLE') or out_html.stem
    subtitle = prompt("Card subtitle for all.html (e.g. 'ICJ — 2024')", "")
    desc = prompt("Card description for all.html", meta.get('DESC', ''))
    href = f'./{out_html.name}'

    try:
        existing_cases = find_all_html_cases(all_html_text)
    except Exception:
        existing_cases = []
    existing = next((c for c in existing_cases if c['href'] == href), None)

    if existing:
        print(c_warn(f"\nA card for '{href}' already exists in all.html:"))
        print(c_warn(f"    title: {existing['title']}"))
        print(c_menu("  [c] Cancel   [o] Overwrite existing card   [r] Rename this text's link"))
        choice = input("> ").strip().lower()
        if choice in ('c', 'cancel', ''):
            print(c_dim("Cancelled -- all.html left unchanged."))
            return
        if choice in ('r', 'rename'):
            new_href = prompt("New href for this text (relative to texts/)", href)
            href = new_href if new_href.startswith('./') or '/' in new_href else f'./{new_href}'
            existing = next((c for c in existing_cases if c['href'] == href), None)
            if existing:
                print(c_warn(f"'{href}' is also already in use -- cancelling to avoid another collision."))
                return
            updated = add_case_to_all_html(all_html_text, title, subtitle, desc, href)
        else:  # overwrite
            updated = replace_case_in_all_html(all_html_text, href, title, subtitle, desc)
    else:
        try:
            updated = add_case_to_all_html(all_html_text, title, subtitle, desc, href)
        except ValueError as e:
            print(c_err(f"Couldn't update all.html: {e}"))
            return

    try:
        all_html_path.write_text(updated, encoding='utf-8')
    except OSError as e:
        print(c_err(f"Couldn't write '{all_html_path}': {e}"))
        return

    print(c_ok(f"Updated {all_html_path} with a card for '{title}' -> {href}"))


def menu_loop():
    state = load_state()
    while True:
        print_status(state)
        ready = bool(state.get('source_txt')) and bool(state.get('annotations'))
        lock = c_dim("   [locked -- complete 1 and 2 first]") if not ready else ""
        add_all = state.get('add_to_all_html', False)
        toggle_label = c_ok('ON') if add_all else c_dim('off')
        print(c_menu(" 1) Set source text (convert a PDF, or point at an existing .txt)"))
        print(c_menu(" 2) Set annotations (a formatted text file, JSON, or an existing notes.js)"))
        print(c_menu(f" 3) Generate the annotated HTML + notes.js") + lock)
        print(c_menu(" 4) Show the annotation text format"))
        print(c_menu(" 5) Toggle 'add to all.html' after generating -- currently ") + toggle_label)
        print(c_menu(" 0) Quit"))
        print(c_dim("-" * 64))
        choice = input("> ").strip()

        if choice == '1':
            action_set_source(state)
        elif choice == '2':
            action_set_annotations(state)
        elif choice == '3':
            if not ready:
                print(c_err("\nSet both the source text and annotations first."))
            else:
                action_generate(state)
        elif choice == '4':
            print(ANNOTATION_FORMAT_HELP)
        elif choice == '5':
            state['add_to_all_html'] = not add_all
            save_state(state)
            new_label = c_ok('ON') if state['add_to_all_html'] else c_dim('off')
            print(f"\n'Add to all.html' is now " + new_label + ".")
        elif choice == '0':
            print(c_dim("Bye."))
            return
        else:
            print(c_err("\nNot a valid option."))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args()
    try:
        menu_loop()
    except (KeyboardInterrupt, EOFError):
        print(c_dim("\nBye."))


if __name__ == '__main__':
    main()