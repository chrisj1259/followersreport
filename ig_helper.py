"""Instagram checks via a persistent, hand-authenticated Chrome session.

Always runs as its own subprocess -- dev mode: `python ig_helper.py
<session_dir> <mode> ...`; frozen: `InstagramFollowerReport.exe --ig-helper
<session_dir> <mode> ...` (app.py intercepts that flag before starting the
UI). Keeping this out-of-process means a Playwright/Scrapling crash or a
stuck browser window can never take the main app down with it.

Modes:
  login              -- open a real, visible Chrome window pointed at a
                         dedicated automation profile under <session_dir>.
                         The user logs into Instagram by hand; this polls for
                         Instagram's `sessionid` cookie (only set after a
                         successful login) and exits once it appears. The
                         profile then stays authenticated for headless runs.
  status <users...>   -- for each username, report "active" if Instagram
                         returns a matching profile + numeric id, else
                         "unverified" (private/removed/blocked/unclear all
                         collapse to the same conservative "leave it alone").
  counts <username>   -- one profile's live follower/following totals.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

USERNAME_RE = re.compile(r"[A-Za-z0-9._]{1,30}")
LOGIN_TIMEOUT_SECONDS = 600
POLL_INTERVAL_SECONDS = 2
SESSION_MARKER = ".authenticated"
RESULT_FILE_ENV = "IFR_IG_RESULT_FILE"


def _has_session_cookie(context) -> bool:
    return any(c["name"] == "sessionid" and "instagram.com" in c["domain"] for c in context.cookies())


def _login(session_dir: Path) -> int:
    from playwright.sync_api import sync_playwright

    session_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(session_dir), channel="chrome", headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.instagram.com/")
        deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
        logged_in = False
        while time.monotonic() < deadline:
            if _has_session_cookie(context):
                logged_in = True
                break
            time.sleep(POLL_INTERVAL_SECONDS)
        if logged_in:
            time.sleep(3)  # let Instagram finish setting all post-login cookies
            (session_dir / SESSION_MARKER).touch()
        context.close()
    return 0 if logged_in else 1


def _write_result(payload: dict) -> None:
    """File IPC works in PyInstaller's windowed build, where stdout is None."""
    result_file = os.environ.get(RESULT_FILE_ENV)
    if result_file:
        Path(result_file).write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


def _friendly_name(xhr) -> str:
    for k, v in (xhr.request_headers or {}).items():
        if k.lower() == "x-fb-friendly-name":
            return v
    return ""


def _profile_user(session, username: str) -> dict | None:
    """One profile-page fetch; returns Instagram's user dict, or None if the
    page didn't return a positively-matching profile (private/removed/blocked
    responses all look like "no match" from here -- the caller's job is to
    treat that as unverified, not as "confirmed gone")."""
    page = session.fetch(f"https://www.instagram.com/{username}/", network_idle=True)
    for xhr in page.captured_xhr:
        if _friendly_name(xhr) != "PolarisProfilePageContentQuery":
            continue
        user = (json.loads(xhr.body).get("data") or {}).get("user") or {}
        if str(user.get("username", "")).lower() == username and user.get("pk"):
            return user
    return None


def _check(session_dir: Path, mode: str, usernames: list[str]) -> int:
    from scrapling.fetchers import DynamicSession

    if not session_dir.is_dir():
        _write_result({})
        return 1

    statuses = {u: "unverified" for u in usernames}
    counts: dict[str, int] = {}
    try:
        with DynamicSession(
            real_chrome=True,
            user_data_dir=str(session_dir),
            headless=True,
            capture_xhr=r"https://(?:www\.)?instagram\.com/(?:api/)?graphql.*",
        ) as session:
            if not _has_session_cookie(session.context):
                (session_dir / SESSION_MARKER).unlink(missing_ok=True)
                return 1
            for username in usernames:
                try:
                    user = _profile_user(session, username)
                except Exception:
                    continue
                if user is None:
                    continue
                if mode == "status":
                    statuses[username] = "active"
                else:
                    followers, following = user.get("follower_count"), user.get("following_count")
                    if isinstance(followers, int) and isinstance(following, int):
                        counts = {"followers": followers, "following": following}
                    break
    finally:
        _write_result(statuses if mode == "status" else counts)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: ig_helper.py <session_dir> login|status|counts [usernames...]", file=sys.stderr)
        return 2
    session_dir, mode = Path(argv[0]), argv[1]
    if mode == "login":
        return _login(session_dir)
    if mode not in ("status", "counts"):
        print(f"unknown mode: {mode}", file=sys.stderr)
        return 2
    usernames = [u.lower() for u in argv[2:] if USERNAME_RE.fullmatch(u)]
    if not usernames:
        return 2
    return _check(session_dir, mode, usernames)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
