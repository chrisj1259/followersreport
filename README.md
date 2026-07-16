# Instagram Follower Report

A local-first Windows desktop app (and CLI) that reads your Instagram data export and shows you who's unfollowed you, who you don't follow back, and who you share mutual follows with. Runs entirely on your machine; nothing is uploaded.

## What you get

- **Stats cards**: followers, following, people you don't follow back, people who don't follow you back.
- **Panels**: full lists of the above, plus a "Likely disabled / deactivated" panel for flagged accounts.
- **Unilateral unfollows**: people who followed you in the past but no longer do, and whom you never followed back.
- **Browser import**: drag-and-drop new snapshots directly into the app UI.
- **Windows installer**: a real `.exe` installer with Start Menu shortcut, desktop shortcut option, and uninstaller.

## Requirements

### To run from source (any OS)

- Python 3.10+
- A modern browser
- Your Instagram data export (see below)

### To build the Windows installer

- Windows 10 or 11
- Python 3.10+
- [Inno Setup 6.x](https://jrsoftware.org/isdl.php) (free, ~5MB)
- `pip install pywebview pyinstaller`

## Quick start (source / CLI)

1. **Request your Instagram data export** in the Instagram app or web: `Settings → Accounts Center → Your information and permissions → Download your information`. Pick **JSON** format and request the download. Instagram emails you a zip; it usually arrives within minutes to an hour.

2. **Unzip the export.** You get a folder containing `followers_1.json` and `following.json`.

3. **Stage your first snapshot** from the repo root:

   ```powershell
   python run.py import <path-to-unzipped-folder> --date MM-DD-YYYY
   ```

   Use today's date in `MM-DD-YYYY` format, e.g. `07-15-2026`. The two JSON files land in `data/07-15-2026/`.

4. **Run the report:**

   ```powershell
   python run.py
   ```

   A browser window opens to `http://localhost:8000`. Hit `Ctrl+C` to stop.

5. **Add more snapshots later** by re-downloading the export and repeating step 3, then run `python run.py` again.

## Building the Windows installer

These steps produce `Output/InstagramFollowerReport-Setup-v1.0.exe`, the file you ship or install.

### 1. Install build dependencies

```powershell
pip install pywebview pyinstaller
```

Install [Inno Setup 6.x](https://jrsoftware.org/isdl.php) once. Make sure `ISCC.exe` is on your PATH or installed to the default `C:\Program Files (x86)\Inno Setup 6\` location.

### 2. Build

From the repo root:

```powershell
python build.py
```

This one command:

1. Downloads `redist/MicrosoftEdgeWebview2Setup.exe` (~80MB, cached after the first build).
2. Runs `pyinstaller build.spec` to produce `dist/InstagramFollowerReport/InstagramFollowerReport.exe`.
3. Invokes `ISCC installer.iss` to compile `Output/InstagramFollowerReport-Setup-v1.0.exe`.

### 3. Ship or install

The file to distribute or run is:

```
Output/InstagramFollowerReport-Setup-v1.0.exe
```

Double-click it on any Windows 10/11 machine to install. The installer will:

- Install the app to `%PROGRAMFILES%\InstagramFollowerReport\`.
- Install the WebView2 runtime if it is missing (Win10 only; Win11 ships it).
- Create a Start Menu shortcut.
- Optionally create a Desktop shortcut.
- Register a real uninstaller with Windows.
- Launch the app when the installer finishes.

## Installing and running on Windows

If you received `InstagramFollowerReport-Setup-v1.0.exe`:

1. **Run the installer** (double-click).
2. **Follow the wizard**. If WebView2 is missing, click Yes to install it.
3. **Launch the app** when the installer finishes (checked by default).
4. **Add your Instagram data** using the **Add data** panel at the bottom of the app window.

Your data lives in `%APPDATA%\InstagramFollowerReport\` and survives reinstalls and uninstalls.

## Using the app

### Add a snapshot from the browser / desktop UI

1. Scroll to the **Add data** panel.
2. Enter the snapshot date as `MM-DD-YYYY`.
3. Attach `followers_1.json` and `following.json` from your unzipped Instagram export.
4. Click **Import snapshot**.

The report refreshes automatically.

### Flag suspicious accounts

Hover any row in **People I don't follow back** or **People who don't follow me back** and click **Flag**. The handle moves into the **Likely disabled / deactivated** panel and is excluded from active lists. Click **Unflag** to restore it. Flagged handles are stored in `data/disabledaccounts.json` (CLI) or `%APPDATA%\InstagramFollowerReport\disabledaccounts.json` (installed app).

### Network-side disabled-account detection

```powershell
python run.py --check-disabled
```

For each handle in "Don't follow me back", this fetches `instagram.com/<handle>` anonymously and merges handles whose page reads as deleted, deactivated, or 404 into the disabled panel. Adds roughly 0.5 seconds per handle. Requires internet.

## Where the data lives

### Source / CLI mode

```
data/
  MM-DD-YYYY/
    followers_1.json
    following.json
  disabledaccounts.json
dist/
  report.json
```

### Installed Windows app

```
%APPDATA%\InstagramFollowerReport\
  data/
    MM-DD-YYYY/
      followers_1.json
      following.json
  disabledaccounts.json
  dist/
    report.json
```

Program files are read-only under `%PROGRAMFILES%\InstagramFollowerReport\`.

## Privacy

- The local server binds to `127.0.0.1` only — never exposed to your LAN.
- `--check-disabled` makes anonymous `instagram.com/<handle>` requests without authentication or cookies. Off by default.
- No telemetry or analytics.
- `data/` and `dist/` are git-ignored so your personal data never accidentally goes to a remote.

## CLI flags at a glance

```powershell
python run.py                       # analyze data/ + serve on :8000
python run.py 9000                  # analyze data/ + serve on :9000
python run.py --check-disabled      # same but additionally walks the DFMB list
python run.py --help                # full usage
python run.py import <src> --date X # stage a new snapshot; no serve
```

## Troubleshooting

**`port 8000 is in use by another process`**  
Pick a different port: `python run.py 8001`. Or kill the stale Python process and retry.

**The installer says WebView2 is required**  
Click Yes. The installer bundles the WebView2 bootstrapper and will install it silently.

**The app window is blank after install**  
Wait a few seconds on first launch while WebView2 initializes. If it persists, reinstall and allow WebView2 installation.

**`python build.py` fails with "ISCC.exe not found"**  
Install Inno Setup 6.x and ensure `ISCC.exe` is on your PATH.

**Follower count drops to zero**  
Check that you unzipped the right Instagram folder. `followers_1.json` should be at the top level of the source.

## Development

- `run.py` — analyze pipeline + local HTTP server.
- `app.py` — Windows desktop entry point (pywebview wrapper).
- `index.html` — the entire UI (single-file HTML/CSS/JS).
- `build.py` — orchestrates PyInstaller + Inno Setup.
- `build.spec` — PyInstaller spec.
- `installer.iss` — Inno Setup script.
- `test_install.ps1` — VM smoke test for the installer.

## License

MIT. Do whatever, no warranty.
