"""📅 키즈노트 일정: the dated events of 📋 준비물·일정 as a Notion calendar.

Stage 3 of the issue #2 ideas, Notion-native instead of a Google Calendar
sync (that would need every parent to set up OAuth).

- One database per backup, created once as an inline table on the page that
  holds the backup database and found again on every run by a short
  backup-id marker in its description (so renaming it is fine). It is never
  recreated, so any view or column a parent changes stays. If a lookup on
  that page fails, the run skips the calendar rather than risk a duplicate.
- When it is created, a calendar view is added and the default table view
  removed (Notion views API, version 2026-03-11). If that fails the database
  simply stays a table: nothing else depends on it.
- Each run adds the events it has not seen yet (one row per event, keyed by
  post id + which text it came from + a hash of dates and text). Past events
  stay as history. A future event whose source text (post body, teacher
  comments, or one attached file) was read this run but no longer has it
  (the teacher edited the date) is archived. Text that was not read (a
  fetch failed, an attachment over the per-run cap, the post left the
  window) never causes an archive.
- Needs the connection to have access to that page, which the setup guide
  gives it. Without access the calendar is skipped with an info line.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date, timedelta
from typing import Any

import supplies

_LOGGER = logging.getLogger(__name__)

CALENDAR_TITLE = "📅 키즈노트 일정"
VIEWS_API_VERSION = "2026-03-11"

P_NAME, P_DATE, P_SOURCE, P_POSTED, P_KEY = "이름", "날짜", "출처", "게시일", "동기화 키"


def marker(backup_db_id: str) -> str:
    return f"백업 {backup_db_id.replace('-', '')[:8]}"


def _h(text: str, n: int) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


def event_key(e: supplies.Event) -> str:
    """``<post id>-<source hash>-<event hash>``."""
    return f"{e.item_id}-{_h(e.source, 6)}-{_h(f'{e.start}|{e.end}|{e.text}', 12)}"


def key_source(key: str) -> tuple[int, str] | None:
    """``(post id, source hash)`` of a key, None for anything else."""
    parts = key.split("-")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]), parts[1]
    except ValueError:
        return None


def source_id(item_id: int, source: str) -> tuple[int, str]:
    return item_id, _h(source, 6)


def _plain(rich: list[dict[str, Any]] | None) -> str:
    return "".join(r.get("plain_text") or (r.get("text") or {}).get("content", "") for r in rich or [])


class CalendarSync:
    """Keeps the 📅 database in step with a ``supplies.Board``."""

    def __init__(self, mirror: Any) -> None:
        self.m = mirror
        self.api = mirror_api()

    # ------------------------------------------------------------ HTTP

    def _call(self, method: str, path: str, *, version: str | None = None, **kw: Any) -> Any:
        headers = self.m._headers()
        if version:
            headers = dict(headers, **{"Notion-Version": version})
        return self.m.session.request(method, f"{self.api}{path}", headers=headers,
                                      timeout=self.m.timeout, **kw)

    # ------------------------------------------------------------ find / create

    def _parent_page(self) -> str | None:
        r = self._call("GET", f"/databases/{self.m.database_id}")
        if not r.ok:
            return None
        parent = r.json().get("parent") or {}
        return parent.get("page_id") if parent.get("type") == "page_id" else None

    def _find(self, page_id: str) -> str | None:
        """The 📅 database on ``page_id`` for this backup (None if not there yet).

        Raises PermissionError when the page can't be read, and RuntimeError
        when a database there couldn't be checked: creating one then could
        duplicate a calendar that is really there.
        """
        cursor = None
        unsure = False
        own = self.m.database_id.replace("-", "")
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            r = self._call("GET", f"/blocks/{page_id}/children", params=params)
            if r.status_code in (403, 404):
                raise PermissionError(r.status_code)
            r.raise_for_status()
            body = r.json()
            for blk in body.get("results") or []:
                if blk.get("type") != "child_database" or blk["id"].replace("-", "") == own:
                    continue
                d = self._call("GET", f"/databases/{blk['id']}")
                if d.status_code == 404:
                    continue  # another connection's database
                if not d.ok:
                    unsure = True
                    continue
                if marker(self.m.database_id) in _plain(d.json().get("description")):
                    return blk["id"]
            if not body.get("has_more"):
                if unsure:
                    raise RuntimeError("could not check every database on the page")
                return None
            cursor = body.get("next_cursor")

    def _create(self, page_id: str) -> str:
        r = self._call("POST", "/databases", json={
            "parent": {"type": "page_id", "page_id": page_id},
            "is_inline": True,
            "title": [{"type": "text", "text": {"content": CALENDAR_TITLE}}],
            "description": [{"type": "text", "text": {"content":
                "📋 준비물·일정에 나온 날짜를 모아요. 매번 자동으로 채워져요. "
                f"({marker(self.m.database_id)})"}}],
            "properties": {
                P_NAME: {"title": {}},
                P_DATE: {"date": {}},
                P_SOURCE: {"rich_text": {}},
                P_POSTED: {"date": {}},
                P_KEY: {"rich_text": {}},
            },
        })
        r.raise_for_status()
        db = r.json()
        _LOGGER.info("📅 Created the %s database", CALENDAR_TITLE)
        date_prop = ((db.get("properties") or {}).get(P_DATE) or {}).get("id")
        try:
            self._calendar_view(db["id"], date_prop)
        except Exception as e:  # noqa: BLE001 - the table still works
            _LOGGER.info("📅 calendar view not added (the table still works): %s", type(e).__name__)
        return db["id"]

    def _calendar_view(self, db_id: str, date_prop_id: str | None) -> None:
        """Add a month calendar view and drop the default table view."""
        if not date_prop_id:
            return
        d = self._call("GET", f"/databases/{db_id}", version=VIEWS_API_VERSION)
        d.raise_for_status()
        sources = d.json().get("data_sources") or []
        if not sources:
            return
        before = self._call("GET", "/views", version=VIEWS_API_VERSION, params={"database_id": db_id})
        before.raise_for_status()
        old_ids = [v["id"] for v in before.json().get("results") or []]
        r = self._call("POST", "/views", version=VIEWS_API_VERSION, json={
            "database_id": db_id,
            "data_source_id": sources[0]["id"],
            "name": "달력",
            "type": "calendar",
            "configuration": {"type": "calendar", "date_property_id": date_prop_id,
                              "view_range": "month", "show_weekends": True},
        })
        r.raise_for_status()
        for vid in old_ids:
            self._call("DELETE", f"/views/{vid}", version=VIEWS_API_VERSION)

    # ------------------------------------------------------------ rows

    def _rows(self, db_id: str) -> dict[str, dict[str, Any]]:
        """key → {page_id, start, posted} for every row."""
        out: dict[str, dict[str, Any]] = {}
        cursor = None
        while True:
            body: dict[str, Any] = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            r = self._call("POST", f"/databases/{db_id}/query", json=body)
            r.raise_for_status()
            data = r.json()
            for page in data.get("results") or []:
                props = page.get("properties") or {}
                key = _plain((props.get(P_KEY) or {}).get("rich_text"))
                if not key:
                    continue
                out[key] = {
                    "page_id": page["id"],
                    "start": supplies.to_date(((props.get(P_DATE) or {}).get("date") or {}).get("start")),
                    "posted": supplies.to_date(((props.get(P_POSTED) or {}).get("date") or {}).get("start")),
                }
            if not data.get("has_more"):
                return out
            cursor = data.get("next_cursor")

    def _row_properties(self, e: supplies.Event, source_label: str) -> dict[str, Any]:
        from notion_mirror import notion_page_url

        when: dict[str, Any] = {"start": e.start.isoformat()}
        if e.end != e.start:
            when["end"] = e.end.isoformat()
        source: dict[str, Any] = {"content": source_label[:200]}
        if e.page_id:
            source["link"] = {"url": notion_page_url(e.page_id)}
        return {
            P_NAME: {"title": [{"type": "text", "text": {"content": e.text[:200]}}]},
            P_DATE: {"date": when},
            P_SOURCE: {"rich_text": [{"type": "text", "text": source}]},
            P_POSTED: {"date": {"start": e.posted.isoformat()}},
            P_KEY: {"rich_text": [{"type": "text", "text": {"content": event_key(e)}}]},
        }

    # ------------------------------------------------------------ sync

    def sync(
        self,
        board: supplies.Board,
        titles: dict[int, str],
        read: set[tuple[int, str]],
    ) -> str | None:
        """Bring the 📅 database in step with ``board``; returns its id (None if skipped).

        ``read``: ``(post id, source)`` of every text read this run (the
        ``Post.item_id`` / ``Post.source`` pairs); only events of those can
        be judged gone.
        """
        read_sources = {source_id(i, s) for i, s in read}
        page_id = self._parent_page()
        if not page_id:
            _LOGGER.info("📅 Calendar skipped: the backup database is not inside a page")
            return None
        try:
            db_id = self._find(page_id) or self._create(page_id)
        except PermissionError:
            _LOGGER.info("📅 Calendar skipped: connect the Notion connection to the page "
                         "that holds the backup database")
            return None
        rows = self._rows(db_id)
        wanted = {event_key(e): e for e in board.events}
        added = removed = 0
        for key, e in wanted.items():
            if key in rows:
                continue
            title = titles.get(e.item_id, "")
            label = f"{e.source}: {title}" if title else e.source
            r = self._call("POST", "/pages", json={
                "parent": {"database_id": db_id},
                "properties": self._row_properties(e, label),
            })
            r.raise_for_status()
            added += 1
        # A future event of a post read this run that the board no longer
        # shows was edited at the source.
        today = board.today
        horizon = today + timedelta(days=supplies.EVENT_AHEAD_DAYS)
        oldest_read = today - timedelta(days=supplies.EVENT_LOOKBACK_DAYS)
        for key, row in rows.items():
            start, posted = row["start"], row["posted"]
            if key in wanted or key_source(key) not in read_sources:
                continue
            if not isinstance(start, date) or not isinstance(posted, date):
                continue
            if today < start <= horizon and posted >= oldest_read:
                self.m._archive_page(row["page_id"])
                removed += 1
        _LOGGER.info("📅 Calendar updated (+%d new, -%d changed at the source, %d kept)",
                     added, removed, len(rows) - removed)
        return db_id


def mirror_api() -> str:
    import notion_mirror
    return notion_mirror.NOTION_API
