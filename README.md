# Instagram Follower Report

A local-first tool that reads your Instagram data export and shows you who's unfollowed you, who you don't follow back, who you share mutual follows with, and how your follower count moves week-over-week. Runs entirely on your machine; nothing is uploaded.

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

5. **(Optional, a week or so later)** Re-download the export and stage a second snapshot:

   ```
   python run.py import <path-to-unzipped-folder> --date MM-DD-YYYY
   python run.py
   ```

   Two snapshots unlock the week-over-week section: unfollowers, new followers, accounts who unfollowed me without ever following back.

## Browser upload (no terminal required after the first run)

Once `python run.py` is up, scroll to the bottom of the page to **Add a new snapshot**. Drag in the same two JSON files, type the date in `MM-DD-YYYY`, click the button. The page refreshes in place. Same flow as the CLI `import` subcommand, just GUI.

The form supports files up to 50 MB, which covers Instagram exports of up to ~50,000 followers.

## Flagging suspicious accounts

Click **Flag** next to any handle that looks like a bot or a deactivated account. That handle moves into the "Likely disabled / deactivated" panel (dark section) and is excluded from your active lists going forward. Click **Unflag** on the dark panel to restore it.

Flagged handles are stored in `data/disabledaccounts.json` (one flat JSON list of usernames) and persist across runs.

## Network-side disabled-account detection

```
python run.py --check-disabled
```

For each handle in your "Don't follow me back" list, this walks `instagram.com/<handle>` anonymously and merges handles whose profile page reads as deleted, deactivated, or 404 into the flagged panel. Adds roughly 0.5 seconds per handle, so don't run it on ten thousand. Requires internet.

If you skip `--check-disabled`, only manually-flagged handles appear in the dark panel.

## What the report shows

**Four stat cards up top:**

- **Followers** — total people who follow you.
- **Following** — total people you follow.
- **I don't follow back** — followers I haven't reciprocated. Nice people.
- **Don't follow me back** — accounts I follow that don't reciprocate. Soft-block candidates.

**Two panels below the cards:**

- **People I don't follow back** — full list, with timestamps and a flag button each.
- **People who don't follow me back** — same, plus flag.
- **Likely disabled / deactivated** — only shows up when there's something to show. Click a row to unflag.

**Tabs** along the top switch between snapshots if you have multiple. The latest one is selected by default.

**With two or more snapshots loaded, a soft-orange "Week over week" section appears** at the bottom of the page with four count cards (new/lost followers + new/lost followings) and two filled lists — "Unfollowers" (ever-followed-back) and "Unfollowed me — I never followed back".

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

Two or more snapshot folders in `data/` are required for the week-over-week section. To delete a snapshot, `rm -rf data/<MM-DD-YYYY>/` and re-run `python run.py`.

Both `data/` and `dist/` are git-ignored. They live only on your machine.

## Privacy

Everything runs on `localhost` (127.0.0.1). The HTTP server binds there and nowhere else, so your report is never visible to anyone on your LAN.

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

`import` requires `MM-DD-YYYY` for the `--date` argument (the browser form is also strict `MM-DD-YYYY` to keep folder names predictable).

## Help! Something went wrong

**`port 8000 is busy — try a different port`**  
Run with a custom port: `python run.py 9000`.

**`unrecognized folder name: '<something>'`**  
A folder in `data/` has a name that isn't a date. Rename it to `MM-DD-YYYY` or remove it. The parser also skips folders whose single-digit parts don't fit the format with a one-line warning — read your terminal output.

**Follower count drops to zero in the report**  
Check that you unzipped the right Instagram folder. `followers_1.json` should be at the top level of the source. If you have multiple unzipped folders (one for connected Facebook, etc.), pick the one whose root directly contains the JSON files.

**Browser form gives `date must be in MM-DD-YYYY format`**  
The server is strict (the form is strict, the server enforces). Use 2-digit month and day with a 4-digit year, like `07-15-2026`. `2026-07-15` is rejected on the browser path; use it via `python run.py import ... --date 2026-07-15` if you prefer ISO.

**The flag button doesn't seem to do anything**  
Look for the dark "Likely disabled / deactivated" panel near the bottom of the page. If the flagged handle doesn't appear there, you may have hit an old load() — refresh the browser tab and try again.

## FAQ

**My export is large. Will it choke?**  
The browser upload accepts files up to 50 MB. Instagram exports of even 50,000 followers usually fit. If you hit a cap, run the CLI directly: `python run.py import <src> --date X` reads from disk and skips the upload limit.

**Can I host this so my friends see it?**  
For the read-only view: yes — push to GitHub, build the static `dist/report.json` weekly with a CI job, deploy to GitHub Pages.  
For the live `/import` upload + the live `/flag` toggle: you need a Python host. Render, Fly.io, and Railway all support `python run.py` on their free tiers.

**I deleted `data/`. Will my flagged handles come back?**  
They were stored in `data/disabledaccounts.json`. If you delete the folder, you delete the flags. Re-flag any handles you care about through the row buttons — they re-persist immediately.

**Why is "Don't follow me back" bigger than "I don't follow back"?**  
Most people use Instagram to follow others without expecting a follow-back. The two lists aren't symmetric: the first shows accounts I follow that don't reciprocate (g − f at this snapshot). The second shows accounts who follow me that I haven't followed back (f − g).

**Where do the days ago ("7d ago") labels come from?**  
From your export's `string_list_data[].timestamp` field. If an export is newer than the post's timestamp, you'll see "today" / "yesterday" / "Nd ago".

## License

MIT. Do whatever, no warranty.
