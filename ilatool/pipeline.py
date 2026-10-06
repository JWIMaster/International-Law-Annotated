"""The end-to-end pipeline, as explicit stages.

```
load  ->  layout  ->  structure  ->  (annotations)  ->  match  ->  render
      ->  write   ->  validate
```

Every stage reports progress through the same callback and records anything
the user should know in one :class:`~ilatool.errors.Diagnostics` collection,
so the TUI and the headless CLI describe a run in exactly the same words.
Temporary files are never left behind, and nothing is written to the output
paths until the whole in-memory result is known to be sound -- a failed build
cannot leave a half-written page behind.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import annotations as ann_mod, backends, docx, layout as layout_mod, textutil
from . import render, sourcefmt, structure, validate
from .errors import (
    Diagnostics,
    InputError,
    InternalError,
    Stage,
    StructureError,
    ToolError,
    UnsupportedDocumentError,
    WriteError,
)
from .pdfmodel import PdfDocument
from .sourcefmt import SourceDocument
from .structure import Block, StructuredDocument

#: progress(stage, fraction, message) -- fraction is 0..1, or None when the
#: stage cannot estimate.
ProgressFn = Callable[[Stage, Optional[float], str], None]


def _noop(stage: Stage, fraction: Optional[float], message: str) -> None:
    return None


@dataclass
class ConvertOptions:
    """How to turn a PDF into a source text."""

    backend: str = "auto"
    drop_front_matter: bool = True
    split_recitals: bool = True
    drop_running_headers: bool = True
    password: str = ""
    keep_aliases_from: Optional[Path] = None
    write_json: bool = True
    #: Skip the "is this even a text PDF?" pre-flight.
    force: bool = False


@dataclass
class ConvertResult:
    source: SourceDocument
    source_path: Optional[Path] = None
    json_path: Optional[Path] = None
    document: Optional[PdfDocument] = None
    layout: Optional[layout_mod.DocumentLayout] = None
    stats: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BuildOptions:
    backend: str = "auto"
    use_pdf_annotations: bool = True
    include_empty_annotations: bool = False
    validate: bool = True
    write_diagnostics: bool = True


@dataclass
class BuildResult:
    source: SourceDocument
    structured: StructuredDocument
    notes: Dict[str, List[Dict[str, str]]]
    match: ann_mod.MatchResult
    page: render.RenderedPage
    html_path: Optional[Path] = None
    notes_path: Optional[Path] = None
    validation: Optional[validate.ValidationReport] = None
    stats: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Stage 1-4: PDF -> structured source
# ---------------------------------------------------------------------------

def convert_pdf(pdf_path: Path, meta: Dict[str, str], options: ConvertOptions,
                progress: ProgressFn = _noop,
                diagnostics: Optional[Diagnostics] = None) -> ConvertResult:
    """Read a PDF and produce the structured document and its source text."""
    diag = diagnostics if diagnostics is not None else Diagnostics()
    if not pdf_path.exists():
        raise InputError(f"no file at '{pdf_path}'")
    if not pdf_path.is_file():
        raise InputError(f"'{pdf_path}' is a directory, not a PDF")

    # Fail fast on a document with no text layer: reading a scanned PDF page
    # by page is slow, and there is nothing to annotate anyway.
    if not options.force:
        sampled, with_text = backends.quick_text_probe(pdf_path)
        if sampled and not with_text:
            raise UnsupportedDocumentError(
                f"'{pdf_path.name}' has no text layer on any of the {sampled} "
                "page(s) checked",
                hint="It looks like a scanned document. OCR it first (for "
                     "example with 'ocrmypdf in.pdf out.pdf'), then convert the "
                     "OCR'd copy.",
                context={"pages_sampled": sampled},
            )

    progress(Stage.LOAD, 0.02, f"opening {pdf_path.name}")
    doc = backends.load_pdf(
        pdf_path,
        backend=options.backend,
        password=options.password,
        progress=lambda i, total, msg: progress(
            Stage.PARSE, (i / total) if total else None,
            f"extracting text ({msg})"),
    )
    diag.info(Stage.LOAD,
              f"read {doc.page_count} page(s) with the {doc.backend} backend",
              backend=doc.backend)

    progress(Stage.LAYOUT, 0.35, "working out the page layout")
    laid_out = layout_mod.analyse_document(
        doc, diag, drop_furniture=options.drop_running_headers,
        drop_front_matter=options.drop_front_matter)
    if laid_out.furniture:
        shown = ", ".join(repr(t) for t in laid_out.furniture[:3])
        diag.info(Stage.LAYOUT,
                  f"removed {len(laid_out.furniture)} running header/footer line(s)",
                  examples=shown)

    if not any(page.has_text for page in laid_out.pages):
        raise UnsupportedDocumentError(
            f"'{pdf_path.name}' has no text layer",
            hint="It is a scanned document, so there is nothing to annotate. "
                 "OCR it first (for example with 'ocrmypdf in.pdf out.pdf') and "
                 "convert the OCR'd copy.",
            context={"pages": doc.page_count, "backend": doc.backend},
        )

    progress(Stage.STRUCTURE, 0.55, "building paragraphs and headings")
    structured = structure.build_blocks(
        laid_out.pages, laid_out.body_size,
        structure._Options(
            split_recitals=options.split_recitals,
            drop_front_matter=options.drop_front_matter,
            skip_title=doc.title,
            hyphenated_terms=laid_out.hyphenated_terms,
        ),
        diag,
    )

    # Re-converting over an existing source must not orphan notes that refer
    # to the old positional ids.  Aliases are derived from the *text* of the
    # old paragraphs, never from their position, so an unrelated renumbering
    # can never move a note to a different sentence.
    aliases: Dict[str, str] = {}
    previous_path = options.keep_aliases_from
    if previous_path and previous_path.exists():
        try:
            previous = sourcefmt.parse_source(previous_path)
            aliases.update(sourcefmt.carry_over_aliases(previous, structured.blocks))
            if aliases:
                diag.info(
                    Stage.STRUCTURE,
                    f"carried {len(aliases)} paragraph alias(es) over from the "
                    f"previous '{previous_path.name}'",
                    hint="Notes that referred to the old positional ids still "
                         "attach to the same sentences.",
                )
        except Exception as exc:
            diag.warn(Stage.STRUCTURE,
                      f"could not read the previous source '{previous_path.name}': {exc}")

    progress(Stage.STRUCTURE, 0.7, "writing the source text")
    source_text = sourcefmt.render_source(meta, structured.blocks, aliases)
    source = sourcefmt.parse_source_text(source_text, pdf_path.with_suffix(".txt"))
    source.aliases.update(aliases)

    stats = {
        "pages": doc.page_count,
        "paragraphs": len(structured.paragraphs),
        "headings": len(structured.headings),
        "body_font_size": laid_out.body_size,
        "backend": doc.backend,
        "furniture_removed": len(laid_out.furniture),
        "text_repairs": laid_out.repairs,
        "front_matter_dropped": len(structured.dropped_front_matter),
        "pdf_annotations": len(doc.all_annotations()),
    }
    return ConvertResult(source=source, document=doc, layout=laid_out, stats=stats)


#: A full stop, question mark or exclamation mark ends a sentence outright.
_SENTENCE_END_RE = __import__("re").compile(r"[.!?]['\"\u201d\u2019)\]]*$")
#: A comma, semicolon or colon could go either way.
_SOFT_END_RE = __import__("re").compile(r"[.,;:]['\"\u201d\u2019)\]]*$")


def _continues(previous: str, following: str) -> bool:
    """Is ``following`` the rest of the paragraph that ``previous`` began?

    Word documents break one paragraph across several ``w:p`` elements, so
    taking them at face value produces paragraphs that begin mid-sentence
    ("paragraph 1 (a) above, taking into account..."), which reads as broken.

    The test is deliberately three-way, because either signal alone gets a
    real document wrong:

    * the previous paragraph ends with ``.``/``!``/``?`` -- it is finished;
    * the next one starts lower-case (or with a digit) -- it continues;
    * the previous one stops with no punctuation at all, mid-word -- it
      continues.

    The middle rule is what keeps a preamble's recitals apart, since they end
    with a comma and the next one starts with a capital.
    """
    previous = previous.strip()
    following = following.strip()
    if not previous or not following:
        return False
    if _SENTENCE_END_RE.search(previous):
        return False
    if following[:1].islower() or following[:1].isdigit():
        return True
    return not _SOFT_END_RE.search(previous)


def convert_docx(docx_path: Path, meta: Dict[str, str],
                 options: ConvertOptions,
                 progress: ProgressFn = _noop,
                 diagnostics: Optional[Diagnostics] = None) -> ConvertResult:
    """Read a Word document's body as the source text.

    Word's own heading styles are used where they exist, and the structural
    keywords ("Article 7", "PART II") are used where they do not, which
    happens with documents exported by tools that flatten the styling.  The
    comments in the same file are the annotations, so one file supplies both
    halves of the build.
    """
    diag = diagnostics if diagnostics is not None else Diagnostics()
    if not docx_path.exists():
        raise InputError(f"no file at '{docx_path}'")

    progress(Stage.PARSE, 0.1, f"reading {docx_path.name}")
    try:
        paragraphs = docx.read_docx_paragraphs(docx_path)
    except ToolError:
        raise
    if not paragraphs:
        raise StructureError(
            f"'{docx_path.name}' contains no text",
            hint="The document body is empty. Is this the annotated text, or "
                 "only a list of comments?",
        )

    blocks: List[Block] = []
    order = 0
    positional = 0
    seen: Counter = Counter()
    aliases: Dict[str, str] = {}
    styled_headings = 0
    joined = 0

    def emit(kind: str, text: str, label: str, level: int) -> None:
        """Add one block, giving it a stable content-addressed key."""
        nonlocal order, positional
        order += 1
        key = structure.content_key(("heading:" if kind == "heading" else "") + text)
        seen[key] += 1
        if seen[key] > 1:
            key = f"{key}-{seen[key]}"
        if kind == "para":
            positional += 1
            aliases.setdefault(f"para-{positional}", key)
        blocks.append(Block(kind=kind, text=text, label=label, level=level,
                            key=key, order=order))

    # Word documents routinely break one paragraph across several ``w:p``
    # elements -- the numbered head is a ListParagraph and its text continues
    # in BodyText paragraphs.  Taking them at face value produces paragraphs
    # that begin mid-sentence ("paragraph 1 (a) above, taking into account..."),
    # which reads as broken.  A paragraph that does not end a sentence is
    # continued by the next one, exactly as with PDF lines.
    buffer: List[str] = []
    buffer_label = "\u00b6"

    def flush_buffer() -> None:
        nonlocal buffer, buffer_label
        if buffer:
            emit("para", " ".join(buffer), buffer_label, 0)
            buffer = []
            buffer_label = "\u00b6"

    for paragraph in paragraphs:
        text = paragraph.text.strip()
        if not text:
            # A blank line is only *evidence* of a paragraph break, and the
            # continuation test below already weighs that: a paragraph that
            # stops mid-sentence ("...their respective") is carried on by the
            # next one even when a blank line sits between them, which is
            # what these Word files quietly do.  Deciding on the blank alone
            # produced paragraphs beginning "obligations on human rights, the
            # right to health..." -- visibly broken.
            continue

        heading_level = paragraph.heading_level
        if not heading_level and structure.looks_like_structural_heading(text):
            heading_level = 2
        if heading_level:
            flush_buffer()
            if paragraph.heading_level:
                styled_headings += 1
            emit("heading", text, "", heading_level)
            continue

        label, remainder = structure.split_marker(text)
        body = remainder if remainder != text else text
        if label != "\u00b6":
            # A list marker is the start of a list item; it must not be
            # swallowed as the continuation of the paragraph before it, and
            # once the marker is stripped the remainder begins lower-case
            # ("by auction;") which would otherwise look like one.
            flush_buffer()
        if buffer and _continues(buffer[-1], body):
            joined += 1
            buffer[-1] = textutil.join_lines(buffer[-1], body)
        else:
            flush_buffer()
            buffer = [body]
            buffer_label = label
    flush_buffer()

    if not any(b.kind == "para" for b in blocks):
        raise StructureError(f"'{docx_path.name}' produced no paragraphs")

    if not styled_headings:
        diag.info(Stage.STRUCTURE,
                  "this document has no Word heading styles; headings were "
                  "inferred from the text instead")

    source = SourceDocument(path=docx_path, meta=dict(meta), blocks=blocks,
                            aliases=aliases)
    comments = 0
    try:
        comments = len(docx.read_docx_comments(docx_path))
    except ToolError:
        comments = 0
    stats = {
        "joined_continuations": joined,
        "paragraphs": len([b for b in blocks if b.kind == "para"]),
        "headings": len([b for b in blocks if b.kind == "heading"]),
        "styled_headings": styled_headings,
        "characters": sum(len(b.text) for b in blocks),
        "comments": comments,
        "source_type": "docx",
    }
    progress(Stage.STRUCTURE, 0.6, f"{stats['paragraphs']} paragraph(s)")
    return ConvertResult(source=source, stats=stats)


def write_source(result: ConvertResult, txt_path: Path,
                 write_json: bool = True,
                 progress: ProgressFn = _noop) -> Tuple[Path, Optional[Path]]:
    """Write the converted source text (and its structured sidecar)."""
    text = sourcefmt.render_source(result.source.meta, result.source.blocks,
                                   result.source.aliases)
    try:
        txt_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(txt_path, text)
    except OSError as exc:
        raise WriteError(f"could not write '{txt_path}'", cause=exc) from exc
    progress(Stage.WRITE, 0.9, f"wrote {txt_path.name}")

    json_path = None
    if write_json and result.layout is not None:
        json_path = txt_path.with_suffix(".layout.json")
        payload = {
            "source": str(result.source.path or ""),
            "pdf": str(result.document.path) if result.document else "",
            "backend": result.document.backend if result.document else "",
            "stats": result.stats,
            "document": result.document.as_dict() if result.document else None,
            "layout": result.layout.as_dict(),
            "blocks": [b.as_dict() for b in result.source.blocks],
        }
        try:
            _atomic_write(json_path, json.dumps(payload, indent=1, ensure_ascii=False))
        except OSError:
            json_path = None
    return txt_path, json_path


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temporary file so a failure cannot truncate the original."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Stage 5-7: source + annotations -> page
# ---------------------------------------------------------------------------

def load_all_annotations(path: Optional[Path], source: SourceDocument,
                         source_dir: Optional[Path] = None,
                         options: Optional[BuildOptions] = None,
                         progress: ProgressFn = _noop,
                         diagnostics: Optional[Diagnostics] = None
                         ) -> List[ann_mod.Annotation]:
    """Load annotations from any supported file type."""
    opts = options or BuildOptions()
    diag = diagnostics if diagnostics is not None else Diagnostics()
    items: List[ann_mod.Annotation] = []
    if path is None:
        return items

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        # Annotations that live inside the PDF itself.  A highlight carries no
        # text of its own, so the page layout is needed to recover what it
        # covers -- without it there would be nothing to anchor the note to.
        doc = backends.load_pdf(path, backend=opts.backend)
        laid_out = layout_mod.analyse_document(doc, diag)
        items.extend(ann_mod.annotations_from_pdf(
            doc, laid_out.pages, include_empty=opts.include_empty_annotations))
        diag.info(Stage.ANNOTATIONS,
                  f"read {len(items)} annotation(s) from the PDF's own markup")
    elif suffix == ".docx":
        items.extend(docx.annotations_from_docx(path))
        diag.info(Stage.ANNOTATIONS,
                  f"read {len(items)} Word comment(s) from {path.name}")
    else:
        items.extend(ann_mod.load_annotations(path))
        diag.info(Stage.ANNOTATIONS,
                  f"read {len(items)} annotation(s) from {path.name}")
    return items


def build_site(source_path: Path, annotation_path: Optional[Path],
               out_html: Path, out_notes: Path,
               options: Optional[BuildOptions] = None,
               progress: ProgressFn = _noop,
               diagnostics: Optional[Diagnostics] = None) -> BuildResult:
    """Generate the annotated page from a source text plus annotations."""
    opts = options or BuildOptions()
    diag = diagnostics if diagnostics is not None else Diagnostics()

    progress(Stage.INPUT, 0.05, f"reading {source_path.name}")
    source = sourcefmt.parse_source(source_path)

    progress(Stage.ANNOTATIONS, 0.2, "reading annotations")
    items = load_all_annotations(annotation_path, source, source_path.parent,
                                 opts, progress, diag)

    progress(Stage.MATCHING, 0.4, "attaching annotations to paragraphs")
    match = ann_mod.attach(items, source, diag)
    if match.duplicates:
        diag.info(Stage.MATCHING,
                  f"ignored {match.duplicates} duplicate annotation(s)")

    # Rebuild the structured document from the source so rendering has the
    # heading levels and labels the text file encodes.
    structured = StructuredDocument(
        blocks=list(source.blocks), body_size=0.0, aliases=dict(source.aliases))

    progress(Stage.RENDER, 0.6, "generating the page")
    notes_filename = out_notes.name
    page = render.render_page(source.meta, structured.blocks, match.notes,
                              notes_filename)

    progress(Stage.WRITE, 0.75, "writing output files")
    try:
        out_html.parent.mkdir(parents=True, exist_ok=True)
        out_notes.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(out_html, page.html)
        _atomic_write(out_notes, page.notes_js)
    except OSError as exc:
        raise WriteError(f"could not write the generated files: {exc}", cause=exc) from exc

    result = BuildResult(source=source, structured=structured, notes=match.notes,
                         match=match, page=page, html_path=out_html,
                         notes_path=out_notes)
    result.stats = {
        "paragraphs": len(source.paragraphs),
        "headings": len(structured.headings),
        "annotated": len(match.notes),
        "notes": match.note_count,
        "unplaced": len(match.unplaced),
        "authors": len({n.get("author", "") for entries in match.notes.values()
                        for n in entries if n.get("author")}),
    }

    if opts.validate:
        progress(Stage.VALIDATE, 0.9, "checking the generated page")
        result.validation = validate.validate_site(out_html, out_notes, source,
                                                   match.notes, diag)
    return result


# ---------------------------------------------------------------------------
# One-shot end-to-end run
# ---------------------------------------------------------------------------

@dataclass
class RunRequest:
    """Everything needed for a complete PDF (or txt) -> website run."""

    source: Path
    annotations: Optional[Path] = None
    #: False for "convert only": produce the source text and stop, instead of
    #: also generating a page.  Converting a PDF should never leave an HTML
    #: file behind that the user did not ask for.
    build_site: bool = True
    out_html: Optional[Path] = None
    out_notes: Optional[Path] = None
    out_source: Optional[Path] = None
    meta: Dict[str, str] = field(default_factory=dict)
    convert: ConvertOptions = field(default_factory=ConvertOptions)
    build: BuildOptions = field(default_factory=BuildOptions)


@dataclass
class RunReport:
    result: Optional[BuildResult] = None
    conversion: Optional[ConvertResult] = None
    diagnostics: Diagnostics = field(default_factory=Diagnostics)
    stages: List[Tuple[str, float]] = field(default_factory=list)
    source_path: Optional[Path] = None
    json_path: Optional[Path] = None
    diagnostics_path: Optional[Path] = None
    error: Optional[ToolError] = None

    @property
    def ok(self) -> bool:
        if self.error is not None:
            return False
        if self.result and self.result.validation and not self.result.validation.ok:
            return False
        return True


class StageTimer:
    def __init__(self) -> None:
        self.times: Dict[Stage, float] = {}
        self._start: Dict[Stage, float] = {}
        self.order: List[Stage] = []

    def start(self, stage: Stage) -> None:
        self._start[stage] = time.time()
        if stage not in self.order:
            self.order.append(stage)

    def stop(self, stage: Stage) -> None:
        if stage in self._start:
            self.times[stage] = self.times.get(stage, 0.0) + (time.time() - self._start.pop(stage))

    def as_list(self) -> List[Tuple[str, float]]:
        return [(s.label, round(self.times.get(s, 0.0), 2)) for s in self.order]


def run(request: RunRequest, progress: ProgressFn = _noop) -> RunReport:
    """Run the whole pipeline, converting a PDF first when one was given.

    Failures are returned rather than raised so the caller (TUI or CLI) can
    decide how to present them; the traceback for a genuine internal error is
    preserved in the report.
    """
    report = RunReport()
    diag = report.diagnostics
    timer = StageTimer()
    current = Stage.INPUT

    def timed(stage: Stage, fraction: Optional[float], message: str) -> None:
        nonlocal current
        if stage != current:
            timer.stop(current)
            timer.start(stage)
            current = stage
        progress(stage, fraction, message)

    try:
        timer.start(Stage.INPUT)
        source_path = request.source
        annotations = request.annotations
        if (request.source.suffix.lower() == ".docx" and annotations is None
                and docx.docx_comment_count(request.source)):
            # The comments in the same file are this instrument's annotations.
            annotations = request.source
        if request.source.suffix.lower() == ".docx":
            out_source = request.out_source or request.source.with_name(
                request.source.stem + ".txt")
            meta = dict(request.meta)
            if not meta.get("TITLE"):
                meta["TITLE"] = request.source.stem.replace("_", " ").title()
            meta.setdefault("HEADER", meta["TITLE"])
            conversion = convert_docx(request.source, meta, request.convert,
                                      progress=timed, diagnostics=diag)
            report.conversion = conversion
            report.source_path, report.json_path = write_source(
                conversion, out_source, request.convert.write_json, timed)
            source_path = out_source
        elif request.source.suffix.lower() == ".pdf":
            out_source = request.out_source or request.source.with_name(
                request.source.stem + ".txt")
            meta = dict(request.meta)
            if not meta.get("TITLE"):
                meta["TITLE"] = _guess_title(request.source)
            meta.setdefault("HEADER", meta["TITLE"])
            conversion = convert_pdf(
                request.source, meta, request.convert,
                progress=timed, diagnostics=diag)
            report.conversion = conversion
            report.source_path, report.json_path = write_source(
                conversion, out_source, request.convert.write_json, timed)
            source_path = out_source
        else:
            report.source_path = source_path

        if request.build_site:
            out_html = request.out_html or source_path.with_suffix(".html")
            out_notes = request.out_notes or out_html.with_name(out_html.stem + "-notes.js")
            result = build_site(source_path, annotations, out_html, out_notes,
                                options=request.build, progress=timed,
                                diagnostics=diag)
            report.result = result

        timer.stop(current)
        report.stages = timer.as_list()

        if request.build.write_diagnostics and request.build_site:
            report.diagnostics_path = _write_diagnostics(
                Path(report.result.html_path), diag, report)

    except ToolError as exc:
        timer.stop(current)
        report.stages = timer.as_list()
        report.error = exc
        diag.error(exc.stage, exc.message, hint=exc.hint)
    except Exception as exc:  # pragma: no cover - the safety net
        timer.stop(current)
        report.stages = timer.as_list()
        wrapped = InternalError.wrap(exc, "running the build")
        report.error = wrapped
        diag.error(Stage.INTERNAL, wrapped.message)
    return report


def _guess_title(pdf_path: Path) -> str:
    try:
        doc = backends.load_pdf(pdf_path, backend="poppler")
        if doc.title:
            return doc.title
    except Exception:
        pass
    return pdf_path.stem.replace("_", " ").replace("-", " ").title()


def _write_diagnostics(out_html: Path, diag: Diagnostics, report: RunReport) -> Optional[Path]:
    path = out_html.with_name(out_html.stem + ".build-report.json")
    payload = {
        "ok": report.ok,
        "stages": report.stages,
        "diagnostics": diag.as_list(),
        "validation": report.result.validation.as_dict() if (
            report.result and report.result.validation) else None,
        "stats": report.result.stats if report.result else {},
    }
    try:
        _atomic_write(path, json.dumps(payload, indent=1, ensure_ascii=False))
        return path
    except OSError:
        return None
