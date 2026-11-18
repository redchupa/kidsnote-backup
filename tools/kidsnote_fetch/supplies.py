"""📋 준비물·일정: requests and dated events from teacher / center posts (stdlib only).

Rule-based, no LLM, and quote-only: every entry is a sentence or line copied
from the post as written, so nothing is guessed. Wording rules come from real
posts (checked 2026-11-18 on 438 reports + 100 notices):

- Requests are imperatives: ``수건 보내주세요~``, ``칫솔 6개와 치약 1개를
  보내주시면 감사하겠습니다``, ``가족 사진 부탁드립니다``, ``* 준비물 : 원복``.
  Narrative past tense (``보내주신 자동차를 타고 …``) and courtesy lines
  (``응원과 격려 부탁드립니다``) are not requests.
- Weekly plans put a date on its own line and the event on the next one
  (``🍂11월 16일(월)`` / ``- 올바른 손씻기 위생교육``); one-off notices use
  ``일시 : 2026년 11월 10일(화) 10시 ~ 12시`` under a title.
- Parent-written posts and comments never carry requests (and on center-wide
  notices they come from other families), so only teacher / center text is
  read. A "parent already sent it" badge was tried and dropped: on real
  replies it fired only on "못보내드렸어요" and "양말 가방에 있었어요", and a
  wrong ✅ could make a family skip packing something.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
REQUEST_LOOKBACK_DAYS = 30   # 🎒 list: posts from the last 30 days
MAX_REQUESTS_PER_POST = 15   # a long attached plan must not flood the board
MAX_EVENTS_PER_POST = 40
EVENT_LOOKBACK_DAYS = 60     # 📅 list: read posts from the last 60 days …
EVENT_AHEAD_DAYS = 60        # … for events dated today through 60 days ahead
MAX_LINE = 300               # quoted text is cut at a word boundary past this

TEACHER_TYPES = {"teacher", "admin"}

# ``보내주세요`` / ``보내 주시면`` / ``챙겨 주세요`` / ``입금해 주세요`` …
_REQUEST_VERB = re.compile(
    r"(보내|챙겨|가져와|가져오|가지고\s*와|가지고\s*오|준비해|입혀|신겨|지참해|넣어|싸|"
    r"제출해|작성해|체크해|입금해|결제해|기입해|기재해|회신해|표시해|확인해)"
    r"\s*(주세요|주시기|주시면|주십시오|주셔야|주실|줘요)"
)
_SUPPLY_LABEL = re.compile(r"준비물\s*[:：]|준비물.{0,12}(안내|참고|확인)|지참\s*[:：]")
_PLEASE = re.compile(r"부탁\s*드(립니다|려요)")
# Courtesy / health-advice lines that also end in 부탁드립니다.
_NOT_A_REQUEST = re.compile(
    r"응원|격려|칭찬|관심|양해|사랑|잘\s*부탁|협조|주의|건강|치료|진료|조심|기도|축하|인사|"
    r"도움|지도|되시길|보내시길|지내|함께해|동참"
)

_WEEKDAY = r"(?:\s*\(\s*[월화수목금토일]\s*(?:요일)?\s*\))?"
_YEAR = r"(?:(?P<y>\d{4}|\d{2})\s*년\s*)?"
# Units / amounts right after a number mean it is a measure, not a date
# (36.5도, 3.5세, 1.5시간, 2/3 정도).
_UNIT = (r"(?!\s*(?:도|세|℃|°|%|시간|시|분|초|개월|개|살|명|배|점|kg|g|cm|mm|ml|m|L|"
         r"원|만|천|억|km|회|번|차|층|호|학년|반|kcal|정도|만큼|가량|쯤|씩|공기|그릇|컵|"
         r"이상|이하|미만|초과))")
_KOREAN = re.compile(
    _YEAR + r"(?P<m>\d{1,2})\s*월\s*(?P<d>\d{1,2})\s*일" + _WEEKDAY
    + r"(?:\s*[~∼\-–]\s*(?:(?P<m2>\d{1,2})\s*월\s*)?(?P<d2>\d{1,2})\s*일" + _WEEKDAY + r")?"
)
_YMD = re.compile(
    r"(?<![\d.])(?P<y>\d{4}|\d{2})\s*\.\s*(?P<m>\d{1,2})\s*\.\s*(?P<d>\d{1,2})(?!\d)\.?" + _WEEKDAY
)
_NUMERIC = re.compile(
    r"(?<![\d.:/])(?P<m>\d{1,2})\s*[/.]\s*(?P<d>\d{1,2})(?![\d.]\d)" + _UNIT + _WEEKDAY
    + r"(?:\s*[~∼\-–]\s*(?:(?P<m2>\d{1,2})\s*[/.]\s*)?(?P<d2>\d{1,2})(?![\d.]\d)" + _UNIT + _WEEKDAY + r")?"
)
_RANGE_TAIL = re.compile(r"\s*[~∼\-–]\s*\d")
_HEADER_WORD = re.compile(r"^(일시|일정|날짜|기간|때|언제)\s*[:：]")
_LEAD_JUNK = re.compile(r"^[\W_]+", re.UNICODE)
_SENTENCE_END = re.compile(r"(?<=[.!?~♡💕💛🤍])\s+|\n+")


@dataclass
class Post:
    """One teacher / center text the extractor reads."""
    source: str                    # 알림장 / 공지 / 선생님 댓글
    item_id: int                   # the kidsnote id the Notion page is keyed on
    posted: date
    text: str
    title: str = ""
    page_id: str = ""


@dataclass
class Request:
    posted: date
    source: str
    text: str
    item_id: int
    page_id: str = ""


@dataclass
class Event:
    start: date
    end: date
    text: str
    source: str
    posted: date
    item_id: int
    page_id: str = ""


@dataclass
class Board:
    today: date
    events: list[Event] = field(default_factory=list)
    requests: list[Request] = field(default_factory=list)


def today_kst() -> date:
    return datetime.now(KST).date()


def to_date(value: object) -> date | None:
    """``2026-11-13`` / ``2026-11-13T09:00:00+09:00`` → date (None if unusable)."""
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def clip(text: str, limit: int = MAX_LINE) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    return text[: cut if cut > limit // 2 else limit].rstrip() + " …"


# ------------------------------------------------------------------ requests

def request_sentences(text: str) -> list[str]:
    """Sentences of ``text`` that ask the family to bring / send / do something."""
    out: list[str] = []
    for raw in _SENTENCE_END.split(text or ""):
        line = raw.strip()
        if not line or len(line) > 400:
            continue
        if _REQUEST_VERB.search(line) or _SUPPLY_LABEL.search(line):
            out.append(clip(line))
        elif _PLEASE.search(line) and not _NOT_A_REQUEST.search(line):
            out.append(clip(line))
    return out


# ------------------------------------------------------------------ dates

def _resolve(y: str | None, m: int, d: int, posted: date) -> date | None:
    """Calendar date for a month/day written in a post on ``posted``.

    No year → the post's year, or the next one when that lands well before
    the post (a December post about 1월). Dates far from the post are not
    dates at all (version numbers, scores), so they are dropped.
    """
    try:
        if y:
            year = int(y) + (2000 if len(y) == 2 else 0)
            got = date(year, m, d)
        else:
            got = date(posted.year, m, d)
            if got < posted - timedelta(days=60):
                got = date(posted.year + 1, m, d)
    except ValueError:
        return None
    if not (posted - timedelta(days=60) <= got <= posted + timedelta(days=200)):
        return None
    return got


def _range_end(start: date, m2: str | None, d2: int) -> date:
    """End of ``start~[m2]d2``: ``12월 30일~1월 2일`` ends next year and
    ``11월 30일~2일`` next month. Anything odd falls back to a one-day event."""
    year, month = start.year, int(m2) if m2 else start.month
    if not m2 and d2 < start.day:
        month += 1
    if month > 12 or (m2 and month < start.month):
        year, month = year + 1, (month - 1) % 12 + 1
    try:
        end = date(year, month, d2)
    except ValueError:
        return start
    return end if start <= end <= start + timedelta(days=62) else start


def find_dates(line: str, posted: date) -> list[tuple[date, date, tuple[int, int]]]:
    """``(start, end, span)`` for every date or date range written in ``line``."""
    found: list[tuple[date, date, tuple[int, int]]] = []
    taken: list[tuple[int, int]] = []
    for rx in (_KOREAN, _YMD, _NUMERIC):
        for m in rx.finditer(line):
            if any(a < m.end() and m.start() < b for a, b in taken):
                continue
            if rx is _NUMERIC and not m.group("d2") and _RANGE_TAIL.match(line, m.end()):
                continue  # "1.5~2시간": the range end failed the unit check
            gd = m.groupdict()
            start = _resolve(gd.get("y"), int(gd["m"]), int(gd["d"]), posted)
            if start is None:
                continue
            end = start
            if gd.get("d2"):
                end = _range_end(start, gd.get("m2"), int(gd["d2"]))
            found.append((start, end, m.span()))
            taken.append(m.span())
    return found


def _label(line: str) -> str:
    return _LEAD_JUNK.sub("", line).strip()


def event_lines(text: str, posted: date, title: str = "") -> list[tuple[date, date, str]]:
    """Dated lines of a post, as ``(start, end, text to show)``.

    A line that is only a date (a weekly-plan header) takes the lines under
    it, up to the next blank or dated line. ``일시 : …`` lines take the post
    title, since the event's name is there rather than on the line.
    """
    lines = (text or "").splitlines()
    out: list[tuple[date, date, str]] = []
    for i, line in enumerate(lines):
        dates = find_dates(line, posted)
        if not dates:
            continue
        rest = line
        for _, _, (a, b) in sorted(dates, key=lambda x: x[2][0], reverse=True):
            rest = rest[:a] + rest[b:]
        rest_label = _label(rest)
        if _HEADER_WORD.match(_label(line)):
            shown = f"{title} · {_label(line)}" if title else _label(line)
        elif len(re.sub(r"[\W\d_]", "", rest_label)) < 2:
            below: list[str] = []
            for nxt in lines[i + 1:]:
                if not nxt.strip() or find_dates(nxt, posted):
                    break
                below.append(_label(nxt))
                if len(below) == 3:
                    break
            shown = " / ".join(x for x in below if x) or title or _label(line)
        else:
            shown = _label(line)
        for start, end, _ in dates:
            out.append((start, end, clip(shown)))
    return out


# ------------------------------------------------------------------ board

def build_board(posts: list[Post], today: date | None = None) -> Board:
    """Collect the 🎒 requests and 📅 upcoming events from ``posts``."""
    today = today or today_kst()
    board = Board(today=today)
    seen_req: set[tuple[int, str]] = set()
    seen_evt: set[tuple[date, date, str]] = set()
    for p in posts:
        if p.posted > today:
            continue
        if p.posted >= today - timedelta(days=REQUEST_LOOKBACK_DAYS):
            for s in request_sentences(p.text)[:MAX_REQUESTS_PER_POST]:
                key = (p.item_id, s)
                if key not in seen_req:
                    seen_req.add(key)
                    board.requests.append(Request(p.posted, p.source, s, p.item_id, p.page_id))
        if p.posted >= today - timedelta(days=EVENT_LOOKBACK_DAYS):
            for start, end, shown in event_lines(p.text, p.posted, p.title)[:MAX_EVENTS_PER_POST]:
                if end < today or start > today + timedelta(days=EVENT_AHEAD_DAYS):
                    continue
                key = (start, end, shown)
                if key not in seen_evt:
                    seen_evt.add(key)
                    board.events.append(Event(start, end, shown, p.source, p.posted,
                                              p.item_id, p.page_id))
    board.requests.sort(key=lambda r: (r.posted, r.item_id), reverse=True)
    board.events.sort(key=lambda e: (e.start, e.end, e.posted))
    return board


def posts_from_kidsnote(
    reports: list[dict],
    notices: list[dict],
    page_ids: dict[int, str],
    comments_of,
    today: date | None = None,
    attachment_texts_of=None,
) -> list[Post]:
    """Turn fetched reports / notices into the posts the board reads.

    Only teacher / center text is used. ``comments_of(kind, item)`` returns
    an item's comments; it is called only for items inside the lookback
    window that have comments, so a quiet run costs a few requests at most.
    ``attachment_texts_of(item)`` returns ``(file name, text)`` of the
    documents attached to a teacher post in the window (monthly plans and
    가정통신문 often live there rather than in the post).
    """
    today = today or today_kst()
    oldest = today - timedelta(days=max(REQUEST_LOOKBACK_DAYS, EVENT_LOOKBACK_DAYS))
    posts: list[Post] = []
    for kind, label, items in (("reports", "알림장", reports), ("notices", "공지", notices)):
        for item in items:
            posted = to_date(item.get("date_written") or item.get("created"))
            if posted is None or posted < oldest:
                continue
            try:
                item_id = int(item.get("id") or 0)
            except (TypeError, ValueError):
                continue
            author = ((item.get("author") or {}).get("type") or "").lower()
            page_id = page_ids.get(item_id, "")
            comments = comments_of(kind, item) if item.get("num_comments") else []
            if author in TEACHER_TYPES:
                text = item.get("content") or ""
                if kind == "notices" and item.get("title"):
                    text = f"{item['title']}\n{text}"
                posts.append(Post(label, item_id, posted, text, item.get("title") or "", page_id))
                if attachment_texts_of and item.get("attached_files"):
                    for name, doc in attachment_texts_of(item):
                        short = name if len(name) <= 24 else name[:23] + "…"
                        posts.append(Post(f"첨부({short})", item_id, posted, doc,
                                          item.get("title") or "", page_id))
            for c in comments:
                if ((c.get("author") or {}).get("type") or "").lower() not in TEACHER_TYPES:
                    continue
                c_posted = to_date(c.get("created")) or posted
                posts.append(Post("선생님 댓글", item_id, c_posted, c.get("content") or "",
                                  item.get("title") or "", page_id))
    return posts
