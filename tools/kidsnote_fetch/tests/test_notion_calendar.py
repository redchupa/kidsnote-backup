"""📅 키즈노트 일정: a persistent calendar database kept in step with 📋."""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import supplies as sp  # noqa: E402

try:
    import requests

    import notion_calendar as nc
    import notion_mirror as nm
except ImportError:  # pragma: no cover - requests not installed
    nm = None

from fake_http import FakeServer  # noqa: E402
from fake_notion import NOTION_TOKEN, dashed, hexid  # noqa: E402

BACKUP, PAGE = hexid(2), hexid(1)
TODAY = date(2026, 11, 18)


class FakeCalendarNotion:
    def __init__(self) -> None:
        self.page_children: list[dict] = [{"object": "block", "id": dashed(BACKUP), "type": "child_database",
                                           "child_database": {"title": "키즈노트 백업"}}]
        self.databases: dict[str, dict] = {}
        self.rows: dict[str, list[dict]] = {}
        self.archived: list[str] = []
        self.views: dict[str, list[str]] = {}
        self.created_views: list[dict] = []
        self.versions: dict[str, str] = {}
        self.parent_type = "page_id"
        self.page_status = 200
        self.views_status = 200
        self._n = 100

    def _id(self) -> str:
        self._n += 1
        return dashed(hexid(self._n))

    def __call__(self, req):
        path, method = req.path, req.method
        self.versions[f"{method} {path.split('/')[1]}"] = req.headers.get("notion-version", "")
        if method == "GET" and path == f"/databases/{BACKUP}":
            return 200, {}, {"object": "database", "id": dashed(BACKUP),
                             "parent": {"type": self.parent_type, self.parent_type: dashed(PAGE)}}
        if method == "GET" and path == f"/blocks/{dashed(PAGE)}/children":
            if self.page_status != 200:
                return self.page_status, {}, {"object": "error", "status": self.page_status}
            return 200, {}, {"results": self.page_children, "has_more": False}
        if method == "POST" and path == "/databases":
            body = req.json()
            db_id = self._id()
            props = {name: {"id": f"p{i}", "type": next(iter(cfg))} for i, (name, cfg)
                     in enumerate(body["properties"].items())}
            self.databases[db_id] = {"description": body["description"], "properties": props}
            self.rows[db_id] = []
            self.views[db_id] = ["default-view"]
            self.page_children.append({"object": "block", "id": db_id, "type": "child_database",
                                       "child_database": {"title": body["title"][0]["text"]["content"]}})
            return 200, {}, {"object": "database", "id": db_id, "properties": props}
        if method == "GET" and path.startswith("/databases/"):
            db_id = path.split("/")[2]
            db = self.databases[db_id]
            desc = [{"plain_text": d["text"]["content"]} for d in db["description"]]
            return 200, {}, {"object": "database", "id": db_id, "description": desc,
                             "data_sources": [{"id": "ds-" + db_id, "name": "x"}]}
        if method == "GET" and path == "/views":
            db_id = req.query["database_id"][0]
            return self.views_status, {}, {"results": [{"object": "view", "id": v} for v in self.views[db_id]]}
        if method == "POST" and path == "/views":
            body = req.json()
            if self.views_status != 200:
                return self.views_status, {}, {"object": "error"}
            self.created_views.append(body)
            self.views[body["database_id"]].append("cal-view")
            return 200, {}, {"object": "view", "id": "cal-view"}
        if method == "DELETE" and path.startswith("/views/"):
            vid = path.split("/")[2]
            for v in self.views.values():
                if vid in v:
                    v.remove(vid)
            return 200, {}, {"object": "view", "id": vid}
        if method == "POST" and path.endswith("/query"):
            db_id = path.split("/")[2]
            return 200, {}, {"results": [r for r in self.rows[db_id] if not r.get("archived")],
                             "has_more": False}
        if method == "POST" and path == "/pages":
            body = req.json()
            db_id = body["parent"]["database_id"]
            props = body["properties"]
            row = {"id": self._id(), "properties": {
                k: ({"rich_text": [{"plain_text": t["text"]["content"]} for t in v["rich_text"]]}
                    if "rich_text" in v else v) for k, v in props.items()}, "sent": props}
            self.rows[db_id].append(row)
            return 200, {}, {"object": "page", "id": row["id"]}
        if method == "PATCH" and path.startswith("/pages/"):
            pid = path.split("/")[2]
            self.archived.append(pid)
            for rows in self.rows.values():
                for r in rows:
                    if r["id"] == pid:
                        r["archived"] = True
            return 200, {}, {"object": "page"}
        return 404, {}, {"object": "error", "message": f"unhandled {method} {path}"}


def ev(day, text, item_id=1, posted=date(2026, 11, 13), end=None, page_id=""):
    start = date(2026, 11, day)
    return sp.Event(start, end or start, text, "공지", posted, item_id, page_id)


@unittest.skipIf(nm is None, "requests not installed")
class CalendarSyncTest(unittest.TestCase):
    def setUp(self):
        self.notion = FakeCalendarNotion()
        self.server = FakeServer(self.notion)
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        p = mock.patch.object(nm, "NOTION_API", self.server.url)
        p.start()
        self.addCleanup(p.stop)
        self.mirror = nm.NotionMirror(NOTION_TOKEN, BACKUP, session=requests.Session())

    def sync(self, events, read_ids=frozenset({1, 2}), titles=None, read=None):
        board = sp.Board(today=TODAY, events=events)
        read = read if read is not None else {(i, "공지") for i in read_ids}
        return self.mirror.sync_calendar(board, titles or {1: "11월 3주 가정통신문"}, read)

    def rows(self, db_id):
        return [r for r in self.notion.rows[db_id] if not r.get("archived")]

    def test_first_run_creates_database_with_a_calendar_view(self):
        with self.assertLogs("notion_calendar", level="INFO"):
            db_id = self.sync([ev(20, "드론과 놀아요", page_id="a" * 32), ev(18, "축구교실")])
        self.assertTrue(db_id)
        view = self.notion.created_views[0]
        self.assertEqual((view["type"], view["configuration"]["type"]), ("calendar", "calendar"))
        self.assertEqual(view["configuration"]["date_property_id"], "p1")  # the 날짜 property
        self.assertEqual(view["data_source_id"], "ds-" + db_id)
        self.assertEqual(self.notion.views[db_id], ["cal-view"])  # default table view removed
        self.assertEqual(self.notion.versions["POST views"], nc.VIEWS_API_VERSION)
        self.assertEqual(self.notion.versions["POST pages"], nm.NOTION_VERSION)
        sent = sorted(r["sent"]["날짜"]["date"]["start"] for r in self.rows(db_id))
        self.assertEqual(sent, ["2026-11-18", "2026-11-20"])
        drone = next(r["sent"] for r in self.rows(db_id) if r["sent"]["날짜"]["date"]["start"] == "2026-11-20")
        src = drone["출처"]["rich_text"][0]["text"]
        self.assertEqual((src["content"], src["link"]["url"]), ("공지: 11월 3주 가정통신문", nm.notion_page_url("a" * 32)))

    def test_later_runs_reuse_the_database_and_add_only_new_events(self):
        first = self.sync([ev(20, "드론과 놀아요")])
        second = self.sync([ev(20, "드론과 놀아요"), ev(25, "김장 체험", item_id=2, end=date(2026, 11, 26))])
        self.assertEqual(first, second)
        self.assertEqual(len(self.notion.created_views), 1)
        rows = self.rows(first)
        self.assertEqual(len(rows), 2)
        kimjang = next(r["sent"] for r in rows if r["sent"]["이름"]["title"][0]["text"]["content"] == "김장 체험")
        self.assertEqual(kimjang["날짜"]["date"], {"start": "2026-11-25", "end": "2026-11-26"})

    def test_edited_future_event_is_replaced_but_history_and_unread_posts_stay(self):
        db_id = self.sync([ev(20, "드론과 놀아요"), ev(25, "현장학습", item_id=2), ev(28, "발표회", item_id=3)],
                          read_ids={1, 2, 3})
        # Next run: post 1's date was edited, post 2 was not read (fetch failed), post 3 unchanged.
        self.sync([ev(21, "드론과 놀아요"), ev(28, "발표회", item_id=3)], read_ids={1, 3})
        names = sorted((r["sent"]["날짜"]["date"]["start"], r["sent"]["이름"]["title"][0]["text"]["content"])
                       for r in self.rows(db_id))
        self.assertEqual(names, [("2026-11-21", "드론과 놀아요"), ("2026-11-25", "현장학습"),
                                 ("2026-11-28", "발표회")])
        self.assertEqual(len(self.notion.archived), 1)

    def test_unread_attachment_keeps_its_events(self):
        doc = sp.Event(date(2026, 11, 27), date(2026, 11, 27), "김장 체험", "첨부(계획.hwp)",
                       date(2026, 11, 13), 1)
        db_id = self.sync([ev(20, "드론과 놀아요"), doc], read={(1, "공지"), (1, "첨부(계획.hwp)")})
        # Next run the post body is read but the attachment is not (cap / download error).
        self.sync([ev(20, "드론과 놀아요")], read={(1, "공지")})
        self.assertEqual(len(self.rows(db_id)), 2)
        self.assertEqual(self.notion.archived, [])

    def test_renamed_calendar_is_still_found(self):
        db_id = self.sync([ev(20, "드론과 놀아요")])
        for blk in self.notion.page_children:
            if blk["id"] == db_id:
                blk["child_database"]["title"] = "하린이 일정"
        self.assertEqual(self.sync([ev(20, "드론과 놀아요")]), db_id)
        self.assertEqual(len(self.notion.created_views), 1)

    def test_failed_lookup_skips_instead_of_creating_a_duplicate(self):
        db_id = self.sync([ev(20, "드론과 놀아요")])
        real_call = self.notion.__call__

        def flaky(req):
            if req.method == "GET" and req.path == f"/databases/{db_id}":
                return 502, {}, {"object": "error", "status": 502}
            return real_call(req)

        self.server.handler = flaky
        with self.assertRaises(RuntimeError):
            self.sync([ev(21, "새 일정")])
        self.assertEqual(sum(1 for b in self.notion.page_children if b["type"] == "child_database"), 2)

    def test_past_events_are_never_archived(self):
        db_id = self.sync([ev(18, "오늘 행사")])
        board = sp.Board(today=date(2026, 11, 25), events=[])
        self.mirror.sync_calendar(board, {}, {(1, "공지")})
        self.assertEqual(len(self.rows(db_id)), 1)

    def test_another_backups_calendar_on_the_same_page_is_not_used(self):
        other = self.notion._id()
        self.notion.databases[other] = {"description": [{"text": {"content": "(백업 ffffffff)"}}], "properties": {}}
        self.notion.page_children.append({"object": "block", "id": other, "type": "child_database",
                                          "child_database": {"title": nc.CALENDAR_TITLE}})
        db_id = self.sync([ev(20, "드론과 놀아요")])
        self.assertNotEqual(db_id, other)
        self.assertEqual(self.notion.rows.get(other, []), [])

    def test_no_page_access_or_no_parent_page_skips_quietly(self):
        self.notion.page_status = 404
        with self.assertLogs("notion_calendar", level="INFO") as logs:
            self.assertIsNone(self.sync([ev(20, "드론과 놀아요")]))
        self.assertIn("connect the Notion connection", logs.output[-1])
        self.notion.page_status, self.notion.parent_type = 200, "workspace"
        self.assertIsNone(self.sync([ev(20, "드론과 놀아요")]))

    def test_views_api_failure_leaves_a_working_table(self):
        self.notion.views_status = 400
        db_id = self.sync([ev(20, "드론과 놀아요")])
        self.assertTrue(db_id)
        self.assertEqual(self.notion.views[db_id], ["default-view"])
        self.assertEqual(len(self.rows(db_id)), 1)

    def test_supplies_page_links_the_calendar(self):
        captured = {}
        with mock.patch.object(nm.NotionMirror, "_replace_singleton",
                               lambda self, rid, title, blocks: captured.setdefault("b", blocks)):
            self.mirror.publish_supplies(sp.Board(today=TODAY), {}, "cal-id")
        para = captured["b"][1]["paragraph"]["rich_text"]
        self.assertEqual(para[1]["text"]["link"]["url"], nm.notion_page_url("cal-id"))
        self.assertEqual(para[1]["text"]["content"], nc.CALENDAR_TITLE)
        self.assertEqual(captured["b"][1]["type"], "paragraph")  # link_to_page rejects inline DBs


class GuideTest(unittest.TestCase):
    def test_readme_names_the_calendar_as_created(self):
        readme = (HERE.parents[2] / "README.md").read_text(encoding="utf-8")
        if nm is None:
            self.skipTest("requests not installed")
        self.assertIn(f"- {nc.CALENDAR_TITLE} —", readme)


class KeyTest(unittest.TestCase):
    def test_key_carries_the_post_id_and_changes_with_the_event(self):
        a = nc.event_key(ev(20, "드론")) if nm else None
        if a is None:
            self.skipTest("requests not installed")
        self.assertEqual(nc.key_source(a), nc.source_id(1, "공지"))
        self.assertNotEqual(a, nc.event_key(ev(21, "드론")))
        self.assertNotEqual(a, nc.event_key(ev(20, "드론 체험")))
        self.assertIsNone(nc.key_source("garbage"))
        self.assertIsNone(nc.key_source("x-y-z"))


if __name__ == "__main__":
    unittest.main()
