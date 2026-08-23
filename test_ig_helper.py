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

    def test_relationships_are_paginated_and_visible_count_is_safe(self):
        class Response:
            ok = True

            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        class Request:
            def __init__(self):
                self.urls = []

            def get(self, url, **_kwargs):
                self.urls.append(url)
                if "web_profile_info" in url:
                    return Response({"data": {"user": {
                        "username": "alice", "id": "7",
                        "edge_followed_by": {"count": 3}, "edge_follow": {"count": 2},
                    }}})
                if "/followers/" in url and "max_id=" not in url:
                    return Response({"users": [{"username": "one", "pk": 11},
                                               {"username": "two", "pk": 22}],
                                     "next_max_id": "page 2"})
                if "/followers/" in url:
                    return Response({"users": [{"username": "three", "pk": 33}]})
                return Response({"users": [{"username": "one", "pk": 11},
                                           {"username": "four", "pk": 44}]})

        request = Request()

        class Session:
            def __init__(self, **_kwargs):
                self.context = types.SimpleNamespace(
                    cookies=lambda: [{"name": "sessionid", "domain": ".instagram.com"}],
                    request=request,
                )

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def fetch(self, *_args, **_kwargs):
                return types.SimpleNamespace(captured_xhr=[])

        fetchers = types.ModuleType("scrapling.fetchers")
        fetchers.DynamicSession = Session
        scrapling = types.ModuleType("scrapling")
        scrapling.fetchers = fetchers
        with tempfile.TemporaryDirectory() as tmp:
            result = Path(tmp) / "result.json"
            with patch.dict(sys.modules, {"scrapling": scrapling, "scrapling.fetchers": fetchers}), \
                    patch.dict(os.environ, {ig_helper.RESULT_FILE_ENV: str(result)}):
                self.assertEqual(ig_helper._check(Path(tmp), "relationships", ["alice"]), 0)
            self.assertEqual(json.loads(result.read_text()), {
                "followers": {"one": "11", "two": "22", "three": "33"},
                "following": {"one": "11", "four": "44"},
                "self_pk": "7",
                # Free with the profile fetch that produced the id, so the
                # caller never needs a second browser run for the totals.
                "counts": {"followers": 3, "following": 2},
            })
        self.assertIn("max_id=page+2", request.urls[2])
        self.assertIn("web_profile_info", request.urls[0])
        # The Instagram user id is what makes a rename provable, so it has to
        # survive the walk -- a bare username list cannot tell "renamed" from
        # "gone".
        self.assertEqual(ig_helper._relationship_users(
            types.SimpleNamespace(request=request), "7", "following", 3),
            {"one": "11", "four": "44"})

    def test_pagination_is_not_bounded_by_the_requested_page_size(self):
        """Instagram serves whatever page size it likes. Budgeting pages against
        the *requested* count truncated every account past roughly 2,000 and
        returned None for the whole fetch, which surfaced as "Instagram
        relationship list unavailable"."""
        pages, per_page = 40, 50
        total = pages * per_page

        class Response:
            ok = True

            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        class Request:
            def get(self, url, **_kwargs):
                page = 0
                if "max_id=" in url:
                    page = int(url.split("max_id=")[1].split("&")[0])
                users = [{"username": f"u{page * per_page + i}", "pk": page * per_page + i}
                         for i in range(per_page)]
                payload = {"users": users}
                if page + 1 < pages:
                    payload["next_max_id"] = str(page + 1)
                return Response(payload)

        users = ig_helper._relationship_users(
            types.SimpleNamespace(request=Request()), "7", "followers", total)
        self.assertIsNotNone(users)
        self.assertEqual(len(users), total)
        self.assertIn("u1999", users)

    def test_truncated_pagination_is_refused_rather_than_returned_partial(self):
        """A partial list reads as "these accounts are gone from Instagram" and
        would get the user to flag people who are perfectly fine."""

        class Response:
            ok = True

            def __init__(self, cursor):
                self.cursor = cursor

            def json(self):
                return {"users": [{"username": f"u{self.cursor}", "pk": self.cursor}],
                        "next_max_id": str(self.cursor)}

        class Request:
            def __init__(self):
                self.calls = 0

            def get(self, url, **_kwargs):
                self.calls += 1
                return Response(self.calls)

        request = Request()
        self.assertIsNone(ig_helper._relationship_users(
            types.SimpleNamespace(request=request), "7", "followers", 10))
        self.assertEqual(request.calls, ig_helper.MAX_RELATIONSHIP_PAGES)

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
