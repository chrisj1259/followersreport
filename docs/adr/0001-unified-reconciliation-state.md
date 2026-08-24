---
status: accepted
---

# Unify flagged, disabled, renamed, and verdict state into one Reconciliation status

Today a handle's "is this a real, current account" status is spread across three mechanisms that don't share a model: a flagged set (`disabledaccounts.json`), a renamed-handle ledger (`identities.json`'s derived `renames`), and a live, one-shot Verdict computed per network probe and never persisted. They interact badly — a Renamed handle could get flagged as a disabled Flagged account because the rename ledger wasn't consulted first (commits 309f9fa, 2164099). We replace all three with one canonical per-handle **Reconciliation status** record, computed by a single function every caller routes through, so the renamed-before-disabled check is structural rather than a discipline every call site has to remember.

This ADR is the composed design for [Followers/Following Counting Redesign](https://github.com/chrisj1259/followersreport/issues/1), synthesizing [Unify flagged, disabled, renamed, and verdict state into one reconciliation state](https://github.com/chrisj1259/followersreport/issues/2), [Fix live-count cache staleness and invalidation](https://github.com/chrisj1259/followersreport/issues/3), and [Design coordination model for the background sweep jobs](https://github.com/chrisj1259/followersreport/issues/4) into one implementable spec, plus the storage format and stat-card composition those three left open. It is the destination artifact for [Write the unified reconciliation design spec and regression-test plan](https://github.com/chrisj1259/followersreport/issues/5). Full implementation is a follow-up effort; this document is precise enough to build against without further guessing.

## Reconciliation status: shape and transitions

```
status: active | renamed | disabled | unverified | unchecked
origin: manual | heuristic | timestamp_heuristic | id_confirmed | ig_confirmed
```

- **active** — confirmed live (present in Instagram's live data, or a heuristic probe confirmed it).
- **renamed** — old handle → new handle. `origin: id_confirmed` (live account-id match, exact) or `timestamp_heuristic` (export-diff timestamp match, weaker); `id_confirmed` wins on conflict.
- **disabled** — not a real, reachable account. `origin: manual` (user flagged it), `heuristic` (`looks_disabled()` returned `True`), or `ig_confirmed` (Instagram's own tombstone rewrite — folds the old "deleted" case in here rather than a sixth status).
- **unverified** — a probe ran and came back inconclusive (`looks_disabled()` returned `None`).
- **unchecked** — no probe has run yet. Kept distinct from `unverified`: "we tried and Instagram wouldn't say" and "we haven't looked" are different facts for the user.

Transition rules, enforced structurally by the single status-computing function, not by caller discipline:

1. Rename evidence is always checked before any `disabled` status can be drawn or kept. This makes the renamed-flagged-as-disabled bug class impossible by construction.
2. An `id_confirmed` rename overrides any existing status, including a `manual` disabled flag — surfaced visibly as an auto-correction, never silently. "Flagged" means "not reachable"; exact proof of reachability makes the flag factually wrong regardless of who set it.
3. A `heuristic` verdict never overrides a `manual` origin. Only the user's own unflag clears a manual flag (short of rule 2).

## Storage: `reconciliation.json`

One new file, `{username: {status, origin, updated_at}}`, at the same location and per-profile scoping as today's `disabledaccounts.json`. It replaces `disabledaccounts.json` entirely and replaces the derived `renames` dict in `identities.json`. `identities.json`'s `ids` mapping (raw Instagram account-id evidence, used to detect that a rename occurred in the first place) stays separate — it's the raw evidence the Reconciliation status is built from, not itself a status.

Migration is lazy and happens once: on first read, if `reconciliation.json` is absent but the old files exist, build it from them (flagged handles → `disabled`/`manual`; ledger entries → `renamed`/`id_confirmed` or `renamed`/`timestamp_heuristic`; everything else unseen → `unchecked`). This reuses the app's existing legacy-migration pattern (`_create_profile(migrate_legacy=True)` in run.py, exercised by smoke.py's T9) rather than inventing a new mechanism.

## Live counts: staleness and refresh

Each `LIVE_PROFILE_COUNTS` entry gains an `as_of` timestamp; a 6-hour TTL (named constant) determines staleness. A stale entry is still served immediately — never block a request behind a fresh Chrome-backed fetch — but exposed via a derived `stale` flag alongside `as_of`, in both `/live-counts` and `_reconcile_side`'s `live` field. `/live-counts` is the sole trigger for refresh: when it serves a stale count it also kicks an async forced re-fetch of `_live_profile_counts()`, alongside its existing `start_recheck_job()` call. `count-audit` / `cached_live_profile_counts()` stays purely passive — it never triggers a fetch, only reads whatever a prior fetch left behind.

`LIVE_PROFILE_COUNTS` stays in-memory only, reset on app restart, as today. The async refresh needs no `_STATE_LOCK` coordination: it's a single in-memory dict assignment, not a read-modify-write on any JSON file, and it's already serialized against both background jobs' own network calls via the existing `_IG_HELPER_LOCK`.

## Sweep-job coordination: `_STATE_LOCK`

One new lock, `_STATE_LOCK` (matching the existing `_IG_HELPER_LOCK`/`_LOGIN_PROC_LOCK` pattern), held across the full read-modify-write critical section — not just around individual file I/O calls — by every caller touching `reconciliation.json`. Wrapping only the read/write helper functions individually would not close the race; the window is between a caller's read and its write, so the lock must span that whole region at each call site.

Call sites that must acquire it (today's equivalents, traced by every unlocked read-modify-write on `disabledaccounts.json`/`identities.json`):

- `record_live_identities` (run.py:1093) — called by both background jobs
- `start_recheck_job`'s inline flagged-set update (run.py:1794-1805)
- `add_flagged` / `unflag` (run.py:414-427) — the manual flag/unflag HTTP handler
- `run_analyze`'s renamed/deleted flagged-set reconciliation (run.py:2907-2911)
- `recheck_flagged_accounts_by_network` (run.py:753-775)
- the manual-flag HTTP handler's `unflag()`/`add_flagged()` + `_refresh_active_report()` pair (run.py:2154-2155) — one lock acquisition covering both calls, not two, or another writer can land between them

Scope is narrow: `_STATE_LOCK` must never wrap either job's live-Instagram-fetch portion, which is already serialized by `_IG_HELPER_LOCK` and can hold for up to 30 minutes. Wrapping the whole job would mean a manual flag/unflag click could block for the job's full duration. Each job acquires and releases `_STATE_LOCK` only around its local bookkeeping steps.

No queue for concurrent sweep requests: keep today's reject-and-report-status behavior (`start_scan_job` returns the current running state; `start_recheck_job` raises if a different profile's recheck is mid-flight). HTTP consistent reads need no new mechanism — `report.json` is already served as a static file, replaced atomically (temp-file + `os.replace`, run.py:117), so a request always sees a complete old-or-new version. Recheck cadence stays weekly (`RECHECK_INTERVAL_SECONDS`); `timestamp_heuristic` origin is the designed-for offline interim signal between live `id_confirmed` checks, so tightening the cadence isn't needed.

## Composing the stat-card numbers

The headline **Active** count for a side — the number meant to match Instagram's own profile page — is handles with status `active` OR `renamed`: a renamed account is still real and reachable, just under a new handle. `disabled` is excluded, same as today. `unverified` and `unchecked` are tracked as two distinct explicit counts, never folded into `active` or `disabled` — this is the map's core requirement, that uncertain state surface rather than resolve silently one way or the other.

`_reconcile_side`'s existing shape stays structurally the same (records → duplicates → unique → [flagged →] active → live → gap); `flagged` is superseded by `disabled`, and `unverified`/`unchecked` are new fields added alongside `active`. Data the stat cards will have available per side: `active`, `disabled`, `unverified`, `unchecked`, `live`, `as_of`, `stale`, `gap`. How this data is rendered is the next section.

## Stat-card UI: staleness and unconfirmed handles

Resolved in [Design stat-card UI for live-count staleness and unverified/unchecked reconciliation status](https://github.com/chrisj1259/followersreport/issues/6). Both additions reuse the card's existing mechanism — the "·"-separated trail text under the headline number (`renderStats`'s `liveLabel`, index.html:1389-1431) — rather than introducing new UI chrome:

- **Unverified/unchecked handles**: `unverified` and `unchecked` are combined into one figure ("N unconfirmed") appended to the trail, gated the same way the existing `flagged > 0` check is — shown only when the combined count is non-zero. The unverified-vs-unchecked split (which matters for what the user should *do* — re-run a check vs. it just hasn't been checked yet) lives in the existing detail panel below the cards (`reconcile-unverified-note`/`-list`), not on the card itself; the card's job is "something here isn't confirmed active," not the full breakdown.
- **Live-count staleness**: since one fetch populates `LIVE_PROFILE_COUNTS` for both Followers and Following together, there is exactly one `stale`/`as_of` per profile, not one per card — both cards are always equally fresh or equally stale. When `stale` is true, the trail appends "live as of Xh ago"; when fresh, nothing is added — silence-means-fresh, matching the card's existing convention (e.g. "matches Instagram" only appears when there's something to say).
- **No shared or distinct visual treatment beyond wording**: both render as plain trail text with no color, dot, or badge. The two phrases are self-distinguishing by content ("N unconfirmed" is about handles, "as of Xh ago" is about the live number) — adding a color/icon vocabulary for a two-phrase trail is speculative chrome, revisit only if real usage shows the two get confused.

## Regression-test plan

smoke.py's T12–T14b already discharge the map's commitment to "one automated check covering the core reconciliation math" (export records → unique → active → gap) against today's three-mechanism implementation: T12 exercises `audit_counts`'s full arithmetic and the live-cache gap; T13 exercises id-proved rename chains; T14 exercises two-way reconciliation (renamed / missing / not-in-export kept apart); T14b exercises the live-account guard (a relationship ending is never mistaken for an account being gone). This is existing groundwork, not a gap this ADR needs to fill.

When the follow-up implementation effort lands `reconciliation.json`, it must:

- Rewrite assertions that currently read `_read_flagged()`/`known_renames()` internals (smoke.py around lines 99, 125-126, 245, 663, 679, 726-733, 743) to instead read the unified record's `{status, origin}`.
- Add one fixture handle per status to exercise every branch of the state machine and the "renamed beats disabled" transition rule: a plain `active` handle; a `renamed` handle whose old and new usernames both appear across two `record_live_identities()` calls (proves the `id_confirmed` path, mirroring T13's existing pattern); a `disabled` handle (network-probe-confirmed gone); an `unverified` handle (probe inconclusive / login-walled, mirroring T16's login-wall fixture); an `unchecked` handle (never probed).
- Keep T14b's existing regression case working under the new model: a handle missing from Instagram's relationship list because the *relationship* ended, not the account, must never be flagged (`disabled`) — it exercises the departed-vs-candidate split.

No new test framework or dependency: this repo already has two test entry points (smoke.py's hand-rolled asserts, test_updates.py's stdlib `unittest`), neither uses pytest, and the redesign doesn't need one either.

## Out of scope

- Actual implementation of `reconciliation.json`, `_STATE_LOCK`, the TTL/staleness fields, and the stat-card trail-text changes above — a follow-up effort building against this ADR.
