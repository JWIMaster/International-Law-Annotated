"""Command line entry point.

Two ways to use the tool:

* ``build`` / ``convert`` / ``inspect`` / ``doctor`` -- non-interactive, for
  scripts, CI and reproducing a build.  These are the commands the test suite
  exercises.
* no arguments, or ``menu`` -- the interactive interface.

Run ``build_tool.py --help`` for the full list.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import __version__, backends, pipeline, sourcefmt
from .errors import Stage, ToolError
from .tui import term as T
from .tui.app import run_tui


def _progress_printer(quiet: bool):
    def show(stage: Stage, fraction: Optional[float], message: str) -> None:
        if quiet:
            return
        width, _ = T.terminal_size()
        pct = "" if fraction is None else f"{fraction:4.0%}"
        line = f"  {stage.value:<11} {pct:>5}  {message}"
        end = "\n" if fraction is None or fraction >= 0.995 else "\r"
        sys.stdout.write(T.fit(line, width - 1) + ("" if end == "\r" else "\n"))
        sys.stdout.flush()
    return show


def _report(report: pipeline.RunReport, verbose: bool = False) -> int:
    diag = report.diagnostics
    if report.result and report.result.validation:
        print()
        print(T.bold("Checks"))
        for check in report.result.validation.checks:
            mark = {"ok": "✔", "warn": "!", "fail": "✖"}.get(check.status, "·")
            colour = {"ok": T.ok, "warn": T.warn, "fail": T.error}.get(check.status, T.dim)
            print(colour(f"  {mark} {check.name}") +
                  (T.dim(f"  {check.detail}") if check.detail else ""))
    notices = list(diag)
    if notices and (verbose or any(d.severity != "info" for d in notices)):
        print()
        print(T.bold("Notices"))
        for item in notices:
            colour = {"info": T.dim, "warning": T.warn, "error": T.error}[item.severity]
            print(colour(f"  [{item.severity}] {item.message}"))
            if item.hint and verbose:
                print(T.dim(f"      {item.hint}"))
    if report.result and report.result.match.unplaced and (verbose or True):
        print()
        print(T.warn(f"{len(report.result.match.unplaced)} annotation(s) could not be attached:"))
        for ann, why in report.result.match.unplaced[:15]:
            label = ann.para or (ann.quote[:52] + "…" if ann.quote else "(no target)")
            print(T.warn(f"  {label}"))
            print(T.dim(f"      {why}"))
        if len(report.result.match.unplaced) > 15:
            print(T.dim(f"  … and {len(report.result.match.unplaced) - 15} more"))
    if report.error:
        print()
        print(T.error(f"{report.error.title} failed"))
        print("  " + report.error.message)
        if report.error.hint:
            print(T.dim("  " + report.error.hint))
        if verbose and report.error.detail:
            print(T.dim(report.error.detail))
    if report.stages:
        print()
        print(T.dim("  " + " · ".join(f"{label} {seconds:.2f}s" for label, seconds in report.stages)))
    if report.diagnostics_path:
        print(T.dim(f"  report: {report.diagnostics_path}"))
    return 0 if report.ok else 1


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_build(args: argparse.Namespace) -> int:
    source = Path(args.source).expanduser()
    if not source.exists():
        print(T.error(f"No file at {source}"), file=sys.stderr)
        return 2
    meta: dict = {}
    if args.title:
        meta["TITLE"] = args.title
        meta.setdefault("HEADER", args.title)
    if args.desc:
        meta["DESC"] = args.desc
    if args.header:
        meta["HEADER"] = args.header
    if args.home:
        meta["HOME"] = args.home

    request = pipeline.RunRequest(
        source=source,
        annotations=Path(args.annotations).expanduser() if args.annotations else None,
        out_html=Path(args.out).expanduser() if args.out else None,
        out_source=Path(args.out_source).expanduser() if args.out_source else None,
        meta=meta,
        convert=pipeline.ConvertOptions(
            backend=args.backend,
            drop_front_matter=not args.keep_front_matter,
            split_recitals=not args.no_recital_split,
            drop_running_headers=not args.keep_headers,
            write_json=not args.no_sidecar,
            force=args.force,
        ),
        build=pipeline.BuildOptions(
            backend=args.backend,
            migrate_from=Path(args.migrate_from).expanduser() if args.migrate_from else None,
            validate=not args.no_validate,
            write_diagnostics=not args.no_report,
        ),
    )
    report = pipeline.run(request, _progress_printer(args.quiet))
    print()
    if report.error:
        return _report(report, args.verbose)
    result = report.result
    assert result is not None
    print(T.ok(f"Wrote {result.html_path}"))
    print(T.ok(f"Wrote {result.notes_path}"))
    if report.source_path:
        print(T.ok(f"Wrote {report.source_path}"))
    stats = result.stats
    print(f"  {stats['paragraphs']} paragraph(s), {stats['headings']} heading(s), "
          f"{stats['annotated']} annotated, {stats['notes']} note(s)")
    return _report(report, args.verbose)


def cmd_convert(args: argparse.Namespace) -> int:
    source = Path(args.pdf).expanduser()
    if not source.exists():
        print(T.error(f"No file at {source}"), file=sys.stderr)
        return 2
    out = Path(args.out).expanduser() if args.out else source.with_name(source.stem + ".txt")
    meta = {"TITLE": args.title or source.stem.replace("_", " ").title()}
    meta["HEADER"] = args.header or meta["TITLE"]
    request = pipeline.RunRequest(
        source=source, out_source=out, meta=meta, build_site=False,
        convert=pipeline.ConvertOptions(
            backend=args.backend,
            keep_aliases_from=out if (args.carry_aliases and out.exists()) else None,
            write_json=not args.no_sidecar,
            force=args.force,
        ),
        build=pipeline.BuildOptions(validate=False, write_diagnostics=False),
    )
    report = pipeline.run(request, _progress_printer(args.quiet))
    print()
    if report.error:
        return _report(report, args.verbose)
    stats = report.conversion.stats
    print(T.ok(f"Wrote {out}"))
    print(f"  {stats['pages']} page(s), {stats['paragraphs']} paragraph(s), "
          f"{stats['headings']} heading(s)")
    if stats["text_repairs"]:
        print(T.warn(f"  text repairs: {stats['text_repairs']}"))
    for diag in report.diagnostics:
        colour = {"info": T.dim, "warning": T.warn, "error": T.error}[diag.severity]
        print(colour(f"  [{diag.severity}] {diag.message}"))
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    path = Path(args.source).expanduser()
    if not path.exists():
        print(T.error(f"No file at {path}"), file=sys.stderr)
        return 2
    if path.suffix.lower() == ".pdf":
        details = backends.probe_pdf(path)
        print(json.dumps(details, indent=2, default=str))
        return 0
    source = sourcefmt.parse_source(path)
    if args.json:
        print(json.dumps({
            "meta": source.meta,
            "aliases": source.aliases,
            "blocks": [b.as_dict() for b in source.blocks],
        }, indent=1, ensure_ascii=False))
        return 0
    print(T.bold(f"{path.name}: {len(source.paragraphs)} paragraph(s), "
                 f"{len(source.blocks) - len(source.paragraphs)} heading(s)"))
    for block in source.blocks[:args.limit]:
        kind = "##" if block.kind == "heading" else block.label
        print(f"  {block.id:<16} {kind:<4} {block.text[:90]}")
    if len(source.blocks) > args.limit:
        print(T.dim(f"  … and {len(source.blocks) - args.limit} more"))
    return 0


def cmd_annotations(args: argparse.Namespace) -> int:
    from . import annotations as ann_mod, docx as docx_mod
    path = Path(args.path).expanduser()
    if not path.exists():
        print(T.error(f"No file at {path}"), file=sys.stderr)
        return 2
    try:
        if path.suffix.lower() == ".docx":
            items = docx_mod.annotations_from_docx(path)
        elif path.suffix.lower() == ".pdf":
            items = ann_mod.annotations_from_pdf(backends.load_pdf(path))
        else:
            items = ann_mod.load_annotations(path)
    except ToolError as exc:
        print(T.error(exc.message))
        return 1
    print(T.bold(f"{len(items)} annotation(s) in {path.name}"))
    for ann in items:
        anchor = ann.para or (f"page {ann.page}" if ann.page else "excerpt")
        print(f"  {(ann.author or 'unknown')[:20]:<20} {anchor:<12} "
              f"{(ann.text or ann.quote)[:70]}")
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from .gui import main as gui_main
    argv = [args.project] if getattr(args, "project", None) else []
    return gui_main(argv)


def cmd_doctor(args: argparse.Namespace) -> int:
    print(T.bold(f"ilatool {__version__}"))
    print()
    print(T.bold("PDF backends"))
    for name, info in backends.backend_status().items():
        mark = "✔" if info["available"] else "✖"
        colour = T.ok if info["available"] else T.warn
        print(colour(f"  {mark} {name:10s} {info['detail']}"))
    if not backends.backend_status()["pymupdf"]["available"]:
        print(T.dim("      pip install pymupdf   # exact geometry, native annotations"))
    print()
    print(T.bold("Python"))
    print(f"  {sys.version.split()[0]} at {sys.executable}")
    print()
    print(T.bold("Project"))
    from .tui.app import load_state
    state = load_state(Path(args.state).expanduser())
    for label, value in (("source", state.source), ("annotations", state.annotations),
                         ("output", state.out_html)):
        print(f"  {label:<12} {value or '(not set)'}")
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_tool.py",
        description="Turn an annotated PDF, Word document or text into an "
                    "annotated website.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run with no arguments for the interactive interface.",
    )
    parser.add_argument("--version", action="version", version=f"ilatool {__version__}")
    sub = parser.add_subparsers(dest="command")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--backend", choices=["auto", "pymupdf", "poppler"], default="auto",
                        help="which PDF reader to use (default: the best available)")
    common.add_argument("-q", "--quiet", action="store_true", help="no progress output")
    common.add_argument("-v", "--verbose", action="store_true",
                        help="show hints and technical detail")

    p = sub.add_parser("build", parents=[common],
                       help="PDF, Word document or source text -> website")
    p.add_argument("source", help="a PDF, a Word .docx, or an existing source .txt")
    p.add_argument("-a", "--annotations", help="annotations file (.txt/.json/.csv/.docx/.pdf/.js)")
    p.add_argument("-o", "--out", help="output HTML path")
    p.add_argument("--out-source", help="where to write the converted source .txt")
    p.add_argument("--title")
    p.add_argument("--header")
    p.add_argument("--desc")
    p.add_argument("--home", default="../index.html")
    p.add_argument("--keep-front-matter", action="store_true",
                   help="do not skip a leading table of contents")
    p.add_argument("--keep-headers", action="store_true",
                   help="keep running headers and footers")
    p.add_argument("--no-recital-split", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="skip the pre-flight that rejects PDFs with no text layer")
    p.add_argument("--no-sidecar", action="store_true",
                   help="do not write the structured layout JSON")
    p.add_argument("--no-report", action="store_true",
                   help="do not write the build report JSON")
    p.add_argument("--migrate-from", metavar="PAGE",
                   help="an existing published page whose paragraph ids should "
                        "be carried over, so notes keyed to them still attach")
    p.add_argument("--no-validate", action="store_true")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("convert", parents=[common], help="PDF -> source text")
    p.add_argument("pdf")
    p.add_argument("-o", "--out")
    p.add_argument("--title")
    p.add_argument("--header")
    p.add_argument("--carry-aliases", action="store_true", default=True,
                   help="keep old paragraph ids resolvable (default: on)")
    p.add_argument("--no-carry-aliases", dest="carry_aliases", action="store_false")
    p.add_argument("--force", action="store_true",
                   help="skip the pre-flight that rejects PDFs with no text layer")
    p.add_argument("--no-sidecar", action="store_true")
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("inspect", help="show what a PDF or source text contains")
    p.add_argument("source")
    p.add_argument("--json", action="store_true")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("annotations", help="show what an annotations file contains")
    p.add_argument("path")
    p.set_defaults(func=cmd_annotations)

    p = sub.add_parser("doctor", help="report on the environment")
    p.add_argument("--state", default=".annotate_state.json")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("gui", help="the desktop application (needs PySide6)")
    p.add_argument("project", nargs="?", default=None,
                   help="project folder to open (the one with index.html)")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("menu", help="the interactive interface (default)")
    p.add_argument("--state", default=".annotate_state.json")
    p.set_defaults(func=lambda a: run_tui(Path.cwd(), Path(a.state).expanduser()))

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not getattr(args, "command", None):
        script_dir = Path(__file__).resolve().parent.parent
        return run_tui(script_dir, script_dir / ".annotate_state.json")
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print()
        print(T.dim("Interrupted."))
        return 130
    except ToolError as exc:
        print(T.error(f"{exc.title}: {exc.message}"))
        if exc.hint:
            print(T.dim(exc.hint))
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
