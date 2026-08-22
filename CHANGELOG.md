# Changelog

## [Unreleased]

## [1.2.1] - 2026-08-22

### Fixed

- **The Following card counted followed hashtags as people.** Instagram ships `following_hashtags.json` in the same export folder as `following.json`, and the filename router matched it on the substring `following`. Every hashtag you follow was counted as an account you follow, so the card overshot instagram.com by exactly that many — and re-importing or clearing snapshots could never fix it, because each fresh export contains the same file. The same bug pulled in `pending_follow_requests.json`, `recently_unfollowed_profiles.json`, `close_friends.json`, and `blocked_profiles.json` when a whole export folder was staged by hand. Those files are now vetoed by name, and any record whose link points at `/explore/` (a hashtag or location, not a person) is dropped even if the file is renamed.
- The **Saved snapshots** inventory used a third, divergent copy of the counting rule: it summed raw record counts without deduping, so it double-counted handles appearing in both `followers_1.json` and `followers_2.json` and could contradict the stat cards directly above it. It now routes and dedupes exactly like the report.

### Added

- **"Doesn't match Instagram?" panel** under the stat cards, showing the arithmetic behind each number — records read from the export, duplicate handles removed, accounts flagged as disabled, the total counted here — and, when a live profile check is available, how that compares with instagram.com's own count.
- **Find the difference** button in that panel checks every account in your export against instagram.com and names the ones with no live profile page, so a genuine gap (deactivated, deleted, or banned accounts, which stay in the export but stop counting on your profile) can be flagged instead of guessed at. Each result carries a Flag button.
- `python run.py audit` prints the same reconciliation from the CLI, including which export files were counted and which were ignored. `--check-network` adds the live sweep, `--scope` narrows it to one list, and `--flag-gone` flags everything it confirms is gone.
- `report.json` snapshots now carry `source_files` plus `follower_records`, `following_records`, `duplicate_followers`, `duplicate_following`, `flagged_followers`, and `flagged_following` for JSON consumers.

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
