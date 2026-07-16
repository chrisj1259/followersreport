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
flag_list = json.loads((sb / "disabledaccounts.json").read_text())
assert "zelda_real" in flag_list, flag_list
(sb / "dist" / "report.json").unlink()
run.run_analyze(argparse.Namespace(check_disabled=False))
rep3 = json.loads((sb / "dist" / "report.json").read_text())
dfmb3 = sorted(u["username"] for u in rep3["snapshots"][0]["dont_follow_me_back"])
disabled3 = sorted(u["username"] for u in rep3["snapshots"][0]["disabled_accounts"])
assert "zelda_real" not in dfmb3, dfmb3
assert "zelda_real" in disabled3, disabled3
print(f"T3 flag pipeline OK: zelda moved DFMB->disabled: dfmb={dfmb3} disabled={disabled3}")

# === T4: end-to-end HTTP /import through FlagHandler ===
httpd = run._run_server(0, run.FlagHandler)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.3)
try:
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

shutil.rmtree(sb, ignore_errors=True)
print("SMOKE_TEST_OK")
