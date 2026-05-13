"""Mirror raw Kidsnote reports directly to a Notion database.

Designed for the GitHub Actions workflow at
.github/workflows/kidsnote-to-notion.yml:

    Kidsnote /api/v1_2/.../reports/   →   Notion pages (one per report)

Each Notion page holds the teacher's raw alimnota text + the original
Kakao-CDN photos, period. No LLM rewriting, no translation.

Dedup:
    Each Notion page stores the Kidsnote `report_id` in a `Report ID`
    number property. Before publishing, we query the database once and
    skip any report whose id is already there. Notion is the source of
    truth; no state.json or git artifact.

Privacy guards:
    - EXIF GPS + MakerNote stripped in-memory before upload.
    - Photo bytes that exceed `max_image_bytes` (Notion free-tier cap
      5 MB) are resized + JPEG-quality-stepped via the shared
      kidsnote_diary_suite.publisher.image_compress helper.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Any

import requests

_LOGGER = logging.getLogger(__name__)


def compress_image_to_bytes(
    raw: bytes,
    target_bytes: int,
    *,
    max_side: int = 1920,
    quality_steps: tuple[int, ...] = (85, 75, 65, 60),
) -> tuple[bytes, bool]:
    """Shrink an image so the encoded bytes fit within target_bytes.

    Already small enough → returned as-is, was_compressed=False.
    Otherwise: EXIF transpose → iterative resize (longest side capped at
    `max_side`) and JPEG quality step-down until the buffer fits the
    target, or the smallest setting is reached.

    Returns (bytes, was_compressed).
    """
    if len(raw) <= target_bytes:
        return raw, False
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return raw, False

    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
    except Exception:
        return raw, False

    # Cap the longest side to max_side without enlarging.
    if max(img.size) > max_side:
        ratio = max_side / max(img.size)
        new_size = (int(img.width * ratio), int(img.height * ratio))
        img = img.resize(new_size, Image.LANCZOS)

    for q in quality_steps:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q, optimize=True, progressive=True)
        data = buf.getvalue()
        if len(data) <= target_bytes:
            return data, True

    # Last resort: return the smallest-quality output even if still oversized.
    return data, True

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
DEFAULT_MAX_IMAGE_BYTES = 5_000_000   # Notion free-tier per-file cap.
MAX_BLOCK_TEXT = 1900                 # Notion paragraph rich_text limit (2000).

# Kidsnote life-record status codes → human Korean. Unknown values are
# rendered as-is, so missing entries here just degrade gracefully.
SLEEP_HOUR_KO = {
    "none": "안 잠",
    "under_30m": "30분 이내",
    "30m_to_1": "30분~1시간",
    "1_to_1.5": "1~1.5시간",
    "1.5_to_2": "1.5~2시간",
    "over_2": "2시간 이상",
}
STATUS_KO = {
    "good": "좋음",
    "average": "보통",
    "bad": "안 좋음",
    "normal": "정상",
    "high": "높음",
    "low": "낮음",
    "soft": "묽음",
    "hard": "딱딱",
    "none": "없음",
    "fixed": "정해진 식단",
    "sick": "아픔",
    "fine": "양호",
    "trimmed": "정리됨",
    "needs_trim": "정리 필요",
    "active": "활발",
    "calm": "차분",
}
WEATHER_KO = {
    "sunny": "☀️ 맑음",
    "partly_cloudy": "⛅ 구름 조금",
    "cloudy": "☁️ 흐림",
    "rainy": "🌧️ 비",
    "snowy": "❄️ 눈",
    "foggy": "🌫️ 안개",
    "windy": "💨 바람",
    "stormy": "⛈️ 폭풍",
    "hot": "🥵 더움",
    "cold": "🥶 추움",
}

# The target database's actual property names are discovered at runtime via
# `GET /v1/databases/{id}`. This lets the Notion Korean UI's auto-translated
# defaults ("이름", "날짜") and user-chosen variants ("리포트 ID") work
# without forcing the user to recreate the DB in English. Name preferences
# (first match wins); otherwise we fall back to the first property of the
# right *type*.
TITLE_NAME_CANDIDATES = ("Name", "이름", "제목")
REPORT_ID_NAME_CANDIDATES = ("Report ID", "리포트 ID", "리포트id", "report_id", "보고서 ID")
DATE_NAME_CANDIDATES = ("Date", "날짜")


def _strip_gps_in_memory(raw: bytes) -> bytes:
    """Drop GPS + MakerNote EXIF tags from a JPEG buffer. Returns possibly
    the same bytes object if the file is not a JPEG or piexif isn't available.
    """
    try:
        import piexif
    except ImportError:
        return raw
    try:
        exif = piexif.load(raw)
    except Exception:
        return raw
    changed = False
    if exif.get("GPS"):
        exif["GPS"] = {}
        changed = True
    exif_ifd = exif.get("Exif") or {}
    if piexif.ExifIFD.MakerNote in exif_ifd:
        exif_ifd.pop(piexif.ExifIFD.MakerNote, None)
        exif["Exif"] = exif_ifd
        changed = True
    if not changed:
        return raw
    try:
        out = io.BytesIO()
        piexif.insert(piexif.dump(exif), raw, out)
        return out.getvalue()
    except Exception:
        return raw


class NotionMirror:
    """Push Kidsnote reports as Notion DB pages with built-in dedup."""

    def __init__(
        self,
        token: str,
        database_id: str,
        *,
        max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
        strip_exif_gps: bool = True,
        session: requests.Session | None = None,
        timeout: int = 60,
    ) -> None:
        self.token = token
        self.database_id = database_id
        self.max_image_bytes = max_image_bytes
        self.strip_exif_gps = strip_exif_gps
        self.session = session or requests.Session()
        self.timeout = timeout
        # Resolved on first use via `_resolve_schema()`.
        self._prop_title: str | None = None
        self._prop_report_id: str | None = None
        self._prop_date: str | None = None

    def _resolve_schema(self) -> None:
        """Discover the title / number / date property names from the live DB."""
        if self._prop_report_id is not None:
            return  # already resolved
        r = self.session.get(
            f"{NOTION_API}/databases/{self.database_id}",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": NOTION_VERSION,
            },
            timeout=self.timeout,
        )
        if r.status_code == 404:
            raise RuntimeError(
                "Notion DB not found. Either the database_id is wrong or "
                "your integration is not shared with the DB "
                "(Notion → DB → Connections → add the integration)."
            )
        r.raise_for_status()
        props: dict[str, Any] = r.json().get("properties") or {}

        def pick(candidates: tuple[str, ...], wanted_type: str) -> str | None:
            for name in candidates:
                meta = props.get(name)
                if meta and meta.get("type") == wanted_type:
                    return name
            for name, meta in props.items():
                if meta.get("type") == wanted_type:
                    return name
            return None

        self._prop_title = pick(TITLE_NAME_CANDIDATES, "title")
        self._prop_report_id = pick(REPORT_ID_NAME_CANDIDATES, "number")
        self._prop_date = pick(DATE_NAME_CANDIDATES, "date")

        if not self._prop_title:
            raise RuntimeError("DB has no title property (every Notion DB has one - check the DB).")
        if not self._prop_report_id:
            raise RuntimeError(
                "DB is missing a Number property for `Report ID`. "
                "Add a Number column named 'Report ID' (or 'Report ID' / '리포트 ID')."
            )
        _LOGGER.info(
            "Notion DB schema resolved: title=%r, number=%r, date=%r",
            self._prop_title, self._prop_report_id, self._prop_date,
        )

    # ----------------------------------------------------------- internals

    def _headers(self, *, content_type: str = "application/json") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": content_type,
        }

    # ----------------------------------------------------------- dedup

    def existing_report_ids(self) -> set[int]:
        """Walk the whole database once, return every existing `Report ID`."""
        self._resolve_schema()
        assert self._prop_report_id is not None
        out: set[int] = set()
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            r = self.session.post(
                f"{NOTION_API}/databases/{self.database_id}/query",
                headers=self._headers(),
                json=body,
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
            for page in data.get("results") or []:
                props = page.get("properties") or {}
                rid_prop = props.get(self._prop_report_id) or {}
                rid = rid_prop.get("number")
                if rid is not None:
                    try:
                        out.add(int(rid))
                    except (TypeError, ValueError):
                        pass
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return out

    # ----------------------------------------------------------- image upload

    def _upload_one_image(
        self,
        raw: bytes,
        filename_hint: str,
    ) -> str | None:
        """EXIF strip → shrink → file_uploads. Returns the file_upload_id or None on failure."""
        is_jpeg = filename_hint.lower().endswith((".jpg", ".jpeg"))
        if self.strip_exif_gps and is_jpeg:
            raw = _strip_gps_in_memory(raw)
        data, was_compressed = compress_image_to_bytes(raw, self.max_image_bytes)
        if len(data) > self.max_image_bytes:
            _LOGGER.warning(
                "image %s still %d bytes after compression > %d cap; skipping",
                filename_hint, len(data), self.max_image_bytes,
            )
            return None

        if was_compressed:
            mime = "image/jpeg"
            send_name = filename_hint.rsplit(".", 1)[0] + ".jpg"
        elif is_jpeg:
            mime = "image/jpeg"
            send_name = filename_hint
        elif filename_hint.lower().endswith(".png"):
            mime = "image/png"
            send_name = filename_hint
        else:
            mime = "application/octet-stream"
            send_name = filename_hint

        try:
            # Step 1 — open an upload handle.
            r = self.session.post(
                f"{NOTION_API}/file_uploads",
                headers=self._headers(),
                json={},
                timeout=self.timeout,
            )
            r.raise_for_status()
            handle = r.json()
            upload_url = handle["upload_url"]
            file_upload_id = handle["id"]
        except Exception as e:
            _LOGGER.warning("file_uploads create failed for %s: %s", filename_hint, e)
            return None

        try:
            # Step 2 — POST the actual bytes (multipart).
            r = self.session.post(
                upload_url,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Notion-Version": NOTION_VERSION,
                },
                files={"file": (send_name, io.BytesIO(data), mime)},
                timeout=self.timeout * 3,
            )
            r.raise_for_status()
        except Exception as e:
            _LOGGER.warning("file upload PUT failed for %s: %s", filename_hint, e)
            return None

        return file_upload_id

    # ----------------------------------------------------------- video / file upload

    @staticmethod
    def _guess_mime(filename: str) -> str:
        """Map a filename suffix to an HTTP-friendly MIME type."""
        ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
        return {
            "mp4": "video/mp4",
            "mov": "video/quicktime",
            "m4v": "video/mp4",
            "webm": "video/webm",
            "avi": "video/x-msvideo",
            "mkv": "video/x-matroska",
            "pdf": "application/pdf",
            "doc": "application/msword",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "xls": "application/vnd.ms-excel",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "ppt": "application/vnd.ms-powerpoint",
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "txt": "text/plain",
            "zip": "application/zip",
        }.get(ext, "application/octet-stream")

    def _upload_one_blob(
        self,
        raw: bytes,
        filename: str,
        *,
        kind: str,  # "video" or "file" — for logging only
    ) -> str | None:
        """Upload a non-image attachment as-is (no compression).

        Notion's per-file cap (5 MiB on free tier) is enforced strictly here:
        anything over the cap is skipped with a warning. Returns file_upload_id
        or None on skip/error. Used for videos and generic files (PDF/XLSX/...).
        """
        if len(raw) > self.max_image_bytes:
            _LOGGER.warning(
                "%s %s is %d bytes > %d cap; skipping (Notion free tier limit)",
                kind, filename, len(raw), self.max_image_bytes,
            )
            return None

        mime = self._guess_mime(filename)
        try:
            r = self.session.post(
                f"{NOTION_API}/file_uploads",
                headers=self._headers(),
                json={},
                timeout=self.timeout,
            )
            r.raise_for_status()
            handle = r.json()
            upload_url = handle["upload_url"]
            file_upload_id = handle["id"]
        except Exception as e:
            _LOGGER.warning("file_uploads create failed for %s %s: %s", kind, filename, e)
            return None

        try:
            r = self.session.post(
                upload_url,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Notion-Version": NOTION_VERSION,
                },
                files={"file": (filename, io.BytesIO(raw), mime)},
                timeout=self.timeout * 3,
            )
            r.raise_for_status()
        except Exception as e:
            _LOGGER.warning("%s upload PUT failed for %s: %s", kind, filename, e)
            return None

        return file_upload_id

    # ----------------------------------------------------------- page build

    @staticmethod
    def _chunk(text: str, size: int = MAX_BLOCK_TEXT) -> list[str]:
        return [text[i : i + size] for i in range(0, len(text), size)] or [""]

    @staticmethod
    def _para(text: str, *, color: str | None = None) -> dict[str, Any]:
        rt = {"type": "text", "text": {"content": text}}
        if color:
            rt["annotations"] = {"color": color}
        return {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [rt]},
        }

    def _build_children(
        self,
        report: dict[str, Any],
        image_upload_ids: list[str],
        video_upload_ids: list[str],
        file_upload_ids: list[tuple[str, str]],  # list of (id, filename)
    ) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []

        # Metadata header (gray, single line)
        meta_bits: list[str] = []
        if report.get("author_name"):
            meta_bits.append(f"선생님 {report['author_name']}")
        if report.get("class_name"):
            meta_bits.append(f"{report['class_name']}")
        if report.get("weather"):
            w = report['weather']
            meta_bits.append(f"날씨 {WEATHER_KO.get(w, w)}")
        if report.get("date_written"):
            meta_bits.append(f"작성 {report['date_written']}")
        if meta_bits:
            blocks.append(self._para(" · ".join(meta_bits), color="gray"))

        # Body content
        body = (report.get("content") or "").strip()
        if body:
            for chunk in self._chunk(body):
                blocks.append(self._para(chunk))

        # Photos (one image block per uploaded file)
        if image_upload_ids:
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": [{"type": "text", "text": {"content": "사진"}}]},
            })
            for fid in image_upload_ids:
                blocks.append({
                    "object": "block",
                    "type": "image",
                    "image": {
                        "type": "file_upload",
                        "file_upload": {"id": fid},
                    },
                })

        # Videos (only those that fit Notion's per-file cap)
        if video_upload_ids:
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": [{"type": "text", "text": {"content": "동영상"}}]},
            })
            for fid in video_upload_ids:
                blocks.append({
                    "object": "block",
                    "type": "video",
                    "video": {
                        "type": "file_upload",
                        "file_upload": {"id": fid},
                    },
                })

        # Generic file attachments (PDF, Excel, etc.)
        if file_upload_ids:
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": [{"type": "text", "text": {"content": "첨부 파일"}}]},
            })
            for fid, fname in file_upload_ids:
                blocks.append({
                    "object": "block",
                    "type": "file",
                    "file": {
                        "type": "file_upload",
                        "file_upload": {"id": fid},
                        "name": fname[:100],
                    },
                })

        return blocks

    # ----------------------------------------------------------- publish

    @staticmethod
    def _summarize_text(text: str, max_chars: int = 70) -> str:
        """Pick a readable one-line summary out of an alimnota body.

        Strategy:
        1. Strip / flatten whitespace.
        2. Try to cut at the first sentence terminator (Korean and Latin).
        3. Otherwise hard-truncate at ``max_chars`` and append an ellipsis.
        """
        if not text:
            return ""
        flat = " ".join(text.split())  # collapse all whitespace
        for delim in ("다. ", "요. ", "다.", "요.", ".", "!", "?", "♡"):
            idx = flat.find(delim)
            if 10 < idx < max_chars:
                return flat[: idx + len(delim)].rstrip()
        if len(flat) > max_chars:
            return flat[:max_chars].rstrip() + "..."
        return flat

    @staticmethod
    def _life_record_bits(report: dict[str, Any]) -> list[str]:
        """Convert the detail-API life-record codes into human Korean chips.

        Only non-empty / informative fields produce a chip. Mapping for
        `*_status` enum codes is best-effort (STATUS_KO); unknown values
        fall through as the original code so they don't disappear silently.
        """
        bits: list[str] = []

        def to_ko(value: str | None) -> str | None:
            if not value:
                return None
            return STATUS_KO.get(value, value)

        meal = to_ko(report.get("meal_status"))
        if meal:
            bits.append(f"🍽️ 식사 {meal}")

        sh = report.get("sleep_hour")
        if sh:
            bits.append(f"💤 수면 {SLEEP_HOUR_KO.get(sh, sh)}")

        bowel = to_ko(report.get("bowel_status"))
        if bowel:
            bits.append(f"💩 배변 {bowel}")

        temp_status = to_ko(report.get("temperature_status"))
        if temp_status:
            bits.append(f"🌡️ 체온 {temp_status}")
        # Numeric temperature if present (some kidsnote setups record actual °C)
        temp = report.get("temperature")
        if temp not in (None, "", 0):
            bits.append(f"🌡️ {temp}°C")

        mood = to_ko(report.get("mood_status"))
        if mood:
            bits.append(f"😊 기분 {mood}")

        health = to_ko(report.get("health_status"))
        if health:
            bits.append(f"💊 건강 {health}")

        outdoor = to_ko(report.get("outdoor_activity_status"))
        if outdoor:
            bits.append(f"🏃 야외활동 {outdoor}")

        bath = to_ko(report.get("bath_status"))
        if bath:
            bits.append(f"🛁 목욕 {bath}")

        nail = to_ko(report.get("nail_status"))
        if nail:
            bits.append(f"💅 손톱 {nail}")

        ar = report.get("activity_rate")
        if ar not in (None, "", 0):
            bits.append(f"⭐ 활동 {ar}")

        return bits

    @staticmethod
    def _life_record_detail_blocks(
        report: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Tabular entries for food/sleep/nursing arrays — one paragraph per row.

        Only includes sections that have at least one entry. Each row is a
        single colored paragraph so the page reads like a timeline.
        """
        out: list[dict[str, Any]] = []

        food = report.get("food") or []
        if isinstance(food, list) and food:
            out.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": [{"type": "text", "text": {"content": "🍽️ 식사 기록"}}]},
            })
            for f in food:
                if not isinstance(f, dict):
                    continue
                t = f.get("time_meal") or ""
                name = f.get("name") or ""
                line = f"{t}  {name}".strip()
                if line:
                    out.append({
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"type": "text", "text": {"content": line}}]},
                    })

        sleep = report.get("sleep") or []
        if isinstance(sleep, list) and sleep:
            out.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": [{"type": "text", "text": {"content": "💤 낮잠"}}]},
            })
            for s in sleep:
                if not isinstance(s, dict):
                    continue
                start = s.get("time_start") or ""
                end = s.get("time_end") or ""
                line = f"{start} ~ {end}".strip(" ~")
                if line:
                    out.append({
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"type": "text", "text": {"content": line}}]},
                    })

        nursing = report.get("nursing") or []
        if isinstance(nursing, list) and nursing:
            out.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": [{"type": "text", "text": {"content": "🍼 수유"}}]},
            })
            for n in nursing:
                if not isinstance(n, dict):
                    continue
                t = n.get("time_nursing") or ""
                vol = n.get("volume")
                line = f"{t}  {vol}ml" if vol else t
                if line:
                    out.append({
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"type": "text", "text": {"content": line}}]},
                    })

        bowel = report.get("bowel") or []
        if isinstance(bowel, list) and bowel:
            out.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": [{"type": "text", "text": {"content": "💩 배변 기록"}}]},
            })
            for b in bowel:
                if not isinstance(b, dict):
                    continue
                line = json.dumps(b, ensure_ascii=False)
                out.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text", "text": {"content": line}}]},
                })

        return out

    def _menu_summary_blocks(self, menu: dict[str, Any]) -> list[dict[str, Any]]:
        """Compact text-only summary of a daily menu for inline embedding
        inside a report page. Photos of food are intentionally omitted to
        keep report pages from doubling in size — full menu (with photos)
        is still available as a separate menu page if the matching date
        has no report.
        """
        out: list[dict[str, Any]] = [{
            "object": "block",
            "type": "heading_3",
            "heading_3": {"rich_text": [{"type": "text", "text": {"content": "🍱 오늘의 식단"}}]},
        }]
        for text_field, _img_field, label in self.MEAL_FIELDS:
            text = (menu.get(text_field) or "").strip()
            if not text:
                continue
            one_line = " · ".join(p for p in text.split("\n") if p.strip())
            out.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [
                    {"type": "text", "text": {"content": f"{label}: "}, "annotations": {"bold": True}},
                    {"type": "text", "text": {"content": one_line}},
                ]},
            })
        return out

    def publish_report(
        self,
        report: dict[str, Any],
        kidsnote_sess: requests.Session,
        *,
        attached_menu: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a Notion page for one Kidsnote report.

        Returns a dict with `{page_id, title, images_uploaded, images_failed}`.
        Caller is responsible for skipping reports whose id is already in the DB
        (see `existing_report_ids`).

        ``attached_menu``: optional matching daily menu (same date as report).
        When provided, a compact text-only menu summary is appended inside
        the report body so a single page captures both the teacher's notes
        and what the child ate / what was on the daily menu.
        """
        report_id = int(report["id"])
        date_str = (
            report.get("date_written")
            or (report.get("modified") or "")[:10]
            or (report.get("created") or "")[:10]
            or datetime.now().date().isoformat()
        )
        summary = self._summarize_text(report.get("content") or "")
        title = f"[{date_str}] {summary}" if summary else f"[{date_str}] 알림장 #{report_id}"

        # Upload photos first so we can drop image blocks into the page body.
        image_upload_ids: list[str] = []
        images_failed = 0
        for img in report.get("attached_images") or []:
            if not isinstance(img, dict):
                continue
            url = (
                img.get("original")
                or img.get("high_resize")
                or img.get("large")
                or img.get("url")
            )
            if not url:
                images_failed += 1
                continue
            try:
                resp = kidsnote_sess.get(url, timeout=120)
                resp.raise_for_status()
                raw_bytes = resp.content
            except Exception as e:
                _LOGGER.warning("photo download failed (%s): %s", url, e)
                images_failed += 1
                continue
            hint = img.get("original_file_name") or f"image_{img.get('id', 'x')}.jpg"
            fid = self._upload_one_image(raw_bytes, hint)
            if fid:
                image_upload_ids.append(fid)
            else:
                images_failed += 1

        # Videos: kidsnote stores it as a single object (or None / list of 1).
        # Notion's per-file cap (5 MiB free) applies; over-cap videos are skipped.
        video_upload_ids: list[str] = []
        videos_failed = 0
        video_objs: list[dict[str, Any]] = []
        for k in ("attached_video", "video", "attached_videos"):
            v = report.get(k)
            if isinstance(v, dict):
                video_objs.append(v)
                break
            if isinstance(v, list) and v:
                video_objs.extend(x for x in v if isinstance(x, dict))
                break
        for vobj in video_objs:
            url = (
                vobj.get("original")
                or vobj.get("high")
                or vobj.get("url")
            )
            if not url:
                videos_failed += 1
                continue
            try:
                resp = kidsnote_sess.get(url, timeout=180)
                resp.raise_for_status()
                raw_bytes = resp.content
            except Exception as e:
                _LOGGER.warning("video download failed (%s): %s", url, e)
                videos_failed += 1
                continue
            hint = vobj.get("original_file_name") or f"video_{vobj.get('id', 'x')}.mp4"
            fid = self._upload_one_blob(raw_bytes, hint, kind="video")
            if fid:
                video_upload_ids.append(fid)
            else:
                videos_failed += 1

        # Other file attachments (PDF, Excel, etc.) — same 5 MiB cap.
        file_upload_ids: list[tuple[str, str]] = []
        files_failed = 0
        for fobj in report.get("attached_files") or []:
            if not isinstance(fobj, dict):
                continue
            url = fobj.get("original") or fobj.get("url")
            if not url:
                files_failed += 1
                continue
            try:
                resp = kidsnote_sess.get(url, timeout=180)
                resp.raise_for_status()
                raw_bytes = resp.content
            except Exception as e:
                _LOGGER.warning("file download failed (%s): %s", url, e)
                files_failed += 1
                continue
            hint = fobj.get("original_file_name") or f"file_{fobj.get('id', 'x')}.bin"
            fid = self._upload_one_blob(raw_bytes, hint, kind="file")
            if fid:
                file_upload_ids.append((fid, hint))
            else:
                files_failed += 1

        children = self._build_children(
            report, image_upload_ids, video_upload_ids, file_upload_ids,
        )

        # Append life-record chips to the meta paragraph (first gray paragraph).
        life_bits = self._life_record_bits(report)
        if life_bits and children and children[0].get("type") == "paragraph":
            rt = children[0]["paragraph"]["rich_text"]
            base = rt[0]["text"]["content"] if rt else ""
            merged = (base + " · " if base else "") + " · ".join(life_bits)
            children[0]["paragraph"]["rich_text"] = [{
                "type": "text",
                "text": {"content": merged},
                "annotations": {"color": "gray"},
            }]

        # Insert life-record detail blocks (food/sleep/nursing timelines) +
        # daily menu summary (if provided) before the attachment sections.
        # Attachment sections start at the first heading_3 named '사진'/'동영상'/'첨부 파일'.
        extras: list[dict[str, Any]] = []
        extras.extend(self._life_record_detail_blocks(report))
        if attached_menu:
            extras.extend(self._menu_summary_blocks(attached_menu))
        if extras:
            insert_idx = len(children)
            attachment_headings = {"사진", "동영상", "첨부 파일"}
            for i, blk in enumerate(children):
                if blk.get("type") == "heading_3":
                    rt = blk["heading_3"]["rich_text"]
                    if rt and rt[0].get("text", {}).get("content") in attachment_headings:
                        insert_idx = i
                        break
            children = children[:insert_idx] + extras + children[insert_idx:]

        # Resolve property names on first publish (cached for subsequent calls).
        self._resolve_schema()
        assert self._prop_title is not None and self._prop_report_id is not None

        properties: dict[str, Any] = {
            self._prop_title: {"title": [{"text": {"content": title[:200]}}]},
            self._prop_report_id: {"number": report_id},
        }
        if date_str and self._prop_date:
            try:
                d = datetime.fromisoformat(date_str[:10]).date().isoformat()
                properties[self._prop_date] = {"date": {"start": d}}
            except (ValueError, TypeError):
                pass

        payload = {
            "parent": {"database_id": self.database_id},
            "properties": properties,
            "children": children,
        }
        r = self.session.post(
            f"{NOTION_API}/pages",
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        r.raise_for_status()
        page = r.json()
        return {
            "page_id": page.get("id", ""),
            "page_url": page.get("url", ""),
            "report_id": report_id,
            "title": title,
            "images_uploaded": len(image_upload_ids),
            "images_failed": images_failed,
            "videos_uploaded": len(video_upload_ids),
            "videos_failed": videos_failed,
            "files_uploaded": len(file_upload_ids),
            "files_failed": files_failed,
        }

    # ----------------------------------------------------------- notice / album publish

    def _publish_simple_item(
        self,
        item: dict[str, Any],
        kidsnote_sess: requests.Session,
        *,
        title: str,
        item_id: int,
        date_str: str,
        meta_bits: list[str],
    ) -> dict[str, Any]:
        """Generic publisher for items with the same shape as reports
        (notices, albums): title/content/author/attached_images/video/files.

        Uses the same upload + block-building logic as ``publish_report``.
        """
        # ---- Upload images ----
        image_upload_ids: list[str] = []
        images_failed = 0
        for img in item.get("attached_images") or []:
            if not isinstance(img, dict):
                continue
            url = (
                img.get("original")
                or img.get("high_resize")
                or img.get("large")
                or img.get("url")
            )
            if not url:
                images_failed += 1
                continue
            try:
                resp = kidsnote_sess.get(url, timeout=120)
                resp.raise_for_status()
                raw_bytes = resp.content
            except Exception as e:
                _LOGGER.warning("photo download failed (%s): %s", url, e)
                images_failed += 1
                continue
            hint = img.get("original_file_name") or f"image_{img.get('id', 'x')}.jpg"
            fid = self._upload_one_image(raw_bytes, hint)
            if fid:
                image_upload_ids.append(fid)
            else:
                images_failed += 1

        # ---- Upload videos ----
        video_upload_ids: list[str] = []
        videos_failed = 0
        video_objs: list[dict[str, Any]] = []
        for k in ("attached_video", "video", "attached_videos"):
            v = item.get(k)
            if isinstance(v, dict):
                video_objs.append(v)
                break
            if isinstance(v, list) and v:
                video_objs.extend(x for x in v if isinstance(x, dict))
                break
        for vobj in video_objs:
            url = vobj.get("original") or vobj.get("high") or vobj.get("url")
            if not url:
                videos_failed += 1
                continue
            try:
                resp = kidsnote_sess.get(url, timeout=180)
                resp.raise_for_status()
                raw_bytes = resp.content
            except Exception as e:
                _LOGGER.warning("video download failed (%s): %s", url, e)
                videos_failed += 1
                continue
            hint = vobj.get("original_file_name") or f"video_{vobj.get('id', 'x')}.mp4"
            fid = self._upload_one_blob(raw_bytes, hint, kind="video")
            if fid:
                video_upload_ids.append(fid)
            else:
                videos_failed += 1

        # ---- Upload generic files ----
        file_upload_ids: list[tuple[str, str]] = []
        files_failed = 0
        for fobj in item.get("attached_files") or []:
            if not isinstance(fobj, dict):
                continue
            url = fobj.get("original") or fobj.get("url")
            if not url:
                files_failed += 1
                continue
            try:
                resp = kidsnote_sess.get(url, timeout=180)
                resp.raise_for_status()
                raw_bytes = resp.content
            except Exception as e:
                _LOGGER.warning("file download failed (%s): %s", url, e)
                files_failed += 1
                continue
            hint = fobj.get("original_file_name") or f"file_{fobj.get('id', 'x')}.bin"
            fid = self._upload_one_blob(raw_bytes, hint, kind="file")
            if fid:
                file_upload_ids.append((fid, hint))
            else:
                files_failed += 1

        # ---- Build body blocks ----
        blocks: list[dict[str, Any]] = []
        if meta_bits:
            blocks.append(self._para(" · ".join(meta_bits), color="gray"))
        body_text = (item.get("content") or "").strip()
        if body_text:
            for chunk in self._chunk(body_text):
                blocks.append(self._para(chunk))

        if image_upload_ids:
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": [{"type": "text", "text": {"content": "사진"}}]},
            })
            for fid in image_upload_ids:
                blocks.append({
                    "object": "block",
                    "type": "image",
                    "image": {"type": "file_upload", "file_upload": {"id": fid}},
                })
        if video_upload_ids:
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": [{"type": "text", "text": {"content": "동영상"}}]},
            })
            for fid in video_upload_ids:
                blocks.append({
                    "object": "block",
                    "type": "video",
                    "video": {"type": "file_upload", "file_upload": {"id": fid}},
                })
        if file_upload_ids:
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": [{"type": "text", "text": {"content": "첨부 파일"}}]},
            })
            for fid, fname in file_upload_ids:
                blocks.append({
                    "object": "block",
                    "type": "file",
                    "file": {
                        "type": "file_upload",
                        "file_upload": {"id": fid},
                        "name": fname[:100],
                    },
                })

        # ---- Create page ----
        self._resolve_schema()
        assert self._prop_title is not None and self._prop_report_id is not None
        properties: dict[str, Any] = {
            self._prop_title: {"title": [{"text": {"content": title[:200]}}]},
            self._prop_report_id: {"number": item_id},
        }
        if date_str and self._prop_date:
            try:
                d = datetime.fromisoformat(date_str[:10]).date().isoformat()
                properties[self._prop_date] = {"date": {"start": d}}
            except (ValueError, TypeError):
                pass
        payload = {
            "parent": {"database_id": self.database_id},
            "properties": properties,
            "children": blocks,
        }
        r = self.session.post(
            f"{NOTION_API}/pages",
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        r.raise_for_status()
        page = r.json()
        return {
            "page_id": page.get("id", ""),
            "title": title,
            "item_id": item_id,
            "images_uploaded": len(image_upload_ids),
            "images_failed": images_failed,
            "videos_uploaded": len(video_upload_ids),
            "videos_failed": videos_failed,
            "files_uploaded": len(file_upload_ids),
            "files_failed": files_failed,
        }

    def publish_notice(
        self,
        notice: dict[str, Any],
        kidsnote_sess: requests.Session,
    ) -> dict[str, Any]:
        """Create a Notion page for one notice (`/centers/.../notices/`)."""
        notice_id = int(notice["id"])
        date_str = (
            (notice.get("created") or "")[:10]
            or (notice.get("modified") or "")[:10]
            or datetime.now().date().isoformat()
        )
        nt = (notice.get("title") or "").strip()
        title = f"[{date_str}] 공지: {nt}" if nt else f"[{date_str}] 공지 #{notice_id}"
        meta_bits: list[str] = []
        if notice.get("author_name"):
            meta_bits.append(f"작성 {notice['author_name']}")
        if notice.get("is_center_notice"):
            meta_bits.append("센터 공지")
        if notice.get("is_always_on_top"):
            meta_bits.append("📌 상단고정")
        if notice.get("num_comments"):
            meta_bits.append(f"댓글 {notice['num_comments']}")
        return self._publish_simple_item(
            notice, kidsnote_sess,
            title=title, item_id=notice_id, date_str=date_str,
            meta_bits=meta_bits,
        )

    def publish_album(
        self,
        album: dict[str, Any],
        kidsnote_sess: requests.Session,
    ) -> dict[str, Any]:
        """Create a Notion page for one album (`/children/.../albums/`)."""
        album_id = int(album["id"])
        date_str = (
            (album.get("created") or "")[:10]
            or (album.get("modified") or "")[:10]
            or datetime.now().date().isoformat()
        )
        at = (album.get("title") or "").strip()
        title = f"[{date_str}] 앨범: {at}" if at else f"[{date_str}] 앨범 #{album_id}"
        meta_bits: list[str] = []
        if album.get("author_name"):
            meta_bits.append(f"작성 {album['author_name']}")
        if album.get("num_comments"):
            meta_bits.append(f"댓글 {album['num_comments']}")
        return self._publish_simple_item(
            album, kidsnote_sess,
            title=title, item_id=album_id, date_str=date_str,
            meta_bits=meta_bits,
        )

    # ----------------------------------------------------------- daily menu publish

    # Per-meal labels for menu page body. Order matters (matches kidsnote app).
    MEAL_FIELDS: list[tuple[str, str, str]] = [
        ("morning", "morning_img", "🌅 아침"),
        ("morning_snack", "morning_snack_img", "🍪 오전 간식"),
        ("lunch", "lunch_img", "🍱 점심"),
        ("afternoon_snack", "afternoon_snack_img", "🍰 오후 간식"),
        ("dinner", "dinner_img", "🍚 저녁"),
    ]

    def publish_menu(
        self,
        menu: dict[str, Any],
        kidsnote_sess: requests.Session,
    ) -> dict[str, Any]:
        """Create a Notion page for one daily lunch menu.

        Page title: ``[YYYY-MM-DD] 식단표``
        Body: per-meal heading → text (each line of the meal) → photo (if any).

        Returns ``{page_id, title, menu_id, images_uploaded, images_failed}``.
        """
        menu_id = int(menu["id"])
        date_str = menu.get("date_menu") or (menu.get("modified") or "")[:10]
        # Title: include lunch summary if present (most informative meal).
        lunch_text = (menu.get("lunch") or "").strip()
        lunch_summary = ""
        if lunch_text:
            # Take first 2-3 menu items joined with comma.
            items = [s.strip() for s in lunch_text.split("\n") if s.strip()]
            lunch_summary = ", ".join(items[:3])
            if len(items) > 3:
                lunch_summary += " 외"
        title = f"[{date_str}] 🍱 {lunch_summary}" if lunch_summary else f"[{date_str}] 식단표"

        # Build body + upload meal photos (each meal has at most 1 image).
        blocks: list[dict[str, Any]] = []
        images_uploaded = 0
        images_failed = 0

        meta_bits: list[str] = []
        if menu.get("author_name"):
            meta_bits.append(f"작성 {menu['author_name']}")
        if menu.get("date_menu"):
            meta_bits.append(f"날짜 {menu['date_menu']}")
        if meta_bits:
            blocks.append(self._para(" · ".join(meta_bits), color="gray"))

        for text_field, img_field, label in self.MEAL_FIELDS:
            meal_text = (menu.get(text_field) or "").strip()
            meal_img = menu.get(img_field)
            if not meal_text and not isinstance(meal_img, dict):
                continue  # skip empty meal slot

            # Heading per meal
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": [{"type": "text", "text": {"content": label}}]},
            })

            # Each newline in the menu text → separate paragraph
            for line in meal_text.split("\n"):
                line = line.strip()
                if line:
                    blocks.append(self._para(line))

            # Photo (if present)
            if isinstance(meal_img, dict):
                url = meal_img.get("original") or meal_img.get("large") or meal_img.get("url")
                if url:
                    try:
                        resp = kidsnote_sess.get(url, timeout=120)
                        resp.raise_for_status()
                        raw = resp.content
                        hint = meal_img.get("original_file_name") or f"menu_{menu_id}_{text_field}.jpg"
                        fid = self._upload_one_image(raw, hint)
                        if fid:
                            blocks.append({
                                "object": "block",
                                "type": "image",
                                "image": {
                                    "type": "file_upload",
                                    "file_upload": {"id": fid},
                                },
                            })
                            images_uploaded += 1
                        else:
                            images_failed += 1
                    except Exception as e:
                        _LOGGER.warning("menu photo download failed (%s): %s", url, e)
                        images_failed += 1

        # Resolve property names + assemble payload.
        self._resolve_schema()
        assert self._prop_title is not None and self._prop_report_id is not None
        properties: dict[str, Any] = {
            self._prop_title: {"title": [{"text": {"content": title[:200]}}]},
            self._prop_report_id: {"number": menu_id},
        }
        if date_str and self._prop_date:
            try:
                d = datetime.fromisoformat(date_str[:10]).date().isoformat()
                properties[self._prop_date] = {"date": {"start": d}}
            except (ValueError, TypeError):
                pass

        payload = {
            "parent": {"database_id": self.database_id},
            "properties": properties,
            "children": blocks,
        }
        r = self.session.post(
            f"{NOTION_API}/pages",
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        r.raise_for_status()
        page = r.json()
        return {
            "page_id": page.get("id", ""),
            "page_url": page.get("url", ""),
            "menu_id": menu_id,
            "title": title,
            "images_uploaded": images_uploaded,
            "images_failed": images_failed,
        }


__all__ = ["NotionMirror", "DEFAULT_MAX_IMAGE_BYTES"]
