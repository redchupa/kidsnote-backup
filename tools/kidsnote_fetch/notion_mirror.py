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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from secret_input import clean_secret, normalize_notion_id, normalize_token  # local module
from secret_input import safe_err as _safe_err, safe_url as _safe_url  # local module
import doc_text  # local module


def _build_retrying_session() -> requests.Session:
    """A requests Session that automatically backs off and retries on
    Notion's 429 (Too Many Requests) and transient 5xx responses.

    Without this, a single 429 during a large backfill aborted that
    one page's POST /pages and left a gap (the page only recovered on
    the *next* cron cycle via dedup). With many parents running the
    workflow after launch, 429s get more frequent, so we retry in-
    process. ``respect_retry_after_header`` honors Notion's
    Retry-After so we wait exactly as long as the API asks.
    """
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=1.0,  # 0s, 1s, 2s, 4s, 8s between attempts
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST", "PATCH", "DELETE"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

# Lazy-loaded global kiwipiepy instance. The first call to ``_get_kiwi()``
# initializes the analyzer (loads the Korean morphology dictionary; ~80MB
# one-time). All subsequent calls reuse the same instance.
_KIWI_INSTANCE: Any = None
_KIWI_TRIED = False
# Ollama availability is checked once per process. If the env var
# OLLAMA_HOST is set AND /api/version responds, we mark it usable.
_OLLAMA_CONFIG: dict[str, str] | None = None
_OLLAMA_TRIED = False


def _get_kiwi() -> Any:
    """Return a shared Kiwi instance, or None if kiwipiepy is unavailable."""
    global _KIWI_INSTANCE, _KIWI_TRIED
    if _KIWI_TRIED:
        return _KIWI_INSTANCE
    _KIWI_TRIED = True
    try:
        from kiwipiepy import Kiwi  # type: ignore[import-not-found]
        _KIWI_INSTANCE = Kiwi()
        logging.getLogger(__name__).info("kiwipiepy loaded for keyword extraction")
    except Exception as e:
        logging.getLogger(__name__).warning(
            "kiwipiepy not available, falling back to heuristic keywords: %s", e,
        )
        _KIWI_INSTANCE = None
    return _KIWI_INSTANCE


def _get_ollama() -> dict[str, str] | None:
    """Return ``{host, model}`` if a reachable Ollama server is configured
    via env vars, otherwise None. Result is cached for the process lifetime.
    """
    global _OLLAMA_CONFIG, _OLLAMA_TRIED
    if _OLLAMA_TRIED:
        return _OLLAMA_CONFIG
    _OLLAMA_TRIED = True
    import os
    host = os.environ.get("OLLAMA_HOST")
    model = os.environ.get("OLLAMA_MODEL") or "qwen2.5:1.5b"
    if not host:
        return None
    # Ollama itself uses OLLAMA_HOST for its server-bind address and accepts
    # schema-less values like ``0.0.0.0:11434`` (= bind all interfaces). Many
    # users have that exact value set system-wide because that's what the
    # Ollama install docs suggest. But ``requests`` rejects schema-less URLs
    # ("No connection adapters were found for '0.0.0.0:11434/api/version'"),
    # so we auto-prepend http:// when missing. Also rewrite the wildcard
    # 0.0.0.0 to 127.0.0.1 since 0.0.0.0 means "all interfaces" on the
    # server side but is not a routable destination from the client side.
    if "://" not in host:
        host = "http://" + host
    host = host.replace("://0.0.0.0", "://127.0.0.1")
    # Probe /api/version with a short timeout — fail fast.
    try:
        r = requests.get(f"{host.rstrip('/')}/api/version", timeout=5)
        r.raise_for_status()
        _OLLAMA_CONFIG = {"host": host.rstrip("/"), "model": model}
        logging.getLogger(__name__).info(
            "Ollama available at %s (model=%s) for keyword extraction",
            host, model,
        )
    except Exception as e:
        logging.getLogger(__name__).warning(
            "OLLAMA_HOST set but unreachable, falling back to kiwi/heuristic: %s", e,
        )
        _OLLAMA_CONFIG = None
    return _OLLAMA_CONFIG

_LOGGER = logging.getLogger(__name__)


# Two-syllable Korean surnames. Anything not in this list is treated as a
# 1-syllable surname (the overwhelming majority).
_TWO_SYL_SURNAMES = ("황보", "남궁", "선우", "독고", "제갈", "사공", "서문", "동방")


def _given_name(full_name: str) -> str:
    """Return just the given name (이름) part of a Korean full name.

    Kidsnote stores ``성+이름`` together (e.g. ``우하린``), but a parent
    addresses the child by given name only (``하린아``). Strip the
    surname conservatively: 1 syllable by default, 2 syllables for the
    handful of well-known compound surnames.
    """
    if not full_name:
        return ""
    if full_name[:2] in _TWO_SYL_SURNAMES:
        return full_name[2:]
    return full_name[1:] if len(full_name) > 1 else full_name


def _vocative_marker(name: str) -> str:
    """Pick the right Korean vocative ending: ``야`` after a vowel-final
    syllable, ``아`` after a consonant-final one (받침 check)."""
    if not name:
        return "야"
    last = name[-1]
    if not ("가" <= last <= "힣"):
        return "야"
    jongseong = (ord(last) - 0xAC00) % 28
    return "야" if jongseong == 0 else "아"


def _addressee(child_name: str) -> str:
    """Compose ``우리 {이름}{야|아}`` for parent-to-child letters; falls back
    to a generic affectionate phrase when no name is available."""
    given = _given_name(child_name)
    if not given:
        return "사랑하는 아이야"
    return f"우리 {given}{_vocative_marker(given)}"


def _topic_form(name: str) -> str:
    """Korean topic-marker-compatible nickname.

    The native parent-speech pattern is ``{name}이`` + topic marker (``하린이가``)
    for consonant-final names, and plain ``{name}`` + topic marker (``유주가``)
    for vowel-final names. Hardcoded `{given}이` in prompts produced wrong forms
    like ``유주이가`` / ``유주이를`` and broke the model's downstream vocative
    too (``유주이야``). This helper restores the rule: append ``이`` ONLY when
    the syllable has 받침.

    Hangul check: anything not in the syllable block (e.g. ``Anna``) is passed
    through as-is — Latin names don't take the ``이`` filler at all.
    """
    if not name:
        return ""
    last = name[-1]
    if not ("가" <= last <= "힣"):
        return name
    jongseong = (ord(last) - 0xAC00) % 28
    return name + ("이" if jongseong != 0 else "")


def _strip_lead_meta(text: str) -> str:
    """Drop leading lines that restate the task instead of answering it.

    llama3.1 sometimes echoes the prompt back as a preamble
    (``알림장을 바탕으로 ~를 써보겠습니다.``) before producing the
    real content. We detect short opening lines that contain task
    verbs and drop them until the first real-content line.
    """
    if not text:
        return text
    TASK_VERBS = (
        # 쓰- (write) variants
        "써보겠습니다", "써봅니다", "써 보겠", "써 봅니다", "써보세요",
        "써 보겠습니다", "써 봅시다", "써보아요",
        # 정리해- (organize)
        "정리해보겠습니다", "정리해 보겠습니다", "정리해보세요",
        "정리해드리겠습니다", "정리해 드리겠습니다", "정리합니다",
        # 분석해- (analyze)
        "분석해보겠습니다", "분석해 보겠습니다",
        "분석해드리겠습니다", "분석해 드리겠습니다",
        # 추출해- (extract)
        "추출해보겠습니다", "추출해 보겠습니다",
        # 변환해- (convert) — caught a parent-post diary that started
        # with "아래와 같이 변환해 드릴게요." Added "변환해 보겠습니다"
        # variants 2026-05-23 after the production live DB shipped pages
        # with leading "알림장의 내용을 일기로 변환해 보겠습니다." —
        # llama3.1:8b loves this phrasing even though the prompt says
        # not to.
        "변환해 드릴게요", "변환해드릴게요", "변환해드리겠습니다",
        "변환해 드리겠습니다", "변환합니다", "변환해 봅니다",
        "변환해 보겠습니다", "변환해보겠습니다", "변환해 볼게요",
        "변환해볼게요",
        # 작성해- (compose) — caught a parent-letter that started with
        # "알림장의 내용을 바탕으로 편지를 작성해 보겠습니다."
        "작성해 드릴게요", "작성해드릴게요", "작성해 보겠습니다",
        "작성해보겠습니다", "작성합니다", "작성해 드리겠습니다",
        "작성해드리겠습니다",
        # 만들어- (make)
        "만들어 드릴게요", "만들어드릴게요", "만들어 보겠습니다",
        "만들어보겠습니다",
        # 답해- (answer)
        "답해 드릴게요", "답해드릴게요", "답해 보겠습니다",
        "답해드리겠습니다",
        # Other lead-in cues
        "구체적으로 인용", "한 단락으로",
        "다음과 같습니다", "다음과 같다",
    )
    # Catch-all pattern: short opening sentence ending with "보겠습니다 /
    # 보겠어요 / 드리겠습니다 / 드릴게요 / 드릴게 / 봅니다 / 답할게요" — these
    # are the closing markers of a task restatement regardless of which
    # verb prefix the model picked. The earlier explicit TASK_VERBS list
    # still catches them when paired with the right verb, but llama3.1
    # sometimes invents a paraphrase ("작성해보도록 하겠습니다") that
    # the literal list misses. A short opening line + one of these endings
    # is essentially always a meta sentence.
    #
    # 2026-05-24: added past-tense "결과입니다 / 결과예요" — production
    # shipped "알림장 내용을 일기로 변환한 결과입니다." as a leak that
    # neither TASK_VERBS (no verb match) nor the live-future markers
    # (보겠습니다 etc) caught. The past-participle form is just another
    # way the model paraphrases the restatement.
    META_ENDINGS = (
        "보겠습니다.", "보겠습니다", "보겠어요.", "보겠어요",
        "드리겠습니다.", "드리겠습니다", "드리겠어요.", "드리겠어요",
        "드릴게요.", "드릴게요", "드릴게.",
        "봅니다.", "봅니다",
        "결과입니다.", "결과입니다", "결과예요.", "결과예요",
        "결과입니다!", "결과입니다 ", "변환한 결과", "정리한 결과",
    )
    lines = text.split("\n")
    out: list[str] = []
    started = False
    for line in lines:
        if not started:
            s = line.strip()
            if not s:
                continue
            short = len(s) < 130
            if short and any(v in s for v in TASK_VERBS):
                continue
            if short and s.endswith(("다음과 같습니다.", "다음과 같다.", "다음과 같이.")):
                continue
            # Catch-all: short line ending with the meta closing marker
            # AND containing "알림장" / "일기" / "편지" / "요약" / "본문" —
            # i.e. references the task itself. This is conservative: a
            # real first sentence of a diary/letter would rarely contain
            # both a task noun and a meta ending.
            if short and any(s.endswith(e) for e in META_ENDINGS):
                if any(t in s for t in ("알림장", "일기", "편지", "요약", "본문", "내용을")):
                    continue
            started = True
        out.append(line)
    return "\n".join(out).strip()


def _extract_after_final_label(text: str, labels: tuple[str, ...]) -> str:
    """If any label appears in ``text``, return content after the LAST one.

    Some models produce analysis + the final answer prefixed by a
    section label like ``성장 스토리:``. The final block is usually the
    cleanest pass — keep only that.
    """
    if not text or not labels:
        return text
    best_idx = -1
    best_len = 0
    for label in labels:
        idx = text.rfind(label)
        if idx > best_idx:
            best_idx = idx
            best_len = len(label)
    if best_idx >= 0:
        return text[best_idx + best_len:].strip()
    return text


def _strip_cjk(text: str) -> tuple[str, int]:
    """Strip CJK Unified Ideographs (한자/중국어/일본어 한자) from ``text``.

    Returns ``(cleaned, removed_count)``. Korean Hangul is preserved
    (separate Unicode block); only the CJK ideograph blocks
    U+3400–U+4DBF, U+4E00–U+9FFF, and the CJK Extension blocks are
    stripped. qwen2.5-family models occasionally leak Chinese into
    Korean output; this is a defensive net to keep keepsake text clean.
    """
    if not text:
        return text, 0
    # Fullwidth / CJK punctuation that leaked into Korean parent-letter
    # output during 2026-05-22 review (e.g. `"운동은 심장에。"`). U+3002
    # is the Japanese/Chinese full stop; the rest are common companions
    # the same models drop into Korean text.
    _FULLWIDTH_PUNCT = {
        "、": ", ",   # 、 ideographic comma
        "。": ". ",   # 。 ideographic full stop
        "，": ", ",   # ， fullwidth comma
        "．": ". ",   # ． fullwidth period
        "：": ": ",   # ： fullwidth colon
        "；": "; ",   # ； fullwidth semicolon
        "！": "! ",   # ！ fullwidth exclamation
        "？": "? ",   # ？ fullwidth question
    }
    removed = 0
    out_chars: list[str] = []
    for ch in text:
        cp = ord(ch)
        is_cjk = (
            0x3400 <= cp <= 0x4DBF
            or 0x4E00 <= cp <= 0x9FFF
            or 0x20000 <= cp <= 0x2A6DF
            or 0x2A700 <= cp <= 0x2B73F
            or 0xF900 <= cp <= 0xFAFF  # CJK compatibility ideographs
        )
        if is_cjk:
            removed += 1
            continue
        # Fullwidth punctuation: substitute the ASCII equivalent so the
        # sentence still reads naturally instead of just being deleted.
        # These don't count toward `removed` (they get replaced, not lost),
        # so the >20% Chinese-leak guard doesn't fire on a punctuation-only
        # leak.
        if ch in _FULLWIDTH_PUNCT:
            out_chars.append(_FULLWIDTH_PUNCT[ch])
            continue
        out_chars.append(ch)
    return "".join(out_chars), removed


def _english_word_leak_ratio(text: str) -> tuple[int, int]:
    """Detect English words mixed into a Korean output.

    Returns ``(word_count, longest_run)``. A "word" here is a run of
    ASCII alphabet chars >= 3 (so 2-letter shorthand like cm/mg/PM
    doesn't trigger). Threshold of 3 chars catches the production
    case ``came`` (4 chars) without flagging legitimate quoted
    proper nouns in real Korean text — those are rare and the
    extracted run still has to be standalone (not part of a Korean
    word).

    Used to catch llama3.1:8b leaks like:
        "오늘도 선생님께서는 우리 집에 전화해 came."
    where "came" landed in the Korean output.
    """
    if not text:
        return 0, 0
    import re
    # Match runs of ASCII letters bounded by non-letter on both sides.
    # \b doesn't work well across Korean boundaries, so use lookarounds
    # that explicitly require non-ASCII-letter neighbors.
    words = re.findall(r"(?<![A-Za-z])[A-Za-z]{3,}(?![A-Za-z])", text)
    if not words:
        return 0, 0
    return len(words), max(len(w) for w in words)


def _looks_like_input_copy(output: str, input_text: str) -> bool:
    """Detect when LLM copies a substantial chunk of input verbatim.

    Caught a case where a parent self-introduction post got fed into
    the child-diary prompt; llama3.1:8b gave up converting and just
    echoed the first sentence ("반갑습니다 선생님 1202동 동대표
    우석만입니다 잘 부탁드립니다."). That leaked the parent's full name
    + apartment number into the Notion page.

    Heuristic: if a 25-char run from the output's first 60 chars
    appears in the input (whitespace + punctuation normalized), it's
    a copy. Diaries should be a re-voicing, not a literal quotation.
    Quoted curriculum names are fine because they're short (<25 chars).
    """
    if not output or not input_text or len(output) < 30:
        return False
    import re
    # Strip punctuation + collapse whitespace so the LLM adding a
    # period after the copied sentence doesn't fool the matcher.
    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^\w\s가-힣]", "", s)).strip()
    out_n = norm(output[:60])
    body_n = norm(input_text)
    if len(out_n) < 25:
        return False
    # Try the first 25-char window. If that's in the body, the LLM
    # echoed a sentence verbatim.
    return out_n[:25] in body_n


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
NOTION_PAGE_CHILDREN_CAP = 100        # Notion: max children per create/append call.
# Document text kept per page. Korean is sent \u-escaped (6 bytes a char), so
# 30k chars ≈ 180KB, well inside Notion's 500KB request limit with the rest.
DOC_TEXT_PAGE_CHARS = 30_000


# ---------------------------------------------------------------- attachments
#
# Videos and generic files are uploaded as-is, so anything over the Notion
# per-file cap can't go in. Instead of dropping it silently, the page (and the
# 📊 dashboard) carries a link to the kidsnote original. Live-checked
# 2026-11-15: every attachment object carries ``file_size`` (bytes), and its
# URLs are permanent public Kakao CDN links (``up-kids-kage.kakao.com/dn/…``,
# no query string, no login needed, 2025 links still answer 200).

def media_url(obj: dict[str, Any]) -> str:
    """The best-quality URL of one attachment object ('' when it has none).

    Videos have no ``original`` key, only ``high`` / ``low`` / ``preview``.
    """
    for key in ("original", "high", "url"):
        url = obj.get(key)
        if isinstance(url, str) and url:
            return url
    return ""


def fetch_capped(sess: Any, url: str, limit: int, timeout: int) -> bytes | None:
    """Download ``url``, giving up (``None``) once it passes ``limit`` bytes.

    ``file_size`` can be missing or wrong, so the body is streamed instead of
    trusting it; HTTP errors raise like ``raise_for_status``.
    """
    r = sess.get(url, timeout=timeout, stream=True)
    try:
        r.raise_for_status()
        buf = bytearray()
        for chunk in r.iter_content(64 * 1024):
            buf += chunk
            if len(buf) > limit:
                return None
        return bytes(buf)
    finally:
        close = getattr(r, "close", None)
        if close:
            close()


def video_objects(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Video attachments of a report / notice / album. Kidsnote stores a single
    object (or None) under ``attached_video``; older shapes used a list."""
    for key in ("attached_video", "video", "attached_videos"):
        v = item.get(key)
        if isinstance(v, dict):
            return [v]
        if isinstance(v, list) and v:
            return [x for x in v if isinstance(x, dict)]
    return []


def file_objects(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Generic file attachments (PDF, Excel, ...) of a report / notice / album."""
    return [f for f in item.get("attached_files") or [] if isinstance(f, dict)]


def media_name(obj: dict[str, Any], kind: str) -> str:
    """Display / upload filename of an attachment."""
    name = obj.get("original_file_name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return f"{kind}_{obj.get('id', 'x')}" + (".mp4" if kind == "video" else ".bin")


def declared_size(obj: dict[str, Any]) -> int | None:
    """``file_size`` from kidsnote metadata, or None when missing / unusable."""
    size = obj.get("file_size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return None
    return size


def cap_label(cap: int) -> str:
    """5_000_000 → '5MB' (the per-file limit as people read it)."""
    return f"{cap / 1_000_000:g}MB"


def format_mb(n_bytes: int) -> str:
    """5_946_685 → '5.9MB'. Decimal MB, matching the '5MB' cap wording."""
    mb = n_bytes / 1_000_000
    return f"{mb:.1f}MB" if mb < 100 else f"{mb:.0f}MB"


def oversized_media(item: dict[str, Any], cap: int) -> list[dict[str, Any]]:
    """Videos / files of ``item`` whose declared size is over ``cap``.

    Used by the 📊 dashboard to list every file Notion can't hold, straight
    from the kidsnote metadata, so the list covers the whole history (pages
    published before links existed included) without re-downloading anything.
    Each entry: ``{kind, name, size, url}``.
    """
    out: list[dict[str, Any]] = []
    for kind, objs in (("video", video_objects(item)), ("file", file_objects(item))):
        for obj in objs:
            size = declared_size(obj)
            if size is not None and size > cap:
                out.append({"kind": kind, "name": media_name(obj, kind),
                            "size": size, "url": media_url(obj)})
    return out


def attachment_stats(
    sources: list[tuple[str, list[dict[str, Any]]]],
    page_ids: dict[int, str],
    cap: int,
) -> dict[str, Any]:
    """The 📊 dashboard's 📎 section, counted from kidsnote metadata.

    ``sources`` is ``[(label, items), ...]`` (알림장 / 공지 / 앨범), all
    fetched every run, so the totals are the whole history rather than
    just this run's uploads, and ``oversized`` lists every video / file
    Notion can't hold. ``page_ids`` maps a kidsnote id to its Notion page,
    so the list can link back to the page the file belongs to.
    """
    images = videos = files = 0
    oversized: list[dict[str, Any]] = []
    for label, items in sources:
        for item in items:
            images += sum(1 for x in item.get("attached_images") or [] if isinstance(x, dict))
            videos += len(video_objects(item))
            files += len(file_objects(item))
            over = oversized_media(item, cap)
            if not over:
                continue
            date = str(item.get("date_written") or item.get("created") or "")[:10]
            try:
                page_id = page_ids.get(int(item.get("id") or 0), "")
            except (TypeError, ValueError):
                page_id = ""
            for o in over:
                oversized.append({**o, "date": date, "source": label, "page_id": page_id})
    return {"images": images, "videos": videos, "files": files,
            "oversized": oversized, "cap": cap}


def _is_linkable(url: str) -> bool:
    """Notion rejects the whole page for a malformed link, so only plain
    http(s) URLs within its 2000-char limit become links."""
    return (
        isinstance(url, str)
        and url.startswith(("https://", "http://"))
        and len(url) <= 2000
        and not any(c.isspace() for c in url)
    )


def notion_page_url(page_id: str) -> str:
    return f"https://www.notion.so/{page_id.replace('-', '')}"

# Kidsnote life-record status codes → human Korean. Unknown values are
# rendered as-is, so missing entries here just degrade gracefully.
# Wording is copied from kidsnote's own web UI (the report page's i18n store,
# tests/data/kidsnote_status_labels_ko.json) so pages read exactly like the
# app. Codes kidsnote's UI no longer lists keep their earlier wording.
SLEEP_HOUR_KO = {
    "no_sleep": "잠을 안 잤어요",
    "none": "안 잠",
    "below_1": "1시간 미만",
    "under_30m": "30분 이내",
    "30m_to_1": "30분~1시간",
    "1_to_1.5": "1시간~1시간30분",
    "1.5_to_2": "1시간30분~2시간",
    "over_2": "2시간 이상",
    "sleep_hardly": "잠을 설쳤어요",
    "sleep_late": "늦잠 잤어요",
    "sleep_well": "잠을 푹 잤어요",
    "slp_good": "단잠",
    "slp_normal": "보통",
    "slp_bad": "부족",
}
STATUS_KO = {
    "good": "좋음",
    "average": "보통",
    "bad": "나쁨",
    "normal": "정상",
    "high": "고열",
    "low": "낮음",
    "soft": "묽음",
    "hard": "딱딱함",
    "none": "안했음",
    "fixed": "정량",
    "more": "많이",
    "less": "적게",
    "sick": "아픔",
    "fine": "양호",
    "trimmed": "정리됨",
    "needs_trim": "정리 필요",
    "active": "활발",
    "calm": "차분",
    # bowel_status / bowel[].status
    "watery": "묽음",
    "diarrhea": "설사",
    "bw_no": "안했음",
    "bw_one": "1회",
    "bw_two": "2회",
    # meal_status
    "ml_free": "자율배식",
    "ml_one": "1회",
    "ml_two": "2회",
    # temperature_status
    "slight": "미열",
    # bath_status
    "bath": "목욕",
    "shower": "샤워",
    # nail_status
    "cut": "자름",
    "ok": "양호",
    # outdoor_activity_status (booleans are looked up as "true" / "false")
    "true": "O",
    "false": "X",
}
ACTIVITY_RATE_KO = {"10": "적극적", "20": "보통", "30": "소극적"}
WEATHER_KO = {
    # Codes the live kidsnote API actually uses (sampled from 391 reports):
    "sunny": "☀️ 맑음",
    "partly_cloudy": "⛅ 구름조금",
    "mostly_cloudy": "🌥️ 구름많음",
    "overcast": "☁️ 흐림",
    "fog": "🌫️ 안개",
    "rain": "🌧️ 비",
    "sunny_after_rain": "🌈 비온후갬",
    "snow": "❄️ 눈",
    "yellow_sand": "🟡 황사",
    "thunderstorm": "⛈️ 낙뢰",
    "mixed_rain_snow": "🌨️ 눈비/비눈",
    # Fallbacks for variants that may show up at other daycares:
    "cloudy": "☁️ 흐림",
    "rainy": "🌧️ 비",
    "snowy": "❄️ 눈",
    "foggy": "🌫️ 안개",
    "windy": "💨 바람",
    "stormy": "⛈️ 폭풍",
    "hot": "🥵 더움",
    "cold": "🥶 추움",
    # Also in kidsnote's own weather picker:
    "hail": "🧊 우박",
    "shower": "🌦️ 소나기",
    "shower_rain": "🌦️ 소나기",
}
# kidsnote's "표시안함" (don't show weather) choices — treat as no weather.
WEATHER_HIDDEN = frozenset({"none", "undefined"})

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
    # 🎓 학습 keyword list intentionally generous to cover language-academy
    # (어학원) alimnotas in addition to daycare alimnotas. Without these
    # academy posts fell into the heuristic-keyword fallback path and the
    # title ended up reading like a noisy word-list ("주요, 시작한다, 명령문...").
    ("🎓 학습",     ("한글", "숫자", "영어", "수업", "글자",
                     "배우는", "익히는", "배운다", "익힌다",
                     "배워", "익혀", "배웠", "익혔", "배웁", "익힙",
                     "학습", "공부", "예습", "복습", "이해",
                     "교재", "교과", "단어", "어휘", "문법",
                     "문장", "표현", "파닉스", "발음", "받아쓰기",
                     "쓰기 연습", "읽기 연습", "말하기 연습",
                     "동사", "명사", "형용사", "부사", "조사",
                     "현재 진행형", "현재진행형", "동명사", "명령문",
                     "프로젝트")),
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
REPORT_ID_NAME_CANDIDATES = (
    "Report ID", "리포트 ID", "리포트id", "report_id", "보고서 ID",
    # Add casual short forms operators tend to type when reading the
    # Quick Start guide: 번호 / 숫자. Without these the fallback
    # "first Number property" path eventually catches it, but the
    # error message users see when the wrong column name takes over
    # is more confusing than just supporting both names directly.
    "번호", "숫자", "Number", "id", "ID",
)
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
        # Secrets often arrive with stray whitespace or a BOM, and the
        # database setting as a whole Notion link instead of the bare id.
        self.token = normalize_token(token)
        self.database_id = normalize_notion_id(database_id) or clean_secret(database_id)
        self.max_image_bytes = max_image_bytes
        self.strip_exif_gps = strip_exif_gps
        self.session = session or _build_retrying_session()
        self.timeout = timeout
        # When True, skip the 3 per-alimnota LLM callouts (💭 요약 /
        # 🧒 자녀 일기 / 👨‍👩‍👧 부모 편지). Used by the --no-llm /
        # DISABLE_ALL_LLM master toggle: operators who want plain
        # backup + statistical dashboards only (graduate backups,
        # privacy-conscious operators, etc.) flip this on. The 4 LLM
        # dashboards still need to be skipped separately via their
        # own toggles — fetch.py wires both at the same time.
        self.disable_llm_callouts: bool = False
        # 2026-05-24: hard wall-clock cap for each LLM dashboard.
        # Without this, the dashboard's internal LLM loop kept running
        # past the 6h GHA cap (8-10h runs observed) because the budget
        # check was only at dashboard ENTRY, not inside the per-report
        # iteration. fetch.py sets self.dashboard_max_seconds before
        # each dashboard call based on the remaining workflow budget.
        # None means no cap (legacy behavior, for local --monthly-sample).
        self.dashboard_max_seconds: float | None = None
        self._dashboard_start_time: float = 0.0
        # Resolved on first use via `_resolve_schema()`.
        self._prop_title: str | None = None
        self._prop_report_id: str | None = None
        self._prop_date: str | None = None

    def _dashboard_started(self) -> None:
        """Stamp start time at the entry of each publish_X dashboard
        method so the wall-clock check inside the loop has an anchor.
        """
        import time as _t
        self._dashboard_start_time = _t.monotonic()

    def _dashboard_over_cap(self) -> bool:
        """True if the current dashboard has exceeded its wall-clock cap.
        Use inside dashboard loops: ``if self._dashboard_over_cap(): break``.
        """
        if self.dashboard_max_seconds is None:
            return False
        import time as _t
        return (_t.monotonic() - self._dashboard_start_time) > self.dashboard_max_seconds

    BACKUP_DATABASE_TITLE = "키즈노트 백업"

    def _maybe_recover_db_id_from_page(self) -> None:
        """Make NOTION_DATABASE_ID point at a usable database.

        People paste one of three things: a database link (used as is), the
        link of a page that already holds an inline database (that database is
        used), or a blank page, which the setup guide asks for because it is
        the easiest to get right (the backup database is created inside it).
        """
        try:
            probe = self.session.get(
                f"{NOTION_API}/databases/{self.database_id}",
                headers=self._headers(),
                timeout=self.timeout,
            )
        except Exception:
            return  # network hiccup; the schema request right after reports it
        if probe.status_code != 400:
            return
        body = probe.text or ""
        if "is a page" not in body and "page, not a database" not in body:
            return
        page_id = self.database_id
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            r = self.session.get(
                f"{NOTION_API}/blocks/{page_id}/children",
                headers=self._headers(),
                params=params,
                timeout=self.timeout,
            )
            if not r.ok:
                raise RuntimeError(
                    f"노션 페이지 내용을 읽지 못했습니다 (HTTP {r.status_code}). "
                    "그 페이지에 연결(통합)을 추가했는지 확인해 주세요."
                )
            data = r.json()
            for blk in data.get("results") or []:
                if blk.get("type") == "child_database" and blk.get("id"):
                    self.database_id = blk["id"].replace("-", "")
                    _LOGGER.info("Using the database inside the NOTION_DATABASE_ID page")
                    return
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        self.database_id = self._create_backup_database(page_id)

    def _create_backup_database(self, page_id: str) -> str:
        """Create the inline backup database (이름 / 날짜 / Report ID) inside ``page_id``."""
        r = self.session.post(
            f"{NOTION_API}/databases",
            headers=self._headers(),
            json={
                "parent": {"type": "page_id", "page_id": page_id},
                "is_inline": True,
                "title": [{"type": "text", "text": {"content": self.BACKUP_DATABASE_TITLE}}],
                "properties": {
                    "이름": {"title": {}},
                    "날짜": {"date": {}},
                    "Report ID": {"number": {"format": "number"}},
                },
            },
            timeout=self.timeout,
        )
        if not r.ok:
            raise RuntimeError(
                f"노션 페이지 안에 백업용 데이터베이스를 만들지 못했습니다 (HTTP {r.status_code}). "
                "노션 연결(통합) 설정에서 콘텐츠 읽기·업데이트·입력 권한이 모두 켜져 있는지 확인해 주세요."
            )
        db_id = (r.json().get("id") or "").replace("-", "")
        if not db_id:
            raise RuntimeError("노션이 새 데이터베이스의 id를 돌려주지 않았습니다.")
        _LOGGER.info("✅ Created the backup database inside the Notion page")
        return db_id

    def _database_properties(self) -> dict[str, Any]:
        r = self.session.get(
            f"{NOTION_API}/databases/{self.database_id}",
            headers=self._headers(),
            timeout=self.timeout,
        )
        if r.status_code == 401:
            raise RuntimeError(
                "노션 토큰(NOTION_TOKEN)이 올바르지 않습니다. "
                "노션 연결(통합) 화면에서 토큰을 다시 복사해 시크릿을 수정해 주세요."
            )
        if r.status_code in (403, 404):
            raise RuntimeError(
                "노션 데이터베이스에 접근할 수 없습니다. 페이지에 연결(통합)을 추가했는지, "
                "NOTION_DATABASE_ID 가 그 페이지의 링크인지 확인해 주세요."
            )
        r.raise_for_status()
        return r.json().get("properties") or {}

    def _resolve_schema(self) -> None:
        """Discover the title / number / date property names, adding any that are missing."""
        if self._prop_report_id is not None:
            return  # already resolved
        self._maybe_recover_db_id_from_page()
        props = self._database_properties()

        # A table made by hand may lack the number / date columns. Add them
        # instead of failing, so any database (or a blank page) is enough.
        missing: dict[str, Any] = {}
        if not any(meta.get("type") == "number" for meta in props.values()):
            name = next((n for n in ("Report ID", "키즈노트 번호") if n not in props), "Report ID 2")
            missing[name] = {"number": {"format": "number"}}
        if not any(meta.get("type") == "date" for meta in props.values()):
            name = next((n for n in ("날짜", "작성일") if n not in props), "날짜 2")
            missing[name] = {"date": {}}
        if missing:
            r = self.session.patch(
                f"{NOTION_API}/databases/{self.database_id}",
                headers=self._headers(),
                json={"properties": missing},
                timeout=self.timeout,
            )
            if not r.ok:
                raise RuntimeError(
                    f"노션 데이터베이스에 필요한 열({', '.join(missing)})을 추가하지 못했습니다 "
                    f"(HTTP {r.status_code})."
                )
            _LOGGER.info("Added missing Notion DB properties: %s", ", ".join(missing))
            props = self._database_properties()

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
            raise RuntimeError("노션 데이터베이스에 숫자 열(Report ID)이 없습니다.")
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
        return set(self.existing_report_page_map().keys())

    def existing_report_page_map(self) -> dict[int, str]:
        """Map every existing `Report ID` to its Notion page id.

        Used by --force-refresh to archive prior versions of each
        report before publishing the new prompt-style callouts.
        """
        self._resolve_schema()
        assert self._prop_report_id is not None
        out: dict[int, str] = {}
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
                if rid is None:
                    continue
                try:
                    rid_int = int(rid)
                except (TypeError, ValueError):
                    continue
                # Skip sentinels (-1..-7) so force-refresh of regular
                # reports never archives the dashboards.
                if rid_int < 0:
                    continue
                out[rid_int] = page["id"]
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return out

    def archive_by_report_id(self, report_id: int, page_map: dict[int, str]) -> bool:
        """Archive the existing Notion page for `report_id` if one exists.

        Returns True when a page was archived. Idempotent — safe to call
        for IDs not yet in the DB.
        """
        page_id = page_map.pop(report_id, None)
        if not page_id:
            return False
        self._archive_page(page_id)
        return True

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

        # Retry the whole create+PUT cycle when the upload_url expires
        # mid-publish. Production logs (2026-05-23) showed many 403s on
        # the upload_url several minutes after creation — the previous
        # alimnota's slow LLM callouts let the signed URL go stale. By
        # retrying the create step on PUT failure, the second attempt
        # gets a fresh URL and usually succeeds.
        last_err: Exception | None = None
        for attempt in (1, 2):
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
                last_err = e
                if attempt == 1:
                    continue  # transient network — retry once
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
                return file_upload_id
            except Exception as e:
                last_err = e
                if attempt == 1:
                    _LOGGER.info(
                        "image upload retry for %s (attempt 1 → %s)",
                        filename_hint, type(e).__name__,
                    )
                    continue
                _LOGGER.warning("file upload PUT failed for %s: %s", filename_hint, _safe_err(e))
                return None
        return None

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
            _LOGGER.warning("%s upload PUT failed for %s: %s", kind, filename, _safe_err(e))
            return None

        return file_upload_id

    def _upload_blob_or_link(
        self,
        obj: dict[str, Any],
        kidsnote_sess: requests.Session,
        kind: str,  # "video" or "file"
    ) -> tuple[str | None, dict[str, Any] | None, str]:
        """Upload one video / file, or say why not.

        Returns ``(file_upload_id, link, text)``: the upload id on success, a
        ``link`` when it can't go into Notion but the page should link the
        kidsnote original (``{kind, name, size, url, reason}``, reason
        ``"too_big"`` or ``"failed"``), and for documents (PDF / HWP / …) the
        text inside, for the page and Notion search (see doc_text.py).

        Over-cap videos are recognised from kidsnote's ``file_size`` and never
        downloaded. Over-cap documents up to 20MB are still fetched, only to
        read their text.
        """
        url = media_url(obj)
        name = media_name(obj, kind)
        size = declared_size(obj)
        if not url:
            return None, None, ""

        def link(reason: str, n_bytes: int | None) -> dict[str, Any]:
            return {"kind": kind, "name": name, "size": n_bytes, "url": url, "reason": reason}

        def too_big(n_bytes: int) -> bool:
            if n_bytes > self.max_image_bytes:
                _LOGGER.info(
                    "%s %s is %s, over the %s Notion file limit: linking the kidsnote original instead",
                    kind, name, format_mb(n_bytes), cap_label(self.max_image_bytes),
                )
                return True
            return False

        wants_text = (kind == "file" and doc_text.can_extract(name)
                      and (size is None or size <= doc_text.MAX_INPUT_BYTES))
        if size is not None and not wants_text and too_big(size):
            return None, link("too_big", size), ""
        limit = doc_text.MAX_INPUT_BYTES if wants_text else self.max_image_bytes
        try:
            raw = fetch_capped(kidsnote_sess, url, limit, 180)
        except Exception as e:
            _LOGGER.warning("%s download failed (%s): %s", kind, _safe_url(url), _safe_err(e))
            known_big = size is not None and size > self.max_image_bytes
            return None, link("too_big" if known_big else "failed", size), ""
        if raw is None:  # bigger than anything we'd keep
            too_big(limit + 1)
            return None, link("too_big", size), ""
        text = doc_text.extract_text(raw, name) if wants_text else ""
        real_size = max(len(raw), size or 0)
        if too_big(real_size):
            return None, link("too_big", real_size), text
        fid = self._upload_one_blob(raw, name, kind=kind)
        if fid:
            return fid, None, text
        return None, link("failed", len(raw)), text

    def _upload_media(
        self,
        item: dict[str, Any],
        kidsnote_sess: requests.Session,
    ) -> dict[str, Any]:
        """Upload every photo / video / file of a report, notice or album.

        Returns ``{image_ids, images_failed, video_ids, videos_failed,
        file_ids, files_failed, links, texts}``; ``file_ids`` holds
        ``(id, name)`` pairs, ``links`` the videos / files that stay on
        kidsnote and ``texts`` ``(name, text)`` of readable documents (see
        ``_upload_blob_or_link``).
        """
        image_ids: list[str] = []
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
                _LOGGER.warning("photo download failed (%s): %s", _safe_url(url), _safe_err(e))
                images_failed += 1
                continue
            hint = img.get("original_file_name") or f"image_{img.get('id', 'x')}.jpg"
            fid = self._upload_one_image(raw_bytes, hint)
            if fid:
                image_ids.append(fid)
            else:
                images_failed += 1

        out: dict[str, Any] = {
            "image_ids": image_ids, "images_failed": images_failed,
            "video_ids": [], "videos_failed": 0,
            "file_ids": [], "files_failed": 0,
            "links": [], "texts": [],
        }
        for kind, objs in (("video", video_objects(item)), ("file", file_objects(item))):
            for obj in objs:
                fid, link, text = self._upload_blob_or_link(obj, kidsnote_sess, kind)
                if text:
                    out["texts"].append((media_name(obj, kind), text))
                if fid and kind == "video":
                    out["video_ids"].append(fid)
                elif fid:
                    out["file_ids"].append((fid, media_name(obj, kind)))
                else:
                    out[f"{kind}s_failed"] += 1
                    if link:
                        out["links"].append(link)
        return out

    def _media_link_blocks(
        self,
        links: list[dict[str, Any]],
        *,
        how_to: bool = True,
    ) -> list[dict[str, Any]]:
        """One bullet per file left on kidsnote, after a gray how-to line
        (``how_to=False`` when the page already showed it)."""
        if not links:
            return []
        blocks: list[dict[str, Any]] = []
        if how_to:
            blocks.append(self._para(
                "노션에 올리지 못한 파일은 키즈노트 원본 링크로 남겨요. "
                "파일 이름을 누르면 보거나 저장할 수 있어요.",
                color="gray",
            ))
        for ln in links:
            rich: list[dict[str, Any]] = [{"type": "text", "text": {"content": "📥 "}}]
            name_rt: dict[str, Any] = {"content": ln["name"][:200]}
            if _is_linkable(ln.get("url") or ""):
                name_rt["link"] = {"url": ln["url"]}
            rich.append({"type": "text", "text": name_rt})
            tail = []
            if ln.get("size"):
                tail.append(format_mb(ln["size"]))
            if ln.get("reason") == "too_big":
                tail.append(f"노션 무료 요금제 한도({cap_label(self.max_image_bytes)}) 초과")
            else:
                tail.append("노션 업로드 실패")
            rich.append({
                "type": "text",
                "text": {"content": " · " + " · ".join(tail)},
                "annotations": {"color": "gray"},
            })
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": rich},
            })
        return blocks

    def _attachment_blocks(
        self,
        image_ids: list[str],
        video_ids: list[str],
        file_ids: list[tuple[str, str]],
        links: list[dict[str, Any]] | None = None,
        texts: list[tuple[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        """사진 / 동영상 / 첨부 파일 sections. A section also appears when all
        its files stayed on kidsnote, so the links have a place to live.
        Readable documents get a collapsed toggle with their text."""
        links = links or []
        texts = texts or []
        video_links = [ln for ln in links if ln["kind"] == "video"]
        file_links = [ln for ln in links if ln["kind"] == "file"]

        def heading(text: str) -> dict[str, Any]:
            return {
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": [{"type": "text", "text": {"content": text}}]},
            }

        blocks: list[dict[str, Any]] = []
        if image_ids:
            blocks.append(heading("사진"))
            for fid in image_ids:
                blocks.append({
                    "object": "block",
                    "type": "image",
                    "image": {"type": "file_upload", "file_upload": {"id": fid}},
                })
        if video_ids or video_links:
            blocks.append(heading("동영상"))
            for fid in video_ids:
                blocks.append({
                    "object": "block",
                    "type": "video",
                    "video": {"type": "file_upload", "file_upload": {"id": fid}},
                })
            blocks.extend(self._media_link_blocks(video_links))
        if file_ids or file_links or texts:
            blocks.append(heading("첨부 파일"))
            for fid, fname in file_ids:
                blocks.append({
                    "object": "block",
                    "type": "file",
                    "file": {
                        "type": "file_upload",
                        "file_upload": {"id": fid},
                        "name": fname[:100],
                    },
                })
            blocks.extend(self._media_link_blocks(file_links, how_to=not video_links))
            budget = DOC_TEXT_PAGE_CHARS
            for name, text in texts:
                if budget <= 0:
                    break
                blocks.append(self._doc_text_toggle(name, text, budget))
                budget -= len(text)
        return blocks

    def _doc_text_toggle(self, name: str, text: str, budget: int) -> dict[str, Any]:
        """Collapsed '📄 <file> 글 내용' toggle holding a document's text.

        ``budget`` caps the characters so a page stays well inside Notion's
        request size limit; cut text ends with a pointer to the original.
        """
        if len(text) > budget:
            text = text[:budget].rstrip() + "\n… (이후 내용은 원본 파일에서 확인하세요)"
        children: list[dict[str, Any]] = [self._para(
            "첨부 파일의 글자만 옮겼어요. 표 모양·그림은 원본 파일에서 확인하세요.", color="gray",
        )]
        chunk = ""
        for line in text.split("\n"):
            while len(line) > MAX_BLOCK_TEXT:
                if chunk:
                    children.append(self._para(chunk))
                    chunk = ""
                children.append(self._para(line[:MAX_BLOCK_TEXT]))
                line = line[MAX_BLOCK_TEXT:]
            if chunk and len(chunk) + 1 + len(line) > MAX_BLOCK_TEXT:
                children.append(self._para(chunk))
                chunk = line
            else:
                chunk = f"{chunk}\n{line}" if chunk else line
        if chunk:
            children.append(self._para(chunk))
        return {
            "object": "block",
            "type": "toggle",
            "toggle": {
                "rich_text": [{"type": "text", "text": {"content": f"📄 {name[:150]} — 글 내용"}}],
                "children": children[:NOTION_PAGE_CHILDREN_CAP],
            },
        }

    def _create_page(
        self,
        properties: dict[str, Any],
        children: list[dict[str, Any]],
        *,
        all_or_nothing: bool = True,
    ) -> dict[str, Any]:
        """POST /pages with up to 100 children, then append the rest in
        100-block chunks (Notion rejects a create with more than 100).
        Raises when the page itself can't be created.

        ``all_or_nothing`` (report / notice / album / menu pages): if an
        append fails, the half-built page is archived and the error raised,
        because dedup skips any id already in the DB, so a partial page would
        never be completed. Dashboards are rebuilt every run and pass False
        to keep whatever landed."""
        r = self.session.post(
            f"{NOTION_API}/pages",
            headers=self._headers(),
            json={
                "parent": {"database_id": self.database_id},
                "properties": properties,
                "children": children[:NOTION_PAGE_CHILDREN_CAP],
            },
            timeout=self.timeout,
        )
        r.raise_for_status()
        page = r.json()
        rest = children[NOTION_PAGE_CHILDREN_CAP:]
        page_id = page.get("id")
        for i in range(0, len(rest), NOTION_PAGE_CHILDREN_CAP):
            if not page_id:
                break
            chunk = rest[i:i + NOTION_PAGE_CHILDREN_CAP]
            try:
                r2 = self.session.patch(
                    f"{NOTION_API}/blocks/{page_id}/children",
                    headers=self._headers(),
                    json={"children": chunk},
                    timeout=self.timeout,
                )
                r2.raise_for_status()
            except Exception as e:
                _LOGGER.warning(
                    "page %s: appending blocks %d-%d failed: %s",
                    page_id, NOTION_PAGE_CHILDREN_CAP + i,
                    NOTION_PAGE_CHILDREN_CAP + i + len(chunk), _safe_err(e),
                )
                if all_or_nothing:
                    self._archive_page(page_id)
                    raise
                break
        return page

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

    @staticmethod
    def _is_english_study_body(text: str) -> bool:
        """Heuristic: detect alimnotas dominated by English vocabulary
        practice (academy posts: "Today we learned about exercise.
        The giraffe has a fur..."). In those bodies a child-voice diary
        ("아 오늘은 ...") and a parent-voice letter ("유주야, 오늘...")
        read awkwardly because the source itself is a teacher's lesson
        log, not a daily activity narrative.

        Returns True when ASCII letters make up at least 25% of the
        non-whitespace characters AND the absolute count is non-trivial
        (≥40 letters). Conservative — Korean alimnotas with one or two
        English keywords don't trip it.
        """
        if not text:
            return False
        non_ws = [ch for ch in text if not ch.isspace()]
        if len(non_ws) < 60:
            return False
        ascii_letters = sum(1 for ch in non_ws if "a" <= ch <= "z" or "A" <= ch <= "Z")
        if ascii_letters < 40:
            return False
        return ascii_letters / len(non_ws) >= 0.25

    @staticmethod
    def _ai_toggle(
        title: str,
        *,
        content: str,
        emoji: str,
        color: str,
    ) -> dict[str, Any]:
        """Wrap an AI-generated callout in a collapsed-by-default toggle.

        Notion ``toggle`` blocks render the children area hidden until the
        user clicks ▶, which is exactly the "default-hide, expand-to-see"
        UX requested for the AI gloss. Toggle title carries the emoji +
        "AI 가공 — 펼치기" label so readers see at a glance what category
        of AI content sits inside without expanding. The inner callout
        keeps its original color/emoji for consistency with prior versions.
        """
        return {
            "object": "block",
            "type": "toggle",
            "toggle": {
                "rich_text": [{"type": "text", "text": {"content": title}}],
                "children": [{
                    "object": "block",
                    "type": "callout",
                    "callout": {
                        "rich_text": [{"type": "text", "text": {"content": content}}],
                        "icon": {"type": "emoji", "emoji": emoji},
                        "color": color,
                    },
                }],
            },
        }

    @staticmethod
    def _meta_bits(report: dict[str, Any]) -> list[str]:
        """Author · class · date parts of the gray meta line (life-record chips are appended later)."""
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
        return meta_bits

    def _build_children(
        self,
        report: dict[str, Any],
        image_upload_ids: list[str],
        video_upload_ids: list[str],
        file_upload_ids: list[tuple[str, str]],  # list of (id, filename)
        media_links: list[dict[str, Any]] | None = None,
        media_texts: list[tuple[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []

        # Metadata header (gray, single line) — author role depends on author.type
        meta_bits = self._meta_bits(report)
        if meta_bits:
            blocks.append(self._para(" · ".join(meta_bits), color="gray"))

        # LLM-driven callouts (only when Ollama available). Skipped gracefully
        # when no LLM is reachable so non-LLM users still get a clean page.
        # Also skipped wholesale when the operator opted out via --no-llm /
        # DISABLE_ALL_LLM (plain backup + stats mode).
        #
        # Each AI callout is wrapped in a collapsed-by-default toggle so the
        # page opens to "original alimnota first, AI on demand". Notion
        # `toggle` blocks render the children area collapsed until the user
        # clicks ▶, which exactly matches the UX requested 2026-05-21:
        # default-hide, expand-to-see. Operators who don't trust the AI
        # output (졸업생 백업, 사생활 보수적, etc.) can simply never expand
        # them; operators who like the AI gloss expand them.
        body_for_summary = (report.get("content") or "").strip()
        cname = report.get("child_name") or ""
        if body_for_summary and not self.disable_llm_callouts:
            # All three AI sections always get a toggle so the page layout is
            # visually consistent. When the LLM returns empty (Chinese leak
            # filtered out, model refused, response too short, etc.) we still
            # render the toggle but with a short "AI 생성 실패" placeholder
            # inside, instead of silently dropping that section. Users
            # previously reported confusion when one page had 3 toggles and
            # another only 2 with no explanation (2026-05-21 feedback).
            _FALLBACK_TEXT = (
                "AI가 이 알림장에서 한국어 응답을 생성하지 못했습니다. "
                "본문이 짧거나, LLM이 한국어 외 언어로 답해서 자동 필터에 걸린 경우입니다. "
                "다음 force-refresh에서 다시 시도됩니다."
            )
            # English-academy alimnotas (vocabulary practice, drill sheets,
            # etc.) read awkwardly as 1st-person diary / parent letter
            # because the SOURCE is a teacher's lesson log, not a daily
            # activity narrative. Detect via ASCII letter density and skip
            # those two callouts with a clear placeholder; summary still
            # runs because a Korean one-liner of "오늘 영어 수업에서 ...를
            # 배웠다" is useful and not awkward.
            is_english_study = self._is_english_study_body(body_for_summary)
            _ENGLISH_SKIP_TEXT = (
                "이 알림장은 영어 학습 내용 중심입니다. 1인칭 일기·부모 편지로 "
                "변환하는 것이 어색해 AI 가공을 건너뛰었습니다. 원문에서 "
                "선생님이 정리한 학습 내용을 직접 확인하세요."
            )

            oneliner = self._summary_oneliner(body_for_summary)
            blocks.append(self._ai_toggle(
                "💭 본문 요약 (AI 가공 — 펼치기)" if oneliner
                else "💭 본문 요약 (AI 생성 실패 — 펼쳐서 확인)",
                content=oneliner or _FALLBACK_TEXT,
                emoji="💭", color="purple_background",
            ))
            # Child first-person diary
            if is_english_study:
                blocks.append(self._ai_toggle(
                    "🧒 자녀의 일기 (영어 학습 알림장 — AI 가공 생략)",
                    content=_ENGLISH_SKIP_TEXT,
                    emoji="🧒", color="yellow_background",
                ))
            else:
                child_diary = self._child_voice_diary(body_for_summary, cname)
                blocks.append(self._ai_toggle(
                    "🧒 자녀의 일기 (AI 가공 — 펼치기)" if child_diary
                    else "🧒 자녀의 일기 (AI 생성 실패 — 펼쳐서 확인)",
                    content=child_diary or _FALLBACK_TEXT,
                    emoji="🧒", color="yellow_background",
                ))
            # Parent diary (imagined; works whether the report itself was
            # parent- or teacher-written — kidsnote shows the alimnota to
            # the family either way).
            if is_english_study:
                blocks.append(self._ai_toggle(
                    "👨‍👩‍👧 부모의 편지 (영어 학습 알림장 — AI 가공 생략)",
                    content=_ENGLISH_SKIP_TEXT,
                    emoji="👨‍👩‍👧", color="pink_background",
                ))
            else:
                parent_diary = self._parent_voice_diary(body_for_summary, cname)
                blocks.append(self._ai_toggle(
                    "👨‍👩‍👧 부모의 편지 (AI 가공 — 펼치기)" if parent_diary
                    else "👨‍👩‍👧 부모의 편지 (AI 생성 실패 — 펼쳐서 확인)",
                    content=parent_diary or _FALLBACK_TEXT,
                    emoji="👨‍👩‍👧", color="pink_background",
                ))

        # Weather callout — only for teacher/admin posts (kidsnote auto-fills
        # weather on parent posts too, which would be misleading) and only
        # when the daycare actually filled in the weather field.
        # No body-text inference (per design: ``있는 그대로``).
        _atype = (report.get("author") or {}).get("type") or ""
        w_code = report.get("weather") if _atype != "parent" else None
        if w_code and w_code not in WEATHER_HIDDEN:
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

        # 사진 / 동영상 / 첨부 파일 (+ links to files left on kidsnote)
        blocks.extend(self._attachment_blocks(
            image_upload_ids, video_upload_ids, file_upload_ids, media_links, media_texts,
        ))

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
                   "의", "에", "와", "과", "로", "랑", "야", "여", "께",
                   # ``때`` is technically a noun but reads like a temporal
                   # particle in alimnota text ("그 때", "돌잔치때") and
                   # stripping it gives a much cleaner keyword.
                   "때")

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
        # Casual sentence endings (often appear in parent posts)
        "라구요", "더라구요", "거든요", "는걸요", "는데요", "답니당",
        "입니당", "당", "거든", "는걸",
        # Adverb-forming endings ("빠르게/신나게/조용하게")
        "게",
        # Common adj-as-modifier endings ("즐거운/예쁜/사랑스러운")
        "스러운", "다운", "러운",
        # Multi-syllable verb-end forms that the legacy heuristic let through
        # ("배운다, 시작한다, 풀어본다, 명령할" — surfaced in 2024 academy
        # alimnotas where the title fell back to heuristic keywording).
        # These are conservative (≥2 chars) so they don't snip real nouns.
        "한다", "은다", "는다", "본다", "였다", "었다", "했다", "겠다",
        "한다요", "는다요",
        "할", "을", "은", "려는",
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
        # Repetitive/temporal adverbs
        "하루하루", "조금씩", "차차", "점점", "갈수록", "이따금",
        # Common verb stems that survive ``는`` strip into 3-char form
        "달라지", "변하", "자라", "커가", "성장하",
        # Adjective-as-relative-clause ("싫은지/좋은지/어떤지")
        "싫은지", "좋은지", "어떤지",
        # Sound effects sometimes captured as 2-char tokens
        "휙휙",
        # Passive/past participles that look like nouns but aren't:
        "펼쳐진", "늘어진", "이루어진", "쥐어진", "기울어진",
        # Common verb stems that survive particle strip
        "했지", "되었지", "보았지", "갔지",
        # Academy-alimnota noise: vague abstract nouns / adverbs that
        # surfaced as top heuristic keywords in 2024 어학원 posts and
        # made titles like "주요, 시작한다, 명령문, 명령할, 이야기".
        "주요", "중요", "내용", "부분", "이야기",
        "동시", "표현", "단어", "문장", "문장들",
        "기본", "일반", "보통", "전체", "각자",
        "시작", "마지막", "이전", "이후", "다음",
        # Imperative/conditional verb forms that snuck through
        "배우기", "익히기", "공부하기", "연습하기",
        "시작한다", "배운다", "공부한다", "이해한다", "사용한다",
        "사용해", "사용하", "사용",
        "풀어본다", "맞춰본다", "해본다", "찾아본다",
        "말한다", "말하기", "말해",
        "묻고", "묻기", "묻는", "물어",
    })

    # 1-character keyword stopwords (filler / adverbs / determiners that
    # would otherwise survive the particle-strip stage when a 2-char
    # word like ``잘은`` → ``잘`` slips through).
    _ONECHAR_STOPWORDS = frozenset({
        "잘", "안", "또", "더", "꼭", "참", "그", "이", "저", "거", "것",
        "수", "들", "수", "곳", "데", "쪽", "분", "내", "네", "왜", "뭐",
        "다", "한", "두", "세", "넷", "막", "쭉", "푹", "쏙",
    })

    @classmethod
    def _extract_event_facts(cls, text: str, max_facts: int = 8) -> list[str]:
        """Return a short list of concrete event-noun phrases that actually
        appear in ``text``.

        Used by the per-alimnota AI callouts to anchor the LLM: prompts
        receive both the full body AND this explicit list, with
        instructions to only use these facts. Without an anchor the
        model invented details (e.g. shipping "린도라" — a non-existent
        animal — into a child-voice diary). The anchor list narrows
        the model's "what's in this alimnota" window.

        Implementation reuses the existing kiwi/heuristic keyword
        extractors (same NNG/NNP-only logic that drives report titles),
        but caps to ``max_facts`` items and returns them as a list
        instead of a comma-joined string.
        """
        if not text or len(text.strip()) < 30:
            return []
        kiwi = _get_kiwi()
        # Reuse the same noun-extraction pipeline as the title keyword
        # extractor, but split back into a list. Both kiwi and heuristic
        # paths emit comma-joined output, so we just split it.
        if kiwi is not None:
            joined = cls._summarize_text_kiwi(kiwi, text, max_chars=400)
        else:
            joined = cls._summarize_text_heuristic(text, max_chars=400)
        if not joined:
            return []
        parts = [p.strip() for p in joined.split(",") if p.strip()]
        return parts[:max_facts]

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
        """Title-line **keyword** extraction. kiwipiepy → heuristic fallback.
        (Ollama is reserved for the longer-form summary callout — see
        ``_summary_oneliner``.)
        """
        if not text:
            return ""
        kiwi = _get_kiwi()
        if kiwi is not None:
            return cls._summarize_text_kiwi(kiwi, text, max_chars)
        return cls._summarize_text_heuristic(text, max_chars)

    @classmethod
    def _ask_ollama(
        cls,
        prompt: str,
        *,
        max_chars: int = 400,
        temperature: float = 0.3,
        num_predict: int = 200,
        timeout: int = 120,
        final_labels: tuple[str, ...] = (),
        strip_meta: bool = True,
        input_text: str = "",
    ) -> str | None:
        """Generic Ollama text-generation call. Returns None when Ollama
        isn't reachable or the response is empty / garbage. Output is
        stripped to a single contiguous block and capped at ``max_chars``.

        ``input_text`` (optional): the source alimnota body. When supplied,
        we extra-reject outputs that literally copy the input (a parent
        self-introduction once leaked their full name + apartment number
        into the child-diary callout because llama3.1:8b gave up
        converting and just echoed the first sentence).
        """
        cfg = _get_ollama()
        if cfg is None:
            return None
        # Retry once with a slightly higher temperature on empty / cjk-leak
        # / too-short response. llama3.1:8b on CPU sometimes produces a
        # short or English-leaning output for parent-written Korean
        # alimnotas on the first attempt; the retry catches most of those.
        log = logging.getLogger(__name__)
        last_reason = "unknown"
        for attempt, temp in enumerate((temperature, min(1.0, temperature + 0.25)), start=1):
            try:
                r = requests.post(
                    f"{cfg['host']}/api/generate",
                    json={
                        "model": cfg["model"],
                        "prompt": prompt,
                        "stream": False,
                        "options": {
                            "temperature": temp,
                            "num_predict": num_predict,
                        },
                    },
                    timeout=timeout,
                )
                r.raise_for_status()
                out = (r.json().get("response") or "").strip()
            except Exception as e:
                last_reason = f"timeout_or_http({type(e).__name__})"
                log.warning("ollama call attempt %d failed: %s", attempt, e)
                continue
            if not out:
                last_reason = "empty_response"
                continue
            # Drop wrapping ``"`` and leading dashes/asterisks
            out = out.strip().strip('"').strip("'").lstrip("- ").lstrip("* ").strip()
            # If the model produced analysis + a clean final block prefixed
            # with a section label, keep only what's after the last label.
            if final_labels:
                out = _extract_after_final_label(out, final_labels)
            # Drop task-restatement lead-ins ("...써봅니다.").
            if strip_meta:
                out = _strip_lead_meta(out)
            # Strip any Chinese hanja the model leaked
            original_len = len(out)
            out, cjk_removed = _strip_cjk(out)
            if cjk_removed and original_len > 0:
                ratio = cjk_removed / original_len
                if ratio > 0.20:
                    last_reason = f"cjk_leak({cjk_removed}/{original_len})"
                    continue
                # Collapse the multi-space gaps left behind.
                out = " ".join(out.split())
            if len(out) < 5:
                last_reason = f"too_short({len(out)})"
                continue
            # English word leak guard: catches mid-Korean tokens like
            # "전화해 came." (production 2026-05-22). Allow up to one
            # ≤4-char alphabetic run (covers AM/PM/cm-style abbreviations
            # and very short proper-noun acronyms). Reject anything with
            # an alphabetic word ≥5 chars OR ≥2 alphabetic words.
            eng_words, eng_longest = _english_word_leak_ratio(out)
            if eng_longest >= 4 or eng_words >= 2:
                last_reason = f"english_leak(words={eng_words},longest={eng_longest})"
                continue
            # Verbatim-input copy guard: if the output starts with text
            # that appears as a contiguous substring in the source body,
            # the model gave up converting and just echoed input. That
            # has leaked parent names / addresses into the child-diary
            # callout (production 2026-05-22).
            if input_text and _looks_like_input_copy(out, input_text):
                last_reason = "input_copy"
                continue
            break  # success
        else:
            log.warning("ollama giving up after retries (reason=%s)", last_reason)
            return None
        # Truncate cleanly at sentence boundary so the last sentence isn't
        # mid-cut. Previously a parent-letter callout shipped truncated as
        # "...아무래도 네가 이 편" (literal "the letter" mid-word). Now
        # the cap finds the last terminator within max_chars and ends there.
        if len(out) > max_chars:
            out = out[:max_chars]
            terminators = [".", "!", "?", "다.", "요.", "야.", "지.",
                           "니다", "어요", "해요", "야!", "지!"]
            best = -1
            for t in terminators:
                idx = out.rfind(t)
                if idx > best:
                    # If the terminator's text spans more than one char, push
                    # the cut to include the whole terminator.
                    best = idx + len(t) - 1
            # Require at least half the budget to be present (avoid an empty
            # output when no terminator was found).
            if best >= max_chars // 2:
                out = out[: best + 1]
        return out

    @classmethod
    def _child_voice_diary(cls, text: str, child_name: str = "") -> str | None:
        """Convert teacher's alimnota into child's first-person diary.

        Uses a one-shot example because instruction-only prompts caused
        weaker models (qwen2.5:7b) to copy the teacher's third-person
        narration verbatim instead of converting perspective.
        """
        if not text or len(text.strip()) < 30:
            return None
        given = _given_name(child_name) or "아이"
        topic = _topic_form(given)  # 하린→하린이 / 유주→유주
        # Anchor list: nouns actually present in the body. Forces the LLM
        # to draw from real events rather than inventing new ones — fixes
        # the "린도라 동물" / "공원에서 놀았어" hallucination class.
        facts = cls._extract_event_facts(text, max_facts=8)
        facts_block = (
            "\n[본문에서 확인된 사실 — 이 항목들만 사용]\n"
            + "\n".join(f"- {f}" for f in facts)
            + "\n"
        ) if facts else ""
        prompt = (
            f"다음 어린이집 알림장을 자녀({given})의 1인칭 일기로 "
            "한국어 2-3문장으로 바꿔쓰세요.\n\n"
            "**규칙 (반드시 지킬 것)**:\n"
            "① ``나``가 주어. 선생님 호칭(``어머니~``, ``선생님께`` 등) 금지.\n"
            "② **환각 절대 금지**: 알림장 본문에 등장하지 않은 사건·활동·"
            "동물·사람·장소·물건 이름을 만들어내지 마세요. 아래 "
            "``[본문에서 확인된 사실]`` 목록에 등장하는 사실만 1인칭으로 "
            "재구성하세요. 그 외의 사물·사건을 추가하면 안 됩니다.\n"
            "③ 본문이 영어 단어(``swimming``, ``exercise`` 등)를 포함해도 "
            "일기는 한국어로 의역하세요. 단, 본문에 그대로 적힌 게임/노래/"
            "교재 이름은 따옴표로 인용 가능합니다.\n"
            "④ 메타 설명(``아래와 같이 변환해 드릴게요`` 등) 절대 금지 — "
            "바로 일기 본문만 답하세요.\n"
            "⑤ 부모가 쓴 글(예: ``하린이는 어제 잘 놀았어요``)도 똑같이 "
            "어린이 1인칭(``어제 잘 놀았어``)으로 변환.\n\n"
            "[예시 1 — 선생님 작성]\n"
            f"알림장: {topic}는 친구에게 장난감을 건네주며 사회성이 "
            "자라는 모습이었습니다.\n"
            "일기: 오늘 친구한테 내 장난감을 줬어. 친구가 좋아하니까 "
            "나도 기분이 좋았어!\n\n"
            "[예시 2 — 부모 작성]\n"
            f"알림장: {topic}는 어제 잘 놀고 잘 먹었습니다. 콧물이 좀 났어요.\n"
            "일기: 어제 잘 놀고 밥도 잘 먹었어! 콧물이 좀 나서 답답했지만 괜찮아.\n\n"
            f"{facts_block}"
            "[지금 변환할 알림장]\n"
            f"알림장: {text[:1200]}\n"
            "일기:"
        )
        # 2026-05-25: num_predict halved 130 → 70. 3-sentence diary fits
        # comfortably in 70 tokens (~140-180 Korean chars) and the
        # halved generation time roughly doubles publish throughput.
        return cls._ask_ollama(
            prompt, max_chars=350, num_predict=70,
            final_labels=("일기:",),
            input_text=text,
        )

    @classmethod
    def _parent_voice_diary(cls, text: str, child_name: str = "") -> str | None:
        """Parent-to-child love letter inspired by today's alimnota.

        Written so the child, years later, will feel moved when reading it
        back. Uses a one-shot example because abstract instructions like
        "느껴지게" leaked verbatim into earlier model outputs.
        """
        if not text or len(text.strip()) < 30:
            return None
        addressee = _addressee(child_name)
        given = _given_name(child_name) or "아이"
        topic = _topic_form(given)  # 하린→하린이 / 유주→유주
        # Same anchor strategy as _child_voice_diary.
        facts = cls._extract_event_facts(text, max_facts=8)
        facts_block = (
            "\n[본문에서 확인된 사실 — 이 항목들만 인용]\n"
            + "\n".join(f"- {f}" for f in facts)
            + "\n"
        ) if facts else ""
        prompt = (
            f"부모가 자녀({given})에게 쓰는 짧은 편지. 알림장에 나온 "
            "그날의 실제 사건 1-2개를 구체적으로 언급하면서, 자녀가 자라서 "
            "이 편지를 봤을 때 부모의 사랑이 전해지게 써. 2-3문장, 한국어.\n\n"
            "**규칙 (반드시 지킬 것)**:\n"
            "① **시점**: 부모(엄마/아빠)가 자녀(``너``)에게 쓰는 글. "
            f"자녀가 부모에게 ``엄마 사랑해`` 같은 표현을 하지 마세요 — "
            "이건 부모가 자녀에게 쓰는 편지입니다.\n"
            "② **환각 금지**: 아래 ``[본문에서 확인된 사실]`` 목록에 "
            "있는 사건만 인용. 그 외의 활동·사물·장소를 만들어내면 안 됩니다.\n"
            "③ **한국어만**: 본문에 영어 단어가 있어도 편지는 한국어로 의역. "
            "교재 이름 같은 고유명사는 따옴표로 인용 가능.\n"
            "④ **마무리**: 마지막 문장이 자연스럽게 끝나도록 (마침표·물음표·"
            "느낌표 중 하나로). 잘리지 않게.\n\n"
            "[예시]\n"
            f"알림장: {topic}는 친구에게 장난감을 건네주며 사회성이 "
            "자라는 모습을 보였습니다. ``동물농장`` 노래에 박수를 쳤어요.\n"
            f"편지: {addressee}, 오늘 친구에게 장난감을 양보했다는 얘기를 "
            "들었어. 엄마는 네가 친구를 아끼는 마음이 자라는 모습이 "
            f"참 자랑스러웠단다. ``동물농장`` 노래에 짝짝 박수치는 너의 "
            "모습이 눈에 선해.\n\n"
            "[지금 작성할 편지]\n"
            f"알림장: {text[:1200]}\n"
            "편지:"
        )
        # 2026-05-25: num_predict halved 240 → 120 for the same reason as
        # the child diary — 2-3 sentence parent letter fits in 120 tokens.
        return cls._ask_ollama(
            prompt, max_chars=600, num_predict=120,
            final_labels=("편지:",),
            input_text=text,
        )

    @classmethod
    def _summary_oneliner(cls, text: str) -> str | None:
        """One-sentence body summary using Ollama (LLM). Returns None when
        no Ollama server is reachable (caller should just skip the summary
        callout in that case).

        Prompt is content-agnostic (어린이집 / 유치원 / 어학원 모두 커버)
        and explicitly bars Chinese characters — earlier prompt that said
        "어린이집 알림장" tripped the model into Chinese-leaning output
        when the actual content was an English-academy class report
        ("운동好处讨论..."). CJK leak still gets filtered downstream as a
        backstop, but addressing it at the prompt level is cleaner.
        """
        if not text or len(text.strip()) < 20:
            return None
        cfg = _get_ollama()
        if cfg is None:
            return None
        prompt = (
            "다음 키즈노트 게시글 본문을 한국어 한 문장(40자 이내)으로 "
            "요약해. 활동·내용·중요 정보만 자연스럽게.\n\n"
            "**규칙**:\n"
            "① 반드시 한국어만 사용 — 중국어 한자(汉字) 절대 금지\n"
            "② 본문에 영어 단어(``exercise``, ``hiking`` 등)가 있어도 "
            "요약은 한국어로 의역. 단어를 그대로 끼워넣지 말 것.\n"
            "③ 문장은 마침표로 끝나야 하며 단어 단독으로 끝나거나 "
            "쉼표 뒤에 영어 단어가 붙는 형태 금지 (예: ``운동, exercise``).\n"
            "④ 다른 설명·서두 없이 요약 한 줄만 답해\n\n"
            "[예시 — 영어 단어가 본문에 있는 경우]\n"
            "본문: Today we learned about ``exercise``. Walking is "
            "good for our heart. 친구들과 운동의 장점에 대해 얘기했어요.\n"
            "요약: 운동의 장점과 걷기의 효능을 친구들과 함께 배웠다.\n\n"
            f"[지금 요약할 본문]\n본문: {text[:1500]}\n\n요약:"
        )
        # Retry once on empty / cjk-leak / short response. CPU Ollama
        # output for parent-written Korean alimnotas is occasionally short
        # or English-leaning on cold-start; a second attempt with slightly
        # different temperature catches most of those without bloating
        # backup time too much.
        log = logging.getLogger(__name__)
        last_reason = "unknown"
        for attempt, temp in enumerate((0.3, 0.55), start=1):
            try:
                r = requests.post(
                    f"{cfg['host']}/api/generate",
                    json={
                        "model": cfg["model"],
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": temp, "num_predict": 80},
                    },
                    timeout=120,
                )
                r.raise_for_status()
                out = (r.json().get("response") or "").strip()
            except Exception as e:
                last_reason = f"timeout_or_http({type(e).__name__})"
                log.warning("summary attempt %d failed: %s", attempt, e)
                continue
            if not out:
                last_reason = "empty_response"
                continue
            # Drop anything after the first line / colon-style prefix.
            first = out.split("\n")[0].strip().lstrip("- ").lstrip("* ")
            # Guard against junk responses
            if len(first) < 5 or len(first) > 200:
                last_reason = f"length_oob({len(first)})"
                continue
            # Chinese-leak filter
            original_len = len(first)
            first, cjk_removed = _strip_cjk(first)
            if cjk_removed and original_len > 0:
                if cjk_removed / original_len > 0.20:
                    last_reason = f"cjk_leak({cjk_removed}/{original_len})"
                    continue
                first = " ".join(first.split())  # collapse gaps
                if len(first) < 5:
                    last_reason = "post_cjk_too_short"
                    continue
            # English-leak filter — same as _ask_ollama
            eng_words, eng_longest = _english_word_leak_ratio(first)
            if eng_longest >= 4 or eng_words >= 2:
                last_reason = f"english_leak(w={eng_words},l={eng_longest})"
                continue
            # Verbatim-input copy guard
            if _looks_like_input_copy(first, text):
                last_reason = "input_copy"
                continue
            return first
        log.warning("summary giving up after retries (reason=%s)", last_reason)
        return None

    @classmethod
    def _summarize_text_kiwi(cls, kiwi: Any, text: str, max_chars: int) -> str:
        """Kiwi-based keyword extraction: nouns only, frequency + length
        weighted, substring-deduped."""
        from collections import Counter
        try:
            result = kiwi.analyze(text)
        except Exception:
            return cls._summarize_text_heuristic(text, max_chars)
        if not result:
            return ""
        tokens = result[0][0]
        # NNG = common noun, NNP = proper noun (names, products, places)
        nouns: list[str] = []
        for tok in tokens:
            tag = getattr(tok, "tag", "")
            form = getattr(tok, "form", "")
            if not form:
                continue
            if tag not in ("NNG", "NNP"):
                continue
            # Length: 2-6 chars (single Korean syllable nouns are rarely
            # meaningful in this context; 7+ is usually compound junk)
            if not (2 <= len(form) <= 6):
                continue
            # Skip generic stopwords (greetings, address terms, calendar,
            # pronouns we explicitly don't want)
            if form in cls._KEYWORD_STOPWORDS:
                continue
            nouns.append(form)

        if not nouns:
            return ""

        counter = Counter(nouns)

        def _score(word: str, freq: int) -> float:
            n = len(word)
            length_bonus = 1.0
            if n >= 5:
                length_bonus = 1.8
            elif n == 4:
                length_bonus = 1.5
            elif n == 3:
                length_bonus = 1.2
            return freq * length_bonus

        ranked = sorted(counter.keys(), key=lambda w: (-_score(w, counter[w]), w))
        kept: list[str] = []
        for w in ranked:
            if any((w in k or k in w) for k in kept):
                continue
            kept.append(w)
            if len(kept) >= 5:
                break
        return ", ".join(kept)[:max_chars]

    @classmethod
    def _summarize_text_heuristic(cls, text: str, max_chars: int = 80) -> str:
        """Legacy fallback used when kiwipiepy isn't installed.

        Strategy (no LLM):
        1. Extract Korean letter runs from the body.
        2. Strip trailing particles (``을/를/이/가/에서/으로``).
        3. Drop greetings / filler / verbal endings (best-effort).
        4. Frequency + length weighted, substring dedup, top 5.
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

        # Score = frequency × length_bonus. Longer Korean tokens (4-5
        # chars) are far more likely to be content nouns (카네이션,
        # 머리띠, 돌잔치 etc) than the inflected 2-3 char forms (친한,
        # 저번, 이제), so we tilt ranking toward them while still
        # respecting repeats.
        def _score(word: str, freq: int) -> float:
            n = len(word)
            length_bonus = 1.0
            if n >= 5:
                length_bonus = 1.8
            elif n == 4:
                length_bonus = 1.5
            elif n == 3:
                length_bonus = 1.2
            return freq * length_bonus

        ranked = sorted(
            counter.keys(),
            key=lambda w: (-_score(w, counter[w]), w),
        )

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

        def to_ko(value: str | bool | None) -> str | None:
            if isinstance(value, bool):  # outdoor_activity_status may be a JSON boolean
                value = "true" if value else "false"
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
        # Numeric temperature if present (some kidsnote setups record actual °C).
        # Schema variants observed in the wild:
        #   - float / str:  37.1
        #   - dict:         {"body_temp_am": "36.8", "body_temp_pm": "36.6"}
        # Older code interpolated dict directly via f-string and produced
        # garbage like ``🌡️ {'body_temp_am': '36.8', 'body_temp_pm': '36.6'}°C``.
        temp = report.get("temperature")
        if isinstance(temp, dict):
            am = (temp.get("body_temp_am") or temp.get("am") or "").strip() if isinstance(temp.get("body_temp_am") or temp.get("am"), str) else (temp.get("body_temp_am") or temp.get("am"))
            pm = (temp.get("body_temp_pm") or temp.get("pm") or "").strip() if isinstance(temp.get("body_temp_pm") or temp.get("pm"), str) else (temp.get("body_temp_pm") or temp.get("pm"))
            if am and pm:
                bits.append(f"🌡️ 오전 {am}°C / 오후 {pm}°C")
            elif am:
                bits.append(f"🌡️ 오전 {am}°C")
            elif pm:
                bits.append(f"🌡️ 오후 {pm}°C")
        elif temp not in (None, "", 0):
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
            bits.append(f"⭐ 활동 {ACTIVITY_RATE_KO.get(str(ar), ar)}")

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
                _LOGGER.warning("menu photo download failed (%s): %s", _safe_url(url), _safe_err(e))
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

    def _comment_blocks(
        self,
        comments: list[dict[str, Any]],
        *,
        scope: str = "report",
    ) -> list[dict[str, Any]]:
        """Render a list of comments as a Notion heading + paragraphs.

        Each comment becomes:
          - one bold gray line:  👩‍🏫 작성자 · 2026-05-12
          - body text (chunked if long)

        author.type=='teacher' → 👩‍🏫,  parent → 👨‍👩‍👧.

        ``scope``: ``"report"`` / ``"album"`` (default: child-private, all
        comments rendered) vs ``"notice"`` (center-wide, parent comments
        come from OTHER families and may contain other children's names
        / schedules → those get filtered out to avoid leaking strangers'
        personal data into this operator's Notion DB). Discovered
        2026-05-22 reviewing the iibii_22 test backup: a notice's
        comments listed 10 other students' names with scheduling info.
        """
        if not comments:
            return []
        # Privacy filter — only applied on center-wide notices.
        skipped_parents = 0
        if scope == "notice":
            kept: list[dict[str, Any]] = []
            for c in comments:
                atype = ((c.get("author") or {}).get("type") or "").lower()
                if atype == "parent":
                    skipped_parents += 1
                    continue
                kept.append(c)
            comments = kept
        if not comments and not skipped_parents:
            return []
        header_text = f"💬 댓글 ({len(comments)})"
        if skipped_parents:
            header_text += f"  · 다른 학부모 댓글 {skipped_parents}건은 개인정보 보호 위해 미수록"
        out: list[dict[str, Any]] = [{
            "object": "block",
            "type": "heading_3",
            "heading_3": {"rich_text": [{
                "type": "text",
                "text": {"content": header_text},
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

        # Weather: API field only. Parent-written entries get the daycare's
        # weather auto-attached by kidsnote (regardless of whether the
        # parent saw it), which is misleading — strip it for parent posts.
        w_code = report.get("weather") if author_type != "parent" else None
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
                # Common Korean particle / suffix patterns that hang off a
                # given name. Strip from the full ``cname`` and from the
                # short (last 2 chars) form, since teachers/parents often
                # use the short name (``하린이`` instead of ``우하린``).
                particle_suffixes = (
                    "이네용", "이네", "이가요", "이가",
                    "이는요", "이는", "이의",
                    "이도", "이만", "이를", "이에게",
                    "이요", "이",
                )
                variants: set[str] = {cname}
                for ps in particle_suffixes:
                    variants.add(cname + ps)
                if len(cname) >= 2:
                    short = cname[-2:]
                    variants.add(short)
                    for ps in particle_suffixes:
                        variants.add(short + ps)
                # Apply longer variants first so a leading shorter form
                # doesn't gobble up part of a longer suffix.
                for v in sorted(variants, key=len, reverse=True):
                    stripped = stripped.replace(v, "")
            summary = self._summarize_text(stripped)
            tail = summary or f"알림장 #{report_id}"

        prefix_emojis = author_icon + (f" {w_emoji}" if w_emoji else "")
        # `_2 ` prefix groups alimnotas in the name-desc sort order below
        # the dashboards (_5/_4) and albums (_3), above notices (_1).
        title = f"_2 [{date_str}] 알림장: {prefix_emojis} {tail}"

        # Upload photos / videos / files first so the page body can embed them.
        media = self._upload_media(report, kidsnote_sess)

        children = self._build_children(
            report, media["image_ids"], media["video_ids"], media["file_ids"],
            media["links"], media["texts"],
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
        # Reports are child-private (only THIS family + teachers participate),
        # so comments are rendered in full — no privacy filter needed here.
        if report.get("num_comments"):
            comments = self._fetch_comments(kidsnote_sess, "reports", report_id)
            children.extend(self._comment_blocks(comments, scope="report"))

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

        page = self._create_page(properties, children)
        return {
            "page_id": page.get("id", ""),
            "page_url": page.get("url", ""),
            "report_id": report_id,
            "title": title,
            **self._media_counts(media),
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
        # ---- Upload photos / videos / files ----
        media = self._upload_media(item, kidsnote_sess)

        # ---- Build body blocks ----
        blocks: list[dict[str, Any]] = []
        if meta_bits:
            blocks.append(self._para(" · ".join(meta_bits), color="gray"))
        body_text = (item.get("content") or "").strip()
        if body_text:
            for chunk in self._chunk(body_text):
                blocks.append(self._para(chunk))
        blocks.extend(self._attachment_blocks(
            media["image_ids"], media["video_ids"], media["file_ids"], media["links"],
            media["texts"],
        ))

        # ---- Append comments (parent + teacher replies) at the end ----
        # comment_kind is `"notices"` or `"albums"`. Notices are center-wide
        # so other parents' comments leak names/schedules/personal info →
        # _comment_blocks with scope="notice" filters those out. Albums are
        # child-specific so we treat them like reports (no filtering).
        if comment_kind and item.get("num_comments"):
            comments = self._fetch_comments(kidsnote_sess, comment_kind, item_id)
            comment_scope = "notice" if comment_kind == "notices" else "album"
            blocks.extend(self._comment_blocks(comments, scope=comment_scope))

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
        page = self._create_page(properties, blocks)
        return {
            "page_id": page.get("id", ""),
            "title": title,
            "item_id": item_id,
            **self._media_counts(media),
        }

    @staticmethod
    def _media_counts(media: dict[str, Any]) -> dict[str, int]:
        """Per-page attachment counters the run log prints."""
        return {
            "images_uploaded": len(media["image_ids"]),
            "images_failed": media["images_failed"],
            "videos_uploaded": len(media["video_ids"]),
            "videos_failed": media["videos_failed"],
            "files_uploaded": len(media["file_ids"]),
            "files_failed": media["files_failed"],
            "media_links": len(media["links"]),
            "doc_texts": len(media["texts"]),
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
        # `_1 ` prefix sorts notices to the bottom in name-desc order.
        title = f"_1 [{date_str}] 공지: {nt}" if nt else f"_1 [{date_str}] 공지 #{notice_id}"
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
        # `_3 ` prefix sorts albums between dashboards (_5/_4) and
        # alimnotas (_2) in name-desc order.
        title = f"_3 [{date_str}] 앨범: {at}" if at else f"_3 [{date_str}] 앨범 #{album_id}"
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
                        _LOGGER.warning("menu photo download failed (%s): %s", _safe_url(url), _safe_err(e))
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

        page = self._create_page(properties, blocks)
        return {
            "page_id": page.get("id", ""),
            "page_url": page.get("url", ""),
            "menu_id": menu_id,
            "title": title,
            "images_uploaded": images_uploaded,
            "images_failed": images_failed,
        }


    # ----------------------------------------------------------- stats dashboard

    # Pinned Report IDs for singleton system pages (kidsnote ids are
    # all positive 1e9+, so any negative number is safe).
    # Dashboard titles use ``{year}`` placeholder so the page title shows
    # the regeneration year ("_ 📊 2026년 통계 대시보드"). Helps the user
    # tell at-a-glance which year's snapshot they're looking at.
    #
    # ``_`` prefix: ASCII underscore (codepoint 95) sorts higher than the
    # alimnota / 공지 / 앨범 prefix ``[`` (codepoint 91). When the operator
    # sorts the Notion DB by 이름 descending, all 7 dashboards cluster at
    # the top, and the daily entries cascade below in reverse-chronological
    # order without dashboards interleaving. Requested 2026-05-21.
    # Titles are now year-agnostic per 2026-05-22 user feedback — each
    # dashboard already groups its content by year *inside* the page, so
    # putting a single year in the title was misleading (the stats
    # dashboard, growth story, interests, milestones, and the thank-you
    # card all span every year of data). The Today's-Memories page got
    # renamed entirely ("작년에 우리 아이는") because that phrasing reads
    # more like a family scrapbook header than a system label.
    DASHBOARD_REPORT_ID = -1
    # 2026-05-26: title prefix scheme for name-desc sort.
    # Default Notion sort is name-desc; with these prefixes the
    # categories cluster top-down in the requested order:
    #   _5 통계 dashboards (3)
    #   _4 AI dashboards   (4)
    #   _3 앨범
    #   _2 알림장
    #   _1 공지
    DASHBOARD_TITLE = "_5 📊 통계 대시보드"
    # MEMORIES (📅 작년추억) was removed 2026-05-27 — feature retired by
    # user request: most accounts didn't have multi-year backups yet so
    # the page was almost always empty, and the page only refreshed once
    # per cron cycle so it was rarely useful as a daily widget. Reserved
    # ID -2 stays burned to avoid clashing if anyone re-introduces it.
    NUTRITION_REPORT_ID = -3
    NUTRITION_TITLE = "_5 🥗 영양 분석"
    # LLM-driven storytelling pages (auto-skip when Ollama isn't reachable)
    GROWTH_STORY_REPORT_ID = -4
    GROWTH_STORY_TITLE = "_4 📖 매월 성장 스토리"
    MILESTONES_REPORT_ID = -5
    MILESTONES_TITLE = "_4 🌟 우리 아이의 처음들 (마일스톤)"
    INTERESTS_REPORT_ID = -6
    INTERESTS_TITLE = "_4 🌱 분기별 관심사"
    TEACHER_THANKS_REPORT_ID = -7
    TEACHER_THANKS_TITLE = "_4 💌 연도별 선생님께"
    # Rule-based (no LLM) 🎒 requests + 📅 upcoming dates, see supplies.py.
    SUPPLIES_REPORT_ID = -8
    SUPPLIES_TITLE = "_5 📋 준비물·일정"

    @classmethod
    def _dashboard_title(cls, template: str) -> str:
        """Resolve ``{year}`` placeholder in a dashboard title template
        using the current year. Centralised so every sentinel page
        formats consistently.
        """
        return template.format(year=datetime.now().year)

    def _find_singleton_page(self, sentinel_report_id: int) -> str | None:
        """Locate an existing system singleton page by its sentinel Report ID.

        If multiple unarchived pages share the sentinel (a known
        side-effect of an earlier publish cycle that didn't archive
        cleanly — e.g. race between two cron runs against the same
        DB), this also archives the older duplicates so only the
        latest one survives. Returns the most-recently-edited page.
        """
        self._resolve_schema()
        assert self._prop_report_id is not None
        try:
            r = self.session.post(
                f"{NOTION_API}/databases/{self.database_id}/query",
                headers=self._headers(),
                json={
                    "filter": {
                        "property": self._prop_report_id,
                        "number": {"equals": sentinel_report_id},
                    },
                    "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
                    "page_size": 10,
                },
                timeout=self.timeout,
            )
            r.raise_for_status()
            results = r.json().get("results") or []
            if not results:
                return None
            # First entry is the latest. Archive any older duplicates
            # to keep "singleton" semantics intact for the operator.
            latest = results[0]["id"]
            for duplicate in results[1:]:
                _LOGGER.warning(
                    "singleton rid=%d had %d duplicates — archiving %s",
                    sentinel_report_id, len(results) - 1, duplicate["id"],
                )
                self._archive_page(duplicate["id"])
            return latest
        except Exception as e:
            _LOGGER.warning("singleton lookup failed (rid=%d): %s", sentinel_report_id, e)
            return None

    def _find_dashboard_page(self) -> str | None:
        return self._find_singleton_page(self.DASHBOARD_REPORT_ID)

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
                "title": [{"text": {"content": self._dashboard_title(self.DASHBOARD_TITLE)}}],
            },
            self._prop_report_id: {"number": self.DASHBOARD_REPORT_ID},
        }
        if self._prop_date:
            properties[self._prop_date] = {
                "date": {"start": datetime.now().date().isoformat()},
            }
        # The 📥 list can run past Notion's 100-blocks-per-create limit.
        return self._create_page(properties, blocks, all_or_nothing=False)

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
        wd = {k: v for k, v in (stats.get("weather_dist") or {}).items() if k not in WEATHER_HIDDEN}
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
        oversized = att.get("oversized") or []
        cap = cap_label(att.get("cap") or self.max_image_bytes)
        if att:
            blocks.append(self._h2("📎 첨부물 누계 (키즈노트 기준)"))

            def over_note(kind: str) -> str:
                n = sum(1 for o in oversized if o.get("kind") == kind)
                return f"  ({cap}가 넘어 원본 링크로 남긴 {n:,} 개 포함)" if n else ""

            lines = [
                f"📷 사진 {att.get('images', 0):,} 장",
                f"🎬 동영상 {att.get('videos', 0):,} 개{over_note('video')}",
                f"📄 첨부파일 {att.get('files', 0):,} 개{over_note('file')}",
            ]
            for line in lines:
                blocks.append(self._para(line))
            if oversized:
                blocks.append(self._para(
                    "📥 노션에 못 올린 파일의 원본 링크는 이 페이지 맨 아래에 있어요.",
                    color="gray",
                ))

        # ---- 연도별 상세 (per-year breakdown) ----
        per_year = stats.get("per_year") or []
        if per_year:
            blocks.append(self._h2("📆 연도별 상세"))
            blocks.append(self._para(
                "각 연도의 알림장 수와 카테고리·월별 분포. 최근 연도부터 표시.",
                color="gray",
            ))
            for yb in per_year:  # already newest-first from caller
                yr = yb.get("year", "")
                blocks.append({
                    "object": "block",
                    "type": "heading_3",
                    "heading_3": {"rich_text": [{
                        "type": "text",
                        "text": {"content": f"📅 {yr}년 — 알림장 {yb.get('reports_total', 0)}개"},
                    }]},
                })
                # 카테고리 분포 (top 10)
                yc = yb.get("category_counts") or {}
                if yc:
                    top = sorted(yc.items(), key=lambda kv: kv[1], reverse=True)[:10]
                    cat_line = ", ".join(f"{k}({v})" for k, v in top)
                    blocks.append(self._para(f"🎨 카테고리: {cat_line}"))
                # 월별 알림장 수
                ym = yb.get("monthly_report_counts") or {}
                if ym:
                    ordered = sorted(ym.items())  # within a year, oldest month → newest month
                    monthly_line = " · ".join(f"{m[5:7]}월 {n}" for m, n in ordered)
                    blocks.append(self._para(f"📅 월별: {monthly_line}"))
                # 작성자 비율
                ya = yb.get("author_counts") or {}
                if ya:
                    author_line = ", ".join(
                        f"{ {'teacher':'선생님','parent':'부모','admin':'원감/원장'}.get(k, k) }({v})"
                        for k, v in ya.items()
                    )
                    blocks.append(self._para(f"✍️ 작성자: {author_line}"))
                # 낮잠 / 식사 / 날씨 — show only when present
                ysh = yb.get("sleep_hour_dist") or {}
                if ysh:
                    blocks.append(self._para(
                        "💤 낮잠: " + ", ".join(
                            f"{SLEEP_HOUR_KO.get(k, k)}({v})" for k, v in ysh.items()
                        )
                    ))
                yms = yb.get("meal_status_dist") or {}
                if yms:
                    blocks.append(self._para(
                        "🍽️ 식사: " + ", ".join(
                            f"{STATUS_KO.get(k, k)}({v})" for k, v in yms.items()
                        )
                    ))
                ywd = {k: v for k, v in (yb.get("weather_dist") or {}).items() if k not in WEATHER_HIDDEN}
                if ywd:
                    blocks.append(self._para(
                        "🌤️ 날씨: " + ", ".join(
                            f"{WEATHER_KO.get(k, k)}({v})" for k, v in ywd.items()
                        )
                    ))

        # ---- 📥 노션에 못 올린 파일 (last: the list can be long) ----
        if oversized:
            blocks.extend(self._oversized_list_blocks(oversized, cap))

        return blocks

    def _oversized_list_blocks(
        self,
        oversized: list[dict[str, Any]],
        cap: str,
    ) -> list[dict[str, Any]]:
        """Dashboard list of every video / file over the Notion cap, newest
        first: date · source (linked to its Notion page when known) · name
        (linked to the kidsnote original) · size."""
        blocks: list[dict[str, Any]] = [
            self._h2(f"📥 노션에 못 올린 파일 {len(oversized):,}개"),
            self._para(
                f"노션 무료 요금제는 파일당 {cap}까지만 올릴 수 있어요. "
                "파일 이름을 누르면 키즈노트 원본을 보거나 저장할 수 있어요.",
                color="gray",
            ),
        ]
        ordered = sorted(
            oversized,
            key=lambda o: (o.get("date") or "", o.get("name") or ""),
            reverse=True,
        )
        for o in ordered:
            rich: list[dict[str, Any]] = []
            if o.get("date"):
                rich.append({"type": "text", "text": {"content": f"{o['date']} · "}})
            rich.append(self._page_link_rt(o.get("source") or "키즈노트", o.get("page_id") or ""))
            icon = "🎬" if o.get("kind") == "video" else "📄"
            rich.append({"type": "text", "text": {"content": f" · {icon} "}})
            name: dict[str, Any] = {"content": (o.get("name") or "")[:200]}
            if _is_linkable(o.get("url") or ""):
                name["link"] = {"url": o["url"]}
            rich.append({"type": "text", "text": name})
            if o.get("size"):
                rich.append({
                    "type": "text",
                    "text": {"content": f" · {format_mb(o['size'])}"},
                    "annotations": {"color": "gray"},
                })
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": rich},
            })
        return blocks


    # ----------------------------------------------------------- 영양 분석

    def publish_nutrition(self, stats: dict[str, Any]) -> dict[str, Any] | None:
        """Replace the singleton ``🥗 영양 분석`` page with menu nutrition breakdown."""
        existing = self._find_singleton_page(self.NUTRITION_REPORT_ID)
        if existing:
            self._archive_page(existing)

        blocks = self._build_nutrition_blocks(stats)
        self._resolve_schema()
        assert self._prop_title is not None and self._prop_report_id is not None
        properties: dict[str, Any] = {
            self._prop_title: {
                "title": [{"text": {"content": self._dashboard_title(self.NUTRITION_TITLE)}}],
            },
            self._prop_report_id: {"number": self.NUTRITION_REPORT_ID},
        }
        if self._prop_date:
            properties[self._prop_date] = {
                "date": {"start": datetime.now().date().isoformat()},
            }
        try:
            return self._create_page(properties, blocks, all_or_nothing=False)
        except Exception as e:
            _LOGGER.warning("nutrition publish failed: %s", e)
            return None

    def _build_nutrition_blocks(self, stats: dict[str, Any]) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []

        last_refreshed = datetime.now().strftime("%Y-%m-%d %H:%M")
        blocks.append({
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {
                    "content": (
                        f"마지막 갱신: {last_refreshed}\n"
                        f"분석 대상: 식단 {stats.get('menus_total', 0)}일치\n"
                        f"식단표 메뉴 텍스트를 분류해서 영양 그룹별 등장 빈도를 보여줍니다."
                    ),
                }}],
                "icon": {"type": "emoji", "emoji": "🥗"},
                "color": "green_background",
            },
        })

        # 영양 그룹별 비율 (pie)
        group_counts = stats.get("nutrition_group_counts") or {}
        if group_counts:
            blocks.append(self._h2("🥗 영양 그룹 비율 (전체 기간)"))
            mer = ["pie title 영양 그룹"]
            for label, n in sorted(group_counts.items(), key=lambda kv: kv[1], reverse=True):
                mer.append(f'  "{label}" : {n}')
            blocks.append(self._mermaid_block("\n".join(mer)))

        # 월별 영양 그룹 분포 (table)
        monthly_group = stats.get("nutrition_monthly") or {}
        if monthly_group:
            blocks.append(self._h2("📅 월별 영양 그룹 분포"))
            groups = sorted({g for m in monthly_group.values() for g in m})
            header = "| 월 | " + " | ".join(groups) + " |"
            sep = "|" + "---|" * (len(groups) + 1)
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": header}}]},
            })
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": sep}}]},
            })
            for m in sorted(monthly_group.keys()):
                row = "| " + m + " | " + " | ".join(str(monthly_group[m].get(g, 0)) for g in groups) + " |"
                blocks.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text", "text": {"content": row}}]},
                })

        # TOP 메뉴 (the most frequently served items)
        top_menus = stats.get("top_menu_items") or []
        if top_menus:
            blocks.append(self._h2("🍱 가장 자주 나온 메뉴 TOP 15"))
            for name, count in top_menus[:15]:
                blocks.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{
                        "type": "text",
                        "text": {"content": f"• {name} — {count}회"},
                    }]},
                })

        # Footer disclaimer
        blocks.append({
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {
                    "content": (
                        "ℹ️ 영양 그룹은 메뉴명 키워드 매칭(rule-based) 기준이며, "
                        "정확한 칼로리·영양소 분석은 아닙니다. "
                        "식단 균형의 큰 그림만 보세요."
                    ),
                }}],
                "icon": {"type": "emoji", "emoji": "ℹ️"},
                "color": "gray_background",
            },
        })
        return blocks


    # ----------------------------------------------------------- LLM dashboards

    # ----------------------------------------------------------- 📋 준비물·일정

    _WEEKDAY_KO = "월화수목금토일"

    @classmethod
    def _md(cls, d: Any, *, weekday: bool = True) -> str:
        """date → '11/20(금)' (or '11/20')."""
        text = f"{d.month}/{d.day}"
        return f"{text}({cls._WEEKDAY_KO[d.weekday()]})" if weekday else text

    @staticmethod
    def _page_link_rt(label: str, page_id: str, color: str | None = None) -> dict[str, Any]:
        """Rich text ``label``, linked to the Notion page when its id is known."""
        text: dict[str, Any] = {"content": label}
        if page_id:
            text["link"] = {"url": notion_page_url(page_id)}
        rt: dict[str, Any] = {"type": "text", "text": text}
        if color:
            rt["annotations"] = {"color": color}
        return rt

    @classmethod
    def _source_rt(cls, source: str, title: str, page_id: str) -> dict[str, Any]:
        """'알림장' / '공지: 11월 3주 가정통신문', linked to its Notion page."""
        label = source
        if title:
            short = title if len(title) <= 30 else title[:30].rstrip() + "…"
            label = f"{source}: {short}"
        return cls._page_link_rt(label, page_id, "gray")

    def _supplies_blocks(self, board: Any, titles: dict[int, str]) -> list[dict[str, Any]]:
        """Body of the 📋 page from a ``supplies.Board``. ``titles`` maps a
        notice id to its title so short lines (``* 준비물 : 원복``) say which
        notice they belong to."""
        from supplies import EVENT_AHEAD_DAYS, REQUEST_LOOKBACK_DAYS, KST

        def bullet(rich: list[dict[str, Any]]) -> dict[str, Any]:
            return {"object": "block", "type": "bulleted_list_item",
                    "bulleted_list_item": {"rich_text": rich}}

        def plain(text: str, **ann: Any) -> dict[str, Any]:
            rt: dict[str, Any] = {"type": "text", "text": {"content": text}}
            if ann:
                rt["annotations"] = ann
            return rt

        blocks: list[dict[str, Any]] = [{
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [plain(
                    "선생님·원에서 쓴 글과 댓글에서 준비물·요청 문장과 날짜가 적힌 일정을 "
                    "원문 그대로 모았어요. 오른쪽 링크를 누르면 원래 글이 열려요.\n"
                    f"마지막 갱신: {datetime.now(KST).strftime('%Y-%m-%d %H:%M')}"
                )],
                "icon": {"type": "emoji", "emoji": "📋"},
                "color": "blue_background",
            },
        }]

        blocks.append(self._h2(f"📅 다가오는 일정 ({len(board.events)})"))
        if not board.events:
            blocks.append(self._para(
                f"앞으로 {EVENT_AHEAD_DAYS}일 안의 날짜가 적힌 일정이 없어요.", color="gray"))
        for e in board.events:
            when = self._md(e.start)
            if e.end != e.start:
                when += f"–{self._md(e.end)}"
            rich = []
            if e.start <= board.today <= e.end:
                rich.append(plain("오늘 ", bold=True, color="red"))
            rich += [plain(when, bold=True), plain(f" · {e.text} · "),
                     self._source_rt(e.source, titles.get(e.item_id, ""), e.page_id)]
            blocks.append(bullet(rich))

        blocks.append(self._h2(f"🎒 준비물·요청 — 최근 {REQUEST_LOOKBACK_DAYS}일 ({len(board.requests)})"))
        if not board.requests:
            blocks.append(self._para(
                f"최근 {REQUEST_LOOKBACK_DAYS}일 동안 선생님 글에서 찾은 요청이 없어요.", color="gray"))
        for r in board.requests:
            rich = [plain(self._md(r.posted, weekday=False), bold=True),
                    plain(f" · {r.text} · "),
                    self._source_rt(r.source, titles.get(r.item_id, ""), r.page_id)]
            blocks.append(bullet(rich))

        blocks.append(self._para(
            "글에 적힌 문장을 그대로 옮겨요. '다음 주 월요일'처럼 날짜 숫자 없이 적힌 일정은 "
            "📅에 나오지 않으니 🎒 목록도 함께 확인하세요.",
            color="gray",
        ))
        return blocks

    def publish_supplies(
        self,
        board: Any,
        titles: dict[int, str],
        calendar_db_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Replace the singleton ``📋 준비물·일정`` page (with a link to the
        📅 calendar database when there is one)."""
        blocks = self._supplies_blocks(board, titles)
        if calendar_db_id:
            # A plain link: Notion's link_to_page block rejects inline databases
            # ("database_id must reference a collection_view_page", live 2026-11-18).
            blocks.insert(1, {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [
                    {"type": "text", "text": {"content": "📅 날짜가 있는 일정은 "}},
                    {"type": "text", "text": {"content": "📅 키즈노트 일정",
                                              "link": {"url": notion_page_url(calendar_db_id)}},
                     "annotations": {"bold": True}},
                    {"type": "text", "text": {"content": " 달력에도 쌓여요."}},
                ]},
            })
        return self._replace_singleton(
            self.SUPPLIES_REPORT_ID,
            self._dashboard_title(self.SUPPLIES_TITLE),
            blocks,
        )

    def sync_calendar(self, board: Any, titles: dict[int, str], read: set[tuple[int, str]]) -> str | None:
        """📅 키즈노트 일정 database (see notion_calendar.py); its id or None."""
        from notion_calendar import CalendarSync
        return CalendarSync(self).sync(board, titles, read)

    def _replace_singleton(
        self,
        report_id: int,
        title: str,
        blocks: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Archive any existing page with ``Report ID == report_id`` and
        create a fresh one with the given title + body blocks. Shared
        helper for all four LLM dashboard pages.
        """
        existing = self._find_singleton_page(report_id)
        if existing:
            self._archive_page(existing)
        self._resolve_schema()
        assert self._prop_title is not None and self._prop_report_id is not None
        props: dict[str, Any] = {
            self._prop_title: {"title": [{"text": {"content": title[:200]}}]},
            self._prop_report_id: {"number": report_id},
        }
        if self._prop_date:
            props[self._prop_date] = {
                "date": {"start": datetime.now().date().isoformat()},
            }
        # Notion caps page-create children to 100; _create_page appends the
        # rest (e.g. a 345-alimnota milestone page) and keeps what landed.
        try:
            return self._create_page(props, blocks, all_or_nothing=False)
        except Exception as e:
            _LOGGER.warning("singleton page publish failed (%s): %s", title, e)
            return None

    def publish_growth_story(
        self,
        reports_by_month: dict[str, list[dict[str, Any]]],
        child_name: str = "",
    ) -> dict[str, Any] | None:
        """📖 매월 성장 스토리. One paragraph per month, LLM-generated."""
        if _get_ollama() is None:
            return None
        blocks: list[dict[str, Any]] = [{
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {
                    "content": f"매월 한 단락. LLM이 그 달 알림장을 보고 따뜻한 톤의 성장 스토리를 작성합니다.\n마지막 갱신: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                }}],
                "icon": {"type": "emoji", "emoji": "📖"},
                "color": "blue_background",
            },
        }]
        self._dashboard_started()
        # Two-axis ordering (2026-05-27 fix):
        #   * Display order = newest first (2026-05-22 user request — most
        #     recent month at the top of the page).
        #   * Processing order = interleave newest ↔ oldest, working inward.
        # If the wall-clock cap fires partway through a 12-15 month run,
        # the old "newest-first" loop published 2026 months and dropped
        # ALL of 2025 — and because the partial result overwrote the
        # singleton, the next cron run skipped LLM dashboards entirely
        # (no new alimnotas → should_run_llm_dashboards=False), so 2025
        # stayed permanently empty. Interleaving guarantees at least one
        # entry per year on every pass.
        #
        # 2026-05-28 fix: incremental self-heal. Earlier interleave fix
        # ensured both year ends got coverage on a single pass but left
        # a worse bug: with cap=45min and ~5 months/cap, EVERY cycle
        # processed the same 8 months and re-wrote the same 7
        # placeholders, so placeholder months were never filled. Now
        # we read the existing page first, reuse the already-completed
        # months verbatim, and only burn LLM time on placeholder
        # months. Each cycle reduces the placeholder set by ~5-7
        # months until coverage is complete.
        existing_month_text: dict[str, str] = {}
        placeholder_months: set[str] = set()
        existing_page_id = self._find_singleton_page(self.GROWTH_STORY_REPORT_ID)
        if existing_page_id:
            try:
                r = self.session.get(
                    f"{NOTION_API}/blocks/{existing_page_id}/children?page_size=100",
                    headers=self._headers(),
                    timeout=self.timeout,
                )
                if r.ok:
                    cur_ym: str | None = None
                    cur_text = ""
                    cur_is_placeholder = False

                    def _close_month() -> None:
                        if cur_ym is None:
                            return
                        if cur_is_placeholder:
                            placeholder_months.add(cur_ym)
                        elif cur_text.strip():
                            existing_month_text[cur_ym] = cur_text

                    for block in r.json().get("results") or []:
                        btype = block.get("type")
                        if btype == "heading_2":
                            _close_month()
                            h_text = "".join(
                                seg.get("plain_text", "")
                                for seg in block.get("heading_2", {}).get("rich_text") or []
                            )
                            m = re.search(r"\d{4}-\d{2}", h_text)
                            cur_ym = m.group(0) if m else None
                            cur_text = ""
                            cur_is_placeholder = False
                        elif cur_ym and btype == "paragraph":
                            rt = block.get("paragraph", {}).get("rich_text") or []
                            text = "".join(seg.get("plain_text", "") for seg in rt)
                            if "다음 cron 사이클" in text:
                                cur_is_placeholder = True
                            else:
                                cur_text += ("\n" if cur_text else "") + text
                    _close_month()
            except Exception as e:
                _LOGGER.warning("growth story: existing-page read failed: %s", e)

        sorted_desc = sorted(reports_by_month.keys(), reverse=True)
        interleaved: list[str] = []
        left, right = 0, len(sorted_desc) - 1
        while left <= right:
            interleaved.append(sorted_desc[left]); left += 1
            if left <= right:
                interleaved.append(sorted_desc[right]); right -= 1
        # Placeholder months first (this cycle's actual work), then any
        # remaining (already-complete; they'll skip the LLM call in the
        # loop). Without this reorder, the cap would keep firing on the
        # same first-N months every cycle.
        processing_order = (
            [m for m in interleaved if m in placeholder_months]
            + [m for m in interleaved if m not in placeholder_months]
        )
        if placeholder_months:
            logging.getLogger(__name__).info(
                "📖 growth story self-heal: %d placeholder month(s) to fill, "
                "%d already complete",
                len(placeholder_months), len(existing_month_text),
            )

        # Per-month rendered content (heading + paragraphs). Filled in
        # processing_order, then walked in sorted_desc order at the end
        # so the page displays newest-first regardless of which months
        # finished this cycle.
        ym_blocks: dict[str, list[dict[str, Any]]] = {}

        def _make_heading(ym: str) -> dict[str, Any]:
            return {
                "object": "block", "type": "heading_2",
                "heading_2": {"rich_text": [{"type": "text", "text": {"content": f"📅 {ym}"}}]},
            }

        # Pre-fill ym_blocks from already-complete months read off the
        # existing page. These skip the LLM call in the main loop.
        for ym, text in existing_month_text.items():
            if ym not in reports_by_month:
                continue
            blocks_for_month: list[dict[str, Any]] = [_make_heading(ym)]
            for chunk in self._chunk(text):
                blocks_for_month.append(self._para(chunk))
            ym_blocks[ym] = blocks_for_month

        cap_hit = False
        for mi, ym in enumerate(processing_order):
            if ym in ym_blocks:
                continue  # already filled from prior cycle, no LLM needed
            if self._dashboard_over_cap():
                logging.getLogger(__name__).warning(
                    "📖 growth story: wall-clock cap %.0fs hit at %d/%d months, "
                    "remaining months will be filled in next cron cycle",
                    self.dashboard_max_seconds or 0, mi, len(processing_order),
                )
                cap_hit = True
                break
            items = reports_by_month[ym]
            if not items:
                ym_blocks[ym] = [
                    _make_heading(ym),
                    self._para("이 달에는 알림장이 없습니다.", color="gray"),
                ]
                continue
            # Shrunk to 2500 chars (was 4500) — llama3.1:8b on CPU was
            # timing out at 180s with the longer context on every month.
            joined = "\n\n".join(
                (r.get("content") or "").strip()[:200] for r in items[:15]
            )[:2500]
            if len(joined.strip()) < 200:
                ym_blocks[ym] = [
                    _make_heading(ym),
                    self._para(
                        f"이 달은 알림장이 짧아서 성장 스토리를 만들기 어렵습니다 (알림장 {len(items)}개).",
                        color="gray",
                    ),
                ]
                continue
            given = _given_name(child_name) or "아이"
            topic = _topic_form(given)  # 하린→하린이 / 유주→유주
            prompt = (
                f"다음은 어린이집 {topic}의 {ym} 한 달치 알림장 모음입니다.\n\n"
                "이 알림장들의 내용만 사용해서 그 달의 성장 스토리를 "
                "한 단락(3-4문장)으로 작성하세요. 알림장에 실제 등장한 "
                f"사건·활동·관찰 2-3개를 구체적으로 인용해야 합니다. "
                f"자녀는 ``{topic}``로만 지칭하세요.\n\n"
                "**규칙 (반드시 지킬 것)**:\n"
                "① 한국어만 사용 (중국어 한자·독일어·기타 외국어 금지). "
                "본문에 영어 단어(``exercise``, ``swimming`` 등)가 있어도 "
                "스토리는 한국어로 의역. 영어 문장(``Am I a cat?`` 같은 게임 대사)은 "
                "**인용하지 말고** 핵심을 한국어로 요약.\n"
                "② **환각 금지**: 알림장에 나오지 않은 사건·활동·"
                "사물·장소를 추가하지 마세요.\n"
                "③ **다른 자녀 이름 익명화**: 본문에 다른 자녀 이름이 "
                "나와도 (``성준이``, ``태민이``, ``민지`` 등) 그대로 쓰지 말고 "
                "``친구`` 또는 ``반 친구들``로 바꿔 표현. 본인 자녀 "
                f"({topic})만 실명으로 지칭.\n"
                "④ 일반적·추상적 표현보다 알림장의 구체적 문구를 활용.\n"
                "⑤ 다른 설명·서두 없이 본문만 답하세요.\n\n"
                f"알림장 모음:\n{joined}\n\n"
                "성장 스토리:"
            )
            story = self._ask_ollama(
                prompt, max_chars=600, num_predict=300, timeout=600,
                final_labels=("성장 스토리:",),
            )
            if not story:
                _LOGGER.warning("growth story for %s: empty LLM output, placeholder", ym)
                ym_blocks[ym] = [
                    _make_heading(ym),
                    self._para(
                        "이 달의 성장 스토리를 LLM이 만들지 못했습니다. 다음 force-refresh에서 다시 시도됩니다.",
                        color="gray",
                    ),
                ]
                continue
            # Example-bleed guard. Previously we dropped the whole month
            # when one of 피아노/발레/자화상/음악교실/미술시간 leaked from
            # the few-shot example. That sometimes wiped EVERY month
            # (full-year run), leaving the page empty. Switch to a
            # softer guard: redact the offending tokens but keep the
            # rest of the story rather than drop the whole paragraph.
            BLEED_TOKENS = ("피아노", "발레", "자화상", "음악교실", "미술시간")
            if any(tok in story for tok in BLEED_TOKENS):
                leaked = [t for t in BLEED_TOKENS if t in story]
                _LOGGER.warning(
                    "growth story for %s leaked example tokens (%s) — redacting",
                    ym, ", ".join(leaked),
                )
                cleaned_sents = []
                for sent in story.replace("<br>", "\n").split("\n"):
                    if not any(tok in sent for tok in BLEED_TOKENS):
                        cleaned_sents.append(sent)
                story = "\n".join(cleaned_sents).strip()
                if len(story) < 30:
                    _LOGGER.warning("growth story for %s: too short after redaction, skipping", ym)
                    continue
            month_blocks = [_make_heading(ym)]
            for chunk in self._chunk(story):
                month_blocks.append(self._para(chunk))
            ym_blocks[ym] = month_blocks

        # Compose page in display order (newest → oldest). Months that
        # didn't finish this cycle get a transparent placeholder so the
        # user can see the gap; the next cron run will fill them in
        # (the self-heal logic in fetch.py treats partial coverage as
        # "missing dashboard" — see GROWTH_STORY incomplete handling).
        for ym_display in sorted_desc:
            if ym_display in ym_blocks:
                blocks.extend(ym_blocks[ym_display])
            else:
                blocks.append(_make_heading(ym_display))
                blocks.append(self._para(
                    "다음 cron 사이클에서 보충됩니다.",
                    color="gray",
                ))
        page_result = self._replace_singleton(
            self.GROWTH_STORY_REPORT_ID, self._dashboard_title(self.GROWTH_STORY_TITLE), blocks,
        )
        # Tag the returned dict so the caller can know whether more
        # months remain — drives the self-heal "regenerate next cron"
        # decision in fetch.py.
        if page_result is not None:
            page_result["_growth_story_complete"] = (not cap_hit) and (len(ym_blocks) == len(sorted_desc))
        return page_result

    def publish_milestones(
        self,
        reports: list[dict[str, Any]],
        child_name: str = "",
    ) -> dict[str, Any] | None:
        """🌟 마일스톤. LLM이 ``처음 ...`` 패턴을 본문에서 추출.

        Iterates every report; LLM responds with one short line or ``없음``.
        Heavy (1 call per report) but valuable.
        """
        if _get_ollama() is None:
            return None
        # Subsample to keep both Ollama call count and Notion block count
        # within reason. A full year produces 300+ reports; analysing each
        # one separately overruns the Notion 100-children page cap and
        # exhausts Ollama after a few hours. Sample uniformly across the
        # date-sorted list: cap at 120 reports total, distributed across
        # the available date range so months stay represented.
        SAMPLE_CAP = 120
        # Sample uniformly across the date range (oldest-first for sampling
        # math), but then REVERSE so the rendered list is newest-first per
        # 2026-05-22 user request.
        ordered_all = sorted(reports, key=lambda r: (r.get("date_written") or ""))
        if len(ordered_all) > SAMPLE_CAP:
            step = len(ordered_all) / SAMPLE_CAP
            ordered = [ordered_all[int(i * step)] for i in range(SAMPLE_CAP)]
            sampled_note = f" (전체 {len(ordered_all)}개 중 {SAMPLE_CAP}개 균등 샘플)"
        else:
            ordered = ordered_all
            sampled_note = ""
        # Reverse for newest-first rendering. Sample selection was done in
        # chronological order above so the spacing remains representative.
        ordered = list(reversed(ordered))
        intro = (
            f"마지막 갱신: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"알림장 {len(ordered)}개를 분석해 자녀의 발달 마일스톤(처음 ...)을 추출했습니다{sampled_note}."
        )
        blocks: list[dict[str, Any]] = [{
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {"content": intro}}],
                "icon": {"type": "emoji", "emoji": "🌟"},
                "color": "yellow_background",
            },
        }]
        self._dashboard_started()
        seen_milestones: set[str] = set()
        scanned = 0
        for r in ordered:
            if self._dashboard_over_cap():
                logging.getLogger(__name__).warning(
                    "🌟 milestones: wall-clock cap %.0fs hit at %d/%d, "
                    "publishing partial result",
                    self.dashboard_max_seconds or 0, scanned, len(ordered),
                )
                break
            scanned += 1
            body = (r.get("content") or "").strip()
            if not body or len(body) < 40:
                continue
            date = (r.get("date_written") or "")[:10]
            given = _given_name(child_name) or "아이"
            topic = _topic_form(given)  # 하린→하린이 / 유주→유주
            prompt = (
                f"다음 어린이집 알림장에서 자녀({given})의 발달·성장 "
                "단서를 명사구 한 줄(20자 이내)로 뽑아. "
                "**규칙**: ① 반드시 ``명사구``로만 답해 — ``~다``, "
                "``~요``, ``~습니다``, ``~보였습니다`` 같은 종결어미 "
                "절대 금지. ② 단서가 정말 없으면 ``없음``만 답해. "
                "③ 한국어만 사용 — 영어/독일어 등 외국어 단어를 명사구에 "
                "끼워넣지 마세요 (``수영doing``, ``스티커arbeit`` 같은 형태 금지). "
                "본문에 영어가 있어도 한국어로 의역하세요.\n\n"
                "[예시 — 명사구 형식 ✓]\n"
                f"알림장: {topic}가 친구에게 장난감을 건네주며 사회성이 "
                "자라는 모습을 보였습니다.\n"
                "단서: 친구에게 장난감 양보\n\n"
                f"알림장: ``동물농장`` 노래에 손을 흔들고 박수를 치며 "
                "리듬을 잘 표현했어요.\n"
                "단서: 노래에 맞춰 리듬감 표현\n\n"
                f"알림장: 처음으로 한 걸음을 떼었습니다.\n"
                "단서: 첫 걸음\n\n"
                f"알림장: 오늘은 식단으로 미역국, 흰쌀밥, 갈비찜을 먹었습니다.\n"
                "단서: 없음\n\n"
                "[잘못된 예 ✗ — 문장 형식은 답하지 마.]\n"
                "단서: 활기차고 씩씩한 모습을 보였습니다.\n"
                "단서: 사회성이 자라는 모습을 보였습니다.\n\n"
                "[지금 분석할 알림장]\n"
                f"알림장: {body[:1200]}\n"
                "단서:"
            )
            ms = self._ask_ollama(
                prompt, max_chars=60, num_predict=40, timeout=60,
                final_labels=("단서:",),
            )
            if not ms:
                continue
            ms = ms.split("\n")[0].strip()
            # "없음" anywhere in the response (model often answers
            # ``알림장의 단서는 "없음"입니다.`` rather than just ``없음``).
            if any(tok in ms for tok in ("없음", "없다", "없습니다", "없어요", "찾을 수 없")):
                continue
            # Conversational chatter ("좋네요!") isn't a milestone.
            if ms.endswith(("!", "?")):
                continue
            CHATTER_TAILS = (
                "좋네요.", "보내요.", "같네요.", "같아요.", "있어요.", "이에요.",
                "보냈어요.", "있겠지요.", "있지요.", "보내요!", "있어요!",
            )
            if any(ms.endswith(t) for t in CHATTER_TAILS):
                continue
            # Sentence-form output — milestone should be a noun phrase, not
            # a description like "...보였습니다." or "...했어요." The prompt
            # asks for noun-phrase only; this is a safety net.
            SENTENCE_TAILS = (
                "습니다.", "습니다", "었어요.", "었습니다.", "었습니다",
                "되었어요.", "되었습니다.", "되었습니다",
                "보였어요.", "보였습니다.", "보였습니다", "보였다.",
                "지냈어요.", "지냈습니다.", "지냈습니다",
                "했어요.", "했습니다.", "했습니다", "했다.",
            )
            if any(ms.endswith(t) for t in SENTENCE_TAILS):
                continue
            # Cap length — milestones should be a noun phrase, not a sentence.
            if len(ms) > 60:
                continue
            # Dedup similar milestones (case-insensitive substring)
            ms_key = ms.lower()
            if any(ms_key in s or s in ms_key for s in seen_milestones):
                continue
            seen_milestones.add(ms_key)
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [
                    {"type": "text", "text": {"content": f"📅 {date}  "}, "annotations": {"bold": True}},
                    {"type": "text", "text": {"content": ms}},
                ]},
            })
        return self._replace_singleton(
            self.MILESTONES_REPORT_ID, self._dashboard_title(self.MILESTONES_TITLE), blocks,
        )

    def publish_interests(
        self,
        reports_by_quarter: dict[str, list[dict[str, Any]]],
        child_name: str = "",
    ) -> dict[str, Any] | None:
        """🌱 분기별 관심사 TOP 5. 4 LLM calls (per quarter)."""
        if _get_ollama() is None:
            return None
        blocks: list[dict[str, Any]] = [{
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {
                    "content": f"분기별로 자녀가 가장 좋아한 활동/사물/사람 TOP 5.\n마지막 갱신: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                }}],
                "icon": {"type": "emoji", "emoji": "🌱"},
                "color": "green_background",
            },
        }]
        self._dashboard_started()
        # Newest quarter first per 2026-05-22 user request.
        all_qs = sorted(reports_by_quarter.keys(), reverse=True)
        for qi, q in enumerate(all_qs):
            if self._dashboard_over_cap():
                logging.getLogger(__name__).warning(
                    "🌱 interests: wall-clock cap %.0fs hit at %d/%d quarters, "
                    "publishing partial result",
                    self.dashboard_max_seconds or 0, qi, len(all_qs),
                )
                break
            items = reports_by_quarter[q]
            if not items:
                blocks.append({
                    "object": "block", "type": "heading_2",
                    "heading_2": {"rich_text": [{"type": "text", "text": {"content": f"🌱 {q}"}}]},
                })
                blocks.append(self._para(
                    "이 분기에는 알림장이 없습니다.", color="gray",
                ))
                continue
            # Shrunk from 5000 chars (was timing out llama3.1:8b on CPU)
            joined = "\n".join(
                (r.get("content") or "").strip()[:150] for r in items[:20]
            )[:2500]
            given = _given_name(child_name)
            prompt = (
                f"다음은 어린이집 자녀의 {q} 분기 알림장 모음이야. "
                "자녀가 이 기간 가장 좋아한 활동·사물·사람을 TOP 5로 정리해. "
                "각 항목은 한 줄로 ``1. ... `` ``2. ...`` 형태로. "
                f"{'자녀 이름은 ``' + given + '``.' if given else ''} "
                "**규칙**: ① 한국어만 사용 (중국어 한자·외국어 단어 금지). "
                "② 알림장에 실제 등장한 활동/사물/사람만 포함 — 없는 항목 추가 금지. "
                "③ 다른 자녀 이름이 본문에 있어도 그대로 쓰지 말고 "
                "``친구`` 또는 ``반 친구들``로 표현 (개인정보 보호). "
                "④ 표준 한국어 사용 — 본문에 ``열시미`` 같은 비표준 표기가 "
                "있어도 표준어(``열심히``)로 교정. "
                "⑤ 다른 설명 없이 1~5번 목록만 답해.\n\n"
                f"알림장:\n{joined}\n\nTOP 5:"
            )
            top = self._ask_ollama(
                prompt, max_chars=400, num_predict=200, timeout=600,
                final_labels=("TOP 5:", "Top 5:"),
            )
            if not top:
                _LOGGER.warning("interests for %s: empty LLM output, placeholder", q)
                blocks.append({
                    "object": "block", "type": "heading_2",
                    "heading_2": {"rich_text": [{"type": "text", "text": {"content": f"🌱 {q}"}}]},
                })
                blocks.append(self._para(
                    "이 분기의 관심사를 LLM이 만들지 못했습니다. 다음 force-refresh에서 다시 시도됩니다.",
                    color="gray",
                ))
                continue
            blocks.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": [{
                    "type": "text", "text": {"content": f"🌱 {q}"},
                }]},
            })
            for line in top.split("\n"):
                line = line.strip()
                if line:
                    blocks.append(self._para(line))
        return self._replace_singleton(
            self.INTERESTS_REPORT_ID, self._dashboard_title(self.INTERESTS_TITLE), blocks,
        )

    def publish_teacher_thanks(
        self,
        reports: list[dict[str, Any]],
        child_name: str = "",
    ) -> dict[str, Any] | None:
        """💌 연도별 선생님께. One letter per year, each grounded in that
        year's teacher-written alimnotas only.

        Per 2026-05-22 user feedback the single-letter format made the
        page misleading on multi-year backups (the previous "2026년
        선생님께" was only writing about 2026 even when the data spanned
        2018-2024). Now we group teacher alimnotas by year, call the LLM
        once per year, and render newest year first.
        """
        if _get_ollama() is None:
            return None
        # Center staff: kidsnote uses both "teacher" (homeroom) and "admin"
        # (원감/원장 — common at academies). Both write alimnotas that the
        # parent receives; the previous filter only kept "teacher", which
        # erased entire years for academy-only accounts (e.g. iibii_22's
        # 2024 NCE 어학원 logs were all author.type=admin → no 2024 letter
        # was generated).
        teacher_reports = [
            r for r in reports
            if ((r.get("author") or {}).get("type") or "") in ("teacher", "admin")
        ]
        if not teacher_reports:
            return None
        given = _given_name(child_name) or "아이"
        topic = _topic_form(given)  # 하린→하린이 / 유주→유주

        # Group by year, newest year first.
        from collections import defaultdict as _dd
        by_year: dict[str, list[dict[str, Any]]] = _dd(list)
        for r in teacher_reports:
            yr = (r.get("date_written") or "")[:4]
            if len(yr) == 4 and yr.isdigit():
                by_year[yr].append(r)
        if not by_year:
            return None

        blocks: list[dict[str, Any]] = [{
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {
                    "content": (
                        "연도별 알림장 기반 감사 편지 초안 — 매 해의 알림장만 사용해 따로 작성합니다. "
                        "졸업·연말 시 가족이 다듬어 사용.\n"
                        f"마지막 갱신: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                    ),
                }}],
                "icon": {"type": "emoji", "emoji": "💌"},
                "color": "purple_background",
            },
        }]

        self._dashboard_started()
        all_yrs = sorted(by_year.keys(), reverse=True)
        for yi, yr in enumerate(all_yrs):
            if self._dashboard_over_cap():
                logging.getLogger(__name__).warning(
                    "💌 teacher thanks: wall-clock cap %.0fs hit at %d/%d years, "
                    "publishing partial result",
                    self.dashboard_max_seconds or 0, yi, len(all_yrs),
                )
                break
            # Take up to 15 most recent excerpts in that year (80 chars each).
            yr_reports = sorted(
                by_year[yr], key=lambda r: r.get("date_written") or "", reverse=True,
            )[:15]
            joined = "\n".join(
                "- " + (r.get("content") or "").strip()[:80]
                for r in yr_reports
            )[:1500]
            prompt = (
                f"다음은 어린이집·학원 선생님이 자녀({given})에 대해 {yr}년 한 해 동안 쓴 "
                "알림장의 짧은 발췌야. 이를 바탕으로 부모가 선생님께 보낼 "
                "감사 편지(4-5문장)를 한국어로 써. 발췌에 실제 등장한 활동·"
                "에피소드 2-3개를 구체적으로 언급해.\n\n"
                "**규칙**: ① 한국어만 사용. ② 발췌에 없는 활동/사건 추가 금지. "
                "③ 다른 자녀 이름이 본문에 있어도 ``친구``로 표현.\n\n"
                "[예시]\n"
                f"발췌:\n- {topic}가 친구에게 장난감을 양보함\n"
                "- ``동물농장`` 노래에 박수\n- 송편 만들기에 참여\n"
                f"편지: 선생님, {yr}년 한 해 동안 우리 {topic}를 사랑으로 "
                "돌봐주셔서 진심으로 감사드립니다. 친구에게 장난감을 양보하는 "
                "사회성도, ``동물농장`` 노래에 박수를 치는 즐거움도, 송편을 "
                f"만져보던 낯선 촉감의 기억까지, 모두 선생님 덕분에 우리 {topic}의 "
                "소중한 추억이 되었습니다. 따뜻한 손길 잊지 않겠습니다.\n\n"
                "[지금 작성할 감사 편지]\n"
                f"발췌:\n{joined}\n"
                "편지:"
            )
            letter = self._ask_ollama(
                prompt, max_chars=900, num_predict=400, timeout=1200,
                final_labels=("편지:", "감사 편지:"),
            )
            # Always render the year heading so empty-LLM years still show
            # up as a visible gap with explanation.
            blocks.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": [{
                    "type": "text",
                    "text": {"content": f"📅 {yr}년"},
                }]},
            })
            if not letter:
                _LOGGER.warning("teacher thanks for %s: empty LLM, placeholder", yr)
                blocks.append(self._para(
                    f"{yr}년 감사 편지를 LLM이 만들지 못했습니다. 다음 force-refresh에서 다시 시도됩니다.",
                    color="gray",
                ))
                continue
            for chunk in self._chunk(letter):
                blocks.append(self._para(chunk))
        return self._replace_singleton(
            self.TEACHER_THANKS_REPORT_ID, self._dashboard_title(self.TEACHER_THANKS_TITLE), blocks,
        )




# Static nutrition group dictionary — keyword-based classification of
# daycare lunch menu items. Order doesn't matter; multiple groups can fire
# for one menu (e.g. ``계란찜`` → 단백질 / ``콩나물`` → 채소).
NUTRITION_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("🥩 단백질", (
        "고기", "소고기", "돼지고기", "닭고기", "오리", "한우",
        "불고기", "갈비", "제육", "삼겹", "장조림", "동그랑땡",
        "햄", "소시지", "햄버그", "미트볼", "스테이크", "찜닭",
        "계란", "달걀", "계란찜", "달걀찜", "오믈렛", "스크램블",
        "두부", "콩", "콩나물", "콩고기", "유부",
        "생선", "고등어", "갈치", "삼치", "동태", "조기", "참치", "연어",
        "오징어", "새우", "굴", "조개", "꽁치",
    )),
    ("🌾 탄수화물", (
        "밥", "쌀밥", "잡곡밥", "현미밥", "보리밥", "콩밥", "팥밥",
        "비빔밥", "주먹밥", "볶음밥", "오므라이스",
        "면", "국수", "라면", "우동", "잔치국수", "스파게티", "파스타",
        "빵", "토스트", "샌드위치", "햄버거빵",
        "떡", "떡국", "송편", "찰떡", "인절미", "백설기",
        "감자", "고구마", "옥수수",
    )),
    ("🥬 채소", (
        "김치", "배추김치", "깍두기", "총각김치", "물김치",
        "나물", "시금치", "콩나물", "숙주", "고사리", "도라지",
        "오이", "당근", "양파", "마늘", "파", "상추", "양배추",
        "배추", "무", "샐러드", "샐러리",
        "브로콜리", "버섯", "팽이", "송이",
        "쌈", "쌈장",
    )),
    ("🍅 과일", (
        "과일", "사과", "배", "딸기", "포도", "감", "귤", "오렌지",
        "키위", "바나나", "참외", "수박", "복숭아", "자두", "체리",
        "블루베리", "토마토", "방울토마토", "파인애플", "망고",
    )),
    ("🥛 유제품", (
        "우유", "두유", "요거트", "요구르트", "치즈", "버터",
        "크림", "아이스크림",
    )),
    ("🍲 국·찌개·수프", (
        "국", "탕", "찌개", "전골", "스튜", "수프", "포타지",
        "된장국", "미역국", "콩나물국", "북엇국", "감자국",
        "김치찌개", "된장찌개", "부대찌개", "순두부",
    )),
    ("🍩 간식·디저트", (
        "쿠키", "케이크", "젤리", "푸딩", "초콜릿", "사탕",
        "파이", "와플", "팬케이크", "약과", "한과", "꿀떡",
        "과자", "비스킷", "타르트",
    )),
)
__all__ = ["NotionMirror", "DEFAULT_MAX_IMAGE_BYTES"]
