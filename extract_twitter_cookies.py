#!/usr/bin/env python3
"""
Extract Twitter/X cookies from Chrome using yt-dlp's cookie extraction.
Run this after logging into Twitter in Chrome.

Usage: python3 extract_twitter_cookies.py
Output: ./.fetcher/twitter_cookies.json
"""

import http.cookiejar
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from argparse import ArgumentParser
from pathlib import Path

from fetcher_core.models import BASE_DIR, STATE_DIR
from typing import Optional

YTDLP = BASE_DIR / "yt-dlp"
if not YTDLP.exists():
    detected_ytdlp = shutil.which("yt-dlp")
    YTDLP = Path(detected_ytdlp) if detected_ytdlp else None
OUTPUT = STATE_DIR / "twitter_cookies.json"

IMPORTANT = {"auth_token", "ct0", "twid", "guest_id"}


def resolve_ytdlp_command():
    if YTDLP and YTDLP.exists():
        return [str(YTDLP)], None
    if shutil.which("yt-dlp"):
        return ["yt-dlp"], None
    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"], None
    return None, (
        "yt-dlp is not installed. Install it and make sure `yt-dlp` is on your PATH, "
        f"or place a `yt-dlp` binary in {BASE_DIR}."
    )


def extract() -> int:
    ytdlp_cmd, ytdlp_error = resolve_ytdlp_command()
    if ytdlp_error or not ytdlp_cmd:
        print(ytdlp_error or "yt-dlp is unavailable.")
        return 1

    # Use yt-dlp to dump cookies from Chrome in Netscape format
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as temp_cookie_file:
        cookie_file = temp_cookie_file.name
    cookie_path = Path(cookie_file)
    try:
        result = subprocess.run(
            [*ytdlp_cmd, "--cookies-from-browser", "chrome",
             "--cookies", cookie_file,
             "--skip-download", "--no-warnings",
             "https://x.com"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        print("Timed out extracting cookies.")
        cookie_path.unlink(missing_ok=True)
        return 1

    try:
        if not cookie_path.exists() or cookie_path.stat().st_size == 0:
            print("Failed to extract cookies from Chrome.")
            print(f"yt-dlp stderr: {result.stderr[:300]}")
            return 1

        # Parse Netscape cookie file
        jar = http.cookiejar.MozillaCookieJar(cookie_file)
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as e:
        print(f"Failed to parse cookie file: {e}")
        return 1
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
        return 1

    print(f"Found {len(cookies)} Twitter/X cookies:")
    for name in sorted(cookies.keys()):
        marker = " ★" if name in IMPORTANT else ""
        val = cookies[name]
        preview = val[:30] + "…" if len(val) > 30 else val
        print(f"  {name}: {preview}{marker}")

    if "auth_token" not in cookies:
        print("\n⚠  auth_token missing — not logged in.")
        print("   Log in at https://x.com in Chrome, then re-run.")
        return 1

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(cookies, indent=2))
    print(f"\n✓ Saved {len(cookies)} cookies to {OUTPUT}")
    print("  Twitter search is now available in FetchTUI.")
    return 0


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        prog="extract_twitter_cookies.py",
        description="Extract Twitter/X cookies from Chrome into ./.fetcher/twitter_cookies.json.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    return extract()


if __name__ == "__main__":
    sys.exit(main())
