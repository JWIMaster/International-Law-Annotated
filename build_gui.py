#!/usr/bin/env python3
"""build_gui.py -- open Annotation Studio, the desktop interface.

    python3 build_gui.py                 # then choose a project folder
    python3 build_gui.py /path/to/site   # open a project straight away

Needs PySide6:  python3 -m pip install PySide6
The command-line tool (build_tool.py) does not.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ilatool.gui import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
