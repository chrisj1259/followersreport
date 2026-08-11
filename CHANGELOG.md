# Changelog

## [Unreleased]

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
