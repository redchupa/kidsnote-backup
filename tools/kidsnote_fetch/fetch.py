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
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

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
    sess: requests.Session, url: str, page_size: int = 100, max_pages: int = 200
) -> list[dict[str, Any]]:
    """Walk a cursor-paginated DRF endpoint until exhausted.

    Some kidsnote endpoints (notices, albums) use cursor-style pagination
    where `next` is either a full URL or a base64 token. This helper
    handles both forms and aggregates results.
    """
    out: list[dict[str, Any]] = []
    next_url: str | None = url
    pages = 0
    while next_url and pages < max_pages:
        sep = "&" if "?" in next_url else "?"
        # If next_url already has page_size we don't re-add it.
        if "page_size=" not in next_url:
            next_url = f"{next_url}{sep}page_size={page_size}"
        r = sess.get(next_url, timeout=60)
        r.raise_for_status()
        body = r.json()
        out.extend(body.get("results") or [])
        nxt = body.get("next")
        if not nxt:
            break
        if nxt.startswith("http"):
            next_url = nxt
        else:
            # bare cursor token — append to original endpoint
            base = url.split("?")[0]
            next_url = f"{base}?page_size={page_size}&cursor={nxt}"
        pages += 1
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
                        name, url, folder.name, e)
        return False


def _resolve_secret(env: dict[str, str], key: str) -> str:
    """Read a credential from either the .env file or the process environment.

    The GitHub Actions workflow injects secrets via os.environ, so we treat
    that as authoritative if present; otherwise we fall back to the .env file
    used for local runs.
    """
    return os.environ.get(key, "") or env.get(key, "")


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
                    help="session-cookie-env (default): reads KIDSNOTE_SESSION_COOKIE "
                         "(value of `sessionid`) from env. Required for headless CI. "
                         "browser-cookie: pulls cookies from a locally logged-in browser.")
    ap.add_argument("--env-file", type=Path,
                    default=Path(__file__).resolve().parents[2] / ".env",
                    help="Path to the .env that holds KIDSNOTE_SESSION_COOKIE / NOTION_TOKEN / "
                         "NOTION_DATABASE_ID. Ignored if the same names exist in process env (CI mode).")
    ap.add_argument("--browser", default="auto",
                    choices=["chrome", "firefox", "edge", "auto"],
                    help="(--auth-mode browser-cookie only)")
    ap.add_argument("--child-id", type=int,
                    help="Pick a specific child id; defaults to the first one.")
    ap.add_argument("--no-menus", action="store_true",
                    help="Skip daily lunch menu sync.")
    ap.add_argument("--no-notices", action="store_true",
                    help="Skip center-wide notice sync.")
    ap.add_argument("--no-albums", action="store_true",
                    help="Skip photo album sync.")
    ap.add_argument("--limit", type=int,
                    help="Only sync the N most recent reports (debugging).")
    ap.add_argument("--monthly-sample", action="store_true",
                    help="For unit testing: pick one report per month (newest "
                         "of each calendar month), instead of N most-recent. "
                         "Useful to verify coverage across a wide date range.")
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
        cookie_val = _resolve_secret(env, "KIDSNOTE_SESSION_COOKIE")
        if not cookie_val:
            sys.exit(
                "KIDSNOTE_SESSION_COOKIE missing. Extract the `sessionid` cookie "
                "value for kidsnote.com from a logged-in browser session and "
                "set it in .env (local) or as a repo secret (GitHub Actions)."
            )
        sess = _baseline_session()
        sess.cookies.set("sessionid", cookie_val, domain="www.kidsnote.com", path="/")
        _LOGGER.info("Using sessionid from KIDSNOTE_SESSION_COOKIE env var")
    else:
        sess = _load_session_from_browser(args.browser)

    # ---- Notion mirror setup (if requested) -----
    mirror = None
    skip_ids: set[int] = set()
    if args.publish_to_notion:
        from notion_mirror import NotionMirror  # local module
        token = _resolve_secret(env, "NOTION_TOKEN")
        db_id = _resolve_secret(env, "NOTION_DATABASE_ID")
        if not token or not db_id:
            sys.exit(
                "NOTION_TOKEN / NOTION_DATABASE_ID missing. "
                "Set them in .env (local) or as repo secrets (GitHub Actions)."
            )
        mirror = NotionMirror(token=token, database_id=db_id)
        try:
            skip_ids = mirror.existing_report_ids()
            _LOGGER.info("Notion DB: %d existing report pages will be skipped", len(skip_ids))
        except Exception as e:
            sys.exit(f"Notion DB query failed: {e}")

    # ---- enumerate child + reports -----
    children = _list_children(sess)
    if not children:
        sys.exit("no children found on this account.")
    if args.child_id:
        target = next((c for c in children if c.get("id") == args.child_id), None)
        if target is None:
            sys.exit(
                f"child id {args.child_id} not in your profile. "
                f"Available: {[(c.get('id'), c.get('name')) for c in children]}"
            )
    else:
        target = children[0]

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
        for r in reports:
            d = _fetch_report_detail(sess, int(r["id"])) or r
            enriched.append(d)
        reports = enriched
        _LOGGER.info("detail enrich done")

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
        for idx, x in enumerate(to_pub, start=1):
            xid = int(x.get("id", 0))
            pct = (idx / total * 100) if total else 100.0
            try:
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

    # ---- Notion mirror: alimnota reports (already enriched above) -----
    notices: list[dict[str, Any]] = []
    albums: list[dict[str, Any]] = []
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

        _publish_batch(reports, _publish_report, "Report")

    # ---- Notion mirror: notices (center-wide) -----
    if mirror is not None and not args.no_notices and center_id:
        try:
            notices = _list_notices(sess, int(center_id))
            if args.limit:
                notices = notices[: args.limit]
            _LOGGER.info("fetched %d notices for center id=%s", len(notices), center_id)
            _publish_batch(notices, mirror.publish_notice, "Notice")
        except Exception as e:
            _LOGGER.warning("notice fetch failed: %s", e)

    # ---- Notion mirror: albums (per child) -----
    if mirror is not None and not args.no_albums:
        try:
            albums = _list_albums(sess, int(target["id"]))
            if args.limit:
                albums = albums[: args.limit]
            _LOGGER.info("fetched %d albums for child id=%s", len(albums), target["id"])
            _publish_batch(albums, mirror.publish_album, "Album")
        except Exception as e:
            _LOGGER.warning("album fetch failed: %s", e)

    # ---- Daily menus are NOT published as standalone pages.
    # ---- Same-day menus are inlined into the matching report (above).
    # ---- Menus without a same-day report are intentionally not published.
    if mirror is not None and menus_fetched:
        _LOGGER.info(
            "Menu mirror: %d total fetched, %d inlined into reports, %d had no matching report (skipped)",
            len(menus_fetched), len(matched_menu_ids), len(menus_fetched) - len(matched_menu_ids),
        )

    # ---- 📊 Stats dashboard ----
    if mirror is not None and reports:
        _LOGGER.info("📊 Dashboard: computing stats from %d reports...", len(reports))
        from collections import Counter
        from notion_mirror import NotionMirror

        cat_counter: Counter[str] = Counter()
        monthly_counter: Counter[str] = Counter()
        author_counter: Counter[str] = Counter()
        sleep_counter: Counter[str] = Counter()
        meal_counter: Counter[str] = Counter()
        weather_counter: Counter[str] = Counter()

        for r in reports:
            for c in NotionMirror._classify_categories(r.get("content") or ""):
                cat_counter[c] += 1
            ym = (r.get("date_written") or "")[:7]
            if ym:
                monthly_counter[ym] += 1
            atype = (r.get("author") or {}).get("type") or "unknown"
            author_counter[atype] += 1
            if r.get("sleep_hour"):
                sleep_counter[r["sleep_hour"]] += 1
            if r.get("meal_status"):
                meal_counter[r["meal_status"]] += 1
            if r.get("weather"):
                weather_counter[r["weather"]] += 1

        att = {
            "images": sum(p.get("images_uploaded", 0) for p in publish_results),
            "videos": sum(p.get("videos_uploaded", 0) for p in publish_results),
            "videos_skipped": sum(p.get("videos_failed", 0) for p in publish_results),
            "files": sum(p.get("files_uploaded", 0) for p in publish_results),
            "files_skipped": sum(p.get("files_failed", 0) for p in publish_results),
        }

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
        }
        try:
            mirror.publish_dashboard(stats)
            _LOGGER.info("📊 Dashboard updated (reports=%d, categories=%d, months=%d)",
                         len(reports), len(cat_counter), len(monthly_counter))
        except Exception as e:
            _LOGGER.warning("dashboard publish failed: %s", e)

    # ---- 📅 오늘의 추억 (Phase 2) ----
    if mirror is not None:
        from datetime import datetime as _dt
        today = _dt.now().date()
        today_md = today.strftime("%m-%d")
        memories_by_year: dict[int, list[dict[str, Any]]] = {}
        _LOGGER.info("📅 Memories: scanning Notion DB for same-day (%s) pages...", today_md)

        # Query the entire Notion DB once to find same-MM-DD alimnota
        # pages from prior years.
        try:
            cur: str | None = None
            while True:
                body: dict[str, Any] = {"page_size": 100}
                if cur:
                    body["start_cursor"] = cur
                rq = mirror.session.post(
                    f"https://api.notion.com/v1/databases/{mirror.database_id}/query",
                    headers=mirror._headers(),
                    json=body,
                    timeout=mirror.timeout,
                )
                rq.raise_for_status()
                data = rq.json()
                for page in data.get("results") or []:
                    props = page.get("properties") or {}
                    # Look up date + title + report_id by their resolved names
                    if not mirror._prop_date or not mirror._prop_title or not mirror._prop_report_id:
                        continue
                    rid_obj = (props.get(mirror._prop_report_id) or {})
                    rid = rid_obj.get("number")
                    if rid is None or rid < 0:
                        continue  # skip system pages (dashboard / memories / nutrition)
                    date_obj = (props.get(mirror._prop_date) or {}).get("date") or {}
                    page_date = date_obj.get("start") or ""
                    if not page_date or len(page_date) < 10:
                        continue
                    if page_date[5:10] != today_md:
                        continue
                    year = int(page_date[:4])
                    if year == today.year:
                        continue  # skip today's own
                    title_rt = (props.get(mirror._prop_title) or {}).get("title") or []
                    title_text = "".join(seg.get("plain_text", "") for seg in title_rt)
                    memories_by_year.setdefault(year, []).append({
                        "notion_page_id": page["id"],
                        "notion_title": title_text,
                        "date_written": page_date,
                    })
                if not data.get("has_more"):
                    break
                cur = data.get("next_cursor")
        except Exception as e:
            _LOGGER.warning("memories query failed: %s", e)

        try:
            mirror.publish_memories(today.isoformat(), memories_by_year)
            n = sum(len(v) for v in memories_by_year.values())
            _LOGGER.info("📅 Memories page updated (%d entries across %d year(s))",
                         n, len(memories_by_year))
        except Exception as e:
            _LOGGER.warning("memories publish failed: %s", e)

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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
