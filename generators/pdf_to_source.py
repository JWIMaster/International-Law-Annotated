#!/usr/bin/env python3
"""
pdf_to_source.py

Convert a PDF of a legal instrument (treaty, statute, convention, protocol...)
into the plain-text "source" format consumed by generate_annotated.py:

    TITLE: ...
    DESC: ...
    HEADER: ...
    HOME: ../index.html
    CLOSING: ...
    ---
    ¶ preamble line...
    ## Article 1
    1. numbered paragraph...
    (a) lettered sub-paragraph...
    (i) roman-numeral sub-paragraph...

This is a best-effort converter, not a certified OCR/legal-parsing tool.
PDFs vary wildly in how they're typeset, so always skim the generated .txt
file before feeding it to generate_annotated.py -- especially the preamble
(recitals often aren't numbered, so paragraph-break detection there relies
on blank lines in the PDF actually separating each recital).

------------------------------------------------------------------------
WHAT IT DOES
------------------------------------------------------------------------
  1. Extracts the text layer with `pdftotext` (poppler-utils).
  2. Strips running headers/footers -- lines that repeat (allowing for a
     changing page number) at the top or bottom of most pages.
  3. Detects heading lines (Article / Part / Chapter / Section / Annex /
     Title + a number) and turns them into "## " headings.
  4. Re-wraps hard-wrapped PDF lines back into single logical paragraphs,
     undoing end-of-line hyphenation, and splitting on:
       - a blank line in the source PDF
       - a new heading
       - a line that starts with a recognised paragraph marker
         (¶, "1.", "1)", "(a)", "(i)")
  5. If the preamble ends up fused into one block (common when a PDF has no
     blank line between recitals), re-splits it at standard recital-opening
     words ("Recognizing", "Recalling", "Being guided by", "Have agreed",
     etc.) -- disable with --no-split-recitals if this misfires.
  6. Writes the result in the source format above, with a front-matter
     block you can hand-edit (or pre-fill with --title/--desc/etc).

It deliberately does NOT try to invent paragraph ids/keys -- paragraphs are
left to auto-number (para-1, para-2, ...) by generate_annotated.py. Add
"{your-key} " in front of any line you plan to annotate if you want a
stable id that survives re-running the converter.

------------------------------------------------------------------------
USAGE
------------------------------------------------------------------------
    python pdf_to_source.py treaty.pdf --out treaty_source.txt \
        --title "Kyoto Protocol to the UNFCCC" \
        --header "Kyoto Protocol"

    # Skip a cover/annex page range, and inspect without writing:
    python pdf_to_source.py treaty.pdf --out out.txt --first-page 2 --last-page 30 --dry-run
"""

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

HEADING_RE = re.compile(
    r'^(Article|Part|Chapter|Section|Annex|Title)\s+[IVXLCDM0-9]+[A-Za-z]*\b.{0,60}$',
    re.IGNORECASE,
)

# Order matters: try numbered, lettered, roman, bare pilcrow.
MARKER_RE = re.compile(
    r'^(?:'
    r'(?P<num>\d+)[.\)]'
    r'|\((?P<letter>[a-z]{1,3})\)'
    r'|\((?P<roman>[ivxlcdm]{1,6})\)'
    r'|(?P<pilcrow>¶)'
    r')\s+(?P<rest>.*)$',
    re.IGNORECASE,
)

PAGE_NUM_LINE_RE = re.compile(r'^\s*[-–—]?\s*\d+\s*[-–—]?\s*$|^\s*page\s+\d+(\s+of\s+\d+)?\s*$', re.IGNORECASE)

# Recital preambles ("The Parties to this Protocol, / Recognizing that..., /
# Recalling..., / Have agreed as follows:") are near-universal in treaty
# drafting, but PDFs often typeset them with no blank line between clauses,
# so pdftotext fuses the whole preamble into one paragraph. These cue words
# almost always start a new recital, so we use them to re-split it.
RECITAL_CUES = [
    'Being', 'Recognizing', 'Recognising', 'Recalling', 'Further recalling',
    'Noting', 'Taking note', 'Taking into account', 'Considering', 'Desiring',
    'Desirous', 'Reaffirming', 'Bearing in mind', 'Convinced', 'Determined',
    'Concerned', 'Emphasizing', 'Emphasising', 'Underlining', 'Acknowledging',
    'Mindful', 'Guided by', 'Welcoming', 'Alarmed by', 'Aware that',
    'Affirming', 'Conscious', 'Stressing', 'Have agreed', 'In pursuit of',
    'Pursuant to',
]
RECITAL_CUE_RE = re.compile(
    r',\s+(?=(?:' + '|'.join(re.escape(c) for c in RECITAL_CUES) + r')\b)'
)


def split_recitals(text: str):
    """Split a fused preamble paragraph back into one clause per recital."""
    parts = [p.strip() for p in RECITAL_CUE_RE.split(text) if p.strip()]
    if len(parts) < 2:
        return [text]
    for i in range(len(parts) - 1):
        if not parts[i].endswith((',', ';', ':', '.')):
            parts[i] += ','
    return parts


# ---------------------------------------------------------------- extract --

def extract_raw_text(pdf_path: Path, first_page=None, last_page=None, layout=False) -> str:
    cmd = ['pdftotext']
    if layout:
        cmd.append('-layout')
    if first_page:
        cmd += ['-f', str(first_page)]
    if last_page:
        cmd += ['-l', str(last_page)]
    cmd += [str(pdf_path), '-']
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"error: pdftotext failed:\n{result.stderr}")
    if not result.stdout.strip():
        sys.exit("error: no text extracted -- this PDF may be scanned/raster-only "
                 "(no text layer). OCR it first (see the pdf-reading skill) and re-run.")
    return result.stdout


def strip_running_headers_footers(raw_text: str) -> str:
    """Drop lines that repeat at the top/bottom of most pages (mastheads,
    page numbers, running titles), allowing digits to vary between pages.
    Also trims blank lines right at each page boundary so that a paragraph
    which happens to wrap across a page break isn't mistaken for two
    separate paragraphs (real paragraph breaks *within* a page are left
    untouched)."""
    pages = raw_text.split('\x0c')  # poppler emits a form-feed between pages
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
            if PAGE_NUM_LINE_RE.match(lines[i]):
                drop.add(i)
        kept = [l for i, l in enumerate(lines) if i not in drop]
        # trim leading/trailing blank lines so the page boundary itself
        # never looks like a paragraph break
        while kept and not kept[0].strip():
            kept.pop(0)
        while kept and not kept[-1].strip():
            kept.pop()
        out_page_lines.append(kept)

    return '\n'.join('\n'.join(lines) for lines in out_page_lines)


# ------------------------------------------------------------------ parse --

def dehyphenate_join(a: str, b: str) -> str:
    """Join a wrapped line back onto its continuation, undoing a trailing
    end-of-line hyphen (but leaving genuine hyphenated compounds like
    "long-term" -- which never appear at the very end of a line -- alone)."""
    if re.search(r'[A-Za-z]-$', a):
        return a[:-1] + b
    return a + ' ' + b


def normalize_text(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', s.lower()).strip()


def group_paragraphs(raw_text: str, skip_title: str = None, split_recitals_enabled: bool = True):
    """Walk the cleaned line stream, grouping wrapped PDF lines into logical
    paragraphs. Returns a list of {'type': 'header'|'para', ...} dicts.
    Lines that exactly match `skip_title` (a title-page banner repeating the
    instrument's name) are dropped rather than merged into the preamble."""
    lines = [l.strip() for l in raw_text.replace('\x0c', '\n').split('\n')]
    skip_norm = normalize_text(skip_title) if skip_title else None

    paragraphs = []
    buf_text = None
    buf_label = None

    def flush():
        nonlocal buf_text, buf_label
        if buf_text:
            paragraphs.append({'type': 'para', 'label': buf_label, 'text': buf_text.strip()})
        buf_text, buf_label = None, None

    for line in lines:
        if not line:
            flush()
            continue

        if skip_norm and normalize_text(line) == skip_norm:
            continue

        if HEADING_RE.match(line):
            flush()
            paragraphs.append({'type': 'header', 'text': line})
            continue

        mm = MARKER_RE.match(line)
        if mm:
            flush()
            if mm.group('num') is not None:
                buf_label = f"{mm.group('num')}."
            elif mm.group('letter') is not None:
                buf_label = f"({mm.group('letter')})"
            elif mm.group('roman') is not None:
                buf_label = f"({mm.group('roman')})"
            else:
                buf_label = '¶'
            buf_text = mm.group('rest')
            continue

        # continuation of the current paragraph, or an unmarked line
        # (e.g. a preamble recital) starting a new default paragraph
        if buf_text is None:
            buf_label = '¶'
            buf_text = line
        else:
            buf_text = dehyphenate_join(buf_text, line)

    flush()

    if split_recitals_enabled:
        # only touch the leading run of unmarked (¶) paragraphs before the
        # first heading or first explicitly-marked (1./（a)/（i)) paragraph
        expanded = []
        in_leading_run = True
        for p in paragraphs:
            if in_leading_run and p['type'] == 'para' and p['label'] == '¶':
                for clause in split_recitals(p['text']):
                    expanded.append({'type': 'para', 'label': '¶', 'text': clause})
            else:
                in_leading_run = False
                expanded.append(p)
        paragraphs = expanded

    return paragraphs


def looks_suspicious(paragraphs):
    """Flag short/likely-garbled paragraphs so the user knows to double-check them."""
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


# ---------------------------------------------------------------- render --

def escape_marker_for_output(label: str) -> str:
    # Output format expects "1." "(a)" "(i)" or "¶" exactly -- already canonical.
    return label


def render_source(meta: dict, paragraphs) -> str:
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
            label = escape_marker_for_output(p['label'])
            lines.append(f"{label} {p['text']}")
    lines.append('')
    return '\n'.join(lines)


# -------------------------------------------------------------------- main --

def guess_title(pdf_path: Path) -> str:
    # crude fallback: pdfinfo's Title field, else the PDF's filename
    try:
        result = subprocess.run(['pdfinfo', str(pdf_path)], capture_output=True, text=True)
        m = re.search(r'^Title:\s*(.+)$', result.stdout, re.MULTILINE)
        if m and m.group(1).strip():
            return m.group(1).strip()
    except FileNotFoundError:
        pass
    return pdf_path.stem.replace('_', ' ').replace('-', ' ').title()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pdf', type=Path, help='input PDF')
    ap.add_argument('--out', type=Path, required=True, help='output .txt path')
    ap.add_argument('--title', help='TITLE front-matter field (defaults to PDF metadata / filename)')
    ap.add_argument('--desc', help='DESC front-matter field')
    ap.add_argument('--header', help='HEADER front-matter field (short header-bar title)')
    ap.add_argument('--home', default='../index.html', help='HOME front-matter field')
    ap.add_argument('--closing', help='CLOSING front-matter field (e.g. "Done at ... this ... day of ...")')
    ap.add_argument('--first-page', type=int, help='first PDF page to extract')
    ap.add_argument('--last-page', type=int, help='last PDF page to extract')
    ap.add_argument('--layout', action='store_true', help='use pdftotext -layout (try this if the default output looks jumbled, e.g. multi-column PDFs)')
    ap.add_argument('--keep-boilerplate', action='store_true', help='skip running header/footer stripping')
    ap.add_argument('--no-split-recitals', action='store_true', help="don't split a fused preamble into one recital per line (see docstring)")
    ap.add_argument('--dry-run', action='store_true', help="parse and report, but don't write the output file")
    args = ap.parse_args()

    if not args.pdf.exists():
        sys.exit(f"error: {args.pdf} not found")

    raw = extract_raw_text(args.pdf, args.first_page, args.last_page, args.layout)
    if not args.keep_boilerplate:
        raw = strip_running_headers_footers(raw)

    title = args.title or guess_title(args.pdf)
    paragraphs = group_paragraphs(raw, skip_title=title, split_recitals_enabled=not args.no_split_recitals)
    if not any(p['type'] == 'para' for p in paragraphs):
        sys.exit("error: no paragraphs detected -- try --layout, or check the PDF actually "
                 "has a text layer (pdffonts <file>).")

    meta = {
        'TITLE': title,
        'DESC': args.desc or '',
        'HEADER': args.header or title,
        'HOME': args.home,
        'CLOSING': args.closing or '',
    }

    n_headers = sum(1 for p in paragraphs if p['type'] == 'header')
    n_paras = sum(1 for p in paragraphs if p['type'] == 'para')
    flags = looks_suspicious(paragraphs)

    print(f"parsed: {n_headers} headings, {n_paras} paragraphs")
    if flags:
        print(f"warning: {len(flags)} paragraph(s) look worth a manual check:", file=sys.stderr)
        for i, snippet, reason in flags[:15]:
            print(f"  - [{reason}] \"{snippet}\"", file=sys.stderr)
        if len(flags) > 15:
            print(f"  ... and {len(flags) - 15} more", file=sys.stderr)

    if args.dry_run:
        print("\n--- dry run: preview of first 20 lines ---")
        preview = render_source(meta, paragraphs).splitlines()
        print('\n'.join(preview[:20]))
        return

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_source(meta, paragraphs), encoding='utf-8')
    print(f"wrote {args.out}")
    print("Review the file, then run generate_annotated.py to build the HTML page.")


if __name__ == '__main__':
    main()
