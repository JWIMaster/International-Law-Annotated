"""ilatool -- the International Law Annotated build tool.

A single library behind the ``build_tool.py`` command line / TUI, split into
small modules with one responsibility each:

    errors        error taxonomy shared by every stage
    pdfmodel      the structured PDF model (pages, blocks, lines, annotations)
    backends      PDF readers (PyMuPDF when available, poppler otherwise)
    textutil      text normalisation and repair (ligatures, hyphenation, ...)
    layout        page-level analysis: columns, reading order, running headers
    structure     block stream -> sections, headings and paragraphs
    sourcefmt     the plain-text ".txt source" format (read + write)
    annotations   the structured annotation model and every input format
    docx          Word comment extraction (the project's usual annotation source)
    render        HTML + notes.js generation
    allhtml       editing the card list in texts/all.html
    pipeline      the end-to-end build, as explicit stages
    validate      post-build checks on the generated site
    tui           the interactive terminal interface

The public entry point is :func:`ilatool.cli.main`.
"""

__version__ = "2.0.0"

__all__ = ["__version__"]
