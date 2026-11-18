"""Videos / files over Notion's per-file cap become kidsnote links instead of vanishing.

Live facts these tests pin (checked 2026-11-15 against a real account): every
attachment carries ``file_size``; videos have ``high`` but no ``original``;
media URLs are public Kakao CDN paths with no query string, so the path itself
is the access key and must never reach a (public fork's) run log.
"""
from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

try:
    import requests

    import fetch
    import notion_mirror as nm
except ImportError:  # pragma: no cover - requests not installed
    nm = None

from fake_http import FakeServer  # noqa: E402
from fake_notion import NOTION_TOKEN, dashed, hexid  # noqa: E402

DB = hexid(2)
CAP = 5_000_000
SECRET_KEY = "ckOGHV/dJMcacrRWAB/wb3F4MmXnnfqHzGr1MyEM"


def video(n: int, size: int, *, name: str | None = None, base: str = "https://up-kids-kage.kakao.com") -> dict:
    return {
        "id": n,
        "original_file_name": name or f"clip_{n}.mp4",
        "file_size": size,
        "high": f"{base}/dn/{SECRET_KEY}/{n}/video_high.mp4",
        "low": f"{base}/dn/{SECRET_KEY}/{n}/video_low.mp4",
    }


class _Backend:
    """One fake server for both sides: Notion API + kidsnote CDN downloads."""

    def __init__(self) -> None:
        self.pages: list[dict] = []
        self.appended: list[tuple[str, list]] = []
        self.uploads = 0
        self.downloads: list[str] = []
        self.media: dict[str, bytes] = {}
        self.missing: set[str] = set()
        self.archived: list[str] = []
        self.fail_append = False
        self._n = 0

    def __call__(self, req):
        path = req.path
        if path.startswith("/dn/"):
            self.downloads.append(path)
            if path in self.missing:
                return 404, {}, b"gone"
            return 200, {"Content-Type": "video/mp4"}, self.media.get(path, b"\0" * 10)
        if path == f"/databases/{DB}" and req.method == "GET":
            return 200, {}, {"object": "database", "id": dashed(DB), "properties": {
                "이름": {"type": "title"}, "날짜": {"type": "date"}, "Report ID": {"type": "number"}}}
        if path == "/file_uploads" and req.method == "POST":
            self._n += 1
            return 200, {}, {"id": f"fu-{self._n}", "upload_url": f"{self.url}/upload/{self._n}"}
        if path.startswith("/upload/"):
            self.uploads += 1
            return 200, {}, {"status": "uploaded"}
        if path == "/pages" and req.method == "POST":
            body = req.json()
            self.pages.append(body)
            return 200, {}, {"object": "page", "id": dashed(hexid(500 + len(self.pages))), "url": ""}
        if path.startswith("/pages/") and req.method == "PATCH":
            if req.json().get("archived"):
                self.archived.append(path.rsplit("/", 1)[-1])
            return 200, {}, {"object": "page"}
        if path.startswith("/blocks/") and req.method == "PATCH":
            if self.fail_append:
                return 502, {}, {"object": "error", "status": 502, "message": "bad gateway"}
            self.appended.append((path, req.json()["children"]))
            return 200, {}, {"object": "list", "results": []}
        if path.endswith("/query") and req.method == "POST":
            return 200, {}, {"object": "list", "results": [], "has_more": False}
        return 404, {}, {"object": "error", "status": 404, "message": f"unhandled {req.method} {path}"}


def _texts(block: dict) -> list[dict]:
    return block[block["type"]].get("rich_text", [])


def _plain(block: dict) -> str:
    return "".join(rt["text"]["content"] for rt in _texts(block))


def _links(block: dict) -> list[str]:
    return [rt["text"]["link"]["url"] for rt in _texts(block) if rt["text"].get("link")]


@unittest.skipIf(nm is None, "requests not installed")
class PublishLinksTest(unittest.TestCase):
    def setUp(self):
        self.backend = _Backend()
        self.server = FakeServer(self.backend)
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.backend.url = self.server.url
        patcher = mock.patch.object(nm, "NOTION_API", self.server.url)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mirror = nm.NotionMirror(NOTION_TOKEN, DB, session=requests.Session())
        self.mirror.disable_llm_callouts = True
        self.kidsnote = requests.Session()

    def report(self, **extra) -> dict:
        base = {"id": 101, "content": "", "date_written": "2026-11-09",
                "author": {"type": "teacher"}, "author_name": "김선생"}
        base.update(extra)
        return base

    def test_oversized_video_is_linked_and_never_downloaded(self):
        big = video(1, 9_482_356, name="2025_05_29 10_07.mp4", base=self.server.url)
        with self.assertLogs("notion_mirror", level="INFO") as logs:
            res = self.mirror.publish_report(self.report(attached_video=big), self.kidsnote)

        self.assertEqual(self.backend.downloads, [], "a file known to be too big must not be fetched")
        self.assertEqual(self.backend.uploads, 0)
        self.assertEqual((res["videos_uploaded"], res["videos_failed"], res["media_links"]), (0, 1, 1))

        children = self.backend.pages[0]["children"]
        heads = [_plain(b) for b in children if b["type"] == "heading_3"]
        self.assertEqual(heads, ["동영상"])
        bullets = [b for b in children if b["type"] == "bulleted_list_item"]
        self.assertEqual(len(bullets), 1)
        self.assertEqual(_links(bullets[0]), [big["high"]])
        text = _plain(bullets[0])
        self.assertIn("2025_05_29 10_07.mp4", text)
        self.assertIn("9.5MB", text)
        self.assertIn("노션 무료 요금제 한도(5MB) 초과", text)
        self.assertNotIn(SECRET_KEY, "\n".join(logs.output))

    def test_small_video_uploads_and_big_one_links_on_the_same_page(self):
        small = video(1, 3_000_000, base=self.server.url)
        big = video(2, 8_250_909, base=self.server.url)
        res = self.mirror.publish_report(self.report(attached_video=[small, big]), self.kidsnote)

        self.assertEqual(self.backend.downloads, [f"/dn/{SECRET_KEY}/1/video_high.mp4"])
        self.assertEqual((res["videos_uploaded"], res["media_links"]), (1, 1))
        types = [b["type"] for b in self.backend.pages[0]["children"]]
        self.assertEqual(types[-4:], ["heading_3", "video", "paragraph", "bulleted_list_item"])

    def test_size_unknown_until_downloaded_still_links(self):
        obj = video(1, 0, base=self.server.url)
        del obj["file_size"]
        self.backend.media[f"/dn/{SECRET_KEY}/1/video_high.mp4"] = b"\0" * (CAP + 1)
        res = self.mirror.publish_report(self.report(attached_video=obj), self.kidsnote)
        self.assertEqual(self.backend.uploads, 0)
        self.assertEqual(res["media_links"], 1)
        bullet = next(b for b in self.backend.pages[0]["children"] if b["type"] == "bulleted_list_item")
        self.assertIn("초과", _plain(bullet))

    def test_download_failure_links_without_leaking_the_url(self):
        obj = video(1, 1_000_000, base=self.server.url)
        self.backend.missing.add(f"/dn/{SECRET_KEY}/1/video_high.mp4")
        with self.assertLogs("notion_mirror", level="WARNING") as logs:
            res = self.mirror.publish_report(self.report(attached_video=obj), self.kidsnote)
        self.assertEqual(res["media_links"], 1)
        bullet = next(b for b in self.backend.pages[0]["children"] if b["type"] == "bulleted_list_item")
        self.assertIn("노션 업로드 실패", _plain(bullet))
        log = "\n".join(logs.output)
        self.assertIn("video_high.mp4", log)
        self.assertNotIn(SECRET_KEY, log)

    def test_oversized_file_on_a_notice_page(self):
        pdf = {"id": 7, "original_file_name": "가정통신문.pdf", "file_size": 12_000_000,
               "original": f"{self.server.url}/dn/{SECRET_KEY}/7/file.pdf"}
        notice = {"id": 202, "title": "11월 안내", "content": "본문", "created": "2026-11-01T09:00:00",
                  "attached_files": [pdf], "author": {"type": "admin"}}
        res = self.mirror.publish_notice(notice, self.kidsnote)
        self.assertEqual((res["files_failed"], res["media_links"]), (1, 1))
        children = self.backend.pages[0]["children"]
        self.assertIn("첨부 파일", [_plain(b) for b in children if b["type"] == "heading_3"])
        bullet = next(b for b in children if b["type"] == "bulleted_list_item")
        self.assertEqual(_links(bullet), [pdf["original"]])

    def test_document_text_goes_into_a_toggle(self):
        from test_doc_text import docx
        small = {"id": 8, "original_file_name": "11월 계획안.docx", "file_size": 3000,
                 "original": f"{self.server.url}/dn/{SECRET_KEY}/8/plan.docx"}
        big = {"id": 9, "original_file_name": "연간 일정.docx", "file_size": 12_000_000,
               "original": f"{self.server.url}/dn/{SECRET_KEY}/9/year.docx"}
        self.backend.media[f"/dn/{SECRET_KEY}/8/plan.docx"] = docx("11/20 드론 체험", "준비물: 원복")
        self.backend.media[f"/dn/{SECRET_KEY}/9/year.docx"] = docx("12월 23일 성탄 행사")
        notice = {"id": 203, "title": "11월 안내", "content": "본문", "created": "2026-11-01T09:00:00",
                  "attached_files": [small, big], "author": {"type": "admin"}}
        res = self.mirror.publish_notice(notice, self.kidsnote)

        # The small file is uploaded, the big one linked; both get their text read.
        self.assertEqual((res["files_uploaded"], res["media_links"], res["doc_texts"]), (1, 1, 2))
        children = self.backend.pages[0]["children"]
        toggles = [b for b in children if b["type"] == "toggle"]
        self.assertEqual([_plain(t) for t in toggles],
                         ["📄 11월 계획안.docx — 글 내용", "📄 연간 일정.docx — 글 내용"])
        inner = [_plain(b) for b in toggles[0]["toggle"]["children"]]
        self.assertEqual(inner[1], "11/20 드론 체험\n준비물: 원복")
        self.assertIn("12월 23일 성탄 행사", _plain(toggles[1]["toggle"]["children"][1]))

    def test_document_text_is_capped_per_page(self):
        toggle = self.mirror._doc_text_toggle("a.hwp", "가" * 50_000, 4000)
        texts = [_plain(b) for b in toggle["toggle"]["children"][1:]]
        self.assertTrue(all(len(t) <= nm.MAX_BLOCK_TEXT for t in texts))
        self.assertTrue(texts[-1].endswith("(이후 내용은 원본 파일에서 확인하세요)"))
        self.assertLess(sum(len(t) for t in texts), 4100)

    def test_download_stops_at_the_byte_cap(self):
        path = f"/dn/{SECRET_KEY}/5/video_high.mp4"
        self.backend.media[path] = b"\0" * 300_000
        sess = requests.Session()
        self.assertIsNone(nm.fetch_capped(sess, self.server.url + path, 100_000, 30))
        self.assertEqual(len(nm.fetch_capped(sess, self.server.url + path, 300_000, 30)), 300_000)

    def test_size_unknown_and_huge_is_linked_without_keeping_it(self):
        obj = video(1, 0, base=self.server.url)
        del obj["file_size"]
        self.backend.media[f"/dn/{SECRET_KEY}/1/video_high.mp4"] = b"\0" * (CAP + 10)
        res = self.mirror.publish_report(self.report(attached_video=obj), self.kidsnote)
        self.assertEqual((res["videos_uploaded"], res["media_links"]), (0, 1))
        bullet = next(b for b in self.backend.pages[0]["children"] if b["type"] == "bulleted_list_item")
        self.assertIn("초과", _plain(bullet))

    def test_failed_fetch_of_an_over_cap_document_still_says_over_the_limit(self):
        doc = {"id": 9, "original_file_name": "연간 일정.hwp", "file_size": 12_000_000,
               "original": f"{self.server.url}/dn/{SECRET_KEY}/9/year.hwp"}
        self.backend.missing.add(f"/dn/{SECRET_KEY}/9/year.hwp")
        notice = {"id": 204, "title": "안내", "content": "", "created": "2026-11-01T09:00:00",
                  "attached_files": [doc], "author": {"type": "admin"}}
        with self.assertLogs("notion_mirror", level="WARNING"):
            self.mirror.publish_notice(notice, self.kidsnote)
        bullet = next(b for b in self.backend.pages[0]["children"] if b["type"] == "bulleted_list_item")
        self.assertIn("노션 무료 요금제 한도(5MB) 초과", _plain(bullet))

    def test_videos_are_still_not_downloaded_for_text(self):
        big = video(1, 9_000_000, base=self.server.url)
        self.mirror.publish_report(self.report(attached_video=big), self.kidsnote)
        self.assertEqual(self.backend.downloads, [])

    def test_no_attachments_no_sections(self):
        self.mirror.publish_report(self.report(content="오늘은 산책"), self.kidsnote)
        types = {b["type"] for b in self.backend.pages[0]["children"]}
        self.assertNotIn("heading_3", types)
        self.assertNotIn("bulleted_list_item", types)

    def test_page_over_100_blocks_is_created_then_appended(self):
        blocks = [self.mirror._para(str(i)) for i in range(250)]
        page = self.mirror._create_page({"이름": {"title": []}}, blocks)
        self.assertTrue(page["id"])
        self.assertEqual(len(self.backend.pages[0]["children"]), 100)
        self.assertEqual([len(c) for _, c in self.backend.appended], [100, 50])
        self.assertEqual(_plain(self.backend.appended[-1][1][-1]), "249")

    def test_half_built_item_page_is_archived_so_the_next_run_retries(self):
        self.backend.fail_append = True
        blocks = [self.mirror._para(str(i)) for i in range(150)]
        with self.assertLogs("notion_mirror", level="WARNING"):
            with self.assertRaises(requests.HTTPError):
                self.mirror._create_page({"이름": {"title": []}}, blocks)
        self.assertEqual(self.backend.archived, [dashed(hexid(501))])

    def test_dashboard_keeps_what_landed_when_an_append_fails(self):
        self.backend.fail_append = True
        blocks = [self.mirror._para(str(i)) for i in range(150)]
        with self.assertLogs("notion_mirror", level="WARNING"):
            page = self.mirror._create_page({"이름": {"title": []}}, blocks, all_or_nothing=False)
        self.assertTrue(page["id"])
        self.assertEqual(self.backend.archived, [])

    def test_how_to_line_shows_once_per_page(self):
        big_video = video(1, 9_000_000, base=self.server.url)
        big_pdf = {"id": 7, "original_file_name": "a.pdf", "file_size": 9_000_000,
                   "original": f"{self.server.url}/dn/{SECRET_KEY}/7/file.pdf"}
        self.mirror.publish_report(self.report(attached_video=big_video, attached_files=[big_pdf]),
                                   self.kidsnote)
        children = self.backend.pages[0]["children"]
        how_to = [b for b in children if _plain(b).startswith("노션에 올리지 못한 파일은")]
        self.assertEqual(len(how_to), 1)
        self.assertEqual(len([b for b in children if b["type"] == "bulleted_list_item"]), 2)

    def test_dashboard_lists_every_oversized_file_newest_first(self):
        reports = [
            {"id": 1, "date_written": "2025-05-29", "attached_video": video(1, 9_482_356),
             "attached_images": [{"id": 1}, {"id": 2}]},
            {"id": 2, "date_written": "2026-11-09", "attached_video": video(2, 5_946_685)},
            {"id": 3, "date_written": "2026-01-01", "attached_video": video(3, 4_000_000)},
        ] + [{"id": 100 + i, "date_written": f"2024-01-{i % 28 + 1:02d}",
              "attached_video": video(100 + i, 6_000_000)} for i in range(120)]
        albums = [{"id": 900, "created": "2026-03-03T10:00:00", "attached_video": video(900, 7_000_000)}]
        att = nm.attachment_stats([("알림장", reports), ("공지", []), ("앨범", albums)],
                                  {2: dashed(hexid(77))}, CAP)
        self.assertEqual((att["images"], att["videos"], att["files"]), (2, 124, 0))
        self.assertEqual(len(att["oversized"]), 123)

        self.mirror.publish_dashboard({"reports_total": 123, "attachments": att})
        all_blocks = self.backend.pages[0]["children"] + [b for _, c in self.backend.appended for b in c]
        self.assertTrue(len(all_blocks) > 100, "the list must survive Notion's 100-block create limit")
        texts = [_plain(b) for b in all_blocks]
        self.assertIn("🎬 동영상 124 개  (5MB가 넘어 원본 링크로 남긴 123 개 포함)", texts)
        self.assertIn("📥 노션에 못 올린 파일 123개", texts)
        bullets = [b for b in all_blocks if b["type"] == "bulleted_list_item"]
        self.assertEqual(len(bullets), 123)
        self.assertTrue(_plain(bullets[0]).startswith("2026-11-09 · 알림장 · 🎬 clip_2.mp4"))
        self.assertEqual(_links(bullets[0]), [nm.notion_page_url(dashed(hexid(77))), video(2, 0)["high"]])
        self.assertTrue(_plain(bullets[1]).startswith("2026-03-03 · 앨범"))
        self.assertEqual(_links(bullets[1]), [video(900, 0)["high"]])  # no page known → no page link

    def test_dashboard_without_oversized_files_has_no_list(self):
        att = nm.attachment_stats([("알림장", [{"id": 1, "attached_video": video(1, 10)}])], {}, CAP)
        self.mirror.publish_dashboard({"reports_total": 1, "attachments": att})
        texts = [_plain(b) for b in self.backend.pages[0]["children"]]
        self.assertIn("🎬 동영상 1 개", texts)
        self.assertFalse(any(t.startswith("📥") for t in texts))


@unittest.skipIf(nm is None, "requests not installed")
class GuideMatchesDashboardTest(unittest.TestCase):
    """The README sends people to the list by its on-screen name."""

    def test_list_heading_quoted_in_readme_is_what_the_dashboard_shows(self):
        readme = (HERE.parents[2] / "README.md").read_text(encoding="utf-8")
        mirror = nm.NotionMirror(NOTION_TOKEN, DB, session=requests.Session())
        heading = _plain(mirror._oversized_list_blocks([{"kind": "video", "name": "a.mp4"}], "5MB")[0])
        self.assertTrue(heading.startswith("📥 노션에 못 올린 파일"))
        self.assertIn("**📥 노션에 못 올린 파일**", readme)
        self.assertIn("📊 통계 대시보드", nm.NotionMirror.DASHBOARD_TITLE)
        self.assertNotIn("유료 플랜", readme, "paid plans hit the same 5MB cap in this code")


@unittest.skipIf(nm is None, "requests not installed")
class HelpersTest(unittest.TestCase):
    def test_dashboard_links_only_pages_that_still_exist(self):
        before = {1: "old-1", 2: "old-2"}
        results = [{"report_id": 1, "page_id": "new-1"}, {"item_id": 9, "page_id": "new-9"},
                   {"report_id": 3, "page_id": ""}]
        self.assertEqual(fetch._live_page_ids(before, results, False),
                         {1: "new-1", 2: "old-2", 9: "new-9"})
        # --force-refresh archives the pre-run pages, so none of them may be linked.
        self.assertEqual(fetch._live_page_ids(before, results, True), {1: "new-1", 9: "new-9"})

    def test_sizes_and_labels(self):
        self.assertEqual(nm.format_mb(5_946_685), "5.9MB")
        self.assertEqual(nm.format_mb(150_000_000), "150MB")
        self.assertEqual(nm.cap_label(5_000_000), "5MB")

    def test_declared_size_rejects_junk(self):
        for junk in (None, "123", -1, True, 1.5):
            self.assertIsNone(nm.declared_size({"file_size": junk}))
        self.assertEqual(nm.declared_size({"file_size": 0}), 0)

    def test_video_has_no_original_key(self):
        self.assertTrue(nm.media_url(video(1, 1)).endswith("video_high.mp4"))
        self.assertEqual(nm.media_url({}), "")

    def test_only_plain_web_links_are_linked(self):
        self.assertTrue(nm._is_linkable("https://up-kids-kage.kakao.com/dn/a/b/c.mp4"))
        for bad in ("", "javascript:alert(1)", "https://a b", "ftp://x", "https://x/" + "a" * 2000):
            self.assertFalse(nm._is_linkable(bad))

    def test_logs_never_carry_the_access_key(self):
        url = f"https://up-kids-kage.kakao.com/dn/{SECRET_KEY}/img.jpg?token=abc"
        for mod in (nm, fetch):
            self.assertEqual(mod._safe_url(url), "up-kids-kage.kakao.com/…/img.jpg")
            err = requests.HTTPError(f"404 Client Error: Not Found for url: {url}")
            self.assertNotIn(SECRET_KEY, mod._safe_err(err))
            self.assertIn("404 Client Error", mod._safe_err(err))


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
