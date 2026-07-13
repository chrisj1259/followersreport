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

Flag an account as deleted/disabled by clicking the "Flag" button on a row.
Flagged handles are persisted to `data/disabledaccounts.json` (a flat JSON
list) and excluded from the main panels on every subsequent run. Same file
also receives handles sniffed by `--check-disabled` (slow, network).

A positional port argument overrides the default 8000.

ponytail: usernames are lower-cased for set membership — defensive against
casing changes in future Instagram exports.
ponytail: bind the http server to localhost only — never expose the report.
"""
from __future__ import annotations

import argparse
import cgi
import contextlib
import functools
import http.server
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).parent.resolve()
DATA = ROOT / "data"
OUT = ROOT / "dist" / "report.json"
DISABLED_FILE = DATA / "disabledaccounts.json"
DEFAULT_PORT = 8000

# ponytail: serialise the in-process /import -> run_analyze rebuild so two
# concurrent uploads can't clobber dist/report.json on the same write. Single-
# user localhost tool, but two clicks in quick succession is plausible.
IMPORT_LOCK = threading.Lock()

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
    lowercased set. Tolerates {"handles": [...]} wrapper for future schema
    evolution. Missing file -> empty set (no flags)."""
    if not DISABLED_FILE.exists():
        return set()
    try:
        raw = json.loads(DISABLED_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if isinstance(raw, list):
        return {str(u).strip().lower() for u in raw if u}
    if isinstance(raw, dict):
        handles = raw.get("handles") or []
        return {str(u).strip().lower() for u in handles if u}
    return set()


def _write_flagged(handles: set[str]) -> None:
    """Atomic write via temp-file rename so a crash mid-write can't corrupt
    the user's curated list."""
    DISABLED_FILE.parent.mkdir(parents=True, exist_ok=True)
    out = sorted(handles)
    fd, tmp_path = tempfile.mkstemp(prefix=".disabled-", dir=str(DISABLED_FILE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, DISABLED_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


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


def filter_disabled_accounts_by_network(snapshot: dict, *, delay: float = 0.25) -> None:
    """For each handle in 'dont_follow_me_back', fetch their Instagram page
    and merge detected-disabled entries into 'disabled_accounts'. Heuristic
    only; never silently drops handles on network failure (unklar verdicts 
    stay in the active list)."""
    label = snapshot["date_folder"]
    items = snapshot["dont_follow_me_back"]
    active, disabled = [], []
    for i, u in enumerate(items):
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


def parse_folder_date(name: str) -> datetime:
    # ponytail: only MM-DD-YYYY. YYYY-MM-DD is rejected so a mistyped date the
    # user *thinks* is unambiguous doesn't silently land in the wrong folder.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", name):
        raise ValueError(
            f"date must be in MM-DD-YYYY format (got YYYY-MM-DD {name!r}; "
            f"e.g. MM-DD-YYYY would be 07-15-2026)"
        )
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


# ---------- per-snapshot ----------


def read_snapshot(folder: Path, flagged: set[str]) -> tuple[dict, list[dict], list[dict]]:
    """Read every *.json file in this dated folder and bucket its records by
    filename substring. Ponytail: substring match handles 'folowers.json',
    'following_v2.json', 'relationships_followers.json', etc.

    All accounts in `flagged` (data/disabledaccounts.json) are pulled out of
    every display bucket and surfaced in 'disabled_accounts' instead. Raw
    follower/following lists are returned alongside the summary so week-over-week
    diffs don't get distorted by manual flagging.
    """
    buckets: dict[str, list[dict]] = {"followers": [], "following": []}
    # ponytail: 'raw' tracks UNFILTERED records so week-over-week fold_pair sees
    # actual full-account membership; 'buckets' is what gets filtered against
    # data/disabledaccounts.json for the on-page display.
    raw:      dict[str, list[dict]] = {"followers": [], "following": []}
    for f in sorted(folder.glob("*.json")):
        n = f.name.lower()
        recs = load_json_records(f)
        if not recs:
            continue
        if "follower" in n or "folower" in n:
            buckets["followers"].extend(recs)
            raw["followers"].extend(recs)
        elif "following" in n:
            buckets["following"].extend(recs)
            raw["following"].extend(recs)
        # ponytail: unrecognized exports are silently ignored — better than
        # raising on Instagram's ever-growing export surface.

    flagged_records: list[dict] = []

    for k, recs in buckets.items():
        if not recs:
            continue
        kept, flagged_here = [], []
        seen: set[str] = set()
        for r in recs:
            uname = r["username"]
            if uname in seen:
                continue
            seen.add(uname)
            (flagged_here if uname in flagged else kept).append(r)
        buckets[k] = kept
        flagged_records.extend(flagged_here)

    fset, gset = to_set(buckets["followers"]), to_set(buckets["following"])
    by_u_f = by_username(buckets["followers"])
    by_u_g = by_username(buckets["following"])

    summary = {
        "date_folder": folder.name,
        "date_iso": parse_folder_date(folder.name).date().isoformat(),
        "counts": {
            "followers":          len(fset),
            "following":          len(gset),
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
    # ponytail: return raw (unfiltered) follower + following lists so week-over-week
    # diffs see actual full-account membership regardless of the manual flag file.
    return summary, raw["followers"], raw["following"]


def fold_pair(prev: tuple[str, list[dict], list[dict]],
              curr: tuple[str, list[dict], list[dict]]) -> dict:
    """Diff a chronological pair of raw (unfiltered) snapshots into a
    week-over-week block. Operates on raw follower/following lists so manual
    flagging doesn't distort lost/added counts.

    prev/curr shape: (folder_name, raw_followers, raw_following).
    """
    p_name, p_f, p_g = prev
    c_name, c_f, c_g = curr
    fset_p, gset_p = to_set(p_f), to_set(p_g)
    fset_c, gset_c = to_set(c_f), to_set(c_g)
    by_p_f, by_p_g = by_username(p_f), by_username(p_g)
    by_c_f, by_c_g = by_username(c_f), by_username(c_g)

    lost_f   = fset_p - fset_c   # followers I had, no longer follow me
    new_f    = fset_c - fset_p   # new mutual followers
    lost_g   = gset_p - gset_c   # accounts I dropped or who blocked me
    new_g    = gset_c - gset_p   # accounts I newly followed

    # ponytail: 'lost_never_followed_back' is the previous 'Dont follow me back'
    # bucket minus the current 'Dont follow me back' bucket — these are people
    # who never followed me back AND are no longer in my followings, i.e. they
    # unfollowed me without ever reciprocating.
    dfmb_prev = gset_p - fset_p
    dfmb_curr = gset_c - fset_c
    lost_never_followed_back = dfmb_prev - dfmb_curr

    return {
        "prev_folder": p_name,
        "curr_folder": c_name,
        "counts": {
            "new_followers":    len(new_f),
            "lost_followers":   len(lost_f),
            "new_followings":   len(new_g),
            "lost_followings":  len(lost_g),
        },
        "lost_followers":          by_ts_desc([by_p_f[u] for u in lost_f]),
        "new_followers":           by_ts_desc([by_c_f[u] for u in new_f]),
        "lost_followings":         by_ts_desc([by_p_g[u] for u in lost_g]),
        "new_followings":          by_ts_desc([by_c_g[u] for u in new_g]),
        "lost_never_followed_back": by_ts_desc([by_p_g[u] for u in lost_never_followed_back]),
    }


# ---------- http server with /flag endpoint ----------


class FlagHandler(http.server.SimpleHTTPRequestHandler):
    """Like SimpleHTTPRequestHandler but accepts POST /flag and POST /import.

    /flag   — toggle a username in data/disabledaccounts.json (small JSON body).
    /import — multipart upload of a followers + following JSON pair; staged into
              data/<date>/ and dist/report.json is rebuilt in the same request.

    Static file serving still works for / and /dist/* because do_GET / do_HEAD
    fall through to super().
    """

    # ponytail: 50 MB cap on /import uploads — Instagram data exports run large
    # but anything bigger is almost certainly the wrong file.
    IMPORT_MAX_BYTES = 50 * 1024 * 1024

    def do_POST(self) -> None:  # type: ignore[override]
        if self.path == "/flag":
            self._handle_flag()
        elif self.path == "/import":
            self._handle_import()
        else:
            self.send_error(404, "no such endpoint")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        except OSError as e:
            self.send_error(500, f"could not write disabledaccounts.json: {e}")
            return
        self._send_json(200, {"ok": True, "username": username.lower(),
                              "action": action, "total": len(new_list)})

    def _handle_import(self) -> None:
        # ponytail: cap the multipart-parsed size at the same 50 MB ceiling as
        # the Content-Length check, so a malformed body that over-promises can't
        # blow up by reading unbounded bytes into cgi.FieldStorage's buffers.
        # Set in-handler so we don't poison the global cgi.maxlen for any other
        # code that happens to import this module.
        cgi.maxlen = self.IMPORT_MAX_BYTES

        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0 or length > self.IMPORT_MAX_BYTES:
            self._send_json(400, {"ok": False, "error":
                f"empty or oversized body (max {self.IMPORT_MAX_BYTES // (1024*1024)} MB)"})
            return
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            self._send_json(400, {"ok": False, "error": "expected multipart/form-data"})
            return
        # ponytail: cgi.FieldStorage is the stdlib multipart parser. Deprecated
        # in 3.13 but still works, and avoids re-implementing RFC 7578 by hand.
        try:
            fs = cgi.FieldStorage(fp=self.rfile, headers=self.headers,
                                  environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype})
        except ValueError as e:
            self._send_json(400, {"ok": False, "error": f"malformed multipart body: {e}"})
            return

        date_raw = (fs.getfirst("date") or "").strip()
        # ponytail: the entire codebase is strict MM-DD-YYYY — CLI, browser,
        # and folder naming. The regex check here keeps the browser form in
        # lockstep with the canonical format so we never have a '2026-08-15'
        # input turn into an '08-15-2026' folder under the user's feet.
        if not re.fullmatch(r"\d{2}-\d{2}-\d{4}", date_raw):
            self._send_json(400, {"ok": False,
                "error": f"date must be in MM-DD-YYYY format (got {date_raw!r})"})
            return
        try:
            date = parse_folder_date(date_raw)
        except ValueError:
            self._send_json(400, {"ok": False,
                "error": f"date must be in MM-DD-YYYY format and be a real calendar date (got {date_raw!r})"})
            return
        date_str = date.strftime("%m-%d-%Y")

        for name in ("followers", "following"):
            field = fs[name] if name in fs else None
            if field is None or not getattr(field, "file", None):
                self._send_json(400, {"ok": False, "error":
                    f"missing '{name}' file (FormData fieldname '{name}')"})
                return
            if not getattr(field, "filename", ""):
                self._send_json(400, {"ok": False,
                    "error": f"'{name}' has no filename — pick a file"})
                return

        # Buffer the two uploaded files into a temp dir so _stage_snapshot can
        # substring-route them just like the CLI path. The chosen filenames keep
        # their 'followers_1.json' / 'following.json' shape so the import pipeline
        # doesn't need any new logic.
        try:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                for name in ("followers", "following"):
                    field = fs[name]
                    original = Path(field.filename).name
                    # If the user picked a file whose real name doesn't contain
                    # 'follower'/'following' (e.g. download.png), we still want
                    # substring routing to work — rename so the routing key is in
                    # the filename without trashing the user's original.
                    suffix_name = "tmp_" + original
                    (tmp / suffix_name).write_bytes(field.file.read())
                try:
                    n_f, n_g = _stage_snapshot(tmp, date_str)
                except SystemExit as e:
                    self._send_json(400, {"ok": False, "error": str(e)})
                    return

            # ponytail: rebuild report.json in-process so dist/report.json is
            # fresh by the time the client re-fetches /dist/report.json. The
            # IMPORT_LOCK keeps two simultaneous uploads from clobbering each
            # other's write. Stdout/stderr are silenced so the server terminal
            # stays quiet during background triggers.
            with IMPORT_LOCK:
                # ponytail: silence run_analyze's terminal output so the server
                # terminal stays quiet while a browser user uploads a snapshot.
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    try:
                        run_analyze(argparse.Namespace(
                            port=DEFAULT_PORT, check_disabled=False, subcommand=None,
                        ))
                    except SystemExit as e:
                        self._send_json(500, {"ok": False, "error": f"analyze failed: {e}"})
                        return
        except OSError as e:
            self._send_json(500, {"ok": False, "error": f"io error: {e}"})
            return

        self._send_json(200, {"ok": True, "date": date_str,
                              "followers": n_f, "following": n_g})

    # ponytail: silence default access-log lines on stdout — they make the 
    # terminal noisy while the user is interacting via the UI.
    def log_message(self, format: str, *args) -> None:  # type: ignore[override]
        pass


def _run_server(port: int) -> http.server.HTTPServer:
    """Bind FlagHandler to localhost:<port> and return a HTTPServer ready to
    serve_forever(). Splits out browser-launch and main-loop from serve() so
    tests can drive the HTTP layer without spawning a browser window.

    ponytail: bind to localhost only — refusing to expose the report on the LAN.
    """
    handler = functools.partial(FlagHandler, directory=str(ROOT))
    try:
        return http.server.HTTPServer(("localhost", port), handler)
    except OSError as e:
        raise SystemExit(f"port {port} is busy ({e}); try a different port") from e


def serve(port: int) -> None:
    """Start http server on localhost:<port> and open the browser. Blocks until Ctrl+C.

    ponytail: bind to localhost only — refusing to expose the report on the LAN."""
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


# ---------- cli + main ----------


def _stage_snapshot(src: Path, date_str: str) -> tuple[int, int]:
    """Source-of-truth for staging a new dated snapshot. Shared by the `import`
    CLI subcommand and the browser POST /import endpoint so both paths validate
    identically.

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

    # ponytail: fold raw (unfiltered) follower/following into week-over-week deltas.
    # Manual flags don't distort the lost/added counts because we operate on the
    # raw lists, not on the flag-filtered summary buckets.
    weeks: list[dict] = []
    for i in range(len(raw_pairs) - 1):
        weeks.append(fold_pair(raw_pairs[i], raw_pairs[i + 1]))

    if args.check_disabled:
        for snap in snapshots:
            n = len(snap["dont_follow_me_back"])
            print(f"checking {snap['date_folder']} for disabled accounts ({n} to check)...", flush=True)
            filter_disabled_accounts_by_network(snap)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "snapshots": snapshots,
        "weeks": weeks,
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
    print(f"\nwrote {OUT.relative_to(ROOT)}")


def main() -> None:
    args = parse_args()
    if args.subcommand == "import":
        try:
            date = parse_folder_date(args.date_str)
        except ValueError as e:
            raise SystemExit(f"bad --date {args.date_str!r}: {e}")
        cmd_import(Path(args.source), date)
    else:
        run_analyze(args)
        serve(args.port)


if __name__ == "__main__":
    main()
