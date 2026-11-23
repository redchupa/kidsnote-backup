"""Check everything a backup run needs, in seconds, before anything is installed (stdlib only).

1. kidsnote login (fresh id/password login, cookie fallback)
2. KIDSNOTE_CHILD_NAME matches exactly one child on the account
3. Notion token works and NOTION_DATABASE_ID is a page or database it can see

GitHub mode writes ok / reason / hint to $GITHUB_OUTPUT and exports the fresh
kidsnote session (masked) to $GITHUB_ENV. It exits 0 even on failure so the
notify-once step decides whether this problem gets an email. Child names are
masked (우*린) in everything printed: Actions logs of a public fork are public.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from typing import Any

import kidsnote_auth
import notion_check
from secret_input import child_label, mask_name, masked_value, match_children

UNEXPECTED_HINT = "사전 점검 중 예상하지 못한 오류가 났습니다. 잠시 후 다시 실행해 보고, 계속되면 이슈로 알려 주세요."


class CheckFailed(Exception):
    def __init__(self, reason: str, hint: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.hint = hint
        self.detail = detail


def pick_child(children: list[dict[str, Any]], wanted: str | None) -> dict[str, Any]:
    """The child KIDSNOTE_CHILD_NAME refers to (same rule as fetch.py: case-insensitive substring)."""
    if not children:
        raise CheckFailed("child_none", "이 키즈노트 계정에 등록된 자녀가 없습니다. 키즈노트 앱에서 자녀 등록 상태를 확인해 주세요.")
    listed = ", ".join(child_label(c) for c in children)
    outcome, matches = match_children(children, wanted)
    if outcome == "empty":
        if len(children) == 1:
            return children[0]
        raise CheckFailed("child_name_required",
                          f"자녀가 {len(children)}명입니다({listed}). "
                          "KIDSNOTE_CHILD_NAME 시크릿에 백업할 자녀 이름을 적어 주세요.")
    if outcome == "several":
        raise CheckFailed("child_name_multiple",
                          "KIDSNOTE_CHILD_NAME 에는 자녀 한 명의 이름만 적어 주세요. "
                          "다른 자녀는 KIDSNOTE_CHILD_NAME_2 · NOTION_DATABASE_ID_2 처럼 번호를 붙여 "
                          f"따로 등록합니다 (안내서 'A. 자녀가 여러 명일 때'). 이 계정의 자녀: {listed}.")
    if outcome == "none":
        raise CheckFailed("child_not_found",
                          f"KIDSNOTE_CHILD_NAME 과 일치하는 자녀가 없습니다. 적은 값: {masked_value(wanted)}. "
                          f"이 계정의 자녀: {listed}. 시크릿 값을 자녀 이름(일부만 적어도 됨)으로 고쳐 주세요.")
    if outcome == "many":
        both = ", ".join(child_label(c) for c in matches)
        raise CheckFailed("child_ambiguous",
                          f"KIDSNOTE_CHILD_NAME 이 자녀 여러 명({both})과 일치합니다. 더 긴 이름으로 적어 주세요.")
    return matches[0]


def run_checks(env: Mapping[str, str]) -> tuple[kidsnote_auth.Session, dict[str, Any], str]:
    """Returns (kidsnote session, selected child, "database" | "page"); raises CheckFailed."""
    try:
        session = kidsnote_auth.resolve_session(
            env.get("KIDSNOTE_USERNAME"), env.get("KIDSNOTE_PASSWORD"), env.get("KIDSNOTE_SESSION_COOKIE"))
        children = kidsnote_auth.list_children(session.sessionid)
    except kidsnote_auth.AuthError as e:
        hint = kidsnote_auth.HINTS.get(e.reason, kidsnote_auth.HINTS["unexpected"])
        raise CheckFailed(e.reason, hint, str(e)) from e
    child = pick_child(children, env.get("KIDSNOTE_CHILD_NAME"))
    try:
        kind = notion_check.check_notion(env.get("NOTION_TOKEN"), env.get("NOTION_DATABASE_ID"))
    except notion_check.NotionCheckError as e:
        hint = notion_check.HINTS.get(e.reason, notion_check.HINTS["notion_unexpected"])
        raise CheckFailed(e.reason, hint, str(e)) from e
    return session, child, kind


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check kidsnote login, child name and Notion access.")
    ap.add_argument("--github", action="store_true",
                    help="GitHub Actions mode: export the session and write ok/reason/hint outputs.")
    args = ap.parse_args(argv)

    try:
        session, child, kind = run_checks(os.environ)
    except CheckFailed as e:
        reason, hint, detail = e.reason, e.hint, e.detail
    except Exception as e:  # report it (once) instead of crash-looping every run
        reason, hint, detail = "unexpected", UNEXPECTED_HINT, f"{type(e).__name__}: {e}"
    else:
        if args.github:
            print(f"::add-mask::{session.sessionid}")
        how = "fresh login" if session.source == "login" else "KIDSNOTE_SESSION_COOKIE"
        print(f"키즈노트 로그인 OK ({how})")
        if session.login_error is not None:
            err = session.login_error
            message = (f"아이디/비밀번호 로그인에 실패해 KIDSNOTE_SESSION_COOKIE로 계속합니다. "
                       f"{kidsnote_auth.HINTS.get(err.reason, kidsnote_auth.HINTS['unexpected'])} [{err.reason}]")
            print(("::warning title=키즈노트 자동 로그인 실패::" if args.github else "WARNING: ") + message)
        print(f"자녀 확인 OK: {mask_name(child.get('name'))}")
        print({
            "database": "노션 연결 OK (데이터베이스)",
            "page_with_database": "노션 연결 OK (페이지 안의 데이터베이스를 사용합니다)",
        }.get(kind, "노션 연결 OK (빈 페이지 — 첫 실행 때 그 안에 백업용 데이터베이스를 만듭니다)"))
        if args.github:
            kidsnote_auth._github_append("GITHUB_ENV", f"KIDSNOTE_SESSION_COOKIE={session.sessionid}")
            kidsnote_auth._github_append("GITHUB_OUTPUT", "ok=true")
        return 0

    print(f"사전 점검 실패 [{reason}] {detail}\n{hint}")
    if args.github:
        kidsnote_auth._github_append("GITHUB_OUTPUT", "ok=false")
        kidsnote_auth._github_append("GITHUB_OUTPUT", f"reason={reason}")
        kidsnote_auth._github_append("GITHUB_OUTPUT", f"hint={hint}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
