# Instagram Follower Report

![Build Windows Installer](https://github.com/chrisj1259/removedfollowers/actions/workflows/build.yml/badge.svg)

A local-first Windows desktop app (and CLI) that reads your Instagram data export and shows you who's unfollowed you, who you don't follow back, and who you share mutual follows with. Runs entirely on your machine; nothing is uploaded.

## What you get

- **Stats cards**: followers, following, people you don't follow back, people who don't follow you back.
- **Panels**: full lists of the above, plus a "Likely disabled / deactivated" panel for flagged accounts.
- **Unilateral unfollows**: people who followed you in the past but no longer do, and whom you never followed back.
- **Browser import**: drag-and-drop new snapshots directly into the app UI.
- **Multiple desktop accounts**: add and switch Instagram usernames without mixing their snapshots, history, or flagged accounts.
- **Windows installer**: a real `.exe` installer with Start Menu shortcut, desktop shortcut option, and uninstaller.

## Requirements

### To run from source (any OS)

- Python 3.10+
- A modern browser
- Your Instagram data export (see below)
- Optional, only for **Log into Instagram** / **Recheck statuses** / live counts: Google Chrome, plus `pip install "scrapling[fetchers]"`. Without these the rest of the app works normally; that one button just stays unavailable.

### To build the Windows installer

- Windows 10 or 11
- Python 3.10+
- [Inno Setup 6.x](https://jrsoftware.org/isdl.php) (free, ~5MB)
- `pip install pywebview pyinstaller "scrapling[fetchers]"`

## Quick start (source / CLI)

1. **Request your Instagram data export** in the Instagram app or web: `Settings → Accounts Center → Your information and permissions → Download your information`. Pick **JSON** format and request the download. Instagram emails you a zip; it usually arrives within minutes to an hour.

2. **Unzip the export.** You get a folder containing `followers_1.json` and `following.json`. Large accounts get the followers list split across `followers_1.json`, `followers_2.json`, and so on — every part is imported, so leave them all in the folder.

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

These steps produce `Output/InstagramFollowerReport-Setup-v<version>.exe`, the file you ship or install.

### 1. Install build dependencies

```powershell
pip install pywebview pyinstaller "scrapling[fetchers]"
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
3. Invokes `ISCC installer.iss` to compile the versioned installer in `Output/`.

### 3. Ship or install

The file to distribute or run is:

```
Output/InstagramFollowerReport-Setup-v<version>.exe
```

Double-click it on any Windows 10/11 machine to install. The installer will:

- Install the app to `%PROGRAMFILES%\InstagramFollowerReport\`.
- Install the WebView2 runtime if it is missing (Win10 only; Win11 ships it).
- Create a Start Menu shortcut.
- Optionally create a Desktop shortcut.
- Register a real uninstaller with Windows.
- Launch the app when the installer finishes.

## Installing and running on Windows

If you received an `InstagramFollowerReport-Setup-v<version>.exe`:

1. **Run the installer** (double-click).
2. **Follow the wizard**. If WebView2 is missing, click Yes to install it.
3. **Launch the app** when the installer finishes (checked by default).
4. **Add your Instagram username** in the header, then add your data using the **Add data** panel.

Your data lives in `%APPDATA%\InstagramFollowerReport\` and survives reinstalls and uninstalls.

## Updates and GitHub releases

The installed app checks the latest public GitHub Release when it opens. If a newer version exists, it offers to download and run the installer; app data remains in `%APPDATA%\InstagramFollowerReport\`.

To publish an update, push a semantic version tag such as `v1.2.3`. GitHub Actions builds that tag and attaches the versioned installer to its release. Regular pushes to `main` only build and test. People who installed a release before this updater existed need to install the first updater-enabled release manually once.

The installers are currently unsigned, so Windows SmartScreen or UAC may show a warning. Add code signing when distributing broadly.

## Using the app

### Add or switch Instagram accounts

Use the account selector in the header to switch profiles, or **Add account** to create one. Usernames are normalized to lowercase and may contain letters, numbers, periods, and underscores. Each profile keeps its own snapshots, report history, and flagged accounts.

If an older installation already has data, the app asks which Instagram username owns it. The migration copies that data into the selected profile and leaves the original files in place as a rollback backup.

### Add a snapshot from the browser / desktop UI

1. Select the Instagram account in the header.
2. Scroll to the **Add data** panel.
3. Enter the snapshot date as `MM-DD-YYYY`.
4. Attach `followers_1.json` and `following.json` from your unzipped Instagram export. If your export split the followers list into `followers_1.json`, `followers_2.json`, … select **all of them** in the followers field — the picker accepts a multi-file selection, and leaving parts out is the most common reason the follower count reads low.
5. Click **Import snapshot**.

The report refreshes automatically.

Follower and following cards count the unique records in that export. Instagram's
live profile totals can differ because exports are point-in-time downloads and may
still include disabled accounts; the card subtitle shows the total after your
manual disabled-account flags. See
[When the numbers don't match Instagram](#when-the-numbers-dont-match-instagram).

### When the numbers don't match Instagram

Open the **Doesn't match Instagram?** panel under the stat cards (or run
`python run.py audit`). It walks the arithmetic one step at a time — records read
from the export, duplicate handles removed, accounts flagged as disabled, the
total counted here — and names any export file it deliberately did **not** count.

Two things cause a gap, and they have different fixes:

- **Accounts Instagram no longer counts.** Deactivated, deleted, and banned
  accounts stay in your data export but disappear from the follower/following
  totals on your profile. Downloading a fresh export will not change this; every
  export contains them. After logging in, click **Find the difference** to compare
  the export with Instagram's paginated live relationship lists and show the
  candidate usernames directly. The CLI fallback (`python run.py audit
  --check-network`) checks handles individually. A
  handle can also read as gone because the person *renamed*. The app separates
  those out for you — see [Username changes](#username-changes) — and lists them
  apart from the accounts that are genuinely absent, with no Flag button.

  The comparison runs in both directions. Accounts that are in your export but
  not on Instagram are the deactivated/deleted/banned ones. Accounts that are on
  Instagram but not in your export are people you gained (or who renamed) after
  the export was generated. The ledger states the arithmetic outright:
  *counted here − absent from Instagram + absent from your export = what
  Instagram's list returned*.

  **Log into Instagram first.** Instagram login-walls anonymous profile views,
  and a walled response looks exactly like a rate-limited one, so without a
  saved session most handles come back *unverified* and the scan can't tell you
  much. The authenticated pass sees private and walled profiles and does the
  bulk of the clearing.

  If the live relationship list is unavailable, fallback results come back in three buckets: **confirmed gone** (safe to flag),
  **confirmed active**, and **couldn't be established either way**. Nothing is
  auto-flagged. If your count is still off after flagging the confirmed ones,
  the account you're looking for is in that third list — open each one and see
  whether it loads.
- **A stale export.** Instagram builds the file when you *request* it, not when
  you download it. Anything you followed or unfollowed in between is missing.
  Request a new export.

Files sitting in the export folder that are *not* people you follow —
`following_hashtags.json`, `pending_follow_requests.json`,
`recently_unfollowed_profiles.json`, `close_friends.json`,
`blocked_profiles.json` — are never counted, and the panel lists the ones it
skipped. (Followed hashtags being counted as followed accounts was a real bug
before v1.2.1; if your Following number used to read a few too high, that was it.)

```powershell
python run.py audit                                  # instant, no network
python run.py audit --check-network                  # sweep both lists
python run.py audit --check-network --scope following
python run.py audit --check-network --flag-gone      # and flag what's confirmed gone
```

### Username changes

A data export carries usernames and nothing else. By handle alone, a rename is
indistinguishable from "they unfollowed me and a stranger followed me" — which
is why a renamed person otherwise reads as a deleted account, gets flagged, and
quietly takes your counts out of step permanently.

The app resolves renames two ways and merges the results into one
**Changed their username** panel:

- **By Instagram account id (certain).** Every live relationship check records
  each account's numeric Instagram id, which never changes, into
  `identities.json` next to your flags. Same id under a different username is a
  rename, full stop. This needs a saved Instagram login and one run of
  **Find the difference** or the weekly recheck, and it catches renames that
  happened *after* your most recent export.
- **By follow timestamp (near-certain).** A rename doesn't break the
  relationship, so the follow timestamp survives it. A handle that vanished and
  a handle that appeared sharing an exact timestamp are the same person. This
  works with no login at all, but needs two or more snapshots on disk, and it
  declines to guess when the timestamp is ambiguous on either side.

Chains resolve to the handle in use today, so `a` → `b` → `c` shows as
`a` → `c`. If a released handle gets re-registered by someone else, the stale
rename is dropped rather than left to rewrite an unrelated person's row. A
flagged account that turns out to have merely renamed is unflagged
automatically.

### Flag suspicious accounts

Hover any row in **People I don't follow back** or **People who don't follow me back** and click **Flag**. The handle moves into the **Likely disabled / deactivated** panel and is excluded from active lists. Click **Unflag** to restore it. Flagged handles are stored in `data/disabledaccounts.json` (CLI) or the active profile's `disabledaccounts.json` (installed app).

The button beside the disabled-account heading reads **Log into Instagram** until you've authenticated, then **Recheck statuses**. Logging in opens a real, visible Chrome window (Chrome must already be installed) pointed at Instagram; log in by hand and the app detects it and closes the window itself — no password ever passes through this app. That session is saved to `ig-session/` (CLI) or `%APPDATA%\InstagramFollowerReport\ig-session\` (installed app) and reused headlessly from then on, so you only do this once. **Recheck statuses** starts the saved-account refresh immediately; otherwise the weekly refresh starts automatically when the app loads live counts.

Rechecks page through the live follower/following lists and remove a flag only when Instagram explicitly returns that username as a current relationship. A profile merely reappearing under the same username is not enough. Instagram sometimes hides disabled accounts from those lists even while including them in its headline total; absent usernames therefore stay flagged. After a successful check, the app repeats it in the background at most once every seven days when the app is opened. Failed or malformed Instagram responses change nothing and retry no sooner than the next day. The per-profile schedule lives in `disabled-recheck.json` next to `disabledaccounts.json`.

The Followers and Following cards make one authenticated profile check per account per app session and show Instagram's live totals when available, once you're logged in. If you haven't logged in, or the check fails, the cards fall back to the export totals.

The card subtitle always shows the full trail, so every step is visible whether or not you're logged in: `103 in export · 101 after 2 flags · 1 to identify`. Flags are subtracted *before* the comparison with Instagram — an account you've already flagged counts as explained, not as part of the remainder. That last number is what **Find the difference** hunts down.

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
    followers_1.json     # followers_2.json, ... when the export is split
    following_1.json
  disabledaccounts.json
  identities.json        # Instagram account ids seen live, and proven renames
dist/
  report.json
ig-session/           # only after you click "Log into Instagram"
```

### Installed Windows app

```
%APPDATA%\InstagramFollowerReport\
  profiles.json
  profiles/
    <internal-id>/
      data/
        MM-DD-YYYY/
          followers_1.json     # followers_2.json, ... when the export is split
          following_1.json
      disabledaccounts.json
      identities.json
  dist/
    report.json
  ig-session/          # only after you click "Log into Instagram"
```

The internal profile ID is generated by the app and is never derived from a username. `dist/report.json` is only the active profile's local render cache. Program files are read-only under `%PROGRAMFILES%\InstagramFollowerReport\`.

## Privacy

- Desktop profiles and the selected account are stored only in `%APPDATA%\InstagramFollowerReport\`.
- The local server binds to `127.0.0.1` only — never exposed to your LAN.
- **Log into Instagram** opens a real Chrome window you type your own password into directly on instagram.com; the app only watches for the resulting login cookie, it never sees or stores your password. The saved browser session (`ig-session/`) lives only on your machine and is never uploaded anywhere.
- **Recheck statuses** and the live follower/following totals run only when clicked/loaded and reuse that local session; they never fall back to anonymous checks. The opt-in `--check-disabled` CLI mode uses anonymous `instagram.com/<handle>` requests instead.
- No telemetry or analytics.
- `data/`, `dist/`, and `ig-session/` are git-ignored so your personal data never accidentally goes to a remote.

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
