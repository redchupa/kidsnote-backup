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
    ap.add_argument("--limit", type=int,
                    help="Only sync the N most recent reports (debugging).")
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
    if args.limit:
        reports = reports[: args.limit]
    _LOGGER.info("fetched %d reports for child id=%s",
                 len(reports), target.get("id"))

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

    # ---- Notion mirror (optional) -----
    if mirror is not None:
        # Pre-count: how many reports are actually new (need publishing) vs
        # already in Notion DB. Used for percentage progress in the log.
        to_publish = [r for r in reports if int(r.get("id", 0)) not in skip_ids]
        already_existed = len(reports) - len(to_publish)
        total_target = len(to_publish)
        _LOGGER.info(
            "Notion mirror: %d total fetched, %d already in DB (skip), %d to publish",
            len(reports), already_existed, total_target,
        )

        published = 0
        failed = 0
        for idx, r in enumerate(to_publish, start=1):
            rid = int(r.get("id", 0))
            pct = (idx / total_target * 100) if total_target else 100.0
            try:
                result = mirror.publish_report(r, sess)
                published += 1
                # Compact summary: each ratio shown only if any of that kind exists.
                attach_parts: list[str] = []
                img_tot = result["images_uploaded"] + result["images_failed"]
                if img_tot:
                    attach_parts.append(f"img={result['images_uploaded']}/{img_tot}")
                vid_tot = result["videos_uploaded"] + result["videos_failed"]
                if vid_tot:
                    attach_parts.append(f"vid={result['videos_uploaded']}/{vid_tot}")
                file_tot = result["files_uploaded"] + result["files_failed"]
                if file_tot:
                    attach_parts.append(f"file={result['files_uploaded']}/{file_tot}")
                attach_str = (" " + " ".join(attach_parts)) if attach_parts else ""
                _LOGGER.info(
                    "Progress %5.1f%% (%d/%d) | Notion +1 rid=%d%s",
                    pct, idx, total_target, rid, attach_str,
                )
            except Exception as e:
                failed += 1
                _LOGGER.warning(
                    "Progress %5.1f%% (%d/%d) | Notion FAILED rid=%d: %s",
                    pct, idx, total_target, rid, e,
                )
        _LOGGER.info(
            "Notion mirror DONE: %d new pages, %d already existed, %d failed",
            published, already_existed, failed,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
