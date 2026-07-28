"""Small offline checks for the release-version rules used by app.py."""
import io
import json
import sys
import types
import unittest
from unittest import mock

sys.modules.setdefault("webview", types.ModuleType("webview"))
import app


class VersionTests(unittest.TestCase):
    def test_stable_semver_only(self) -> None:
        self.assertEqual(app._version_key("v1.2.3"), (1, 2, 3))
        self.assertEqual(app._version_key("1.2.3"), (1, 2, 3))
        self.assertIsNone(app._version_key("v1.2"))
        self.assertIsNone(app._version_key("v1.2.3-beta"))

    def test_selects_only_the_expected_newer_installer(self) -> None:
        release = {
            "tag_name": "v1.2.0",
            "draft": False,
            "prerelease": False,
            "assets": [{
                "name": "InstagramFollowerReport-Setup-v1.2.0.exe",
                "browser_download_url": (
                    "https://github.com/chrisj1259/removedfollowers/releases/"
                    "download/v1.2.0/InstagramFollowerReport-Setup-v1.2.0.exe"
                ),
            }],
        }
        response = io.BytesIO(json.dumps(release).encode())
        response.__enter__ = lambda self: self
        response.__exit__ = lambda *args: None
        with (
            mock.patch.object(app.sys, "frozen", True, create=True),
            mock.patch.object(app, "_installed_version", return_value=(1, 1, 0)),
            mock.patch.object(app.urllib.request, "urlopen", return_value=response),
        ):
            self.assertEqual(app._latest_update(), ("1.2.0", release["assets"][0]["browser_download_url"]))


if __name__ == "__main__":
    unittest.main()
