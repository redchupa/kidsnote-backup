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
import json
import logging
import re
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
    "no_sleep": "안 잤음",
    "none": "안 잠",
    "below_1": "1시간 미만",
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
    "more": "많이 먹음",
    "less": "적게 먹음",
    "sick": "아픔",
    "fine": "양호",
    "trimmed": "정리됨",
    "needs_trim": "정리 필요",
    "active": "활발",
    "calm": "차분",
}
WEATHER_KO = {
    # Codes the live kidsnote API actually uses (sampled from 391 reports):
    "sunny": "☀️ 맑음",
    "partly_cloudy": "⛅ 구름 조금",
    "mostly_cloudy": "🌥️ 구름 많음",
    "overcast": "☁️ 흐림",
    "fog": "🌫️ 안개",
    "rain": "🌧️ 비",
    "sunny_after_rain": "🌈 비온 뒤 맑음",
    "snow": "❄️ 눈",
    "yellow_sand": "🟡 황사",
    "thunderstorm": "⛈️ 천둥번개",
    "mixed_rain_snow": "🌨️ 진눈깨비",
    # Fallbacks for variants that may show up at other daycares:
    "cloudy": "☁️ 흐림",
    "rainy": "🌧️ 비",
    "snowy": "❄️ 눈",
    "foggy": "🌫️ 안개",
    "windy": "💨 바람",
    "stormy": "⛈️ 폭풍",
    "hot": "🥵 더움",
    "cold": "🥶 추움",
}

# Activity categories used to label alimnota titles.
# Order matters — earlier entries get matched first when multiple categories
# fit. Each tuple is the list of body keywords that activates the label.
ACTIVITY_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("🎨 미술",     ("색연필", "그림", "점토", "물감", "크레파스", "만들기",
                     "찰흙", "색종이", "도화지", "스티커", "꾸미기", "오리기",
                     "붙이기", "색칠", "그리기")),
    ("🎵 음악",     ("노래", "동요", "악기", "율동", "리듬", "탬버린",
                     "트라이앵글", "마라카스", "춤추")),
    ("📚 책읽기",   ("책", "동화", "독서", "그림책", "이야기책")),
    ("🚶 산책",     ("산책", "공원", "나들이", "외출", "바깥놀이", "야외놀이")),
    ("🌳 자연",     ("나뭇잎", "나무", "꽃잎", "벌레", "곤충", "햇살",
                     "흙", "모래", "동물원", "관찰")),
    ("🌸 꽃",       ("꽃", "꽃밭")),
    ("🍱 식사",     ("도시락", "점심", "급식", "반찬", "냠냠", "맛있게",
                     "식사", "식단")),
    ("🍪 간식",     ("간식", "과자", "우유", "빵", "과일", "치즈")),
    ("💤 낮잠",     ("낮잠", "수면", "잠을 잤", "꿈나라")),
    ("🧩 블록",     ("블록", "퍼즐", "쌓기", "레고", "구성놀이")),
    ("🚗 역할놀이", ("역할놀이", "소꿉", "병원놀이", "마트놀이",
                     "엄마놀이", "아빠놀이", "선생님놀이")),
    ("💧 물놀이",   ("물놀이", "수영", "분수")),
    ("🏃 신체활동", ("체조", "운동", "달리기", "뛰기", "체육", "신체놀이",
                     "공놀이", "킥보드", "자전거")),
    ("📅 행사",     ("생일", "졸업", "입학", "운동회", "발표회", "재롱",
                     "공연", "현장학습", "소풍", "참여수업", "공개수업")),
    ("🎉 기념일",   ("어버이날", "어린이날", "스승의날", "어버이의날",
                     "어머니의날", "아버지의날", "추석", "설날",
                     "성탄절", "크리스마스", "핼러윈", "할로윈",
                     "부활절", "한글날", "광복절", "삼일절")),
    ("❤️ 감정/표현", ("사랑한다", "안아주", "포옹", "뽀뽀", "사랑해",
                     "고맙다", "감사", "꼭 안", "토닥")),
    ("🎓 학습",     ("한글", "숫자", "영어", "수업", "글자",
                     "배우는", "익히는")),
    ("🧒 친구관계", ("사이좋게", "양보", "도와주", "친구랑", "또래",
                     "함께 놀")),
    ("💉 건강",     ("병원", "체온", "감기", "약을", "안전교육", "소방",
                     "지진훈련")),
    ("🏠 가정활동", ("할머니", "할아버지", "외할머니", "외할아버지",
                     "친정", "본가", "집에서")),
)

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

        # Metadata header (gray, single line) — author role depends on author.type
        meta_bits: list[str] = []
        atype = (report.get("author") or {}).get("type") or ""
        aname = report.get("author_name") or (report.get("author") or {}).get("name") or ""
        if aname:
            role_label = {
                "teacher": "👩‍🏫 선생님",
                "parent": "👨‍👩‍👧 부모",
                "admin": "🏫 원감",
            }.get(atype, "✏️ 작성자")
            meta_bits.append(f"{role_label} {aname}")
        if report.get("class_name"):
            meta_bits.append(f"{report['class_name']}")
        if report.get("date_written"):
            meta_bits.append(f"작성 {report['date_written']}")
        if meta_bits:
            blocks.append(self._para(" · ".join(meta_bits), color="gray"))

        # Weather callout — only when the daycare actually filled in the
        # weather field. No body-text inference (per design: ``있는 그대로``).
        w_code = report.get("weather")
        if w_code:
            w_display = WEATHER_KO.get(w_code, w_code)
            blocks.append({
                "object": "block",
                "type": "callout",
                "callout": {
                    "rich_text": [{
                        "type": "text",
                        "text": {"content": f"오늘의 날씨: {w_display}"},
                    }],
                    "icon": {"type": "emoji", "emoji": "🌤️"},
                    "color": "blue_background",
                },
            })

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

    # Stopwords for the keyword-based title extractor. Anything filler /
    # generic / verbal-ending gets dropped so the leftover keywords are
    # the day's actual activity nouns (점토, 색연필, 도시락 etc).
    _KEYWORD_STOPWORDS = frozenset({
        # Greetings + address terms
        "안녕하세요", "어머님", "어머니", "아버님", "아버지", "부모님", "부모",
        # Calendar
        "오늘", "어제", "내일", "하루", "이번", "다음", "주말", "평일", "낮", "밤",
        # Generic people
        "선생님", "친구", "친구들", "아이", "아기", "동생", "형", "누나", "엄마", "아빠",
        # Filler / pronouns / adverbs
        "우리", "너무", "정말", "그래서", "그리고", "함께", "같이", "다같이",
        "이렇게", "저렇게", "그렇게", "이런", "저런", "그런", "약간", "많이", "조금",
        "처음", "다시", "또한", "역시", "참고",
        # Common verb stems left after particle strip
        "있어", "없어", "되어", "되었", "했어", "했었", "했답", "있었",
        "있는", "없는", "되는", "하는", "보고", "보며", "보이", "보았",
    })

    # Korean josa (particles) we strip from the tail of each word before
    # frequency counting. Two-char particles are tried first.
    _PARTICLE_2 = ("으로", "에서", "에게", "한테", "처럼", "보다", "마다",
                   "까지", "부터", "이라", "라고", "이고", "이며", "이지",
                   "에는", "에도", "에만", "은데", "는데")
    _PARTICLE_1 = ("을", "를", "이", "가", "은", "는", "도", "만",
                   "의", "에", "와", "과", "로", "랑", "야", "여", "께")

    @classmethod
    def _strip_particle(cls, word: str) -> str:
        # 2-char particles: word must keep at least 1 char after strip.
        for p in cls._PARTICLE_2:
            if word.endswith(p) and len(word) > len(p):
                return word[: -len(p)]
        # 1-char particles: word must keep at least 1 char after strip
        # (so ``꽃도`` → ``꽃``).
        for p in cls._PARTICLE_1:
            if word.endswith(p) and len(word) > 1:
                return word[:-1]
        return word

    # Verb / adjective tails we filter out (these come AFTER particle-strip
    # so the remaining base form still has the verb/adj inflection).
    _VERB_ADJ_TAILS = (
        # Connective endings
        "고", "서", "며", "면", "도록", "면서", "지만", "아도", "어도", "려고",
        "더니", "더라", "다가", "으니", "으면", "라서", "라며", "는데",
        "자마자", "더라도", "을수록", "을지", "은채", "은채로", "다면",
        # Past-tense stems
        "았", "었", "였", "겠", "했", "봤", "갔", "왔", "됐", "었던", "았던",
        # Final endings beyond what the particle stripper handled
        "어요", "아요", "에요", "예요", "습니다", "답니다", "지요", "네요",
        "대요", "아서", "어서", "으며", "으면", "하며", "려고", "려서",
        # Adverb-forming endings ("빠르게/신나게/조용하게")
        "게",
        # Common adj-as-modifier endings ("즐거운/예쁜/사랑스러운")
        "스러운", "다운", "러운",
        # 1-char verb/adj inflection endings — keep only the ones that
        # never legitimately end a Korean noun in alimnota text. ``진/킨/긴/된``
        # were dropped because they would block real nouns like ``사진``.
        # Specific passive forms (펼쳐진/늘어진/이루어진) are added as
        # multi-char stopwords below instead.
        "여", "워", "는", "은", "운",
    )

    # Adjective/verb stems we still want to drop when they slip through
    # the verbal-ending filter (e.g. ``예쁜`` is only 2 chars). This list
    # grows over time as user feedback identifies more noise.
    _EXTRA_STOPWORDS = frozenset({
        "즐거운", "예쁜", "신나는", "신나게", "사랑", "사랑스러운", "기특", "행복", "활발",
        "가득", "가득한", "표정", "기어", "기특한", "다정한", "조용한", "씩씩한",
        "보더니", "보고", "보며", "보았", "가서", "가고", "왔어", "갔어",
        "주는", "주었", "주신", "받았", "되었", "있어", "없어", "해서", "하며",
        "되어", "하고", "되는", "되어서", "있는", "없는", "있어요",
        "오늘은", "이렇게", "저렇게", "그렇게",
        "중에", "사이", "동안", "그동안", "이번엔", "다음엔",
        "정말로", "참으로", "마찬가지", "마치", "마침",
        # Passive/past participles that look like nouns but aren't:
        "펼쳐진", "늘어진", "이루어진", "쥐어진", "기울어진",
        # Common verb stems that survive particle strip
        "했지", "되었지", "보았지", "갔지",
    })

    # 1-character keyword stopwords (filler / adverbs / determiners that
    # would otherwise survive the particle-strip stage when a 2-char
    # word like ``잘은`` → ``잘`` slips through).
    _ONECHAR_STOPWORDS = frozenset({
        "잘", "안", "또", "더", "꼭", "참", "그", "이", "저", "거", "것",
        "수", "들", "수", "곳", "데", "쪽", "분", "내", "네", "왜", "뭐",
        "다", "한", "두", "세", "넷", "막", "쭉", "푹", "쏙",
    })

    # Cache compiled patterns: each keyword turns into a Korean word-boundary
    # pattern so ``책`` does NOT match inside ``산책``.
    _CATEGORY_PATTERNS: list[tuple[str, list]] | None = None

    @classmethod
    def _ensure_category_patterns(cls) -> None:
        if cls._CATEGORY_PATTERNS is not None:
            return
        cls._CATEGORY_PATTERNS = []
        for label, keywords in ACTIVITY_CATEGORIES:
            patterns = []
            for kw in keywords:
                # Word-start boundary only: keyword must NOT be a tail of
                # another Korean word (so ``책`` doesn't fire inside ``산책``).
                # No constraint on what follows so attached particles like
                # ``을/를/도/이/가`` still let the keyword match.
                pat = re.compile(rf"(?<![가-힣]){re.escape(kw)}")
                patterns.append(pat)
            cls._CATEGORY_PATTERNS.append((label, patterns))

    @classmethod
    def _classify_categories(cls, text: str, max_n: int = 3) -> list[str]:
        """Match the body against ACTIVITY_CATEGORIES, return up to ``max_n``
        labels. Word-boundary aware so ``책`` won't match inside ``산책``.
        """
        if not text:
            return []
        cls._ensure_category_patterns()
        matched: list[str] = []
        for label, patterns in cls._CATEGORY_PATTERNS or []:
            for pat in patterns:
                if pat.search(text):
                    matched.append(label)
                    break
            if len(matched) >= max_n:
                break
        return matched

    @classmethod
    def _summarize_text(cls, text: str, max_chars: int = 80) -> str:
        """Pick a comma-joined list of meaningful Korean keywords from an
        alimnota body.

        Strategy (no LLM):
        1. Extract Korean letter runs from the body.
        2. Strip trailing particles (``을/를/이/가/에서/으로``).
        3. Drop greetings / filler / verbal endings (best-effort).
        4. Prefer keywords that appear 2+ times; fall back to singletons
           only when there aren't enough repeats.
        5. Dedup substring overlaps and clip to 5 keywords.
        """
        if not text:
            return ""
        from collections import Counter
        raw_words = re.findall(r"[가-힣]+", text)
        words: list[str] = []
        for w in raw_words:
            base = cls._strip_particle(w)
            n = len(base)
            # 1-char tokens are nearly always verb stems or particles
            # leftover; the few legit ones (꽃/물/밥) aren't worth the
            # noise, so we hard-drop them entirely.
            if not (2 <= n <= 5):
                continue
            if base in cls._KEYWORD_STOPWORDS or base in cls._EXTRA_STOPWORDS:
                continue
            if any(base.endswith(t) for t in cls._VERB_ADJ_TAILS):
                continue
            words.append(base)

        counter = Counter(words)
        # Phase 1: repeats only (frequency >= 2) — these are the most
        # signal-bearing keywords in an alimnota.
        repeats = [w for w, c in counter.most_common(20) if c >= 2]
        # Phase 2: top singletons as fallback when we don't have enough.
        singletons = [w for w, c in counter.most_common(20) if c == 1]
        ranked = repeats + singletons

        kept: list[str] = []
        for w in ranked:
            if any((w in k or k in w) for k in kept):
                continue
            kept.append(w)
            if len(kept) >= 5:
                break

        out = ", ".join(kept)
        return out[:max_chars]

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
                t = b.get("time_bowel") or ""
                status_raw = b.get("status") or ""
                status_ko = STATUS_KO.get(status_raw, status_raw)
                if t and status_ko:
                    line = f"{t}  {status_ko}"
                else:
                    line = t or status_ko
                if line:
                    out.append({
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"type": "text", "text": {"content": line}}]},
                    })

        return out

    def _menu_summary_blocks(
        self,
        menu: dict[str, Any],
        kidsnote_sess: requests.Session | None = None,
    ) -> list[dict[str, Any]]:
        """Inline daily menu (heading + text + photo per meal) for embedding
        inside a report page.

        If ``kidsnote_sess`` is provided, each meal's photo (when present) is
        downloaded and uploaded to Notion, then embedded as an image block
        right after the meal text. Without a session, only the text is shown.
        """
        out: list[dict[str, Any]] = [{
            "object": "block",
            "type": "heading_3",
            "heading_3": {"rich_text": [{"type": "text", "text": {"content": "🍱 오늘의 식단"}}]},
        }]
        for text_field, img_field, label in self.MEAL_FIELDS:
            text = (menu.get(text_field) or "").strip()
            img = menu.get(img_field)
            if not text and not isinstance(img, dict):
                continue

            # Meal heading line: "🍱 점심: 잔치국수 · 김치"
            one_line = " · ".join(p for p in text.split("\n") if p.strip()) if text else ""
            out.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [
                    {"type": "text", "text": {"content": f"{label}: "}, "annotations": {"bold": True}},
                    {"type": "text", "text": {"content": one_line}},
                ]},
            })

            # Meal photo (if any + session available)
            if kidsnote_sess is None or not isinstance(img, dict):
                continue
            url = img.get("original") or img.get("large") or img.get("url")
            if not url:
                continue
            try:
                resp = kidsnote_sess.get(url, timeout=120)
                resp.raise_for_status()
                raw = resp.content
            except Exception as e:
                _LOGGER.warning("menu photo download failed (%s): %s", url, e)
                continue
            hint = img.get("original_file_name") or f"menu_{text_field}.jpg"
            fid = self._upload_one_image(raw, hint)
            if fid:
                out.append({
                    "object": "block",
                    "type": "image",
                    "image": {"type": "file_upload", "file_upload": {"id": fid}},
                })
        return out

    @staticmethod
    def _fetch_comments(
        kidsnote_sess: requests.Session,
        kind: str,
        item_id: int,
    ) -> list[dict[str, Any]]:
        """Fetch parent + teacher comments on a report/notice/album.

        Confirmed live on 2026-05-13:
            GET /api/v1/reports/<id>/comments/
            GET /api/v1/notices/<id>/comments/
            GET /api/v1/albums/<id>/comments/  (same pattern)

        Returns empty list on any error so callers don't have to special-case.
        """
        try:
            r = kidsnote_sess.get(
                f"https://www.kidsnote.com/api/v1/{kind}/{item_id}/comments/",
                timeout=15,
            )
            if r.status_code != 200:
                return []
            return r.json().get("results") or []
        except Exception:
            return []

    def _comment_blocks(self, comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Render a list of comments as a Notion heading + paragraphs.

        Each comment becomes:
          - one bold gray line:  👩‍🏫 작성자 · 2026-05-12
          - body text (chunked if long)

        author.type=='teacher' → 👩‍🏫,  parent → 👨‍👩‍👧.
        """
        if not comments:
            return []
        out: list[dict[str, Any]] = [{
            "object": "block",
            "type": "heading_3",
            "heading_3": {"rich_text": [{
                "type": "text",
                "text": {"content": f"💬 댓글 ({len(comments)})"},
            }]},
        }]
        for c in comments:
            author = c.get("author") or {}
            atype = author.get("type") or ""
            prefix = {"teacher": "👩‍🏫", "parent": "👨‍👩‍👧", "admin": "🏫"}.get(atype, "")
            name = c.get("author_name") or author.get("name") or "?"
            created = (c.get("created") or "")[:10]
            head = f"{prefix} {name} · {created}".strip()
            out.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{
                    "type": "text",
                    "text": {"content": head},
                    "annotations": {"color": "gray", "bold": True},
                }]},
            })
            content = (c.get("content") or "").strip()
            if not content and c.get("emoticon_content"):
                content = "[이모티콘]"
            if content:
                for chunk in self._chunk(content):
                    out.append(self._para(chunk))
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
        # Title parts (built in order):
        #   [date]  author_icon  weather_emoji?  activity_labels_or_summary
        # Each piece appears only when meaningful.
        author_type = (report.get("author") or {}).get("type") or ""
        author_icon = {
            "teacher": "👩‍🏫",
            "parent": "👨‍👩‍👧",
            "admin": "🏫",
        }.get(author_type, "📝")

        # Weather: API field only. Empty → no weather in title or callout.
        w_code = report.get("weather")
        w_emoji = ""
        if w_code:
            w_display = WEATHER_KO.get(w_code, "")
            if w_display:
                w_emoji = w_display.split()[0]

        body_text = report.get("content") or ""
        categories = self._classify_categories(body_text)
        if categories:
            tail = " · ".join(categories)
        else:
            # Fallback to keyword summary. Strip several variants of the
            # child's name so it doesn't dominate the keyword list:
            #   full name (e.g. ``우하린``),
            #   last 2 chars (``하린``),
            #   either of those + ``이`` (``하린이`` is what teachers write).
            cname = report.get("child_name") or ""
            stripped = body_text
            if cname:
                variants = {cname, cname + "이", cname + "이가",
                            cname + "이는", cname + "이의"}
                if len(cname) >= 2:
                    short = cname[-2:]
                    variants.update({short, short + "이",
                                     short + "이가", short + "이는"})
                # Apply longer variants first so we don't accidentally
                # leave dangling "이"s.
                for v in sorted(variants, key=len, reverse=True):
                    stripped = stripped.replace(v, "")
            summary = self._summarize_text(stripped)
            tail = summary or f"알림장 #{report_id}"

        prefix_emojis = author_icon + (f" {w_emoji}" if w_emoji else "")
        title = f"[{date_str}] 알림장: {prefix_emojis} {tail}"

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
            # Pass session so meal photos get downloaded + uploaded inline.
            extras.extend(self._menu_summary_blocks(attached_menu, kidsnote_sess))
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

        # Append comments (parent + teacher replies) at the very end.
        if report.get("num_comments"):
            comments = self._fetch_comments(kidsnote_sess, "reports", report_id)
            children.extend(self._comment_blocks(comments))

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
        comment_kind: str | None = None,  # "notices" / "albums"; None = skip comments
    ) -> dict[str, Any]:
        """Generic publisher for items with the same shape as reports
        (notices, albums): title/content/author/attached_images/video/files.

        Uses the same upload + block-building logic as ``publish_report``.
        ``comment_kind``: URL segment for the comments endpoint (notices/albums).
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

        # ---- Append comments (parent + teacher replies) at the end ----
        if comment_kind and item.get("num_comments"):
            comments = self._fetch_comments(kidsnote_sess, comment_kind, item_id)
            blocks.extend(self._comment_blocks(comments))

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
            meta_bits=meta_bits, comment_kind="notices",
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
            meta_bits=meta_bits, comment_kind="albums",
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


    # ----------------------------------------------------------- stats dashboard

    # Pinned report id used so the dashboard page lives inside the DB
    # but never collides with a real kidsnote alimnota (positive 1e9 ids).
    DASHBOARD_REPORT_ID = -1
    DASHBOARD_TITLE = "📊 통계 대시보드"

    def _find_dashboard_page(self) -> str | None:
        """Locate the existing dashboard page by its sentinel Report ID."""
        self._resolve_schema()
        assert self._prop_report_id is not None
        try:
            r = self.session.post(
                f"{NOTION_API}/databases/{self.database_id}/query",
                headers=self._headers(),
                json={
                    "filter": {
                        "property": self._prop_report_id,
                        "number": {"equals": self.DASHBOARD_REPORT_ID},
                    },
                    "page_size": 1,
                },
                timeout=self.timeout,
            )
            r.raise_for_status()
            results = r.json().get("results") or []
            return results[0]["id"] if results else None
        except Exception as e:
            _LOGGER.warning("dashboard lookup failed: %s", e)
            return None

    def _archive_page(self, page_id: str) -> None:
        try:
            self.session.patch(
                f"{NOTION_API}/pages/{page_id}",
                headers=self._headers(),
                json={"archived": True},
                timeout=self.timeout,
            )
        except Exception as e:
            _LOGGER.warning("page archive failed (%s): %s", page_id, e)

    def publish_dashboard(self, stats: dict[str, Any]) -> dict[str, Any]:
        """Replace (archive + recreate) the singleton stats dashboard page.

        ``stats`` is a dict computed by the caller from the aggregated
        reports/menus/notices/albums. See ``_build_dashboard_blocks`` for
        the keys it consumes.
        """
        existing = self._find_dashboard_page()
        if existing:
            self._archive_page(existing)

        blocks = self._build_dashboard_blocks(stats)
        self._resolve_schema()
        assert self._prop_title is not None and self._prop_report_id is not None
        properties: dict[str, Any] = {
            self._prop_title: {
                "title": [{"text": {"content": self.DASHBOARD_TITLE}}],
            },
            self._prop_report_id: {"number": self.DASHBOARD_REPORT_ID},
        }
        if self._prop_date:
            properties[self._prop_date] = {
                "date": {"start": datetime.now().date().isoformat()},
            }
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
        return r.json()

    @staticmethod
    def _mermaid_block(code: str) -> dict[str, Any]:
        """Notion code block in mermaid language for inline charts."""
        return {
            "object": "block",
            "type": "code",
            "code": {
                "language": "mermaid",
                "rich_text": [{"type": "text", "text": {"content": code[:2000]}}],
            },
        }

    @staticmethod
    def _h2(text: str) -> dict[str, Any]:
        return {
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": text}}]},
        }

    def _build_dashboard_blocks(self, stats: dict[str, Any]) -> list[dict[str, Any]]:
        """Assemble the dashboard page body from a pre-computed stats dict."""
        blocks: list[dict[str, Any]] = []

        # ---- header callout: total counts + last refreshed timestamp ----
        last_refreshed = datetime.now().strftime("%Y-%m-%d %H:%M")
        summary_lines = [
            f"📨 알림장 {stats.get('reports_total', 0)}개  ·  "
            f"📢 공지 {stats.get('notices_total', 0)}개  ·  "
            f"📷 앨범 {stats.get('albums_total', 0)}개  ·  "
            f"🍱 식단 {stats.get('menus_total', 0)}개",
            f"마지막 갱신: {last_refreshed}",
        ]
        blocks.append({
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {"content": "\n".join(summary_lines)}}],
                "icon": {"type": "emoji", "emoji": "📊"},
                "color": "blue_background",
            },
        })

        # ---- 카테고리 분포 (top 10 pie) ----
        cat_counts = stats.get("category_counts") or {}
        if cat_counts:
            blocks.append(self._h2("🎨 카테고리 분포 (Top 10)"))
            top = sorted(cat_counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
            mer = ["pie title 활동 카테고리"]
            for label, n in top:
                # Strip leading emoji for mermaid label, keep Korean only
                safe = label.split(" ", 1)[-1] if " " in label else label
                mer.append(f'  "{safe}" : {n}')
            blocks.append(self._mermaid_block("\n".join(mer)))

        # ---- 월별 알림장 수 (table) ----
        monthly = stats.get("monthly_report_counts") or {}
        if monthly:
            blocks.append(self._h2("📅 월별 알림장 수"))
            ordered = sorted(monthly.items())
            lines = ["| 월 | 알림장 수 |", "|---|---|"]
            for m, n in ordered:
                lines.append(f"| {m} | {n} |")
            for line in lines:
                blocks.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text", "text": {"content": line}}]},
                })

        # ---- 작성자 비율 (pie) ----
        ac = stats.get("author_counts") or {}
        if ac:
            blocks.append(self._h2("✍️ 작성자 비율"))
            mer = ["pie title 작성자"]
            for atype, n in ac.items():
                label = {"teacher": "선생님", "parent": "부모", "admin": "원감/원장"}.get(atype, atype)
                mer.append(f'  "{label}" : {n}')
            blocks.append(self._mermaid_block("\n".join(mer)))

        # ---- 평균 수면 시간 분포 ----
        sh = stats.get("sleep_hour_dist") or {}
        if sh:
            blocks.append(self._h2("💤 낮잠 시간 분포"))
            mer = ["pie title 낮잠 시간"]
            for code, n in sh.items():
                label = SLEEP_HOUR_KO.get(code, code)
                mer.append(f'  "{label}" : {n}')
            blocks.append(self._mermaid_block("\n".join(mer)))

        # ---- 식사 상태 분포 ----
        ms = stats.get("meal_status_dist") or {}
        if ms:
            blocks.append(self._h2("🍽️ 식사 상태"))
            mer = ["pie title 식사 상태"]
            for code, n in ms.items():
                label = STATUS_KO.get(code, code)
                mer.append(f'  "{label}" : {n}')
            blocks.append(self._mermaid_block("\n".join(mer)))

        # ---- 날씨 분포 ----
        wd = stats.get("weather_dist") or {}
        if wd:
            blocks.append(self._h2("🌤️ 날씨 분포 (입력된 알림장만)"))
            mer = ["pie title 날씨"]
            for code, n in wd.items():
                label = WEATHER_KO.get(code, code)
                # mermaid pie labels can't include emojis cleanly — strip leading emoji
                ko_only = label.split(" ", 1)[-1] if " " in label else label
                mer.append(f'  "{ko_only}" : {n}')
            blocks.append(self._mermaid_block("\n".join(mer)))

        # ---- 첨부물 통계 ----
        att = stats.get("attachments") or {}
        if att:
            blocks.append(self._h2("📎 첨부물 누계"))
            lines = [
                f"📷 사진 {att.get('images', 0):,} 장",
                f"🎬 동영상 {att.get('videos', 0):,} 개  (5MB 이상 skip {att.get('videos_skipped', 0)} 개)",
                f"📄 첨부파일 {att.get('files', 0):,} 개  (5MB 이상 skip {att.get('files_skipped', 0)} 개)",
            ]
            for line in lines:
                blocks.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text", "text": {"content": line}}]},
                })

        return blocks


__all__ = ["NotionMirror", "DEFAULT_MAX_IMAGE_BYTES"]
