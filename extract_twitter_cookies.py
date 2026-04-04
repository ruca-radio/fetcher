#!/usr/bin/env python3
"""
Extract Twitter/X cookies from Chrome using yt-dlp's cookie extraction.
Run this after logging into Twitter in Chrome.

Usage: python3 extract_twitter_cookies.py
Output: ./.fetcher/twitter_cookies.json
"""

import http.cookiejar
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / ".fetcher"
YTDLP = BASE_DIR / "yt-dlp"
if not YTDLP.exists():
    YTDLP = Path(shutil.which("yt-dlp") or "/usr/bin/yt-dlp")
STATE_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT = STATE_DIR / "twitter_cookies.json"

IMPORTANT = {"auth_token", "ct0", "twid", "guest_id"}


def extract():
    # Use yt-dlp to dump cookies from Chrome in Netscape format
    cookie_file = tempfile.mktemp(suffix=".txt")
    try:
        result = subprocess.run(
            [str(YTDLP), "--cookies-from-browser", "chrome",
             "--cookies", cookie_file,
             "--skip-download", "--no-warnings",
             "https://x.com"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        print("Timed out extracting cookies.")
        return

    cookie_path = Path(cookie_file)
    if not cookie_path.exists() or cookie_path.stat().st_size == 0:
        print("Failed to extract cookies from Chrome.")
        print(f"yt-dlp stderr: {result.stderr[:300]}")
        return

    # Parse Netscape cookie file
    jar = http.cookiejar.MozillaCookieJar(cookie_file)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as e:
        print(f"Failed to parse cookie file: {e}")
        return
    finally:
        cookie_path.unlink(missing_ok=True)

    # Extract Twitter/X cookies
    cookies = {}
    for cookie in jar:
        if any(d in cookie.domain for d in ["twitter.com", "x.com"]):
            cookies[cookie.name] = cookie.value or ""

    if not cookies:
        print("No Twitter/X cookies found.")
        print("Make sure you're logged into twitter.com or x.com in Chrome.")
        return

    print(f"Found {len(cookies)} Twitter/X cookies:")
    for name in sorted(cookies.keys()):
        marker = " ★" if name in IMPORTANT else ""
        val = cookies[name]
        preview = val[:30] + "…" if len(val) > 30 else val
        print(f"  {name}: {preview}{marker}")

    if "auth_token" not in cookies:
        print("\n⚠  auth_token missing — not logged in.")
        print("   Log in at https://x.com in Chrome, then re-run.")
        return

    OUTPUT.write_text(json.dumps(cookies, indent=2))
    print(f"\n✓ Saved {len(cookies)} cookies to {OUTPUT}")
    print("  Twitter search is now available in FetchTUI.")


if __name__ == "__main__":
    extract()
