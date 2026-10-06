#!/usr/bin/env python3
"""build_tool.py -- build "International Law Annotated" pages from annotated PDFs.

    python3 build_tool.py                 # interactive interface
    python3 build_tool.py --help          # every command

The implementation lives in the ``ilatool`` package next to this file; this
script is only the entry point, kept at its historical name so existing
habits, notes and scripts keep working.

Typical non-interactive use::

    python3 build_tool.py build materials/CISG/cisg.txt \\
        -a texts/cisg-notes.js -o texts/cisg.html

    python3 build_tool.py convert some.pdf            # PDF -> source text
    python3 build_tool.py inspect texts/cisg.txt      # what was parsed
    python3 build_tool.py doctor                      # environment check
"""

import sys
from pathlib import Path

# Make the sibling package importable no matter where the script is run from.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ilatool.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
