#!/usr/bin/env python3
"""Windows desktop entry point for the Instagram Follower Report app.

When launched:
  1. Bootstrap `%APPDATA%\\InstagramFollowerReport\\` (data/, dist/,
     disabledaccounts.json) on first launch and migrate any developer-mode
     data/ from CWD if found.
  2. Inject the data paths into run.py via run._set_data_paths().
  3. Start run.py's in-process HTTP server on an OS-picked port (port=0), via
     a custom FlagHandler that serves static HTML/JS/CSS from the PyInstaller
     bundle (`sys._MEIPASS`) and redirects `/dist/*` to the writable
     `%APPDATA%` location.
  4. Open a pywebview window pointing at the server URL.
  5. Block in webview.start() until the user closes the window, then shut the
     HTTP server down cleanly.

Pure-stdlib Python + a single third-party dep (`pywebview`). The static UI
(`index.html`) is read-only next to the .exe; nothing in `index.html` changes.
Ponytail: minimum changes to run.py -- ONE small global-mutator helper.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

# PyInstaller's windowed bootloader exposes no standard streams. Keep both the
# GUI and its headless helper compatible with third-party diagnostic writes.
if sys.stdin is None:
    sys.stdin = open(os.devnull, "r")
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

# The frozen executable re-invokes itself for browser work. Exit before loading
# pywebview or any GUI state; PyInstaller still sees and bundles imports below.
if len(sys.argv) > 1 and sys.argv[1] == "--ig-helper":
    import ig_helper
    raise SystemExit(ig_helper.main(sys.argv[2:]))

import run  # the existing analyze.py + serve.py layer (analyze + FlagHandler + etc.)
import webview  # pip install pywebview -- the only third-party dep beyond stdlib


# === Path resolution ====================================================

# ponytail: when run from a PyInstaller bundle, sys._MEIPASS points at the
# extracted resource dir (read-only, next to the .exe). In dev mode (plain
# `python app.py`), fall back to the directory next to this script.
if getattr(sys, "frozen", False):
    MEIPASS = Path(sys._MEIPASS)                  # type: ignore[attr-defined]
    PROGRAM_DIR = Path(sys.executable).parent
else:
    MEIPASS = Path(__file__).parent.resolve()
    PROGRAM_DIR = MEIPASS

# ponytail: per-user writable data lives under %APPDATA%. On non-Windows dev
# (unlikely but possible) we fall back to ~/.local/share so dev mode works on
# Linux/macOS for sanity-checking.
_win_appdata = os.environ.get("APPDATA")
if _win_appdata:
    APPDATA = Path(_win_appdata)
elif sys.platform == "win32":
    APPDATA = Path.home() / "AppData" / "Roaming"
elif sys.platform == "darwin":
    APPDATA = Path.home() / "Library" / "Application Support"
else:
    APPDATA = Path.home() / ".local" / "share"

DATA_ROOT = APPDATA / "InstagramFollowerReport"
UPDATE_API = "https://api.github.com/repos/chrisj1259/removedfollowers/releases/latest"
UPDATE_ASSET_PREFIX = "InstagramFollowerReport-Setup-v"


def _version_key(version: str) -> tuple[int, int, int] | None:
    """Return a comparable stable release version, or None for anything else."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version.strip())
    return tuple(map(int, match.groups())) if match else None  # type: ignore[return-value]


def _installed_version() -> tuple[int, int, int] | None:
    """Read the version written by the Inno Setup installer."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\InstagramFollowerReport") as key:
            version, _ = winreg.QueryValueEx(key, "Version")
        return _version_key(str(version))
    except OSError:
        return None


def _latest_update() -> tuple[str, str] | None:
    """Return (version, installer URL) only when GitHub has a newer stable release."""
    current = _installed_version()
    if not getattr(sys, "frozen", False) or current is None:
        return None
    try:
        request = urllib.request.Request(UPDATE_API, headers={"User-Agent": "InstagramFollowerReport"})
        with urllib.request.urlopen(request, timeout=3) as response:
            release = json.load(response)
        if not isinstance(release, dict):
            return None
        if release.get("draft") or release.get("prerelease"):
            return None
        version = str(release.get("tag_name", "")).removeprefix("v")
        if (latest := _version_key(version)) is None or latest <= current:
            return None
        expected_name = f"{UPDATE_ASSET_PREFIX}{version}.exe"
        assets = release.get("assets", [])
        if not isinstance(assets, list):
            return None
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            url = asset.get("browser_download_url", "")
            if asset.get("name") == expected_name and isinstance(url, str) and url.startswith("https://github.com/"):
                return version, url
    except (OSError, ValueError, json.JSONDecodeError):
        pass  # Offline or a bad response must never prevent a local app launch.
    return None


def _download_installer(version: str, url: str) -> Path:
    """Download a selected release into the user's temp folder."""
    target = Path(tempfile.gettempdir()) / f"{UPDATE_ASSET_PREFIX}{version}.exe"
    temporary = target.with_suffix(".download")
    try:
        with urllib.request.urlopen(url, timeout=30) as response, temporary.open("wb") as file:
            shutil.copyfileobj(response, file)
        temporary.replace(target)
        return target
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _start_available_update() -> bool:
    """Ask once at startup, launch the selected installer, and report whether to exit."""
    update = _latest_update()
    if update is None:
        return False
    version, url = update
    prompt = (
        f"Version {version} is available. Download and install it now?\n\n"
        "The app will close while Windows installs the update. Your data is kept."
    )
    if ctypes.windll.user32.MessageBoxW(0, prompt, "Instagram Follower Report", 0x24) != 6:
        return False
    try:
        installer = _download_installer(version, url)
        subprocess.Popen([str(installer), "/SP-"])
        return True
    except OSError as e:
        _show_error("Instagram Follower Report", f"Could not download the update.\n\n{e}")
        return False


# === Custom Handler =====================================================

class _SplitFlagHandler(run.FlagHandler):
    """Static HTML/JS/CSS live next to the .exe (read-only). The report JSON
    and user-uploaded data live in %APPDATA% (writable).

    Ponytail rationale for delegating to super(): the base class's
    translate_path does three things we want to keep --
      (1) percent-decoding the URL,
      (2) stripping the query string,
      (3) scrubbing `..` traversal attempts.
    We intercept just two URL shapes BEFORE super() runs so the writable
    %APPDATA%\\InstagramFollowerReport\\dist\\report.json is reachable even
    though it is NOT under sys._MEIPASS.
    """

    def translate_path(self, path: str) -> str:
        decoded = path.split("?", 1)[0]
        # ponytail: both /dist/* and /data/* live in DATA_ROOT. The Python
        # write paths (run_analyze writes OUT, _handle_import stages into
        # DATA/data/<date>) put files there, so the GETs must read from there too.
        if decoded.startswith(("/dist/", "/data/")):
            return str(DATA_ROOT / decoded.lstrip("/"))
        # ponytail: mirror SimpleHTTPRequestHandler.translate_path's behavior of
        # appending `index.html` when path ends with `/`. Without this, a GET /
        # returns the bare MEIPASS directory and send_head either 404s on Windows
        # (where Path('C:\\x') + '' drops the trailing separator) or quietly
        # doesn't serve index.html. Explicit is better than implicit.
        suffix = decoded.lstrip("/") or "index.html"
        return str(MEIPASS / suffix)


# === Bootstrap ==========================================================

def bootstrap_data_root() -> None:
    """Ensure `%APPDATA%\\InstagramFollowerReport\\` exists with the expected
    shape. On first install (no `.installed` marker), opportunistically copy
    any developer-mode data/ from either CWD or the install dir so users
    running the installer over an existing checkout don't lose their snapshots.

    PONYTAIL: Start Menu shortcuts default CWD to `C:\\Windows\\System32` on
    Windows, so walking only `Path.cwd()` would silently no-op the migration
    on the most common launch path. We also probe PROGRAM_DIR
    (`Path(sys.executable).parent`), which is the install dir on a frozen
    launch and the project root in dev mode (`python app.py`). Either path
    having a dev `data/` + `disabledaccounts.json` will seed APPDATA; both
    paths resolving to the same place collapse via the dedup.
    """
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    (DATA_ROOT / "data").mkdir(exist_ok=True)
    (DATA_ROOT / "dist").mkdir(exist_ok=True)
    flagged = DATA_ROOT / "disabledaccounts.json"

    marker = DATA_ROOT / ".installed"
    if marker.exists():
        return

    # Walk BOTH the dev-mode CWD and PROGRAM_DIR (the install dir). Skip any
    # candidate that resolves to DATA_ROOT so we never round-trip APPDATA
    # into APPDATA.
    src_candidates: list[Path] = []
    for raw in (Path.cwd(), PROGRAM_DIR):
        try:
            src_candidates.append(raw.resolve())
        except OSError:
            pass

    for src_base in src_candidates:
        try:
            if src_base == DATA_ROOT.resolve():
                continue
        except OSError:
            continue
        src_data = src_base / "data"
        if src_data.is_dir():
            for child in src_data.iterdir():
                target = DATA_ROOT / "data" / child.name
                if not target.exists():
                    if child.is_dir():
                        shutil.copytree(child, target)
                    else:
                        shutil.copy2(child, target)
        src_flagged = src_base / "disabledaccounts.json"
        if src_flagged.exists():
            try:
                shutil.copy2(src_flagged, flagged)
            except OSError:
                pass

    marker.write_text("ok", encoding="utf-8")


# === Lifecycle ==========================================================

def _show_error(title: str, body: str) -> None:
    """ponytail: tiny native MessageBoxW Win32 wrapper -- used when the auto-
    launch path from the installer's [Run] entry fails BEFORE pywebview has a
    chance to show its own UI. Reads native Win32 via ctypes (in stdlib) so we
    don't have to declare a tkinter hidden import in build.spec just to surface
    one error dialog in a corner case."""
    MB_ICONERROR = 0x10
    ctypes.windll.user32.MessageBoxW(0, body, title, MB_ICONERROR)


def _auto_generate_report() -> None:
    """Generate the initial report or its empty-state stub."""
    if run._profiles_payload()["active_profile_id"] is None:
        run._emit_stub_report()
        return
    try:
        run.run_analyze(argparse.Namespace(check_disabled=False))
        return
    except SystemExit:
        pass
    run._emit_stub_report()


def main() -> int:
    # ponytail: the installer's [Run] entry launches this .exe immediately
    # after install completes. If %APPDATA% is unwritable (disk full, OneDrive
    # sync conflict, roaming-profile-broken permissions), bootstrap_data_root()
    # raises and the user sees the wizard close with NO error and NO window.
    # Catch OSError here and pop a native error dialog so the failure is
    # explainable, then exit cleanly so the installer's [Run] returns 0.
    try:
        bootstrap_data_root()
    except (OSError, shutil.Error) as e:
        # ponytail: shutil.copytree raises shutil.Error (NOT an OSError
        # subclass) when some sub-files fail to copy mid-tree. Catching only
        # OSError would silently miss partial-copy failures. shutil.Error
        # inherits from Exception so we list it explicitly. The OS-level
        # errors (permission denied, disk full, read-only filesystem) are
        # covered by OSError above.
        _show_error(
            "Instagram Follower Report",
            f"Could not create the app's data folder.\n\n"
            f"Expected location: {DATA_ROOT}\n\n"
            f"Error: {e}\n\n"
            "Make sure your user profile (typically C:\\Users\\<you>\\AppData\\Roaming) "
            "is writable and has free disk space, then re-launch Instagram Follower "
            "Report from the Start Menu.",
        )
        return 1
    if _start_available_update():
        return 0
    run.enable_profile_mode(DATA_ROOT, DATA_ROOT / "dist")

    _auto_generate_report()

    # In-process HTTP server on an OS-picked port. HTTPServer synchronously
    # bind+listens inside __init__, so as soon as this returns the port is
    # accepting; the OS will queue the pywebview window's first GET until
    # serve_forever() enters its accept loop.
    httpd = run._run_server(0, _SplitFlagHandler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    # ponytail: bust WebView2/Edge disk cache on app updates by appending a
    # version query string. Without this, the embedded browser can serve a
    # stale index.html from a previous install even after the files on disk
    # have been overwritten, making UI fixes invisible until the cache expires.
    url = f"http://127.0.0.1:{port}/?v=2"
    try:
        window = webview.create_window(
            "Instagram Follower Report",
            url,
            width=1100,
            height=820,
            resizable=True,
        )
        # webview.start() blocks the main thread until the window is closed.
        webview.start()
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass
        try:
            run.stop_instagram_login()
        except Exception:
            pass
        try:
            httpd.server_close()
        except Exception:
            pass
        t.join(timeout=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
