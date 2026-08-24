"""One-shot end-to-end smoke test for the import+analyze+persistence+flag+HTTP-import flow.

Run with: python smoke.py
Exit code 0 + final line 'SMOKE_TEST_OK' = pass.
Sandboxes APPDATA so the real %APPDATA% is never touched.
"""
import argparse
import json
import os
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sb = Path(tempfile.mkdtemp(prefix="ifr-smoke-"))
os.environ["APPDATA"] = str(sb)
print(f"sandbox APPDATA: {sb}")

import run

run._set_data_paths(
    data_dir=sb / "data",
    out_dir=sb / "dist",
    disabled_file=sb / "disabledaccounts.json",
)
assert run.ROOT == sb, f"ROOT rebind wrong: {run.ROOT}"
print("ROOT rebind OK (ROOT == sandbox sb)")


def ig(name):
    return {
        "title": name,
        "string_list_data": [{"value": name, "href": f"https://instagram.com/{name}", "timestamp": 1700000000}],
    }


# --- username-change detection (pure functions, no I/O) ---
def rec(name, ts):
    return {"username": name, "ts": ts}


# same timestamp on both sides of the diff = same person, new handle
assert run.detect_renames(
    [rec("old_name", 100), rec("stays", 200), rec("real_unfollow", 300)],
    [rec("new_name", 100), rec("stays", 200), rec("real_new", 999)],
) == {"old_name": "new_name"}, "rename not matched by timestamp"
# ambiguous timestamps stay unmatched rather than pairing strangers
assert run.detect_renames(
    [rec("a", 100), rec("b", 100)], [rec("c", 100), rec("d", 100)]
) == {}, "ambiguous timestamp must not pair"
assert run.detect_renames([rec("a", None)], [rec("b", None)]) == {}, "missing ts must not pair"
# a -> b -> c resolves to the current handle
assert run.renames_across([
    ("t1", [rec("a", 100)], []), ("t2", [rec("b", 100)], []), ("t3", [rec("c", 100)], []),
]) == {"a": "c", "b": "c"}, "rename chain not resolved to latest handle"
# a deleted account gets tombstoned, not renamed — it must stay flagged
assert run.renames_across([("t1", [rec("gone", 100)], []),
                           ("t2", [rec(run.TOMBSTONE_PREFIX + "x9", 100)], [])]) == {
    "gone": run.TOMBSTONE_PREFIX + "x9"}, "tombstone pairing broken"
print("username-change detection OK")

date_str = "06-30-2026"
t4_date = "07-01-2026"
(sb / "data" / date_str).mkdir(parents=True)
(sb / "data" / date_str / "followers_1.json").write_text(
    json.dumps([ig(n) for n in ["alice_real", "bob_real", "carol_real"]])
)
(sb / "data" / date_str / "following.json").write_text(
    json.dumps([ig(n) for n in ["bob_real", "carol_real", "zelda_real", "yolanda_real"]])
)

# === T1: first analyze produces correct counts ===
assert not (sb / "dist" / "report.json").exists()
run.run_analyze(argparse.Namespace(check_disabled=False))
rep1 = json.loads((sb / "dist" / "report.json").read_text())
s1 = rep1["snapshots"][0]["counts"]
nfb = sorted(u["username"] for u in rep1["snapshots"][0]["not_followed_back"])
dfmb = sorted(u["username"] for u in rep1["snapshots"][0]["dont_follow_me_back"])
assert s1["followers"] == 3, s1
assert s1["following"] == 4, s1
assert nfb == ["alice_real"], nfb
assert dfmb == ["yolanda_real", "zelda_real"], dfmb
print(f"T1 analyze OK: followers=3 following=4 nfb={nfb} dfmb={dfmb}")

# === T2: persistence -- delete report.json, re-analyze, expect identical ===
(sb / "dist" / "report.json").unlink()
run.run_analyze(argparse.Namespace(check_disabled=False))
rep2 = json.loads((sb / "dist" / "report.json").read_text())
assert rep2 == rep1, "PERSISTENCE BROKEN"
print("T2 persistence OK: re-run reproduces identical report.json")

# === T3: flag pipeline -- flag zelda, expect DFMB -> disabled_accounts ===
run.add_flagged("zelda_real")
run.add_flagged("bob_real")
assert {"bob_real", "zelda_real"}.issubset(run._read_flagged()), run._read_flagged()
recon = json.loads((sb / "reconciliation.json").read_text())
assert recon["zelda_real"]["status"] == "disabled" and recon["zelda_real"]["origin"] == "manual", recon["zelda_real"]
assert recon["bob_real"]["status"] == "disabled" and recon["bob_real"]["origin"] == "manual", recon["bob_real"]
(sb / "dist" / "report.json").unlink()
run.run_analyze(argparse.Namespace(check_disabled=False))
rep3 = json.loads((sb / "dist" / "report.json").read_text())
dfmb3 = sorted(u["username"] for u in rep3["snapshots"][0]["dont_follow_me_back"])
disabled3 = sorted(u["username"] for u in rep3["snapshots"][0]["disabled_accounts"])
assert "zelda_real" not in dfmb3, dfmb3
assert disabled3 == ["bob_real", "zelda_real"], disabled3
assert rep3["snapshots"][0]["counts"]["followers"] == 3, rep3["snapshots"][0]["counts"]
assert rep3["snapshots"][0]["counts"]["following"] == 4, rep3["snapshots"][0]["counts"]
assert rep3["snapshots"][0]["counts"]["active_followers"] == 2, rep3["snapshots"][0]["counts"]
assert rep3["snapshots"][0]["counts"]["active_following"] == 2, rep3["snapshots"][0]["counts"]
print(f"T3 flag pipeline OK: zelda moved DFMB->disabled: dfmb={dfmb3} disabled={disabled3}")

# === T3b: saved disabled flags recover only on a positive active verdict,
# and only when they weren't flagged manually -- rule 3 says a heuristic
# verdict never overrides a manual origin, only the user's own unflag does
# (short of a proven id_confirmed rename). bob_real/zelda_real were flagged
# via add_flagged (manual) in T3, so a clean False verdict for bob_real must
# NOT reenable him; heur_flagged is seeded with a heuristic-origin flag
# instead, to prove the positive-verdict recovery path still works.
run.add_flagged("mystery_real")
run._write_flagged_file(run.DISABLED_FILE, run._read_flagged() | {"heur_flagged"})
recon = json.loads((sb / "reconciliation.json").read_text())
assert recon["heur_flagged"]["origin"] == "heuristic", recon["heur_flagged"]
real_looks_disabled = run.looks_disabled
run.looks_disabled = lambda username, timeout=4.0: {
    "bob_real": False,
    "zelda_real": True,
    "mystery_real": None,
    "heur_flagged": False,
}[username]
try:
    assert run.recheck_flagged_accounts_by_network() == ["heur_flagged"]
finally:
    run.looks_disabled = real_looks_disabled
assert run._read_flagged() == {"bob_real", "mystery_real", "zelda_real"}, run._read_flagged()
print("T3b saved-flag recheck OK: heuristic flags recover on a positive verdict; manual flags don't (rule 3)")

# === T4: end-to-end HTTP /import through FlagHandler ===
httpd = run._run_server(0, run.FlagHandler)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.3)
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/profiles", timeout=5) as r:
        cli_profiles = json.loads(r.read())
    assert cli_profiles == {
        "profile_mode": False,
        "active_profile_id": None,
        "profiles": [],
        "legacy_needs_username": False,
    }, cli_profiles
    print("T4a CLI profile compatibility OK: profile mode disabled")

    boundary = "----ifr"
    body = (
        f"--{boundary}\r\n"
        + "Content-Disposition: form-data; name=\"date\"\r\n\r\n"
        + f"{t4_date}\r\n"
        + f"--{boundary}\r\n"
        + "Content-Disposition: form-data; name=\"followers\"; filename=\"x.json\"\r\n"
        + "Content-Type: application/json\r\n\r\n"
        + json.dumps([ig("eve_real"), ig("frank_real")])
        + f"\r\n--{boundary}\r\n"
        + "Content-Disposition: form-data; name=\"following\"; filename=\"y.json\"\r\n"
        + "Content-Type: application/json\r\n\r\n"
        + json.dumps([ig("eve_real")])
        + f"\r\n--{boundary}--\r\n"
    ).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/import",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        import_resp = json.loads(r.read())
    assert import_resp["ok"] is True
    rep4 = json.loads((sb / "dist" / "report.json").read_text())
    # ponytail: T4 stages into t4_date (a NEW date) so prior snapshot at
    # date_str stays intact from T1-T3 -- readers can clearly see the
    # diff each sub-test made. Read snapshots[-1] which is also how
    # index.html's latestSnapshot() (line 693) renders the report.
    latest_snap = rep4["snapshots"][-1]
    nfb4 = sorted(u["username"] for u in latest_snap["not_followed_back"])
    assert "frank_real" in nfb4 and len(rep4["snapshots"]) == 2, nfb4
    print(f"T4 HTTP /import OK: 200 {import_resp} -> frank in latest NFB={nfb4}")

    # === T4b: verify the new unilateral_unfollowed field in weeks[0] ===
    # ponytail: after T4 we have 2 snapshots (06-30-2026, 07-01-2026).
    # The fold_pair computes lost followers I never followed back. With
    # our test data: lost followers are {alice, bob, carol}; I followed
    # bob+carol back in 06-30 (gset_p), so they don't count.
    # alice was never in any following set, so she IS pure unrequited
    # -> unilateral_unfollowed == [alice_real].
    rep_after_t4 = json.loads((sb / "dist" / "report.json").read_text())
    assert "weeks" in rep_after_t4 and len(rep_after_t4["weeks"]) == 1, \
        f"expected 1 week after T4, got {rep_after_t4.get('weeks')}"
    wk = rep_after_t4["weeks"][0]
    assert "unilateral_unfollowed" in wk, \
        f"weeks[0] missing unilateral_unfollowed field: {list(wk.keys())}"
    unilateral_t4 = sorted([u["username"] for u in wk["unilateral_unfollowed"]])
    assert unilateral_t4 == ["alice_real"], \
        f"expected only alice (was 06-30 follower but never followed back), got {unilateral_t4}"
    assert wk["counts"]["unilateral_unfollowed"] == 1, \
        f"counts.unilateral_unfollowed = {wk['counts']['unilateral_unfollowed']}, expected 1"
    # ponytail: also assert the new root-level global unilateral_unfollowed
    # key, which now drives the UI. It should match the fold_pair result
    # when only two snapshots exist.
    assert "unilateral_unfollowed" in rep_after_t4, \
        f"root-level unilateral_unfollowed missing from report: {list(rep_after_t4.keys())}"
    global_unilateral_t4 = sorted([u["username"] for u in rep_after_t4["unilateral_unfollowed"]])
    assert global_unilateral_t4 == ["alice_real"], \
        f"expected global unilateral_unfollowed [alice_real], got {global_unilateral_t4}"
    print(f"T4b unilateral_unfollowed OK: weeks[-1] = {unilateral_t4} count={wk['counts']['unilateral_unfollowed']} global={global_unilateral_t4}")

    # === T4c: historical unfollowers can be flagged and restored ===
    for action in ("add", "remove"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/flag",
            data=json.dumps({"username": "alice_real", "action": action}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            assert json.loads(r.read())["ok"] is True
        flagged_report = json.loads((sb / "dist" / "report.json").read_text())
        unilateral = {u["username"] for u in flagged_report["unilateral_unfollowed"]}
        disabled = {u["username"] for u in flagged_report["snapshots"][-1]["disabled_accounts"]}
        assert ("alice_real" not in unilateral and "alice_real" in disabled) if action == "add" \
            else ("alice_real" in unilateral and "alice_real" not in disabled)
    print("T4c historical flag OK: alice moves to disabled and Unflag restores her")

    # === T4d: /recheck-disabled requires a logged-in Instagram session ===
    req = urllib.request.Request(f"http://127.0.0.1:{port}/recheck-disabled", data=b"")
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "recheck should be rejected while logged out"
    except urllib.error.HTTPError as e:
        assert e.code == 409, e.code
    print("T4d POST /recheck-disabled OK: rejected while logged out")

    # === T4e: one-click status recheck refreshes flags and the report ===
    # zelda_real is manually flagged (T3), so even a positive authenticated
    # verdict must not reenable her (rule 3) -- heur_recheck is seeded with a
    # heuristic-origin flag to prove the recovery path still fires over HTTP.
    run._write_flagged_file(run.DISABLED_FILE, run._read_flagged() | {"heur_recheck"})
    real_ig_authenticated = run.ig_authenticated
    real_authenticated_statuses = run._authenticated_statuses
    run.ig_authenticated = lambda: True
    run._authenticated_statuses = lambda usernames: {
        username: False if username == "heur_recheck" else None for username in usernames
    }
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/recheck-disabled", data=b"")
        with urllib.request.urlopen(req, timeout=5) as r:
            recheck = json.loads(r.read())
    finally:
        run.ig_authenticated = real_ig_authenticated
        run._authenticated_statuses = real_authenticated_statuses
    assert recheck["ok"] is True and recheck["reenabled"] == ["heur_recheck"], recheck
    assert run._read_flagged() == {"bob_real", "mystery_real", "zelda_real"}, run._read_flagged()
    print(f"T4e POST /recheck-disabled OK: {recheck}")

    # === T5: list snapshots via GET /snapshots ===
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/snapshots", timeout=5) as r:
        list_resp = json.loads(r.read())
    assert list_resp["ok"] is True, list_resp
    items = list_resp["snapshots"]
    # ponytail: oldest first -- _list_snapshots sorts by parse_folder_date().
    # The 'warnings' field is the parallel debug surface for non-date
    # folders under DATA -- reviewer-caught addition so GUI users get
    # the same diagnostic run_analyze prints to stderr in CLI mode.
    assert len(items) == 2, f"expected 2 snapshots, got {len(items)}: {items}"
    assert items[0]["date"] == "06-30-2026", items
    assert items[1]["date"] == "07-01-2026", items
    assert items[0]["followers"] == 3 and items[0]["following"] == 4, items[0]
    assert items[1]["followers"] == 2 and items[1]["following"] == 1, items[1]
    assert "imported_at" in items[0] and items[0]["imported_at"], items[0]
    assert "warnings" in list_resp and isinstance(list_resp["warnings"], list), list_resp
    assert list_resp["warnings"] == [], f"clean sandbox should have 0 warnings, got {list_resp['warnings']}"
    print(f"T5 GET /snapshots OK: 200 -> dates={[i['date'] for i in items]} followers={[i['followers'] for i in items]} warnings={list_resp['warnings']}")

    # === T6: delete 07-01-2026 (the most recent) via POST /delete ===
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/delete",
        data=json.dumps({"date": "07-01-2026"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        del_resp = json.loads(r.read())
    assert del_resp["ok"] is True and del_resp["deleted"] == "07-01-2026", del_resp
    assert not (sb / "data" / "07-01-2026").exists(), "folder still on disk after delete"
    rep6 = json.loads((sb / "dist" / "report.json").read_text())
    assert len(rep6["snapshots"]) == 1, f"expected 1 snapshot, got {len(rep6['snapshots'])}"
    assert rep6["snapshots"][0]["date_folder"] == "06-30-2026", rep6["snapshots"][0]
    print(f"T6 POST /delete OK: 200 {del_resp} -> 07-01-2026 gone from disk, only 06-30-2026 left")

    # === T7: delete the LAST snapshot -- expect stub fallback (no folders left) ===
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/delete",
        data=json.dumps({"date": "06-30-2026"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        del_resp = json.loads(r.read())
    assert del_resp["ok"] is True and del_resp["deleted"] == "06-30-2026", del_resp
    assert not (sb / "data" / "06-30-2026").exists()
    rep7 = json.loads((sb / "dist" / "report.json").read_text())
    # ponytail: stub-write branch fired -- report.json has empty snapshots
    # so the frontend's latestSnapshot() returns null on next load. This
    # exercises the SystemExit -> _emit_stub_report path in _handle_delete.
    assert rep7["snapshots"] == [], f"expected empty snapshots stub, got {rep7['snapshots']}"
    assert rep7["weeks"] == [], f"expected empty weeks, got {rep7['weeks']}"
    assert "generated_at" in rep7 and rep7["generated_at"], rep7
    print(f"T7 delete-last OK: 200 {del_resp} -> report.json stub-rewritten, no folders left on disk")
finally:
    httpd.shutdown()
    httpd.server_close()


# === T8: desktop profiles isolate snapshots, reports, and flags ===
def request_json(port, path, payload=None, expected=200):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            assert response.status == expected, (response.status, expected)
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        assert error.code == expected, (error.code, expected, error.read())
        return None


def read_flagged_set(profile_dir):
    """Reconciliation status=='disabled' usernames for a profile dir, read
    directly by path -- run._read_flagged() only sees the active profile."""
    path = profile_dir / "reconciliation.json"
    if not path.exists():
        return set()
    records = json.loads(path.read_text())
    return {u for u, r in records.items() if r.get("status") == "disabled"}


def import_snapshot(port, date, follower, following):
    boundary = "----ifr-profile"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"date\"\r\n\r\n{date}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"followers\"; filename=\"followers_1.json\"\r\n"
        f"Content-Type: application/json\r\n\r\n{json.dumps([ig(follower)])}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"following\"; filename=\"following.json\"\r\n"
        f"Content-Type: application/json\r\n\r\n{json.dumps([ig(following)])}\r\n--{boundary}--\r\n"
    ).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/import", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        result = json.loads(response.read())
    assert result["ok"] is True, result


desktop = sb / "desktop"
run.enable_profile_mode(desktop, desktop / "dist")
live_count_calls = []
real_profile_counts = run._authenticated_profile_counts
run._authenticated_profile_counts = lambda username: (
    live_count_calls.append(username) or {"followers": 641, "following": 522}
)
profile_httpd = run._run_server(0, run.FlagHandler)
profile_port = profile_httpd.server_address[1]
threading.Thread(target=profile_httpd.serve_forever, daemon=True).start()
try:
    initial = request_json(profile_port, "/profiles")
    assert initial["profile_mode"] is True and initial["active_profile_id"] is None, initial
    assert initial["profiles"] == [] and initial["legacy_needs_username"] is False, initial
    ig_status = request_json(profile_port, "/ig-status")
    expected_ig_state = "logged_out" if run._ig_deps_available() and run._chrome_available() else "unavailable"
    assert ig_status == {"ok": True, "state": expected_ig_state}, ig_status
    print(f"T8a GET /ig-status OK: {expected_ig_state} with no session marker yet")
    request_json(profile_port, "/snapshots", expected=409)
    for path in ("/import", "/delete", "/flag"):
        request_json(profile_port, path, {}, expected=409)

    alpha = request_json(profile_port, "/profiles", {"action": "create", "name": " @Alpha.One "})
    alpha_id = alpha["active_profile_id"]
    assert alpha["profiles"] == [{"id": alpha_id, "name": "alpha.one"}], alpha
    request_json(profile_port, "/profiles", {"action": "create", "name": ""}, expected=400)
    request_json(profile_port, "/profiles", {"action": "create", "name": "../../escape"}, expected=400)
    request_json(profile_port, "/profiles", {"action": "create", "name": "ALPHA.ONE"}, expected=400)
    request_json(profile_port, "/profiles", {"action": "select", "profile_id": "../unknown"}, expected=404)
    import_snapshot(profile_port, date_str, "alpha_follower", "alpha_following")
    live = request_json(profile_port, "/live-counts")
    assert live["ok"] is True and live["stale"] is False, live
    assert live["counts"]["followers"] == 641 and live["counts"]["following"] == 522, live
    assert isinstance(live["counts"]["as_of"], float), live
    second_live = request_json(profile_port, "/live-counts")
    assert second_live["counts"] == live["counts"] and second_live["stale"] is False, second_live
    assert live_count_calls == ["alpha.one"], live_count_calls
    request_json(profile_port, "/flag", {"username": "alpha_follower", "action": "add"})
    alpha_flagged = json.loads((desktop / "dist" / "report.json").read_text())["snapshots"][0]
    assert alpha_flagged["not_followed_back"] == []
    assert alpha_flagged["disabled_accounts"][0]["username"] == "alpha_follower"
    request_json(profile_port, "/flag", {"username": "alpha_follower", "action": "remove"})
    alpha_unflagged = json.loads((desktop / "dist" / "report.json").read_text())["snapshots"][0]
    assert alpha_unflagged["not_followed_back"][0]["username"] == "alpha_follower"
    assert alpha_unflagged["dont_follow_me_back"][0]["username"] == "alpha_following"
    request_json(profile_port, "/flag", {"username": "alpha_disabled", "action": "add"})

    beta = request_json(profile_port, "/profiles", {"action": "create", "name": "beta_two"})
    beta_id = beta["active_profile_id"]
    import_snapshot(profile_port, date_str, "beta_follower", "beta_following")
    request_json(profile_port, "/flag", {"username": "beta_disabled", "action": "add"})

    request_json(profile_port, "/profiles", {"action": "select", "profile_id": alpha_id})
    alpha_report = json.loads((desktop / "dist" / "report.json").read_text())
    assert alpha_report["snapshots"][0]["not_followed_back"][0]["username"] == "alpha_follower"
    assert read_flagged_set(desktop / "profiles" / alpha_id) == {"alpha_disabled"}
    request_json(profile_port, "/flag", {"username": "alpha_disabled", "action": "remove"})
    assert read_flagged_set(desktop / "profiles" / alpha_id) == set()
    assert request_json(profile_port, "/snapshots")["snapshots"][0]["date"] == date_str
    request_json(profile_port, "/delete", {"date": date_str})
    assert json.loads((desktop / "dist" / "report.json").read_text())["snapshots"] == []

    request_json(profile_port, "/profiles", {"action": "select", "profile_id": beta_id})
    beta_report = json.loads((desktop / "dist" / "report.json").read_text())
    assert beta_report["snapshots"][0]["not_followed_back"][0]["username"] == "beta_follower"
    assert read_flagged_set(desktop / "profiles" / beta_id) == {"beta_disabled"}
    assert (desktop / "profiles" / beta_id / "data" / date_str).is_dir()

    # Weekly saved-flag recheck: one live relationship-list fetch, background
    # completion, per-profile schedule, and no removal for absent/unclear names.
    session_dir = desktop / "ig-session"
    session_dir.mkdir(exist_ok=True)
    (session_dir / run.IG_SESSION_MARKER).touch()
    real_relationships = run._authenticated_relationships
    relationship_calls = []
    try:
        run._authenticated_relationships = lambda username: (
            relationship_calls.append(username) or
            {"followers": {}, "following": {"beta_disabled": "9001"}}
        )
        started = request_json(profile_port, "/recheck-disabled", {})
        assert started["state"] == "running" and started["checked"] == 1, started
        for _ in range(100):
            recheck = request_json(profile_port, "/recheck-status")
            if recheck["state"] != "running":
                break
            time.sleep(0.01)
        # beta_disabled was flagged manually (/flag), and direct relationship-
        # list membership is heuristic-strength evidence, not manual or a
        # proven rename -- so rule 3 keeps it flagged even though it's live.
        # Only the user's own unflag, or a proven id_confirmed rename, moves
        # a manual flag; T8b below (beta_old -> beta_new) covers that path.
        assert recheck["state"] == "done" and recheck["reenabled"] == [], recheck
        beta_flags = desktop / "profiles" / beta_id / "disabledaccounts.json"
        schedule = beta_flags.with_name("disabled-recheck.json")
        assert read_flagged_set(beta_flags.parent) == {"beta_disabled"}
        schedule_data = json.loads(schedule.read_text())
        assert schedule_data["next_due"] > time.time() + 6 * 24 * 60 * 60, schedule_data

        request_json(profile_port, "/flag", {"username": "beta_waiting", "action": "add"})
        not_due = request_json(profile_port, "/live-counts")
        assert ("recheck" not in not_due
                and read_flagged_set(beta_flags.parent) == {"beta_disabled", "beta_waiting"}), not_due

        schedule.unlink()  # force the automatic path due inside this disposable sandbox
        run._authenticated_relationships = lambda username: (
            relationship_calls.append(username) or {"followers": {}, "following": {}}
        )
        auto = request_json(profile_port, "/live-counts")
        assert auto["recheck"]["state"] == "running" and auto["recheck"]["automatic"] is True, auto
        for _ in range(100):
            recheck = request_json(profile_port, "/recheck-status")
            if recheck["state"] != "running":
                break
            time.sleep(0.01)
        assert recheck["state"] == "done" and recheck["reenabled"] == [], recheck
        assert read_flagged_set(beta_flags.parent) == {"beta_disabled", "beta_waiting"}
        assert relationship_calls == ["beta_two", "beta_two"], relationship_calls

        # A flagged account that merely CHANGED ITS USERNAME must come back.
        # Its old handle is absent from every live list forever, so without the
        # identity ledger it stays buried in the disabled panel under a name
        # nobody uses -- flagged for a disappearance that never happened.
        request_json(profile_port, "/flag", {"username": "beta_old", "action": "add"})
        run.record_live_identities({"followers": {"beta_old": "4242"}, "following": {}})
        run._authenticated_relationships = lambda username: {
            "followers": {"beta_new": "4242"}, "following": {},
        }
        renamed_run = request_json(profile_port, "/recheck-disabled", {})
        assert renamed_run["state"] == "running", renamed_run
        for _ in range(100):
            recheck = request_json(profile_port, "/recheck-status")
            if recheck["state"] != "running":
                break
            time.sleep(0.01)
        assert recheck["state"] == "done" and recheck["reenabled"] == ["beta_old"], recheck
        assert read_flagged_set(beta_flags.parent) == {"beta_disabled", "beta_waiting"}
        assert run.known_renames() == {"beta_old": "beta_new"}, run.known_renames()
    finally:
        run._authenticated_relationships = real_relationships
    print("T8b periodic flag recheck OK: live relationships restore; renamed handles unflag; absent handles stay flagged; weekly schedule skips")

    run.enable_profile_mode(desktop, desktop / "dist")
    persisted = request_json(profile_port, "/profiles")
    assert persisted["active_profile_id"] == beta_id, persisted
    print("T8 desktop profiles OK: validation, 409 guard, import/delete/report/flag isolation, persistence")

    # === T8c: a long-held _IG_HELPER_LOCK (a live scan/recheck job) must not
    # freeze other requests -- that's the whole point of ThreadingHTTPServer.
    release_lock = threading.Event()

    def hold_lock():
        with run._IG_HELPER_LOCK:
            release_lock.wait(2)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    time.sleep(0.2)  # let the holder actually acquire before racing it
    start = time.time()
    status = request_json(profile_port, "/ig-status")
    elapsed = time.time() - start
    release_lock.set()
    holder.join(timeout=3)
    assert status["ok"] is True, status
    assert elapsed < 1.0, f"/ig-status took {elapsed:.2f}s behind a held _IG_HELPER_LOCK -- server froze"
    print(f"T8c concurrency OK: /ig-status answered in {elapsed:.2f}s while _IG_HELPER_LOCK was held by another thread")
finally:
    profile_httpd.shutdown()
    profile_httpd.server_close()
    run._authenticated_profile_counts = real_profile_counts


# === T9: legacy migration copies safely, prefers root flags, and is idempotent ===
def seed_legacy(root, nested_flags=False):
    folder = root / "data" / date_str
    folder.mkdir(parents=True)
    (folder / "followers_1.json").write_text(json.dumps([ig("legacy_follower")]))
    (folder / "following.json").write_text(json.dumps([ig("legacy_following")]))
    flag_file = root / "data" / "disabledaccounts.json" if nested_flags else root / "disabledaccounts.json"
    flag_file.write_text(json.dumps(["legacy_disabled"]))
    return flag_file


legacy = sb / "legacy"
legacy_flag = seed_legacy(legacy)
(legacy / "data" / "disabledaccounts.json").write_text(json.dumps(["nested_must_lose"]))
run.enable_profile_mode(legacy, legacy / "dist")
assert run._profiles_payload()["legacy_needs_username"] is True
migrated = run._create_profile("Legacy.Owner", migrate_legacy=True)
migrated_id = migrated["active_profile_id"]
assert (legacy / "data" / date_str).is_dir() and legacy_flag.is_file(), "legacy rollback files changed"
assert json.loads((legacy / "profiles" / migrated_id / "disabledaccounts.json").read_text()) == ["legacy_disabled"]
run.enable_profile_mode(legacy, legacy / "dist")
assert run._profiles_payload()["active_profile_id"] == migrated_id
try:
    run._create_profile("again", migrate_legacy=True)
    raise AssertionError("completed migration reran")
except ValueError:
    pass

nested = sb / "nested-legacy"
seed_legacy(nested, nested_flags=True)
run.enable_profile_mode(nested, nested / "dist")
nested_result = run._create_profile("nested_owner", migrate_legacy=True)
nested_id = nested_result["active_profile_id"]
assert json.loads((nested / "profiles" / nested_id / "disabledaccounts.json").read_text()) == ["legacy_disabled"]

failed = sb / "failed-legacy"
seed_legacy(failed)
run.enable_profile_mode(failed, failed / "dist")
real_copytree = run.shutil.copytree
run.shutil.copytree = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic copy failure"))
try:
    try:
        run._create_profile("failure_owner", migrate_legacy=True)
        raise AssertionError("migration failure was swallowed")
    except OSError:
        pass
finally:
    run.shutil.copytree = real_copytree
failed_manifest = json.loads((failed / "profiles.json").read_text())
assert failed_manifest["profiles"] == [] and failed_manifest["migration_complete"] is False
assert list((failed / "profiles").iterdir()) == [] and (failed / "data" / date_str).is_dir()
print("T9 legacy migration OK: root/nested flags, originals retained, cleanup, idempotence")

# === T10: source-mode startup enables the same profile backend as desktop ===
source = sb / "source-mode"
(source / "data" / date_str).mkdir(parents=True)
(source / "data" / date_str / "followers_1.json").write_text(json.dumps([ig("source_follower")]))
(source / "data" / date_str / "following.json").write_text(json.dumps([ig("source_following")]))
run.PROFILE_ROOT = run.PROFILE_MANIFEST = run.PROFILE_OUT_DIR = None
run.ROOT = source
run._set_data_paths(data_dir=source / "data", out_dir=source / "dist",
                    disabled_file=source / "data" / "disabledaccounts.json")
real_parse_args, real_serve = run.parse_args, run.serve
run.parse_args = lambda: argparse.Namespace(subcommand=None, check_disabled=False, port=0)
run.serve = lambda port: None
try:
    run.main()
finally:
    run.parse_args, run.serve = real_parse_args, real_serve
payload = run._profiles_payload()
assert payload["profile_mode"] is True and payload["legacy_needs_username"] is True
print("T10 source profiles OK: web startup exposes legacy migration/account controls")

# --- T11: export-folder noise must never reach the headline counts -------
# Regression for "the app says I follow more people than Instagram does".
# Instagram ships following_hashtags.json in the SAME folder as following.json;
# a plain `"following" in filename` router counted every followed hashtag as a
# person, so the following card overshot instagram.com by exactly the number of
# hashtags followed and no amount of re-importing could fix it.
def profile_rec(name, ts=1700000000):
    return {"title": "", "string_list_data": [
        {"href": f"https://www.instagram.com/{name}", "value": name, "timestamp": ts}]}


def hashtag_rec(tag, ts=1700000000):
    return {"title": tag, "string_list_data": [
        {"href": f"https://www.instagram.com/explore/tags/{tag}", "value": tag, "timestamp": ts}]}


noise = sb / "audit-export" / "01-02-2026"
noise.mkdir(parents=True)
(noise / "followers_1.json").write_text(json.dumps(
    [profile_rec(f"f{i}") for i in range(5)]))
(noise / "following.json").write_text(json.dumps(
    {"relationships_following": [profile_rec(f"f{i}") for i in range(3)]}))
(noise / "following_hashtags.json").write_text(json.dumps(
    {"relationships_following_hashtags": [hashtag_rec(t) for t in ("jdm", "cars")]}))
(noise / "pending_follow_requests.json").write_text(json.dumps(
    [profile_rec("pending_person")]))
(noise / "recently_unfollowed_profiles.json").write_text(json.dumps([profile_rec("dropped")]))
(noise / "close_friends.json").write_text(json.dumps([profile_rec("bestie")]))
(noise / "blocked_profiles.json").write_text(json.dumps([profile_rec("blocked_person")]))

summary, _, following_recs = run.read_snapshot(noise, set())
assert summary["counts"]["followers"] == 5, summary["counts"]
assert summary["counts"]["following"] == 3, summary["counts"]
assert {r["username"] for r in following_recs} == {"f0", "f1", "f2"}, following_recs
ignored = {f["name"] for f in summary["source_files"] if f["bucket"] is None}
assert "following_hashtags.json" in ignored and "close_friends.json" in ignored, ignored
# The href guard is the second line of defense: even hand-renamed into the
# following bucket, a hashtag list must parse to zero people.
assert run.load_json_records(noise / "following_hashtags.json") == []
# ...and the staging router must agree with the snapshot router, or an import
# would quietly re-introduce the bug it just fixed.
n_f, n_g = run._stage_snapshot(noise, "01-03-2026")
assert (n_f, n_g) == (5, 3), (n_f, n_g)
# The saved-snapshots inventory must route and dedupe identically, or it
# contradicts the stat cards sitting directly above it on the page.
listed = {item["date"]: item for item in run._list_snapshots()[0]}
assert listed["01-03-2026"]["followers"] == 5, listed["01-03-2026"]
assert listed["01-03-2026"]["following"] == 3, listed["01-03-2026"]
print("T11 export noise OK: hashtags/pending/close-friends files never reach the counts")

# --- T12: audit reconciles the arithmetic behind the headline numbers ----
run.LIVE_PROFILE_COUNTS.clear()
audit = run.audit_counts()
assert audit["date_folder"] == "01-03-2026", audit["date_folder"]
assert audit["following"]["unique"] == 3 and audit["following"]["active"] == 3, audit["following"]
assert audit["following"]["live"] is None and audit["following"]["gap"] is None, audit["following"]
assert audit["scan"] is None, audit["scan"]

run.add_flagged("f2")
audit = run.audit_counts()
assert audit["following"]["flagged"] == 1 and audit["following"]["active"] == 2, audit["following"]
assert audit["followers"]["flagged"] == 1 and audit["followers"]["active"] == 4, audit["followers"]

# A warmed live-count cache is what turns the ledger into a verdict. The audit
# must read that cache rather than spawning the browser subprocess itself --
# the HTTP server is single-threaded, so a cold fetch there would stall the page.
real_cached = run.cached_live_profile_counts
run.cached_live_profile_counts = lambda: {"followers": 4, "following": 1}
try:
    audit = run.audit_counts()
finally:
    run.cached_live_profile_counts = real_cached
assert audit["live_available"] is True
assert audit["following"]["gap"] == 1, audit["following"]   # 2 active vs 1 live
assert audit["followers"]["gap"] == 0, audit["followers"]   # 4 active vs 4 live
# Without a warmed cache the audit reports "unknown" instead of blocking.
assert run.audit_counts()["live_available"] is False
run.unflag("f2")

# One authenticated relationship-list comparison names the gap directly. It
# only returns candidates; the user's saved flag file remains untouched.
relationship_audit = run.audit_counts(
    scope="following",
    relationships={"followers": {}, "following": {"f2": "102"}},
)
relationship_scan = relationship_audit["scan"]
assert relationship_scan["method"] == "relationships", relationship_scan
assert sorted(r["username"] for r in relationship_scan["following_candidates"]) == ["f0", "f1"], relationship_scan
assert relationship_scan["live_profiles"] == 1 and run._read_flagged() == set(), relationship_scan

# The network sweep itself, with instagram.com stubbed out: it must name the
# handles whose page is gone, leave "unclear" ones alone, and only flag when
# explicitly asked to.
real_looks_disabled = run.looks_disabled
run.looks_disabled = lambda u, timeout=8.0: {"f0": True, "f1": None}.get(u, False)
try:
    audit = run.audit_counts(check_network=True, scope="following")
    scan = audit["scan"]
    assert scan["scope"] == "following" and scan["checked"] == 3, scan
    assert [r["username"] for r in scan["gone"]] == ["f0"], scan["gone"]
    # "Instagram wouldn't answer" is reported as its own bucket, never folded
    # into "gone" -- that distinction is the whole point of the scan.
    assert [r["username"] for r in scan["unverified"]] == ["f1"], scan["unverified"]
    assert scan["live_profiles"] == 1, scan["live_profiles"]
    assert run._read_flagged() == set(), "audit must not flag anything on its own"

    # Scope narrows the sweep; "both" also covers followers-only handles.
    assert run.audit_counts(check_network=True, scope="both")["scan"]["checked"] == 5

    # With a saved login the authenticated pass runs FIRST and clears whatever
    # it can see, so only what it could not resolve reaches the anonymous
    # probe. Instagram login-walls anonymous views, so leading with the probe
    # is what makes a scan come back empty-handed.
    probed = []
    run.looks_disabled = lambda u, timeout=8.0: (
        probed.append(u), {"f0": True}.get(u))[1]   # f1 -> None: walled/unclear
    real_auth, real_raw = run.ig_authenticated, run._authenticated_raw_statuses
    run.ig_authenticated = lambda: True
    run._authenticated_raw_statuses = lambda users, progress=None: {
        u: ("unverified" if u in ("f0", "f1") else "active") for u in users}
    try:
        scan = run.audit_counts(check_network=True, scope="following")["scan"]
        assert sorted(probed) == ["f0", "f1"], probed
        assert [r["username"] for r in scan["gone"]] == ["f0"], scan["gone"]
        assert [r["username"] for r in scan["unverified"]] == ["f1"], scan["unverified"]
        assert scan["live_profiles"] == 1 and scan["authenticated"] is True, scan
    finally:
        run.ig_authenticated, run._authenticated_raw_statuses = real_auth, real_raw

    # A dead authenticated session must not turn the whole list into "gone".
    run._authenticated_raw_statuses = lambda users, progress=None: None
    run.ig_authenticated = lambda: True
    run.looks_disabled = lambda u, timeout=8.0: None
    try:
        scan = run.audit_counts(check_network=True, scope="following")["scan"]
        assert scan["gone"] == [] and len(scan["unverified"]) == 3, scan
    finally:
        run.ig_authenticated, run._authenticated_raw_statuses = real_auth, real_raw
finally:
    run.looks_disabled = real_looks_disabled
print("T12 count audit OK: records -> unique -> after-flags -> live gap -> gone accounts")

# --- T13 identity ledger: renames proved by Instagram user id ---------------
# The export carries usernames and nothing else, so a rename is invisible to
# it. Instagram's live list carries the numeric id, which never changes.
run._atomic_json(run._identity_path(), {"ids": {}})
run._write_reconciliation({})
first = run.record_live_identities({
    "followers": {"stays": "1", "old_handle": "2"},
    "following": {"partner": "3"},
})
assert first == {}, first                       # first sighting proves nothing
assert run.known_renames() == {}, run.known_renames()

second = run.record_live_identities({
    "followers": {"stays": "1", "new_handle": "2"},
    "following": {"partner": "3"},
})
assert second == {"old_handle": "new_handle"}, second
assert run.known_renames() == {"old_handle": "new_handle"}, run.known_renames()

# Rename again: the chain resolves all the way to the handle in use today.
run.record_live_identities({"followers": {"stays": "1", "newest": "2"}, "following": {}})
assert run.known_renames() == {"old_handle": "newest", "new_handle": "newest"}, run.known_renames()

# A handle that is live again is somebody's current handle, so the rename
# recorded away from it is stale -- otherwise it would silently rewrite an
# unrelated person's row forever.
run.record_live_identities({"followers": {"new_handle": "77"}, "following": {}})
assert "new_handle" not in run.known_renames(), run.known_renames()
run._identity_path().unlink()
print("T13 identity ledger OK: id-proved renames, chains resolved, stale entries dropped")

# --- T14 reconciliation reports BOTH directions -----------------------------
# "in the export but not on Instagram" is only half the gap. The other half --
# accounts Instagram lists that the export has no row for -- is where a
# rename's new handle and every post-export follow lands, and without it the
# arithmetic can never close.
run._atomic_json(run._identity_path(), {"ids": {}})
run._write_reconciliation({})
run.record_live_identities({"followers": {}, "following": {"f1": "501"}})
run.record_live_identities({"followers": {}, "following": {"f1_renamed": "501"}})
two_way = run.audit_counts(scope="following", relationships={
    "followers": {},
    "following": {"f1_renamed": "501", "someone_new": "999"},
})["scan"]
# f1 renamed to f1_renamed: proved by id, so NOT a missing account.
assert two_way["renamed"] == {"f1": "f1_renamed"}, two_way["renamed"]
assert [r["username"] for r in two_way["following_renamed"]] == ["f1"], two_way
assert two_way["following_renamed"][0]["renamed_to"] == "f1_renamed", two_way
# f0/f2 really are absent from Instagram's list.
assert sorted(r["username"] for r in two_way["following_candidates"]) == ["f0", "f2"], two_way
# someone_new is on Instagram but not in the export -- a stale export.
assert two_way["following_extra"] == ["someone_new"], two_way
assert two_way["following_listed"] == 2, two_way
run._identity_path().unlink()
print("T14 two-way reconciliation OK: renamed / missing / not-in-export kept apart")

# --- T14b a live account is never offered as a "disabled" candidate ---------
# Missing from Instagram's relationship list has two causes with opposite
# answers: the ACCOUNT is gone (flag it), or the RELATIONSHIP is gone -- you
# unfollowed them after the export was generated -- and the account is
# perfectly alive (flagging it is simply wrong). One authenticated profile
# fetch separates them; without the split, live accounts get filed into the
# disabled panel and stay there.
run._atomic_json(run._identity_path(), {"ids": {}})
run._write_reconciliation({})
real_auth, real_raw = run.ig_authenticated, run._authenticated_raw_statuses
run.ig_authenticated = lambda: True
# f0 still has a live profile (you just unfollowed them); f2's profile is gone.
run._authenticated_raw_statuses = lambda users, progress=None: {
    u: ("active" if u == "f0" else "unverified") for u in users}
try:
    split = run.audit_counts(scope="following", verify_missing=True, relationships={
        "followers": {}, "following": {"f1": "501"},
    })["scan"]
finally:
    run.ig_authenticated, run._authenticated_raw_statuses = real_auth, real_raw
assert split["verified"] is True, split
assert [r["username"] for r in split["departed"]] == ["f0"], split["departed"]
assert [r["username"] for r in split["following_departed"]] == ["f0"], split
assert [r["username"] for r in split["candidates"]] == ["f2"], split["candidates"]
assert [r["username"] for r in split["following_candidates"]] == ["f2"], split
assert split["verify_truncated"] is False, split

# With no way to verify, nothing is quietly promoted to "gone" -- every
# missing handle stays a candidate and the payload says it was unverified,
# so the UI can warn instead of pretending certainty.
unverified_split = run.audit_counts(scope="following", verify_missing=True, relationships={
    "followers": {}, "following": {"f1": "501"},
})["scan"]
assert unverified_split["verified"] is False, unverified_split
assert sorted(r["username"] for r in unverified_split["candidates"]) == ["f0", "f2"], unverified_split
assert unverified_split["departed"] == [], unverified_split
run._identity_path().unlink()
print("T14b live-account guard OK: alive -> 'no longer connected', gone -> flag candidate")

# --- T14c the relationship walk is cached, and publishes its counts ---------
# One walk is a Chrome launch plus a request per ~23 accounts. Clicking the
# button twice used to pay for it twice to get an answer that cannot have
# changed. The profile totals ride along on the same fetch, so the stat cards
# no longer need a Chrome launch of their own.
run.invalidate_relationships_cache()
run.LIVE_PROFILE_COUNTS.clear()
calls = []
real_scrapling, real_auth = run._scrapling_result, run.ig_authenticated
run.ig_authenticated = lambda: True
run._scrapling_result = lambda mode, args, timeout: (
    calls.append(mode) or {"followers": {"a": "1"}, "following": {"b": "2"},
                           "self_pk": "9", "counts": {"followers": 5, "following": 7}})
try:
    first = run._authenticated_relationships("me")
    second = run._authenticated_relationships("me")
    assert first == second == {"followers": {"a": "1"}, "following": {"b": "2"}}, first
    assert calls == ["relationships"], f"second call must come from cache, got {calls}"
    # Switching accounts must drop it -- a cached list belongs to one account.
    run.invalidate_relationships_cache()
    run._authenticated_relationships("me")
    assert calls == ["relationships", "relationships"], calls
finally:
    run._scrapling_result, run.ig_authenticated = real_scrapling, real_auth
    run.invalidate_relationships_cache()
print("T14c relationship cache OK: repeat walk served from cache, counts published free")

# --- T15 multi-part exports are staged whole --------------------------------
# Instagram splits a big export across followers_1.json, followers_2.json, ...
# Staging one file per bucket silently dropped everyone past the first part,
# which is the single largest source of "this doesn't match Instagram".
multi = sb / "multipart-export"
multi.mkdir()
(multi / "followers_1.json").write_text(json.dumps(
    {"relationships_followers": [ig("part1_a"), ig("part1_b")]}))
(multi / "followers_2.json").write_text(json.dumps(
    {"relationships_followers": [ig("part2_a"), ig("part1_b")]}))  # overlap is deduped
(multi / "following.json").write_text(json.dumps(
    {"relationships_following": [ig("part1_a")]}))
(multi / "following_hashtags.json").write_text(json.dumps(
    {"relationships_following_hashtags": [hashtag_rec("cars")]}))
n_f, n_g = run._stage_snapshot(multi, "05-05-2026")
assert (n_f, n_g) == (3, 1), (n_f, n_g)
staged = sorted(pp.name for pp in (run.DATA / "05-05-2026").glob("*.json"))
assert staged == ["followers_1.json", "followers_2.json", "following_1.json"], staged
summary, _, _ = run.read_snapshot(run.DATA / "05-05-2026", set())
assert summary["counts"]["followers"] == 3, summary["counts"]

# Re-importing a smaller export must not strand the old parts behind, or the
# report keeps counting people the new export dropped.
shrunk = sb / "shrunk-export"
shrunk.mkdir()
(shrunk / "followers_1.json").write_text(json.dumps(
    {"relationships_followers": [ig("part1_a")]}))
(shrunk / "following.json").write_text(json.dumps(
    {"relationships_following": [ig("part1_a")]}))
assert run._stage_snapshot(shrunk, "05-05-2026") == (1, 1)
staged = sorted(pp.name for pp in (run.DATA / "05-05-2026").glob("*.json"))
assert staged == ["followers_1.json", "following_1.json"], staged
shutil.rmtree(run.DATA / "05-05-2026")
print("T15 multi-part export OK: every part staged, deduped, stale parts cleared")

# --- T16 the anonymous probe must not read a login wall as a live profile ---
wall = ('<html><head><meta property="og:description" content="See photos">'
        '</head><body><form id="loginForm"><input name="username">'
        '</form>Log in to Instagram</body></html>')


class _FakePage:
    def __init__(self, body):
        self.body = body.encode()

    def read(self, _n=None):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


real_urlopen = run.urllib.request.urlopen
try:
    run.urllib.request.urlopen = lambda *a, **k: _FakePage(wall)
    assert run.looks_disabled("walled") is None, "login wall must read as unverified"
    run.urllib.request.urlopen = lambda *a, **k: _FakePage(
        '<html><head><meta property="og:description" content="1 posts">'
        '</head><body>photos</body></html>')
    assert run.looks_disabled("real") is False
    run.urllib.request.urlopen = lambda *a, **k: _FakePage(
        "<html><body>Sorry, this page isn't available.</body></html>")
    assert run.looks_disabled("gone") is True
finally:
    run.urllib.request.urlopen = real_urlopen
print("T16 anonymous probe OK: login wall is unverified, not 'active'")

# --- T17 reconciliation status: the transition rules and one fixture per status ---
# ADR 0001: resolve_reconciliation_status() is pure and covers every rule
# directly, independent of any storage or HTTP plumbing.
_t = "2026-01-01T00:00:00"
active_rec = {"status": "active", "origin": "heuristic", "updated_at": _t}
manual_disabled = {"status": "disabled", "origin": "manual", "updated_at": _t}
heuristic_disabled = {"status": "disabled", "origin": "heuristic", "updated_at": _t}
id_rename = {"status": "renamed", "origin": "id_confirmed", "renamed_to": "new", "updated_at": _t}
ts_rename = {"status": "renamed", "origin": "timestamp_heuristic", "renamed_to": "new", "updated_at": _t}
resolve = run.resolve_reconciliation_status
# Rule 1/2: an id_confirmed rename always wins, even over a manual flag.
assert resolve(manual_disabled, id_rename) == id_rename, "id_confirmed rename must override manual disabled"
# Rule 2: once renamed, only a fresh id_confirmed rename can change it --
# a weaker timestamp_heuristic rename attempt is rejected.
assert resolve(id_rename, ts_rename) == id_rename, "a proven rename must not be downgraded by a weaker one"
# Rule 3: a heuristic verdict never overrides a manual origin.
assert resolve(manual_disabled, heuristic_disabled) == manual_disabled, "heuristic must not re-stamp a manual flag"
assert resolve(manual_disabled, active_rec) == manual_disabled, "heuristic active must not clear a manual flag"
assert resolve(manual_disabled, ts_rename) == manual_disabled, "timestamp_heuristic must not override manual (only id_confirmed does, rule 2)"
# Heuristic overrides heuristic, and a first-ever record is accepted as-is.
assert resolve(heuristic_disabled, active_rec) == active_rec, "heuristic must be able to override an earlier heuristic"
assert resolve(None, manual_disabled) == manual_disabled, "first evidence for a handle must be accepted"
print("T17a resolve_reconciliation_status OK: id_confirmed always wins, renamed sticks, heuristic never overrides manual")

# One fixture handle per status, exercised through the real, persisted paths
# this app actually uses -- not resolve_reconciliation_status called by hand.
recon_dir = sb / "recon-fixtures"
run._set_data_paths(data_dir=recon_dir / "data", out_dir=recon_dir / "dist",
                    disabled_file=recon_dir / "disabledaccounts.json")
run.add_flagged("h_disabled")                                    # disabled/manual
run._persist_scan_verdicts({"h_active": run.ACTIVE})             # active/heuristic
run._persist_scan_verdicts({"h_unverified": run.UNVERIFIED})     # unverified/heuristic
run.record_live_identities({"followers": {"h_renamed_old": "9101"}, "following": {}})
run.record_live_identities({"followers": {"h_renamed_new": "9101"}, "following": {}})  # renamed/id_confirmed
records = run._read_reconciliation()
assert records["h_disabled"]["status"] == "disabled" and records["h_disabled"]["origin"] == "manual", records
assert records["h_active"]["status"] == "active", records["h_active"]
assert records["h_unverified"]["status"] == "unverified", records["h_unverified"]
assert records["h_renamed_old"]["status"] == "renamed" and records["h_renamed_old"]["renamed_to"] == "h_renamed_new", records["h_renamed_old"]
assert run.known_renames() == {"h_renamed_old": "h_renamed_new"}, run.known_renames()
# unchecked has no fixture record at all -- "we haven't looked" is the
# absence of an entry, not a stored status (see the ADR discussion of scope).
assert "h_unchecked" not in records, records
print("T17b fixture per status OK: active, renamed, disabled, unverified all persisted with the right origin")

# The regression the ADR names by commit: a renamed handle must never be
# flagged disabled, even if the flag comes in after the rename is known
# (record_live_identities already enforces rule 1 at write time).
run.record_live_identities({"followers": {"h_bug_old": "9202"}, "following": {}})
run.record_live_identities({"followers": {"h_bug_new": "9202"}, "following": {}})
run.add_flagged("h_bug_old")
assert run._read_reconciliation()["h_bug_old"]["status"] == "renamed", \
    "a manual flag on a handle already proven renamed must not create a disabled status"
assert "h_bug_old" not in run._read_flagged(), run._read_flagged()
print("T17c renamed-beats-disabled OK: flagging an already-proven-renamed handle cannot mark it disabled")

# "unchecked" at the audit level: a relationships scan that identifies more
# missing candidates than MISSING_VERIFY_LIMIT allows leaves the overflow
# unchecked rather than silently calling it a candidate or departed.
(recon_dir / "data" / date_str).mkdir(parents=True)
(recon_dir / "data" / date_str / "following.json").write_text(
    json.dumps([ig("un_a"), ig("un_b")]))
real_limit = run.MISSING_VERIFY_LIMIT
real_auth, real_raw = run.ig_authenticated, run._authenticated_raw_statuses
run.MISSING_VERIFY_LIMIT = 1
run.ig_authenticated = lambda: True
run._authenticated_raw_statuses = lambda users, progress=None: {u: "unverified" for u in users}
try:
    unchecked_audit = run.audit_counts(scope="following", verify_missing=True, relationships={
        "followers": {}, "following": {},
    })
finally:
    run.MISSING_VERIFY_LIMIT = real_limit
    run.ig_authenticated, run._authenticated_raw_statuses = real_auth, real_raw
assert unchecked_audit["scan"]["verify_truncated"] is True, unchecked_audit["scan"]
assert unchecked_audit["following"]["unchecked"] == 1, unchecked_audit["following"]
print("T17d unchecked scope OK: only the MISSING_VERIFY_LIMIT overflow counts as unchecked, not every unprobed handle")

# --- T18 _STATE_LOCK: concurrent writers must not lose each other's updates ---
# Without a lock spanning the whole read-modify-write, two threads racing
# add_flagged() can each read the same pre-write dict and the second write
# silently clobbers the first's addition. 20 usernames makes that race easy
# to hit if the lock were missing or too narrow.
run._set_data_paths(data_dir=sb / "lock-test" / "data", out_dir=sb / "lock-test" / "dist",
                    disabled_file=sb / "lock-test" / "disabledaccounts.json")
concurrent_names = [f"concurrent_{i}" for i in range(20)]
concurrent_threads = [threading.Thread(target=run.add_flagged, args=(n,)) for n in concurrent_names]
for th in concurrent_threads:
    th.start()
for th in concurrent_threads:
    th.join()
assert run._read_flagged() == set(concurrent_names), run._read_flagged()
print("T18 _STATE_LOCK OK: 20 concurrent add_flagged calls all persisted, none lost")

shutil.rmtree(sb, ignore_errors=True)
print("SMOKE_TEST_OK")
