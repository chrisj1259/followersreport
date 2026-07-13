"""End-to-end verification for the POST /import endpoint.

Builds a synthetic export for an extra snapshot, starts the server, POSTs
multipart to /import, asserts dist/report.json reflects the new snapshot +
weeks, then cleans up. Designed to run from project root with python:
`python scripts/verify_import.py`

Failure paths:
- Prints traceback on uncaught Exception
- Exit code 0 on success, 1 on assertion / HTTP failure
"""
import argparse
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))
import run  # noqa: E402

PORT = 8765
SNAPSHOT_DATE = "07-15-2026"
TEST_DIR = Path("/tmp/verify_import_upload")


def make_fixture():
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)
    TEST_DIR.mkdir(parents=True)

    followers = {
        "relationships_followers": [
            {
                "title": f"u{i:03d}",
                "string_list_data": [
                    {
                        "value": f"u{i:03d}",
                        "href": f"https://instagram.com/u{i:03d}",
                        "timestamp": 1783605797,
                    }
                ],
            }
            for i in range(550)
        ]
    }
    following = {
        "relationships_following": [
            {
                "title": h,
                "string_list_data": [
                    {
                        "value": h,
                        "href": f"https://instagram.com/{h}",
                        "timestamp": 1783605797,
                    }
                ],
            }
            for h in [f"u{i:03d}" for i in range(480)] + ["special_handle"]
        ]
    }
    (TEST_DIR / "followers_1.json").write_text(json.dumps(followers))
    (TEST_DIR / "following.json").write_text(json.dumps(following))
    print(f"  built: {sorted(p.name for p in TEST_DIR.iterdir())}")


def post_import():
    """Hand-build a multipart/form-data body and POST it."""
    boundary = "----TestBoundary12345"

    def field(name, value, filename=None):
        head = f"--{boundary}\r\n"
        if filename:
            head += (
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: application/json\r\n\r\n"
            )
        else:
            head += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        return head.encode("utf-8") + value.encode("utf-8") + b"\r\n"

    body = b""
    body += field("date", SNAPSHOT_DATE)
    body += field(
        "followers",
        (TEST_DIR / "followers_1.json").read_text(encoding="utf-8"),
        "followers_1.json",
    )
    body += field(
        "following",
        (TEST_DIR / "following.json").read_text(encoding="utf-8"),
        "following.json",
    )
    body += f"--{boundary}--\r\n".encode("utf-8")

    c = http.client.HTTPConnection("localhost", PORT, timeout=15)
    c.request(
        "POST",
        "/import",
        body=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Connection": "close"},
    )
    r = c.getresponse()
    body_text = r.read().decode("utf-8")
    print(f"  status: {r.status} {r.reason}")
    print(f"  body:   {body_text}")
    c.close()
    if r.status != 200:
        raise SystemExit(f"expected 200, got {r.status}: {body_text}")
    payload = json.loads(body_text)
    assert payload["ok"] is True, payload
    assert payload["date"] == SNAPSHOT_DATE, payload
    assert payload["followers"] == 550, payload
    assert payload["following"] == 481, payload
    print("  POST /import 200 OK with expected counts")


def assert_report():
    rep = json.loads((ROOT / "dist" / "report.json").read_text(encoding="utf-8"))
    folders = [s["date_folder"] for s in rep["snapshots"]]
    print(f"  snapshots: {folders}")
    assert "07-12-2026" in folders, folders
    assert SNAPSHOT_DATE in folders, folders
    assert len(rep["weeks"]) >= 1, rep["weeks"]
    w0 = rep["weeks"][0]
    print(f"  weeks[0]: {w0['prev_folder']} -> {w0['curr_folder']} | counts={w0['counts']}")
    # 07-12-2026 had 636 followers / 540 following (real data), B has 550 / 481.
    # expected: lost_followers = 636 - 550 = 86, new_followings > 0.
    if w0["curr_folder"] == SNAPSHOT_DATE:
        assert w0["counts"]["lost_followers"] >= 1, w0["counts"]
        assert w0["counts"]["new_followings"] >= 1, w0["counts"]
    print("  weeks[0] has populated deltas")


def cleanup():
    target = ROOT / "data" / SNAPSHOT_DATE
    if target.exists():
        shutil.rmtree(target)
        print(f"  rm -rf data/{SNAPSHOT_DATE}")
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)
    # re-run analyze so the dist/report.json reflects the cleaned state again
    run.run_analyze(argparse.Namespace(
        port=8000, check_disabled=False, subcommand=None,
    ))
    rep = json.loads((ROOT / "dist" / "report.json").read_text(encoding="utf-8"))
    folders = [s["date_folder"] for s in rep["snapshots"]]
    print(f"  after cleanup, snapshots: {folders}")
    assert folders == ["07-12-2026"], folders


def main():
    print("STEP 0: list data/ + cleanup leftover test fixtures")
    print(f"  before: {sorted(p.name for p in (ROOT / 'data').iterdir())}")
    assert (ROOT / "data" / "07-12-2026").is_dir(), "real 07-12-2026 missing"
    # ponytail: nuke any prior run's SNAPSHOT_DATE folder so parse_folder_date
    # doesn't get tripped up by data pollution from a crashed prior test run.
    stale = ROOT / "data" / SNAPSHOT_DATE
    if stale.exists():
        shutil.rmtree(stale)
        print(f"  cleared stale {stale}")

    print("\nSTEP 1: build fixture")
    make_fixture()

    print("\nSTEP 2: start server on :8765")
    httpd = run._run_server(PORT)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.5)
    # ponytail: probe GET / first to confirm the server is actually bound and
    # answering; if the headless thread died on bind-fail (a previous run's
    # orphan holding :8765, say) the subsequent POST will just hang.
    probe = http.client.HTTPConnection("localhost", PORT, timeout=5)
    probe.request("GET", "/", headers={"Connection": "close"})
    pr = probe.getresponse()
    pr.read(1024)
    probe.close()
    print(f"  server-up probe: GET / -> {pr.status} {pr.reason}")
    assert pr.status < 500, f"server probe returned {pr.status} \u2014 thread likely dead"

    try:
        print("\nSTEP 3: POST /import")
        post_import()

        print("\nSTEP 4: assert dist/report.json has new snapshot + weeks")
        assert_report()

        print("\nSTEP 5: error-path probes")
        # Bad-date POST should come back as JSON 400 with the friendly error
        # string the browser alert will surface.
        boundary2 = "----TestBoundary56789"
        def field2(name, value, filename=None):
            head = f"--{boundary2}\r\n"
            if filename:
                head += (f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                         f"Content-Type: application/json\r\n\r\n")
            else:
                head += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            return head.encode("utf-8") + value.encode("utf-8") + b"\r\n"
        bad_body  = field2("date", "2026-08-15")  # YYYY-MM-DD instead of MM-DD-YYYY
        bad_body += field2("followers", "{}", "followers_1.json")
        bad_body += field2("following", "{}", "following.json")
        bad_body += f"--{boundary2}--\r\n".encode("utf-8")
        c = http.client.HTTPConnection("localhost", PORT, timeout=10)
        c.request(
            "POST", "/import", body=bad_body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary2}",
                     "Connection": "close"},
        )
        r = c.getresponse()
        body_text = r.read(4096).decode("utf-8")
        c.close()
        print(f"  bad-date POST /import -> {r.status}")
        print(f"  body: {body_text[:200]}")
        assert r.status == 400, r.status
        payload = json.loads(body_text)
        assert payload.get("ok") is False, payload
        assert "MM-DD-YYYY" in payload.get("error", ""), payload
        print("  bad-date POST /import returned JSON 400 with friendly message")

        print("\nSTEP 6: cleanup + reanalyze")
        cleanup()

        print("\nALL CHECKS PASS")
    finally:
        # ensure thread doesn't keep the process alive
        pass


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\nASSERTION FAILED: {e}")
        sys.exit(1)
    except SystemExit as e:
        print(f"\nSystemExit: {e}")
        sys.exit(1)
