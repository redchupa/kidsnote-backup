"""Kidsnote fetch script.

Pulls a child's reports + attached photos from Kidsnote's unofficial
/api/v1_2 endpoints, using the sessionid cookie from a logged-in browser
session, and either mirrors them straight to a Notion database
(`--publish-to-notion`) or writes them to a local folder layout for
further processing.

Local output layout (one folder per report):

    <backup-root>/
        20260504_093015/
            note.txt
            image_001.jpg
            image_002.jpg
        20260505_142030/
            ...

The endpoint paths, field names, and response shape are best-effort and
may need tweaking against your actual API responses. Run with --dump-raw
once to inspect what Kidsnote returns for your account, then adjust the
constants below if any field name has drifted.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from secret_input import clean_secret, mask_name  # local module
from secret_input import safe_err as _safe_err, safe_url as _safe_url  # local module

try:
    import browser_cookie3
except ImportError:  # surface a clear hint before the first call
    browser_cookie3 = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Kidsnote endpoints (unofficial)
# ---------------------------------------------------------------------------
KIDSNOTE_BASE = "https://www.kidsnote.com"
API = f"{KIDSNOTE_BASE}/api/v1_2"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

# Common candidate keys for the image URL inside a report's attachment object.
# Kidsnote has used "original" historically; the others are fallbacks in case
# the API has evolved.
IMAGE_URL_KEYS = ("original", "url", "src", "high", "high_resize", "large_resize")
ATTACH_LIST_KEYS = ("attached_images", "attached_pictures", "pictures", "images")
TEXT_KEYS = ("content", "body", "report")
# Video attachments — usually a single object (or None) on each report.
# Confirmed schema (2026-05-13): same shape as image attachments —
# `original` is the full-resolution URL.
VIDEO_OBJECT_KEYS = ("attached_video", "video", "attached_videos")
# Misc file attachments (PDFs, Excel etc.) — list of objects keyed by
# `original` (download URL) + `original_file_name` (display name).
FILE_LIST_KEYS = ("attached_files", "files", "attachments")

_LOGGER = logging.getLogger("kidsnote_fetch")

# ------------------------------------------------------------------
# Time budget for cron auto-resume
# ------------------------------------------------------------------
# GitHub-hosted runners hard-cap a single job at 6 hours. To make the
# multi-run backfill fully autonomous (no manual re-trigger needed)
# the workflow uses a 4-hour cron schedule and the script gracefully
# stops processing once we approach the cap. Concurrency group ensures
# the next cron run queues until the current one releases, so dedup
# picks up where we left off.
_START_TIME = time.monotonic()
# 2026-05-23: budget reshape after a single job dragged to 8h46m
# (publish loop stopped cleanly at 5h28m but dashboards ate the next
# 3h+, breaking concurrency for subsequent cron runs).
#   * Publish phase budget shrunk to 4h45m (was 5h30m): leaves enough
#     wall-time for dashboards within the 6h GHA hard cap.
#   * Dashboard reserve raised to 30min (was 2min): publish loop now
#     stops with 30 min runway so dashboards have a real chance to
#     run instead of all skipping with the smaller reserve.
# Net effect: max wall-time is ~5h15m + cleanup, safely under the 6h
# GHA cap. Cron auto-resume picks up next cycle.
TIME_BUDGET_SEC = 4 * 3600 + 45 * 60  # 4h45m for alimnota publish loop
DASHBOARD_RESERVE_SEC = 30 * 60       # 30 min reserved for dashboards


def _remaining_budget() -> float:
    """Seconds remaining in the workflow's self-imposed time budget."""
    return TIME_BUDGET_SEC - (time.monotonic() - _START_TIME)


def _load_env_file(path: Path) -> dict[str, str]:
    """Tiny .env parser — no python-dotenv dep, no shell expansion."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        v = v.strip()
        # Strip surrounding quotes if user wrapped value.
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        out[k.strip()] = v
    return out


def _baseline_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ko",
        "Referer": KIDSNOTE_BASE,
    })
    return sess


def _load_session_from_browser(browser: str) -> requests.Session:
    if browser_cookie3 is None:
        raise RuntimeError("Missing dependency: pip install browser-cookie3")
    loaders = {
        "chrome": browser_cookie3.chrome,
        "firefox": browser_cookie3.firefox,
        "edge": browser_cookie3.edge,
        "auto": browser_cookie3.load,
    }
    if browser not in loaders:
        raise ValueError(f"Unknown browser: {browser}")
    jar = loaders[browser](domain_name="kidsnote.com")
    sess = _baseline_session()
    sess.cookies = jar
    return sess


def _list_children(sess: requests.Session) -> list[dict[str, Any]]:
    """Look up the children registered under the logged-in account.

    Confirmed against the live API on 2026-05-13: `/api/v1/me/children/`
    returns a DRF-style page (`{count, next, previous, results}`), each
    result is `{id, name, date_birth, gender, enrollment, family_type,
    parent, created}`. We only consume `id` (and surface `name` in logs
    so a multi-child household can tell which one we hit).
    """
    url = f"{KIDSNOTE_BASE}/api/v1/me/children/"
    r = sess.get(url, timeout=30)
    if r.status_code == 401:
        raise RuntimeError(
            "401 on /api/v1/me/children/ - session not logged in. "
            "Retry with valid credentials in .env."
        )
    r.raise_for_status()
    data = r.json()
    return data.get("results") or data.get("children") or []


def _list_reports(
    sess: requests.Session, child_id: int, page_size: int = 9999
) -> list[dict[str, Any]]:
    r = sess.get(
        f"{API}/children/{child_id}/reports/",
        params={"page_size": page_size, "tz": "Asia/Seoul", "child": child_id},
        timeout=60,
    )
    r.raise_for_status()
    body = r.json()
    return body.get("results") or body.get("reports") or []


def _list_comments(
    sess: requests.Session, kind: str, item_id: int
) -> list[dict[str, Any]]:
    """Comments for a report / notice / album.

    Confirmed live on 2026-05-13:
        GET /api/v1/reports/<id>/comments/
        GET /api/v1/notices/<id>/comments/
        GET /api/v1/albums/<id>/comments/  (assumed, same pattern)

    `kind` is the URL segment: ``reports`` / ``notices`` / ``albums``.
    Returns empty list on any error so the caller doesn't have to special-case.
    """
    try:
        r = sess.get(
            f"{KIDSNOTE_BASE}/api/v1/{kind}/{item_id}/comments/",
            timeout=15,
        )
        if r.status_code != 200:
            return []
        return r.json().get("results") or []
    except Exception:
        return []


def _fetch_report_detail(
    sess: requests.Session, report_id: int
) -> dict[str, Any] | None:
    """Single-report endpoint returns ~15 extra `life record` fields
    (meal/sleep/bowel/temperature/mood/etc) that the list endpoint omits.

    Confirmed live: ``GET /api/v1_2/reports/<id>/``. Returns ``None`` on error
    so the caller can fall back to the summary record from the list call.
    """
    try:
        r = sess.get(f"{API}/reports/{report_id}/", timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        _LOGGER.warning("report detail fetch failed for id=%d: %s", report_id, e)
        return None


def _list_menus(
    sess: requests.Session, center_id: int, page_size: int = 9999
) -> list[dict[str, Any]]:
    """Daily lunch menu for a daycare center. Confirmed live on 2026-05-13:

        GET /api/v1/centers/<center_id>/menu/?page_size=9999

    Each result: {id, date_menu, morning, morning_snack, lunch,
    afternoon_snack, dinner} + per-item *_img attachments
    (Kakao-CDN photos of the served food). `date_menu` is ISO date.
    """
    url = f"{KIDSNOTE_BASE}/api/v1/centers/{center_id}/menu/"
    r = sess.get(url, params={"page_size": page_size}, timeout=60)
    r.raise_for_status()
    body = r.json()
    return body.get("results") or []


def _list_paginated(
    sess: requests.Session, url: str, page_size: int = 100, max_pages: int = 30
) -> list[dict[str, Any]]:
    """Walk a cursor-paginated DRF endpoint until exhausted.

    Defensive against:
    - Cursor cycles (kidsnote occasionally returns a cursor that loops back to
      data it already gave us → infinite pagination with duplicate results).
    - Buggy ``next`` tokens that never become None.

    Stops when (a) ``next`` is None, (b) we've already seen the cursor, or
    (c) we hit ``max_pages``. Duplicate ids inside results are also
    deduped on output.
    """
    out: list[dict[str, Any]] = []
    seen_ids: set[Any] = set()
    seen_cursors: set[str] = set()
    next_url: str | None = url
    pages = 0
    while next_url and pages < max_pages:
        sep = "&" if "?" in next_url else "?"
        if "page_size=" not in next_url:
            next_url = f"{next_url}{sep}page_size={page_size}"
        r = sess.get(next_url, timeout=60)
        r.raise_for_status()
        body = r.json()
        results = body.get("results") or []
        # Dedup by id (kidsnote cursor sometimes returns overlap)
        new_in_page = 0
        for item in results:
            iid = item.get("id")
            if iid is not None and iid in seen_ids:
                continue
            if iid is not None:
                seen_ids.add(iid)
            out.append(item)
            new_in_page += 1
        nxt = body.get("next")
        if not nxt:
            break
        if nxt in seen_cursors:
            _LOGGER.info("pagination: cursor cycle detected after %d pages, stopping", pages + 1)
            break
        seen_cursors.add(nxt)
        if new_in_page == 0:
            _LOGGER.info("pagination: 0 new items in page %d, stopping", pages + 1)
            break
        if nxt.startswith("http"):
            next_url = nxt
        else:
            base = url.split("?")[0]
            next_url = f"{base}?page_size={page_size}&cursor={nxt}"
        pages += 1
    if pages >= max_pages:
        _LOGGER.warning("pagination: hit max_pages=%d (%d items so far)", max_pages, len(out))
    return out


def _list_notices(
    sess: requests.Session, center_id: int
) -> list[dict[str, Any]]:
    """Center-wide notices (`/api/v1/centers/<id>/notices/`).

    Cursor-paginated; we walk the full history. Each result has the same
    shape as a report (title/content/author/attached_images/video/files).
    """
    return _list_paginated(
        sess, f"{KIDSNOTE_BASE}/api/v1/centers/{center_id}/notices/"
    )


def _list_albums(
    sess: requests.Session, child_id: int
) -> list[dict[str, Any]]:
    """Photo albums for one child (`/api/v1/children/<id>/albums/`)."""
    return _list_paginated(
        sess, f"{KIDSNOTE_BASE}/api/v1/children/{child_id}/albums/"
    )


def _first_existing_key(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d:
            return d[k]
    return None


def _parse_report_datetime(report: dict[str, Any]) -> datetime:
    """Pick the most useful timestamp for the folder name.

    Real-world Kidsnote responses (2026-05-13): `date_written` is a date-only
    field (parses to midnight), while `created` / `modified` carry full
    `YYYY-MM-DDTHH:MM:SS+09:00`. We want stable, content-anchored folder
    names matching the existing BackupKidsnote layout, so:

    1. Prefer `date_written` when it has a non-midnight time component.
    2. Otherwise use `modified` / `created` (they keep HH:MM:SS so the same
       report doesn't shift folders on re-fetch).
    3. Fall back to date-only `date_written` if nothing better is available.
    """
    def _parse(raw: Any) -> datetime | None:
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None

    written = _parse(report.get("date_written"))
    if written is not None and (written.hour or written.minute or written.second):
        return written

    for k in ("modified", "created", "date_modified", "date_created"):
        dt = _parse(report.get(k))
        if dt is not None:
            return dt

    return written or datetime.now()


def _save_report(
    sess: requests.Session,
    report: dict[str, Any],
    backup_root: Path,
) -> tuple[Path, int]:
    """Mirror BackupKidsnote-compatible layout for one report. Returns (folder, n_new_files)."""
    dt = _parse_report_datetime(report)
    folder = backup_root / dt.strftime("%Y%m%d_%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)

    new_files = 0

    # note.txt
    text = _first_existing_key(report, TEXT_KEYS) or ""
    note_path = folder / "note.txt"
    if not note_path.exists():
        note_path.write_text(text, encoding="utf-8")
        new_files += 1

    # photos
    images = _first_existing_key(report, ATTACH_LIST_KEYS) or []
    for i, img in enumerate(images, start=1):
        if _download_attachment(
            sess, img, folder, f"image_{i:03d}", default_suffix=".jpg"
        ):
            new_files += 1

    # video (single, or None)
    video = None
    for k in VIDEO_OBJECT_KEYS:
        v = report.get(k)
        if isinstance(v, dict):
            video = v
            break
        if isinstance(v, list) and v and isinstance(v[0], dict):
            video = v[0]
            break
    if video is not None:
        if _download_attachment(
            sess, video, folder, "video_001", default_suffix=".mp4"
        ):
            new_files += 1

    # generic files (PDFs, Excel, etc.)
    files = _first_existing_key(report, FILE_LIST_KEYS) or []
    for i, fobj in enumerate(files, start=1):
        if _download_attachment(
            sess, fobj, folder, f"file_{i:03d}", default_suffix=".bin",
            keep_original_name=True,
        ):
            new_files += 1

    return folder, new_files


def _download_attachment(
    sess: requests.Session,
    obj: Any,
    folder: Path,
    stem: str,
    *,
    default_suffix: str,
    keep_original_name: bool = False,
) -> bool:
    """Download one attachment (image / video / file). Returns True if a new
    file landed on disk, False if skipped (no URL) or already cached.

    `stem` is the base filename without suffix (e.g. ``image_001`` / ``video_001``).
    `default_suffix` is used when neither the URL path nor `original_file_name`
    carry a recognizable extension.
    `keep_original_name` (file attachments only) makes the saved filename
    ``<stem>_<original_file_name>`` so a PDF/XLSX keeps its identifying name
    while still sorting deterministically alongside other attachments.
    """
    if isinstance(obj, str):
        url = obj
        orig_name = None
    elif isinstance(obj, dict):
        url = _first_existing_key(obj, IMAGE_URL_KEYS)
        orig_name = obj.get("original_file_name")
    else:
        return False
    if not url:
        return False

    # Pick a suffix: original_file_name > URL path > default.
    suffix = ""
    if orig_name:
        suffix = Path(orig_name).suffix.lower()
    if not suffix:
        suffix = Path(urlparse(url).path).suffix.lower()
    if not suffix:
        suffix = default_suffix

    name = stem + suffix
    if keep_original_name and orig_name:
        # Sanitise: drop any path components + suffix duplication.
        safe = re.sub(r"[^\w.\- ]", "_", Path(orig_name).stem).strip() or stem
        name = f"{stem}_{safe}{suffix}"
    out = folder / name
    if out.exists():
        return False
    try:
        r = sess.get(url, timeout=180, stream=True)
        r.raise_for_status()
        with out.open("wb") as fh:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    fh.write(chunk)
        return True
    except Exception as e:
        _LOGGER.warning("attachment %s (%s) failed in %s: %s",
                        name, _safe_url(url), folder.name, _safe_err(e))
        return False


def _live_page_ids(
    page_map: dict[int, str],
    publish_results: list[dict[str, Any]],
    force_refresh: bool,
) -> dict[int, str]:
    """kidsnote id → Notion page id for the 📥 dashboard links.

    Pages created this run win. Under --force-refresh the pre-run pages are
    archived as they get replaced, so only this run's pages are safe to link.
    """
    out: dict[int, str] = {} if force_refresh else dict(page_map)
    for res in publish_results:
        rid = res.get("report_id", res.get("item_id"))
        if rid is not None and res.get("page_id"):
            out[int(rid)] = res["page_id"]
    return out


def _resolve_secret(env: dict[str, str], key: str) -> str:
    """Read a credential from either the .env file or the process environment.

    The GitHub Actions workflow injects secrets via os.environ, so we treat
    that as authoritative if present; otherwise we fall back to the .env file
    used for local runs.
    """
    return os.environ.get(key, "") or env.get(key, "")


def _truthy(value: str) -> bool:
    """Parse a workflow input / env var into a bool.

    GitHub Actions ``workflow_dispatch`` choice inputs arrive as strings
    (``"true"`` / ``"false"``), so a naive ``bool()`` of the raw string would
    treat ``"false"`` as truthy. Accepts the common forms operators type.
    """
    return value.strip().lower() in ("true", "1", "yes", "on", "y")


def _chronological_key(item: dict[str, Any]) -> tuple[str, int]:
    """Sort key that puts the oldest item first.

    Sort priority: ``date_written`` > ``created`` > id. Falls back to the
    item's id (numeric) for stable ordering when dates collide or are
    missing. Kidsnote ids are monotonic, so id-as-secondary keeps near-
    chronological order even for items that share a date.
    """
    date = (item.get("date_written") or item.get("created") or "")[:10]
    iid = item.get("id") or 0
    try:
        iid = int(iid)
    except (TypeError, ValueError):
        iid = 0
    return (date, iid)


def _pick_child(
    children: list[dict[str, Any]],
    child_id: int | None,
    child_name: str,
    child_index: int | None,
) -> dict[str, Any]:
    """Resolve which child to mirror from the multi-child profile.

    Priority: explicit id > name substring match > 1-based index > children[0].
    Name match is case-insensitive Korean substring (e.g. ``유주`` matches
    ``우유주``). Raises SystemExit with the available list if nothing matches —
    that's nearly always more helpful than silently defaulting to children[0],
    which was the original cause of the "I selected the second child in the
    web UI but got the first child's data" bug report.
    """
    if not children:
        sys.exit("no children found on this account.")
    if child_id is not None:
        match = next((c for c in children if c.get("id") == child_id), None)
        if match is None:
            avail = ", ".join(f"{c.get('id')}={mask_name(c.get('name'))}" for c in children)
            sys.exit(f"--child-id {child_id} not in your profile. Available: {avail}")
        return match
    if child_name:
        needle = clean_secret(child_name).lower()
        matches = [c for c in children if needle in (c.get("name") or "").lower()]
        if len(matches) == 0:
            avail = ", ".join(f"{c.get('id')}={mask_name(c.get('name'))}" for c in children)
            sys.exit(
                f"--child-name {mask_name(child_name)!r} matched no child. Available: {avail}"
            )
        if len(matches) > 1:
            avail = ", ".join(f"{c.get('id')}={mask_name(c.get('name'))}" for c in matches)
            sys.exit(
                f"--child-name {mask_name(child_name)!r} ambiguous ({len(matches)} matches): "
                f"{avail}. Use --child-id instead for an exact pick."
            )
        return matches[0]
    if child_index is not None:
        if child_index < 1 or child_index > len(children):
            sys.exit(
                f"--child-index {child_index} out of range (have {len(children)} child(ren))"
            )
        return children[child_index - 1]
    return children[0]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Personal Kidsnote fetcher - not part of the public package."
    )
    ap.add_argument("--backup-root", type=Path,
                    help="Folder where reports + photos will land. "
                         "Required unless --no-local-save is set.")
    ap.add_argument("--no-local-save", action="store_true",
                    help="Skip writing reports to disk. Use with "
                         "--publish-to-notion when running in a stateless "
                         "CI runner (GitHub Actions).")
    ap.add_argument("--publish-to-notion", action="store_true",
                    help="Mirror each new report to a Notion database. "
                         "Reads NOTION_TOKEN + NOTION_DATABASE_ID from .env or "
                         "process env (whichever is set).")
    ap.add_argument("--auth-mode", default="session-cookie-env",
                    choices=["session-cookie-env", "browser-cookie"],
                    help="session-cookie-env (default): logs in with KIDSNOTE_USERNAME / "
                         "KIDSNOTE_PASSWORD, falling back to KIDSNOTE_SESSION_COOKIE "
                         "(value of `sessionid`). Required for headless CI. "
                         "browser-cookie: pulls cookies from a locally logged-in browser.")
    ap.add_argument("--env-file", type=Path,
                    default=Path(__file__).resolve().parents[2] / ".env",
                    help="Path to the .env that holds KIDSNOTE_SESSION_COOKIE / NOTION_TOKEN / "
                         "NOTION_DATABASE_ID. Ignored if the same names exist in process env (CI mode).")
    ap.add_argument("--browser", default="auto",
                    choices=["chrome", "firefox", "edge", "auto"],
                    help="(--auth-mode browser-cookie only)")
    # ---- Child selection (priority: id > name > index > first) ----
    # Multi-child accounts: previously the script silently defaulted to the
    # first child the API returned, which surprised operators who had
    # "switched active child" in the kidsnote web UI (a sessionid cookie is
    # not child-scoped — switching in the web changes nothing for the API).
    # Three knobs now cover the realistic cases.
    ap.add_argument("--child-id", type=int,
                    help="Pick a specific child id (exact). "
                         "See startup log for available ids on your account.")
    ap.add_argument("--child-name", default="",
                    help="Pick by case-insensitive substring of child's name "
                         "(e.g. '유주' to match '우유주'). "
                         "Errors out if multiple match — use --child-id instead.")
    ap.add_argument("--child-index", type=int,
                    help="Pick by 1-based position in the children list "
                         "(only useful when names collide).")
    ap.add_argument("--no-menus", action="store_true",
                    help="Skip daily lunch menu sync.")
    ap.add_argument("--no-notices", action="store_true",
                    help="Skip center-wide notice sync.")
    ap.add_argument("--no-albums", action="store_true",
                    help="Skip photo album sync.")
    # ---- LLM toggles -----
    # The script can be split into two layers of LLM-driven content:
    #   1. Per-alimnota inline callouts: 💭 요약 / 🧒 자녀의 일기 /
    #      👨‍👩‍👧 부모의 편지 (3 LLM calls × every report).
    #   2. Four standalone dashboard pages: 📖 매월 성장 스토리 / 🌟 마일스톤
    #      / 🌱 분기 관심사 / 💌 선생님께 감사 카드.
    # `--no-llm` (or DISABLE_ALL_LLM=true) flips BOTH layers off in one shot,
    # producing a plain backup + the 3 statistical dashboards (📊 통계 /
    # 📅 추억 / 🥗 영양) that don't need LLM. Graduate-backup operators and
    # privacy-conscious users typically want this. The 4 individual
    # dashboard toggles are still available for fine-grained tuning when
    # only some of the dashboards read awkwardly.
    ap.add_argument("--no-llm", action="store_true",
                    help="Plain backup + statistical dashboards only. Skips ALL "
                         "AI-generated content: alimnota callouts (요약/자녀일기/"
                         "부모편지) AND the 4 LLM dashboards. Equivalent to setting "
                         "DISABLE_ALL_LLM=true.")
    ap.add_argument("--no-growth-story", action="store_true",
                    help="Skip 📖 매월 성장 스토리 (LLM-written paragraph per month).")
    ap.add_argument("--no-milestones", action="store_true",
                    help="Skip 🌟 마일스톤 (LLM-extracted '처음 ...' moments).")
    ap.add_argument("--no-interests", action="store_true",
                    help="Skip 🌱 분기 관심사 (LLM-summarized themes per quarter).")
    ap.add_argument("--no-teacher-thanks", action="store_true",
                    help="Skip 💌 선생님께 감사 카드 (LLM-drafted thank-you letter).")
    ap.add_argument("--limit", type=int,
                    help="Only sync the N most recent reports (debugging).")
    ap.add_argument("--monthly-sample", action="store_true",
                    help="For unit testing: pick one report per month (newest "
                         "of each calendar month), instead of N most-recent. "
                         "Useful to verify coverage across a wide date range.")
    ap.add_argument("--force-refresh", action="store_true",
                    help="Re-publish every report by archiving its existing "
                         "Notion page first (bypasses Report-ID dedup). Use "
                         "after prompt/LLM changes so old callouts get "
                         "regenerated. Sentinel dashboard pages are never "
                         "touched by this — they're always replaced anyway.")
    ap.add_argument("--relabel-existing", action="store_true",
                    help="Rewrite outdated label text (life-record chips, weather callout, "
                         "bowel lines) on already-published report pages in place, only where "
                         "it matches what an older version rendered. Pages keep their links.")
    ap.add_argument("--dump-raw", action="store_true",
                    help="Dump the raw /reports/ JSON to backup_root for inspection. "
                         "Ignored when --no-local-save is set.")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Sanity: at least one output channel must be active.
    if not args.no_local_save and args.backup_root is None:
        sys.exit("--backup-root is required unless --no-local-save is set.")
    if args.no_local_save and not args.publish_to_notion:
        sys.exit("--no-local-save is only useful with --publish-to-notion.")

    env = _load_env_file(args.env_file) if args.env_file.exists() else {}

    # ---- auth -----
    if args.auth_mode == "session-cookie-env":
        from kidsnote_auth import AuthError, resolve_session  # local module
        username = _resolve_secret(env, "KIDSNOTE_USERNAME")
        password = _resolve_secret(env, "KIDSNOTE_PASSWORD")
        cookie_val = _resolve_secret(env, "KIDSNOTE_SESSION_COOKIE")
        if not (username and password) and not cookie_val:
            sys.exit(
                "Kidsnote credentials missing. Set KIDSNOTE_USERNAME + "
                "KIDSNOTE_PASSWORD (auto-login) or KIDSNOTE_SESSION_COOKIE "
                "in .env (local) or as repo secrets (GitHub Actions)."
            )
        # Validates the session up front, so a dead login stops here before
        # any Notion work. In CI the "Kidsnote login" workflow step already
        # logged in and passes the fresh sessionid via KIDSNOTE_SESSION_COOKIE.
        try:
            session = resolve_session(username, password, cookie_val, USER_AGENT)
        except AuthError as e:
            sys.exit(f"Kidsnote login failed ({e.reason}): {e}")
        if session.login_error is not None:
            _LOGGER.warning(
                "Kidsnote id/password login failed (%s: %s); continuing with KIDSNOTE_SESSION_COOKIE",
                session.login_error.reason, session.login_error,
            )
        sess = _baseline_session()
        sess.cookies.set("sessionid", session.sessionid, domain="www.kidsnote.com", path="/")
        _LOGGER.info(
            "Kidsnote session ready (%s)",
            "fresh login" if session.source == "login" else "KIDSNOTE_SESSION_COOKIE",
        )
    else:
        sess = _load_session_from_browser(args.browser)

    # ---- Resolve LLM master toggle once, used in two places ----
    # CLI flag wins; env var (= workflow input passthrough) is the fallback.
    # When the master is on, the per-alimnota callouts AND every LLM
    # dashboard get short-circuited — see below.
    disable_all_llm = args.no_llm or _truthy(_resolve_secret(env, "DISABLE_ALL_LLM"))

    # ---- Notion mirror setup (if requested) -----
    # 2026-05-25: multi-child support. Collect (token, db_id) pairs from
    # numbered secrets so a 2/3/.../N-child account can back up each child
    # into a separate Notion DB without operator-side child_name input.
    #
    # Pairing rules:
    #   * NOTION_DATABASE_ID         (required, slot 1)
    #   * NOTION_DATABASE_ID_2 / _3 / ... (optional, slots 2-10)
    #   * NOTION_TOKEN_<N> reuses NOTION_TOKEN when not provided — most
    #     setups share one integration token across the workspace.
    # API child[0] → slot 1, child[1] → slot 2, etc. Operators who need
    # a different mapping (gender/age order vs API order) can still set
    # KIDSNOTE_CHILD_NAME_<N> to override per slot.
    mirror = None  # legacy single-mirror reference (kept for one-child path)
    skip_ids: set[int] = set()
    page_map: dict[int, str] = {}
    notion_slots: list[dict[str, Any]] = []  # [{db_id, token, child_name_override}]
    if args.publish_to_notion:
        from notion_mirror import NotionMirror  # local module
        token0 = _resolve_secret(env, "NOTION_TOKEN")
        db_id0 = _resolve_secret(env, "NOTION_DATABASE_ID")
        if not token0 or not db_id0:
            sys.exit(
                "NOTION_TOKEN / NOTION_DATABASE_ID missing. "
                "Set them in .env (local) or as repo secrets (GitHub Actions)."
            )
        notion_slots.append({
            "db_id": db_id0,
            "token": token0,
            "name_override": _resolve_secret(env, "KIDSNOTE_CHILD_NAME"),
        })
        # Slots 2..10 are optional. Stop scanning at the first gap.
        for slot in range(2, 11):
            db_n = _resolve_secret(env, f"NOTION_DATABASE_ID_{slot}")
            if not db_n:
                break
            notion_slots.append({
                "db_id": db_n,
                "token": _resolve_secret(env, f"NOTION_TOKEN_{slot}") or token0,
                "name_override": _resolve_secret(env, f"KIDSNOTE_CHILD_NAME_{slot}"),
            })
        if len(notion_slots) > 1:
            _LOGGER.info(
                "Multi-child mode: %d Notion DB slots detected",
                len(notion_slots),
            )
        # For backward compatibility the original single-mirror path below
        # uses the first slot. Multi-child loop wraps this same logic per
        # slot further down.
        mirror = NotionMirror(token=token0, database_id=db_id0)
        # Master switch: propagate to the mirror so per-alimnota callouts
        # (요약 / 자녀일기 / 부모편지) are skipped during publishing.
        if disable_all_llm:
            mirror.disable_llm_callouts = True
            _LOGGER.info(
                "🚫 --no-llm / DISABLE_ALL_LLM is ON — alimnota AI callouts and "
                "the 4 LLM dashboards will all be skipped (plain backup + "
                "statistical dashboards only)."
            )
        try:
            page_map = mirror.existing_report_page_map()
            if args.force_refresh:
                _LOGGER.info(
                    "Notion DB: --force-refresh active, %d existing pages will be "
                    "archived + re-published", len(page_map),
                )
                # Empty skip_ids so every report gets re-published.
            else:
                skip_ids = set(page_map.keys())
                _LOGGER.info(
                    "Notion DB: %d existing report pages will be skipped", len(skip_ids),
                )
        except Exception as e:
            sys.exit(f"Notion DB query failed: {e}")

    # ---- enumerate child + reports -----
    children = _list_children(sess)
    # ALWAYS print the full child roster so operators can self-diagnose
    # multi-child accounts. Previously the only log line was "fetched N
    # reports for child id=...", which silently swallowed the selection.
    if children:
        _LOGGER.info(
            "Account has %d child(ren): %s",
            len(children),
            ", ".join(f"#{i + 1} id={c.get('id')} name={mask_name(c.get('name'))}"
                      for i, c in enumerate(children)),
        )
    # Resolve which child to mirror. CLI flag wins over env var so a manual
    # workflow run can override the repo-secret default without editing it.
    child_name = args.child_name or _resolve_secret(env, "KIDSNOTE_CHILD_NAME")
    target = _pick_child(children, args.child_id, child_name, args.child_index)
    _LOGGER.info(
        "Selected child: id=%s name=%s (override with --child-id / --child-name / KIDSNOTE_CHILD_NAME)",
        target.get("id"), mask_name(target.get("name")),
    )

    reports = _list_reports(sess, int(target["id"]))
    if args.monthly_sample:
        # One report per (YYYY-MM). Reports are newest-first so the first
        # seen per month is the latest of that month.
        seen_months: set[str] = set()
        sampled: list[dict[str, Any]] = []
        for r in reports:
            ym = (r.get("date_written") or "")[:7]
            if ym and ym not in seen_months:
                seen_months.add(ym)
                sampled.append(r)
        reports = sampled
        _LOGGER.info("monthly-sample mode: kept %d reports (one per month)",
                     len(reports))
    elif args.limit:
        reports = reports[: args.limit]
    _LOGGER.info("fetched %d reports for child id=%s",
                 len(reports), target.get("id"))

    # Enrich reports with detail API in one pass — both the publish step and
    # the dashboard stats need fields that only the detail endpoint exposes
    # (meal_status / sleep_hour / weather / food / sleep / nursing / bowel).
    # 1 extra HTTP call per report, but skipping it would force two passes.
    if mirror is not None and reports:
        _LOGGER.info("enriching %d reports with detail API...", len(reports))
        enriched: list[dict[str, Any]] = []
        for i, r in enumerate(reports, 1):
            d = _fetch_report_detail(sess, int(r["id"])) or r
            enriched.append(d)
            if i % 5 == 0 or i == len(reports):
                _LOGGER.info("  detail enrich %d/%d done", i, len(reports))
        reports = enriched
        _LOGGER.info("detail enrich complete")

    # ---- --relabel-existing: fix label wording on already-published pages ----
    if args.relabel_existing and mirror is not None and reports:
        if args.force_refresh:
            _LOGGER.info("🏷️ Relabel skipped: --force-refresh re-publishes every page anyway")
        else:
            from relabel import relabel_report_pages  # local module
            _LOGGER.info("🏷️ Relabel: checking %d published report pages...",
                         sum(1 for r in reports if int(r.get("id") or 0) in page_map))
            relabel_counts = relabel_report_pages(
                mirror, reports, page_map,
                time_left=lambda: _remaining_budget() - DASHBOARD_RESERVE_SEC,
            )
            _LOGGER.info("🏷️ Relabel done: %s",
                         ", ".join(f"{k}={v}" for k, v in relabel_counts.items()))

    # ---- local save (optional) -----
    total_new_files = 0
    if not args.no_local_save:
        args.backup_root.mkdir(parents=True, exist_ok=True)
        if args.dump_raw:
            raw_path = args.backup_root / f"_raw_reports_{target['id']}.json"
            raw_path.write_text(
                json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _LOGGER.info("dumped raw JSON to %s", raw_path)
        for r in reports:
            folder, n_new = _save_report(sess, r, args.backup_root)
            total_new_files += n_new
            if args.verbose:
                _LOGGER.debug("  %s  (+%d files)", folder.name, n_new)
        _LOGGER.info("local save: %d reports, %d new files under %s",
                     len(reports), total_new_files, args.backup_root)

    # Accumulator for attachment counts (across all kinds).
    publish_results: list[dict[str, Any]] = []

    # ---- helper: publish a batch of items via the given publish method ----
    def _publish_batch(
        items: list[dict[str, Any]],
        publish_fn: Any,
        kind_label: str,
    ) -> None:
        # `publish_fn` is mirror.publish_notice / publish_album / publish_menu.
        # Dedup against the same skip_ids set (Report ID is shared column).
        to_pub = [x for x in items if int(x.get("id", 0)) not in skip_ids]
        already = len(items) - len(to_pub)
        total = len(to_pub)
        _LOGGER.info(
            "%s mirror: %d total fetched, %d already in DB (skip), %d to publish",
            kind_label, len(items), already, total,
        )
        pub = 0
        fail = 0
        stopped_early = False
        for idx, x in enumerate(to_pub, start=1):
            # Graceful exit before the 6h hard cap so cron auto-resume
            # has time to write the dashboards instead of being SIGTERM'd
            # mid-page-create.
            if _remaining_budget() < DASHBOARD_RESERVE_SEC:
                _LOGGER.warning(
                    "%s mirror: time budget reached at %d/%d, stopping early. "
                    "Next cron run will resume via dedup.",
                    kind_label, idx - 1, total,
                )
                stopped_early = True
                break
            xid = int(x.get("id", 0))
            pct = (idx / total * 100) if total else 100.0
            try:
                # --force-refresh: archive prior version (if any) so the
                # new prompt-style callouts replace the old ones.
                if args.force_refresh and xid in page_map:
                    mirror.archive_by_report_id(xid, page_map)
                res = publish_fn(x, sess)
                publish_results.append(res)
                pub += 1
                img_tot = res.get("images_uploaded", 0) + res.get("images_failed", 0)
                parts = []
                if img_tot:
                    parts.append(f"img={res['images_uploaded']}/{img_tot}")
                vid_tot = res.get("videos_uploaded", 0) + res.get("videos_failed", 0)
                if vid_tot:
                    parts.append(f"vid={res['videos_uploaded']}/{vid_tot}")
                file_tot = res.get("files_uploaded", 0) + res.get("files_failed", 0)
                if file_tot:
                    parts.append(f"file={res['files_uploaded']}/{file_tot}")
                if res.get("media_links"):
                    parts.append(f"kidsnote-link={res['media_links']}")
                attach_str = (" " + " ".join(parts)) if parts else ""
                _LOGGER.info(
                    "%s %5.1f%% (%d/%d) | Notion +1 id=%d%s",
                    kind_label, pct, idx, total, xid, attach_str,
                )
            except Exception as e:
                fail += 1
                _LOGGER.warning(
                    "%s %5.1f%% (%d/%d) | Notion FAILED id=%d: %s",
                    kind_label, pct, idx, total, xid, e,
                )
        _LOGGER.info(
            "%s mirror DONE: %d new pages, %d already existed, %d failed",
            kind_label, pub, already, fail,
        )

    # ---- Find center_id once for notice + menu sync ----
    enr = target.get("enrollment")
    center_id: int | None = None
    if isinstance(enr, list) and enr:
        center_id = enr[0].get("center_id") or enr[0].get("center")
    elif isinstance(enr, dict):
        center_id = enr.get("center_id") or enr.get("center")

    # ---- Pre-fetch daily menus once, so reports can embed the matching
    # ----- day's menu summary inline. Menus matched to a report are
    # ----- removed from the standalone-publish pool below.
    menus_for_match: dict[str, dict[str, Any]] = {}
    menus_fetched: list[dict[str, Any]] = []
    if mirror is not None and not args.no_menus and center_id:
        try:
            # Always fetch the full menu set for date-matching, regardless
            # of --limit (otherwise a small limit could leave reports
            # without their same-day menu attached).
            menus_fetched = _list_menus(sess, int(center_id))
            for m in menus_fetched:
                d = m.get("date_menu")
                if d:
                    menus_for_match[d] = m
            _LOGGER.info("pre-loaded %d daily menus (matching by date_menu)",
                         len(menus_for_match))
        except Exception as e:
            _LOGGER.warning("menu pre-fetch failed: %s", e)

    matched_menu_ids: set[int] = set()

    # ---- Pre-fetch notices + albums BEFORE publishing anything ---------
    # The publish loop below is year-interleaved (one year's reports + albums
    # + notices, then next year's, ...). To interleave we need all three
    # lists fetched first; previously each list was fetched-then-published
    # in turn, which made the final Notion view group by category instead
    # of by year.
    notices: list[dict[str, Any]] = []
    albums: list[dict[str, Any]] = []
    if mirror is not None and not args.no_notices and center_id:
        try:
            notices = _list_notices(sess, int(center_id))
            if args.limit:
                notices = notices[: args.limit]
            _LOGGER.info("fetched %d notices for center id=%s", len(notices), center_id)
        except Exception as e:
            _LOGGER.warning("notice fetch failed: %s", e)
    if mirror is not None and not args.no_albums:
        try:
            albums = _list_albums(sess, int(target["id"]))
            if args.limit:
                albums = albums[: args.limit]
            _LOGGER.info("fetched %d albums for child id=%s", len(albums), target["id"])
        except Exception as e:
            _LOGGER.warning("album fetch failed: %s", e)

    # ---- Notion mirror: year-interleaved publish ---------------------------
    # User-requested ordering (2026-05-22 revision 2): the default Notion view
    # (Created time descending) should show:
    #
    #     [통계 대시보드 7개]
    #     2026년 앨범 (newest first)         ← 가장 빠른 액세스
    #     2026년 알림장 (newest first)
    #     2026년 공지 (newest first)
    #     2025년 앨범
    #     2025년 알림장
    #     2025년 공지
    #     ...
    #     2018년 공지 (oldest)
    #
    # Because Notion `Created time desc` puts the LAST-published page at
    # the top, we publish in reverse: oldest year first, and within each
    # year `notices → reports → albums` (so albums land last for that
    # year → top of that year's block in the default view). Dashboards
    # are published after all data so they appear at the very top.
    #
    # Why this category order: the user wants alimnotas (daily activity)
    # and albums (photo sets) close to the top because those get read
    # repeatedly; notices (one-off center announcements) are referenced
    # rarely so they sit at the bottom of each year.
    if mirror is not None:
        def _publish_report(detail: dict[str, Any], sess_: requests.Session) -> dict[str, Any]:
            # Same-day menu is only embedded into TEACHER posts (alimnota
            # from the daycare). Parent-written entries describe what the
            # family did at home, so attaching the daycare menu there is
            # nonsensical.
            author_type = (detail.get("author") or {}).get("type") or ""
            date_w = detail.get("date_written")
            attached_menu = None
            if author_type == "teacher" and date_w:
                attached_menu = menus_for_match.get(date_w)
                if attached_menu:
                    matched_menu_ids.add(int(attached_menu["id"]))
            return mirror.publish_report(detail, sess_, attached_menu=attached_menu)

        def _year_of(item: dict[str, Any]) -> str:
            raw = item.get("date_written") or item.get("created") or ""
            return raw[:4] if len(raw) >= 4 else "0000"

        from collections import defaultdict as _dd
        reports_by_year: dict[str, list[dict[str, Any]]] = _dd(list)
        notices_by_year: dict[str, list[dict[str, Any]]] = _dd(list)
        albums_by_year: dict[str, list[dict[str, Any]]] = _dd(list)
        for _r in reports:
            reports_by_year[_year_of(_r)].append(_r)
        for _n in notices:
            notices_by_year[_year_of(_n)].append(_n)
        for _a in albums:
            albums_by_year[_year_of(_a)].append(_a)

        all_years = sorted(
            set(reports_by_year) | set(notices_by_year) | set(albums_by_year)
        )
        _LOGGER.info(
            "Year-interleaved publish: %d year(s) total (%s)",
            len(all_years), ", ".join(all_years) if all_years else "none",
        )
        for _year in all_years:
            yr_reports = sorted(reports_by_year[_year], key=_chronological_key)
            yr_albums = sorted(albums_by_year[_year], key=_chronological_key)
            yr_notices = sorted(notices_by_year[_year], key=_chronological_key)
            # Order within a year: notices → reports → albums. With Notion
            # default Created-time-desc the LAST publish lands at the top,
            # so albums end up on top inside each year, alimnotas in the
            # middle, notices at the bottom.
            if yr_notices:
                _publish_batch(yr_notices, mirror.publish_notice, f"Notice {_year}")
            if yr_reports:
                _publish_batch(yr_reports, _publish_report, f"Report {_year}")
            if yr_albums:
                _publish_batch(yr_albums, mirror.publish_album, f"Album {_year}")

    # ---- Daily menus are NOT published as standalone pages.
    # ---- Same-day menus are inlined into the matching report (above).
    # ---- Menus without a same-day report are intentionally not published.
    if mirror is not None and menus_fetched:
        _LOGGER.info(
            "Menu mirror: %d total fetched, %d inlined into reports, %d had no matching report (skipped)",
            len(menus_fetched), len(matched_menu_ids), len(menus_fetched) - len(matched_menu_ids),
        )

    # ---- Buffer before dashboards so Notion's Created time stamps each
    # ---- dashboard distinctly after every alimnota / notice / album. Without
    # ---- this, the millisecond-precision Created times sometimes interleave
    # ---- a late album publish into the dashboard block under the default
    # ---- desc sort, defeating the "all dashboards on top" layout.
    if mirror is not None:
        import time as _t
        _t.sleep(2.0)

    # ---- 📊 Stats dashboard ----
    if mirror is not None and reports:
        _LOGGER.info("📊 Dashboard: computing stats from %d reports...", len(reports))
        from collections import Counter, defaultdict
        from notion_mirror import NotionMirror

        def _new_year_bucket() -> dict[str, Counter]:
            return {
                "category": Counter(),
                "monthly": Counter(),
                "author": Counter(),
                "sleep": Counter(),
                "meal": Counter(),
                "weather": Counter(),
            }

        cat_counter: Counter[str] = Counter()
        monthly_counter: Counter[str] = Counter()
        author_counter: Counter[str] = Counter()
        sleep_counter: Counter[str] = Counter()
        meal_counter: Counter[str] = Counter()
        weather_counter: Counter[str] = Counter()
        # Per-year breakdown — populated alongside the totals so the
        # dashboard page renders both "전체 누적" and "연도별 상세" sections.
        per_year: dict[str, dict[str, Counter]] = defaultdict(_new_year_bucket)
        per_year_report_total: Counter[str] = Counter()

        for r in reports:
            yr = (r.get("date_written") or "")[:4]
            cats = NotionMirror._classify_categories(r.get("content") or "")
            for c in cats:
                cat_counter[c] += 1
                if yr:
                    per_year[yr]["category"][c] += 1
            ym = (r.get("date_written") or "")[:7]
            if ym:
                monthly_counter[ym] += 1
                if yr:
                    per_year[yr]["monthly"][ym] += 1
            atype = (r.get("author") or {}).get("type") or "unknown"
            author_counter[atype] += 1
            if yr:
                per_year[yr]["author"][atype] += 1
                per_year_report_total[yr] += 1
            if r.get("sleep_hour"):
                sleep_counter[r["sleep_hour"]] += 1
                if yr:
                    per_year[yr]["sleep"][r["sleep_hour"]] += 1
            if r.get("meal_status"):
                meal_counter[r["meal_status"]] += 1
                if yr:
                    per_year[yr]["meal"][r["meal_status"]] += 1
            if r.get("weather"):
                weather_counter[r["weather"]] += 1
                if yr:
                    per_year[yr]["weather"][r["weather"]] += 1

        # 📎 totals + the 📥 list of files Notion can't hold, counted from the
        # kidsnote metadata of everything fetched (not just this run's
        # uploads, which reset to 0 on every quiet 6-hourly run).
        from notion_mirror import attachment_stats
        att = attachment_stats(
            [("알림장", reports), ("공지", notices), ("앨범", albums)],
            _live_page_ids(page_map, publish_results, args.force_refresh),
            mirror.max_image_bytes,
        )
        _LOGGER.info(
            "📎 attachments: %d photos, %d videos, %d files; %d over the %s Notion limit "
            "(listed with kidsnote links on the 📊 dashboard)",
            att["images"], att["videos"], att["files"], len(att["oversized"]),
            f"{mirror.max_image_bytes / 1_000_000:g}MB",
        )

        # Per-year stats payload (newest first)
        per_year_payload: list[dict[str, Any]] = []
        for yr in sorted(per_year.keys(), reverse=True):
            buckets = per_year[yr]
            per_year_payload.append({
                "year": yr,
                "reports_total": per_year_report_total[yr],
                "category_counts": dict(buckets["category"]),
                "monthly_report_counts": dict(buckets["monthly"]),
                "author_counts": dict(buckets["author"]),
                "sleep_hour_dist": dict(buckets["sleep"]),
                "meal_status_dist": dict(buckets["meal"]),
                "weather_dist": dict(buckets["weather"]),
            })

        stats = {
            "reports_total": len(reports),
            "notices_total": len(notices),
            "albums_total": len(albums),
            "menus_total": len(menus_fetched),
            "category_counts": dict(cat_counter),
            "monthly_report_counts": dict(monthly_counter),
            "author_counts": dict(author_counter),
            "sleep_hour_dist": dict(sleep_counter),
            "meal_status_dist": dict(meal_counter),
            "weather_dist": dict(weather_counter),
            "attachments": att,
            "per_year": per_year_payload,
        }
        try:
            mirror.publish_dashboard(stats)
            _LOGGER.info("📊 Dashboard updated (reports=%d, categories=%d, months=%d)",
                         len(reports), len(cat_counter), len(monthly_counter))
        except Exception as e:
            _LOGGER.warning("dashboard publish failed: %s", e)
        # Spacing between dashboards so their Created time stamps strictly
        # increase, preserving the "all dashboards on top" layout under
        # Notion's default Created-time-desc view. 0.5s is enough for the
        # API to commit before the next page POST without bloating runtime.
        import time as _t
        _t.sleep(0.5)

    # 📅 작년추억 (Phase 2) was removed 2026-05-27 — see notion_mirror.py
    # MEMORIES_REPORT_ID comment for rationale.

    # ---- 🥗 영양 분석 (Phase 3) ----
    if mirror is not None and menus_fetched:
        _LOGGER.info("🥗 Nutrition: analyzing %d menus...", len(menus_fetched))
        from collections import Counter as _Counter, defaultdict as _defaultdict
        from notion_mirror import NUTRITION_GROUPS

        group_counter: _Counter[str] = _Counter()
        monthly_group: dict[str, _Counter[str]] = _defaultdict(_Counter)
        item_counter: _Counter[str] = _Counter()

        for menu in menus_fetched:
            ym = (menu.get("date_menu") or "")[:7]
            full_text_parts = []
            for fld in ("morning", "morning_snack", "lunch", "afternoon_snack", "dinner"):
                txt = menu.get(fld) or ""
                if txt.strip():
                    full_text_parts.append(txt)
            full_text = "\n".join(full_text_parts)
            for line in full_text.split("\n"):
                item = line.strip()
                if not item:
                    continue
                item_counter[item] += 1
                for group, keywords in NUTRITION_GROUPS:
                    for kw in keywords:
                        if kw in item:
                            group_counter[group] += 1
                            if ym:
                                monthly_group[ym][group] += 1
                            break

        nutrition_stats = {
            "menus_total": len(menus_fetched),
            "nutrition_group_counts": dict(group_counter),
            "nutrition_monthly": {ym: dict(c) for ym, c in monthly_group.items()},
            "top_menu_items": item_counter.most_common(15),
        }
        try:
            mirror.publish_nutrition(nutrition_stats)
            _LOGGER.info("🥗 Nutrition page updated (%d distinct menu items, %d groups)",
                         len(item_counter), len(group_counter))
        except Exception as e:
            _LOGGER.warning("nutrition publish failed: %s", e)
        import time as _t
        _t.sleep(0.5)

    # ---- 📖 매월 성장 스토리 / 🌟 마일스톤 / 🌱 분기 관심사 / 💌 감사 카드
    # ---- (LLM-driven; auto-skipped when Ollama isn't reachable) ----
    #
    # Cron auto-resume: regenerating the 4 LLM dashboards costs ~1.5 hours
    # of Ollama time. With the workflow on a 6-hour cron schedule we don't
    # want to burn that on every run; only do it when there's actually new
    # content to incorporate, when the user explicitly asked for a refresh,
    # OR when one of the four LLM dashboards is missing from the DB (the
    # previous cycle's wall-clock cap may have skipped it; the next cycle
    # should fill the gap without needing operator intervention).
    LLM_DASHBOARD_SENTINELS = {-4, -5, -6, -7}  # growth/milestones/interests/teacher_thanks
    missing_llm_dashboards = (
        bool(LLM_DASHBOARD_SENTINELS - set(page_map.keys()))
        if mirror is not None else False
    )

    # Partial-growth-story self-heal: the publish_growth_story singleton
    # is overwritten in full each cycle, so a wall-clock-cap truncation
    # mid-loop leaves a permanently incomplete page (no new alimnotas
    # later means the dashboard skip path takes over and the gap never
    # closes). Detect the leftover placeholder text from
    # notion_mirror.publish_growth_story and treat it as "regenerate me"
    # the same way a missing sentinel would.
    incomplete_growth_story = False
    if mirror is not None and -4 in page_map:
        try:
            growth_page_id = page_map[-4]
            children = mirror.session.get(
                f"https://api.notion.com/v1/blocks/{growth_page_id}/children?page_size=100",
                headers=mirror._headers(), timeout=mirror.timeout,
            )
            if children.ok:
                placeholder_marker = "다음 cron 사이클에서 보충됩니다"
                for block in children.json().get("results") or []:
                    if block.get("type") != "paragraph":
                        continue
                    rt = (block.get("paragraph") or {}).get("rich_text") or []
                    txt = "".join(seg.get("plain_text", "") for seg in rt)
                    if placeholder_marker in txt:
                        incomplete_growth_story = True
                        break
        except Exception as e:
            _LOGGER.warning("growth-story completeness check failed: %s", e)

    new_pages_published = len(publish_results)
    should_run_llm_dashboards = (
        mirror is not None and reports
        and (
            new_pages_published > 0
            or args.force_refresh
            or missing_llm_dashboards
            or incomplete_growth_story
        )
    )
    if mirror is not None and reports and not should_run_llm_dashboards:
        _LOGGER.info(
            "LLM dashboards: skipping (no new alimnotas added this run; "
            "set force_refresh=true to force regeneration)"
        )
    elif missing_llm_dashboards and new_pages_published == 0 and not args.force_refresh:
        missing_set = LLM_DASHBOARD_SENTINELS - set(page_map.keys())
        _LOGGER.info(
            "LLM dashboards: running to fill missing sentinel(s) %s",
            sorted(missing_set),
        )
    elif incomplete_growth_story and new_pages_published == 0 and not args.force_refresh:
        _LOGGER.info(
            "LLM dashboards: running to fill partial growth story (placeholder months detected)",
        )
    if should_run_llm_dashboards:
        cname = (reports[0].get("child_name") or "") if reports else ""

        # Per-dashboard skip toggles. Either the CLI flag (manual local run)
        # OR the matching DISABLE_* env var (workflow input → repo secret →
        # script env) turns the corresponding page off. Each toggle is
        # independent because graduate-backup operators reported the
        # 💌 감사 카드 and 📖 성장 스토리 read awkwardly due to vocative
        # errors, while 🌟 마일스톤 / 🌱 관심사 are statistical and read fine.
        # The master --no-llm toggle short-circuits all four at once.
        skip_growth = disable_all_llm or args.no_growth_story or _truthy(_resolve_secret(env, "DISABLE_GROWTH_STORY"))
        skip_milestones = disable_all_llm or args.no_milestones or _truthy(_resolve_secret(env, "DISABLE_MILESTONES"))
        skip_interests = disable_all_llm or args.no_interests or _truthy(_resolve_secret(env, "DISABLE_INTERESTS"))
        skip_thanks = disable_all_llm or args.no_teacher_thanks or _truthy(_resolve_secret(env, "DISABLE_TEACHER_THANKS"))

        # Group by month + quarter
        from collections import defaultdict
        by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_quarter: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in reports:
            d = r.get("date_written") or ""
            if len(d) < 7:
                continue
            ym = d[:7]
            by_month[ym].append(r)
            try:
                year, month = int(d[:4]), int(d[5:7])
                q = (month - 1) // 3 + 1
                by_quarter[f"{year} Q{q}"].append(r)
            except (ValueError, TypeError):
                pass

        # Each LLM dashboard checks the time budget before starting — if
        # we're close to the 6h cap (e.g. force_refresh re-published a
        # lot of alimnotas), the heavy ones get deferred to the next
        # cron run instead of being SIGTERM'd mid-page-create.
        #
        # 2026-05-24: also enforce a per-dashboard wall-clock cap so a
        # single LLM-heavy dashboard can't drag the whole job past 6h.
        # 8h-10h runs were observed on cycles 2+ once reports accumulated
        # and publish_milestones started scanning 100+ entries. The cap
        # makes the worst case: 4 dashboards × DASHBOARD_PER_CAP_SEC.
        MIN_BUDGET_FAST = 60       # 1 min for quick dashboards
        MIN_BUDGET_SLOW = 900      # 15 min — heuristic for big LLM ones
        # 2026-05-26: cap raised 18min → 45min. The previous 18min cap was
        # halting growth_story at 4/15 months on a 15-month account, so
        # only 2026 was covered and 2025 was permanently skipped (the
        # singleton was overwritten by the partial result each cycle).
        # 45min lets all 15 months finish in one pass on 8b CPU Ollama.
        DASHBOARD_PER_CAP = 45 * 60  # 45 min wall-clock cap per dashboard

        def _set_dashboard_cap() -> None:
            # Each dashboard gets the smaller of (per-dashboard cap,
            # remaining workflow budget). Prevents the last few dashboards
            # from running into the 6h GHA hard cap.
            mirror.dashboard_max_seconds = min(
                DASHBOARD_PER_CAP, max(60, _remaining_budget() - 60),
            )

        if skip_growth:
            _LOGGER.info("📖 Growth story: skipped (--no-growth-story / DISABLE_GROWTH_STORY)")
        elif _remaining_budget() < MIN_BUDGET_SLOW:
            _LOGGER.warning(
                "📖 Growth story: skipped (low time budget %.0fs, "
                "next cron run will retry)", _remaining_budget(),
            )
        else:
            _LOGGER.info("📖 Growth story: %d months", len(by_month))
            _set_dashboard_cap()
            try:
                res = mirror.publish_growth_story(by_month, cname)
                _LOGGER.info("📖 Growth story page: %s",
                             "OK" if res else "FAILED (see WARNING above for cause)")
            except Exception as e:
                _LOGGER.warning("growth story publish failed: %s", e)
            import time as _t
            _t.sleep(0.5)

        if skip_milestones:
            _LOGGER.info("🌟 Milestones: skipped (--no-milestones / DISABLE_MILESTONES)")
        elif _remaining_budget() < MIN_BUDGET_SLOW:
            _LOGGER.warning(
                "🌟 Milestones: skipped (low time budget %.0fs)",
                _remaining_budget(),
            )
        else:
            _LOGGER.info("🌟 Milestones: scanning %d reports...", len(reports))
            _set_dashboard_cap()
            try:
                res = mirror.publish_milestones(reports, cname)
                _LOGGER.info("🌟 Milestones page: %s",
                             "OK" if res else "FAILED (see WARNING above for cause)")
            except Exception as e:
                _LOGGER.warning("milestones publish failed: %s", e)
            import time as _t
            _t.sleep(0.5)

        if skip_interests:
            _LOGGER.info("🌱 Interests: skipped (--no-interests / DISABLE_INTERESTS)")
        elif _remaining_budget() < MIN_BUDGET_FAST:
            _LOGGER.warning(
                "🌱 Interests: skipped (low time budget %.0fs)",
                _remaining_budget(),
            )
        else:
            _LOGGER.info("🌱 Interests: %d quarters", len(by_quarter))
            _set_dashboard_cap()
            try:
                res = mirror.publish_interests(by_quarter, cname)
                _LOGGER.info("🌱 Interests page: %s",
                             "OK" if res else "FAILED (see WARNING above for cause)")
            except Exception as e:
                _LOGGER.warning("interests publish failed: %s", e)
            import time as _t
            _t.sleep(0.5)

        if skip_thanks:
            _LOGGER.info("💌 Teacher thanks: skipped (--no-teacher-thanks / DISABLE_TEACHER_THANKS)")
        elif _remaining_budget() < MIN_BUDGET_FAST:
            _LOGGER.warning(
                "💌 Teacher thanks: skipped (low time budget %.0fs)",
                _remaining_budget(),
            )
        else:
            _LOGGER.info("💌 Teacher thanks: composing letter...")
            _set_dashboard_cap()
            try:
                res = mirror.publish_teacher_thanks(reports, cname)
                _LOGGER.info("💌 Teacher thanks page: %s",
                             "OK" if res else "FAILED (see WARNING above; or no teacher posts)")
            except Exception as e:
                _LOGGER.warning("teacher thanks publish failed: %s", e)

    _LOGGER.info(
        "Run complete. Elapsed %.0fs (%.1fh). New pages published this run: %d.",
        time.monotonic() - _START_TIME,
        (time.monotonic() - _START_TIME) / 3600,
        len(publish_results),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
