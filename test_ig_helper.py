import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import ig_helper


class InstagramHelperTests(unittest.TestCase):
    def test_check_captures_graphql_and_writes_file_result(self):
        seen = {}
        xhr = types.SimpleNamespace(
            request_headers={"x-fb-friendly-name": "PolarisProfilePageContentQuery"},
            body=json.dumps({"data": {"user": {
                "username": "alice", "pk": "1", "follower_count": 2, "following_count": 3,
            }}}).encode(),
        )

        class Session:
            def __init__(self, **kwargs):
                seen.update(kwargs)
                self.context = types.SimpleNamespace(cookies=lambda: [
                    {"name": "sessionid", "domain": ".instagram.com"}
                ])

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def fetch(self, *_args, **_kwargs):
                return types.SimpleNamespace(captured_xhr=[xhr])

        fetchers = types.ModuleType("scrapling.fetchers")
        fetchers.DynamicSession = Session
        scrapling = types.ModuleType("scrapling")
        scrapling.fetchers = fetchers
        with tempfile.TemporaryDirectory() as tmp:
            result = Path(tmp) / "result.json"
            with patch.dict(sys.modules, {"scrapling": scrapling, "scrapling.fetchers": fetchers}), \
                    patch.dict(os.environ, {ig_helper.RESULT_FILE_ENV: str(result)}):
                self.assertEqual(ig_helper._check(Path(tmp), "counts", ["alice"]), 0)
            self.assertEqual(json.loads(result.read_text()), {"followers": 2, "following": 3})
        self.assertIn("graphql", seen["capture_xhr"])

    def test_check_removes_marker_when_cookie_expired(self):
        class Session:
            def __init__(self, **_kwargs):
                self.context = types.SimpleNamespace(cookies=lambda: [])

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        fetchers = types.ModuleType("scrapling.fetchers")
        fetchers.DynamicSession = Session
        scrapling = types.ModuleType("scrapling")
        scrapling.fetchers = fetchers
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / ig_helper.SESSION_MARKER
            marker.touch()
            result = Path(tmp) / "result.json"
            with patch.dict(sys.modules, {"scrapling": scrapling, "scrapling.fetchers": fetchers}), \
                    patch.dict(os.environ, {ig_helper.RESULT_FILE_ENV: str(result)}):
                self.assertEqual(ig_helper._check(Path(tmp), "status", ["alice"]), 1)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
