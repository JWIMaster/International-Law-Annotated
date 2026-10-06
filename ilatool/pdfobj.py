"""A deliberately small, dependency-free PDF *object* reader.

This is not a text extractor -- poppler and PyMuPDF do that far better than
anything we could write here.  What this module provides is the structural
layer that the command-line poppler tools cannot give us:

  * the real page tree, with each page's ``/MediaBox`` and ``/Rotate``,
  * the raw ``/Annots`` dictionaries (author, contents, rect, quad points,
    colour, dates, subtype),

and it does so with nothing but ``zlib`` and the standard library.  That
matters because it means *annotations still work* on a machine where
PyMuPDF is not installed, and because it lets us convert annotation
rectangles out of unrotated page space ourselves.

Scope is intentionally narrow: enough of the format to walk objects and
read dictionaries/arrays/strings/streams.  Anything we do not understand
is returned as an opaque token rather than raising, so a single odd object
never takes down the whole file.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union


class PdfObjectError(Exception):
    """Raised only for whole-file problems (not per-object)."""


# ---------------------------------------------------------------------------
# Object model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Ref:
    num: int
    gen: int = 0

    def __str__(self) -> str:
        return f"{self.num} {self.gen} R"


class Name(str):
    """A PDF name (``/Foo``).  A ``str`` subclass whose repr is unambiguous."""

    def __repr__(self) -> str:  # pragma: no cover
        return f"/{str(self)}"


@dataclass
class Stream:
    dict: Dict[str, Any]
    raw: bytes
    doc: "PdfFile" = field(repr=False, default=None)  # type: ignore[assignment]
    _decoded: Optional[bytes] = None

    def get_data(self) -> bytes:
        if self._decoded is None:
            self._decoded = decode_stream(self.raw, self.dict, self.doc)
        return self._decoded


# ---------------------------------------------------------------------------
# Lexer / parser
# ---------------------------------------------------------------------------

_WHITESPACE = b"\x00\t\n\x0c\r "
_DELIMITERS = b"()<>[]{}/%"
_NUMBER_RE = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)")
_KEYWORD_RE = re.compile(rb"[A-Za-z*'\"]+")

_ESCAPES = {
    b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f",
    b"(": b"(", b")": b")", b"\\": b"\\",
}


class _Parser:
    """Parses PDF objects out of a byte buffer."""

    def __init__(self, data: bytes, doc: "PdfFile", pos: int = 0) -> None:
        self.data = data
        self.doc = doc
        self.pos = pos

    # -- lexing helpers -------------------------------------------------------

    def skip_ws(self) -> None:
        data, n = self.data, len(self.data)
        pos = self.pos
        while pos < n:
            ch = data[pos]
            if ch in _WHITESPACE:
                pos += 1
            elif ch == 0x25:  # '%' comment
                while pos < n and data[pos] not in b"\r\n":
                    pos += 1
            else:
                break
        self.pos = pos

    def peek(self) -> int:
        self.skip_ws()
        return self.data[self.pos] if self.pos < len(self.data) else -1

    def read_token(self) -> bytes:
        self.skip_ws()
        data, n = self.data, len(self.data)
        start = self.pos
        pos = start
        while pos < n and data[pos] not in _WHITESPACE and data[pos] not in _DELIMITERS:
            pos += 1
        if pos == start and pos < n:
            # A delimiter on its own (e.g. '>' of a dictionary close).
            pos += 1
            if data[start:start + 1] == b">" and pos < n and data[pos:pos + 1] == b">":
                pos += 1
            elif data[start:start + 1] == b"<" and pos < n and data[pos:pos + 1] == b"<":
                pos += 1
        self.pos = pos
        return data[start:pos]

    # -- object parsing -------------------------------------------------------

    def parse_object(self, depth: int = 0) -> Any:
        if depth > 60:
            return None
        self.skip_ws()
        data = self.data
        if self.pos >= len(data):
            return None
        ch = data[self.pos]

        if ch == 0x2F:  # '/'
            return Name(self._parse_name())
        if ch == 0x28:  # '('
            return self._parse_literal_string()
        if ch == 0x3C:  # '<'
            if data[self.pos:self.pos + 2] == b"<<":
                return self._parse_dict()
            return self._parse_hex_string()
        if ch == 0x5B:  # '['
            return self._parse_array()

        token = self.read_token()
        if token == b"true":
            return True
        if token == b"false":
            return False
        if token == b"null":
            return None

        if _NUMBER_RE.fullmatch(token):
            # Could be the start of an indirect reference: "<num> <gen> R".
            save = self.pos
            self.skip_ws()
            tok2 = self.read_token()
            if _NUMBER_RE.fullmatch(tok2):
                save2 = self.pos
                self.skip_ws()
                if self.data[self.pos:self.pos + 1] == b"R":
                    self.pos += 1
                    return Ref(int(token), int(tok2))
                self.pos = save2
            self.pos = save
            text = token.decode("latin-1")
            try:
                return int(text)
            except ValueError:
                return float(text)

        # A bare keyword: "obj", "endobj", "stream", ...
        return _Keyword(token.decode("latin-1", "replace"))

    def _parse_name(self) -> str:
        self.pos += 1  # skip '/'
        data, n = self.data, len(self.data)
        out = bytearray()
        pos = self.pos
        while pos < n and data[pos] not in _WHITESPACE and data[pos] not in _DELIMITERS:
            ch = data[pos]
            if ch == 0x23 and pos + 2 < n:  # '#xx' escape
                try:
                    out.append(int(data[pos + 1:pos + 3], 16))
                    pos += 3
                    continue
                except ValueError:
                    pass
            out.append(ch)
            pos += 1
        self.pos = pos
        return out.decode("latin-1")

    def _parse_literal_string(self) -> bytes:
        data, n = self.data, len(self.data)
        pos = self.pos + 1
        out = bytearray()
        depth = 1
        while pos < n:
            ch = data[pos]
            if ch == 0x5C:  # backslash
                nxt = data[pos + 1:pos + 2]
                if nxt in _ESCAPES:
                    out += _ESCAPES[nxt]
                    pos += 2
                    continue
                if nxt.isdigit():
                    octal = data[pos + 1:pos + 4]
                    m = re.match(rb"[0-7]{1,3}", octal)
                    if m:
                        out.append(int(m.group(0), 8) & 0xFF)
                        pos += 1 + len(m.group(0))
                        continue
                if nxt in (b"\n", b"\r"):
                    pos += 2
                    if nxt == b"\r" and data[pos:pos + 1] == b"\n":
                        pos += 1
                    continue
                out += nxt
                pos += 2
                continue
            if ch == 0x28:
                depth += 1
            elif ch == 0x29:
                depth -= 1
                if depth == 0:
                    pos += 1
                    break
            out.append(ch)
            pos += 1
        self.pos = pos
        return bytes(out)

    def _parse_hex_string(self) -> bytes:
        data, n = self.data, len(self.data)
        end = data.find(b">", self.pos)
        if end < 0:
            end = n
        hex_text = re.sub(rb"[^0-9A-Fa-f]", b"", data[self.pos + 1:end])
        if len(hex_text) % 2:
            hex_text += b"0"
        self.pos = end + 1
        try:
            return bytes.fromhex(hex_text.decode("ascii"))
        except ValueError:
            return b""

    def _parse_array(self) -> List[Any]:
        self.pos += 1
        out: List[Any] = []
        while True:
            self.skip_ws()
            if self.pos >= len(self.data):
                break
            if self.data[self.pos] == 0x5D:  # ']'
                self.pos += 1
                break
            before = self.pos
            out.append(self.parse_object(depth=1))
            if self.pos == before:
                self.pos += 1
        return out

    def _parse_dict(self) -> Dict[str, Any]:
        self.pos += 2
        out: Dict[str, Any] = {}
        while True:
            self.skip_ws()
            if self.pos >= len(self.data):
                break
            if self.data[self.pos:self.pos + 2] == b">>":
                self.pos += 2
                break
            if self.data[self.pos] != 0x2F:
                before = self.pos
                self.parse_object(depth=1)
                if self.pos == before:
                    self.pos += 1
                continue
            key = self._parse_name()
            value = self.parse_object(depth=1)
            out[key] = value
        return out


@dataclass
class _Keyword:
    name: str

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{self.name}>"


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def decode_stream(raw: bytes, stream_dict: Dict[str, Any], doc: Optional["PdfFile"] = None) -> bytes:
    """Apply the stream's filters.  Unknown filters return the raw bytes."""
    filters = _as_list(stream_dict.get("Filter"))
    parms = _as_list(stream_dict.get("DecodeParms")) or [None] * len(filters)
    data = raw
    for i, filt in enumerate(filters):
        name = str(filt) if isinstance(filt, Name) else ""
        parm = parms[i] if i < len(parms) else None
        if not isinstance(parm, dict):
            parm = {}
        try:
            if name in ("FlateDecode", "Fl"):
                data = _inflate(data, parm)
            elif name in ("ASCIIHexDecode", "AHx"):
                data = bytes.fromhex(re.sub(rb"[^0-9A-Fa-f]", b"", data.split(b">")[0]).decode("ascii") + "00")
            elif name in ("ASCII85Decode", "A85"):
                data = _ascii85(data)
            elif name in ("LZWDecode", "LZW"):
                data = _lzw(data, parm)
            else:
                # DCTDecode/JPXDecode/CCITTFaxDecode are image codecs; the
                # bytes are already what a caller would want.
                break
        except Exception:
            break
    return data


def _inflate(data: bytes, parm: Dict[str, Any]) -> bytes:
    try:
        out = zlib.decompress(data)
    except zlib.error:
        try:
            out = zlib.decompressobj().decompress(data)
        except zlib.error:
            return data
    predictor = _int(parm.get("Predictor"), 1)
    if predictor and predictor > 1:
        out = _unpredict(out, predictor,
                         _int(parm.get("Colors"), 1),
                         _int(parm.get("BitsPerComponent"), 8),
                         _int(parm.get("Columns"), 1))
    return out


def _unpredict(data: bytes, predictor: int, colors: int, bpc: int, columns: int) -> bytes:
    """Undo PNG/TIFF predictors (only the PNG ones appear in practice)."""
    if predictor < 10:
        return data  # TIFF predictor 2 is rare; leave as-is rather than corrupt
    bpp = max(1, (colors * bpc + 7) // 8)
    row_len = max(1, (columns * colors * bpc + 7) // 8)
    out = bytearray()
    prev = bytearray(row_len)
    pos = 0
    while pos + 1 + row_len <= len(data) + row_len:
        if pos >= len(data):
            break
        tag = data[pos]
        row = bytearray(data[pos + 1:pos + 1 + row_len])
        if len(row) < row_len:
            row.extend(b"\x00" * (row_len - len(row)))
        pos += 1 + row_len
        if tag == 1:
            for i in range(bpp, row_len):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif tag == 2:
            for i in range(row_len):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif tag == 3:
            for i in range(row_len):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif tag == 4:
            for i in range(row_len):
                a = row[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[i] = (row[i] + pred) & 0xFF
        out += row
        prev = row
    return bytes(out)


def _ascii85(data: bytes) -> bytes:
    import base64
    payload = data.strip()
    if payload.startswith(b"<~"):
        payload = payload[2:]
    end = payload.find(b"~>")
    if end >= 0:
        payload = payload[:end]
    return base64.a85decode(payload, adobe=False, ignorechars=b" \t\r\n\v")


def _lzw(data: bytes, parm: Dict[str, Any]) -> bytes:
    """Minimal LZW decoder (early change = 1, the PDF default)."""
    result = bytearray()
    table: List[bytes] = [bytes([i]) for i in range(256)] + [b"", b""]
    bitpos = 0
    codelen = 9
    prev: Optional[bytes] = None

    def read_code() -> Optional[int]:
        nonlocal bitpos
        if bitpos + codelen > len(data) * 8:
            return None
        value = 0
        for _ in range(codelen):
            byte = data[bitpos >> 3]
            bit = (byte >> (7 - (bitpos & 7))) & 1
            value = (value << 1) | bit
            bitpos += 1
        return value

    while True:
        code = read_code()
        if code is None:
            break
        if code == 256:
            table = [bytes([i]) for i in range(256)] + [b"", b""]
            codelen = 9
            prev = None
            continue
        if code == 257:
            break
        if prev is None:
            entry = table[code]
        elif code < len(table):
            entry = table[code]
            table.append(prev + entry[:1])
        else:
            entry = prev + prev[:1]
            table.append(entry)
        result += entry
        prev = entry
        if len(table) + 1 >= (1 << codelen) and codelen < 12:
            codelen += 1
    return bytes(result)


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------

_OBJ_RE = re.compile(rb"(?<![0-9])(\d{1,10})\s+(\d{1,5})\s+obj\b")
_XREF_ENTRY_RE = re.compile(rb"^\s*(\d{10})\s+(\d{5})\s+([nf])\s*$")
_TRAILER_RE = re.compile(rb"trailer\b")


class PdfFile:
    """Reads a PDF far enough to walk its page tree and annotations."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offsets: Dict[int, int] = {}
        self.trailer: Dict[str, Any] = {}
        self.cache: Dict[int, Any] = {}
        self.objstm_cache: Dict[int, Dict[int, Any]] = {}
        self.encrypted = False
        self._recovered = False
        self._load()

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_path(cls, path: Union[str, Path]) -> "PdfFile":
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            raise PdfObjectError(f"could not read {path}: {exc}") from exc
        if not data.startswith(b"%PDF"):
            # Some producers prepend junk; look for the header nearby.
            idx = data.find(b"%PDF", 0, 4096)
            if idx < 0:
                raise PdfObjectError("not a PDF file (no %PDF header)")
        return cls(data)

    def _load(self) -> None:
        if not self._read_xref_chain():
            # Broken, linearised or encrypted-with-broken-xref files: fall
            # back to scanning for "N G obj" markers, which is what every
            # recovery tool does and works on the overwhelming majority.
            self._recover_object_offsets()
        trailer = self.trailer
        if isinstance(trailer.get("Encrypt"), (Ref, dict)):
            self.encrypted = True
        if "Root" not in trailer and self.offsets:
            self._recover_object_offsets()

    # -- xref -----------------------------------------------------------------

    def _read_xref_chain(self) -> bool:
        start = self.data.rfind(b"startxref")
        if start < 0:
            return False
        m = _NUMBER_RE.search(self.data[start + 9:start + 60])
        if not m:
            return False
        try:
            offset = int(float(m.group(0)))
        except ValueError:
            return False
        seen: set = set()
        ok = False
        while offset and offset not in seen and 0 <= offset < len(self.data):
            seen.add(offset)
            nxt = self._read_xref_section(offset)
            ok = True
            if nxt is None:
                break
            offset = nxt
        return ok

    def _read_xref_section(self, offset: int) -> Optional[int]:
        data = self.data
        chunk = data[offset:offset + 4096]
        if chunk.startswith(b"xref"):
            return self._read_classic_xref(offset)
        # Otherwise it should be an xref *stream*: N G obj << ... >> stream
        m = _OBJ_RE.match(data, offset)
        if not m:
            m = _OBJ_RE.search(data, offset, offset + 200)
            if not m:
                return None
        obj = self._parse_indirect_at(m.end())
        if not isinstance(obj, Stream):
            return None
        for key, value in obj.dict.items():
            self.trailer.setdefault(key, value)
        self._apply_xref_stream(obj)
        prev = obj.dict.get("Prev")
        return int(prev) if isinstance(prev, int) else None

    def _read_classic_xref(self, offset: int) -> Optional[int]:
        data = self.data
        pos = offset + 4
        entries: List[Tuple[int, int, str]] = []
        while True:
            line_end = data.find(b"\n", pos)
            if line_end < 0:
                break
            line = data[pos:line_end].strip()
            pos = line_end + 1
            if line.startswith(b"trailer"):
                break
            if not line:
                continue
            parts = line.split()
            if len(parts) == 2 and parts[1] == b"f":
                continue
            if len(parts) == 2:
                # Subsection header: "start count"
                continue
            m = _XREF_ENTRY_RE.match(line)
            if m:
                entries.append((int(m.group(1)), int(m.group(2)),
                                m.group(3).decode()))
        # Re-read subsection headers properly: walk the section again.
        self._walk_classic_subsections(offset)
        # Trailer dictionary
        idx = data.find(b"trailer", pos - 1)
        if idx >= 0:
            parser = _Parser(data, self, idx + 7)
            trailer = parser.parse_object()
            if isinstance(trailer, dict):
                for key, value in trailer.items():
                    self.trailer.setdefault(key, value)
            prev = self.trailer.get("Prev")
            return int(prev) if isinstance(prev, int) else None
        return None

    def _walk_classic_subsections(self, offset: int) -> None:
        """Populate ``self.offsets`` from a classic xref section."""
        data = self.data
        pos = offset + 4
        while True:
            while pos < len(data) and data[pos:pos + 1] in b"\r\n \t":
                pos += 1
            if data[pos:pos + 7] == b"trailer":
                return
            line_end = data.find(b"\n", pos)
            if line_end < 0:
                return
            header = data[pos:line_end].strip()
            pos = line_end + 1
            parts = header.split()
            if len(parts) != 2 or not all(p.isdigit() for p in parts):
                return
            start, count = int(parts[0]), int(parts[1])
            for i in range(count):
                line_end = data.find(b"\n", pos)
                if line_end < 0:
                    return
                entry = data[pos:line_end]
                pos = line_end + 1
                m = _XREF_ENTRY_RE.match(entry)
                if not m:
                    continue
                if m.group(3) == b"n":
                    self.offsets.setdefault(start + i, int(m.group(1)))

    def _apply_xref_stream(self, stream: Stream) -> None:
        d = stream.dict
        try:
            data = stream.get_data()
        except Exception:
            return
        widths = _as_list(d.get("W")) or [1, 2, 1]
        widths = [_int(w, 0) for w in widths] + [0, 0, 0]
        size = _int(d.get("Size"), 0)
        index = _as_list(d.get("Index")) or [0, size]
        type_w = widths[0]
        pos = 0
        for i in range(0, len(index) - 1, 2):
            first, count = _int(index[i], 0), _int(index[i + 1], 0)
            for j in range(count):
                if pos + sum(widths) > len(data):
                    return
                fields = []
                for w in widths:
                    if w == 0:
                        fields.append(None)
                    else:
                        fields.append(int.from_bytes(data[pos:pos + w], "big"))
                        pos += w
                if fields[0] == 1 and fields[1] is not None:
                    self.offsets.setdefault(first + j, fields[1])
                elif fields[0] == 2 and fields[1] is not None:
                    self.objstm_cache.setdefault(fields[1], {})[first + j] = fields[2]

    def _recover_object_offsets(self) -> None:
        """Scan the whole file for ``N G obj`` and rebuild the map."""
        self._recovered = True
        self.offsets.clear()
        for m in _OBJ_RE.finditer(self.data):
            self.offsets.setdefault(int(m.group(1)), m.start())
        for m in _TRAILER_RE.finditer(self.data):
            parser = _Parser(self.data, self, m.end())
            trailer = parser.parse_object()
            if isinstance(trailer, dict):
                for key, value in trailer.items():
                    self.trailer.setdefault(key, value)

    # -- objects --------------------------------------------------------------

    def _parse_indirect_at(self, pos: int) -> Any:
        parser = _Parser(self.data, self, pos)
        obj = parser.parse_object()
        if isinstance(obj, dict):
            parser.skip_ws()
            if self.data[parser.pos:parser.pos + 6] == b"stream":
                pos2 = parser.pos + 6
                if self.data[pos2:pos2 + 2] == b"\r\n":
                    pos2 += 2
                elif self.data[pos2:pos2 + 1] in (b"\n", b"\r"):
                    pos2 += 1
                length = obj.get("Length")
                end = None
                if isinstance(length, int):
                    end = pos2 + length
                elif isinstance(length, Ref):
                    resolved = self.resolve(length)
                    if isinstance(resolved, int):
                        end = pos2 + resolved
                if end is None or end > len(self.data):
                    end = self.data.find(b"endstream", pos2)
                    if end < 0:
                        end = len(self.data)
                raw = self.data[pos2:end]
                return Stream(obj, raw, self)
        return obj

    def resolve(self, obj: Any, depth: int = 0) -> Any:
        """Follow indirect references until a direct object appears."""
        while isinstance(obj, Ref) and depth < 32:
            obj = self.get_object(obj.num)
            depth += 1
        return obj

    def get_object(self, num: int) -> Any:
        if num in self.cache:
            return self.cache[num]
        self.cache[num] = None  # cycle guard
        offset = self.offsets.get(num)
        result: Any = None
        if offset is not None:
            m = _OBJ_RE.match(self.data, offset)
            if m and int(m.group(1)) == num:
                result = self._parse_indirect_at(m.end())
            else:
                for candidate in _OBJ_RE.finditer(self.data):
                    if int(candidate.group(1)) == num:
                        result = self._parse_indirect_at(candidate.end())
                        break
        if result is None:
            result = self._load_from_objstm(num)
        self.cache[num] = result
        return result

    def _load_from_objstm(self, num: int) -> Any:
        # Find an object stream that claims to contain this object.
        for m in re.finditer(rb"/Type\s*/ObjStm", self.data):
            start = self.data.rfind(b"obj", 0, m.start())
            if start < 0:
                continue
            header = _OBJ_RE.search(self.data, max(0, start - 40), start + 3)
            if not header:
                continue
            stream_obj = self.get_object(int(header.group(1)))
            if not isinstance(stream_obj, Stream):
                continue
            try:
                data = stream_obj.get_data()
            except Exception:
                continue
            n = _int(stream_obj.dict.get("N"), 0)
            first = _int(stream_obj.dict.get("First"), 0)
            header_text = data[:first].split()
            for i in range(n):
                try:
                    onum = int(header_text[2 * i])
                    ooff = int(header_text[2 * i + 1])
                except (IndexError, ValueError):
                    break
                if onum == num:
                    parser = _Parser(data, self, first + ooff)
                    return parser.parse_object()
        return None

    # -- document structure ---------------------------------------------------

    @property
    def root(self) -> Dict[str, Any]:
        root = self.resolve(self.trailer.get("Root"))
        return root if isinstance(root, dict) else {}

    def catalog_pages(self) -> List[Dict[str, Any]]:
        """Return every page dictionary in document order."""
        root = self.root
        pages: List[Dict[str, Any]] = []
        seen: set = set()

        def walk(node: Any, inherited: Dict[str, Any]) -> None:
            node = self.resolve(node)
            if not isinstance(node, dict) or id(node) in seen:
                return
            seen.add(id(node))
            merged = dict(inherited)
            for key in ("MediaBox", "CropBox", "Rotate", "Resources"):
                if key in node:
                    merged[key] = node[key]
            kids = self.resolve(node.get("Kids"))
            node_type = str(node.get("Type", ""))
            if node_type == "Page" or (kids is None and "Contents" in node):
                page = dict(node)
                for key, value in merged.items():
                    page.setdefault(key, value)
                pages.append(page)
                return
            if isinstance(kids, list):
                for kid in kids:
                    walk(kid, merged)

        pages_node = root.get("Pages")
        if pages_node is not None:
            walk(pages_node, {})
        if not pages:
            # Recovered files sometimes lost /Root; find page objects directly.
            for num in sorted(self.offsets):
                obj = self.resolve(self.get_object(num))
                if isinstance(obj, dict) and str(obj.get("Type", "")) == "Page":
                    pages.append(obj)
        return pages


# ---------------------------------------------------------------------------
# Convenience helpers for the rest of the tool
# ---------------------------------------------------------------------------

def text_of(value: Any) -> str:
    """Decode a PDF string object (PDFDocEncoding/UTF-16BE) to Python text."""
    if isinstance(value, bytes):
        if value.startswith(b"\xfe\xff"):
            return value[2:].decode("utf-16-be", "replace")
        return value.decode("latin-1")
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def number_list(value: Any) -> List[float]:
    out: List[float] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, (int, float)):
                out.append(float(item))
            elif isinstance(item, Ref):
                resolved = item
                out.append(float(resolved.num))
    return out


def page_rotation(page: Dict[str, Any]) -> int:
    try:
        rot = int(page.get("Rotate", 0))
    except (TypeError, ValueError):
        rot = 0
    return rot % 360


def page_mediabox(page: Dict[str, Any]) -> Tuple[float, float, float, float]:
    box = page.get("MediaBox") or page.get("CropBox") or [0, 0, 612, 792]
    nums = number_list(box)
    if len(nums) != 4:
        return (0.0, 0.0, 612.0, 792.0)
    x0, y0, x1, y1 = nums
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
