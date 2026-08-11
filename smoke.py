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
flag_list = json.loads((sb / "disabledaccounts.json").read_text())
assert {"bob_real", "zelda_real"}.issubset(flag_list), flag_list
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
profile_httpd = run._run_server(0, run.FlagHandler)
profile_port = profile_httpd.server_address[1]
threading.Thread(target=profile_httpd.serve_forever, daemon=True).start()
try:
    initial = request_json(profile_port, "/profiles")
    assert initial["profile_mode"] is True and initial["active_profile_id"] is None, initial
    assert initial["profiles"] == [] and initial["legacy_needs_username"] is False, initial
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
    assert json.loads((desktop / "profiles" / alpha_id / "disabledaccounts.json").read_text()) == ["alpha_disabled"]
    request_json(profile_port, "/flag", {"username": "alpha_disabled", "action": "remove"})
    assert json.loads((desktop / "profiles" / alpha_id / "disabledaccounts.json").read_text()) == []
    assert request_json(profile_port, "/snapshots")["snapshots"][0]["date"] == date_str
    request_json(profile_port, "/delete", {"date": date_str})
    assert json.loads((desktop / "dist" / "report.json").read_text())["snapshots"] == []

    request_json(profile_port, "/profiles", {"action": "select", "profile_id": beta_id})
    beta_report = json.loads((desktop / "dist" / "report.json").read_text())
    assert beta_report["snapshots"][0]["not_followed_back"][0]["username"] == "beta_follower"
    assert json.loads((desktop / "profiles" / beta_id / "disabledaccounts.json").read_text()) == ["beta_disabled"]
    assert (desktop / "profiles" / beta_id / "data" / date_str).is_dir()
    run.enable_profile_mode(desktop, desktop / "dist")
    persisted = request_json(profile_port, "/profiles")
    assert persisted["active_profile_id"] == beta_id, persisted
    print("T8 desktop profiles OK: validation, 409 guard, import/delete/report/flag isolation, persistence")
finally:
    profile_httpd.shutdown()
    profile_httpd.server_close()


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

shutil.rmtree(sb, ignore_errors=True)
print("SMOKE_TEST_OK")
