"""Error taxonomy for the whole tool.

The point of this module is that every failure the tool can produce carries
enough structure for the TUI (and the headless CLI) to:

  * say which *stage* failed, in the user's language,
  * distinguish "you gave me something I can't use" from "my own bug",
  * keep the raw technical detail for a diagnostics file, and
  * decide whether retrying makes sense.

Nothing here prints anything; formatting lives in the callers.
"""

from __future__ import annotations

import enum
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class Stage(str, enum.Enum):
    """The pipeline stage a failure belongs to."""

    INPUT = "input"
    LOAD = "load"
    PARSE = "parse"
    LAYOUT = "layout"
    STRUCTURE = "structure"
    ANNOTATIONS = "annotations"
    MATCHING = "matching"
    RENDER = "render"
    WRITE = "write"
    VALIDATE = "validate"
    INTERNAL = "internal"

    @property
    def label(self) -> str:
        return {
            Stage.INPUT: "Reading your input",
            Stage.LOAD: "Opening the source file",
            Stage.PARSE: "Extracting content",
            Stage.LAYOUT: "Working out the page layout",
            Stage.STRUCTURE: "Building paragraphs and headings",
            Stage.ANNOTATIONS: "Reading annotations",
            Stage.MATCHING: "Attaching annotations to the text",
            Stage.RENDER: "Generating the page",
            Stage.WRITE: "Writing the output files",
            Stage.VALIDATE: "Checking the generated site",
            Stage.INTERNAL: "Internal error",
        }[self]


class ToolError(Exception):
    """Base class for everything this tool raises on purpose.

    ``retryable`` is a hint for the TUI: when True it offers "try again",
    when False it offers "go back" (because retrying the same thing with the
    same input will fail the same way).
    """

    stage: Stage = Stage.INTERNAL
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        hint: str = "",
        detail: str = "",
        context: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.detail = detail
        self.context: Dict[str, Any] = dict(context or {})
        if cause is not None:
            self.__cause__ = cause
            self.context.setdefault("cause", f"{type(cause).__name__}: {cause}")

    # -- presentation helpers -------------------------------------------------

    @property
    def title(self) -> str:
        return self.stage.label

    def diagnostics(self) -> str:
        """Multi-line technical detail, for a log file (never the main UI)."""
        bits: List[str] = [f"{type(self).__name__}: {self.message}"]
        if self.hint:
            bits.append(f"hint: {self.hint}")
        if self.detail:
            bits.append("detail:\n" + self.detail)
        for key, value in sorted(self.context.items()):
            bits.append(f"{key}: {value}")
        if self.__cause__ is not None:
            bits.append("cause:\n" + "".join(
                traceback.format_exception(type(self.__cause__), self.__cause__, self.__cause__.__traceback__)
            ))
        return "\n".join(bits)


class InputError(ToolError):
    """The user pointed at something that isn't usable (missing file, wrong
    kind of file, unreadable directory...)."""

    stage = Stage.INPUT
    retryable = True


class UnsupportedDocumentError(ToolError):
    """The file is a real PDF but needs something we can't do (encrypted,
    scanned with no text layer, ...). Distinct from a parse failure because
    the file isn't broken -- our approach is what's insufficient."""

    stage = Stage.PARSE
    retryable = False


class PdfParseError(ToolError):
    """The PDF could not be read/extracted at all."""

    stage = Stage.PARSE
    retryable = False


class LayoutError(ToolError):
    """Layout analysis could not produce a usable document."""

    stage = Stage.LAYOUT
    retryable = False


class StructureError(ToolError):
    """Paragraph/heading inference produced nothing usable."""

    stage = Stage.STRUCTURE
    retryable = False


class AnnotationError(ToolError):
    """An annotation file is malformed or unusable."""

    stage = Stage.ANNOTATIONS
    retryable = True


class RenderError(ToolError):
    """Generating the page HTML/JS failed."""

    stage = Stage.RENDER


class WriteError(ToolError):
    stage = Stage.WRITE
    retryable = True


class ValidationError(ToolError):
    """The generated site failed the post-build checks."""

    stage = Stage.VALIDATE


class InternalError(ToolError):
    """A bug in this tool. Always reported with a traceback, never hidden."""

    stage = Stage.INTERNAL

    @classmethod
    def wrap(cls, exc: BaseException, where: str) -> "InternalError":
        return cls(
            f"unexpected {type(exc).__name__} while {where}",
            hint="This is a bug in the tool rather than a problem with your files. "
                 "The details below are worth reporting.",
            detail="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            cause=exc,
        )


class BuildCancelled(ToolError):
    """The user asked to stop a running build.

    Raised from the progress callback so the pipeline unwinds through its
    normal error path -- no half-written output, no orphaned temporary file.
    """

    stage = Stage.INTERNAL
    retryable = True

    def __init__(self, message: str = "cancelled") -> None:
        super().__init__(message, hint="Nothing was written.")


@dataclass
class Diagnostic:
    """A non-fatal observation collected while building.

    Warnings are first-class: the tool must never silently drop a note or
    mangle a paragraph. Everything the user should know ends up here.
    """

    stage: Stage
    severity: str  # 'info' | 'warning' | 'error'
    message: str
    hint: str = ""
    context: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage.value,
            "severity": self.severity,
            "message": self.message,
            "hint": self.hint,
            "context": self.context,
        }


class Diagnostics:
    """An ordered, de-duplicated collection of :class:`Diagnostic`."""

    def __init__(self) -> None:
        self._items: List[Diagnostic] = []
        self._seen: set = set()

    def add(
        self,
        stage: Stage,
        severity: str,
        message: str,
        *,
        hint: str = "",
        **context: Any,
    ) -> None:
        key = (stage, severity, message)
        if key in self._seen:
            return
        self._seen.add(key)
        self._items.append(Diagnostic(stage, severity, message, hint, dict(context)))

    def info(self, stage: Stage, message: str, **kw: Any) -> None:
        self.add(stage, "info", message, **kw)

    def warn(self, stage: Stage, message: str, **kw: Any) -> None:
        self.add(stage, "warning", message, **kw)

    def error(self, stage: Stage, message: str, **kw: Any) -> None:
        self.add(stage, "error", message, **kw)

    def __iter__(self):
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    def of(self, severity: str) -> List[Diagnostic]:
        return [d for d in self._items if d.severity == severity]

    @property
    def warnings(self) -> List[Diagnostic]:
        return self.of("warning")

    @property
    def errors(self) -> List[Diagnostic]:
        return self.of("error")

    def as_list(self) -> List[Dict[str, Any]]:
        return [d.as_dict() for d in self._items]

    def summary(self) -> str:
        n_warn = len(self.warnings)
        n_err = len(self.errors)
        if not n_warn and not n_err:
            return "no problems detected"
        parts = []
        if n_warn:
            parts.append(f"{n_warn} warning{'s' if n_warn != 1 else ''}")
        if n_err:
            parts.append(f"{n_err} error{'s' if n_err != 1 else ''}")
        return ", ".join(parts)
