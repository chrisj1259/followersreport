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
ponytail: bind the http server to localhost only — never expose the report.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import re
import shutil
import socket
import sys
import tempfile
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


def _selftest() -> None:
    """Lightweight self-check for /import's parsing + date code-path.

    Run with: python -c "import run; run._selftest()"
    """
    body = (
        b'--AaB03x\r\nContent-Disposition: form-data; name="date"\r\n\r\n'
        b'07-15-2026\r\n'
        b'--AaB03x\r\nContent-Disposition: form-data; name="followers"; filename="x.json"\r\nContent-Type: application/json\r\n\r\n'
        b'{"a":1}\r\n'
        b'--AaB03x\r\nContent-Disposition: form-data; name="following"; filename="y.json"\r\nContent-Type: application/json\r\n\r\n'
        b'[]\r\n'
        b'--AaB03x--\r\n'
    )
    fields, files = _parse_multipart('multipart/form-data; boundary=AaB03x', body)
    assert fields.get("date") == "07-15-2026", fields
    assert files.get("followers") == b'{"a":1}', files
    assert files.get("following") == b"[]", files

    parse_folder_date("07-15-2026")  # accept canonical
    for bad in ("2026-07-15", "07/15/2026", "", "07-15-26", "07-15-202",
                "07-ab-2026", "abcd-ef-ghij", "7-15-2026",
                "007-15-2026", "07-015-2026"):
        try:
            parse_folder_date(bad)
            assert False, f"should have rejected {bad!r}"
        except ValueError:
            pass
    print("self-check ok")


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


# ---------- per-snapshot ----------


def read_snapshot(folder: Path, flagged: set[str]) -> tuple[dict, list[dict], list[dict]]:
    """Read every *.json file in this dated folder and bucket its records by
    filename substring. Ponytail: substring match handles 'folowers.json',
    'following_v2.json', 'relationships_followers.json', etc.

    All accounts in `flagged` (data/disabledaccounts.json) are pulled out of
    every display bucket and surfaced in 'disabled_accounts' instead. Raw
    follower/following lists are returned alongside the summary.
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
    # ponytail: return raw (unfiltered) follower + following lists so analytics
    # consumers see actual full-account membership regardless of the manual
    # flag file. (Local-only consumers today — no UI uses this; cheap to keep.)
    return summary, buckets["followers"], buckets["following"]


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

    lost_f   = fset_p - fset_c   # followers I had, no longer follow me
    new_f    = fset_c - fset_p   # new mutual followers
    lost_g   = gset_p - gset_c   # accounts I dropped or who blocked me
    new_g    = gset_c - gset_p   # accounts I newly followed

    # ponytail: 'lost_never_followed_back' should really be "I unfollowed them
    # and they never followed me back". It is NOT "they unfollowed me".
    # Compute it as: people I was following at the previous snapshot, am no
    # longer following now, and were not following me in either snapshot.
    # This also excludes anyone who followed me back between snapshots.
    lost_never_followed_back = (gset_p - gset_c) - fset_p - fset_c

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

    ponytail: bind to localhost only — refusing to expose the report on the LAN.
    """
    IMPORT_MAX_BYTES = 50_000_000  # ~50 MB; an IG export is JSON only, well under

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
            except SystemExit as e:
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


def _run_server(port: int) -> http.server.HTTPServer:
    """Bind the static handler to localhost:<port> and return a HTTPServer ready
    to serve_forever(). Splits out browser-launch and main-loop from serve() so
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

    # ponytail: keep week-over-week folds in report.json for JSON consumers;
    # the read-only UI doesn't render them. Cheap and preserves the historical
    # summary so power users can diff snapshots via the data file.
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
    # ponytail: the UI is read-only and always picks the latest snapshot, so the
    # total snapshot count is the only thing worth surfacing next to the latest.
    if len(snapshots) > 1:
        print(f"  (older {len(snapshots) - 1} snapshot(s) on disk, not displayed)")
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
