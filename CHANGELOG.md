# Changelog

## [Unreleased]

### Added

- GitHub Actions workflow to build the Windows installer on pushes to `main`.
- Automated GitHub Releases that attach the compiled installer artifact.
- CHANGELOG-driven release notes in the release body.

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
