# Changelog

## [Unreleased]

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
