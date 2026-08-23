# Changelog

## [1.2.1] - 2026-08-22

### Fixed

- **The Following card counted followed hashtags as people.** Instagram ships `following_hashtags.json` in the same export folder as `following.json`, and the filename router matched it on the substring `following`. Every hashtag you follow was counted as an account you follow, so the card overshot instagram.com by exactly that many — and re-importing or clearing snapshots could never fix it, because each fresh export contains the same file. The same bug pulled in `pending_follow_requests.json`, `recently_unfollowed_profiles.json`, `close_friends.json`, and `blocked_profiles.json` when a whole export folder was staged by hand. Those files are now vetoed by name, and any record whose link points at `/explore/` (a hashtag or location, not a person) is dropped even if the file is renamed.
- The **Saved snapshots** inventory used a third, divergent copy of the counting rule: it summed raw record counts without deduping, so it double-counted handles appearing in both `followers_1.json` and `followers_2.json` and could contradict the stat cards directly above it. It now routes and dedupes exactly like the report.
- **Flagging an account no longer looks like it did nothing.** The stat-card subtitle showed "N active after flags" in v1.1.2, but v1.2.0 replaced it with a live-counts-only label, so with no saved Instagram login the card fell all the way back to a bare "records in this export". Since the headline number is the raw export total by design, nothing on screen moved when you flagged an account. The subtitle now carries the whole trail — `103 in export · 101 after 2 flags · 1 to identify` — and subtracts flags *before* comparing against Instagram's live number, so an account you already flagged is counted as explained rather than as part of the remainder.
- **Only the first part of a split export was ever imported.** Instagram splits a large export across `followers_1.json`, `followers_2.json`, and so on. Both the `import` CLI and the in-app upload form took exactly one file per side, so everybody past the first part was dropped before the report ever saw them — the follower count then read low against instagram.com by however many people lived in parts 2 and up, and no amount of re-importing fixed it. Every part in a bucket is now staged, deduped across parts, and counted; the upload form accepts a multi-file selection. Re-importing a smaller export also clears the previous parts instead of stranding them in the folder.
- **Live relationship lists failed outright for anyone with more than roughly 2,000 accounts.** The pagination budget assumed Instagram would return 200 users per page; Instagram decides its own page size and routinely returns fewer, so the walk ran out of pages mid-list and abandoned the whole fetch. Both "Find the difference" and the weekly saved-flag recheck reported "Instagram relationship list unavailable" as a result. The walk now runs until Instagram stops handing out a cursor. A genuinely truncated list is still refused rather than returned partial — a partial list reads as "these accounts are gone" and would get perfectly healthy accounts flagged.
- The relationship fetch had a flat 180-second timeout, which is less time than paging a few thousand accounts through a headless browser takes. It killed its own subprocess mid-walk and reported the result as an Instagram failure. The timeout now scales with the size of the lists.
- **The anonymous fallback probe read Instagram's login wall as a live profile.** The wall is a normal 200 with a full `og:description`, so a deleted account behind one came back "confirmed active" and quietly disappeared from the list the user was hunting through. A wall now reads as *unverified*, which is what it is.
- Live follower/following totals could come back empty when Instagram's GraphQL profile response omitted the count fields; the request now falls through to the REST endpoint that always carries them instead of reporting the account as unreachable.
- `python run.py audit --check-network` never used the live relationship lists even with a saved login, so the CLI was strictly worse than the same button in the app. It now leads with the one-pass comparison and falls back to per-account probes only when logged out.
- When Instagram refuses the relationship lists, the app now says so and names the likely cause (an account renamed on Instagram but not here) instead of silently downgrading to the far weaker per-account scan and looking like it found nothing.
- A background scan or saved-flag recheck that hit an unexpected error could get stuck reporting "running" forever, since only a narrow set of expected exceptions cleared the job state. Any error now clears it.
- The saved-flag recheck's status check for a large flagged list made one unbatched subprocess call with no progress and no partial result if it failed partway; it now chunks the same way the count-audit scan already did.
- A flagged account that turned out to still be live on Instagram was reported as "on Instagram but not in your export" instead of as already accounted for, which told the user to request a fresh export instead of just unflagging it.

### Added

- **"Doesn't match Instagram?" panel** under the stat cards, showing the arithmetic behind each number — records read from the export, duplicate handles removed, accounts flagged as disabled, the total counted here — and, when a live profile check is available, how that compares with instagram.com's own count.
- **Find the difference** button in that panel checks every account in your export against instagram.com and names the ones with no live profile page, so a genuine gap (deactivated, deleted, or banned accounts, which stay in the export but stop counting on your profile) can be flagged instead of guessed at. Each result carries a Flag button.
- The sweep leads with your saved Instagram login when there is one. Instagram login-walls anonymous profile views, and a walled response is indistinguishable from a rate-limited one — leading with the anonymous probe meant a scan could return "unclear" for every handle and surface nothing. The authenticated pass is the only one that sees private and walled profiles, so it does the clearing and the anonymous probe is left to confirm what it could not.
- Scan results are reported three ways — **confirmed gone**, **confirmed active**, and **couldn't be established either way** — in two separate lists. Nothing is ever auto-flagged: "Instagram wouldn't answer" is not evidence an account is gone, and the unverified list is a shortlist to eyeball, which is exactly where a missing account hides when the confirmed list comes up empty.
- The sweep runs on its own thread with live progress (`Checking 80/163…`) and the page stays usable while it works. Walking a whole following list through a headless browser is minutes of work, and this server is single-threaded, so running it inline would have frozen the very page that launched it.
- `python run.py audit` prints the same reconciliation from the CLI, including which export files were counted and which were ignored. `--check-network` adds the live sweep, `--scope` narrows it to one list, and `--flag-gone` flags everything it confirms is gone.
- `report.json` snapshots now carry `source_files` plus `follower_records`, `following_records`, `duplicate_followers`, `duplicate_following`, `flagged_followers`, and `flagged_following` for JSON consumers.
- **Username changes are now tracked by Instagram account id, not guessed at.** The data export carries usernames and nothing else, so by handle alone a rename is indistinguishable from "they left and a stranger arrived" — the old detector inferred it from a follow timestamp surviving the diff, which needs two snapshots on disk and a timestamp that happens to be unique on both sides, and can never see a rename that happened before the first import or after the most recent export. Every live relationship fetch now records each account's numeric Instagram id, which never changes, into `identities.json` beside your flags. Same id, different username, and the rename is a fact rather than a heuristic. Both sources feed the same resolved map, and chains (`a` → `b` → `c`) resolve to the handle in use today. A handle that goes live again invalidates any rename recorded away from it, so a released-and-re-registered username cannot silently rewrite an unrelated person's row.
- **A "Changed their username" panel.** The pipeline had been resolving renames and writing them into `report.json` for a while, and nothing ever put them on screen — so a person who renamed looked exactly like a person who vanished, which is the confusion the whole feature existed to prevent.
- **"Find the difference" now reports both directions.** "In your export but not on Instagram" was only half the comparison. Accounts Instagram lists that your export has no row for — where a rename's new handle and every follow made after the export was generated end up — are now listed too. The reconciliation ledger states the closed equation: *counted here − absent from Instagram + absent from your export = what Instagram's list returned*. Renamed accounts are pulled out into their own list with no Flag button, because flagging a renamed person as disabled is the fastest way to put the counts permanently out of step.
- **An account with a live profile is never offered as a "disabled" candidate.** Missing from Instagram's relationship list has two causes with opposite answers: the *account* is gone (deactivated, deleted, banned, blocking you), or the *relationship* is gone — you unfollowed them, or they unfollowed you, after the export was generated — and the account is perfectly alive. They look identical from the export, and the scan used to call both "deactivated, deleted, banned" and tell you to flag them, which files live accounts into the disabled panel where nothing can release them. One authenticated profile fetch now separates the two: profiles that load are reported as *no longer connected* with no Flag button, and only unreachable ones are offered for flagging. When Instagram won't confirm a profile the list says so instead of implying certainty.
- The weekly recheck clears a flag when the flagged handle's *new* name is a current relationship, so someone who renamed while flagged no longer stays hidden forever under a name nobody uses.
- `python run.py audit` prints the same two-directional breakdown from the CLI.

### Changed

- **"Find the difference" repeats are now instant.** One relationship walk costs a browser launch, a profile page load, and one request per ~23 accounts — about 16 seconds for a 650-follower account, growing with the account. Running it twice, or running a scan straight after the weekly recheck, paid that twice for an answer that could not have changed. The walk is cached for five minutes and dropped whenever the active account changes.
- The live follower/following totals now ride along on the relationship walk instead of costing a browser launch of their own — the helper already loads the profile to read the account id, and the totals are on the same response.
- The scan status poll dropped from 2000ms to 750ms, which was adding up to two seconds of dead time after the work had already finished.
- The count cards no longer say "N to identify" about accounts a completed scan has already explained. Renamed and no-longer-connected accounts are reported as such, and "to identify" is reserved for accounts still genuinely unaccounted for.
- Saved disabled-account flags are now reconciled against every follower/following page Instagram exposes in a background job. A flag is removed only when that username is explicitly returned as a current relationship; absent usernames remain flagged even when Instagram's headline count differs.
- A successful recheck schedules the next automatic check for seven days later when the app is opened. Failed or malformed Instagram responses change nothing and back off for one day before retrying.

## [1.2.0] - 2026-08-11

### Fixed

- People who change their Instagram username are no longer mistaken for unfollowers or for disabled accounts. The export contains no user ID, so a rename previously looked like "they unfollowed you and a stranger followed you" — and because the old handle 404s on instagram.com, it also got flagged as deactivated. Renames are now matched by the follow timestamp, which survives a username change, and any saved flag on a renamed account is cleared automatically.
- Accounts Instagram has actually deleted (their handle becomes `__deleted__…`) are now detected from the export alone and flagged as disabled, with no network check needed.

### Changed

- The authenticated status check no longer depends on a separate, machine-specific Scrapling project. Scrapling and Playwright now ship inside the app itself, and a new **Log into Instagram** button (shown before you've authenticated) opens a real Chrome window for you to log in by hand once; the session is saved locally and reused for every later **Recheck statuses** click and live-count check. This is what makes both features actually usable by anyone who installs the app, not just on the machine they were built on.
- **Recheck statuses** now reports plainly when you haven't logged in yet instead of silently claiming it checked accounts and finding nothing.

## [1.1.4] - 2026-08-11

### Added

- A new **Recheck statuses** button checks every saved disabled-account flag through the existing authenticated Scrapling session. It restores only profiles Instagram positively identifies as active; blocked or unclear responses preserve their flags.
- Followers and Following cards now show Instagram's live profile totals from one cached authenticated check, while retaining the downloaded-export totals underneath.

## [1.1.3] - 2026-08-11

### Fixed

- Flagging a historical unfollower (someone absent from the latest snapshot) now removes them from "People who unfollowed me…" and surfaces them in "Likely disabled," instead of leaving them stuck in both lists.
- Unflagging a historical unfollower now correctly returns them to the historical-unfollower list.

## [1.1.2] - 2026-08-11

### Fixed

- Headline follower/following counts now reflect the raw export totals instead of silently subtracting flagged accounts; active-after-flag counts are shown separately.
- Unflagging a mutual account no longer misroutes it into "don't follow me back" — the report is regenerated and reloaded instead of being guessed client-side in JavaScript.
- Flagging/unflagging now rebuilds and reloads the report so cards and lists stay consistent.

## [1.1.1] - 2026-07-29

### Changed

- Reworked the web report's visual design: dark, high-contrast theme with a single red accent, sharp-cornered cards, pill-shaped buttons/badges, and a large lowercase display headline.
- Removed hover-only affordances (card borders, row tints, button color flips) so nothing changes on mouse-over except the `@username` links.
- `python run.py` (source-mode startup) now enables the same per-account profile backend as the packaged desktop app, so account switching and the legacy-migration prompt work from a plain checkout too.

### Fixed

- `_write_flagged` now reuses the shared atomic-JSON writer instead of a duplicated temp-file-and-rename implementation.

## [1.1.0] - 2026-07-28

### Added

- Desktop profiles for adding and switching any number of Instagram usernames.
- Header account selector, first-run account setup, and empty-profile guidance.
- Per-profile snapshots, history, and disabled-account flags under `%APPDATA%\InstagramFollowerReport\profiles\`.
- Atomic `profiles.json` manifest with opaque profile IDs and persisted active-account selection.
- One-time migration that asks which username owns existing desktop data while retaining the legacy files as a backup.
- GitHub Actions workflow to build the Windows installer on pushes to `main` and version tags.
- Startup update prompt that downloads and runs newer public GitHub Release installers.
- CHANGELOG-driven release notes in the release body.

### Changed

- GitHub Releases now publish only from semantic `vX.Y.Z` tags, with matching versioned installer assets.
- The installer records its installed version so the app only offers newer updates.

### Fixed

- Flagging a user from the "People who Unfollowed me that I never followed" panel no longer throws "row vanished from client state".

## [1.0.0] - 2026-07-16

### Added

- Initial release of Instagram Follower Report.
- Local-first Instagram follower analysis from JSON exports.
- Web UI showing followers, following, mutuals, non-followers, and likely-disabled accounts.
- Browser-based snapshot import.
- Manual flag/unflag for disabled or deactivated accounts.
- Windows desktop app and Inno Setup installer with WebView2 bootstrapper.
