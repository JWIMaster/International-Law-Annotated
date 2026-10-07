"""Annotation Studio -- the desktop application.

    python3 build_tool.py gui [project-folder]
    python3 build_gui.py [project-folder]

PySide6 is imported only here, so the command-line tool keeps working on a
machine where Qt is not installed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional


def missing_qt_message() -> str:
    return (
        "Annotation Studio needs PySide6, which is not installed.\n\n"
        "    python3 -m pip install PySide6\n\n"
        "The command-line tool works without it:\n"
        "    python3 build_tool.py --help"
    )


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print(missing_qt_message(), file=sys.stderr)
        return 3

    from .mainwindow import MainWindow
    from .services import ProjectSession

    app = QApplication.instance() or QApplication(sys.argv[:1] + argv)
    app.setApplicationName("Annotation Studio")
    app.setOrganizationName("InternationalLawAnnotated")
    app.setApplicationDisplayName("Annotation Studio")

    folder = None
    for arg in argv:
        if not arg.startswith("-"):
            candidate = Path(arg).expanduser()
            if candidate.exists():
                folder = candidate if candidate.is_dir() else candidate.parent
                break

    window = MainWindow(ProjectSession(), start=folder)
    window.show()
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
