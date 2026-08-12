#!/usr/bin/env python3
"""One-shot build pipeline: cache the WebView2 bootstrapper locally, bundle
the app with PyInstaller, then compile the Inno Setup installer.

Run:
    pip install pywebview pyinstaller "scrapling[fetchers]"
    python build.py

Output:
    redist/MicrosoftEdgeWebview2Setup.exe    (~80MB; cached after first build)
    dist/InstagramFollowerReport/InstagramFollowerReport.exe   (PyInstaller --onedir)
    Output/InstagramFollowerReport-Setup-v<version>.exe       (final installer — ship this)

The orchestration is stdlib-only; PyInstaller, pywebview, and Scrapling's
fetcher extra are build-time inputs bundled into the installed app.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
REDIST = ROOT / "redist"
WEBVIEW2_BOOTSTRAPPER_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
WEBVIEW2_BOOTSTRAPPER_PATH = REDIST / "MicrosoftEdgeWebview2Setup.exe"
APP_VERSION = os.environ.get("APP_VERSION", "0.0.0-dev")

if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", APP_VERSION):
    raise SystemExit("APP_VERSION must be a semantic version such as 1.2.3")


def ensure_webview2_bootstrapper() -> Path:
    """Download MicrosoftEdgeWebview2Setup.exe (~80MB) if not cached locally.

    ponytail: an 80MB binary in a git repo is bad form, so we keep it gitignored
    (see .gitignore `redist/`) and pull it on first build. Subsequent builds hit
    the cache.
    """
    if WEBVIEW2_BOOTSTRAPPER_PATH.exists() and WEBVIEW2_BOOTSTRAPPER_PATH.stat().st_size > 100_000:
        print(f"[bootstrapper] cached: {WEBVIEW2_BOOTSTRAPPER_PATH} "
              f"({WEBVIEW2_BOOTSTRAPPER_PATH.stat().st_size:,} bytes)")
        return WEBVIEW2_BOOTSTRAPPER_PATH
    REDIST.mkdir(parents=True, exist_ok=True)
    print(f"[bootstrapper] downloading from {WEBVIEW2_BOOTSTRAPPER_URL}")
    print(f"[bootstrapper]   -> {WEBVIEW2_BOOTSTRAPPER_PATH}")
    with urllib.request.urlopen(WEBVIEW2_BOOTSTRAPPER_URL) as resp:
        data = resp.read()
    WEBVIEW2_BOOTSTRAPPER_PATH.write_bytes(data)
    print(f"[bootstrapper] downloaded {len(data):,} bytes")
    return WEBVIEW2_BOOTSTRAPPER_PATH


def find_iscc() -> str:
    """Locate the Inno Setup command-line compiler. Try PATH, then the
    canonical Program Files install paths. Falls back with a helpful error
    if neither is found."""
    on_path = shutil.which("ISCC") or shutil.which("iscc")
    if on_path:
        return on_path
    candidates = [
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
        str(Path.home() / "Program Files (x86)" / "Inno Setup 6" / "ISCC.exe"),
        str(Path.home() / "Program Files" / "Inno Setup 6" / "ISCC.exe"),
        # ponytail: chocolatey installs ISCC.exe to a path outside Program Files
        # so the four canonical locations above wouldn't pick it up. This is
        # the path choco installs to when running `choco install innosetup`,
        # which is one route users can take when jrsoftware.org is blocked.
        r"C:\ProgramData\chocolatey\bin\ISCC.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    raise SystemExit(
        "Inno Setup Compiler (ISCC.exe) not found.\n"
        "Install Inno Setup 6.x from https://jrsoftware.org/isdl.php (free) "
        "and re-run build.py, or compile installer.iss manually.\n"
    )


def main() -> int:
    ensure_webview2_bootstrapper()

    # ponytail: --noconfirm (alias -y) replaces previous output without
    # confirmation. Without it, PyInstaller 5.x+ refuses to overwrite a
    # non-empty dist/ folder and silently bails with `ERROR: The output
    # directory ... is not empty` -- which is exactly what bit a previous
    # run when staging a second build-installer pass on the same dev box.
    # On PyInstaller 5.13+ the .spec does not have a 'clean' flag anymore;
    # the CLI --noconfirm is the canonical replacement and keeps the build
    # reproducible from a dirty working tree.
    print("[pyinstaller] running...")
    res = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "build.spec"],
        cwd=str(ROOT),
    )
    if res.returncode != 0:
        raise SystemExit(f"PyInstaller failed with exit code {res.returncode}")
    onedir = ROOT / "dist" / "InstagramFollowerReport"
    if not (onedir / "InstagramFollowerReport.exe").exists():
        raise SystemExit(
            f"PyInstaller succeeded but {onedir / 'InstagramFollowerReport.exe'} "
            "is missing -- did build.spec change?"
        )

    iscc = find_iscc()
    iss_path = ROOT / "installer.iss"
    print(f"[inno] running {iscc} on {iss_path.name}")
    res = subprocess.run([iscc, f"/DAppVersion={APP_VERSION}", str(iss_path)], cwd=str(ROOT))
    if res.returncode != 0:
        raise SystemExit(f"Inno Setup failed with exit code {res.returncode}")

    out = ROOT / "Output" / f"InstagramFollowerReport-Setup-v{APP_VERSION}.exe"
    if out.exists():
        print(f"[done] installer: {out} ({out.stat().st_size:,} bytes)")
    else:
        print(f"[done] ISCC finished; expected installer: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
