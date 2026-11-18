"""📋 준비물·일정: quote-only extraction of requests and dated events.

Sentences below are the wording real teachers / centers used (checked
2026-11-18 on 438 reports + 100 notices), with names swapped out.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import supplies as sp  # noqa: E402

try:
    import notion_mirror as nm
except ImportError:  # pragma: no cover - requests not installed
    nm = None

NOV13 = date(2026, 11, 13)


class RequestSentenceTest(unittest.TestCase):
    def test_real_requests_are_found(self):
        for line in (
            "수건 보내주세요~",
            "우리 아이 턱받이, 수건 보내 주세요.~",
            "칫솔 6개와 치약 1개를 보내주시면 감사하겠습니다.💛",
            "다음 주 월요일 등원 시 이불 보내주세요~",
            "가족 사진 부탁드립니다~",
            "투약의뢰서 부탁드립니다",
            "🌳10월 행사비 입금 부탁드립니다.",
            "* 준비물 : 원복",
            "마지막으로 2학기 준비물 안내드립니다.",
            "공지사항의 가정통신문에 준비물을 참고하여 주시기 바랍니다~",
            "10월 13일(화)까지 체크해주시면 감사하겠습니다.",
        ):
            self.assertEqual(sp.request_sentences(line), [line], line)

    def test_narrative_and_courtesy_lines_are_not_requests(self):
        for line in (
            "오늘은 부모님께서 보내주신 자동차를 타고 신나는 놀이 시간을 가져보았습니다.",
            "따뜻하게 챙겨 입으시고 감기 조심하세요.",
            "펀아트 준비물도 함께 살펴보았습니다.",
            "작은 동생들에게 장난감을 챙겨 주는 모습이 너무 사랑스러웠어요.",
            "많은 칭찬과 응원 부탁드립니다 😊",
            "잘 부탁드립니다 🤍",
            "학부모님들의 각별한 주의와 협조 부탁드립니다.",
            "10월 20일 축구교실 체험 정산 내역을 보내드립니다.",
            "이번 주도 잘 지낼 수 있도록 많은 도움 부탁드립니다.",
            "즐거운 주말 보내시길 부탁드립니다.",
            "보내주신 약은 복용하지 않고 가방 앞주머니에 잘 보관해 두었습니다.",
        ):
            self.assertEqual(sp.request_sentences(line), [], line)

    def test_sentences_are_split_and_quoted_as_written(self):
        text = "오늘은 산책을 했어요. 내일 우비 보내주세요~ 감사합니다."
        self.assertEqual(sp.request_sentences(text), ["내일 우비 보내주세요~"])

    def test_long_lines_are_clipped_at_a_word(self):
        out = sp.request_sentences(("가 " * 200) + "보내주세요")
        self.assertEqual(out, [])  # over 400 chars: a paragraph, not a request line
        short = sp.clip("단어 " * 200, 50)
        self.assertTrue(short.endswith(" …") and len(short) <= 52)


class DateTest(unittest.TestCase):
    def dates(self, line, posted=NOV13):
        return [(a, b) for a, b, _ in sp.find_dates(line, posted)]

    def test_korean_numeric_and_ranges(self):
        d = date
        self.assertEqual(self.dates("🍂11월 16일(월)"), [(d(2026, 11, 16),) * 2])
        self.assertEqual(self.dates("11/18~11/20 체험학습"), [(d(2026, 11, 18), d(2026, 11, 20))])
        self.assertEqual(self.dates("11월 18일~20일"), [(d(2026, 11, 18), d(2026, 11, 20))])
        self.assertEqual(self.dates("10월 28일(수)~29일(목), 이틀간 공사", date(2026, 10, 28)),
                         [(d(2026, 10, 28), d(2026, 10, 29))])
        self.assertEqual(self.dates("11.18 견학"), [(d(2026, 11, 18),) * 2])
        self.assertEqual(self.dates("일시 : 2026년 11월 10일(화) 10시 ~ 12시"),
                         [(d(2026, 11, 10),) * 2])
        self.assertEqual(self.dates("26년 10월 14일(수)", date(2026, 9, 28)), [(d(2026, 10, 14),) * 2])

    def test_ranges_across_a_month_or_the_new_year(self):
        d = date
        self.assertEqual(self.dates("12월 31일~1월 2일 겨울방학", d(2026, 12, 20)),
                         [(d(2026, 12, 31), d(2027, 1, 2))])
        self.assertEqual(self.dates("12/30~1/2 방학", d(2026, 12, 20)), [(d(2026, 12, 30), d(2027, 1, 2))])
        self.assertEqual(self.dates("11월 30일~2일 체험", d(2026, 11, 20)), [(d(2026, 11, 30), d(2026, 12, 2))])

    def test_year_month_day_numbers(self):
        d = date(2026, 11, 25)
        for line in ("현장학습: 2026. 11. 25.(수)", "2026.11.25 견학", "26.11.25(수) 견학"):
            self.assertEqual(self.dates(line), [(d, d)], line)
        self.assertEqual(self.dates("2026.11.25", date(2026, 11, 13)), [(d, d)])

    def test_december_post_about_january_is_next_year(self):
        self.assertEqual(self.dates("1월 5일 방학식", date(2026, 12, 20)), [(date(2027, 1, 5),) * 2])

    def test_measures_are_not_dates(self):
        for line in ("체온 36.5도", "3.5세 반", "낮잠 1.5~2시간", "1.5 시간 동안", "키 1.2m",
                     "점수 4.5점", "버전 2.0.1", "10:30 등원", "010-1234-5678", "2/3 정도 먹었어요",
                     "밥 1/2 공기", "37.7 / 37.4도", "12.5도 이상"):
            self.assertEqual(self.dates(line), [], line)

    def test_impossible_dates_are_dropped(self):
        self.assertEqual(self.dates("2월 30일"), [])
        self.assertEqual(self.dates("13/40"), [])


class EventLineTest(unittest.TestCase):
    def test_weekly_plan_header_takes_the_line_below(self):
        text = "🍂11월 16일(월)\n- 올바른 손씻기 위생교육\n\n🍂11월20일(금)\n- 드론과 놀아요\n- 다같이 참여"
        self.assertEqual(sp.event_lines(text, NOV13), [
            (date(2026, 11, 16), date(2026, 11, 16), "올바른 손씻기 위생교육"),
            (date(2026, 11, 20), date(2026, 11, 20), "드론과 놀아요 / 다같이 참여"),
        ])

    def test_when_line_takes_the_post_title(self):
        got = sp.event_lines("일시 : 2026년 11월 10일(화) 10시 ~ 12시\n장소 : 수련관", NOV13, "동요대회")
        self.assertEqual(got, [(date(2026, 11, 10), date(2026, 11, 10),
                                "동요대회 · 일시 : 2026년 11월 10일(화) 10시 ~ 12시")])

    def test_sentence_with_a_date_is_quoted_whole(self):
        got = sp.event_lines("⭐️2학기 부모상담 설문지 10월 12일(월)까지 보내주세요.", date(2026, 10, 8))
        self.assertEqual(got[0][2], "2학기 부모상담 설문지 10월 12일(월)까지 보내주세요.")


def post(source="공지", item_id=1, posted=NOV13, text="", **kw):
    return sp.Post(source, item_id, posted, text, **kw)


class BoardTest(unittest.TestCase):
    TODAY = date(2026, 11, 18)

    def test_windows_order_and_dedup(self):
        posts = [
            post(text="🍂11월 16일(월)\n- 지난 행사\n\n🍂11월 20일(금)\n- 드론과 놀아요"),
            post(item_id=2, text="11월 19일 견학"),
            post(item_id=2, text="11월 19일 견학"),                       # same line twice
            post(item_id=3, posted=date(2026, 9, 1), text="11월 25일 오래된 글"),  # outside 60 days
            post(item_id=4, posted=date(2026, 11, 20), text="11월 21일 미래 글"),   # after today
            post("알림장", 5, date(2026, 11, 17), "수건 보내주세요~"),
            post("알림장", 6, date(2026, 10, 1), "턱받이 보내주세요~"),            # older than 30 days
            post("알림장", 7, date(2026, 11, 10), "물티슈 보내주세요."),
        ]
        b = sp.build_board(posts, self.TODAY)
        self.assertEqual([(e.start.day, e.text) for e in b.events],
                         [(19, "11월 19일 견학"), (20, "드론과 놀아요")])
        self.assertEqual([r.item_id for r in b.requests], [5, 7])

    def test_multi_day_event_still_running_is_upcoming(self):
        b = sp.build_board([post(text="11월 17일~19일 공사")], self.TODAY)
        self.assertEqual(len(b.events), 1)


class FromKidsnoteTest(unittest.TestCase):
    TODAY = date(2026, 11, 18)

    def setUp(self):
        self.calls = []
        self.comments = {
            ("reports", 10): [
                {"author": {"type": "teacher"}, "content": "내일 우비 보내주세요~", "created": "2026-11-16T10:00:00"},
                {"author": {"type": "parent"}, "content": "네 가방에 넣어 보냈어요!", "created": "2026-11-16T20:00:00"},
            ],
            ("notices", 20): [
                {"author": {"type": "parent"}, "content": "보냈습니다. 그리고 우리 OO는 11월 19일 결석해요",
                 "created": "2026-11-14"},
                {"author": {"type": "admin"}, "content": "11월 21일까지 신청서 보내주세요", "created": "2026-11-14"},
            ],
        }

    def comments_of(self, kind, item):
        self.calls.append((kind, item["id"]))
        return self.comments.get((kind, item["id"]), [])

    def test_only_teacher_text_is_read(self):
        reports = [
            {"id": 10, "date_written": "2026-11-16", "author": {"type": "teacher"},
             "content": "오늘 즐거웠어요.", "num_comments": 2},
            {"id": 11, "date_written": "2026-11-16", "author": {"type": "parent"},
             "content": "내일 늦게 보내주세요"},                        # parent post: ignored
            {"id": 12, "date_written": "2025-01-01", "author": {"type": "teacher"},
             "content": "수건 보내주세요", "num_comments": 3},           # too old: no lookup
        ]
        notices = [{"id": 20, "created": "2026-11-14T09:00:00+09:00", "title": "11월 3주 안내",
                    "author": {"type": "admin"}, "content": "* 준비물 : 원복", "num_comments": 2}]
        posts = sp.posts_from_kidsnote(reports, notices, {10: "pg-10"}, self.comments_of, self.TODAY)
        self.assertEqual(self.calls, [("reports", 10), ("notices", 20)])
        b = sp.build_board(posts, self.TODAY)

        texts = {(r.source, r.text, r.page_id) for r in b.requests}
        self.assertEqual(texts, {
            ("선생님 댓글", "내일 우비 보내주세요~", "pg-10"),
            ("공지", "* 준비물 : 원복", ""),
            ("선생님 댓글", "11월 21일까지 신청서 보내주세요", ""),
        })
        # Parent posts / comments (here another family's, on a center-wide notice) are never read.
        self.assertFalse(any("결석" in e.text for e in b.events))
        self.assertEqual([e.start.day for e in b.events], [21])


@unittest.skipIf(nm is None, "requests not installed")
class FetchStepTest(unittest.TestCase):
    """fetch._publish_supplies: wiring, budget guard, and that it never fails the run."""

    class Mirror:
        def __init__(self, fail=False):
            self.fail, self.calls, self.published = fail, [], None

        def _fetch_comments(self, sess, kind, item_id):
            self.calls.append((kind, item_id))
            return [{"author": {"type": "teacher"}, "content": "우비 보내주세요~", "created": str(date.today())}]

        def publish_supplies(self, board, titles):
            if self.fail:
                raise RuntimeError("notion down https://up-kids-kage.kakao.com/dn/SECRET/x.jpg")
            self.published = (board, titles)

    def setUp(self):
        import fetch
        self.fetch = fetch
        today = str(date.today())
        self.reports = [{"id": 1, "date_written": today, "author": {"type": "teacher"},
                         "content": "수건 보내주세요", "num_comments": 1}]
        self.notices = [{"id": 2, "created": today, "title": " 11월 안내 ", "author": {"type": "admin"},
                         "content": "* 준비물 : 원복"}]

    def test_publishes_with_titles_links_and_comments(self):
        m = self.Mirror()
        self.fetch._publish_supplies(m, None, self.reports, self.notices, {1: "pg"})
        board, titles = m.published
        self.assertEqual(titles, {2: "11월 안내"})
        self.assertEqual(m.calls, [("reports", 1)])
        self.assertEqual({(r.source, r.page_id) for r in board.requests},
                         {("알림장", "pg"), ("선생님 댓글", "pg"), ("공지", "")})

    def test_no_comment_lookups_near_the_time_cap(self):
        from unittest import mock
        m = self.Mirror()
        with mock.patch.object(self.fetch, "_remaining_budget", return_value=60):
            self.fetch._publish_supplies(m, None, self.reports, self.notices, {})
        self.assertEqual(m.calls, [])
        self.assertTrue(m.published)

    def test_a_failure_is_a_warning_without_the_media_key(self):
        with self.assertLogs("kidsnote_fetch", level="WARNING") as logs:
            self.fetch._publish_supplies(self.Mirror(fail=True), None, self.reports, self.notices, {})
        self.assertIn("supplies publish failed", logs.output[0])
        self.assertNotIn("SECRET", logs.output[0])


@unittest.skipIf(nm is None, "requests not installed")
class PageBlocksTest(unittest.TestCase):
    def setUp(self):
        import requests
        self.mirror = nm.NotionMirror("ntn_x", "0" * 32, session=requests.Session())

    @staticmethod
    def text(block):
        return "".join(rt["text"]["content"] for rt in block[block["type"]].get("rich_text", []))

    def test_page_shows_dates_quotes_links_and_badges(self):
        today = date(2026, 11, 18)
        board = sp.Board(today=today, events=[
            sp.Event(date(2026, 11, 17), date(2026, 11, 19), "공사", "공지", date(2026, 11, 13), 20, "a" * 32),
            sp.Event(date(2026, 11, 20), date(2026, 11, 20), "드론과 놀아요", "공지", date(2026, 11, 13), 21),
        ], requests=[
            sp.Request(date(2026, 11, 17), "알림장", "수건 보내주세요~", 10, "b" * 32),
        ])
        blocks = self.mirror._supplies_blocks(board, {20: "11월 3주 가정통신문"})
        texts = [self.text(b) for b in blocks]
        self.assertIn("📅 다가오는 일정 (2)", texts)
        self.assertIn("오늘 11/17(화)–11/19(목) · 공사 · 공지: 11월 3주 가정통신문", texts)
        self.assertIn("11/20(금) · 드론과 놀아요 · 공지", texts)
        self.assertIn("11/17 · 수건 보내주세요~ · 알림장", texts)
        first = next(b for b in blocks if self.text(b).startswith("오늘"))
        links = [rt["text"].get("link", {}).get("url") for rt in first["bulleted_list_item"]["rich_text"]]
        self.assertIn(nm.notion_page_url("a" * 32), links)

    def test_empty_board_says_so(self):
        texts = [self.text(b) for b in self.mirror._supplies_blocks(sp.Board(today=date(2026, 11, 18)), {})]
        self.assertIn("앞으로 60일 안의 날짜가 적힌 일정이 없어요.", texts)
        self.assertIn("최근 30일 동안 선생님 글에서 찾은 요청이 없어요.", texts)

    def test_guide_names_the_page_as_it_appears(self):
        readme = (HERE.parents[2] / "README.md").read_text(encoding="utf-8")
        self.assertTrue(nm.NotionMirror.SUPPLIES_TITLE.endswith("📋 준비물·일정"))
        self.assertIn("📋 **준비물·일정**", readme)
        self.assertIn(f"최근 {sp.REQUEST_LOOKBACK_DAYS}일", readme)
        self.assertIn(f"오늘부터 {sp.EVENT_AHEAD_DAYS}일", readme)

    def test_title_sorts_with_the_other_dashboards(self):
        self.assertTrue(nm.NotionMirror.SUPPLIES_TITLE.startswith("_5 "))
        ids = [nm.NotionMirror.DASHBOARD_REPORT_ID, nm.NotionMirror.NUTRITION_REPORT_ID,
               nm.NotionMirror.GROWTH_STORY_REPORT_ID, nm.NotionMirror.MILESTONES_REPORT_ID,
               nm.NotionMirror.INTERESTS_REPORT_ID, nm.NotionMirror.TEACHER_THANKS_REPORT_ID,
               nm.NotionMirror.SUPPLIES_REPORT_ID, -2]
        self.assertEqual(len(set(ids)), len(ids))


if __name__ == "__main__":
    unittest.main()
