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
  relationships <username>
                      -- every follower/following username Instagram exposes,
                         each mapped to its numeric Instagram user id so the
                         caller can detect username changes exactly.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

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


def _has_counts(user: dict) -> bool:
    return all(isinstance(user.get(k), int) for k in ("follower_count", "following_count"))


def _profile_user(session, username: str, *, need_counts: bool = False) -> dict | None:
    """One profile-page fetch; returns Instagram's user dict, or None if the
    page didn't return a positively-matching profile (private/removed/blocked
    responses all look like "no match" from here -- the caller's job is to
    treat that as unverified, not as "confirmed gone").

    need_counts makes the captured GraphQL response insufficient on its own
    unless it actually carries follower_count/following_count. Instagram has
    shipped PolarisProfilePageContentQuery payloads without them, and
    accepting one anyway made `counts` and `relationships` return empty --
    which the app then reported as "Instagram is unavailable" even though the
    web_profile_info endpoint two lines below would have answered fine.
    """
    page = session.fetch(f"https://www.instagram.com/{username}/", network_idle=True)
    for xhr in page.captured_xhr:
        if _friendly_name(xhr) != "PolarisProfilePageContentQuery":
            continue
        user = (json.loads(xhr.body).get("data") or {}).get("user") or {}
        user["pk"] = user.get("pk") or user.get("id")
        if str(user.get("username", "")).lower() != username or not user["pk"]:
            continue
        if need_counts and not _has_counts(user):
            break  # fall through to the REST endpoint, which always carries counts
        return user
    response = session.context.request.get(
        f"https://www.instagram.com/api/v1/users/web_profile_info/?{urlencode({'username': username})}",
        headers={"X-IG-App-ID": "936619743392459", "Referer": f"https://www.instagram.com/{username}/"},
    )
    if response.ok:
        user = (response.json().get("data") or {}).get("user") or {}
        user["pk"] = user.get("pk") or user.get("id")
        if not isinstance(user.get("follower_count"), int):
            user["follower_count"] = (user.get("edge_followed_by") or {}).get("count")
        if not isinstance(user.get("following_count"), int):
            user["following_count"] = (user.get("edge_follow") or {}).get("count")
        if str(user.get("username", "")).lower() == username and user["pk"]:
            return user
    return None


# Instagram decides its own page size and has repeatedly served fewer rows
# than `count` asks for. Budgeting pages against the *requested* size was the
# bug: `(expected + 199) // 200 + 5` assumed 200 rows per page, so an account
# with more than roughly 2,000 relationships exhausted the budget mid-walk and
# the whole fetch returned None -- which surfaced in the app as "Instagram
# relationship list unavailable" and took "Find the difference" and the weekly
# recheck down with it. The walk now runs until Instagram stops handing out a
# cursor, with a flat ceiling that only exists to bound a server-side loop.
MAX_RELATIONSHIP_PAGES = 600


def _relationship_users(context, pk: str, side: str, expected: int) -> dict[str, str] | None:
    """Fetch every visible page of one live relationship list.

    Returns {username: instagram_user_id}. The id is the only stable identity
    Instagram exposes -- usernames change, ids do not -- so keeping it is what
    lets the app tell "this person renamed" apart from "this person vanished
    and a stranger appeared", which no amount of username diffing can do.

    Instagram's headline count can include hidden/disabled accounts that its
    list endpoint omits. Returned relationships are still positive evidence;
    callers conservatively leave every absent saved flag untouched.
    """
    if expected == 0:
        return {}
    users: dict[str, str] = {}
    max_id: str | None = None
    seen_ids: set[str] = set()
    for _ in range(MAX_RELATIONSHIP_PAGES):
        params = {"count": "200"}
        if max_id:
            params["max_id"] = max_id
        response = context.request.get(
            f"https://www.instagram.com/api/v1/friendships/{pk}/{side}/?{urlencode(params)}",
            headers={
                "X-IG-App-ID": "936619743392459",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://www.instagram.com/",
            },
        )
        if not response.ok:
            print(f"Instagram {side} request failed: HTTP {response.status}", file=sys.stderr)
            return None
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("users"), list):
            print(f"Instagram {side} response had no user list", file=sys.stderr)
            return None
        for item in payload["users"]:
            if not isinstance(item, dict):
                continue
            username = str(item.get("username", "")).lower()
            if USERNAME_RE.fullmatch(username):
                users[username] = str(item.get("pk") or item.get("id") or "")
        next_id = payload.get("next_max_id")
        if not next_id:
            if len(users) != expected:
                print(f"Instagram {side} count differs: profile says {expected}, list shows {len(users)}",
                      file=sys.stderr)
            return users
        max_id = str(next_id)
        if max_id in seen_ids:
            print(f"Instagram {side} pagination repeated a cursor", file=sys.stderr)
            return None
        seen_ids.add(max_id)
    # Ran out of pages with a cursor still outstanding. A truncated list would
    # read as "these accounts are gone from Instagram" and get the user to flag
    # people who are fine, so refuse it outright rather than return a partial.
    print(f"Instagram {side} list exceeded {MAX_RELATIONSHIP_PAGES} pages", file=sys.stderr)
    return None


def _check(session_dir: Path, mode: str, usernames: list[str]) -> int:
    from scrapling.fetchers import DynamicSession

    if not session_dir.is_dir():
        _write_result({})
        return 1

    statuses = {u: "unverified" for u in usernames}
    counts: dict[str, int] = {}
    relationships: dict[str, dict[str, str]] = {}
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
                    user = _profile_user(session, username, need_counts=mode != "status")
                except Exception:
                    continue
                if user is None:
                    continue
                if mode == "status":
                    statuses[username] = "active"
                elif mode == "counts":
                    followers, following = user.get("follower_count"), user.get("following_count")
                    if isinstance(followers, int) and isinstance(following, int):
                        counts = {"followers": followers, "following": following}
                    break
                else:
                    follower_count, following_count = user.get("follower_count"), user.get("following_count")
                    if not (isinstance(follower_count, int) and isinstance(following_count, int)):
                        break
                    followers = _relationship_users(session.context, str(user["pk"]),
                                                    "followers", follower_count)
                    following = _relationship_users(session.context, str(user["pk"]),
                                                    "following", following_count)
                    if followers is not None and following is not None:
                        relationships = {"followers": followers, "following": following,
                                         "self_pk": str(user["pk"]),
                                         # Free: read off the same profile fetch
                                         # that produced the account id, so the
                                         # caller never needs a separate run
                                         # just for the headline totals.
                                         "counts": {"followers": follower_count,
                                                    "following": following_count}}
                    break
    finally:
        _write_result(statuses if mode == "status" else
                      (counts if mode == "counts" else relationships))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: ig_helper.py <session_dir> login|status|counts|relationships [usernames...]", file=sys.stderr)
        return 2
    session_dir, mode = Path(argv[0]), argv[1]
    if mode == "login":
        return _login(session_dir)
    if mode not in ("status", "counts", "relationships"):
        print(f"unknown mode: {mode}", file=sys.stderr)
        return 2
    usernames = [u.lower() for u in argv[2:] if USERNAME_RE.fullmatch(u)]
    if not usernames:
        return 2
    return _check(session_dir, mode, usernames)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
