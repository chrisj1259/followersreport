#!/usr/bin/env python3
"""Analyze Instagram data exports (.json) and serve the report locally. End-to-end.

Stdlib only. This file is the formerly-separate analyze.py + serve.py merged
into one — `python run.py` does everything.

Usage:
    python run.py
    python run.py --check-disabled
    python run.py 9000
    python run.py --check-disabled 9000
    python run.py --help

Reads every `*.json` file in each `data/<MM-DD-YYYY>/` subfolder and routes
by filename substring to a bucket:

  *follower* / *folower*  -> followers  (people who follow me)
  *following*             -> following  (people I follow)

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
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ---------- disabled-account persistence (data/disabledaccounts.json) ----------


def _read_flagged() -> set[str]:
    """Read data/disabledaccounts.json (flat list of usernames) and return a
    lowercased set. Missing file -> empty set (no flags)."""
    if not DISABLED_FILE.exists():
        return set()
    try:
        raw = json.loads(DISABLED_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(raw, list):
        return set()
    return {str(u).strip().lower() for u in raw if u}


def _write_flagged(handles: set[str]) -> None:
    """Atomic write via temp-file rename so a crash mid-write can't corrupt
    the user's curated list."""
    _atomic_json(DISABLED_FILE, sorted(handles))


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
    """Use the saved, hand-authenticated Instagram session when it is available."""
    statuses = _scrapling_result("status", usernames, max(90, 30 + 20 * len(usernames)))
    if statuses is None:
        return None
    return {u: False if statuses.get(u) == "active" else None for u in usernames}


def _authenticated_profile_counts(username: str) -> dict[str, int] | None:
    counts = _scrapling_result("counts", [username], 90)
    if counts is None:
        return None
    if all(isinstance(counts.get(k), int) and counts[k] >= 0 for k in ("followers", "following")):
        return {"followers": counts["followers"], "following": counts["following"]}
    return None


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
    files:  {name: raw_bytes} for uploaded files (Content-Disposition has filename=).

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
    files: dict[str, bytes] = {}
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
            files[name] = data
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
    resolved = {}
    for old in chain:
        seen, new = {old}, chain[old]
        while new in chain and new not in seen:
            seen.add(new)
            new = chain[new]
        resolved[old] = new
    return resolved


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
    for f in sorted(folder.glob("*.json")):
        n = f.name.lower()
        recs = load_json_records(f)
        if not recs:
            continue
        if "follower" in n or "folower" in n:
            buckets["followers"].extend(recs)
        elif "following" in n:
            buckets["following"].extend(recs)
        # ponytail: unrecognized exports are silently ignored — better than
        # raising on Instagram's ever-growing export surface.

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
        },
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


# ---------- http server (static files only) ----------


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
        if self.path in ("/flag", "/import", "/delete", "/recheck-disabled") and self._reject_without_profile():
            return
        if self.path == "/flag":
            self._handle_flag()
        elif self.path == "/import":
            self._handle_import()
        elif self.path == "/delete":
            self._handle_delete()
        elif self.path == "/recheck-disabled":
            self._handle_recheck_disabled()
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
                self._send_json(200, {"ok": True, "counts": _live_profile_counts()})
            except OSError as e:
                self.send_error(500, f"could not check live counts: {e}")
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
        if "followers" not in files or "following" not in files:
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
            (src / "followers_1.json").write_bytes(files["followers"])
            (src / "following.json").write_bytes(files["following"])
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
    load_json_records (NOT read_snapshot's full summary) -- intentional
    cost-skip since the listing UI just needs record counts.
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
        n_f, n_g = 0, 0
        for f in sorted(p.glob("*.json")):
            n = f.name.lower()
            recs = load_json_records(f)
            if not recs:
                continue
            if "follower" in n or "folower" in n:
                n_f += len(recs)
            elif "following" in n:
                n_g += len(recs)
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

    ponytail: substring-routes the source files exactly like read_snapshot does,
    so 'folowers_May2026.json' -> followers bucket, 'who_i_follow.json' ->
    following bucket, etc. Pre-parses both files so a malformed export produces
    a clean error BEFORE we touch data/.
    """
    src = src.resolve()
    if not src.exists():
        raise SystemExit(f"source folder does not exist: {src}")
    if not src.is_dir():
        raise SystemExit(f"source is not a folder: {src}")

    follower_src, following_src = None, None
    for f in sorted(src.iterdir()):
        if not f.is_file():
            continue
        n = f.name.lower()
        if follower_src is None and ("follower" in n or "folower" in n):
            follower_src = f
        elif following_src is None and "following" in n:
            following_src = f

    if follower_src is None:
        raise SystemExit(f"{src}: no 'followers' file (looked for *follower* in filename)")
    if following_src is None:
        raise SystemExit(f"{src}: no 'following' file (looked for *following* in filename)")

    f_recs = load_json_records(follower_src)
    g_recs = load_json_records(following_src)
    if not f_recs:
        raise SystemExit(f"{follower_src.name}: parsed 0 records \u2014 bad format or empty?")
    if not g_recs:
        raise SystemExit(f"{following_src.name}: parsed 0 records \u2014 bad format or empty?")

    dest = DATA / date_str
    if dest.exists():
        print(f"warning: {dest} already exists \u2014 overwriting JSON files inside", file=sys.stderr)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(follower_src, dest / "followers_1.json")
    shutil.copy2(following_src, dest / "following.json")
    return len(f_recs), len(g_recs)


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
            "  python run.py import <src_dir> --date MM-DD-YYYY\n"
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

    if argv and argv[0] == "import":
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
    renamed = renames_across(raw_pairs)
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
    if args.subcommand == "import":
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
