# International Law Annotated — build tool

Turns an instrument (a PDF of a treaty, judgment or opinion) plus its
annotations into an annotated web page in the style of this site: click a
highlighted paragraph and its annotations open in a pane on the right, with an
author filter, a note-density rail beside the text, and a jump link.

The whole tool is `build_tool.py` plus the `ilatool/` package next to it.
Nothing needs installing for the basic path; `pymupdf` is optional and
strictly better (see *PDF backends* below).

---

## Quick start

```bash
python3 build_tool.py                     # interactive interface
python3 build_tool.py doctor              # what is available here
```

Or non-interactively:

```bash
# Everything in one go: PDF text + Word-comment annotations -> website
python3 build_tool.py build materials/CISG/CISG_english.pdf \
    -a materials/CISG/CISG_annotated.docx \
    -o texts/cisg.html --title "CISG"

# A Word document is a source in its own right: the body is the text and the
# comments are the annotations, so one file supplies both halves.
python3 build_tool.py build "materials/UNFCCC/Kyoto Protocol Annotated.docx" \
    -o texts/kyoto.html --title "Kyoto Protocol"

# Step by step
python3 build_tool.py convert some.pdf            # PDF -> source .txt
python3 build_tool.py inspect some.txt            # what was parsed
python3 build_tool.py annotations notes.json      # what the notes say
python3 build_tool.py build some.txt -a notes.json -o texts/some.html
```

`build` writes three or four files:

| file | what it is |
|---|---|
| `<name>.html` | the annotated page |
| `<name>-notes.js` | the notes, as `window.NOTES` |
| `<name>.txt` | (PDF input only) the editable source text |
| `<name>.layout.json` | (PDF input only) the full parsed structure, for debugging |
| `<name>.build-report.json` | stages, timings, warnings and validation results |

---

## The workflow

```
PDF or .docx ──► structured document ──► source text ──► page + notes.js
             │                          ▲
             └── page layout, columns,  │
                 headings, paragraphs   │
                                        │
annotations (.txt/.json/.csv/.docx/.pdf)┘  matched onto paragraphs
```

The intermediate **source text** is deliberately a readable, editable file:
when the parser gets a paragraph break wrong you fix it in the text file
rather than fighting the PDF. Re-running `build` on the `.txt` is instant.

### The source text format

```
TITLE: Kyoto Protocol to the United Nations Framework Convention on Climate Change
DESC: Annotated Kyoto Protocol text with inline author-filtered notes.
HEADER: Kyoto Protocol
HOME: ../index.html
CLOSING: Done at Kyoto this tenth day of December one thousand nine hundred and ninety-seven.
IDMAP para-9=k1f3e9030
---
## Article 1
{k1f3e9030} ¶ For the purposes of this Protocol, the definitions in Article 1 apply.
(1) "Conference of the Parties" means the Conference of the Parties to the Convention.
```

* `## ` starts a heading.
* Every other line is one annotatable paragraph. A leading marker
  (`¶`, `1.`, `(a)`, `(iv)`, `•`, `§`) becomes the paragraph's visible label.
* `{key}` gives a paragraph a stable id (`para-<key>`). The PDF converter
  writes one for every paragraph, derived from a hash of its text.
* `IDMAP <old-id>=<key>` keeps an *older* source file's positional ids
  resolvable. When you re-convert a PDF over an existing source the tool
  reads that file, matches its paragraphs to the new ones **by text**, and
  writes the aliases out, so `@para-9` notes keep pointing at the same
  sentence. Nothing is ever re-attached by position.

### Annotation formats

Run `python3 build_tool.py menu` → *Show the annotation formats*, or see
`ilatool/annotations.py`.

* **`.txt`** — `@<id>` blocks or `"""`-fenced excerpts, with optional
  `Author:` / `Title:` / `Source:` lines.
* **`.json`** — a list of `{"para"|"quote", "author", "title", "text", "source"}`.
* **`.csv` / `.tsv`** — the prep-folder columns
  (`id`/`quote` + `comment_author`/`comment_title`/`comment_text`/`comment_source`).
* **`.docx`** — Word comments. Author, date, and the anchored text range come
  straight out of the file, so there is nothing to retype.  A `.docx` can also
  be the *text* source (see above): its heading styles are used directly, and
  a paragraph Word split across several elements is joined back together.  A
  note anchored to a heading attaches to the text that heading introduces.
* **`.pdf`** — the PDF's own markup (highlights, underlines, sticky notes),
  with page, position and author.

An excerpt that cannot be matched to exactly one paragraph is **reported, not
guessed**: a note on the wrong sentence is worse than a note you place by hand.

---

## Using the interface

| key | action |
|---|---|
| `↑` `↓` or `k` `j` | move the selection |
| `Enter` | choose |
| `1`–`7` | jump straight to a step |
| `PgUp` / `PgDn` / `Home` / `End` | scroll a long report |
| `Esc` | back to the menu (from the menu it does nothing) |
| `q` or `Ctrl-C` | quit |

Arrow keys are read as full escape sequences, including the `ESC O A` form
used by tmux and screen and modified forms like `Ctrl-↑`. `Esc` on its own is
never treated as "quit": a terminal that delivers an arrow key's bytes slowly
can produce a bare `Esc`, and that must not end the session.

---

## PDF backends

| backend | how to get it | what it gives you |
|---|---|---|
| **PyMuPDF** (preferred) | `pip install pymupdf` | exact coordinates, native annotations, correct rotated pages, vector drawings |
| **poppler** (fallback) | `brew install poppler` / `apt install poppler-utils` | everything else; geometry on rotated pages is approximate |

The tool picks the best one automatically and says which it used.
`python3 build_tool.py doctor` reports the choice.

---

## What the parser does about the hard cases

* **Reading order.** Lines are ordered by column and then by position, not by
  the order the PDF happens to draw them in. Two-column pages read down the
  left column and then down the right; a full-width title spanning both is
  kept in the right place.
* **Running headers and footers.** Removed when the same line repeats in the
  same margin, when it looks like a page number / URL / browser print stamp,
  or when it is an isolated "Article 9 (continued)" separated from the body by
  a gap far larger than the line spacing.
* **Paragraphs across page breaks** are joined, undoing end-of-line
  hyphenation.
* **Broken fonts.** Ligature code points are normalised and the common
  macOS/browser artifact where the `ff` ligature extracts as a double quote
  (`di"erent`) is repaired. Every repair is counted and reported.
* **Headings** are recognised from structural keywords, relative font size,
  weight and indentation -- never from a line that is merely short. A heading
  is always a block of its own, and a short "Article 7" always starts one.
* **Cover matter and back matter.** A court publication opens with a title
  page, often in two languages, and sometimes pages of summary; it closes with
  a contents list of the fascicle. Everything before the first run of
  numbered paragraphs, and any trailing contents list, is skipped and
  reported. `--keep-front-matter` turns that off.
* **Quoted lists stay together.** "(1) ...; (2) ...; (3) ..." inside quotation
  marks is one paragraph, not four. A clause marker *outside* a quotation
  still starts its own paragraph.
* **Cut-off paragraphs are detected.** A conversion that produces one
  7,000-character paragraph has merged two; a conversion that produces forty
  one-line paragraphs has split one. Both are tested against the published
  `texts/nicaragua-germany.html`.
* **Scanned pages** (no text layer) and rotated pages are detected and
  reported rather than silently producing nonsense.
* **Everything is escaped.** Paragraph text containing `<`, `&` or quotes can
  no longer break the generated page.

## The page

Clicking any paragraph that has annotations opens them in a pane on the right.
The page **makes room** for the pane rather than the pane floating over the
text: the reading column keeps its width and slides across, so nothing is
covered and nothing shrinks to fit. Above roughly 1440px that means the column
is untouched apart from the shift; on narrower screens it narrows only as much
as it must, and the paragraph you clicked stays exactly where it was on screen
so the page never appears to jump. The pane is flush with the page edge — no
shadow, no dimming — and below 1024px, where there is no room for both, it
becomes an overlay and then a full screen on a phone.

The pane can be dismissed with its ✕, by clicking outside it, or with Esc, and
`↑`/`↓` (or Alt+↑ / Alt+↓) step through the annotated paragraphs without going
back to the text. The paragraph being annotated stays highlighted. Each note
ends with its author as a signature, so the note itself gets the top of the
card.

The rail beside the text marks where the annotated paragraphs are. Hovering a
mark tells you which paragraph it is and how many notes it has; clicking it
scrolls there and opens the pane. Marks that would land on top of each other
are pushed apart so all of them stay clickable, and the rail is hidden when
the column has slid far enough over that its gutter is gone.

## Validation

Every build re-reads what it wrote and checks it: every paragraph is present
and unique, every note points at a paragraph that exists, the page loads the
notes file that was actually written, and the notes file parses and is
complete. A build that fails a check exits non-zero.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

`tests/test_corpus.py` rebuilds every page in `texts/` from its material and
asserts the formatting inequalities that make a page readable: no paragraph
starts mid-sentence, none is a runaway merge, none is page furniture, and the
published text survives.  `tests/test_reference_parity.py` compares the
Nicaragua conversion with the published page paragraph by paragraph.

The fixtures in `tests/fixtures/pdf/` are generated (not downloaded) by
`tests/fixtures/make_fixtures.py` and cover multi-column pages, running
headers, page-break paragraph continuation, mixed page sizes, rotation,
images, vector graphics, empty pages and every PDF annotation subtype.
With PyMuPDF installed the suite also checks that both backends agree.

## Older per-instrument scripts

`texts/ccao_prep_folder/`, `texts/ila_prep_folder/` and
`texts/unfccc-builder/` contain the tool's predecessors: separate
`extract_icj.py` / `build_site.py` / `qc.py` scripts that each re-implemented
part of this pipeline against a CSV. They are kept for reference and for any
spreadsheet-based workflow still running against them, but they are
**superseded** — `build_tool.py build … -a instrument.csv` covers the same
ground with the current parser, and fixes to parsing or rendering belong in
`ilatool/`, not in those copies.
