"""Writing files so that a failure cannot destroy what was there.

Everything that rewrites a project file goes through here.  A half-written
``notes.js`` is not a recoverable state -- the page would load nothing -- so
the write lands in a temporary file in the same directory and is moved into
place, which is atomic on every platform this tool runs on.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .errors import InputError


def atomic_write(path: Path, text: str, backup: bool = False) -> Optional[Path]:
    """Replace ``path`` with ``text``, all or nothing.

    Returns the path of the backup that was made, if one was asked for.
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InputError(f"could not create '{path.parent}'", cause=exc) from exc
    made: Optional[Path] = None
    if backup and path.exists():
        made = path.with_name(path.name + ".bak")
        try:
            shutil.copy2(path, made)
        except OSError as exc:
            raise InputError(f"could not back up '{path.name}'", cause=exc) from exc
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise InputError(f"could not write '{path.name}'", cause=exc) from exc
    return made


def fingerprint(path: Path) -> str:
    """A value that changes whenever the file's contents change.

    Used to notice that somebody edited a file behind the application's back,
    which matters more than the timestamp: a save that restores the previous
    content is not a change worth warning about.
    """
    path = Path(path)
    if not path.exists():
        return ""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(65536), b""):
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


@dataclass
class FileState:
    """What a file looked like when we last read or wrote it."""

    path: Path
    exists: bool = False
    digest: str = ""
    mtime: float = 0.0
    size: int = 0

    @classmethod
    def read(cls, path: Path) -> "FileState":
        path = Path(path)
        if not path.exists():
            return cls(path=path, exists=False)
        try:
            stat = path.stat()
        except OSError:
            return cls(path=path, exists=False)
        return cls(path=path, exists=True, digest=fingerprint(path),
                   mtime=stat.st_mtime, size=stat.st_size)

    def matches(self, path: Optional[Path] = None) -> bool:
        """Is the file still exactly as we left it?"""
        other = FileState.read(path or self.path)
        if other.exists != self.exists:
            return False
        if not other.exists:
            return True
        return other.digest == self.digest

    def describe(self) -> str:
        return f"{self.path.name} ({self.size} bytes)" if self.exists else \
               f"{self.path.name} (missing)"
