"""doc_text: text out of attached PDF / HWP / HWPX / PPTX / DOCX, never raising.

Fixtures are built here in code. The extractor was also run on real local
documents (10 HWP, 2 PPTX, 40 PDF, 25 DOCX) on 2026-11-18: every file with a
text layer gave Korean text; the PDFs that gave none were scanned images.
"""
from __future__ import annotations

import importlib.util
import io
import struct
import sys
import types
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import doc_text as dt  # noqa: E402

try:
    import pypdf
except ImportError:  # pragma: no cover
    pypdf = None


def zip_bytes(members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in members.items():
            zf.writestr(name, text)
    return buf.getvalue()


HP = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"'


def hwpx(*sections: str) -> bytes:
    return zip_bytes({f"Contents/section{i}.xml": s for i, s in enumerate(sections)}
                     | {"mimetype": "application/hwp+zip"})


def hwpx_section(*paras: str) -> str:
    body = "".join(f"<hp:p><hp:run><hp:t>{p}</hp:t></hp:run></hp:p>" for p in paras)
    return f'<?xml version="1.0" encoding="UTF-8"?><hs:sec xmlns:hs="urn:s" {HP}>{body}</hs:sec>'


def docx(*paras: str) -> bytes:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paras)
    return zip_bytes({"word/document.xml": f"<w:document {w}><w:body>{body}</w:body></w:document>"})


def pptx(*slides: str) -> bytes:
    a = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    return zip_bytes({f"ppt/slides/slide{i + 1}.xml":
                      f"<p:sld xmlns:p='urn:p' {a}><a:p><a:r><a:t>{s}</a:t></a:r></a:p></p:sld>"
                      for i, s in enumerate(slides)})


def pdf(text_codes: bytes, cmap: dict[int, str]) -> bytes:
    """One-page PDF drawing ``text_codes`` with a font whose ToUnicode CMap
    maps each byte to ``cmap`` (how Korean PDFs carry their text)."""
    bf = "".join(f"<{k:02X}> <{ord(v):04X}>\n" for k, v in cmap.items())
    tounicode = (
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        "1 begincodespacerange <00> <FF> endcodespacerange\n"
        f"{len(cmap)} beginbfchar\n{bf}endbfchar\nendcmap end end"
    ).encode()
    content = b"BT /F1 12 Tf 50 700 Td <" + text_codes.hex().encode() + b"> Tj ET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 6 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Length %d >>\nstream\n" % len(tounicode) + tounicode + b"\nendstream",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    return out.getvalue()


KOREAN = {1: "수", 2: "건", 3: " ", 4: "보", 5: "내", 6: "주", 7: "세", 8: "요"}
KOREAN_PDF = pdf(bytes([1, 2, 3, 4, 5, 6, 7, 8]), KOREAN)


def hwp_record(tag: int, payload: bytes) -> bytes:
    if len(payload) >= 0xFFF:
        return struct.pack("<II", tag | (0xFFF << 20), len(payload)) + payload
    return struct.pack("<I", tag | (len(payload) << 20)) + payload


def para_text(text: str) -> bytes:
    return hwp_record(dt.HWPTAG_PARA_TEXT, (text + "\r").encode("utf-16-le"))


class OfficeZipTest(unittest.TestCase):
    def test_hwpx_sections_in_natural_order(self):
        sections = [hwpx_section(f"섹션{i}") for i in range(11)]
        text = dt.extract_text(hwpx(*sections), "가정통신문.hwpx")
        self.assertEqual(text.split("\n"), [f"섹션{i}" for i in range(11)])

    def test_hwpx_table_cells_each_on_a_line(self):
        cell = "<hp:tc><hp:subList><hp:p><hp:run><hp:t>{}</hp:t></hp:run></hp:p></hp:subList></hp:tc>"
        table = ("<hp:p><hp:run><hp:tbl><hp:tr>" + cell.format("11월 18일(수)") + cell.format("수건 보내주세요")
                 + "</hp:tr></hp:tbl></hp:run></hp:p>")
        sec = f'<hs:sec xmlns:hs="urn:s" {HP}>{table}</hs:sec>'
        self.assertEqual(dt.extract_text(hwpx(sec), "a.HWPX"), "11월 18일(수)\n수건 보내주세요")

    def test_pptx_and_docx(self):
        self.assertEqual(dt.extract_text(pptx("첫 장", "둘째 장"), "계획.pptx"), "첫 장\n\n둘째 장")
        self.assertEqual(dt.extract_text(docx("준비물: 원복", "11/20 견학"), "안내.docx"),
                         "준비물: 원복\n11/20 견학")

    def test_unpacked_size_budget_skips_huge_members(self):
        with mock.patch.object(dt, "MAX_UNPACKED_BYTES", 500):
            raw = hwpx(hwpx_section("작다"), hwpx_section("크" * 1000))
            self.assertEqual(dt.extract_text(raw, "a.hwpx"), "작다")


@unittest.skipIf(pypdf is None, "pypdf not installed")
class PdfTest(unittest.TestCase):
    def test_korean_text_through_tounicode(self):
        self.assertEqual(dt.extract_text(KOREAN_PDF, "통신문.pdf"), "수건 보내주세요")

    def test_password_protected_pdf_gives_nothing(self):
        w = pypdf.PdfWriter(clone_from=pypdf.PdfReader(io.BytesIO(KOREAN_PDF)))
        w.encrypt(user_password="pw", owner_password="owner")
        buf = io.BytesIO()
        w.write(buf)
        self.assertEqual(dt.extract_text(buf.getvalue(), "a.pdf"), "")

    def test_owner_only_encryption_still_reads(self):
        w = pypdf.PdfWriter(clone_from=pypdf.PdfReader(io.BytesIO(KOREAN_PDF)))
        w.encrypt(user_password="", owner_password="owner", algorithm="RC4-128")
        buf = io.BytesIO()
        w.write(buf)
        self.assertEqual(dt.extract_text(buf.getvalue(), "a.pdf"), "수건 보내주세요")


class HwpTest(unittest.TestCase):
    def test_records_controls_and_long_sizes(self):
        extended = "\x0b" + "\x00" * 7          # a table/shape control: 8 code units
        long_text = "가" * 3000                  # forces the 0xFFF extended size header
        data = (hwp_record(0x10 + 50, b"\x00" * 22)  # PARA_HEADER, ignored
                + para_text(f"{extended}표 앞 글")
                + para_text("줄\n바꿈")
                + para_text(long_text))
        self.assertEqual(dt.hwp_section_text(data).split("\n"), ["표 앞 글", "줄", "바꿈", long_text, ""])

    def test_tab_control_counts_eight_units(self):
        payload = ("앞" + "\t" + "\x00" * 7 + "뒤\r").encode("utf-16-le")
        self.assertEqual(dt.hwp_section_text(hwp_record(dt.HWPTAG_PARA_TEXT, payload)), "앞\t뒤\n")

    def test_truncated_stream_does_not_raise(self):
        data = para_text("온전한 문단")[:-3]
        self.assertIsInstance(dt.hwp_section_text(data), str)

    def fake_ole(self, flags: int, sections: list[bytes]):
        header = dt._HWP_SIGNATURE.ljust(32, b"\0") + struct.pack("<II", 0x05000000, flags)
        streams = {"FileHeader": header.ljust(256, b"\0")}
        streams |= {f"BodyText/Section{i}": s for i, s in enumerate(sections)}

        class Ole:
            def __init__(self, _fh):
                self.closed = False

            def exists(self, name):
                return name in streams

            def openstream(self, name):
                return io.BytesIO(streams[name])

            def close(self):
                self.closed = True

        return types.SimpleNamespace(OleFileIO=Ole)

    @staticmethod
    def deflate(data: bytes) -> bytes:
        c = zlib.compressobj(wbits=-15)
        return c.compress(data) + c.flush()

    def test_compressed_document(self):
        secs = [self.deflate(para_text("11월 20일(금) 드론과 놀아요")), self.deflate(para_text("수건 보내주세요"))]
        with mock.patch.dict(sys.modules, {"olefile": self.fake_ole(dt._FLAG_COMPRESSED, secs)}):
            self.assertEqual(dt.extract_text(b"x" * 10, "plan.hwp"), "11월 20일(금) 드론과 놀아요\n수건 보내주세요")

    def test_locked_documents_give_nothing(self):
        for flag in (dt._FLAG_PASSWORD, dt._FLAG_DISTRIBUTION):
            fake = self.fake_ole(dt._FLAG_COMPRESSED | flag, [self.deflate(para_text("비밀"))])
            with mock.patch.dict(sys.modules, {"olefile": fake}):
                self.assertEqual(dt.extract_text(b"x" * 10, "a.hwp"), "")


class GuideTest(unittest.TestCase):
    def test_readme_names_the_toggle_and_formats_as_shown(self):
        readme = (HERE.parents[2] / "README.md").read_text(encoding="utf-8")
        self.assertIn("**📄 (파일 이름) — 글 내용**", readme)
        self.assertEqual(dt.EXTENSIONS, (".pdf", ".hwp", ".hwpx", ".pptx", ".docx"))
        self.assertIn("PDF·한글(HWP·HWPX)·파워포인트·워드", readme)


def ole2(streams: dict[str, bytes]) -> bytes:
    """Minimal OLE2 compound file (what an .hwp is) with ``FileHeader`` and
    ``BodyText/SectionN`` streams. Streams are padded to 4096 bytes so they
    live in regular sectors (no mini stream); readers use the declared data."""
    sec, end, free, fat_mark, none = 512, 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD, 0xFFFFFFFF
    sectors: list[bytes] = []
    fat: list[int] = []
    starts: dict[str, int] = {}
    for name, data in streams.items():
        data = data.ljust(4096, b"\0")
        data = data.ljust(-(-len(data) // sec) * sec, b"\0")
        n = len(data) // sec
        starts[name] = len(sectors)
        for i in range(n):
            sectors.append(data[i * sec:(i + 1) * sec])
            fat.append(len(sectors) if i < n - 1 else end)

    def entry(name, kind, left=none, right=none, child=none, start=end, size=0):
        raw = name.encode("utf-16-le") + b"\0\0"
        return (raw.ljust(64, b"\0") + struct.pack("<HBB", len(raw), kind, 1)
                + struct.pack("<III", left, right, child) + b"\0" * 36
                + struct.pack("<II", start, size) + b"\0" * 4)

    sections = sorted(n for n in streams if n.startswith("BodyText/"))
    names = [n.split("/", 1)[1] for n in sections]
    # 0 root, 1 FileHeader, 2 BodyText storage, 3.. sections (as a right-sibling chain).
    dir_entries = [
        entry("Root Entry", 5, child=1),
        entry("FileHeader", 2, left=2, start=starts["FileHeader"], size=4096),
        entry("BodyText", 1, child=3 if names else none),
    ]
    for i, (full, short) in enumerate(zip(sections, names)):
        nxt = 3 + i + 1 if i + 1 < len(names) else none
        dir_entries.append(entry(short, 2, right=nxt, start=starts[full], size=4096))
    directory = b"".join(dir_entries)
    directory = directory.ljust(-(-len(directory) // sec) * sec, b"\0")
    dir_start = len(sectors)
    n_dir = len(directory) // sec
    for i in range(n_dir):
        sectors.append(directory[i * sec:(i + 1) * sec])
        fat.append(len(sectors) if i < n_dir - 1 else end)
    fat_start = len(sectors)
    fat.append(fat_mark)
    assert len(fat) <= sec // 4
    sectors.append(struct.pack(f"<{sec // 4}I", *(fat + [free] * (sec // 4 - len(fat)))))
    header = (bytes.fromhex("D0CF11E0A1B11AE1") + b"\0" * 16
              + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) + b"\0" * 6
              + struct.pack("<IIIIIIIII", 0, 1, dir_start, 0, 4096, end, 0, end, 0)
              + struct.pack("<109I", fat_start, *([free] * 108)))
    return header + b"".join(sectors)


@unittest.skipIf(importlib.util.find_spec("olefile") is None, "olefile not installed")
class RealOleHwpTest(unittest.TestCase):
    """Same extractor, through the real olefile library."""

    @staticmethod
    def hwp(flags: int, *paras: str) -> bytes:
        header = dt._HWP_SIGNATURE.ljust(32, b"\0") + struct.pack("<II", 0x05000000, flags)
        streams = {"FileHeader": header.ljust(256, b"\0")}
        for i, p in enumerate(paras):
            body = para_text(p)
            if flags & dt._FLAG_COMPRESSED:
                c = zlib.compressobj(wbits=-15)
                body = c.compress(body) + c.flush()
            streams[f"BodyText/Section{i}"] = body
        return ole2(streams)

    def test_compressed_and_plain_sections(self):
        self.assertEqual(dt.extract_text(self.hwp(dt._FLAG_COMPRESSED, "11월 20일 견학", "우비 보내주세요"),
                                         "계획.hwp"), "11월 20일 견학\n우비 보내주세요")
        self.assertEqual(dt.extract_text(self.hwp(0, "압축 안 한 문서"), "a.hwp"), "압축 안 한 문서")

    def test_distribution_locked_file_gives_nothing(self):
        raw = self.hwp(dt._FLAG_COMPRESSED | dt._FLAG_DISTRIBUTION, "잠긴 글")
        self.assertEqual(dt.extract_text(raw, "a.hwp"), "")


class RobustnessTest(unittest.TestCase):
    def test_garbage_and_unsupported_never_raise(self):
        for name in ("a.pdf", "a.hwp", "a.hwpx", "a.pptx", "a.docx"):
            self.assertEqual(dt.extract_text(b"not a document at all", name), "")
        self.assertEqual(dt.extract_text(b"text", "a.xlsx"), "")
        self.assertEqual(dt.extract_text(b"", "a.pdf"), "")
        self.assertFalse(dt.can_extract("photo.jpg"))
        self.assertTrue(dt.can_extract("월간계획.HWP"))

    def test_oversized_input_is_not_read(self):
        with mock.patch.object(dt, "MAX_INPUT_BYTES", 10):
            self.assertEqual(dt.extract_text(docx("내용"), "a.docx"), "")

    def test_missing_library_gives_nothing(self):
        with mock.patch.dict(sys.modules, {"olefile": None, "pypdf": None}):
            self.assertEqual(dt.extract_text(b"x" * 10, "a.hwp"), "")
            self.assertEqual(dt.extract_text(KOREAN_PDF, "a.pdf"), "")

    def test_output_is_tidied_and_capped(self):
        raw = docx("  여러   칸  ", "", "", "다음")
        self.assertEqual(dt.extract_text(raw, "a.docx"), "여러 칸\n\n다음")
        with mock.patch.object(dt, "MAX_CHARS", 30):
            out = dt.extract_text(docx(*[f"{i}번째 줄입니다" for i in range(20)]), "a.docx")
            self.assertTrue(out.endswith("\n…") and len(out) <= 32)


if __name__ == "__main__":
    unittest.main()
