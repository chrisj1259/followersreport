"""Run a conservative profile-status check inside the existing Scrapling venv."""
import json
import re
import sys
from pathlib import Path


def main() -> int:
    project = Path(sys.argv[1]).resolve()
    count_mode = len(sys.argv) == 4 and sys.argv[2] == "counts"
    values = sys.argv[3:] if count_mode else sys.argv[2:]
    usernames = [u.lower() for u in values if re.fullmatch(r"[A-Za-z0-9._]{1,30}", u)]
    results = {u: "unverified" for u in usernames}

    sys.path.insert(0, str(project))
    from dotenv import load_dotenv
    load_dotenv(project / ".env")
    from ig_client import InstagramClient, InstagramRateLimited, _friendly_name, _json

    if count_mode:
        counts = {}
        try:
            with InstagramClient(max_requests=10) as instagram:
                page = instagram._fetch(f"https://www.instagram.com/{usernames[0]}/", network_idle=True)
                for xhr in page.captured_xhr:
                    if _friendly_name(xhr) != "PolarisProfilePageContentQuery":
                        continue
                    user = (_json(xhr).get("data") or {}).get("user") or {}
                    if str(user.get("username", "")).lower() != usernames[0] or not user.get("pk"):
                        continue
                    followers = user.get("follower_count")
                    following = user.get("following_count")
                    if isinstance(followers, int) and isinstance(following, int):
                        counts = {"followers": followers, "following": following}
                    break
        except Exception:
            pass
        print("IFR_COUNTS_JSON=" + json.dumps(counts, separators=(",", ":")))
        return 0

    try:
        with InstagramClient(max_requests=max(10, len(usernames) * 6)) as instagram:
            for username in usernames:
                try:
                    profile = instagram.profile_and_captions(username)
                    if profile.get("pk") and str(profile.get("username", "")).lower() == username:
                        results[username] = "active"
                except InstagramRateLimited:
                    break
                except Exception:
                    continue
    finally:
        print("IFR_STATUS_JSON=" + json.dumps(results, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
