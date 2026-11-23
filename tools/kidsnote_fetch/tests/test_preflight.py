"""Tests for preflight.py: kidsnote login, child name and Notion access, checked before a run."""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import kidsnote_auth as ka  # noqa: E402
import notion_check as nc  # noqa: E402
import preflight  # noqa: E402
from fake_http import FakeServer  # noqa: E402
from fake_kidsnote import SESSIONID, FakeKidsnote  # noqa: E402
from fake_notion import NOTION_TOKEN, FakeNotion, hexid  # noqa: E402

try:
    import fetch
except ImportError:  # pragma: no cover - requests not installed
    fetch = None

USER, PASSWORD = "parent01", "Secret!23"
PAGE, DB = hexid(1), hexid(2)
SETTINGS = ("KIDSNOTE_USERNAME", "KIDSNOTE_PASSWORD", "KIDSNOTE_SESSION_COOKIE",
            "KIDSNOTE_CHILD_NAME", "NOTION_TOKEN", "NOTION_DATABASE_ID")


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.kn = FakeKidsnote({USER: PASSWORD})
        self.kn.children = [{"id": 11, "name": "우하린"}]
        self.notion = FakeNotion()
        self.notion.add_page(PAGE)
        self.notion.add_database(DB)
        self.kn_server, self.notion_server = FakeServer(self.kn), FakeServer(self.notion)
        for server in (self.kn_server, self.notion_server):
            server.__enter__()
            self.addCleanup(server.__exit__, None, None, None)
        for patcher in (mock.patch.object(ka, "KIDSNOTE_BASE", self.kn_server.url),
                        mock.patch.object(ka, "_sleep", lambda s: None),
                        mock.patch.object(nc, "NOTION_API", self.notion_server.url),
                        mock.patch.object(nc, "_sleep", lambda s: None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_main(self, *, github=True, **overrides):
        settings = {"KIDSNOTE_USERNAME": USER, "KIDSNOTE_PASSWORD": PASSWORD, "KIDSNOTE_CHILD_NAME": "하린",
                    "NOTION_TOKEN": NOTION_TOKEN,
                    "NOTION_DATABASE_ID": f"https://www.notion.so/키즈노트-백업-{PAGE}?source=copy_link"}
        settings.update(overrides)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env_file, out_file = Path(tmp.name, "env"), Path(tmp.name, "output")
        env_file.touch()
        out_file.touch()
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_ENV": str(env_file), "GITHUB_OUTPUT": str(out_file)}):
            for key in SETTINGS:
                os.environ.pop(key, None)
            os.environ.update({k: v for k, v in settings.items() if v is not None})
            with redirect_stdout(buf):
                code = preflight.main(["--github"] if github else [])
        outputs = dict(line.split("=", 1) for line in out_file.read_text(encoding="utf-8").splitlines()
                       if "=" in line)
        return code, buf.getvalue(), env_file.read_text(encoding="utf-8"), outputs

    def assertFailed(self, outputs, env, reason):
        self.assertEqual(outputs.get("ok"), "false")
        self.assertEqual(outputs.get("reason"), reason)
        self.assertTrue(outputs.get("hint"))
        self.assertEqual(env, "")

    def test_everything_ok_with_a_page_link(self):
        code, out, env, outputs = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(outputs, {"ok": "true"})
        self.assertEqual(env, f"KIDSNOTE_SESSION_COOKIE={SESSIONID}\n")
        self.assertTrue(out.startswith(f"::add-mask::{SESSIONID}\n"))
        self.assertIn("자녀 확인 OK: 우*린", out)
        self.assertIn("노션 연결 OK (빈 페이지", out)
        for secret in ("우하린", PASSWORD, NOTION_TOKEN):
            self.assertNotIn(secret, out)

    def test_page_that_already_holds_the_backup_table(self):
        holder = hexid(8)
        self.notion.add_page(holder)
        self.notion.add_database(hexid(9), parent_page=holder)
        _, out, _, outputs = self.run_main(NOTION_DATABASE_ID=f"https://app.notion.com/p/{holder}")
        self.assertEqual(outputs, {"ok": "true"})
        self.assertIn("노션 연결 OK (페이지 안의 데이터베이스를 사용합니다)", out)

    def test_database_link(self):
        _, out, _, outputs = self.run_main(NOTION_DATABASE_ID=DB)
        self.assertEqual(outputs, {"ok": "true"})
        self.assertIn("노션 연결 OK (데이터베이스)", out)

    def test_child_name_is_forgiving_about_spaces(self):
        self.assertEqual(self.run_main(KIDSNOTE_CHILD_NAME=" ﻿하린\n")[3], {"ok": "true"})

    def test_single_child_without_a_name_is_fine(self):
        self.assertEqual(self.run_main(KIDSNOTE_CHILD_NAME=None)[3], {"ok": "true"})

    def test_child_name_that_matches_nobody_lists_masked_names(self):
        self.kn.children = [{"id": 11, "name": "우하린"}, {"id": 12, "name": "우하준"}]
        code, out, env, outputs = self.run_main(KIDSNOTE_CHILD_NAME="민준")
        self.assertEqual(code, 0)
        self.assertFailed(outputs, env, "child_not_found")
        self.assertIn("우*린, 우*준", outputs["hint"])
        self.assertNotIn("우하린", out + outputs["hint"])

    def test_issue_3_three_children_with_look_alike_names(self):
        """Issue #3: three children shown as 송*정, 송*정, 송*정 and nothing matched."""
        import unicodedata
        self.kn.children = [{"id": 1, "name": "송하정", "date_birth": "2019-03-02"},
                            {"id": 2, "name": "송다정", "date_birth": "2021-05-01"},
                            {"id": 3, "name": "송예정", "date_birth": "2023-01-09"}]
        # Korean copied on a Mac is decomposed (NFD) but must still match.
        self.assertEqual(self.run_main(KIDSNOTE_CHILD_NAME=unicodedata.normalize("NFD", "다정"))[3], {"ok": "true"})
        self.assertEqual(self.run_main(KIDSNOTE_CHILD_NAME="송 예정")[3], {"ok": "true"})
        # All three names in one slot: say so, and point to the numbered slots.
        for value in ("송하정, 송다정, 송예정", "송하정 송다정"):
            _, out, env, outputs = self.run_main(KIDSNOTE_CHILD_NAME=value)
            self.assertFailed(outputs, env, "child_name_multiple")
            self.assertIn("KIDSNOTE_CHILD_NAME_2", outputs["hint"])
        # Not found: show the masked value written and tell the children apart by birth year.
        _, out, env, outputs = self.run_main(KIDSNOTE_CHILD_NAME="민준")
        self.assertFailed(outputs, env, "child_not_found")
        self.assertIn("적은 값: 민*", outputs["hint"])
        self.assertIn("송*정(2019년생), 송*정(2021년생), 송*정(2023년생)", outputs["hint"])
        self.assertNotIn("송하정", out + outputs["hint"])

    def test_ambiguous_child_name(self):
        self.kn.children = [{"id": 11, "name": "우하린"}, {"id": 12, "name": "우하준"}]
        _, _, env, outputs = self.run_main(KIDSNOTE_CHILD_NAME="우하")
        self.assertFailed(outputs, env, "child_ambiguous")

    def test_two_children_need_a_name(self):
        self.kn.children = [{"id": 11, "name": "우하린"}, {"id": 12, "name": "우하준"}]
        _, _, env, outputs = self.run_main(KIDSNOTE_CHILD_NAME="")
        self.assertFailed(outputs, env, "child_name_required")

    def test_account_without_children(self):
        self.kn.children = []
        _, _, env, outputs = self.run_main()
        self.assertFailed(outputs, env, "child_none")

    def test_kidsnote_failure_stops_before_notion(self):
        _, _, env, outputs = self.run_main(KIDSNOTE_PASSWORD="wrong")
        self.assertFailed(outputs, env, "invalid_credentials")
        self.assertEqual(outputs["hint"], ka.HINTS["invalid_credentials"])
        self.assertEqual(self.notion_server.requests, [])

    def test_notion_failures_after_a_good_login(self):
        for overrides, reason in (({"NOTION_TOKEN": "ntn_wrong"}, "notion_token_invalid"),
                                  ({"NOTION_DATABASE_ID": hexid(99)}, "notion_no_access"),
                                  ({"NOTION_DATABASE_ID": "그냥 글자"}, "notion_bad_id"),
                                  ({"NOTION_TOKEN": None}, "notion_missing")):
            with self.subTest(reason=reason):
                _, _, env, outputs = self.run_main(**overrides)
                self.assertFailed(outputs, env, reason)
                self.assertEqual(outputs["hint"], nc.HINTS[reason])

    def test_crash_is_reported_not_raised(self):
        with mock.patch.object(preflight, "run_checks", side_effect=ValueError("boom")):
            code, _, env, outputs = self.run_main()
            self.assertEqual(code, 0)
            self.assertFailed(outputs, env, "unexpected")
            self.assertEqual(self.run_main(github=False)[0], 1)

    def test_local_mode_exit_codes(self):
        self.assertEqual(self.run_main(github=False)[0], 0)
        self.assertEqual(self.run_main(github=False, KIDSNOTE_CHILD_NAME="민준")[0], 1)


class PickChildTest(unittest.TestCase):
    CHILDREN = [{"id": 11, "name": "우하린"}, {"id": 12, "name": "정에스더"}]

    def test_substring_and_case_insensitive(self):
        self.assertEqual(preflight.pick_child(self.CHILDREN, "하린")["id"], 11)
        self.assertEqual(preflight.pick_child(self.CHILDREN, "스더")["id"], 12)
        self.assertEqual(preflight.pick_child([{"id": 1, "name": "Emma Kim"}], "emma")["id"], 1)

    @unittest.skipIf(fetch is None, "fetch.py dependencies not installed")
    def test_same_choice_as_the_backup_itself(self):
        for name in ("하린", "우하린", "스더", "린", "김", "우", "에"):
            with self.subTest(name=name):
                try:
                    expected = fetch._pick_child(self.CHILDREN, None, name, None)["id"]
                except SystemExit:
                    expected = None
                try:
                    got = preflight.pick_child(self.CHILDREN, name)["id"]
                except preflight.CheckFailed:
                    got = None
                self.assertEqual(got, expected)


if __name__ == "__main__":
    unittest.main()
