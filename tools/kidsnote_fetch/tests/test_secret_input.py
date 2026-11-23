"""Tests for secret_input.py: setup values the way people really paste them."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import secret_input as si  # noqa: E402
from secret_input import clean_secret, mask_name, normalize_notion_id, normalize_token  # noqa: E402

ID = "238f5e29c0894adfb6c4d8e1a5b2c3d4"
DASHED = "238f5e29-c089-4adf-b6c4-d8e1a5b2c3d4"
VIEW = "abcdef1234567890abcdef1234567890"


class CleanSecretTest(unittest.TestCase):
    def test_strips_whitespace_invisible_characters_and_quotes(self):
        for raw in ("value", " value\n", "﻿value", "val​ue", '"value"', "' value '", "\r\nvalue\t"):
            with self.subTest(raw=raw):
                self.assertEqual(clean_secret(raw), "value")

    def test_empty(self):
        self.assertEqual(clean_secret(None), "")
        self.assertEqual(clean_secret("  \n"), "")
        self.assertEqual(clean_secret('""'), "")


class NormalizeTokenTest(unittest.TestCase):
    def test_common_paste_forms(self):
        for raw in ("ntn_abc123", " ntn_abc123\n", "﻿ntn_abc123", "Bearer ntn_abc123", "bearer  ntn_abc123 "):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_token(raw), "ntn_abc123")
        self.assertEqual(normalize_token("secret_legacy"), "secret_legacy")


class NormalizeNotionIdTest(unittest.TestCase):
    def test_accepts_every_form_a_user_might_paste(self):
        cases = [
            ID, ID.upper(), DASHED, f"  {ID}\n", f'"{ID}"', f"﻿{ID}", f"{ID}?v={VIEW}",
            f"https://www.notion.so/{ID}?v={VIEW}",
            f"https://www.notion.so/myspace/{ID}?v={VIEW}&pvs=4",
            f"https://www.notion.so/키즈노트-백업-{ID}?source=copy_link",
            f"https://www.notion.so/%ED%82%A4%EC%A6%88-{ID}",
            f"https://www.notion.so/Kidsnote-Backup-{ID}#{VIEW}",
            f"https://app.notion.com/p/{ID}?pvs=204",
            f"https://someone.notion.site/Backup-{ID}",
            f"notion.so/{ID}",
            f"www.notion.so/myspace/{DASHED}",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_notion_id(raw), ID)

    def test_view_id_in_query_never_wins(self):
        self.assertEqual(normalize_notion_id(f"https://www.notion.so/{ID}?v={VIEW}"), ID)
        self.assertEqual(normalize_notion_id(f"{DASHED}?v={VIEW}"), ID)

    def test_no_id(self):
        for raw in ("", None, "  ", "not an id", "https://www.notion.so/", "12345", f"?v={VIEW}"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_notion_id(raw), "")


class ChildNameTest(unittest.TestCase):
    KIDS = [{"id": 1, "name": "송하정"}, {"id": 2, "name": "송다정"}, {"id": 3, "name": "김민준 "}]

    def ids(self, wanted):
        outcome, kids = si.match_children(self.KIDS, wanted)
        return outcome, [k["id"] for k in kids]

    def test_name_key_unifies_how_a_name_can_be_typed(self):
        import unicodedata
        for variant in ("송하정", unicodedata.normalize("NFD", "송하정"), " 송 하정 ", "송​하정",
                        "﻿송하정\n", '"송하정"'):
            self.assertEqual(si.name_key(variant), "송하정", repr(variant))
        self.assertEqual(si.name_key("Ｈａｒｉｎ"), "harin")  # full-width, any case

    def test_outcomes(self):
        self.assertEqual(self.ids(""), ("empty", []))
        self.assertEqual(self.ids("하정"), ("one", [1]))
        self.assertEqual(self.ids("민준"), ("one", [3]))
        self.assertEqual(self.ids("정"), ("many", [1, 2]))
        self.assertEqual(self.ids("서연"), ("none", []))
        self.assertEqual(self.ids("송하정, 송다정"), ("several", [1, 2]))
        self.assertEqual(self.ids("송하정/김민준"), ("several", [1, 3]))
        self.assertEqual(self.ids("송하정 송다정"), ("several", [1, 2]))

    def test_labels_never_show_a_full_name(self):
        self.assertEqual(si.child_label({"name": "송하정", "date_birth": "2019-03-02"}), "송*정(2019년생)")
        self.assertEqual(si.child_label({"name": "송하정"}), "송*정")
        import unicodedata
        self.assertEqual(si.masked_value(unicodedata.normalize("NFD", "송하정")), "송*정")


class MaskNameTest(unittest.TestCase):
    def test_masks_the_middle(self):
        self.assertEqual(mask_name("우하린"), "우*린")
        self.assertEqual(mask_name("정에스더"), "정**더")
        self.assertEqual(mask_name("하린"), "하*")
        self.assertEqual(mask_name("린"), "*")
        self.assertEqual(mask_name(" 우하린\n"), "우*린")

    def test_empty(self):
        self.assertEqual(mask_name(""), "")
        self.assertEqual(mask_name(None), "")


if __name__ == "__main__":
    unittest.main()
