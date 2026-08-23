#!/usr/bin/env python3
"""Analyze Instagram data exports (.json) and serve the report locally. End-to-end.

Stdlib only. This file is the formerly-separate analyze.py + serve.py merged
into one — `python run.py` does everything.

Usage:
    python run.py
    python run.py --check-disabled
    python run.py 9000
    python run.py --check-disabled 9000
    python run.py audit [--check-network] [--scope both] [--flag-gone]
    python run.py --help

Reads every `*.json` file in each `data/<MM-DD-YYYY>/` subfolder and routes
by filename substring to a bucket:

  *follower* / *folower*  -> followers  (people who follow me)
  *following*             -> following  (people I follow)

...but only after NON_RELATIONSHIP_FILE_MARKERS vetoes the export files that
merely *look* like relationship lists (following_hashtags.json,
pending_follow_requests.json, recently_unfollowed_profiles.json, ...). Counting
those is the single most common cause of "the app says I follow more people
than Instagram does".

Each JSON is the conventional Instagram export shape:

    {relationships_followers:  [{title, string_list_data:[{value, href, timestamp}]}]}
    {relationships_following: [{title, string_list_data:[{value, href, timestamp}]}]}
    -- or a bare list of the same items --

username is taken from string_list_data[0].value (lowercased); timestamp from
string_list_data[0].timestamp (unix seconds, preserved).

Disabled-account state is loaded from `data/disabledaccounts.json` (a flat
JSON list of usernames) on every analyze. Handles in this list are pulled out
of the "Don't follow me back" panel and surfaced under "Likely disabled /
deactivated". The UI exposes Flag buttons on rows in the NFB / DFMB panels
(POST /flag) and an Unflag button on each row in the disabled panel — keep or
remove handles from the flagged list via clicking instead of editing the JSON
file by hand. `python run.py --check-disabled` additionally walks each DFMB
handle anonymously and merges likely-deactivated Instagram pages into the
same file.

A positional port argument overrides the default 8000.

ponytail: usernames are lower-cased for set membership — defensive against
casing changes in future Instagram exports.
ponytail: bind the http server to 127.0.0.1 (IPv4 loopback) — never expose the report.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import errno
import functools
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError

RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)).resolve()
ROOT = Path(__file__).parent.resolve()
DATA = ROOT / "data"
OUT = ROOT / "dist" / "report.json"
DISABLED_FILE = DATA / "disabledaccounts.json"
DEFAULT_PORT = 8000
PROFILE_ROOT: Path | None = None
PROFILE_MANIFEST: Path | None = None
PROFILE_OUT_DIR: Path | None = None
LIVE_PROFILE_COUNTS: dict[str, dict[str, int]] = {}
IG_SESSION_DIR: Path | None = None
IG_SESSION_MARKER = ".authenticated"
_LOGIN_PROC: subprocess.Popen | None = None
_IG_HELPER_LOCK = threading.Lock()


def _set_data_paths(*, data_dir: Path, out_dir: Path, disabled_file: Path) -> None:
    """Mutate module-level DATA / OUT / DISABLED_FILE so the desktop entry
    point (app.py under pywebview+PyInstaller) can redirect every write to
    `%APPDATA%\\InstagramFollowerReport\\` without forking the analyze pipeline.

    PONYTAIL: dedicated helper rather than thread-the-path-through-every-fn.
    CLI runs keep the CWD-relative defaults untouched.
    """
    global DATA, OUT, DISABLED_FILE, ROOT
    DATA = data_dir
    OUT = out_dir / "report.json"
    DISABLED_FILE = disabled_file
    # ponytail: also rebind ROOT to out_dir.parent so run_analyze's end-of-run
    # summary print (`OUT.relative_to(ROOT)`) doesn't blow up with
    # 'is not in the subpath of' when the bundled app reroutes OUT to
    # %APPDATA%. CLI calls keep the original project-root ROOT because the
    # defaults are unchanged when this helper is never invoked. The smoke
    # test caught this exactly: report.json wrote correctly, then the print
    # crashed the analyze topline; FlagHandler._handle_import's `except
    # SystemExit` doesn't catch ValueError so the in-app Import button
    # would silently 500 the user with files staged but no UI refresh.
    ROOT = out_dir.parent


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _legacy_profile_data_exists(storage_root: Path) -> bool:
    data = storage_root / "data"
    if data.is_dir():
        for child in data.iterdir():
            if child.is_dir():
                try:
                    parse_folder_date(child.name)
                    return True
                except ValueError:
                    pass
    for flags in (storage_root / "disabledaccounts.json", data / "disabledaccounts.json"):
        try:
            raw = json.loads(flags.read_text(encoding="utf-8"))
            if isinstance(raw, list) and raw:
                return True
        except (OSError, json.JSONDecodeError):
            pass
    return False


def _read_profiles() -> dict:
    if PROFILE_MANIFEST is None:
        return {"schema_version": 1, "active_profile_id": None,
                "migration_complete": True, "profiles": []}
    try:
        manifest = json.loads(PROFILE_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise OSError(f"could not read {PROFILE_MANIFEST}: {e}") from e
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or not isinstance(manifest.get("profiles"), list)):
        raise OSError(f"unsupported or malformed profile manifest: {PROFILE_MANIFEST}")
    for profile in manifest["profiles"]:
        profile_id = profile.get("id") if isinstance(profile, dict) else None
        try:
            valid_id = (isinstance(profile_id, str) and len(profile_id) == 32
                        and uuid.UUID(profile_id).hex == profile_id)
        except ValueError:
            valid_id = False
        if not valid_id:
            raise OSError(f"malformed profile id in {PROFILE_MANIFEST}")
    return manifest


def _profile_dir(profile_id: str) -> Path:
    assert PROFILE_ROOT is not None
    return PROFILE_ROOT / profile_id


def _activate_profile(profile_id: str | None) -> None:
    assert PROFILE_OUT_DIR is not None
    if profile_id is None:
        return
    profile = _profile_dir(profile_id)
    (profile / "data").mkdir(parents=True, exist_ok=True)
    flags = profile / "disabledaccounts.json"
    if not flags.exists():
        flags.write_text("[]", encoding="utf-8")
    _set_data_paths(data_dir=profile / "data", out_dir=PROFILE_OUT_DIR, disabled_file=flags)


def enable_profile_mode(storage_root: Path, out_dir: Path) -> None:
    """Enable per-account storage."""
    global PROFILE_ROOT, PROFILE_MANIFEST, PROFILE_OUT_DIR, IG_SESSION_DIR
    PROFILE_ROOT = storage_root / "profiles"
    PROFILE_MANIFEST = storage_root / "profiles.json"
    PROFILE_OUT_DIR = out_dir
    IG_SESSION_DIR = storage_root / "ig-session"
    LIVE_PROFILE_COUNTS.clear()
    invalidate_relationships_cache()
    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    _set_data_paths(data_dir=PROFILE_ROOT, out_dir=out_dir,
                    disabled_file=PROFILE_ROOT / ".inactive-disabledaccounts.json")
    if not PROFILE_MANIFEST.exists():
        legacy = _legacy_profile_data_exists(storage_root)
        _atomic_json(PROFILE_MANIFEST, {
            "schema_version": 1,
            "active_profile_id": None,
            "migration_complete": not legacy,
            "profiles": [],
        })
    manifest = _read_profiles()
    active = manifest.get("active_profile_id")
    if any(p.get("id") == active for p in manifest["profiles"] if isinstance(p, dict)):
        _activate_profile(active)


def _profiles_payload() -> dict:
    if PROFILE_MANIFEST is None:
        return {"profile_mode": False, "active_profile_id": None, "profiles": [],
                "legacy_needs_username": False}
    manifest = _read_profiles()
    active = manifest.get("active_profile_id")
    if not any(p.get("id") == active for p in manifest["profiles"] if isinstance(p, dict)):
        active = None
    return {
        "profile_mode": True,
        "active_profile_id": active,
        "profiles": manifest["profiles"],
        "legacy_needs_username": not bool(manifest.get("migration_complete")),
    }


def _normalize_profile_name(value: object) -> str:
    name = str(value or "").strip()
    if name.startswith("@"):
        name = name[1:]
    name = name.lower()
    if not re.fullmatch(r"[a-z0-9._]+", name):
        raise ValueError("username must contain only letters, numbers, periods, and underscores")
    return name


def _refresh_active_report() -> None:
    has_snapshot = False
    if DATA.is_dir():
        for child in DATA.iterdir():
            if not child.is_dir():
                continue
            try:
                parse_folder_date(child.name)
                has_snapshot = True
                break
            except ValueError:
                pass
    if has_snapshot:
        try:
            run_analyze(argparse.Namespace(check_disabled=False))
        except SystemExit as e:
            raise OSError(f"could not analyze profile: {e}") from e
    else:
        _emit_stub_report()


def _create_profile(name_value: object, migrate_legacy: bool = False) -> dict:
    if PROFILE_MANIFEST is None or PROFILE_ROOT is None:
        raise ValueError("profile mode is disabled")
    name = _normalize_profile_name(name_value)
    manifest = _read_profiles()
    if any(str(p.get("name", "")).lower() == name for p in manifest["profiles"]):
        raise ValueError("that username already exists")
    needs_migration = not bool(manifest.get("migration_complete"))
    if needs_migration and not migrate_legacy:
        raise RuntimeError("legacy data needs a username before adding another account")
    if migrate_legacy and not needs_migration:
        raise ValueError("legacy data has already been migrated")

    profile_id = uuid.uuid4().hex
    target = _profile_dir(profile_id)
    staging = PROFILE_ROOT / f".{profile_id}.tmp"
    try:
        data_target = staging / "data"
        data_target.mkdir(parents=True)
        if migrate_legacy:
            legacy_data = PROFILE_ROOT.parent / "data"
            if legacy_data.is_dir():
                for child in legacy_data.iterdir():
                    if not child.is_dir():
                        continue
                    try:
                        parse_folder_date(child.name)
                    except ValueError:
                        continue
                    shutil.copytree(child, data_target / child.name)
        flags_target = staging / "disabledaccounts.json"
        flags_source = PROFILE_ROOT.parent / "disabledaccounts.json"
        nested_flags = PROFILE_ROOT.parent / "data" / "disabledaccounts.json"
        if migrate_legacy and flags_source.is_file():
            shutil.copy2(flags_source, flags_target)
        elif migrate_legacy and nested_flags.is_file():
            shutil.copy2(nested_flags, flags_target)
        else:
            flags_target.write_text("[]", encoding="utf-8")
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(target, ignore_errors=True)
        raise

    old_active = manifest.get("active_profile_id")
    manifest["profiles"].append({"id": profile_id, "name": name})
    manifest["active_profile_id"] = profile_id
    if migrate_legacy:
        manifest["migration_complete"] = True
    try:
        _activate_profile(profile_id)
        _refresh_active_report()
        _atomic_json(PROFILE_MANIFEST, manifest)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        if old_active:
            _activate_profile(old_active)
            _refresh_active_report()
        else:
            _set_data_paths(data_dir=PROFILE_ROOT, out_dir=PROFILE_OUT_DIR,
                            disabled_file=PROFILE_ROOT / ".inactive-disabledaccounts.json")
            _emit_stub_report()
        raise
    return _profiles_payload()


def _select_profile(profile_id: object) -> dict:
    if PROFILE_MANIFEST is None:
        raise ValueError("profile mode is disabled")
    profile_id = str(profile_id or "")
    manifest = _read_profiles()
    if not any(p.get("id") == profile_id for p in manifest["profiles"] if isinstance(p, dict)):
        raise KeyError("unknown profile")
    old_active = manifest.get("active_profile_id")
    _activate_profile(profile_id)
    try:
        _refresh_active_report()
        manifest["active_profile_id"] = profile_id
        _atomic_json(PROFILE_MANIFEST, manifest)
    except Exception:
        if old_active:
            _activate_profile(old_active)
            _refresh_active_report()
        else:
            _set_data_paths(data_dir=PROFILE_ROOT, out_dir=PROFILE_OUT_DIR,
                            disabled_file=PROFILE_ROOT / ".inactive-disabledaccounts.json")
            _emit_stub_report()
        raise
    return _profiles_payload()


# ponytail: disabled-account sniff is heuristic — Instagram can change their
# markup tomorrow. We never silently drop handles on network failure; an
# unclear verdict keeps the user in the active list.
DISABLED_MARKERS = (
    "page isn't available",
    "page not available",
    "user not found",
    "user not exist",
    "has been deactivated",
    "isn't available",
    "no longer available",
)
# Anonymous profile views get login-walled, and the wall is a normal 200 with
# full page furniture. Without these markers the wall is indistinguishable
# from a live profile, so a gone account comes back "active".
LOGIN_WALL_MARKERS = (
    "login_and_signup_page",
    "loginform",
    'name="username"',
    "log in to instagram",
    "/accounts/login/?next=",
)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ---------- disabled-account persistence (data/disabledaccounts.json) ----------


def _read_flagged_file(path: Path) -> set[str]:
    """Read one flat JSON username list. Missing/malformed -> empty set."""
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(raw, list):
        return set()
    return {str(u).strip().lower() for u in raw if u}


def _read_flagged() -> set[str]:
    """Read the active profile's saved disabled-account handles."""
    return _read_flagged_file(DISABLED_FILE)


def _write_flagged_file(path: Path, handles: set[str]) -> None:
    _atomic_json(path, sorted(handles))


def _write_flagged(handles: set[str]) -> None:
    """Atomic write via temp-file rename so a crash mid-write can't corrupt
    the user's curated list."""
    _write_flagged_file(DISABLED_FILE, handles)


def add_flagged(username: str) -> list[str]:
    """Append username to data/disabledaccounts.json (lowercased, deduped). Atomic."""
    handles = _read_flagged()
    handles.add(username.strip().lower())
    _write_flagged(handles)
    return sorted(handles)


def unflag(username: str) -> list[str]:
    """Remove username from data/disabledaccounts.json if present. Atomic."""
    handles = _read_flagged()
    handles.discard(username.strip().lower())
    _write_flagged(handles)
    return sorted(handles)


# ---------- disabled-account network detection (opt-in) ----------


def looks_disabled(username: str, timeout: float = 8.0) -> bool | None:
    """GET instagram.com/<user> anonymous, sniff for disabled-account markers.

    True: markers present or page is bare (no og:description meta).
    False: profile page looks normal.
    None: connection was blocked/timed out — caller keeps the user active."""
    req = urllib.request.Request(
        f"https://instagram.com/{username}",
        headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html = r.read(120_000).decode("utf-8", "ignore")
    except HTTPError as e:
        if e.code in (404, 410):
            return True
        return None
    except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError):
        return None

    body = html.lower()
    if any(m in body for m in DISABLED_MARKERS):
        return True
    # Instagram's anonymous login wall is a full page WITH an og:description,
    # so the check below read it as a healthy profile and reported deleted
    # accounts as active -- quietly shrinking the very list the user is
    # hunting through. A wall is "we were not allowed to look", which is
    # exactly what None means here.
    if any(m in body for m in LOGIN_WALL_MARKERS):
        return None
    if 'property="og:description"' in body:
        return False
    return None


def ig_authenticated() -> bool:
    """Whether a saved, hand-authenticated Instagram browser session exists."""
    return IG_SESSION_DIR is not None and (IG_SESSION_DIR / IG_SESSION_MARKER).is_file()


def _ig_deps_available() -> bool:
    """In a frozen build scrapling/playwright are always bundled; in source
    mode they're an optional extra (`pip install "scrapling[fetchers]"`) only
    needed for this one feature, so check rather than assume."""
    if getattr(sys, "frozen", False):
        return True
    import importlib.util
    return all(importlib.util.find_spec(package) is not None for package in (
        "scrapling", "playwright", "browserforge", "apify_fingerprint_datapoints",
        "curl_cffi", "patchright", "msgspec", "anyio", "protego",
    ))


def _chrome_available() -> bool:
    if shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("google-chrome-stable"):
        return True
    if sys.platform == "win32":
        return any(
            (Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe").is_file()
            for root in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                         os.environ.get("LOCALAPPDATA")) if root
        )
    if sys.platform == "darwin":
        return (Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome").is_file()
                or (Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome").is_file())
    return False


def _ig_helper_command(mode: str, args: list[str]) -> list[str] | None:
    if IG_SESSION_DIR is None:
        return None
    if getattr(sys, "frozen", False):
        return [sys.executable, "--ig-helper", str(IG_SESSION_DIR), mode, *args]
    helper = RESOURCE_ROOT / "ig_helper.py"
    if not helper.is_file():
        return None
    return [sys.executable, str(helper), str(IG_SESSION_DIR), mode, *args]


def instagram_login_state() -> str:
    """'unavailable' (missing setup, dependencies, or Chrome), 'in_progress',
    'logged_in', or 'logged_out'."""
    if IG_SESSION_DIR is None or not _ig_deps_available() or not _chrome_available():
        return "unavailable"
    if _LOGIN_PROC is not None and _LOGIN_PROC.poll() is None:
        return "in_progress"
    return "logged_in" if ig_authenticated() else "logged_out"


def start_instagram_login() -> str:
    """Launch the visible Chrome login window as a detached subprocess."""
    global _LOGIN_PROC
    command = _ig_helper_command("login", [])
    if command is None or not _ig_deps_available():
        raise RuntimeError("Instagram login is not available")
    if not _chrome_available():
        raise RuntimeError("Google Chrome is not installed")
    if _LOGIN_PROC is not None and _LOGIN_PROC.poll() is None:
        return "in_progress"
    _LOGIN_PROC = subprocess.Popen(
        command,
        cwd=IG_SESSION_DIR.parent if IG_SESSION_DIR else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return "in_progress"


def stop_instagram_login() -> None:
    """Terminate an in-flight visible login helper during app shutdown."""
    global _LOGIN_PROC
    proc, _LOGIN_PROC = _LOGIN_PROC, None
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _scrapling_result(mode: str, args: list[str], timeout: int) -> dict | None:
    """Run the bundled helper against the saved, hand-authenticated session."""
    command = _ig_helper_command(mode, args)
    if command is None or not ig_authenticated():
        return None
    fd, result_path = tempfile.mkstemp(prefix="ifr-ig-", suffix=".json")
    os.close(fd)
    try:
        env = os.environ.copy()
        env["IFR_IG_RESULT_FILE"] = result_path
        # ponytail: one saved login means one global lock; split by session only
        # if the app ever supports simultaneous Instagram logins.
        with _IG_HELPER_LOCK:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
                env=env,
            )
        payload = json.loads(Path(result_path).read_text(encoding="utf-8"))
        if result.returncode or not isinstance(payload, dict):
            raise ValueError("checker failed")
        return payload
    except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError):
        print("warning: authenticated Instagram checker unavailable", file=sys.stderr)
        return None
    finally:
        Path(result_path).unlink(missing_ok=True)


def _authenticated_statuses(usernames: list[str]) -> dict[str, bool | None] | None:
    """Use the saved, hand-authenticated Instagram session when it is available.

    Routes through _authenticated_raw_statuses() so a large flagged list gets
    the same AUTH_STATUS_BATCH chunking instead of one unbounded subprocess call.
    """
    statuses = _authenticated_raw_statuses(usernames)
    if statuses is None:
        return None
    return {u: False if statuses.get(u) == "active" else None for u in usernames}


# One profile page per handle through a real headless Chrome is a few seconds
# of work, so a whole following list has to be batched: a single subprocess
# call for 100 handles would need a ~35 minute timeout with no progress and no
# partial result if it died at handle 99.
AUTH_STATUS_BATCH = 20


def _authenticated_raw_statuses(usernames: list[str], *,
                                progress=None) -> dict[str, str] | None:
    """{username: 'active' | 'unverified'} from the saved login, or None if the
    authenticated checker is unavailable entirely.

    _authenticated_statuses() collapses 'unverified' to None on purpose — it
    exists to un-flag accounts and must never claim a profile is gone. The
    count audit needs the distinction kept: 'unverified' is precisely the
    shortlist of handles worth looking at by hand.
    """
    if not usernames:
        return {}
    out: dict[str, str] = {}
    for i in range(0, len(usernames), AUTH_STATUS_BATCH):
        batch = usernames[i:i + AUTH_STATUS_BATCH]
        statuses = _scrapling_result("status", batch, 30 + 20 * len(batch))
        if statuses is None:
            # Session died or the helper is missing. Partial results are still
            # worth returning; nothing here is treated as evidence of absence.
            return out or None
        for username in batch:
            out[username] = "active" if statuses.get(username) == "active" else "unverified"
        if progress:
            progress(len(out), len(usernames))
    return out


def _authenticated_profile_counts(username: str) -> dict[str, int] | None:
    counts = _scrapling_result("counts", [username], 90)
    if counts is None:
        return None
    if all(isinstance(counts.get(k), int) and counts[k] >= 0 for k in ("followers", "following")):
        return {"followers": counts["followers"], "following": counts["following"]}
    return None


# One relationship walk costs a Chrome launch, a profile page load and a
# request per ~23 accounts -- about 16 seconds for a 650-follower account, and
# it grows with the account. Clicking "Find the difference" twice in a row, or
# running a scan right after the weekly recheck, used to pay that twice for an
# answer that cannot have changed. Short TTL: long enough to make a re-run
# instant, short enough that a deliberate re-check still sees new data.
#
# ponytail: single-entry cache keyed by username; the app checks one account at
# a time. Make it a dict if simultaneous logins ever land.
RELATIONSHIPS_TTL_SECONDS = 300
_RELATIONSHIPS_CACHE: dict[str, tuple[float, dict[str, dict[str, str]]]] = {}


def invalidate_relationships_cache() -> None:
    _RELATIONSHIPS_CACHE.clear()


def _relationships_timeout() -> int:
    """Seconds to allow one full relationship walk.

    The old flat 180s was a bug for anyone with a real following: paging a few
    thousand accounts through a headless browser takes minutes, the subprocess
    got killed mid-walk, and the app reported "Instagram relationship list
    unavailable" for a request that was working fine. Scale off the live totals
    when they are already cached.

    ponytail: linear in list size, floor 5 min / ceiling 30 min. Revisit only
    if Instagram's per-page latency changes by an order of magnitude.
    """
    try:
        counts = cached_live_profile_counts() or {}
    except OSError:
        counts = {}
    total = sum(v for v in counts.values() if isinstance(v, int))
    return max(300, min(1800, 120 + total // 20))


def _authenticated_relationships(username: str) -> dict[str, dict[str, str]] | None:
    """Every follower/following username Instagram currently exposes, each
    mapped to that account's numeric Instagram user id.

    The id is the whole point: usernames are the only handle the data export
    carries, and they change. The id does not, so a username that disappears
    from your export's point of view can be positively identified as "same
    person, new handle" instead of being guessed at from follow timestamps.
    """
    cached = _RELATIONSHIPS_CACHE.get(username)
    if cached and time.time() - cached[0] < RELATIONSHIPS_TTL_SECONDS:
        return cached[1]
    payload = _scrapling_result("relationships", [username], _relationships_timeout())
    if payload is None:
        return None
    out: dict[str, dict[str, str]] = {}
    for side in ("followers", "following"):
        value = payload.get(side)
        if not isinstance(value, dict):
            return None
        if not all(isinstance(u, str) and isinstance(pk, str) for u, pk in value.items()):
            return None
        out[side] = value
    # The helper already read the profile to get the account id, so the live
    # totals came back with it for free. Publishing them here means the stat
    # cards and the ledger no longer need their own Chrome launch.
    counts = payload.get("counts")
    if isinstance(counts, dict):
        live = {k: counts.get(k) for k in ("followers", "following")}
        if all(isinstance(v, int) and v >= 0 for v in live.values()):
            active = _profiles_payload()["active_profile_id"]
            if active:
                LIVE_PROFILE_COUNTS[active] = live
    _RELATIONSHIPS_CACHE[username] = (time.time(), out)
    return out


def _live_profile_counts() -> dict[str, int] | None:
    payload = _profiles_payload()
    active = payload["active_profile_id"]
    if active is None:
        return None
    if active in LIVE_PROFILE_COUNTS:
        return LIVE_PROFILE_COUNTS[active]
    profile = next(p for p in payload["profiles"] if p.get("id") == active)
    counts = _authenticated_profile_counts(profile["name"])
    if counts is not None:
        LIVE_PROFILE_COUNTS[active] = counts
    return counts


def cached_live_profile_counts() -> dict[str, int] | None:
    """Live counts ONLY if a previous /live-counts call already fetched them.

    _live_profile_counts() can spawn a headless Chrome and block for up to 90
    seconds. The server is single-threaded, so calling it from the count audit
    would stall the whole page on load. The UI asks /live-counts on every load
    anyway; this reads whatever that left behind and otherwise reports unknown.
    """
    active = _profiles_payload()["active_profile_id"]
    return LIVE_PROFILE_COUNTS.get(active) if active else None


def recheck_flagged_accounts_by_network(*, timeout: float = 4.0,
                                         authenticated: bool = False) -> list[str]:
    """Unflag saved handles only when Instagram positively shows an active profile."""
    flagged = _read_flagged()
    if not flagged:
        return []

    usernames = sorted(flagged)
    if authenticated:
        statuses = _authenticated_statuses(usernames)
        verdicts = [statuses[u] for u in usernames] if statuses is not None else [None] * len(usernames)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(usernames))) as pool:
            verdicts = list(pool.map(lambda u: looks_disabled(u, timeout), usernames))

    reenabled = [u for u, verdict in zip(usernames, verdicts) if verdict is False]
    for username, verdict in zip(usernames, verdicts):
        status = "re-enabled" if verdict is False else ("disabled" if verdict is True else "unchanged")
        print(f"  [saved flag] @{username}: {status}", flush=True)
    if reenabled:
        _write_flagged(flagged - set(reenabled))
        print(f"restored {len(reenabled)} re-enabled account(s)", flush=True)
    return reenabled


def filter_disabled_accounts_by_network(snapshot: dict, *, delay: float = 0.25,
                                        renamed: dict[str, str] | None = None) -> None:
    """For each handle in 'dont_follow_me_back', fetch their Instagram page
    and merge detected-disabled entries into 'disabled_accounts'. Heuristic
    only; never silently drops handles on network failure (unklar verdicts
    stay in the active list). Handles known to have been renamed are skipped —
    their old page 404s, which this sniff would misread as deactivated."""
    label = snapshot["date_folder"]
    items = snapshot["dont_follow_me_back"]
    renamed = renamed or {}
    active, disabled = [], []
    for i, u in enumerate(items):
        if u["username"] in renamed:
            print(f"  [{label}] {i + 1}/{len(items)} @{u['username']}: renamed -> @{renamed[u['username']]}", flush=True)
            active.append(u)
            continue
        verdict = looks_disabled(u["username"])
        tag = "disabled" if verdict is True else ("active" if verdict is False else "?")
        print(f"  [{label}] {i + 1}/{len(items)} @{u['username']}: {tag}", flush=True)
        if verdict is True:
            disabled.append(u)
        else:
            active.append(u)
        if delay > 0:
            time.sleep(delay)
    snapshot["dont_follow_me_back"] = active
    snapshot["counts"]["dont_follow_me_back"] = len(active)
    existing = {d["username"] for d in snapshot.get("disabled_accounts", [])}
    merged = list(snapshot.get("disabled_accounts", []))
    for d in disabled:
        if d["username"] not in existing:
            existing.add(d["username"])
            merged.append(d)
    snapshot["disabled_accounts"] = merged
    snapshot["counts"]["disabled"] = len(merged)


# ---------- parsing ----------


def _parse_multipart(content_type: str, body: bytes):
    """Tiny stdlib multipart/form-data parser. Returns (fields, files).

    fields: {name: decoded_string} for non-file form fields.
    files:  {name: [raw_bytes, ...]} for uploaded files (Content-Disposition
            has filename=). A list, not a single value: Instagram splits a
            large export across followers_1.json, followers_2.json, ... and
            keeping only the last part silently dropped every follower after
            the first file.

    ponytail: hand-rolled because cgi.FieldStorage is deprecated in 3.13. Hand-
    rolled is also much smaller — RFC 2046 favors simplicity here. Browsers
    pick a random boundary that doesn't collide with payload bytes, so a
    literal split is safe in practice.
    """
    m = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', content_type)
    if not m:
        return None
    delim = b"--" + (m.group(1) or m.group(2)).encode()
    fields: dict[str, str] = {}
    files: dict[str, list[bytes]] = {}
    for part in body.split(delim):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue  # leading empty, trailing terminator
        head, _, data = part.partition(b"\r\n\r\n")
        if not head:
            continue
        data = data.rstrip(b"\r\n")
        name_m = re.search(rb'name="([^"]+)"', head)
        if not name_m:
            continue
        name = name_m.group(1).decode("utf-8", "replace")
        if b'filename=' in head:
            files.setdefault(name, []).append(data)
        else:
            fields[name] = data.decode("utf-8", "replace")
    return fields, files


def parse_folder_date(name: str) -> datetime:
    # ponytail: strict MM-DD-YYYY only. Two guards: (1) YYYY-MM-DD is
    # rejected so a mistyped date the user *thinks* is unambiguous doesn't
    # land in the wrong folder; (2) any deviation from `DD-DD-DDDD`
    # 2-digit components is rejected too — Python's %m-%d-%Y strptime is
    # permissive ('7-15-2026' parses fine) but the user and README both
    # ask for the strict form. One root-cause tighten here covers both the
    # CLI flow and the browser POST /import flow.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", name):
        raise ValueError(
            f"date must be in MM-DD-YYYY format (got YYYY-MM-DD {name!r}; "
            f"e.g. MM-DD-YYYY would be 07-15-2026)"
        )
    if not re.fullmatch(r"\d{2}-\d{2}-\d{4}", name):
        raise ValueError(f"date must be in MM-DD-YYYY format (got {name!r})")
    try:
        return datetime.strptime(name, "%m-%d-%Y")
    except ValueError:
        raise ValueError(f"date must be in MM-DD-YYYY format (got {name!r})")


def username_from_href(href: str) -> str:
    """Extract username from an Instagram profile URL.

    Handles instagram.com/_u/<user> (modern redirect), instagram.com/<user>
    (direct), and a few minor variants. Returns '' if no username segment
    can be found. Lowercased.
    """
    if not href:
        return ""
    parts = [p for p in href.strip().rstrip("/").split("/") if p]
    # Drop protocol tokens and the literal 'instagram.com' / 'www.instagram.com' host.
    host_tokens = {"https:", "http:", "www.instagram.com", "instagram.com"}
    kept = [p for p in parts if p.lower() not in host_tokens]
    if not kept:
        return ""
    # Some IG exports use '/_u/<user>' — the username is the last segment, but if
    # the last segment is literally '_u', look at the prior segment.
    if kept[-1].lower() == "_u" and len(kept) >= 2:
        return kept[-2].lower()
    return kept[-1].lower()


# Instagram's export ships far more than followers_1.json / following.json in
# the same folder: close_friends.json, blocked_profiles.json,
# pending_follow_requests.json, recently_unfollowed_profiles.json and — the
# expensive one — following_hashtags.json. A naive `"following" in name` router
# files every hashtag you follow into the following bucket, and the headline
# count then overshoots instagram.com by exactly the number of hashtags you
# follow. These markers veto a filename before follower/following is consulted.
NON_RELATIONSHIP_FILE_MARKERS = (
    "hashtag", "close_friend", "blocked", "restricted", "pending",
    "request", "unfollowed", "hide_story", "removed_suggest", "suggested",
    "dismissed", "recently_followed",
)

# Followed hashtags and locations live at /explore/tags/<tag> and
# /explore/locations/<id>; a real person is at /<username>. Belt-and-braces for
# the filename veto above, since an export renamed by hand can still smuggle a
# hashtag list into a followers/following bucket.
NON_PROFILE_HREF_MARKER = "/explore/"


def bucket_for_filename(name: str) -> str | None:
    """Route an export filename to 'followers', 'following', or None.

    Single source of truth for both read_snapshot (which globs a staged
    snapshot folder) and _stage_snapshot (which picks files out of a raw
    export folder), so the two can never disagree about what counts.
    """
    n = name.lower()
    if any(marker in n for marker in NON_RELATIONSHIP_FILE_MARKERS):
        return None
    if "follower" in n or "folower" in n:
        return "followers"
    if "following" in n:
        return "following"
    return None


def load_json_records(path: Path) -> list[dict]:
    """Read a conventional Instagram JSON export and return [{'username','ts'}, ...].

    Tolerates three top-level shapes:
      1) {relationships_followers: [...]} / {relationships_following: [...]}
      2) bare list of items
      3) any dict whose first list-valued key contains 'string_list_data' items

    Items look like {title, string_list_data: [{value, href, timestamp}]}.
    username is taken from string_list_data[0].value (lowercased); timestamp
    from string_list_data[0].timestamp (unix seconds). Per-file dedup by username.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    # Unwrap a top-level dict that holds the actual list under e.g. relationships_following.
    if isinstance(raw, dict):
        unfolded: list = []
        for v in raw.values():
            if (
                isinstance(v, list)
                and v
                and isinstance(v[0], dict)
                and "string_list_data" in v[0]
            ):
                unfolded = v
                break
        if not unfolded:
            return []
        raw = unfolded
    if not isinstance(raw, list):
        return []

    seen: set[str] = set()
    out: list[dict] = []
    for rec in raw:
        if not isinstance(rec, dict):
            continue
        sld = rec.get("string_list_data") or []
        if not isinstance(sld, list) or not sld:
            continue
        # A followed hashtag/location is not a person. Its 'value' is the plain
        # tag name, indistinguishable from a username, so the href is the only
        # reliable tell — drop the record before it can inflate a count.
        if any(NON_PROFILE_HREF_MARKER in str(e.get("href") or "")
               for e in sld if isinstance(e, dict)):
            continue
        u, ts = "", None
        # Primary: the modern 'value' field on string_list_data entries.
        for entry in sld:
            if not isinstance(entry, dict):
                continue
            v = (entry.get("value") or "").strip().lower()
            if v and not u:
                u = v
            t = entry.get("timestamp")
            if t is not None and ts is None:
                ts = t
        # Fallback A: parse username out of an 'href' field (older IG exports
        # ship only href + timestamp, with no 'value').
        if not u:
            for entry in sld:
                if not isinstance(entry, dict):
                    continue
                extracted = username_from_href(entry.get("href") or "")
                if extracted:
                    u = extracted
                    break
        # Fallback B: the per-record 'title' field is always populated by IG and
        # is itself the canonical handle.
        if not u:
            title = (rec.get("title") or "").strip().lower()
            if title:
                u = title
        if u and u not in seen:
            seen.add(u)
            out.append({"username": u, "ts": ts})
    return out


def to_set(records: list[dict]) -> set[str]:
    return {r["username"] for r in records}


def by_username(records: list[dict]) -> dict[str, dict]:
    return {r["username"]: r for r in records}


def by_ts_desc(records: list[dict]) -> list[dict]:
    return sorted(records, key=lambda r: r.get("ts") or 0, reverse=True)


# ---------- username changes ----------

# Instagram rewrites a deleted account's handle to `__deleted__<hash>`, so a
# "rename" landing on one is proof of deletion, not of a live new handle.
TOMBSTONE_PREFIX = "__deleted__"


def _resolve_chain(chain: dict[str, str]) -> dict[str, str]:
    """Follow a -> b -> c to the end. The `seen` guard stops a reused handle
    from looping forever."""
    resolved: dict[str, str] = {}
    for old in chain:
        seen, new = {old}, chain[old]
        while new in chain and new not in seen:
            seen.add(new)
            new = chain[new]
        if new != old:
            resolved[old] = new
    return resolved


# ---------- identity ledger (data/identities.json) ----------
#
# The data export carries usernames and nothing else, so by handle alone a
# rename is indistinguishable from "they left and a stranger arrived".
# detect_renames() infers it from a shared follow timestamp, which needs two
# snapshots on disk and a timestamp that happens to be unique on both sides --
# it cannot see a rename that happened before your first import or after your
# most recent export.
#
# Instagram's live relationship list carries the numeric user id, which never
# changes. Recording id -> username on every live fetch turns rename detection
# from a heuristic into a lookup: same id, different username, and the app
# knows for certain. This ledger is what makes that persist across runs.


def _identity_path() -> Path:
    return DISABLED_FILE.with_name("identities.json")


def _read_identities() -> dict:
    """{"ids": {instagram_id: username}, "renames": {old_handle: new_handle}}"""
    try:
        payload = json.loads(_identity_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    ids = payload.get("ids")
    renames = payload.get("renames")
    return {
        "ids": ids if isinstance(ids, dict) else {},
        "renames": renames if isinstance(renames, dict) else {},
    }


def known_renames() -> dict[str, str]:
    """Every recorded username change, each old handle resolved to the handle
    the account carries now."""
    return _resolve_chain(_read_identities()["renames"])


def record_live_identities(relationships: dict[str, dict[str, str]]) -> dict[str, str]:
    """Fold one live relationship fetch into the ledger. Returns the renames
    this fetch newly proved, {old: new}.

    Two directions of evidence, both exact:
      - a known id now answering to a different username  -> that is a rename
      - a username now answering to a *different* id      -> the old handle was
        released and re-registered by someone else, so any rename recorded
        against it is stale and gets dropped rather than left to silently
        rewrite an unrelated person's row.
    """
    store = _read_identities()
    ids, renames = dict(store["ids"]), dict(store["renames"])
    live: dict[str, str] = {}
    for side in ("followers", "following"):
        for username, pk in relationships.get(side, {}).items():
            if pk:
                live[pk] = username

    fresh: dict[str, str] = {}
    for pk, username in live.items():
        previous = ids.get(pk)
        if previous and previous != username:
            fresh[previous] = username
            renames[previous] = username
        ids[pk] = username

    # A handle that is live right now is somebody's current handle, so any
    # rename recorded *away* from it is stale -- either the person renamed
    # back, or the handle was released and re-registered by someone else.
    # Leaving it would silently rewrite an unrelated person's row.
    for username in live.values():
        if username in renames and username not in fresh:
            renames.pop(username, None)

    if ids != store["ids"] or renames != store["renames"]:
        try:
            _atomic_json(_identity_path(), {"ids": ids, "renames": renames})
        except OSError as e:
            print(f"warning: could not update {_identity_path().name}: {e}", file=sys.stderr)
    return _resolve_chain(fresh)


def detect_renames(prev: list[dict], curr: list[dict]) -> dict[str, str]:
    """Map old_handle -> new_handle for people who changed their username.

    The export carries no user id, so by handle alone a rename is
    indistinguishable from "they unfollowed me AND a stranger followed me".
    The follow *timestamp* survives a rename though (the relationship never
    broke), so a handle that vanished and a handle that appeared sharing an
    exact timestamp are the same person.

    ponytail: requires the timestamp to be unique on both sides of the diff.
    Two unrelated people acting in the same unix second would otherwise pair
    up; ambiguous timestamps are simply left unmatched (old behavior).
    """
    p, c = by_username(prev), by_username(curr)
    gone = {u: p[u]["ts"] for u in p.keys() - c.keys() if p[u].get("ts")}
    fresh_by_ts: dict[int, list[str]] = {}
    for u in c.keys() - p.keys():
        if c[u].get("ts"):
            fresh_by_ts.setdefault(c[u]["ts"], []).append(u)
    gone_ts = list(gone.values())
    return {
        old: fresh_by_ts[ts][0]
        for old, ts in gone.items()
        if gone_ts.count(ts) == 1 and len(fresh_by_ts.get(ts, ())) == 1
    }


def renames_across(raw_pairs: list[tuple[str, list[dict], list[dict]]]) -> dict[str, str]:
    """Every rename seen across consecutive snapshots, each old handle resolved
    to the *current* one. People rename repeatedly (a -> b -> c), so chains are
    followed to the end; the `seen` guard stops a handle that got reused."""
    chain: dict[str, str] = {}
    for (_, p_f, p_g), (_, c_f, c_g) in zip(raw_pairs, raw_pairs[1:]):
        chain.update(detect_renames(p_f, c_f))
        chain.update(detect_renames(p_g, c_g))
    return _resolve_chain(chain)


# ---------- per-snapshot ----------


def read_snapshot(folder: Path, flagged: set[str]) -> tuple[dict, list[dict], list[dict]]:
    """Read every *.json file in this dated folder and bucket its records by
    filename substring. Ponytail: substring match handles 'folowers.json',
    'following_v2.json', 'relationships_followers.json', etc.

    All accounts in `flagged` (data/disabledaccounts.json) are pulled out of
    every relationship panel and surfaced in 'disabled_accounts' instead.
    Headline totals and returned lists still reflect the unfiltered export.
    """
    buckets: dict[str, list[dict]] = {"followers": [], "following": []}
    # ponytail: every JSON in the folder is reported in source_files — including
    # the ones that were skipped — so `run.py audit` can show a user exactly
    # which files fed which headline number instead of leaving them to guess.
    source_files: list[dict] = []
    for f in sorted(folder.glob("*.json")):
        recs = load_json_records(f)
        bucket = bucket_for_filename(f.name)
        source_files.append({"name": f.name, "bucket": bucket, "records": len(recs)})
        if not recs or bucket is None:
            # ponytail: unrecognized exports are silently ignored — better than
            # raising on Instagram's ever-growing export surface.
            continue
        buckets[bucket].extend(recs)

    # Records as read off disk, before per-username dedupe. followers_1.json +
    # followers_2.json legitimately overlap on large accounts, so the gap
    # between this and the unique count is worth reporting rather than hiding.
    read_counts = {k: len(v) for k, v in buckets.items()}

    raw: dict[str, list[dict]] = {"followers": [], "following": []}
    flagged_by_username: dict[str, dict] = {}
    for k, recs in buckets.items():
        unique = list(by_username(recs).values())
        raw[k] = unique
        buckets[k] = [r for r in unique if r["username"] not in flagged]
        for r in unique:
            if r["username"] in flagged:
                flagged_by_username.setdefault(r["username"], r)

    raw_fset, raw_gset = to_set(raw["followers"]), to_set(raw["following"])
    fset, gset = to_set(buckets["followers"]), to_set(buckets["following"])
    by_u_f = by_username(buckets["followers"])
    by_u_g = by_username(buckets["following"])
    flagged_records = list(flagged_by_username.values())

    summary = {
        "date_folder": folder.name,
        "date_iso": parse_folder_date(folder.name).date().isoformat(),
        "counts": {
            "followers":          len(raw_fset),
            "following":          len(raw_gset),
            "active_followers":   len(fset),
            "active_following":   len(gset),
            "not_followed_back":  len(fset - gset),
            "dont_follow_me_back":len(gset - fset),
            "mutuals":            len(fset & gset),
            "flagged":            len(flagged_records),
            # Reconciliation trail: export records read -> unique handles ->
            # handles left after flags. `run.py audit` and the UI's "Doesn't
            # match Instagram?" panel walk exactly these numbers.
            "follower_records":   read_counts["followers"],
            "following_records":  read_counts["following"],
            "duplicate_followers": read_counts["followers"] - len(raw_fset),
            "duplicate_following": read_counts["following"] - len(raw_gset),
            "flagged_followers":  len(raw_fset - fset),
            "flagged_following":  len(raw_gset - gset),
        },
        "source_files": source_files,
        "not_followed_back":   by_ts_desc([by_u_f[u] for u in fset - gset]),
        "dont_follow_me_back": by_ts_desc([by_u_g[u] for u in gset - fset]),
        "mutuals":             [by_u_f[u] for u in (fset & gset)],
        "disabled_accounts":   by_ts_desc(flagged_records),
    }
    return summary, raw["followers"], raw["following"]


def fold_pair(prev: tuple[str, list[dict], list[dict]],
              curr: tuple[str, list[dict], list[dict]]) -> dict:
    """Diff a chronological pair of raw snapshots into a week-over-week block.
    Operates on raw follower/following lists so manual flagging doesn't distort
    lost/added counts. Available in report.json for users who want to inspect
    the JSON directly; not rendered in the read-only UI.

    prev/curr shape: (folder_name, raw_followers, raw_following).
    """
    p_name, p_f, p_g = prev
    c_name, c_f, c_g = curr
    fset_p, gset_p = to_set(p_f), to_set(p_g)
    fset_c, gset_c = to_set(c_f), to_set(c_g)
    by_p_f, by_p_g = by_username(p_f), by_username(p_g)
    by_c_f, by_c_g = by_username(c_f), by_username(c_g)

    # A username change is not a lost follower and not a new one. Drop both
    # sides of every matched pair out of the deltas.
    renamed = detect_renames(p_f, c_f) | detect_renames(p_g, c_g)
    old_handles, new_handles = renamed.keys(), set(renamed.values())

    lost_f   = fset_p - fset_c - old_handles   # followers I had, no longer follow me
    new_f    = fset_c - fset_p - new_handles   # new mutual followers
    lost_g   = gset_p - gset_c - old_handles   # accounts I dropped or who blocked me
    new_g    = gset_c - gset_p - new_handles   # accounts I newly followed

    # ponytail: 'lost_never_followed_back' should really be "I unfollowed them
    # and they never followed me back". It is NOT "they unfollowed me".
    # Compute it as: people I was following at the previous snapshot, am no
    # longer following now, and were not following me in either snapshot.
    # This also excludes anyone who followed me back between snapshots.
    lost_never_followed_back = (gset_p - gset_c) - fset_p - fset_c - old_handles

    # ponytail: `unilateral_unfollowed` is a semantic COMPLEMENT to
    # `lost_never_followed_back`. The latter captures "I unfollowed
    # them and they never followed me back"; this captures "they
    # unfollowed me and I never followed them back" -- the one-sided
    # follower relationship where the follower side lapsed before I
    # ever reciprocated. Excludes anyone who ever had a mutual follow
    # (gset_p or gset_c), so the panel only shows pure unrequited
    # losses. Preserves prev-snapshot records via by_p_f so timestamps
    # match what was actually observed.
    unilateral_unfollowed = (fset_p - fset_c) - gset_p - gset_c - old_handles

    return {
        "prev_folder": p_name,
        "curr_folder": c_name,
        "counts": {
            "new_followers":          len(new_f),
            "lost_followers":         len(lost_f),
            "new_followings":         len(new_g),
            "lost_followings":        len(lost_g),
            "unilateral_unfollowed":  len(unilateral_unfollowed),
            "renamed":                len(renamed),
        },
        "renamed":                 renamed,
        "lost_followers":          by_ts_desc([by_p_f[u] for u in lost_f]),
        "new_followers":           by_ts_desc([by_c_f[u] for u in new_f]),
        "lost_followings":         by_ts_desc([by_p_g[u] for u in lost_g]),
        "new_followings":          by_ts_desc([by_c_g[u] for u in new_g]),
        "lost_never_followed_back": by_ts_desc([by_p_g[u] for u in lost_never_followed_back]),
        "unilateral_unfollowed":   by_ts_desc([by_p_f[u] for u in unilateral_unfollowed]),
    }


# ---------- count reconciliation ("why doesn't this match Instagram?") ----------

AUDIT_SCOPES = ("following", "followers", "both")


def latest_snapshot_folder() -> Path | None:
    """Newest correctly-named data/<MM-DD-YYYY>/ folder, or None if there is none."""
    if not DATA.exists():
        return None
    dated = []
    for entry in DATA.iterdir():
        if not entry.is_dir():
            continue
        try:
            dated.append((parse_folder_date(entry.name), entry))
        except ValueError:
            continue
    return max(dated)[1] if dated else None


# Scan verdicts. Deliberately three-way: "we could not establish this" is a
# different and far more common answer than "this account is gone", and
# collapsing the two is what makes a scan look like it found nothing.
GONE, ACTIVE, UNVERIFIED = "gone", "active", "unverified"


def scan_handles_for_disabled(usernames: list[str], *, timeout: float = 8.0,
                              workers: int = 4, label: str = "",
                              progress=None) -> dict[str, str]:
    """Establish, per handle, whether Instagram still serves a profile page.

    Returns {username: GONE | ACTIVE | UNVERIFIED}.

    The saved login runs FIRST when there is one. Instagram login-walls
    anonymous profile views heavily, and an anonymous request that gets walled
    is indistinguishable from one that got rate-limited — so leading with the
    anonymous probe means a scan can return "unclear" for every handle and
    surface nothing. The authenticated pass is the only one that can see
    private and login-walled profiles, so it does the bulk of the clearing and
    the anonymous probe is left to confirm the few handles it could not.

    Neither pass is allowed to call an account gone on its own absence of
    evidence: ig_helper reports "active" or "unverified" and never claims a
    profile is missing, so only the anonymous probe's positive 404/deactivated
    markers produce GONE. Everything unresolved stays UNVERIFIED, which is the
    shortlist a human should eyeball — not a list to flag blindly.
    """
    if not usernames:
        return {}
    verdicts: dict[str, str] = {}
    candidates = list(usernames)

    if ig_authenticated():
        raw = _authenticated_raw_statuses(usernames, progress=progress)
        if raw:
            for username in usernames:
                if raw.get(username) == "active":
                    verdicts[username] = ACTIVE
            candidates = [u for u in usernames if u not in verdicts]
            print(f"  [{label or 'scan'}] saved login cleared "
                  f"{len(verdicts)}/{len(usernames)}; {len(candidates)} to confirm", flush=True)

    if candidates:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(candidates))) as pool:
            probes = dict(zip(candidates, pool.map(lambda u: looks_disabled(u, timeout), candidates)))
        for username, probe in probes.items():
            verdicts[username] = {True: GONE, False: ACTIVE}.get(probe, UNVERIFIED)
        if progress:
            progress(len(usernames), len(usernames))

    if label:
        tally = {v: sum(1 for x in verdicts.values() if x == v) for v in (GONE, ACTIVE, UNVERIFIED)}
        print(f"  [{label}] {len(usernames)} checked, {tally[GONE]} gone, "
              f"{tally[ACTIVE]} active, {tally[UNVERIFIED]} unverified", flush=True)
    return verdicts


def _reconcile_side(counts: dict, live: dict | None, side: str) -> dict:
    """Per-card arithmetic: export records -> unique handles -> after flags,
    then the residual gap against Instagram's own number when we have it."""
    unique = counts[side]
    active = counts[f"active_{side}"]
    live_value = (live or {}).get(side)
    return {
        "records":    counts.get(f"{'follower' if side == 'followers' else 'following'}_records", unique),
        "unique":     unique,
        "duplicates": counts.get(f"duplicate_{side}", 0),
        "flagged":    counts.get(f"flagged_{side}", unique - active),
        "active":     active,
        "live":       live_value if isinstance(live_value, int) else None,
        # Positive gap = the export lists more accounts than instagram.com
        # shows, which is what deactivated/deleted accounts look like.
        "gap":        (active - live_value) if isinstance(live_value, int) else None,
    }


# Verifying a missing handle costs one authenticated profile fetch, a few
# seconds each. The missing list is normally tiny, but a badly stale export can
# make it large, and nobody wants a twenty-minute scan. Past this many we stop
# verifying and say so rather than quietly guessing.
MISSING_VERIFY_LIMIT = 60


def audit_counts(*, check_network: bool = False, scope: str = "both",
                 timeout: float = 8.0, fetch_live: bool = False,
                 progress=None, verify_missing: bool = False,
                 relationships: dict[str, dict[str, str]] | None = None) -> dict:
    """Explain, number by number, how the headline counts were reached.

    Instagram's profile page hides accounts it no longer serves — deactivated,
    deleted, and banned ones — but the data export you downloaded still lists
    every handle that was in your graph when the export was generated. That is
    the usual reason "following" reads higher here than on instagram.com, and
    no amount of re-importing fixes it, because the export is faithfully
    reporting what Instagram gave it.

    With check_network=True this walks the export and names the specific
    handles whose profile pages are gone, so they can be flagged and the two
    numbers brought back into agreement.
    """
    if scope not in AUDIT_SCOPES:
        raise ValueError(f"scope must be one of {', '.join(AUDIT_SCOPES)}")
    folder = latest_snapshot_folder()
    if folder is None:
        raise FileNotFoundError("no dated snapshot folders in data/ — import an export first")

    flagged = _read_flagged()
    summary, raw_followers, raw_following = read_snapshot(folder, flagged)
    counts = summary["counts"]
    # The CLI has no page to stall, so it may pay for a live fetch; the HTTP
    # handler must not, so it reads the cache the UI has already warmed.
    try:
        live = _live_profile_counts() if fetch_live else cached_live_profile_counts()
    except OSError:
        live = None

    audit = {
        "date_folder": summary["date_folder"],
        "date_iso": summary["date_iso"],
        "source_files": summary["source_files"],
        "ignored_files": [f["name"] for f in summary["source_files"] if f["bucket"] is None],
        "flagged_total": len(flagged),
        "live_available": live is not None,
        "followers": _reconcile_side(counts, live, "followers"),
        "following": _reconcile_side(counts, live, "following"),
        "scan": None,
    }
    pools = {"followers": raw_followers, "following": raw_following}
    wanted = ("followers", "following") if scope == "both" else (scope,)
    if relationships is not None:
        # Every username change Instagram's live list has ever proved to us,
        # each old handle pointed at the handle that account carries today.
        renames = known_renames()
        missing_by_side: dict[str, list] = {"followers": [], "following": []}
        renamed_by_side: dict[str, list] = {"followers": [], "following": []}
        extra_by_side: dict[str, list] = {"followers": [], "following": []}
        listed_by_side: dict[str, int] = {}
        missing_by_username: dict[str, dict] = {}
        renamed_pairs: dict[str, str] = {}
        checked: set[str] = set()
        visible: set[str] = set()
        for side in wanted:
            live_side = relationships.get(side)
            if not isinstance(live_side, dict):
                raise ValueError(f"Instagram {side} relationship list unavailable")
            active = {r["username"]: r for r in pools[side] if r["username"] not in flagged}
            checked.update(active)
            listed_by_side[side] = len(live_side)
            visible.update(active.keys() & live_side.keys())

            # A handle absent from Instagram's list because the person renamed
            # is NOT a missing account. Splitting these out is the difference
            # between "flag these 9" and "flag these 6, two of them just
            # changed their name" -- flagging a renamed person as disabled is
            # the single easiest way to make the counts permanently wrong.
            side_renamed = {u: renames[u] for u in active
                            if u in renames and renames[u] in live_side}
            renamed_pairs.update(side_renamed)
            renamed_by_side[side] = by_ts_desc(
                [dict(active[u], renamed_to=new) for u, new in side_renamed.items()])
            missing_by_side[side] = by_ts_desc(
                [r for u, r in active.items()
                 if u not in live_side and u not in side_renamed])
            for record in missing_by_side[side]:
                missing_by_username.setdefault(record["username"], record)

            # The other direction, which nothing here used to compute: accounts
            # Instagram lists that the export has no row for. This is where a
            # rename's *new* handle lands, and where every follow/unfollow made
            # after the export was generated lands. Without it the arithmetic
            # cannot close -- the gap is (missing - extra), not just missing --
            # and a stale export reads as a pile of unexplained accounts.
            accounted = set(active) | set(side_renamed.values()) | flagged
            extra_by_side[side] = sorted(set(live_side) - accounted)

        # An account can be missing from Instagram's relationship list for two
        # completely different reasons, and telling the user to flag both is
        # how live accounts end up buried in the "disabled" panel forever:
        #
        #   1. the account is gone      -- deactivated, deleted, banned, blocked
        #   2. the RELATIONSHIP is gone -- you unfollowed them, or they
        #      unfollowed you, after Instagram generated this export
        #
        # Only (1) is a disabled account. (2) is a live person and flagging
        # them is simply wrong. One authenticated profile fetch separates them:
        # if the profile still loads, the relationship ended, not the account.
        departed_by_side: dict[str, list] = {"followers": [], "following": []}
        verified = False
        truncated = False
        if verify_missing and missing_by_username and ig_authenticated():
            names = sorted(missing_by_username)
            truncated = len(names) > MISSING_VERIFY_LIMIT
            statuses = _authenticated_raw_statuses(names[:MISSING_VERIFY_LIMIT],
                                                   progress=progress) or {}
            if statuses:
                verified = True
                alive = {u for u, state in statuses.items() if state == "active"}
                for side in wanted:
                    departed_by_side[side] = [r for r in missing_by_side[side]
                                              if r["username"] in alive]
                    missing_by_side[side] = [r for r in missing_by_side[side]
                                             if r["username"] not in alive]
                for username in alive:
                    missing_by_username.pop(username, None)

        departed_by_username: dict[str, dict] = {}
        for side in wanted:
            for record in departed_by_side[side]:
                departed_by_username.setdefault(record["username"], record)

        audit["scan"] = {
            "scope": scope,
            "checked": len(checked),
            "candidates": by_ts_desc(list(missing_by_username.values())),
            "followers_candidates": missing_by_side["followers"],
            "following_candidates": missing_by_side["following"],
            # Alive, just no longer connected. Never a flag candidate.
            "departed": by_ts_desc(list(departed_by_username.values())),
            "followers_departed": departed_by_side["followers"],
            "following_departed": departed_by_side["following"],
            "verified": verified,
            "verify_truncated": truncated,
            "renamed": renamed_pairs,
            "followers_renamed": renamed_by_side["followers"],
            "following_renamed": renamed_by_side["following"],
            "followers_extra": extra_by_side["followers"],
            "following_extra": extra_by_side["following"],
            "followers_listed": listed_by_side.get("followers"),
            "following_listed": listed_by_side.get("following"),
            "live_profiles": len(visible),
            "authenticated": True,
            "method": "relationships",
            "gone": [],
            "unverified": [],
        }
        return audit

    if not check_network:
        return audit

    handles: dict[str, dict] = {}
    for side in wanted:
        for record in pools[side]:
            # Already-flagged handles are skipped — "Recheck statuses" is the
            # path that re-verifies those, and re-probing them here would just
            # spend requests confirming what the user already told us.
            if record["username"] not in flagged:
                handles.setdefault(record["username"], record)

    usernames = sorted(handles)
    print(f"scanning {len(usernames)} handle(s) in scope '{scope}' against instagram.com...", flush=True)
    verdicts = scan_handles_for_disabled(usernames, timeout=timeout, label=scope,
                                         progress=progress)
    audit["scan"] = {
        "scope": scope,
        "checked": len(usernames),
        # Confirmed gone: safe to flag.
        "gone": by_ts_desc([handles[u] for u, v in verdicts.items() if v == GONE]),
        # Could not be established either way — the shortlist to eyeball. This
        # is where a missing account hides when Instagram will not answer.
        "unverified": by_ts_desc([handles[u] for u, v in verdicts.items() if v == UNVERIFIED]),
        "live_profiles": sum(1 for v in verdicts.values() if v == ACTIVE),
        "authenticated": ig_authenticated(),
    }
    return audit


# ---------- background scan job ----------

# The http server is single-threaded, so a scan that walks a whole following
# list through a headless browser would hold the socket for minutes and freeze
# the page it was launched from. The scan runs on its own thread instead and
# the UI polls /scan-status.
_SCAN_LOCK = threading.Lock()
_SCAN_JOB: dict = {"state": "idle", "checked": 0, "total": 0,
                   "audit": None, "error": None, "profile_id": None,
                   "warning": None}


def scan_job_snapshot() -> dict:
    with _SCAN_LOCK:
        return dict(_SCAN_JOB)


def start_scan_job(*, scope: str = "both") -> dict:
    """Kick off a background count-audit scan. Returns the job state.

    Refuses to start a second scan while one is running — the work is bounded
    by Instagram's patience, not ours, and two concurrent sweeps would just
    get each other rate-limited.
    """
    if scope not in AUDIT_SCOPES:
        raise ValueError(f"scope must be one of {', '.join(AUDIT_SCOPES)}")
    profiles = _profiles_payload()
    profile_id = profiles["active_profile_id"]
    profile_name = next((p["name"] for p in profiles["profiles"] if p.get("id") == profile_id), None)
    with _SCAN_LOCK:
        if _SCAN_JOB["state"] == "running":
            return dict(_SCAN_JOB)
        _SCAN_JOB.update({"state": "running", "checked": 0, "total": 0,
                          "audit": None, "error": None, "warning": None,
                          "profile_id": profile_id})
        started = dict(_SCAN_JOB)

    def progress(checked: int, total: int) -> None:
        with _SCAN_LOCK:
            _SCAN_JOB["checked"], _SCAN_JOB["total"] = checked, total

    def work() -> None:
        try:
            relationships = (_authenticated_relationships(profile_name)
                             if profile_name and ig_authenticated() else None)
            if relationships is not None:
                # Fold the live ids into the ledger BEFORE auditing, so a
                # rename this very fetch proved is already known when the audit
                # decides which absent handles are actually gone.
                record_live_identities(relationships)
            elif profile_name and ig_authenticated():
                # Logged in, but Instagram would not hand over the lists. The
                # per-handle fallback below is far weaker, and silently
                # downgrading to it is what makes the feature look like it
                # "found nothing" -- say so instead.
                with _SCAN_LOCK:
                    _SCAN_JOB["warning"] = (
                        f"Instagram would not return the follower/following lists for "
                        f"@{profile_name}. Checking each account one at a time instead, "
                        f"which is slower and can leave most of them unverified. If you "
                        f"renamed @{profile_name} on Instagram, update the account name "
                        f"in this app to match."
                    )
            audit = (audit_counts(scope=scope, relationships=relationships,
                                  verify_missing=True, progress=progress)
                     if relationships is not None else
                     audit_counts(check_network=True, scope=scope, progress=progress))
        except Exception as e:
            # Catch-all, not just the expected I/O errors: this runs on its own
            # thread, and start_scan_job() refuses a second scan while one is
            # "running" -- any exception that slipped past a narrower except
            # would wedge every future scan until the server restarts.
            with _SCAN_LOCK:
                _SCAN_JOB.update({"state": "error", "error": str(e)})
            return
        with _SCAN_LOCK:
            # Switching accounts mid-scan invalidates the result -- it was
            # computed against the previous profile's export.
            if _SCAN_JOB["profile_id"] != _profiles_payload()["active_profile_id"]:
                _SCAN_JOB.update({"state": "idle", "audit": None})
                return
            _SCAN_JOB.update({"state": "done", "audit": audit})

    threading.Thread(target=work, name="count-audit-scan", daemon=True).start()
    return started


# ---------- periodic saved-flag recheck ----------

RECHECK_INTERVAL_SECONDS = 7 * 24 * 60 * 60
RECHECK_RETRY_SECONDS = 24 * 60 * 60
_RECHECK_LOCK = threading.Lock()
_RECHECK_JOB: dict = {"state": "idle", "checked": 0, "reenabled": [],
                      "remaining": 0, "error": None, "automatic": False,
                      "profile_id": None}


def _recheck_state_path(flags_path: Path) -> Path:
    return flags_path.with_name("disabled-recheck.json")


def _read_recheck_state(flags_path: Path) -> dict:
    try:
        payload = json.loads(_recheck_state_path(flags_path).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _periodic_recheck_due(flags_path: Path) -> bool:
    if not _read_flagged_file(flags_path):
        return False
    next_due = _read_recheck_state(flags_path).get("next_due")
    return not isinstance(next_due, (int, float)) or time.time() >= next_due


def _record_recheck(flags_path: Path, *, success: bool) -> None:
    now = int(time.time())
    state = _read_recheck_state(flags_path)
    state["last_attempt"] = now
    state["next_due"] = now + (RECHECK_INTERVAL_SECONDS if success else RECHECK_RETRY_SECONDS)
    if success:
        state["last_success"] = now
    _atomic_json(_recheck_state_path(flags_path), state)


def recheck_job_snapshot() -> dict:
    with _RECHECK_LOCK:
        return dict(_RECHECK_JOB)


def start_recheck_job(*, force: bool = False) -> dict | None:
    """Start one relationship-list recheck for the active profile.

    Automatic calls return None when the weekly check is not due. Manual
    calls ignore the schedule. The worker captures the profile's paths before
    starting, so switching accounts cannot write flags into the wrong profile.
    """
    payload = _profiles_payload()
    profile_id = payload["active_profile_id"]
    if profile_id is None:
        raise RuntimeError("select an account before rechecking saved flags")
    profile = next(p for p in payload["profiles"] if p.get("id") == profile_id)
    flags_path = DISABLED_FILE
    flagged = _read_flagged_file(flags_path)
    if not force and not _periodic_recheck_due(flags_path):
        return None
    if not ig_authenticated():
        raise RuntimeError("not logged into Instagram yet")

    with _RECHECK_LOCK:
        if _RECHECK_JOB["state"] == "running":
            if _RECHECK_JOB["profile_id"] != profile_id:
                raise RuntimeError("another account recheck is already running")
            return dict(_RECHECK_JOB)
        _RECHECK_JOB.update({
            "state": "running", "checked": len(flagged), "reenabled": [],
            "remaining": len(flagged), "error": None, "automatic": not force,
            "profile_id": profile_id,
        })
        started = dict(_RECHECK_JOB)

    def work() -> None:
        # Everything below runs on its own thread, and start_recheck_job()
        # refuses a second recheck for this profile while one is "running" --
        # any exception that escaped this function, including from the
        # _authenticated_relationships() call itself, would wedge the job
        # until the server restarts. One catch-all covers the whole worker
        # rather than guarding each risky call separately.
        try:
            relationships = _authenticated_relationships(profile["name"])
            if relationships is None:
                try:
                    _record_recheck(flags_path, success=False)
                except OSError:
                    pass
                with _RECHECK_LOCK:
                    _RECHECK_JOB.update({
                        "state": "error",
                        "error": (f"Instagram would not return the follower/following lists "
                                  f"for @{profile['name']}; no flags changed. If you renamed "
                                  f"that account on Instagram, update the account name here "
                                  f"to match."),
                    })
                return

            record_live_identities(relationships)
            renamed = known_renames()
            current = _read_flagged_file(flags_path)
            live = relationships["followers"].keys() | relationships["following"].keys()
            # A flagged handle whose owner simply renamed is not disabled. The
            # ledger proves that by id, so clear the flag on the old handle
            # too -- otherwise the person stays hidden in the disabled panel
            # forever under a name nobody uses any more. The whole ledger is
            # consulted, not just what this fetch proved, so a rename recorded
            # weeks ago still frees its old handle.
            reenabled = sorted(current & (live | {old for old, new in renamed.items()
                                                  if new in live}))
            if reenabled:
                _write_flagged_file(flags_path, current - set(reenabled))
            remaining = len(current) - len(reenabled)
            if reenabled and _profiles_payload()["active_profile_id"] == profile_id:
                _refresh_active_report()
            _record_recheck(flags_path, success=True)
        except Exception as e:
            with _RECHECK_LOCK:
                _RECHECK_JOB.update({"state": "error", "error": str(e)})
            return
        with _RECHECK_LOCK:
            _RECHECK_JOB.update({
                "state": "done", "reenabled": reenabled,
                "remaining": remaining,
            })

    threading.Thread(target=work, name="disabled-account-recheck", daemon=True).start()
    return started


def _print_side(label: str, side: dict) -> None:
    print(f"  {label}")
    print(f"    records read from export : {side['records']}")
    if side["duplicates"]:
        print(f"    duplicate handles removed: {side['duplicates']}")
    print(f"    unique handles           : {side['unique']}")
    print(f"    flagged as disabled      : {side['flagged']}")
    print(f"    counted as active        : {side['active']}")
    if side["live"] is None:
        print(f"    instagram.com says       : unknown (log in for live counts)")
    else:
        print(f"    instagram.com says       : {side['live']}")
        if side["gap"] > 0:
            print(f"    ** {side['gap']} more in your export than Instagram shows **")
        elif side["gap"] < 0:
            print(f"    ** {-side['gap']} fewer in your export than Instagram shows **")
        else:
            print(f"    reconciled — the numbers agree")


def _print_relationship_scan(scan: dict) -> None:
    """Print both directions of the comparison against Instagram's live lists.

    Only one direction used to exist. "In your export but not on Instagram" is
    half the story; "on Instagram but not in your export" is where a rename's
    new handle and every follow made after the export was generated live, and
    without it the two numbers can never be made to add up.
    """
    print(f"{chr(10)}compared {scan['checked']} account(s) against Instagram's own lists.")
    for side in ("followers", "following"):
        missing = scan[f"{side}_candidates"]
        renamed = scan[f"{side}_renamed"]
        extra = scan[f"{side}_extra"]
        listed = scan.get(f"{side}_listed")
        print(f"{chr(10)}  {side.upper()}  (Instagram's list returned {listed})")
        for record in renamed:
            print(f"    [renamed]                     @{record['username']}"
                  f" -> @{record['renamed_to']}")
        for record in scan[f"{side}_departed"]:
            verb = "they unfollowed you" if side == "followers" else "you unfollowed them"
            print(f"    [no longer connected]         @{record['username']}"
                  f"  (profile is alive - {verb})")
        for record in missing:
            print(f"    [no profile found]            @{record['username']}")
        for username in extra:
            print(f"    [on Instagram, not in export] @{username}")
        if not (missing or renamed or extra or scan[f"{side}_departed"]):
            print("    reconciled - every account matches")
    if any(scan[f"{side}_extra"] for side in ("followers", "following")):
        print(f"{chr(10)}Accounts on Instagram but not in your export are people you "
              f"gained (or who{chr(10)}renamed) after the export was generated. Request "
              f"a fresh export to bring them in.")
    if scan.get("departed"):
        print(f"{chr(10)}{len(scan['departed'])} account(s) are alive on Instagram but no "
              f"longer connected to you.{chr(10)}The relationship ended after this export "
              f"was generated - do NOT flag these,{chr(10)}they are not disabled accounts. "
              f"A fresh export drops them on its own.")
    if scan["candidates"]:
        verified_note = ("" if scan.get("verified") else
                         f"{chr(10)}(Instagram would not confirm these profiles, so this "
                         f"list may still contain live accounts.)")
        print(f"{chr(10)}{len(scan['candidates'])} account(s) sit in your export and have no "
              f"reachable profile{chr(10)}- deactivated, deleted, banned, or blocking you. "
              f"Flag them to take them{chr(10)}out of your counts.{verified_note}")
    if scan.get("verify_truncated"):
        print(f"{chr(10)}Only the first {MISSING_VERIFY_LIMIT} missing handles were verified; "
              f"the rest are unchecked.")


def cmd_audit(args: argparse.Namespace) -> None:
    """CLI: python run.py audit [--check-network] [--scope ...] [--flag-gone]"""
    # Prefer Instagram's own relationship lists when there is a saved login:
    # one paginated pass answers for every account at once and is the only
    # source that can tell a rename from a disappearance. The per-handle
    # probe below is the fallback for logged-out runs. The CLI used to skip
    # this path entirely, so `audit --check-network` was strictly worse than
    # the same button in the app.
    relationships = None
    if args.check_network and ig_authenticated():
        payload = _profiles_payload()
        active = payload["active_profile_id"]
        profile_name = next((p["name"] for p in payload["profiles"]
                             if p.get("id") == active), None)
        if profile_name:
            print(f"reading Instagram's live follower/following lists for "
                  f"@{profile_name}...", flush=True)
            relationships = _authenticated_relationships(profile_name)
            if relationships is None:
                print("Instagram would not return the lists; falling back to "
                      "one-account-at-a-time checks.", flush=True)
            else:
                record_live_identities(relationships)

    audit = audit_counts(check_network=args.check_network and relationships is None,
                         scope=args.scope, fetch_live=True,
                         verify_missing=relationships is not None,
                         relationships=relationships)
    print(f"snapshot data/{audit['date_folder']}/")
    for f in audit["source_files"]:
        role = f["bucket"] or "ignored (not a relationship list)"
        print(f"  {f['name']:<44} {f['records']:>6} records -> {role}")
    print()
    _print_side("FOLLOWERS", audit["followers"])
    print()
    _print_side("FOLLOWING", audit["following"])

    scan = audit["scan"]
    if scan is not None and scan.get("method") == "relationships":
        _print_relationship_scan(scan)
        return
    if scan is None:
        print("\nRe-run with --check-network to find the specific accounts behind a gap.")
        return

    print(f"\nchecked {scan['checked']} handle(s); {len(scan['gone'])} confirmed gone, "
          f"{scan['live_profiles']} confirmed active, {len(scan['unverified'])} unverified.")
    if not scan["authenticated"]:
        print("(no saved Instagram login — Instagram login-walls anonymous profile\n"
              " views, so 'unverified' can swallow the whole list. Log in from the\n"
              " app and re-run; that pass sees private and walled profiles too.)")
    for record in scan["gone"]:
        print(f"  [gone]       @{record['username']}")
    for record in scan["unverified"]:
        print(f"  [unverified] @{record['username']}")
    if scan["unverified"]:
        print(f"\n{len(scan['unverified'])} handle(s) could not be established either way — "
              f"open them in a browser to settle it. --flag-gone never touches these.")
    if scan["gone"] and args.flag_gone:
        for record in scan["gone"]:
            add_flagged(record["username"])
        print(f"flagged {len(scan['gone'])} account(s) as disabled — "
              f"re-run `python run.py` to rebuild the report.")
    elif scan["gone"]:
        print("\nRe-run with --flag-gone to move these into the disabled panel, "
              "or flag them individually in the app.")


# ---------- http server (static files only) ----------


def _scan_status_payload(job: dict) -> dict:
    """Trim the internal job record down to what the UI needs."""
    return {"state": job["state"], "checked": job["checked"], "total": job["total"],
            "audit": job["audit"], "error": job["error"],
            "warning": job.get("warning")}


def _recheck_status_payload(job: dict) -> dict:
    return {key: job[key] for key in (
        "state", "checked", "reenabled", "remaining", "error",
        "automatic", "profile_id",
    )}


class FlagHandler(http.server.SimpleHTTPRequestHandler):
    """Like SimpleHTTPRequestHandler but accepts POST /flag and POST /import.

    /flag   — toggle a username in data/disabledaccounts.json (small JSON body).
    /import — multipart upload: date + followers + following JSON files.
              Stages into data/<MM-DD-YYYY>/ then re-runs analyze so the next
              page refresh picks it up.

    Static file serving still works for / and /dist/* because do_GET / do_HEAD
    fall through to super(). Silent default access log keeps the server
    terminal quiet while the user pokes at the UI.

    ponytail: bind to 127.0.0.1 (IPv4 loopback) — refusing to expose the report on the LAN.
    """
    IMPORT_MAX_BYTES = 50_000_000  # ~50 MB; an IG export is JSON only, well under

    def do_POST(self) -> None:  # type: ignore[override]
        if self.path == "/profiles":
            self._handle_profiles()
            return
        if self.path == "/login-instagram":
            self._handle_login_instagram()
            return
        if self.path in ("/flag", "/import", "/delete", "/recheck-disabled",
                         "/count-audit") and self._reject_without_profile():
            return
        if self.path == "/flag":
            self._handle_flag()
        elif self.path == "/import":
            self._handle_import()
        elif self.path == "/delete":
            self._handle_delete()
        elif self.path == "/recheck-disabled":
            self._handle_recheck_disabled()
        elif self.path == "/count-audit":
            self._handle_count_audit(check_network=True)
        else:
            self.send_error(404, "no such endpoint")

    def do_GET(self) -> None:  # type: ignore[override]
        # ponytail: /snapshots is a JSON endpoint, not a static file. Intercept
        # BEFORE super().do_GET() runs translate_path + serves-from-disk, so
        # /snapshots doesn't get 404 by being mapped to MEIPASS/snapshots.
        # Everything else (static HTML / /dist/*) still falls through to the
        # inherited handler -- _SplitFlagHandler.app.py's translate_path
        # override still applies because Python resolves self.translate_path
        # dynamically at call time.
        if self.path == "/profiles":
            try:
                self._send_json(200, _profiles_payload())
            except OSError as e:
                self.send_error(500, str(e))
            return
        if self.path == "/snapshots":
            if self._reject_without_profile():
                return
            self._handle_list_snapshots()
            return
        if self.path == "/live-counts":
            if self._reject_without_profile():
                return
            try:
                counts = _live_profile_counts()
                response = {"ok": True, "counts": counts}
                if counts is not None:
                    try:
                        job = start_recheck_job(force=False)
                    except RuntimeError:
                        job = None
                    if job is not None:
                        response["recheck"] = _recheck_status_payload(job)
                self._send_json(200, response)
            except OSError as e:
                self.send_error(500, f"could not check live counts: {e}")
            return
        if self.path == "/count-audit":
            if self._reject_without_profile():
                return
            self._handle_count_audit(check_network=False)
            return
        if self.path == "/scan-status":
            if self._reject_without_profile():
                return
            self._send_json(200, {"ok": True, **_scan_status_payload(scan_job_snapshot())})
            return
        if self.path == "/recheck-status":
            if self._reject_without_profile():
                return
            self._send_json(200, {"ok": True,
                                  **_recheck_status_payload(recheck_job_snapshot())})
            return
        if self.path == "/ig-status":
            self._send_json(200, {"ok": True, "state": instagram_login_state()})
            return
        super().do_GET()

    def end_headers(self) -> None:  # type: ignore[override]
        # ponytail: WebView2 / Edge caches static files aggressively. Force
        # every response (index.html, /dist/*, JSON) to be revalidated so
        # app updates are visible immediately after reinstall.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        super().end_headers()

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reject_without_profile(self) -> bool:
        if PROFILE_MANIFEST is None:
            return False
        try:
            active = _profiles_payload()["active_profile_id"]
        except OSError as e:
            self.send_error(500, str(e))
            return True
        if active:
            return False
        self._send_json(409, {"ok": False, "error": "select or create an account first"})
        return True

    def _handle_profiles(self) -> None:
        if PROFILE_MANIFEST is None:
            self._send_json(409, {"ok": False, "error": "profile mode is disabled"})
            return
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0 or length > 4096:
            self.send_error(400, "empty or oversized body")
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            action = str(body.get("action", "")).strip().lower()
            if action == "create":
                payload = _create_profile(body.get("name"), body.get("migrate_legacy") is True)
            elif action == "select":
                payload = _select_profile(body.get("profile_id"))
            else:
                self.send_error(400, "action must be 'create' or 'select'")
                return
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_error(400, "bad json body")
            return
        except KeyError as e:
            self.send_error(404, e.args[0])
            return
        except RuntimeError as e:
            self.send_error(409, str(e))
            return
        except ValueError as e:
            self.send_error(400, str(e))
            return
        except OSError as e:
            self.send_error(500, f"profile operation failed: {e}")
            return
        self._send_json(200, {"ok": True, **payload})

    def _handle_flag(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0 or length > 4096:
            self.send_error(400, "empty or oversized body")
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            username = str(body.get("username", "")).strip()
            action = str(body.get("action", "add")).strip().lower() or "add"
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_error(400, "bad json body")
            return
        if not username:
            self.send_error(400, "missing username")
            return
        try:
            new_list = unflag(username) if action == "remove" else add_flagged(username)
            _refresh_active_report()
        except OSError as e:
            self.send_error(500, f"could not refresh flagged accounts: {e}")
            return
        self._send_json(200, {"ok": True, "username": username.lower(),
                              "action": action, "total": len(new_list)})

    def _handle_login_instagram(self) -> None:
        """POST /login-instagram -- open the visible Chrome login window."""
        try:
            state = start_instagram_login()
        except RuntimeError as e:
            self._send_json(409, {"ok": False, "error": str(e)})
            return
        self._send_json(200, {"ok": True, "state": state})

    def _handle_recheck_disabled(self) -> None:
        """POST /recheck-disabled -- refresh every saved flag in one pass."""
        if not ig_authenticated():
            self._send_json(409, {"ok": False, "error": "not logged into Instagram yet"})
            return
        # Legacy embedders without profile mode retain the old status-page
        # pass. The shipped source and desktop entry points both use profiles.
        if PROFILE_MANIFEST is not None:
            try:
                job = start_recheck_job(force=True)
            except RuntimeError as e:
                self._send_json(409, {"ok": False, "error": str(e)})
                return
            assert job is not None
            self._send_json(200, {"ok": True, **_recheck_status_payload(job)})
            return
        checked = len(_read_flagged())
        try:
            reenabled = recheck_flagged_accounts_by_network(authenticated=True)
            _refresh_active_report()
        except OSError as e:
            self.send_error(500, f"could not refresh disabled accounts: {e}")
            return
        if not ig_authenticated():
            self._send_json(409, {"ok": False, "error": "Instagram session expired; log in again"})
            return
        self._send_json(200, {
            "ok": True,
            "checked": checked,
            "reenabled": reenabled,
            "remaining": len(_read_flagged()),
        })

    def _handle_count_audit(self, *, check_network: bool) -> None:
        """GET /count-audit  — instant arithmetic behind the headline numbers.
        POST /count-audit — start the background network sweep that names the
                            handles Instagram no longer serves. Optional JSON
                            body: {"scope": "following"|"followers"|"both"}.
                            Returns immediately; poll GET /scan-status.

        ponytail: the sweep walks a whole following list through a headless
        browser, which is minutes of work, and this server is single-threaded.
        Running it inline would hold the socket and freeze the very page that
        launched it, so POST only starts the job. Nothing is auto-flagged: the
        scan reports, the user decides, because "Instagram wouldn't answer" is
        not evidence an account is gone.
        """
        scope = "both"
        if check_network:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self.send_error(400, "malformed Content-Length header")
                return
            if length > 4096:
                self.send_error(400, "oversized body")
                return
            if length > 0:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self.send_error(400, "bad json body")
                    return
                scope = str(body.get("scope", scope)).strip().lower() or scope
            # Hand the sweep to the background thread and answer immediately;
            # the UI polls /scan-status for progress and the finished audit.
            try:
                job = start_scan_job(scope=scope)
            except ValueError as e:
                self.send_error(400, str(e))
                return
            self._send_json(200, {"ok": True, **_scan_status_payload(job)})
            return
        try:
            audit = audit_counts(check_network=False, scope=scope)
        except ValueError as e:
            self.send_error(400, str(e))
            return
        except FileNotFoundError as e:
            self._send_json(409, {"ok": False, "error": str(e)})
            return
        except OSError as e:
            self.send_error(500, f"count audit failed: {e}")
            return
        self._send_json(200, {"ok": True, "audit": audit, "flagged": []})

    def _handle_list_snapshots(self) -> None:
        """GET /snapshots -- return every dated folder on disk as JSON.

        ponytail: separate from /flag and /import because the listing UI
        fires once on init and again after any /delete, so it's a GET not
        a POST. Cache-Control: no-store is set by _send_json, so each
        load() call re-hits disk and reflects the post-mutation state.
        The 'warnings' field carries non-date folders under DATA so GUI
        users get the same diagnostic run_analyze prints to stderr in CLI
        mode (reviewer-caught: silent-skip left GUI users guessing why
        their hand-typed folder wasn't surfaced).
        """
        try:
            items, warnings = _list_snapshots()
        except OSError as e:
            self.send_error(500, f"could not list snapshots: {e}")
            return
        self._send_json(200, {"ok": True, "snapshots": items, "warnings": warnings})

    def _handle_delete(self) -> None:
        """POST /delete -- permanently remove a single dated snapshot folder
        from DATA/<date>/. Mirrors the body-validation shape of _handle_flag
        (small JSON body, username-shaped) but with a single 'date' field
        instead of 'username' + 'action'.

        ponytail: same final-action shape as _handle_import (rmtree +
        run_analyze) so the post-delete report.json re-derives from
        remaining snapshots on disk. If the deleted snapshot was the
        last one, run_analyze raises SystemExit -- fall through to
        _emit_stub_report so the next page load renders the empty-state
        UI instead of the stale 'last-snapshot' content.
        """
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0 or length > 4096:
            self.send_error(400, "empty or oversized body")
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            date_str = str(body.get("date", "")).strip()
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_error(400, "bad json body")
            return
        if not date_str:
            self.send_error(400, "missing 'date'")
            return
        try:
            parse_folder_date(date_str)
        except ValueError as e:
            self.send_error(400, f"bad date: {e}")
            return
        target = DATA / date_str
        # ponytail: capture how many OTHER dated folders will remain AFTER
        # rmtree. Drives the is_last flag below -- if we're going to be
        # left with zero dated folders, we must write the empty-state stub
        # ourselves (NOT delegate to run_analyze), because analyze raises
        # SystemExit in that case and any failure in the stub-write
        # fallback would leave the user with stale pre-delete content
        # in dist/report.json.
        remaining_count = 0
        if DATA.exists():
            for p in DATA.iterdir():
                if not p.is_dir() or p.name == date_str:
                    continue
                try:
                    parse_folder_date(p.name)
                except ValueError:
                    continue
                remaining_count += 1
        is_last = remaining_count == 0

        # ponytail: defense-in-depth against pre-existing symlinks under
        # DATA pointing outside it. parse_folder_date's regex above
        # already filters path-traversal chars, so target_resolved can
        # only differ from the literal DATA/<date_str> path via a
        # symlink that someone placed under DATA by hand; this catches
        # it before shutil.rmtree walks off the data dir. Symlink
        # protection is the only residual attack surface once the regex
        # has filtered the input -- the comment from earlier ("USB
        # drive letter, hand-crafted date field with ..") overstated
        # the threat model because the regex itself rejects all of that.
        if not target.is_dir():
            self.send_error(404, f"snapshot {date_str} does not exist")
            return
        try:
            target_resolved = target.resolve()
            data_resolved = DATA.resolve()
        except OSError:
            target_resolved = target
            data_resolved = DATA
        try:
            target_resolved.relative_to(data_resolved)
        except ValueError:
            self.send_error(400, f"refusing to delete path outside {DATA}")
            return
        try:
            shutil.rmtree(target)
        except OSError as e:
            self.send_error(500, f"could not delete {target}: {e}")
            return

        # ponytail: pre-empt the dual-failure mode the reviewer caught.
        # If THIS was the last folder AND run_analyze then raises
        # SystemExit (no folders left), AND _emit_stub_report raises
        # OSError (OneDrive-locked %APPDATA%, disk full) -- without a
        # pre-write, dist/report.json would still hold the pre-delete
        # content and a page refresh would show phantom data. Writing
        # the stub FIRST guarantees the on-disk JSON is at worst the
        # correct empty-state, not stale. Reviewer caught this as the
        # top severity finding in the audit.
        if is_last:
            try:
                _emit_stub_report()
            except OSError as e:
                self.send_error(500, f"deleted {date_str} but could not write stub report: {e}")
                return

        # ponytail: analyze-or-stub for the typical (other-folders-still-
        # exist) case. If analyze succeeds it overwrites our stub with
        # real content (fine). If analyze raises SystemExit (race: a
        # different folder vanished between our pre-write and now) we
        # fall back to a stub-write -- best-effort because the
        # remaining_count check above should already have caught that.
        try:
            run_analyze(argparse.Namespace(check_disabled=False))
        except SystemExit:
            try:
                _emit_stub_report()
            except OSError:
                # ponytail: both branches fired. OUT may be stale but the
                # user can recover by re-launching the app, which will
                # re-trigger _auto_generate_report's stub-write path anyway.
                pass
        except OSError as e:
            # ponytail: if is_last pre-write above succeeded, the stub is
            # already on disk and OUT is unchanged by analyze's failed
            # write_text (it's the LAST step, after all other work). The
            # post-delete state on disk (stub report + zero folders) is
            # therefore still exactly what the user asked for, so a 500
            # here would lie about reality -- log to stderr and return
            # success. For the non-is_last case, OUT still holds the
            # pre-delete content (stale but recoverable on relaunch) so
            # the 500 + recovery hint matches the actual disk state.
            if is_last:
                print(f"warning: deleted {date_str}; analyze post-step failed: {e} -- dist/report.json is the empty-state stub", file=sys.stderr)
            else:
                self.send_error(500, f"deleted {date_str} but analyze failed: {e}")
                return
        self._send_json(200, {"ok": True, "deleted": date_str})

    def _handle_import(self) -> None:
        """Stage an in-browser upload into data/<date>/ and refresh the report.

        ponytail: tiny stdlib multipart parser instead of importing cgi (deprecated
        in 3.13). Splits body on --boundary, partitions each part on \r\n\r\n
        into (headers, data), reads name= / filename= from Content-Disposition.
        Delegates to _stage_snapshot by writing the uploads into a tempdir —
        reusing the substring filename router instead of branching the funcs.
        """
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            self.send_error(400, "expected multipart/form-data")
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self.send_error(400, "malformed Content-Length header")
            return
        if length <= 0 or length > self.IMPORT_MAX_BYTES:
            self.send_error(400, f"body must be 1..{self.IMPORT_MAX_BYTES} bytes")
            return
        parsed = _parse_multipart(ctype, self.rfile.read(length))
        if parsed is None:
            self.send_error(400, "could not parse multipart body")
            return
        fields, files = parsed
        date_str = (fields.get("date") or "").strip()
        try:
            parse_folder_date(date_str)
        except ValueError as e:
            self.send_error(400, f"bad date: {e}")
            return
        if not files.get("followers") or not files.get("following"):
            self.send_error(400, "need both 'followers' and 'following' file fields")
            return

        # ponytail: capture whether the destination folder already existed so a
        # failed analyze only wipes the freshly-staged copies — never the user's
        # prior snapshot for that date. _stage_snapshot overwrites in place, so
        # without this guard a transient analyze SystemExit would also nuke
        # pre-existing data on the re-import path.
        dest_existed = (DATA / date_str).exists()

        # ponytail: write to a tempdir with names matching _stage_snapshot's
        # substring router, then delegate. The tempdir cleans itself up.
        with tempfile.TemporaryDirectory(prefix="import-") as td:
            src = Path(td)
            # Every part the browser sent, not just one per field: Instagram
            # splits big exports into followers_1.json, followers_2.json, ...
            # and the form now accepts all of them in a single pick.
            for field in ("followers", "following"):
                for i, blob in enumerate(files[field], 1):
                    (src / f"{field}_{i}.json").write_bytes(blob)
            try:
                n_f, n_g = _stage_snapshot(src, date_str)
            except SystemExit as e:
                self.send_error(400, str(e))
                return

            # ponytail: re-run analyze via Namespace(check_disabled=False) so the
            # just-staged snapshot is in dist/report.json by the next page load.
            # We don't expose port/subcommand here — run_analyze only reads
            # check_disabled off the Namespace.
            try:
                run_analyze(argparse.Namespace(check_disabled=False))
            # ponytail: broaden from SystemExit-only to (SystemExit, OSError).
            # run_analyze's final OUT.write_text() can raise OSError on
            # disk-full (ENOSPC), read-only filesystem (EROFS), or
            # OneDrive-locked %APPDATA%\...\dist\ (PermissionError on Windows).
            # The bundled Import button would otherwise surface this as a
            # connection-reset / silent 500 instead of a clean, parseable
            # 500+message that the in-app UI can render. Reviewer caught this
            # as the second remaining import-flow bug after Fix #1 (ROOT
            # rebind in _set_data_paths) made ValueError-on-print unreachable.
            except (SystemExit, OSError) as e:
                if not dest_existed:
                    # Only safe to sweep when the folder was newly created by
                    # _stage_snapshot — anything that was there before this
                    # request is the user's, not ours, and stays.
                    shutil.rmtree(DATA / date_str, ignore_errors=True)
                self.send_error(500, f"staged files into data/{date_str}/ but analyze failed: {e}")
                return

            self._send_json(200, {
                "ok": True, "date": date_str,
                "followers": n_f, "following": n_g,
                "snapshot_folder": f"data/{date_str}/",
            })

    # ponytail: silence default access-log lines on stdout — they make the
    # terminal noisy while the user is interacting via the UI.
    def log_message(self, format: str, *args) -> None:  # type: ignore[override]
        pass


def _run_server(port: int, handler_cls: type = FlagHandler) -> http.server.HTTPServer:
    """Bind the static handler to 127.0.0.1:<port> (IPv4 loopback) and return
    a HTTPServer ready to serve_forever(). Splits out browser-launch and
    main-loop from serve() so tests can drive the HTTP layer without
    spawning a browser window.

    ponytail: bind to 127.0.0.1 (IPv4 loopback) — refusing to expose the report on the LAN.
    ponytail: SO_REUSEADDR lets a half-cleaned port from a crashed prior run
    (TIME_WAIT) bind cleanly without waiting -- but on Linux/Mac only. Windows'
    SO_REUSEADDR semantics permit stealing an *active* listener, which would
    silently defeat the friendly-error path. On Windows we keep Python's
    default (no reuse) so an active-listener bind attempt still raises and
    surfaces the recipe below.
    ponytail: handler_cls is parameterized so the desktop entry point
    (app.py) can pass a subclass that splits /dist/* and /data/* over to
    %APPDATA% while everything else still resolves under the bundled
    PyInstaller MEIPASS dir. Default stays FlagHandler — every existing
    caller's behavior is preserved.
    """
    handler = functools.partial(handler_cls, directory=str(ROOT))

    # ponytail: local subclass so the SO_REUSEADDR knob stays inside this
    # function. Mutating http.server.HTTPServer.allow_reuse_address globally
    # would leak the flag into any other HTTPServer construction in the process.
    class _Server(http.server.HTTPServer):
        allow_reuse_address = sys.platform != "win32"

        def handle_error(self, request, client_address) -> None:
            # Reloading the page, navigating away, or closing the window aborts
            # the socket mid-response. socketserver's default dumps a full
            # traceback for it, which reads like a crash to anyone watching the
            # terminal -- and it is completely routine. Swallow connection
            # teardown only; a real handler bug still prints.
            if not isinstance(sys.exc_info()[1], ConnectionError):
                super().handle_error(request, client_address)

    try:
        return _Server(("127.0.0.1", port), handler)
    except OSError as e:
        # ponytail: errno-only classification -- EADDRINUSE is 98 on Linux/Mac
        # and WSAEADDRINUSE is 10048 on Windows. Other failures fall through
        # to a "try a different port" hint below.
        if e.errno == errno.EADDRINUSE:
            on_windows = sys.platform == "win32"
            if on_windows:
                # ponytail: use the user's literal tasklist + taskkill recipe
                # (manual PID pick — we never force-kill a process by name).
                find_cmd = "tasklist | findstr python.exe"
                kill_cmd = "taskkill /F /PID <pid>"
            else:
                find_cmd = f"lsof -i :{port}"
                kill_cmd = f"lsof -ti :{port} | xargs kill"
            raise SystemExit(
                f"port {port} is in use by another process.\n"
                f"\n"
                f"  If a stale python run.py still holds {port}:\n"
                f"    {find_cmd}\n"
                f"    {kill_cmd}\n"
                f"  ...or just pick a different port:\n"
                f"    python run.py {port + 1}\n"
            ) from e
        raise SystemExit(
            f"port {port}: {e} -- try a different port: python run.py {port + 1}"
        ) from e

def serve(port: int) -> None:
    """Start http server on localhost:<port> and open the browser. Blocks until Ctrl+C.

    ponytail: bind to 127.0.0.1 (IPv4 loopback) — refusing to expose the report on the LAN."""
    httpd = _run_server(port)
    url = f"http://localhost:{port}/"
    print(f"Serving on {url}  (Ctrl+C to stop)", flush=True)
    webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


# ---------- snapshot inventory + stub fallback ----------


def _list_snapshots() -> tuple[list[dict], list[str]]:
    """Return ({date, followers, following, imported_at}, warnings).

    ponytail: walks DATA once, partitioning entries into dated folders
    (returned with per-folder record counts) and non-date folders
    (returned as observer warnings). The dual-purpose walk matches
    run_analyze's partition-then-folder list so a typo'd folder under
    DATA surfaces via the /snapshots warnings field instead of being
    silently pruned -- a CLI user sees the same warning on stderr from
    run_analyze, a GUI user now sees it on the manage panel. Counts via
    load_json_records + bucket_for_filename (NOT read_snapshot's full
    summary) -- intentional cost-skip since the listing UI just needs unique
    handle counts, but it must route and dedupe identically or the inventory
    panel disagrees with the stat cards.
    """
    items: list[dict] = []
    warnings: list[str] = []
    if not DATA.exists():
        return items, warnings
    dated: list[tuple[datetime, Path]] = []
    for p in DATA.iterdir():
        if not p.is_dir():
            continue
        try:
            dt = parse_folder_date(p.name)
        except ValueError:
            warnings.append(p.name)
            continue
        dated.append((dt, p))
    dated.sort(key=lambda t: t[0])
    for _, p in dated:
        # ponytail: same router AND same per-username dedupe as read_snapshot.
        # Summing raw record counts through a substring match was a third,
        # divergent copy of the counting rule — it double-counted handles
        # shared by followers_1.json/followers_2.json and swallowed
        # following_hashtags.json whole, so this panel could contradict the
        # stat cards sitting directly above it.
        seen: dict[str, set[str]] = {"followers": set(), "following": set()}
        for f in sorted(p.glob("*.json")):
            bucket = bucket_for_filename(f.name)
            if bucket is None:
                continue
            seen[bucket].update(r["username"] for r in load_json_records(f))
        n_f, n_g = len(seen["followers"]), len(seen["following"])
        # ponytail: folder mtime is set on Folder.mkdir() in _stage_snapshot
        # and only changes on a subsequent overwriting re-import. Use it as
        # "imported_at" -- the user can see at-a-glance when each snapshot
        # was staged.
        try:
            mtime = p.stat().st_mtime
            imported_at = datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
        except OSError:
            imported_at = ""
        items.append({
            "date": p.name,
            "followers": n_f,
            "following": n_g,
            "imported_at": imported_at,
        })
    return items, warnings


def _emit_stub_report() -> None:
    """Write an empty report.json under OUT so the window can render the
    empty-state UI instead of the 'Could not load report' fallback.

    ponytail: same shape as run_analyze would have produced for zero
    snapshots, so render() / latestSnapshot() consume it without
    branching. Shared by app.py:_auto_generate_report (first launch with
    no data) and FlagHandler._handle_delete (last snapshot just
    removed) -- dedup keeps the stub schema in one place so a future
    schema bump only touches one function.
    """
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "snapshots": [],
                "weeks": [],
                "unilateral_unfollowed": [],
                "renamed": {},
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# ---------- cli + main ----------


def _stage_snapshot(src: Path, date_str: str) -> tuple[int, int]:
    """Source-of-truth for staging a new dated snapshot. Shared by the `import`
    CLI subcommand so the on-disk format stays predictable.

    ponytail: routes the source files through bucket_for_filename, exactly like
    read_snapshot does, so 'folowers_May2026.json' -> followers bucket while
    'following_hashtags.json' (a real file sitting right next to
    following.json in every export) is vetoed instead of silently becoming
    your following list. Pre-parses the files so a malformed export produces
    a clean error BEFORE we touch data/.

    EVERY file in a bucket is staged, not just the first. Instagram splits a
    large export across followers_1.json, followers_2.json, ... and this used
    to copy one file per bucket, so every follower past the first part was
    dropped on the floor at import time. read_snapshot globs the whole staged
    folder and always did, which is why the loss was invisible: the report
    faithfully counted every record that made it to disk, and the missing
    thousands never got there. That is the single largest source of "the
    follower count here doesn't match Instagram".
    """
    src = src.resolve()
    if not src.exists():
        raise SystemExit(f"source folder does not exist: {src}")
    if not src.is_dir():
        raise SystemExit(f"source is not a folder: {src}")

    sources: dict[str, list[Path]] = {"followers": [], "following": []}
    for f in sorted(src.iterdir()):
        if not f.is_file():
            continue
        bucket = bucket_for_filename(f.name)
        if bucket:
            sources[bucket].append(f)

    if not sources["followers"]:
        raise SystemExit(f"{src}: no 'followers' file (looked for *follower* in filename)")
    if not sources["following"]:
        raise SystemExit(f"{src}: no 'following' file (looked for *following* in filename)")

    # Dedupe across parts exactly the way read_snapshot will, so the number
    # reported back to the user is the number the report goes on to show.
    parsed = {bucket: {r["username"]: r for f in files for r in load_json_records(f)}
              for bucket, files in sources.items()}
    for bucket in ("followers", "following"):
        if not parsed[bucket]:
            names = ", ".join(f.name for f in sources[bucket])
            raise SystemExit(f"{names}: parsed 0 records \u2014 bad format or empty?")

    dest = DATA / date_str
    if dest.exists():
        print(f"warning: {dest} already exists \u2014 replacing the relationship JSON "
              f"files inside", file=sys.stderr)
    dest.mkdir(parents=True, exist_ok=True)
    written: set[str] = set()
    for bucket, files in sources.items():
        for i, f in enumerate(files, 1):
            name = f"{bucket}_{i}.json"
            shutil.copy2(f, dest / name)
            written.add(name)
    # Only now clear parts left over from a previous, larger import. Doing this
    # after the copies means a copy that fails partway leaves the folder no
    # emptier than it started. Re-importing a smaller export would otherwise
    # strand followers_4.json here and the report would keep counting people
    # the new export no longer lists.
    for stale in dest.glob("*.json"):
        if stale.name not in written and bucket_for_filename(stale.name):
            stale.unlink()
    return len(parsed["followers"]), len(parsed["following"])


def cmd_import(src: Path, date: datetime) -> None:
    """CLI wrapper around _stage_snapshot — used by `python run.py import src --date X`."""
    date_str = date.strftime("%m-%d-%Y")
    n_f, n_g = _stage_snapshot(src, date_str)
    print(f"imported {n_f:,} followers and {n_g:,} followings")
    print(f"from {src.name} into data/{date_str}/")
    print(f"Run \`python run.py\` to refresh dist/report.json with the new snapshot.")
    print(f"  (add \`--check-disabled\` to live-filter disabled accounts.)")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args. With no subcommand, runs the analyze + serve pipeline.
    The 'import' subcommand stages a raw-export folder into data/<date>/ so it
    joins the canonical snapshot timeline.

    ponytail: argparse validates the positional 'port' (int) before checking
    subcommand names, so the naive 'p.add_subparsers() / sub.add_parser("import")'
    setup misroutes `python run.py import src --date ...` and errors with
    'invalid int value: import'. We sidestep this by inspecting argv[0] and
    ONLY registering the import subparser when it's the first token. Standard
    subcommand dispatch then takes over cleanly.
    """
    if argv is None:
        argv = sys.argv[1:]

    p = argparse.ArgumentParser(
        description=(
            "Analyze Instagram data exports and serve the report locally.\n\n"
            "Default mode (no subcommand): analyze + serve.\n"
            "  python run.py [port] [--check-disabled]\n\n"
            "Subcommand 'import': stage a raw-export folder into data/<date>/.\n"
            "  python run.py import <src_dir> --date MM-DD-YYYY\n\n"
            "Subcommand 'audit': explain where the headline counts came from and\n"
            "why they may disagree with instagram.com.\n"
            "  python run.py audit [--check-network] [--scope both] [--flag-gone]\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # ponytail: the no-subcommand branch doesn't ever set args.subcommand, so
    # main() needs a default value to branch on. set_defaults gives 'subcommand'
    # the value None for the default-mode Namespace; the import subparser
    # overrides it via dest='subcommand' when present.
    p.set_defaults(subcommand=None)
    p.add_argument(
        "--check-disabled", action="store_true",
        help="For each handle in 'Don't follow me back', fetch their Instagram "
             "profile page anonymously and merge handles whose page reads as "
             "disabled/deactivated into 'disabled_accounts'. Slow (~0.5s/handle) "
             "and requires network.",
    )

    if argv and argv[0] in ("import", "audit"):
        sub = p.add_subparsers(dest="subcommand", required=True)
        p_imp = sub.add_parser(
            "import",
            help="Stage a raw-export folder into data/<date>/.",
            description="Import a raw-export folder into data/<MM-DD-YYYY>/.",
        )
        p_imp.add_argument(
            "source",
            help="Path to a folder containing a followers file (*follower* in name) "
                 "and a following file (*following* in name).",
        )
        p_imp.add_argument(
            "--date", dest="date_str", required=True,
            help="Date the data was pulled from (must be MM-DD-YYYY).",
        )
        p_aud = sub.add_parser(
            "audit",
            help="Explain the headline counts and reconcile them with instagram.com.",
            description=(
                "Walk the arithmetic behind the followers/following cards: which "
                "export files were counted, which were ignored, how many duplicate "
                "and flagged handles were removed, and — with --check-network — "
                "which specific accounts in your export no longer have a live "
                "Instagram page. Deactivated, deleted, and banned accounts stay in "
                "your data export but are not counted by instagram.com, which is "
                "the usual reason the two numbers differ."
            ),
        )
        p_aud.add_argument(
            "--check-network", action="store_true",
            help="Probe every handle in scope against instagram.com and list the "
                 "ones whose page is gone. Slow (~0.2s/handle) and needs network.",
        )
        p_aud.add_argument(
            "--scope", choices=AUDIT_SCOPES, default="both",
            help="Which list(s) --check-network sweeps (default: both).",
        )
        p_aud.add_argument(
            "--flag-gone", action="store_true",
            help="Also add every confirmed-gone handle to disabledaccounts.json.",
        )
    else:
        p.add_argument(
            "port", nargs="?", type=int, default=DEFAULT_PORT,
            help=f"port for the local http server (default: {DEFAULT_PORT})",
        )

    return p.parse_args(argv)


def run_analyze(args: argparse.Namespace) -> None:
    """Data-processing pipeline. Writes dist/report.json as a side-effect."""
    if not DATA.exists():
        raise SystemExit(f"missing data dir: {DATA}")

    flagged = _read_flagged()
    if flagged:
        print(f"loaded {len(flagged)} flagged handle(s) from {DISABLED_FILE.name}", flush=True)

    # ponytail: skip folders whose name isn't a known date format with a one-line
    # warning. The earlier strict behavior crashed the entire run on stray dirs,
    # which is hostile when somebody drops an unrelated folder into @data by hand.
    folders: list[Path] = []
    for p in DATA.iterdir():
        if not p.is_dir():
            continue
        try:
            parse_folder_date(p.name)
            folders.append(p)
        except ValueError:
            print(f"warning: skipping non-date folder data/{p.name} (rename to MM-DD-YYYY or remove)", file=sys.stderr)
    folders.sort(key=lambda p: parse_folder_date(p.name))
    if not folders:
        raise SystemExit(f"no dated subfolders found in {DATA}")

    snapshots: list[dict] = []
    raw_pairs: list[tuple[str, list[dict], list[dict]]] = []
    for folder in folders:
        summary, fs, gs = read_snapshot(folder, flagged)
        snapshots.append(summary)
        raw_pairs.append((folder.name, fs, gs))

    # Resolve username changes before anything treats a vanished handle as a
    # loss. An old handle 404s on instagram.com, so without this it reads as a
    # deactivated account and gets flagged — the person just renamed.
    #
    # Two sources, merged: the identity ledger (exact, by Instagram user id,
    # populated whenever a live relationship fetch runs) and the snapshot
    # timestamp heuristic (works offline, but needs two snapshots and a
    # timestamp that happens to be unique on both sides of the diff). Either
    # alone leaves a hole the other covers, and re-resolving the union follows
    # a chain that starts in one source and ends in the other.
    renamed = _resolve_chain({**known_renames(), **renames_across(raw_pairs)})
    deleted = {old for old, new in renamed.items() if new.startswith(TOMBSTONE_PREFIX)}
    for old, new in sorted(renamed.items()):
        label = "deleted" if old in deleted else f"renamed -> @{new}"
        print(f"  [{label}] @{old}", flush=True)
    # Renamed people are not disabled; tombstoned ones provably are.
    stale_flags = (flagged & renamed.keys()) - deleted
    new_flags = deleted - flagged
    if stale_flags or new_flags:
        flagged = (flagged - stale_flags) | new_flags
        _write_flagged(flagged)
        if stale_flags:
            print(f"unflagged {len(stale_flags)} renamed account(s) wrongly marked disabled", flush=True)
        if new_flags:
            print(f"flagged {len(new_flags)} deleted account(s)", flush=True)
        snapshots = [read_snapshot(folder, flagged)[0] for folder in folders]

    # ponytail: keep week-over-week folds in report.json for JSON consumers;
    # the read-only UI doesn't render them. Cheap and preserves the historical
    # summary so power users can diff snapshots via the data file.
    weeks: list[dict] = []
    for i in range(len(raw_pairs) - 1):
        weeks.append(fold_pair(raw_pairs[i], raw_pairs[i + 1]))

    # ponytail: compute the GLOBAL 'people who unfollowed me that I never
    # followed' set across ALL snapshots, not just the latest pair. Someone
    # who followed me months ago, unfollowed at any point, and was never
    # followed back should still surface here. Build from raw_pairs so we
    # use the actual records (with timestamps) from the snapshot where each
    # follower was last seen. raw_pairs is guaranteed non-empty here.
    ever_f = {r["username"]: r for _, fs, _ in raw_pairs for r in fs}
    ever_g = {r["username"] for _, _, gs in raw_pairs for r in gs}
    latest_f = {r["username"] for r in raw_pairs[-1][1]}
    global_unilateral = by_ts_desc(
        [ever_f[u] for u in ever_f.keys() - latest_f - ever_g - flagged - renamed.keys()])

    # A flagged historical unfollower is absent from the latest snapshot, so
    # surface all previously seen flags here to keep Unflag available.
    ever_records = {r["username"]: r for _, fs, gs in raw_pairs for r in fs + gs}
    disabled = {u: ever_records[u] for u in (flagged & ever_records.keys()) - (renamed.keys() - deleted)}
    disabled.update({r["username"]: r for r in snapshots[-1]["disabled_accounts"]})
    snapshots[-1]["disabled_accounts"] = by_ts_desc(list(disabled.values()))

    if args.check_disabled:
        for snap in snapshots:
            n = len(snap["dont_follow_me_back"])
            print(f"checking {snap['date_folder']} for disabled accounts ({n} to check)...", flush=True)
            filter_disabled_accounts_by_network(snap, renamed=dict(
                (o, n) for o, n in renamed.items() if o not in deleted))

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "snapshots": snapshots,
        "weeks": weeks,
        "unilateral_unfollowed": global_unilateral,
        "renamed": renamed,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    # terminal self-check
    latest = snapshots[-1]
    c = latest["counts"]
    print(f"latest ({latest['date_folder']}):")
    print(f"  followers:           {c['followers']}")
    print(f"  following:           {c['following']}")
    print(f"  not_followed_back:   {c['not_followed_back']}")
    print(f"  dont_follow_me_back: {c['dont_follow_me_back']}")
    print(f"  mutuals:             {c['mutuals']}")
    print(f"  flagged (excluded):  {c['flagged']}")
    if c.get("disabled") and c["disabled"] > c["flagged"]:
        print(f"  network-disabled:    {c['disabled'] - c['flagged']}")
    # ponytail: the UI is read-only and always picks the latest snapshot, so the
    # total snapshot count is the only thing worth surfacing next to the latest.
    if len(snapshots) > 1:
        print(f"  (older {len(snapshots) - 1} snapshot(s) on disk, not displayed)")
    print(f"\nwrote {OUT.relative_to(ROOT)}")


def main() -> None:
    args = parse_args()
    if args.subcommand == "audit":
        # Mirror `import`: only switch to per-account storage when the app has
        # already created it, so a source-only data/ layout still audits.
        if (ROOT / "profiles.json").exists():
            enable_profile_mode(ROOT, ROOT / "dist")
            if _profiles_payload()["active_profile_id"] is None:
                raise SystemExit("select or create an account in the browser before auditing")
        try:
            cmd_audit(args)
        except (FileNotFoundError, ValueError) as e:
            raise SystemExit(str(e))
    elif args.subcommand == "import":
        if (ROOT / "profiles.json").exists():
            enable_profile_mode(ROOT, ROOT / "dist")
            if _profiles_payload()["active_profile_id"] is None:
                raise SystemExit("select or create an account in the browser before importing")
        try:
            date = parse_folder_date(args.date_str)
        except ValueError as e:
            raise SystemExit(f"bad --date {args.date_str!r}: {e}")
        cmd_import(Path(args.source), date)
    else:
        enable_profile_mode(ROOT, ROOT / "dist")
        if _profiles_payload()["active_profile_id"] is None:
            _emit_stub_report()
        else:
            run_analyze(args)
        serve(args.port)


if __name__ == "__main__":
    main()
