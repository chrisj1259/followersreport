# Instagram Follower Report

A local-first tool that reads your Instagram data export and shows you who's unfollowed you, who you don't follow back, who you share mutual follows with. Runs entirely on your machine; nothing is uploaded.

Built for the curious Instagram user who wants a more useful view of their follower list than the app gives.

## Requirements

- Python 3.10+
- A modern browser (Chrome, Firefox, Safari, or Edge)
- Your Instagram data export (see step 1 below)

There is nothing to install. The pipeline is Python's standard library only.

## Quick start

1. **Get your Instagram data export.** In the Instagram app or web: Settings → Accounts Center → Your information and permissions → Download your information. Pick **JSON** format and request the download. Instagram emails you a zip; the zip arrives within minutes to an hour.

2. **Unzip the export.** You get a folder whose contents include `followers_1.json` and `following.json`. The exact outer folder name varies (`your_instagram_activity/` is common).

3. **Stage your first snapshot.** From the root of this repo:

   ```
   python run.py import <path-to-unzipped-folder> --date MM-DD-YYYY
   ```

   Use today's date in `MM-DD-YYYY` format, e.g. `07-15-2026`. The two JSON files land in `data/07-15-2026/` (renamed to the canonical names `followers_1.json` and `following.json`).

4. **Run the report.**

   ```
   python run.py
   ```

   A browser window opens to `http://localhost:8000` showing your report. Hit `Ctrl+C` to stop.

5. **(Optional, a week or so later)** Re-download the export and stage another snapshot:

   ```
   python run.py import <path-to-unzipped-folder> --date MM-DD-YYYY
   python run.py
   ```

   The report always shows the latest snapshot.

## Adding a snapshot from the browser

The same `import` flow runs without leaving the report page. Scroll to **Add data** at the bottom of the page while the server is running:

1. **Date** — type the snapshot's date. As you type digits, dashes slot in automatically after the 2nd and 5th characters, so `07152026` becomes `07-15-2026` as you type. The field is `MM-DD-YYYY`-strict: unpadded `7-15-2026` and `YYYY-MM-DD` are both rejected.
2. **Followers file** — attach `followers_1.json` (the file inside your unzipped Instagram export).
3. **Following file** — attach `following.json`.
4. Click **Import snapshot**. The server stages both files into `data/<MM-DD-YYYY>/` and re-runs analyze in the same request — `dist/report.json` is rewritten before the browser's auto-refresh, so the next render shows the new counts without restarting `python run.py`.

## Flagging suspicious accounts

Hover any row in **People I don't follow back** or **People who don't follow me back** and click **Flag**. The handle moves into the "Likely disabled / deactivated" panel (dark section at the bottom) and is excluded from your active lists going forward. Click **Unflag** on the dark panel to restore it. The disclosure chevron above the disabled panel auto-opens so you can see the move take effect immediately.

Flagged handles are stored in `data/disabledaccounts.json` (one flat JSON list of lowercased usernames, e.g. `["dead_account", "spammer_xyz"]`) and persist across runs. Edit this file directly if you'd rather skip the UI.

## Network-side disabled-account detection

```

python run.py --check-disabled
```

For each handle in your "Don't follow me back" list, this walks `instagram.com/<handle>` anonymously and merges handles whose profile page reads as deleted, deactivated, or 404 into `data/disabledaccounts.json`. Adds roughly 0.5 seconds per handle, so don't run it on ten thousand. Requires internet.

If you skip `--check-disabled`, only the handles you manually maintain in `data/disabledaccounts.json` appear in the disabled panel.

## What the report shows

**Four stat cards up top:**

- **Followers** — total people who follow you.
- **Following** — total people you follow.
- **I don't follow back** — followers I haven't reciprocated. Nice people.
- **Don't follow me back** — accounts I follow that don't reciprocate. Soft-block candidates.

**Two panels below the cards:**

- **People I don't follow back** — full list of followers you haven't reciprocated.
- **People who don't follow me back** — accounts you follow that don't reciprocate.
- **Likely disabled / deactivated** — only shows up when there's something to show. Read-only list sourced from `data/disabledaccounts.json` and `--check-disabled`.

The report always shows the latest snapshot — read-only. Older snapshots live on disk in `data/` for analytical use but aren't surfaced in the UI.

## Where the data lives

```
data/
  7-12-2026/             <- one folder per dated snapshot (your existing data)
    followers_1.json    <- Instagram export (copied as-is on import)
    following.json      <- Instagram export (copied as-is on import)
  08-15-2026/            <- a second snapshot, after `python run.py import ...`
    followers_1.json
    following.json
  disabledaccounts.json  <- your flagged handles; persists across runs
dist/
  report.json            <- the generated report, rebuilt on every analyze
```

The latest snapshot folder in `data/` drives the report. Older snapshots are kept on disk and surfaced in `dist/report.json` as week-over-week folds for analytical use, but the read-only UI shows only the most recent one. To delete a snapshot, `rm -rf data/<MM-DD-YYYY>/` and re-run `python run.py`.

Both `data/` and `dist/` are git-ignored. They live only on your machine.

## Privacy

The HTTP server is hard-pinned to `127.0.0.1` (IPv4 loopback), not the bare `localhost` host — so the bind lands on the address your browser opens regardless of `getaddrinfo`. Your report is never visible to anyone on your LAN.

The one optional external call is `--check-disabled`, which makes anonymous `instagram.com/<handle>` HEAD requests without authentication or cookies. Off by default.

No telemetry, no analytics. `data/` and `dist/` are git-noted in `.gitignore` so they never accidentally go to a remote.

## CLI flags at a glance

```
python run.py                       # analyze data/ + serve on :8000
python run.py 9000                  # analyze data/ + serve on :9000
python run.py --check-disabled      # same but additionally walks the DFMB list
python run.py --help                # prints the full usage
python run.py import <src> --date X # stage a new snapshot; no serve
```

`import` requires `MM-DD-YYYY` for the `--date` argument to keep snapshot folder names predictable.

## Help! Something went wrong

**`port 8000 is in use by another process`**  
Another `python run.py` is bound to that port (typically a stale process from a crashed prior run). On Windows, pick the right PID from the list and force-kill it:

```
tasklist | findstr python.exe
taskkill /F /PID <pid>
```

On macOS / Linux, `SO_REUSEADDR` lets the kernel hand the port back from a `TIME_WAIT` socket immediately — a fresh `python run.py` right after a crash binds cleanly without seeing this error. If you’d rather kill the stale process explicitly, the recipe the server prints is:

```
lsof -i :8000
lsof -ti :8000 | xargs kill
```

…or just rerun on a different port: `python run.py 8001`.

**`unrecognized folder name: '<something>'`**  
A folder in `data/` has a name that isn't a date. Rename it to `MM-DD-YYYY` or remove it. The parser also skips folders whose single-digit parts don't fit the format with a one-line warning — read your terminal output.

**Follower count drops to zero in the report**  
Check that you unzipped the right Instagram folder. `followers_1.json` should be at the top level of the source. If you have multiple unzipped folders (one for connected Facebook, etc.), pick the one whose root directly contains the JSON files.

**`python run.py import --date` says `date must be in MM-DD-YYYY format`**  
The CLI subcommand is strict MM-DD-YYYY so folder names stay predictable. Use 2-digit month and day with a 4-digit year, like `07-15-2026`.

**The disabled panel doesn't show what I expect**  
It's hidden by default — click the red chevron above the panel to drop down the flagged accounts. If your disabled list is empty, the panel is hidden entirely. To bulk-edit without clicking each row, edit `data/disabledaccounts.json` (a flat JSON list of lowercase usernames) and re-run `python run.py`.

## FAQ

**My export is large. Will it choke?**  
`python run.py import <src> --date X` reads from disk and is bounded by available RAM. Instagram exports of even 50,000 followers usually fit comfortably.

**Can I host this so my friends see it?**  
Yes — the page is fully read-only and `dist/report.json` is a plain static file. Push to GitHub, regenerate `dist/report.json` weekly via a CI job (`python run.py` after staging snapshots), and deploy to GitHub Pages. The server itself no longer has any endpoints that accept writes, so a static host works.

**I deleted `data/`. Will my flagged handles come back?**  
They were stored in `data/disabledaccounts.json`. If you delete the folder, you delete the flags. Restore them by recreating `data/disabledaccounts.json` (a flat JSON list of usernames) and re-flagging any handles you care about via the row buttons — they re-persist immediately on the next click.

**Why is "Don't follow me back" bigger than "I don't follow back"?**  
Most people use Instagram to follow others without expecting a follow-back. The two lists aren't symmetric: the first shows accounts I follow that don't reciprocate (g − f at this snapshot). The second shows accounts who follow me that I haven't followed back (f − g).

**Where do the days ago ("7d ago") labels come from?**  
From your export's `string_list_data[].timestamp` field. If an export is newer than the post's timestamp, you'll see "today" / "yesterday" / "Nd ago".

## License

MIT. Do whatever, no warranty.
