# Followers Report

A local Windows app that reconciles the follower/following lists in your downloaded Instagram data export against what Instagram itself currently shows, so you can see who has actually unfollowed you and which listed accounts are no longer real.

## Language

**Export record**:
One row for one relationship (a follower or a following), as it appears in a downloaded Instagram data-export JSON file, before any deduplication.
_Avoid_: entry, row

**Duplicate**:
An export record whose username appears more than once on the same side (e.g. `followers_1.json` and `followers_2.json` legitimately overlap on large accounts).

**Unique handle**:
One username per side (followers/following) after deduplication. This is the headline "followers"/"following" number before any flagged accounts are removed.
_Avoid_: raw count

**Flagged account**:
A username the app has recorded as no longer a real, reachable account — because it was deactivated, deleted, or manually marked — and therefore excluded from the "active" count. Persisted in `data/disabledaccounts.json`.
_Avoid_: disabled account (the storage file's name, but "flagged" is the concept used everywhere else)

**Active**:
Unique handles minus flagged accounts, for one side. This is the number meant to match what Instagram's own profile page currently shows.

**Renamed handle**:
A username that used to point to the same Instagram account as a different, current username — discovered by comparing the account's stable profile id across two live fetches. Tracked separately from flagged accounts, in the identities ledger.
_Avoid_: alias

**Verdict**:
The result of a live, per-account network probe for a single username's profile page: GONE, ACTIVE, or UNVERIFIED. One *origin* feeding a Reconciliation status, not a status by itself.

**Reconciliation status**:
The single per-handle record the redesign consolidates Flagged account, Renamed handle, and Verdict into: `{status, origin}`. `status` is one of `active | renamed | disabled | unverified | unchecked`; `unverified` means a probe ran and came back inconclusive, `unchecked` means no probe has run yet — the two are kept distinct on purpose (see the map's requirement that uncertain state surface explicitly). Renamed evidence is always checked before a `disabled` status can be drawn or kept, by construction of the state machine, not by caller discipline. See [Unify flagged, disabled, renamed, and verdict state into one reconciliation state](https://github.com/chrisj1259/followersreport/issues/2).

**Origin**:
Which evidence produced a Reconciliation status: `manual` (the user clicked flag/unflag), `heuristic` (a Verdict's True/False), `timestamp_heuristic` (export-diff timestamp match — the offline rename-detection fallback), `id_confirmed` (a live Instagram account-id match — exact), or `ig_confirmed` (Instagram's own tombstone rewrite of a deleted handle — the old "deleted" case, folded into `disabled` rather than kept as a separate status). Exact origins override weaker ones; `manual` only clears on explicit user action, except an `id_confirmed` rename, which overrides it — visibly surfaced as an auto-correction, since "flagged" means "not reachable" and proof of reachability makes the flag factually wrong regardless of who set it.

**Live counts**:
The two headline numbers (follower count, following count) read directly off Instagram's own profile page for the active profile, as opposed to numbers derived from the data export. Cached rather than fetched on every read (a fetch is an expensive live network check); a cached value carries an as-of time and is surfaced as stale past its TTL rather than presented as current fact. See [Fix live-count cache staleness and invalidation](https://github.com/chrisj1259/followersreport/issues/3).
_Avoid_: profile counts

**Gap**:
The difference between Active and Live counts for one side. A positive gap means the export lists more accounts than Instagram's profile page currently shows — the signature of accounts that were deactivated or deleted after the export was taken.

**Reconciliation**:
The process of walking export records down to unique handles, then to active handles, then comparing the result against live counts to explain any remaining gap.
_Avoid_: audit (used in code/CLI as `audit_counts`, but "reconciliation" is the concept)

**Sweep** / **Recheck**:
A background scan that re-probes flagged or listed accounts against Instagram to confirm their current verdict. Two exist today (a count-audit sweep and a disabled-account recheck) and are a likely source of the "numbers drift after new data comes in" symptom — see the wayfinder map, [Followers/Following Counting Redesign](https://github.com/chrisj1259/followersreport/issues/1).
