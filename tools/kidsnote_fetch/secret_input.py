"""Clean up setup values the way people actually paste them (stdlib only).

Secrets typed into GitHub's form often carry a trailing newline, an invisible
BOM or zero-width character, wrapping quotes, or, for Notion, a whole link
instead of the bare id. Normalizing here turns those into working values
instead of cryptic API errors.
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

_INVISIBLE = re.compile("[﻿​‌‍⁠]")
_NOTION_ID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|[0-9a-fA-F]{32}"
)


def clean_secret(value: str | None) -> str:
    """Drop invisible characters, surrounding whitespace and wrapping quotes."""
    text = _INVISIBLE.sub("", value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def normalize_token(value: str | None) -> str:
    """A Notion token, tolerating a pasted 'Bearer ' prefix."""
    text = clean_secret(value)
    if text.lower().startswith("bearer "):
        text = text[len("bearer "):].strip()
    return text


def normalize_notion_id(value: str | None) -> str:
    """32-hex Notion id from a bare id, a dashed UUID or any Notion link; '' if none is found.

    Only the path of a link is searched: the `?v=` view id in a database link
    is also 32 hex characters and must not win.
    """
    text = clean_secret(value)
    if not text:
        return ""
    if "/" in text:
        text = urlsplit(text if "://" in text else "https://" + text).path
    else:
        text = text.split("?", 1)[0].split("#", 1)[0]
    ids = _NOTION_ID.findall(text)
    return ids[-1].replace("-", "").lower() if ids else ""


def safe_url(url: str) -> str:
    """Shorten a media URL to host + last path segment for logs.

    Kidsnote media live on a public CDN (``up-kids-kage.kakao.com/dn/<key>/…``)
    where the path itself is the access key: anyone holding it can open the
    photo without logging in, and the run logs of a public fork are public.
    So logs get only the host and the file's last segment (``img.jpg``,
    ``video_high.mp4``), never the key or a query string.
    """
    if not isinstance(url, str):
        return str(url)
    parts = urlsplit(url)
    last = parts.path.rstrip("/").rsplit("/", 1)[-1]
    return f"{parts.netloc}/…/{last}" if parts.netloc else "…"


_URL_IN_TEXT = re.compile(r"https?://[^\s'\"<>)]+")


def safe_err(err: BaseException) -> str:
    """``str(err)`` with every URL shortened by ``safe_url``. requests puts the
    full URL in its messages (``404 Client Error: … for url: https://…``)."""
    return _URL_IN_TEXT.sub(lambda m: safe_url(m.group(0)), str(err))


# ---------------------------------------------------------------- child names
#
# KIDSNOTE_CHILD_NAME is compared with the names kidsnote returns. Issue #3:
# a parent with three children got "no child matches" for every value they
# tried. What breaks a plain substring test, all seen in pasted secrets:
# Korean copied on a Mac is decomposed (NFD: ㅅ+ㅗ+ㅇ looks like 송 but isn't
# equal), a space inside the name, invisible characters, and several names
# typed into the one slot ("송하정, 송다정").

_NAME_SEPARATORS = re.compile(r"[,，、/;；·|]")


def name_key(value: str | None) -> str:
    """Comparable form of a name: NFKC, no invisible characters or spaces, case-folded."""
    text = unicodedata.normalize("NFKC", clean_secret(value))
    return "".join(_INVISIBLE.sub("", text).split()).casefold()


def match_children(children: list[dict], wanted: str | None) -> tuple[str, list[dict]]:
    """Which children ``wanted`` names, as ``(outcome, children)``.

    outcome: ``"empty"`` (nothing written), ``"one"``, ``"many"`` (it fits
    several children: write more of the name), ``"none"``, or ``"several"``
    when the value holds more than one name (each child needs its own slot).
    Matching is a substring test on ``name_key`` of both sides.
    """
    raw = unicodedata.normalize("NFKC", clean_secret(wanted))
    parts = [name_key(p) for p in _NAME_SEPARATORS.split(raw)]
    parts = [p for p in parts if p]
    if not parts:
        return "empty", []
    if len(parts) > 1:
        return "several", [c for c in children if any(p in name_key(c.get("name")) for p in parts)]
    matches = [c for c in children if parts[0] in name_key(c.get("name"))]
    if len(matches) == 1:
        return "one", matches
    if matches:
        return "many", matches
    # "송하정 송다정": names separated by spaces only.
    words = [name_key(w) for w in raw.split()]
    named = [c for c in children if any(len(w) >= 2 and w in name_key(c.get("name")) for w in words)]
    if len(words) > 1 and len(named) > 1:
        return "several", named
    return "none", []


def masked_value(value: str | None) -> str:
    """What was written, masked like a child's name ('송*정'), for messages."""
    return mask_name(unicodedata.normalize("NFC", clean_secret(value)))


def child_label(child: dict) -> str:
    """'송*정(2019년생)': masked name plus birth year, so same-looking names differ."""
    label = mask_name(child.get("name"))
    year = str(child.get("date_birth") or "")[:4]
    return f"{label}({year}년생)" if year.isdigit() else label


def mask_name(name: str | None) -> str:
    """'우하린' -> '우*린': recognizable to the parent, but not a full name in public Actions logs."""
    text = clean_secret(name)
    if len(text) <= 1:
        return "*" if text else ""
    if len(text) == 2:
        return text[0] + "*"
    return text[0] + "*" * (len(text) - 2) + text[-1]
