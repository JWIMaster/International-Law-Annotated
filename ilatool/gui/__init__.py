"""The Qt desktop application.

Importing this package does not import PySide6: ``ilatool.gui.app`` does, and
only when it is asked for.  That keeps the command-line tool usable on a
machine with no Qt.
"""

from __future__ import annotations

__all__ = ["main"]


def main(argv=None) -> int:
    from .app import main as run
    return run(argv)
