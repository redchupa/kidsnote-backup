"""Plain text out of the documents daycares attach (PDF, HWP, HWPX, PPTX, DOCX).

Monthly plans and 가정통신문 often live inside an attached ``.hwp`` / ``.pdf``
rather than in the post itself (idea from issue #2). The text goes into the
Notion page (so Notion search finds it) and into the 📋 준비물·일정 board.

- PDF via ``pypdf`` and the HWP5 container via ``olefile`` (both pure Python,
  well tested). HWPX / PPTX / DOCX are zip + XML, read with the stdlib.
- Never raises: a broken, encrypted, or 배포용 (distribution-locked) file,
  or a missing library, just gives ``""`` and the backup carries on.
- Bounded: input size, decompressed size, page / section count and output
  length are all capped, so a hostile or huge file can't stall a run.
"""
from __future__ import annotations

import io
import logging
import re
import struct
import zipfile
import zlib
from xml.etree import ElementTree as ET

_LOGGER = logging.getLogger(__name__)

MAX_INPUT_BYTES = 20_000_000       # files bigger than this are not read
MAX_UNPACKED_BYTES = 60_000_000    # zip members / HWP sections, total
MAX_CHARS = 20_000                 # text kept per document
MAX_PDF_PAGES = 60
MAX_HWP_SECTIONS = 100

EXTENSIONS = (".pdf", ".hwp", ".hwpx", ".pptx", ".docx")


def can_extract(filename: str) -> bool:
    return isinstance(filename, str) and filename.lower().endswith(EXTENSIONS)


def extract_text(raw: bytes, filename: str) -> str:
    """Text of the document (``""`` when unsupported, unreadable or empty)."""
    if not raw or len(raw) > MAX_INPUT_BYTES or not can_extract(filename):
        return ""
    ext = filename.lower().rsplit(".", 1)[-1]
    try:
        text = {
            "pdf": _pdf,
            "hwp": _hwp,
            "hwpx": _hwpx,
            "pptx": _pptx,
            "docx": _docx,
        }[ext](raw)
    except Exception as e:  # noqa: BLE001 - any parser failure means "no text"
        _LOGGER.info("no text from %s (%s)", filename, type(e).__name__)
        return ""
    return _tidy(text)


def _tidy(text: str) -> str:
    lines = []
    for line in (text or "").replace("\r", "\n").split("\n"):
        line = " ".join(line.replace(" ", " ").split())
        if line or (lines and lines[-1]):
            lines.append(line)
    out = "\n".join(lines).strip()
    if len(out) > MAX_CHARS:
        cut = out.rfind("\n", 0, MAX_CHARS)
        out = out[: cut if cut > MAX_CHARS // 2 else MAX_CHARS].rstrip() + "\n…"
    return out


# ------------------------------------------------------------------ PDF

def _pdf(raw: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                return ""
        except Exception:  # noqa: BLE001 - unsupported encryption
            return ""
    parts: list[str] = []
    total = 0
    for i, page in enumerate(reader.pages):
        if i >= MAX_PDF_PAGES or total > MAX_CHARS:
            break
        t = page.extract_text() or ""
        parts.append(t)
        total += len(t)
    return "\n".join(parts)


# ------------------------------------------------------------------ zip + XML

def _zip_members(raw: bytes, pattern: str) -> list[bytes]:
    """Members matching ``pattern`` in natural order (section2 before section10)."""
    zf = zipfile.ZipFile(io.BytesIO(raw))
    rx = re.compile(pattern)
    names = [n for n in zf.namelist() if rx.fullmatch(n)]
    names.sort(key=lambda n: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", n)])
    out: list[bytes] = []
    budget = MAX_UNPACKED_BYTES
    for name in names:
        size = zf.getinfo(name).file_size
        if size > budget:
            break
        budget -= size
        with zf.open(name) as fh:
            out.append(fh.read(size + 1)[:size])
    return out


def _xml_text(data: bytes, text_tag: str, para_tag: str, *, tab_tag: str = "", br_tag: str = "") -> str:
    """Text of ``text_tag`` elements, a newline after every ``para_tag``.

    Tags are matched on their local name, so any namespace prefix works.
    """
    out: list[str] = []
    for event, el in ET.iterparse(io.BytesIO(data), events=("end",)):
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == text_tag:
            out.append("".join(el.itertext()))
        elif tag == para_tag:
            out.append("\n")
        elif tab_tag and tag == tab_tag:
            out.append("\t")
        elif br_tag and tag == br_tag:
            out.append("\n")
        if tag == para_tag:
            el.clear()
    return "".join(out)


def _hwpx(raw: bytes) -> str:
    # OWPML: Contents/section0.xml … with <hp:p><hp:run><hp:t>text</hp:t>
    sections = _zip_members(raw, r"Contents/section\d+\.xml")
    return "".join(_xml_text(x, "t", "p") for x in sections)  # each ends in "\n"


def _pptx(raw: bytes) -> str:
    slides = _zip_members(raw, r"ppt/slides/slide\d+\.xml")
    return "\n\n".join(_xml_text(x, "t", "p", br_tag="br") for x in slides)


def _docx(raw: bytes) -> str:
    parts = _zip_members(raw, r"word/document\.xml")
    return "\n".join(_xml_text(x, "t", "p", tab_tag="tab", br_tag="br") for x in parts)


# ------------------------------------------------------------------ HWP 5

_HWP_SIGNATURE = b"HWP Document File"
_FLAG_COMPRESSED, _FLAG_PASSWORD, _FLAG_DISTRIBUTION = 0x1, 0x2, 0x4
HWPTAG_PARA_TEXT = 0x10 + 51

# Control characters inside PARA_TEXT (HWP 5.0 spec, 표 6). "Char" controls
# take one code unit; inline / extended controls take eight.
_CHAR_CONTROLS = {0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31}


def _hwp(raw: bytes) -> str:
    import olefile

    ole = olefile.OleFileIO(io.BytesIO(raw))
    try:
        header = ole.openstream("FileHeader").read(256)
        if not header.startswith(_HWP_SIGNATURE):
            return ""
        flags = struct.unpack_from("<I", header, 36)[0]
        if flags & (_FLAG_PASSWORD | _FLAG_DISTRIBUTION):
            return ""  # encrypted body: no readable text
        compressed = bool(flags & _FLAG_COMPRESSED)
        parts: list[str] = []
        budget = MAX_UNPACKED_BYTES
        for n in range(MAX_HWP_SECTIONS):
            name = f"BodyText/Section{n}"
            if not ole.exists(name):
                break
            data = ole.openstream(name).read()
            if compressed:
                d = zlib.decompressobj(-15)
                data = d.decompress(data, budget)
            budget -= len(data)
            parts.append(hwp_section_text(data))
            if budget <= 0:
                break
        return "".join(parts)  # each section ends in "\n"
    finally:
        ole.close()


def hwp_section_text(data: bytes) -> str:
    """Paragraph text of one decompressed ``BodyText/SectionN`` record stream.

    Table cells are paragraphs too, so a table comes out cell by cell, one
    per line, in reading order.
    """
    out: list[str] = []
    pos, end = 0, len(data)
    while pos + 4 <= end:
        (head,) = struct.unpack_from("<I", data, pos)
        pos += 4
        tag, size = head & 0x3FF, (head >> 20) & 0xFFF
        if size == 0xFFF:
            if pos + 4 > end:
                break
            (size,) = struct.unpack_from("<I", data, pos)
            pos += 4
        if tag == HWPTAG_PARA_TEXT:
            out.append(_para_text(data[pos:pos + size]))
            out.append("\n")
        pos += size
    return "".join(out)


def _para_text(payload: bytes) -> str:
    units = struct.unpack(f"<{len(payload) // 2}H", payload[: len(payload) // 2 * 2])
    chars: list[str] = []
    i = 0
    while i < len(units):
        c = units[i]
        if c >= 32:
            chars.append(chr(c))
            i += 1
        elif c in _CHAR_CONTROLS:
            if c == 10:
                chars.append("\n")
            elif c in (30, 31):
                chars.append(" ")
            i += 1
        else:
            if c == 9:
                chars.append("\t")
            i += 8
    # Surrogate pairs arrive as two units; re-pair them.
    return "".join(chars).encode("utf-16", "surrogatepass").decode("utf-16", "replace")
