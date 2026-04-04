#!/usr/bin/env python3
"""
Fetcher v1.0 - Multi-site video downloader
Tabs: Porn (PornHub · xHamster · RedTube · Eporner) | Twitter/X
Parallel downloads with per-video progress bars
Temporary downloads are moved into the configured output directory on completion
"""

import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
from argparse import ArgumentParser, Namespace
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import quote_plus

import httpx
from PIL import Image
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.message import Message
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import (
    Button, Checkbox, DataTable, Footer, Header,
    Input, Label, Log, ProgressBar, Select, Static,
    TabbedContent, TabPane,
)

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / ".fetcher"
DEFAULT_TMP_DIR = BASE_DIR / "tmp"
DEFAULT_OUTPUT_DIR = Path.home() / "Downloads" / "fetcher"
DOWNLOADED_IDS = STATE_DIR / "downloaded_ids.txt"
TWITTER_COOKIES = STATE_DIR / "twitter_cookies.json"
SETTINGS_PATH  = STATE_DIR / "settings.json"
YTDLP = BASE_DIR / "yt-dlp"
if not YTDLP.exists():
    detected_ytdlp = shutil.which("yt-dlp")
    YTDLP = Path(detected_ytdlp) if detected_ytdlp else Path("/usr/bin/yt-dlp")

STATE_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_TMP_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SITES = ["All Sites", "PornHub", "xHamster", "RedTube", "Eporner"]
MPV = shutil.which("mpv")
DEFAULT_EXCLUDE_PHRASES = ""


# ── Already-downloaded detection ──────────────────────────────────────────────
def _normalize_title(title: str) -> str:
    """Lowercase, strip non-alphanumeric for fuzzy title matching."""
    return re.sub(r"[^a-z0-9]", "", title.lower())


def _split_exclude_phrases(raw: str) -> list[str]:
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def load_settings() -> AppSettings:
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text())
            sf = data.get("search_filters", {}) or {}
            return AppSettings(
                theme=str(data.get("theme", "textual-dark")),
                thumbnails_enabled=bool(data.get("thumbnails_enabled", True)),
                video_previews_enabled=bool(data.get("video_previews_enabled", True)),
                plex_dir=str(data.get("plex_dir", DEFAULT_OUTPUT_DIR)),
                tmp_dir=str(data.get("tmp_dir", DEFAULT_TMP_DIR)),
                parallel_downloads=max(1, int(data.get("parallel_downloads", 3))),
                search_filters=SearchFilters(
                    min_duration_min=max(0, int(sf.get("min_duration_min", 0) or 0)),
                    max_duration_min=max(0, int(sf.get("max_duration_min", 0) or 0)),
                    min_quality=str(sf.get("min_quality", "any")),  # type: ignore[arg-type]
                    min_rating=max(0, int(sf.get("min_rating", 0) or 0)),
                    hd_only=bool(sf.get("hd_only", False)),
                    pages=max(1, int(sf.get("pages", 2) or 2)),
                    exclude_phrases=str(sf.get("exclude_phrases", DEFAULT_EXCLUDE_PHRASES)),
                ),
            )
        except Exception:
            pass
    return AppSettings()


def save_settings(settings: AppSettings) -> None:
    SETTINGS_PATH.write_text(json.dumps({
        "theme": settings.theme,
        "thumbnails_enabled": settings.thumbnails_enabled,
        "video_previews_enabled": settings.video_previews_enabled,
        "plex_dir": settings.plex_dir,
        "tmp_dir": settings.tmp_dir,
        "parallel_downloads": settings.parallel_downloads,
        "search_filters": {
            "min_duration_min": settings.search_filters.min_duration_min,
            "max_duration_min": settings.search_filters.max_duration_min,
            "min_quality": settings.search_filters.min_quality,
            "min_rating": settings.search_filters.min_rating,
            "hd_only": settings.search_filters.hd_only,
            "pages": settings.search_filters.pages,
            "exclude_phrases": settings.search_filters.exclude_phrases,
        },
    }, indent=2))


def load_downloaded(plex_dir: Path) -> tuple[set[str], set[str]]:
    """Return (video_ids, normalized_titles) of already-downloaded content."""
    ids: set[str] = set()
    titles: set[str] = set()
    if DOWNLOADED_IDS.exists():
        for line in DOWNLOADED_IDS.read_text().splitlines():
            line = line.strip()
            if line:
                ids.add(line)
    if plex_dir.exists():
        for f in plex_dir.iterdir():
            if f.suffix == ".mp4":
                norm = _normalize_title(f.stem)
                if norm:
                    titles.add(norm)
    return ids, titles


def _fmt_size(nbytes: int) -> str:
    if nbytes >= 1_073_741_824:
        return f"{nbytes / 1_073_741_824:.1f} GB"
    if nbytes >= 1_048_576:
        return f"{nbytes / 1_048_576:.0f} MB"
    return f"{nbytes / 1024:.0f} KB"


def cleanup_tmp(tmp_dir: Path, plex_dir: Path) -> tuple[int, int]:
    """Move completed mp4s to plex, delete stale partials. Returns (moved, deleted)."""
    moved = deleted = 0
    if not tmp_dir.exists():
        return moved, deleted
    tmp_dir.mkdir(parents=True, exist_ok=True)
    plex_dir.mkdir(parents=True, exist_ok=True)
    for f in tmp_dir.iterdir():
        if f.suffix == ".mp4" and ".part" not in f.name:
            try:
                shutil.move(str(f), str(plex_dir / f.name))
                moved += 1
            except Exception:
                pass
        elif f.suffix in (".part", ".ytdl") or ".part-Frag" in f.name:
            try:
                f.unlink()
                deleted += 1
            except Exception:
                pass
    return moved, deleted


def _render_ascii_thumbnail(image_bytes: bytes, width: int = 34) -> str:
    chars = " .:-=+*#%@"
    image = Image.open(BytesIO(image_bytes)).convert("L")
    aspect = image.height / max(1, image.width)
    height = max(8, math.ceil(width * aspect * 0.5))
    image = image.resize((width, height))

    lines: list[str] = []
    for y in range(image.height):
        row_chars: list[str] = []
        for x in range(image.width):
            value = image.getpixel((x, y))
            idx = round((value / 255) * (len(chars) - 1))
            row_chars.append(chars[idx])
        lines.append("".join(row_chars))
    return "\n".join(lines)


# ── Data model ────────────────────────────────────────────────────────────────
@dataclass
class Video:
    id: str
    title: str
    duration_s: int
    views: int
    url: str
    site: str
    thumbnail_url: Optional[str] = None
    rating_percent: Optional[int] = None
    quality_p: Optional[int] = None
    is_hd: Optional[bool] = None
    selected: bool = False

    @property
    def duration_str(self) -> str:
        m, s = divmod(self.duration_s, 60)
        return f"{m}:{s:02d}"

    @property
    def views_str(self) -> str:
        v = self.views
        if v >= 1_000_000:
            return f"{v/1_000_000:.1f}M"
        if v >= 1_000:
            return f"{v/1_000:.0f}K"
        return str(v)


@dataclass
class DownloadJob:
    video: Video
    status: str = "queued"   # queued | downloading | paused | done | failed
    progress: float = 0.0
    speed: str = ""
    eta: str = ""
    filepath: Optional[Path] = None
    file_size: int = 0
    proc: Optional[subprocess.Popen] = None
    worker_started: bool = False


SortKey = Literal["title", "duration", "site", "views"]
FileSortKey = Literal["name", "size", "modified", "path"]
QualityFilter = Literal["any", "360", "480", "720", "1080"]


@dataclass
class SearchFilters:
    min_duration_min: int = 0
    max_duration_min: int = 0
    min_quality: QualityFilter = "any"
    min_rating: int = 0
    hd_only: bool = False
    pages: int = 2
    exclude_phrases: str = DEFAULT_EXCLUDE_PHRASES

    @property
    def min_duration_s(self) -> int:
        return max(0, self.min_duration_min) * 60

    @property
    def max_duration_s(self) -> int:
        if self.max_duration_min <= 0:
            return 24 * 60 * 60
        return self.max_duration_min * 60


@dataclass
class AppSettings:
    theme: str = "textual-dark"
    thumbnails_enabled: bool = True
    video_previews_enabled: bool = True
    plex_dir: str = str(DEFAULT_OUTPUT_DIR)
    tmp_dir: str = str(DEFAULT_TMP_DIR)
    parallel_downloads: int = 3
    search_filters: SearchFilters = field(default_factory=SearchFilters)

    @property
    def plex_path(self) -> Path:
        return Path(self.plex_dir).expanduser()

    @property
    def tmp_path(self) -> Path:
        return Path(self.tmp_dir).expanduser()


@dataclass
class FileEntry:
    path: Path
    size: int
    modified_ts: float

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def modified_str(self) -> str:
        return datetime.fromtimestamp(self.modified_ts).strftime("%Y-%m-%d %H:%M")


# ── Porn search functions ────────────────────────────────────────────────────
def _parse_dur(s: str) -> int:
    parts = str(s).split(":")
    try:
        if len(parts) == 1:
            return int(float(parts[0]))
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except Exception:
        pass
    return 0


def _parse_percent(value: object) -> Optional[int]:
    try:
        text = str(value).strip().rstrip("%")
        if not text:
            return None
        return max(0, min(100, int(float(text))))
    except Exception:
        return None


def _parse_quality(value: object) -> Optional[int]:
    if value is None:
        return None
    text = str(value).lower()
    match = re.search(r"(\d{3,4})", text)
    if match:
        return int(match.group(1))
    if text in {"hd", "true"}:
        return 720
    if text in {"sd", "false"}:
        return 480
    return None


def search_pornhub(query: str, min_s: int, max_s: int, pages: int) -> list[Video]:
    out, seen = [], set()
    with httpx.Client(timeout=20, verify=False, headers=HEADERS) as c:
        for page in range(1, pages + 1):
            url = f"https://www.pornhub.com/webmasters/search?search={quote_plus(query)}&page={page}"
            try:
                for v in c.get(url).json().get("videos", []):
                    vid = v.get("video_id", "")
                    if not vid or vid in seen:
                        continue
                    seen.add(vid)
                    dur = _parse_dur(v.get("duration", ""))
                    if min_s <= dur <= max_s:
                        hd = bool(v.get("hd"))
                        out.append(Video(id=vid, title=v.get("title", ""),
                            duration_s=dur, views=int(v.get("views", 0) or 0),
                            url=f"https://www.pornhub.com/view_video.php?viewkey={vid}",
                            site="PornHub",
                            thumbnail_url=v.get("default_thumb") or v.get("thumb"),
                            rating_percent=_parse_percent(v.get("rating")),
                            quality_p=_parse_quality(v.get("quality")) or (720 if hd else 480),
                            is_hd=hd))
            except Exception:
                pass
    return out


def _xh_parse(html: str) -> list[dict]:
    idx = html.find('"videoThumbProps"')
    if idx < 0:
        return []
    try:
        start = html.index("[", idx)
        depth = 0
        for i, ch in enumerate(html[start:], start):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return json.loads(html[start:i+1].replace("\\/", "/"))
    except Exception:
        pass
    return []


def search_xhamster(query: str, min_s: int, max_s: int, pages: int) -> list[Video]:
    out, seen = [], set()
    slug = re.sub(r"\s+", "-", query.strip())
    with httpx.Client(timeout=20, verify=False, headers=HEADERS, follow_redirects=True) as c:
        for page in range(1, pages + 1):
            url = f"https://xhamster.com/search/{slug}" + (f"?page={page}" if page > 1 else "")
            try:
                for v in _xh_parse(c.get(url).text):
                    vid = str(v.get("id", ""))
                    if not vid or vid in seen:
                        continue
                    seen.add(vid)
                    dur = int(v.get("duration", 0))
                    if min_s <= dur <= max_s:
                        quality = _parse_quality(v.get("quality")) or _parse_quality(v.get("videoQuality"))
                        hd_flag = v.get("isHD")
                        out.append(Video(id=vid, title=v.get("title", ""),
                            duration_s=dur, views=int(v.get("viewCount", 0) or 0),
                            url=v.get("pageURL", "").replace("\\/", "/"),
                            site="xHamster",
                            thumbnail_url=(
                                v.get("thumbURL")
                                or v.get("thumbURL169")
                                or v.get("imageURL")
                            ),
                            rating_percent=_parse_percent(v.get("rating")),
                            quality_p=quality,
                            is_hd=bool(hd_flag) if hd_flag is not None else (quality >= 720 if quality is not None else None)))
            except Exception:
                pass
    return out


def search_redtube(query: str, min_s: int, max_s: int, pages: int) -> list[Video]:
    out, seen = [], set()
    with httpx.Client(timeout=20, verify=False, headers=HEADERS) as c:
        for page in range(1, pages + 1):
            url = (f"https://api.redtube.com/?data=redtube.Videos.searchVideos"
                   f"&search={quote_plus(query)}&output=json&page={page}&limit=20"
                   f"&min_duration={min_s}&max_duration={max_s}")
            try:
                for entry in c.get(url).json().get("videos", []):
                    v = entry.get("video", {})
                    vid = str(v.get("video_id", ""))
                    if not vid or vid in seen:
                        continue
                    seen.add(vid)
                    dur = _parse_dur(str(v.get("duration", "0:00")))
                    if min_s <= dur <= max_s:
                        hd = bool(v.get("hd"))
                        out.append(Video(id=vid, title=v.get("title", ""),
                            duration_s=dur, views=int(v.get("views", 0) or 0),
                            url=v.get("url", f"https://www.redtube.com/{vid}"),
                            site="RedTube",
                            thumbnail_url=v.get("default_thumb") or v.get("thumb"),
                            rating_percent=_parse_percent(v.get("rating")),
                            quality_p=_parse_quality(v.get("quality")) or (720 if hd else 480),
                            is_hd=hd))
            except Exception:
                pass
    return out


def search_eporner(query: str, min_s: int, max_s: int, pages: int) -> list[Video]:
    out, seen = [], set()
    with httpx.Client(timeout=20, verify=False, headers=HEADERS) as c:
        for page in range(1, pages + 1):
            url = (f"https://www.eporner.com/api/v2/video/search/"
                   f"?query={quote_plus(query)}&per_page=20&page={page}"
                   f"&min_duration={min_s}&max_duration={max_s}&format=json")
            try:
                for v in c.get(url).json().get("videos", []):
                    vid = str(v.get("id", ""))
                    if not vid or vid in seen:
                        continue
                    seen.add(vid)
                    dur = int(v.get("length_sec", 0))
                    raw_url = v.get("url", "")
                    if not raw_url:
                        raw_url = f"https://www.eporner.com/video-{vid}/"
                    elif not raw_url.startswith("http"):
                        raw_url = "https://www.eporner.com" + raw_url
                    if min_s <= dur <= max_s:
                        quality = _parse_quality(v.get("quality"))
                        out.append(Video(id=vid, title=v.get("title", ""),
                            duration_s=dur, views=int(v.get("views", 0) or 0),
                            url=raw_url, site="Eporner",
                            thumbnail_url=v.get("default_thumb") or v.get("thumb"),
                            rating_percent=_parse_percent(v.get("rate") or v.get("rating")),
                            quality_p=quality,
                            is_hd=(quality >= 720) if quality is not None else None))
            except Exception:
                pass
    return out


SITE_FN = {
    "PornHub":  search_pornhub,
    "xHamster": search_xhamster,
    "RedTube":  search_redtube,
    "Eporner":  search_eporner,
}


def filter_video(video: Video, filters: SearchFilters) -> bool:
    if video.duration_s < filters.min_duration_s:
        return False
    if filters.max_duration_min > 0 and video.duration_s > filters.max_duration_s:
        return False
    if filters.min_rating > 0 and video.rating_percent is not None and video.rating_percent < filters.min_rating:
        return False
    if filters.hd_only and video.is_hd is False:
        return False
    if filters.min_quality != "any":
        min_quality = int(filters.min_quality)
        if video.quality_p is not None and video.quality_p < min_quality:
            return False
        if video.quality_p is None and video.is_hd is False and min_quality >= 720:
            return False
    title_lower = video.title.lower()
    for phrase in _split_exclude_phrases(filters.exclude_phrases):
        if phrase in title_lower:
            return False
    return True


def dedupe_videos(videos: list[Video]) -> list[Video]:
    seen_ids: set[str] = set()
    seen_titles: set[str] = set()
    unique: list[Video] = []
    for video in videos:
        norm = _normalize_title(video.title)
        if video.id in seen_ids:
            continue
        if norm and norm in seen_titles:
            continue
        seen_ids.add(video.id)
        if norm:
            seen_titles.add(norm)
        unique.append(video)
    return unique


def search_videos(query: str, site: str, filters: SearchFilters, twitter_count: int = 40) -> list[Video]:
    if site == "Twitter":
        return dedupe_videos(search_twitter(query, twitter_count, TWITTER_COOKIES))

    sites = list(SITE_FN) if site == "All Sites" else [site]
    results: list[Video] = []
    for name in sites:
        found = SITE_FN[name](query, filters.min_duration_s, filters.max_duration_s, filters.pages)
        results.extend(video for video in found if filter_video(video, filters))
    return dedupe_videos(results)


def video_to_dict(video: Video) -> dict[str, object]:
    return {
        "id": video.id,
        "title": video.title,
        "site": video.site,
        "duration_seconds": video.duration_s,
        "duration": video.duration_str,
        "views": video.views,
        "url": video.url,
        "rating_percent": video.rating_percent,
        "quality_p": video.quality_p,
        "is_hd": video.is_hd,
    }


def build_filters_from_args(args: Namespace, settings: AppSettings) -> SearchFilters:
    base = settings.search_filters
    return SearchFilters(
        min_duration_min=args.min_duration if args.min_duration is not None else base.min_duration_min,
        max_duration_min=args.max_duration if args.max_duration is not None else base.max_duration_min,
        min_quality=args.min_quality if args.min_quality is not None else base.min_quality,
        min_rating=args.min_rating if args.min_rating is not None else base.min_rating,
        hd_only=args.hd_only if getattr(args, "hd_only", False) else base.hd_only,
        pages=args.pages if args.pages is not None else base.pages,
        exclude_phrases=args.exclude if args.exclude is not None else base.exclude_phrases,
    )


def normalize_download_target(target: str, site: str) -> tuple[str, str]:
    if re.match(r"^https?://", target):
        return target, target
    if site == "PornHub":
        return target, f"https://www.pornhub.com/view_video.php?viewkey={target}"
    return target, target


def build_download_video(target: str, site: str) -> Video:
    video_id, url = normalize_download_target(target, site)
    return Video(
        id=video_id,
        title=target,
        duration_s=0,
        views=0,
        url=url,
        site=site,
    )


# ── Twitter/X search via headless Chrome + network capture ───────────────────
def search_twitter(query: str, count: int, cookies_file: Path) -> list[Video]:
    """Search Twitter Media tab using headless Chrome, capture GraphQL responses."""
    import time as _time
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    # Load auth_token from cookies file
    if cookies_file.exists():
        try:
            cdata = json.loads(cookies_file.read_text())
            auth_token = cdata.get("auth_token", "")
        except Exception:
            auth_token = ""
    else:
        return []

    if not auth_token:
        return []

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(options=opts)
    out: list[Video] = []
    seen: set[str] = set()

    try:
        # Set auth cookie
        driver.get("https://x.com")
        _time.sleep(2)
        driver.add_cookie({
            "name": "auth_token", "value": auth_token,
            "domain": ".x.com", "path": "/",
        })

        # Search media tab
        from urllib.parse import quote_plus as _qp
        driver.get(f"https://x.com/search?q={_qp(query)}&f=media")
        _time.sleep(6)

        # Scroll to load more results
        scrolls = max(1, count // 40)
        for _ in range(scrolls):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
            _time.sleep(3)

        # Parse network logs for SearchTimeline responses
        logs = driver.get_log("performance")
        for entry in logs:
            msg = json.loads(entry["message"])["message"]
            if msg["method"] != "Network.responseReceived":
                continue
            url = msg["params"]["response"]["url"]
            if "SearchTimeline" not in url:
                continue
            if msg["params"]["response"]["status"] != 200:
                continue

            req_id = msg["params"]["requestId"]
            try:
                body = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": req_id})
                data = json.loads(body["body"])
            except Exception:
                continue

            instructions = (data.get("data", {})
                           .get("search_by_raw_query", {})
                           .get("search_timeline", {})
                           .get("timeline", {})
                           .get("instructions", []))

            for inst in instructions:
                # Collect items from grid entries and moduleItems
                items: list[dict] = []
                for e in inst.get("entries", []):
                    content = e.get("content", {})
                    items.extend(content.get("items", []))
                items.extend(inst.get("moduleItems", []))

                for it in items:
                    result = (it.get("item", {})
                             .get("itemContent", {})
                             .get("tweet_results", {})
                             .get("result", {}))

                    if result.get("__typename") == "TweetWithVisibilityResults":
                        result = result.get("tweet", {})

                    tid = result.get("rest_id", "")
                    if not tid or tid in seen:
                        continue
                    seen.add(tid)

                    legacy = result.get("legacy", {})
                    user_legacy = (result.get("core", {})
                                  .get("user_results", {})
                                  .get("result", {})
                                  .get("legacy", {}))

                    user_result = (result.get("core", {})
                                  .get("user_results", {})
                                  .get("result", {}))
                    user_core = user_result.get("core", {})
                    sn = (user_core.get("screen_name", "")
                          or user_legacy.get("screen_name", "")
                          or user_result.get("rest_id", ""))
                    text = legacy.get("full_text", "")[:120].replace("\n", " ")
                    title = f"@{sn}: {text}" if sn else text

                    # Check for video media
                    media = legacy.get("extended_entities", {}).get("media", [])
                    has_vid = any(m.get("type") == "video" for m in media)
                    dur_ms = 0
                    for m in media:
                        if m.get("type") == "video":
                            dur_ms = m.get("video_info", {}).get("duration_millis", 0)
                    thumb_url = ""
                    for m in media:
                        if m.get("type") == "video":
                            thumb_url = m.get("media_url_https") or m.get("media_url", "")
                            break

                    views_raw = result.get("views", {}).get("count", "0")
                    views = int(views_raw) if str(views_raw).isdigit() else 0
                    likes = legacy.get("favorite_count", 0)

                    out.append(Video(
                        id=tid,
                        title=title,
                        duration_s=dur_ms // 1000,
                        views=views if views else likes,
                        url=f"https://x.com/{sn}/status/{tid}",
                        site="Twitter",
                        thumbnail_url=thumb_url,
                    ))
    except Exception:
        pass
    finally:
        driver.quit()

    return out


# ── Download worker (runs in thread) ─────────────────────────────────────────
def run_download(job: DownloadJob, tmp_dir: Path, plex_dir: Path, progress_cb, done_cb) -> None:
    """Execute yt-dlp, parse progress into job fields, move file to plex."""
    job.status = "downloading"
    cmd = [
        str(YTDLP),
        "-o", str(tmp_dir / "%(title)s.%(ext)s"),
        "-f", "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "--merge-output-format", "mp4",
        "--no-playlist", "--newline", "--no-warnings",
        "--retries", "3", "--fragment-retries", "5",
    ]
    # Twitter NSFW videos require authenticated cookies
    if job.video.site == "Twitter":
        cmd.extend(["--cookies-from-browser", "chrome"])
    cmd.append(job.video.url)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, start_new_session=True)
    job.proc = proc
    progress_cb()

    dest: Optional[Path] = None
    pct_re   = re.compile(r"\[download\]\s+([\d.]+)%.*?at\s+([\d.]+\s*\S+).*?ETA\s+(\S+)")
    done_re  = re.compile(r"\[download\]\s+100%\s+of")
    dest_re  = re.compile(r"\[download\] Destination: (.+)")
    merge_re = re.compile(r'\[Merger\] Merging formats into "(.+)"')

    for line in proc.stdout or []:
        line = line.rstrip()
        if m := pct_re.search(line):
            job.progress = float(m.group(1))
            job.speed    = m.group(2)
            job.eta      = m.group(3)
            progress_cb()
        elif done_re.search(line):
            job.progress = 100.0
            job.speed    = ""
            job.eta      = ""
            progress_cb()
        elif m := dest_re.search(line):
            dest = Path(m.group(1).strip())
        elif m := merge_re.search(line):
            dest = Path(m.group(1).strip().strip('"'))

    proc.wait()
    job.proc = None

    if proc.returncode == 0:
        if not (dest and dest.exists()):
            candidates = sorted(tmp_dir.glob("*.mp4"),
                                key=lambda p: p.stat().st_mtime, reverse=True)
            dest = candidates[0] if candidates else None

        if dest and dest.exists():
            target = plex_dir / dest.name
            try:
                shutil.move(str(dest), str(target))
                job.filepath  = target
                job.file_size = target.stat().st_size
                job.status    = "done"
                job.progress  = 100.0
                with open(DOWNLOADED_IDS, "a") as f:
                    f.write(job.video.id + "\n")
            except Exception as e:
                job.status = "failed"
                job.eta    = str(e)
        else:
            job.status = "failed"
            job.eta    = "no output file"
    else:
        job.status = "failed"
        job.eta    = f"exit {proc.returncode}"

    done_cb(job)


def resolve_stream_urls(video: Video) -> tuple[list[str], Optional[str]]:
    """Resolve direct media URLs for mpv without relying on mpv's yt-dlp hook."""
    cmd = [
        str(YTDLP),
        "-g",
        "--no-playlist",
        "-f",
        "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    ]
    if video.site == "Twitter":
        cmd.extend(["--cookies-from-browser", "chrome"])
    cmd.append(video.url)

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except Exception as exc:
        return [], str(exc)

    urls = [line.strip() for line in proc.stdout.splitlines() if line.strip().startswith("http")]
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip() or f"yt-dlp exit {proc.returncode}"
        return urls, err
    if not urls:
        return [], "yt-dlp returned no playable media URL"
    return urls, None


def print_results_table(results: list[Video], limit: Optional[int] = None) -> None:
    rows = results[:limit] if limit else results
    if not rows:
        print("No results.")
        return
    for idx, video in enumerate(rows, start=1):
        duration = video.duration_str if video.duration_s else "—"
        print(f"{idx:>3}. [{video.site}] {duration:>6}  {video.views_str:>6}  {video.title}")
        print(f"     {video.url}")


def download_one(video: Video, tmp_dir: Path, output_dir: Path) -> DownloadJob:
    job = DownloadJob(video=video)

    def progress_cb() -> None:
        return

    def done_cb(_: DownloadJob) -> None:
        return

    run_download(job, tmp_dir, output_dir, progress_cb, done_cb)
    return job


def run_batch_download(videos: list[Video], tmp_dir: Path, output_dir: Path, parallel: int) -> list[DownloadJob]:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    jobs: list[DownloadJob] = []
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as executor:
        futures = {executor.submit(download_one, video, tmp_dir, output_dir): video for video in videos}
        for future in as_completed(futures):
            jobs.append(future.result())
    return jobs


# ── Per-job progress row widget ───────────────────────────────────────────────
SITE_COLOR = {
    "PornHub":  "orange",
    "xHamster": "red",
    "RedTube":  "bright_red",
    "Eporner":  "blue",
    "Twitter":  "cyan",
}

class JobRow(Static):
    """One row in the queue panel: title line + progress bar + controls."""

    DEFAULT_CSS = """
    JobRow {
        height: auto;
        padding: 0 0 1 0;
        border-bottom: dashed $panel-lighten-1;
    }
    JobRow .job-title  { height: 1; }
    JobRow .job-meta   { height: 1; color: $text-muted; }
    JobRow ProgressBar { height: 1; }
    JobRow .job-controls { height: 1; layout: horizontal; }
    JobRow .job-controls Button { min-width: 3; height: 1; margin: 0 0; padding: 0 1; }
    """

    def __init__(self, job: DownloadJob) -> None:
        super().__init__()
        self._job = job

    def compose(self) -> ComposeResult:
        j = self._job
        color = SITE_COLOR.get(j.video.site, "white")
        yield Static(
            f"⏳ [{color}]{j.video.site}[/]  {j.video.title[:55]}",
            classes="job-title",
        )
        yield ProgressBar(total=100, show_eta=False, show_percentage=False)
        with Horizontal(classes="job-controls"):
            yield Static("queued", classes="job-meta")
            yield Button("▶", name="play",  variant="success", disabled=True)
            yield Button("⏸", name="pause", variant="warning", disabled=True)
            yield Button("■", name="stop",  variant="error",   disabled=False)
            yield Button("↻", name="retry", variant="default", disabled=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        j = self._job
        name = event.button.name
        if name == "play" and j.proc and j.status == "paused":
            try:
                os.killpg(os.getpgid(j.proc.pid), signal.SIGCONT)
            except ProcessLookupError:
                pass
            j.status = "downloading"
            self.refresh_display()
        elif name == "pause" and j.proc and j.status == "downloading":
            try:
                os.killpg(os.getpgid(j.proc.pid), signal.SIGSTOP)
            except ProcessLookupError:
                pass
            j.status = "paused"
            self.refresh_display()
        elif name == "stop" and j.status in ("downloading", "paused", "queued"):
            if j.proc:
                if j.status == "paused":
                    try:
                        os.killpg(os.getpgid(j.proc.pid), signal.SIGCONT)
                    except ProcessLookupError:
                        pass
                j.proc.terminate()
            j.status = "failed"
            j.eta = "stopped"
            self.refresh_display()
            self.post_message(self.StopRequested(self))
        elif name == "retry" and j.status == "failed":
            j.status = "retry"
            self.refresh_display()
            self.post_message(self.RetryRequested(self))
        event.stop()

    class RetryRequested(Message):
        """Bubble up to FetchTUI to re-queue this job."""
        def __init__(self, row: "JobRow") -> None:
            super().__init__()
            self.row = row

    class StopRequested(Message):
        """Bubble up to FetchTUI to schedule removal of stopped job."""
        def __init__(self, row: "JobRow") -> None:
            super().__init__()
            self.row = row

    def refresh_display(self) -> None:
        j     = self._job
        color = SITE_COLOR.get(j.video.site, "white")
        icons = {
            "queued": "⏳", "downloading": "⬇ ", "paused": "⏸ ",
            "done": "✓ ", "failed": "✗ ", "retry": "↻ ",
        }
        icon = icons.get(j.status, "? ")

        self.query_one(".job-title", Static).update(
            f"{icon}[{color}]{j.video.site}[/]  {j.video.title[:55]}"
        )

        bar = self.query_one(ProgressBar)
        bar.progress = j.progress

        if j.status == "downloading":
            meta = f"{j.speed}  ETA {j.eta}  {j.progress:.0f}%"
        elif j.status == "paused":
            meta = f"⏸ paused at {j.progress:.0f}%"
        elif j.status == "done":
            size = _fmt_size(j.file_size) if j.file_size else ""
            meta = f"✓ {j.video.duration_str}  {size}"
        elif j.status == "failed":
            meta = f"✗ {j.eta}"
        elif j.status == "retry":
            meta = "↻ retrying…"
        else:
            meta = "queued"
        self.query_one(".job-meta", Static).update(meta)

        active = j.status in ("downloading", "paused", "queued")
        for btn in self.query(Button):
            if btn.name == "play":
                btn.disabled = (j.status != "paused")
            elif btn.name == "pause":
                btn.disabled = (j.status != "downloading")
            elif btn.name == "stop":
                btn.disabled = not active
            elif btn.name == "retry":
                btn.disabled = (j.status != "failed")


# ── Help modal ────────────────────────────────────────────────────────────────
class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape,q", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Static(
                "[bold cyan]FetchTUI[/]\n\n"
                "[yellow]Search[/]\n"
                "  Enter / Search    Run search\n"
                "  Click headers     Sort by name / duration / source / views\n"
                "  Site dropdown     PornHub · xHamster · RedTube · Eporner · All\n"
                "  Min/Max/Pages     Duration filter (min) & result depth\n"
                "  Parallel          Max simultaneous downloads\n"
                "  Hide downloaded   Skip already-grabbed videos\n\n"
                "[yellow]Selection[/]\n"
                "  Space / Enter     Toggle row selection\n"
                "  a                 Select all results\n"
                "  c                 Clear all selections\n\n"
                "[yellow]Downloads[/]\n"
                "  d                 Download selected\n"
                "  p                 Play highlighted result in mpv\n"
                "  S                 Stop all active downloads\n"
                "  x                 Clean tmp/ (move mp4, delete partials)\n"
                "  ↻ button          Retry a failed download\n\n"
                "[yellow]Files[/]\n"
                "  Files tab         Browse the configured output directory\n"
                "  Click headers     Sort by name / size / modified / path\n"
                "  Play file         Open highlighted local file\n\n"
                "[yellow]Twitter[/]\n"
                "  Requires login (username + password) — cookies saved locally\n"
                "  Search returns video tweets only\n\n"
                "[yellow]Other[/]\n"
                "  ?   Help    q   Quit (confirms if downloads active)\n",
                markup=True,
            )
            yield Button("Close", variant="primary", id="close-help")

    @on(Button.Pressed, "#close-help")
    def _close(self) -> None:
        self.dismiss()


# ── Quit confirmation modal ──────────────────────────────────────────────────
class QuitScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "dismiss_no", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="quit-box"):
            yield Static(
                "[bold red]Downloads are still active![/]\n\n"
                "Quitting will terminate them.\n",
                markup=True,
            )
            with Horizontal(id="quit-buttons"):
                yield Button("Quit anyway", variant="error", name="yes")
                yield Button("Cancel", variant="primary", name="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.name == "yes")

    def action_dismiss_no(self) -> None:
        self.dismiss(False)


# ── Twitter login modal ──────────────────────────────────────────────────────
class TwitterLoginScreen(ModalScreen[str]):
    BINDINGS = [Binding("escape", "dismiss_cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="tw-login-box"):
            yield Static(
                "[bold cyan]Twitter/X Auth[/]\n\n"
                "Paste your [bold]auth_token[/] from Chrome DevTools:\n"
                "  F12 → Application → Cookies → x.com → auth_token\n\n"
                "Or paste full cookies JSON:\n"
                '  [dim]{"auth_token":"abc…"}[/]\n',
                markup=True,
            )
            yield Label("auth_token (or JSON):")
            yield Input(placeholder="49be93f66eafc…  or  {\"auth_token\":\"…\"}", id="tw-token")
            yield Static("", id="tw-login-status")
            with Horizontal(id="tw-login-buttons"):
                yield Button("Save", variant="primary", name="save")
                yield Button("Cancel", variant="default", name="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.name == "cancel":
            self.dismiss("")
        elif event.button.name == "save":
            raw = self.query_one("#tw-token", Input).value.strip()
            if not raw:
                self.query_one("#tw-login-status", Static).update("[red]Paste a token[/]")
                return

            # Try as JSON first
            if raw.startswith("{"):
                try:
                    cookies = json.loads(raw)
                    if not isinstance(cookies, dict) or "auth_token" not in cookies:
                        raise ValueError("JSON must contain auth_token key")
                    TWITTER_COOKIES.write_text(json.dumps(cookies, indent=2))
                    self.dismiss("Cookies saved.")
                    return
                except (json.JSONDecodeError, ValueError) as e:
                    self.query_one("#tw-login-status", Static).update(f"[red]{e}[/]")
                    return

            # Treat as raw auth_token value
            if len(raw) < 20:
                self.query_one("#tw-login-status", Static).update("[red]Token too short[/]")
                return
            TWITTER_COOKIES.write_text(json.dumps({"auth_token": raw}, indent=2))
            self.dismiss("auth_token saved.")

    def action_dismiss_cancel(self) -> None:
        self.dismiss("")


class SearchFilterScreen(ModalScreen[Optional[SearchFilters]]):
    BINDINGS = [Binding("escape", "dismiss_cancel", "Cancel")]

    def __init__(self, filters: SearchFilters) -> None:
        super().__init__()
        self._filters = filters

    def compose(self) -> ComposeResult:
        with Vertical(id="filter-box"):
            yield Static(
                "[bold cyan]Search Filters[/]\n\n"
                "These filters apply globally to Porn searches.\n"
                "Leave max duration or rating at 0 for no limit.\n",
                markup=True,
            )
            with Horizontal(classes="settings-row"):
                yield Label("Min Duration (min)")
                yield Input(str(self._filters.min_duration_min), id="filter-min-duration", type="integer")
            with Horizontal(classes="settings-row"):
                yield Label("Max Duration (min)")
                yield Input(str(self._filters.max_duration_min), id="filter-max-duration", type="integer")
            with Horizontal(classes="settings-row"):
                yield Label("Min Quality")
                yield Select(
                    [("Any", "any"), ("360p+", "360"), ("480p+", "480"), ("720p+", "720"), ("1080p+", "1080")],
                    id="filter-min-quality",
                    allow_blank=False,
                    value=self._filters.min_quality,
                )
            with Horizontal(classes="settings-row"):
                yield Label("Min Rating %")
                yield Input(str(self._filters.min_rating), id="filter-min-rating", type="integer")
            yield Checkbox("HD only", value=self._filters.hd_only, id="filter-hd-only")
            with Horizontal(classes="settings-row"):
                yield Label("Search Pages")
                yield Input(str(self._filters.pages), id="filter-pages", type="integer")
            with Horizontal(classes="settings-row"):
                yield Label("Exclude Phrases")
                yield Input(self._filters.exclude_phrases, id="filter-exclude-phrases")
            yield Static("", id="filter-status")
            with Horizontal(id="filter-buttons"):
                yield Button("Save", variant="primary", name="save")
                yield Button("Cancel", variant="default", name="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.name == "cancel":
            self.dismiss(None)
            return
        try:
            min_duration = max(0, int(self.query_one("#filter-min-duration", Input).value or "0"))
            max_duration = max(0, int(self.query_one("#filter-max-duration", Input).value or "0"))
            min_rating = max(0, int(self.query_one("#filter-min-rating", Input).value or "0"))
            pages = max(1, int(self.query_one("#filter-pages", Input).value or "1"))
        except ValueError:
            self.query_one("#filter-status", Static).update("Duration, rating, and pages must be numbers.")
            return
        if max_duration and max_duration < min_duration:
            self.query_one("#filter-status", Static).update("Max duration must be greater than min duration.")
            return
        self.dismiss(SearchFilters(
            min_duration_min=min_duration,
            max_duration_min=max_duration,
            min_quality=str(self.query_one("#filter-min-quality", Select).value or "any"),  # type: ignore[arg-type]
            min_rating=min_rating,
            hd_only=self.query_one("#filter-hd-only", Checkbox).value,
            pages=pages,
            exclude_phrases=self.query_one("#filter-exclude-phrases", Input).value.strip() or DEFAULT_EXCLUDE_PHRASES,
        ))

    def action_dismiss_cancel(self) -> None:
        self.dismiss(None)


# ── Main App ──────────────────────────────────────────────────────────────────
class FetchTUI(App):
    CSS = """
    Screen { layout: vertical; }

    /* ── Tabs ── */
    TabbedContent { height: 1fr; }

    /* ── Porn tab ── */
    #porn-search-bar {
        height: auto; padding: 0 1;
        background: $panel; layout: horizontal;
    }
    #site-select  { width: 18; margin: 0 1; }
    #search-input { width: 1fr; margin: 0 1; }
    #btn-search, #btn-filters { min-width: 10; margin: 0 1; }

    #porn-filter-bar {
        height: auto; padding: 0 1;
        background: $panel-darken-1; layout: horizontal;
    }
    #porn-filter-bar Label { padding: 0 1; content-align: center middle; }
    #parallel { width: 5; }
    #result-count { padding: 0 2; color: $text-muted; }
    #filter-summary { padding: 0 2; color: $text-muted; width: 1fr; }
    #hide-dl { margin: 0 1; }

    #results-table { height: 2fr; border: solid $accent; min-height: 6; }

    #bottom-panels { height: 1fr; min-height: 5; }

    #queue-section     { width: 1fr; }
    #downloaded-section { width: 1fr; }
    #player-section { width: 1fr; }

    #queue-panel {
        height: 1fr;
        border: solid $primary;
        background: $panel-darken-2;
        overflow-y: scroll;
        padding: 0 1;
    }
    #queue-header {
        height: 1; background: $primary;
        padding: 0 1; color: $text;
    }

    #downloaded-panel {
        height: 1fr;
        border: solid $success;
        background: $panel-darken-2;
        overflow-y: scroll;
        padding: 0 1;
    }
    #downloaded-header {
        height: 1; background: $success-darken-2;
        padding: 0 1; color: $text;
    }
    .dl-entry { height: 1; }
    #player-header {
        height: 1; background: $warning-darken-2;
        padding: 0 1; color: $text;
    }
    #player-panel {
        height: 1fr;
        border: solid $warning;
        background: $panel-darken-2;
        padding: 1;
    }
    #player-thumb { height: auto; color: $text-muted; }
    #player-target { height: 1fr; }
    #player-controls { height: auto; }
    #player-controls Button { margin: 0 1 0 0; }
    #player-status { height: 1; color: $text-muted; }

    /* ── Twitter tab ── */
    #tw-search-bar {
        height: auto; padding: 0 1;
        background: $panel; layout: horizontal;
    }
    #tw-search-input { width: 1fr; margin: 0 1; }
    #tw-btn-search   { min-width: 10; margin: 0 1; }
    #tw-btn-login    { min-width: 10; margin: 0 1; }

    #tw-filter-bar {
        height: auto; padding: 0 1;
        background: $panel-darken-1; layout: horizontal;
    }
    #tw-filter-bar Label { padding: 0 1; content-align: center middle; }
    #tw-count { width: 5; }
    #tw-result-count { padding: 0 2; color: $text-muted; }
    #tw-hide-dl { margin: 0 1; }

    #tw-results-table { height: 2fr; border: solid cyan; min-height: 6; }

    #tw-bottom-panels { height: 1fr; min-height: 5; }

    #tw-queue-section     { width: 1fr; }
    #tw-downloaded-section { width: 1fr; }

    #tw-queue-panel {
        height: 1fr;
        border: solid $primary;
        background: $panel-darken-2;
        overflow-y: scroll;
        padding: 0 1;
    }
    #tw-queue-header {
        height: 1; background: $primary;
        padding: 0 1; color: $text;
    }

    #tw-downloaded-panel {
        height: 1fr;
        border: solid cyan;
        background: $panel-darken-2;
        overflow-y: scroll;
        padding: 0 1;
    }
    #tw-downloaded-header {
        height: 1; background: $surface;
        padding: 0 1; color: $text;
    }

    #tw-login-status { color: $text-muted; padding: 0 2; }

    /* ── Files tab ── */
    #files-toolbar {
        height: auto; padding: 0 1;
        background: $panel; layout: horizontal;
    }
    #files-path { width: 1fr; color: $text-muted; padding: 0 1; }
    #btn-files-refresh, #btn-files-play {
        min-width: 12; margin: 0 1;
    }
    #files-table { height: 2fr; border: solid $accent; min-height: 8; }
    #files-bottom-panels { height: 1fr; min-height: 6; }
    #files-preview-section { width: 1fr; }
    #files-preview-header {
        height: 1; background: $primary;
        padding: 0 1; color: $text;
    }
    #files-preview-panel {
        height: 1fr;
        border: solid $primary;
        background: $panel-darken-2;
        padding: 1;
    }
    #files-preview-info { height: 1fr; }

    /* ── Settings tab ── */
    #settings-scroll {
        height: 1fr;
        padding: 1 2;
    }
    .settings-section {
        height: auto;
        border: solid $accent;
        background: $panel-darken-2;
        padding: 1 2;
        margin: 0 0 1 0;
    }
    .settings-row {
        height: auto;
        layout: horizontal;
        margin: 0 0 1 0;
    }
    .settings-row Label {
        width: 24;
        padding: 0 1 0 0;
        content-align: left middle;
    }
    .settings-row Input, .settings-row Select {
        width: 1fr;
    }
    #settings-actions { height: auto; }
    #settings-actions Button { margin: 0 1 0 0; }
    #settings-status { color: $text-muted; }

    /* ── Log (shared) ── */
    #log-panel {
        height: 4;
        border: solid $surface-lighten-1;
        background: $panel-darken-3;
    }

    /* ── Modals ── */
    #help-box {
        width: 65; height: auto;
        border: solid $accent; background: $panel;
        padding: 2 4; margin: 4 8;
    }

    #quit-box {
        width: 45; height: auto;
        border: solid $error; background: $panel;
        padding: 2 4; margin: 8 16;
    }
    #quit-buttons { height: auto; }
    #quit-buttons Button { margin: 1 2; }

    #tw-login-box {
        width: 50; height: auto;
        border: solid cyan; background: $panel;
        padding: 2 4; margin: 6 12;
    }
    #tw-cookies { width: 1fr; }
    #tw-login-buttons { height: auto; }
    #tw-login-buttons Button { margin: 1 2; }

    #filter-box {
        width: 56; height: auto;
        border: solid $accent; background: $panel;
        padding: 2 4; margin: 6 12;
    }
    #filter-buttons { height: auto; }
    #filter-buttons Button { margin: 1 2 0 0; }
    #filter-status { color: $error; }
    """

    BINDINGS = [
        Binding("q", "quit_app", "Quit"),
        Binding("d", "download_selected", "Download"),
        Binding("a", "select_all", "Sel All"),
        Binding("c", "clear_selection", "Clear"),
        Binding("S", "stop_all", "Stop All"),
        Binding("x", "clean_tmp", "Clean tmp"),
        Binding("p", "play_selected", "Play"),
        Binding("space", "toggle_row", "Toggle", show=False),
        Binding("?", "help", "Help"),
    ]

    TITLE     = "Fetcher v1.0"
    SUB_TITLE = "Porn · Twitter downloader"

    def __init__(self) -> None:
        super().__init__()
        self.settings = load_settings()
        self._tmp_dir = self.settings.tmp_path
        self._plex_dir = self.settings.plex_path
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._plex_dir.mkdir(parents=True, exist_ok=True)
        # Shared state
        ids, titles = load_downloaded(self._plex_dir)
        self._downloaded:        set[str] = ids
        self._downloaded_titles: set[str] = titles
        self._all_jobs:       list[DownloadJob] = []
        self._all_job_rows:   dict[str, JobRow] = {}
        self._download_parallel: int = max(1, self.settings.parallel_downloads)

    # Porn tab state
        self._porn_results: list[Video] = []
        self._porn_dl_count: int = 0
        self._porn_sort_key: SortKey = "views"
        self._porn_sort_reverse: bool = True

        # Twitter tab state
        self._tw_results: list[Video] = []
        self._tw_dl_count: int = 0
        self._tw_logged_in: bool = TWITTER_COOKIES.exists()
        self._tw_sort_key: SortKey = "views"
        self._tw_sort_reverse: bool = True

        # Files tab state
        self._file_entries: list[FileEntry] = []
        self._file_sort_key: FileSortKey = "modified"
        self._file_sort_reverse: bool = True
        self._selected_file: Optional[FileEntry] = None

        # Player state
        self._player_proc: Optional[subprocess.Popen] = None
        self._player_video: Optional[Video] = None
        self._player_launch_token: int = 0
        self._thumb_request_id: int = 0

    # ── Layout ────────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Header()

        with TabbedContent():
            with TabPane("Porn", id="porn-tab"):
                with Horizontal(id="porn-search-bar"):
                    yield Select([(s, s) for s in SITES], value="All Sites", id="site-select")
                    yield Input(placeholder="Search (e.g. bbc creampie)…", id="search-input")
                    yield Button("Filters", variant="default", id="btn-filters")
                    yield Button("Search", variant="primary", id="btn-search")

                with Horizontal(id="porn-filter-bar"):
                    yield Label("", id="filter-summary")
                    yield Label("Parallel:")
                    yield Input("3", id="parallel", type="integer")
                    yield Checkbox("Hide dl'd", value=True, id="hide-dl")
                    yield Label("", id="result-count")

                yield DataTable(id="results-table", cursor_type="row", zebra_stripes=True)

                with Horizontal(id="bottom-panels"):
                    with Vertical(id="queue-section"):
                        yield Static("  ⬇  Queue  (0)", id="queue-header")
                        with ScrollableContainer(id="queue-panel"):
                            yield Vertical(id="queue-inner")
                    with Vertical(id="downloaded-section"):
                        yield Static("  ✓  Downloaded  (0)", id="downloaded-header")
                        with ScrollableContainer(id="downloaded-panel"):
                            yield Vertical(id="downloaded-inner")
                    with Vertical(id="player-section"):
                        yield Static("  ▶  Player", id="player-header")
                        with Vertical(id="player-panel"):
                            yield Static("", id="player-thumb")
                            yield Static("Highlight a result to preview it here.", id="player-target")
                            with Horizontal(id="player-controls"):
                                yield Button("Play stream", variant="success", id="btn-play-stream")
                                yield Button("Play file", variant="primary", id="btn-play-file")
                                yield Button("Stop", variant="error", id="btn-stop-player")
                            yield Static("", id="player-status")

            with TabPane("Twitter", id="twitter-tab"):
                with Horizontal(id="tw-search-bar"):
                    yield Input(placeholder="Search Twitter (e.g. nsfw video)…", id="tw-search-input")
                    yield Button("Search", variant="primary", id="tw-btn-search")
                    login_label = "✓ Logged in" if self._tw_logged_in else "Login"
                    yield Button(login_label, variant="success" if self._tw_logged_in else "warning", id="tw-btn-login")

                with Horizontal(id="tw-filter-bar"):
                    yield Label("Results:")
                    yield Input("40", id="tw-count", type="integer")
                    yield Checkbox("Hide dl'd", value=True, id="tw-hide-dl")
                    yield Label("", id="tw-result-count")
                    yield Label("", id="tw-login-status")

                yield DataTable(id="tw-results-table", cursor_type="row", zebra_stripes=True)

                with Horizontal(id="tw-bottom-panels"):
                    with Vertical(id="tw-queue-section"):
                        yield Static("  ⬇  Queue  (0)", id="tw-queue-header")
                        with ScrollableContainer(id="tw-queue-panel"):
                            yield Vertical(id="tw-queue-inner")
                    with Vertical(id="tw-downloaded-section"):
                        yield Static("  ✓  Downloaded  (0)", id="tw-downloaded-header")
                        with ScrollableContainer(id="tw-downloaded-panel"):
                            yield Vertical(id="tw-downloaded-inner")

            with TabPane("Files", id="files-tab"):
                with Horizontal(id="files-toolbar"):
                    yield Static("", id="files-path")
                    yield Button("Refresh", variant="primary", id="btn-files-refresh")
                    yield Button("Play file", variant="success", id="btn-files-play")
                yield DataTable(id="files-table", cursor_type="row", zebra_stripes=True)
                with Horizontal(id="files-bottom-panels"):
                    with Vertical(id="files-preview-section"):
                        yield Static("  📁  File Preview", id="files-preview-header")
                        with Vertical(id="files-preview-panel"):
                            yield Static("Select a file to preview it here.", id="files-preview-info")

            with TabPane("Settings", id="settings-tab"):
                with ScrollableContainer(id="settings-scroll"):
                    with Vertical(classes="settings-section"):
                        yield Static("[bold]Appearance[/]", markup=True)
                        with Horizontal(classes="settings-row"):
                            yield Label("Theme")
                            yield Select([("textual-dark", "textual-dark")], id="settings-theme", allow_blank=False)
                    with Vertical(classes="settings-section"):
                        yield Static("[bold]Functionality[/]", markup=True)
                        yield Checkbox("Enable Thumbnails", id="settings-thumbnails")
                        yield Checkbox("Enable Video Previews", id="settings-video-previews")
                    with Vertical(classes="settings-section"):
                        yield Static("[bold]Paths[/]", markup=True)
                        with Horizontal(classes="settings-row"):
                            yield Label("Download Directory")
                            yield Input(id="settings-plex-dir")
                        with Horizontal(classes="settings-row"):
                            yield Label("Temporary Directory")
                            yield Input(id="settings-tmp-dir")
                    with Vertical(classes="settings-section"):
                        yield Static("[bold]Downloads[/]", markup=True)
                        with Horizontal(classes="settings-row"):
                            yield Label("Parallel Downloads")
                            yield Input(id="settings-parallel", type="integer")
                    with Horizontal(id="settings-actions"):
                        yield Button("Save Settings", variant="primary", id="btn-settings-save")
                        yield Button("Reload Settings", variant="default", id="btn-settings-reload")
                    yield Static("", id="settings-status")

        with ScrollableContainer(id="log-panel"):
            yield Log(id="app-log", max_lines=300)

        yield Footer()

    def on_mount(self) -> None:
        self.theme = self.settings.theme
        self._configure_porn_table()
        self._configure_twitter_table()
        self._configure_files_table()
        self._populate_settings_controls()
        self._refresh_filter_summary()
        self._refresh_player_status()
        self._load_files()
        self.query_one("#parallel", Input).value = str(self.settings.parallel_downloads)
        self.query_one("#files-path", Static).update(str(self._plex_dir))

        self._log(f"Ready. {len(self._downloaded)} known downloads.")
        if self._tw_logged_in:
            self._log("Twitter: cookies loaded.")
        else:
            self._log("Twitter: not logged in — click Login to authenticate.")

        moved, deleted = cleanup_tmp(self._tmp_dir, self._plex_dir)
        if moved or deleted:
            self._log(f"tmp/ cleanup: {moved} mp4 moved to plex, {deleted} partials deleted.")

    def _log(self, msg: str) -> None:
        self.query_one("#app-log", Log).write_line(msg)

    def _set_settings_status(self, msg: str) -> None:
        self.query_one("#settings-status", Static).update(msg)

    def _populate_settings_controls(self) -> None:
        theme_select = self.query_one("#settings-theme", Select)
        themes = sorted(self.available_themes.keys())
        theme_select.set_options([(name, name) for name in themes])
        theme_select.value = self.settings.theme
        self.query_one("#settings-thumbnails", Checkbox).value = self.settings.thumbnails_enabled
        self.query_one("#settings-video-previews", Checkbox).value = self.settings.video_previews_enabled
        self.query_one("#settings-plex-dir", Input).value = str(self._plex_dir)
        self.query_one("#settings-tmp-dir", Input).value = str(self._tmp_dir)
        self.query_one("#settings-parallel", Input).value = str(self.settings.parallel_downloads)
        self._set_settings_status("")

    def _filter_summary_text(self) -> str:
        filters = self.settings.search_filters
        parts = [f"{filters.min_duration_min}+ min"]
        if filters.max_duration_min > 0:
            parts.append(f"max {filters.max_duration_min} min")
        if filters.min_quality != "any":
            parts.append(f"{filters.min_quality}p+")
        if filters.min_rating > 0:
            parts.append(f"rating {filters.min_rating}%+")
        if filters.hd_only:
            parts.append("HD only")
        parts.append(f"{filters.pages} page(s)")
        excludes = _split_exclude_phrases(filters.exclude_phrases)
        if excludes:
            parts.append("exclude " + ", ".join(excludes))
        return "Filters: " + " | ".join(parts)

    def _refresh_filter_summary(self) -> None:
        self.query_one("#filter-summary", Label).update(self._filter_summary_text())

    def _reload_downloaded_cache(self) -> None:
        self._downloaded, self._downloaded_titles = load_downloaded(self._plex_dir)

    def _active_tab(self) -> str:
        """Return active top-level tab id alias."""
        tc = self.query_one(TabbedContent)
        active = tc.active
        if active == "twitter-tab":
            return "twitter"
        if active == "files-tab":
            return "files"
        return "porn"

    # ── Queue headers ────────────────────────────────────────────────────────
    def _update_queue_headers(self) -> None:
        porn_active = sum(1 for j in self._all_jobs
                         if j.video.site != "Twitter" and j.status in ("downloading", "paused", "queued"))
        tw_active = sum(1 for j in self._all_jobs
                       if j.video.site == "Twitter" and j.status in ("downloading", "paused", "queued"))
        self.query_one("#queue-header", Static).update(f"  ⬇  Queue  ({porn_active})")
        self.query_one("#tw-queue-header", Static).update(f"  ⬇  Queue  ({tw_active})")

    def _find_downloaded_file(self, video: Video) -> Optional[Path]:
        norm = _normalize_title(video.title)
        if not self._plex_dir.exists():
            return None
        for path in self._plex_dir.glob("*.mp4"):
            if _normalize_title(path.stem) == norm:
                return path
        return None

    def _refresh_player_status(self) -> None:
        if self._player_proc is not None and self._player_proc.poll() is not None:
            self._player_proc = None
        video = self._player_video
        status = self.query_one("#player-status", Static)
        play_stream = self.query_one("#btn-play-stream", Button)
        play_file = self.query_one("#btn-play-file", Button)
        stop = self.query_one("#btn-stop-player", Button)

        has_video = video is not None
        local_file = self._find_downloaded_file(video) if video else None
        play_stream.disabled = not (has_video and MPV and self.settings.video_previews_enabled)
        play_file.disabled = not (local_file and MPV and self.settings.video_previews_enabled)
        stop.disabled = self._player_proc is None

        if not self.settings.video_previews_enabled:
            status.update("Video previews disabled in Settings.")
        elif not MPV:
            status.update("mpv not found in PATH.")
        elif self._player_proc is not None:
            status.update(f"Playing: {video.title[:50]}" if video else "Player active")
        elif local_file:
            status.update(f"Local file ready: {local_file.name[:50]}")
        elif video:
            status.update("Ready to stream selected result.")
        else:
            status.update("")

    def _update_thumbnail_widget(self, content: str) -> None:
        self.query_one("#player-thumb", Static).update(Text.from_markup(content))

    @work(thread=True)
    def _load_thumbnail(self, video: Video, request_id: int) -> None:
        if not self.settings.thumbnails_enabled:
            self.call_from_thread(self._update_thumbnail_widget, "[dim]Thumbnails disabled in Settings.[/]")
            return
        if not video.thumbnail_url:
            self.call_from_thread(self._update_thumbnail_widget, "[dim]No thumbnail available.[/]")
            return
        try:
            with httpx.Client(timeout=15, verify=False, headers=HEADERS) as client:
                response = client.get(video.thumbnail_url)
                response.raise_for_status()
            thumb = _render_ascii_thumbnail(response.content)
            content = f"[dim]{thumb}[/]"
        except Exception as exc:
            content = f"[dim]Thumbnail unavailable: {exc}[/]"

        def apply_thumbnail() -> None:
            if request_id != self._thumb_request_id or self._player_video is None:
                return
            if self._player_video.id != video.id:
                return
            self._update_thumbnail_widget(content)

        self.call_from_thread(apply_thumbnail)

    def _set_player_target(self, video: Optional[Video]) -> None:
        self._player_video = video
        target = self.query_one("#player-target", Static)
        thumb = self.query_one("#player-thumb", Static)
        if not video:
            self._thumb_request_id += 1
            thumb.update("")
            target.update("Highlight a result to preview it here.")
            self._refresh_player_status()
            return

        self._thumb_request_id += 1
        request_id = self._thumb_request_id
        if self.settings.thumbnails_enabled:
            thumb.update(Text.from_markup("[dim]Loading thumbnail…[/]"))
        else:
            thumb.update(Text.from_markup("[dim]Thumbnails disabled in Settings.[/]"))
        local_file = self._find_downloaded_file(video)
        location = str(local_file) if local_file else video.url
        target.update(
            Text.from_markup(
                f"[bold]{video.title}[/]\n"
                f"{video.site}  •  {video.duration_str if video.duration_s else '—'}  •  {video.views_str} views\n"
                f"[dim]{location}[/]"
            ),
        )
        self._load_thumbnail(video, request_id)
        self._refresh_player_status()

    def _launch_player(self, target: str) -> None:
        if not self.settings.video_previews_enabled:
            self._log("Video previews are disabled in Settings.")
            self._refresh_player_status()
            return
        if not MPV:
            self._log("Player unavailable: mpv not installed.")
            self._refresh_player_status()
            return
        self._stop_player(log_message=False)
        try:
            self._player_proc = subprocess.Popen(
                [MPV, "--force-window=yes", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self._log(f"▶ player started: {target[:70]}")
        except Exception as exc:
            self._player_proc = None
            self._log(f"Player error: {exc}")
        self._refresh_player_status()

    def _launch_player_urls(self, urls: list[str]) -> None:
        if not urls:
            self._log("Player error: no playable stream URL.")
            self._refresh_player_status()
            return
        if not self.settings.video_previews_enabled:
            self._log("Video previews are disabled in Settings.")
            self._refresh_player_status()
            return
        if not MPV:
            self._log("Player unavailable: mpv not installed.")
            self._refresh_player_status()
            return

        cmd = [MPV, "--force-window=yes"]
        if len(urls) >= 2:
            cmd.append(f"--audio-file={urls[1]}")
        cmd.append(urls[0])

        self._stop_player(log_message=False)
        try:
            self._player_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self._log("▶ player started")
        except Exception as exc:
            self._player_proc = None
            self._log(f"Player error: {exc}")
        self._refresh_player_status()

    @work(thread=True)
    def _play_video_stream(self, video: Video, launch_token: int) -> None:
        urls, error = resolve_stream_urls(video)

        def finish() -> None:
            if launch_token != self._player_launch_token:
                return
            if error:
                self._log(f"Player stream resolve failed: {error}")
                self._refresh_player_status()
                return
            self._launch_player_urls(urls)

        self.call_from_thread(finish)

    def _stop_player(self, log_message: bool = True) -> None:
        self._player_launch_token += 1
        if self._player_proc is not None:
            try:
                self._player_proc.terminate()
            except Exception:
                pass
            self._player_proc = None
            if log_message:
                self._log("■ player stopped")
        self._refresh_player_status()

    # ══════════════════════════════════════════════════════════════════════════
    # PORN TAB
    # ══════════════════════════════════════════════════════════════════════════

    def _header_label(self, title: str, key: str, active: str, reverse: bool) -> str:
        if key != active:
            return title
        return f"{title} {'▼' if reverse else '▲'}"

    def _sort_results(self, results: list[Video], key: SortKey, reverse: bool) -> list[Video]:
        def sort_value(video: Video):
            if key == "title":
                return video.title.lower()
            if key == "duration":
                return video.duration_s
            if key == "site":
                return video.site.lower()
            return video.views

        return sorted(results, key=sort_value, reverse=reverse)

    def _configure_porn_table(self) -> None:
        table = self.query_one("#results-table", DataTable)
        table.clear(columns=True)
        table.add_columns(
            "",
            self._header_label("Source", "site", self._porn_sort_key, self._porn_sort_reverse),
            self._header_label("Name", "title", self._porn_sort_key, self._porn_sort_reverse),
            self._header_label("Duration", "duration", self._porn_sort_key, self._porn_sort_reverse),
            self._header_label("Views", "views", self._porn_sort_key, self._porn_sort_reverse),
        )

    def _configure_twitter_table(self) -> None:
        table = self.query_one("#tw-results-table", DataTable)
        table.clear(columns=True)
        table.add_columns(
            "",
            self._header_label("Name", "title", self._tw_sort_key, self._tw_sort_reverse),
            self._header_label("Duration", "duration", self._tw_sort_key, self._tw_sort_reverse),
            self._header_label("Views", "views", self._tw_sort_key, self._tw_sort_reverse),
        )

    def _configure_files_table(self) -> None:
        table = self.query_one("#files-table", DataTable)
        table.clear(columns=True)
        table.add_columns(
            self._header_label("Name", "name", self._file_sort_key, self._file_sort_reverse),
            self._header_label("Size", "size", self._file_sort_key, self._file_sort_reverse),
            self._header_label("Modified", "modified", self._file_sort_key, self._file_sort_reverse),
            self._header_label("Path", "path", self._file_sort_key, self._file_sort_reverse),
        )

    def _sort_files(self, entries: list[FileEntry]) -> list[FileEntry]:
        def sort_value(entry: FileEntry):
            relpath = self._relative_plex_path(entry.path)
            if self._file_sort_key == "name":
                return entry.name.lower()
            if self._file_sort_key == "size":
                return entry.size
            if self._file_sort_key == "path":
                return relpath.lower()
            return entry.modified_ts

        return sorted(entries, key=sort_value, reverse=self._file_sort_reverse)

    def _relative_plex_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self._plex_dir))
        except ValueError:
            return str(path)

    def _video_matches_search_filters(self, video: Video) -> bool:
        filters = self.settings.search_filters
        if video.duration_s < filters.min_duration_s:
            return False
        if filters.max_duration_min > 0 and video.duration_s > filters.max_duration_s:
            return False
        if filters.min_rating > 0 and video.rating_percent is not None and video.rating_percent < filters.min_rating:
            return False
        if filters.hd_only and video.is_hd is False:
            return False
        if filters.min_quality != "any":
            min_quality = int(filters.min_quality)
            if video.quality_p is not None and video.quality_p < min_quality:
                return False
            if video.quality_p is None and video.is_hd is False and min_quality >= 720:
                return False
        title_lower = video.title.lower()
        for phrase in _split_exclude_phrases(filters.exclude_phrases):
            if phrase in title_lower:
                return False
        return True

    def _render_porn_results(self) -> None:
        self._configure_porn_table()
        table = self.query_one("#results-table", DataTable)
        for i, v in enumerate(self._porn_results):
            table.add_row("☐" if not v.selected else "☑", v.site, v.title[:62], v.duration_str, v.views_str, key=str(i))
        if self._porn_results:
            table.move_cursor(row=0, animate=False)
            self._set_player_target(self._porn_results[0])
        else:
            self._set_player_target(None)

    def _render_twitter_results(self) -> None:
        self._configure_twitter_table()
        table = self.query_one("#tw-results-table", DataTable)
        for i, v in enumerate(self._tw_results):
            dur = v.duration_str if v.duration_s > 0 else "—"
            table.add_row("☐" if not v.selected else "☑", v.title[:72], dur, v.views_str, key=str(i))
        if self._tw_results:
            table.move_cursor(row=0, animate=False)
            self._set_player_target(self._tw_results[0])
        else:
            self._set_player_target(None)

    def _render_files_table(self) -> None:
        self._configure_files_table()
        table = self.query_one("#files-table", DataTable)
        for i, entry in enumerate(self._file_entries):
            table.add_row(entry.name, _fmt_size(entry.size), entry.modified_str, self._relative_plex_path(entry.path), key=str(i))
        if self._file_entries:
            table.move_cursor(row=0, animate=False)
            self._set_selected_file(self._file_entries[0])
        else:
            self._set_selected_file(None)

    def _load_files(self) -> None:
        entries: list[FileEntry] = []
        if self._plex_dir.exists():
            for path in sorted(self._plex_dir.rglob("*.mp4")):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                entries.append(FileEntry(path=path, size=stat.st_size, modified_ts=stat.st_mtime))
        self._file_entries = self._sort_files(entries)
        self._render_files_table()
        self.query_one("#files-path", Static).update(str(self._plex_dir))
        self._log(f"Files: loaded {len(self._file_entries)} videos from {self._plex_dir}")

    def _set_selected_file(self, entry: Optional[FileEntry]) -> None:
        self._selected_file = entry
        info = self.query_one("#files-preview-info", Static)
        if entry is None:
            info.update("Select a file to preview it here.")
            return
        info.update(
            Text.from_markup(
                f"[bold]{entry.name}[/]\n"
                f"Size: {_fmt_size(entry.size)}\n"
                f"Modified: {entry.modified_str}\n"
                f"[dim]{entry.path}[/]"
            )
        )

    @on(Button.Pressed, "#btn-search")
    def _porn_btn(self) -> None:
        self._porn_do_search()

    @on(Button.Pressed, "#btn-filters")
    def _porn_filters_btn(self) -> None:
        self.push_screen(SearchFilterScreen(self.settings.search_filters), self._filter_callback)

    @on(Input.Submitted, "#search-input")
    def _porn_submit(self) -> None:
        self._porn_do_search()

    def _filter_callback(self, result: Optional[SearchFilters]) -> None:
        if result is None:
            return
        self.settings.search_filters = result
        save_settings(self.settings)
        self._refresh_filter_summary()
        self._log(f"Search filters updated: {self._filter_summary_text()}")

    def _porn_do_search(self) -> None:
        query = self.query_one("#search-input", Input).value.strip()
        if not query:
            return
        filters = self.settings.search_filters
        min_s = filters.min_duration_s
        max_s = filters.max_duration_s
        pages = filters.pages

        site = str(self.query_one("#site-select", Select).value)

        btn = self.query_one("#btn-search", Button)
        btn.disabled = True
        btn.label = "Searching…"
        self.query_one("#result-count", Label).update("  searching…")
        self._log(f"Searching [{site}] for '{query}' with {self._filter_summary_text()}…")
        self._porn_search_worker(query, site, min_s, max_s, pages)

    @work(thread=True)
    def _porn_search_worker(self, query: str, site: str, min_s: int, max_s: int, pages: int) -> None:
        results: list[Video] = []
        targets = list(SITE_FN.keys()) if site == "All Sites" else [site]
        for s in targets:
            found = SITE_FN[s](query, min_s, max_s, pages)
            results.extend(found)
            self.call_from_thread(self._log, f"  {s}: {len(found)} results")

        results = [video for video in results if self._video_matches_search_filters(video)]
        seen_ids: set[str] = set()
        seen_titles: set[str] = set()
        unique: list[Video] = []
        for v in results:
            norm = _normalize_title(v.title)
            if v.id in seen_ids:
                continue
            if norm and norm in seen_titles:
                continue
            seen_ids.add(v.id)
            if norm:
                seen_titles.add(norm)
            unique.append(v)
        self.call_from_thread(self._porn_populate, unique, site, query)

    def _porn_populate(self, results: list[Video], site: str, query: str) -> None:
        btn = self.query_one("#btn-search", Button)
        btn.disabled = False
        btn.label = "Search"

        if self.query_one("#hide-dl", Checkbox).value:
            results = [
                v for v in results
                if v.id not in self._downloaded
                and _normalize_title(v.title) not in self._downloaded_titles
            ]

        self._porn_results = self._sort_results(results, self._porn_sort_key, self._porn_sort_reverse)
        self._render_porn_results()

        self.query_one("#result-count", Label).update(f"  {len(results)} results")
        self._log(f"[{site}] '{query}' → {len(results)} videos")

    # ══════════════════════════════════════════════════════════════════════════
    # TWITTER TAB
    # ══════════════════════════════════════════════════════════════════════════

    @on(Button.Pressed, "#tw-btn-login")
    def _tw_login_btn(self) -> None:
        self.push_screen(TwitterLoginScreen(), self._tw_login_callback)

    def _tw_login_callback(self, result: Optional[str]) -> None:
        if result:
            self._log(f"Twitter: {result}")
            self._tw_logged_in = TWITTER_COOKIES.exists()
            btn = self.query_one("#tw-btn-login", Button)
            if self._tw_logged_in:
                btn.label = "✓ Logged in"
                btn.variant = "success"
            else:
                btn.label = "Login"
                btn.variant = "warning"

    @on(Button.Pressed, "#tw-btn-search")
    def _tw_btn(self) -> None:
        self._tw_do_search()

    @on(Input.Submitted, "#tw-search-input")
    def _tw_submit(self) -> None:
        self._tw_do_search()

    def _tw_do_search(self) -> None:
        if not self._tw_logged_in:
            self._log("Twitter: login required first.")
            self.push_screen(TwitterLoginScreen(), self._tw_login_callback)
            return

        query = self.query_one("#tw-search-input", Input).value.strip()
        if not query:
            return

        try:
            count = int(self.query_one("#tw-count", Input).value or 40)
        except ValueError:
            count = 40

        btn = self.query_one("#tw-btn-search", Button)
        btn.disabled = True
        btn.label = "Searching…"
        self.query_one("#tw-result-count", Label).update("  searching…")
        self._log(f"Twitter: searching '{query}'…")
        self._tw_search_worker(query, count)

    @work(thread=True)
    def _tw_search_worker(self, query: str, count: int) -> None:
        try:
            results = search_twitter(query, count, TWITTER_COOKIES)
        except Exception as e:
            self.call_from_thread(self._log, f"Twitter search error: {e}")
            results = []

        # Dedup
        seen_ids: set[str] = set()
        unique: list[Video] = []
        for v in results:
            if v.id not in seen_ids:
                seen_ids.add(v.id)
                unique.append(v)
        self.call_from_thread(self._tw_populate, unique, query)

    def _tw_populate(self, results: list[Video], query: str) -> None:
        btn = self.query_one("#tw-btn-search", Button)
        btn.disabled = False
        btn.label = "Search"

        if self.query_one("#tw-hide-dl", Checkbox).value:
            results = [
                v for v in results
                if v.id not in self._downloaded
                and _normalize_title(v.title) not in self._downloaded_titles
            ]

        self._tw_results = self._sort_results(results, self._tw_sort_key, self._tw_sort_reverse)
        self._render_twitter_results()

        self.query_one("#tw-result-count", Label).update(f"  {len(results)} results")
        self._log(f"Twitter: '{query}' → {len(results)} video tweets")

    # ══════════════════════════════════════════════════════════════════════════
    # SELECTION (tab-aware)
    # ══════════════════════════════════════════════════════════════════════════

    def _current_results(self) -> list[Video]:
        if self._active_tab() == "files":
            return []
        return self._tw_results if self._active_tab() == "twitter" else self._porn_results

    def _current_table_id(self) -> str:
        if self._active_tab() == "files":
            return "#files-table"
        return "#tw-results-table" if self._active_tab() == "twitter" else "#results-table"

    def action_toggle_row(self) -> None:
        if self._active_tab() == "files":
            return
        table = self.query_one(self._current_table_id(), DataTable)
        self._toggle_index(table.cursor_row, self._current_results(), self._current_table_id())

    @on(DataTable.RowSelected)
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        table_id = f"#{event.data_table.id}" if event.data_table.id else ""
        results = self._tw_results if "tw-" in table_id else self._porn_results
        try:
            self._toggle_index(int(event.row_key.value), results, table_id)  # type: ignore[arg-type]
        except (AttributeError, ValueError, TypeError):
            pass

    @on(DataTable.RowHighlighted)
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        table_id = f"#{event.data_table.id}" if event.data_table.id else ""
        if table_id == "#files-table":
            try:
                idx = int(event.row_key.value)  # type: ignore[arg-type]
            except (AttributeError, ValueError, TypeError):
                return
            if 0 <= idx < len(self._file_entries):
                self._set_selected_file(self._file_entries[idx])
            return
        results = self._tw_results if "tw-" in table_id else self._porn_results
        try:
            idx = int(event.row_key.value)  # type: ignore[arg-type]
        except (AttributeError, ValueError, TypeError):
            return
        if 0 <= idx < len(results):
            self._set_player_target(results[idx])

    @on(DataTable.HeaderSelected)
    def _header_selected(self, event: DataTable.HeaderSelected) -> None:
        table_id = f"#{event.data_table.id}" if event.data_table.id else ""
        if table_id == "#results-table":
            mapping: dict[int, SortKey] = {1: "site", 2: "title", 3: "duration", 4: "views"}
            key = mapping.get(event.column_index)
            if not key:
                return
            self._porn_sort_reverse = not self._porn_sort_reverse if self._porn_sort_key == key else False
            self._porn_sort_key = key
            self._porn_results = self._sort_results(self._porn_results, self._porn_sort_key, self._porn_sort_reverse)
            self._render_porn_results()
        elif table_id == "#tw-results-table":
            mapping = {1: "title", 2: "duration", 3: "views"}
            key = mapping.get(event.column_index)
            if not key:
                return
            self._tw_sort_reverse = not self._tw_sort_reverse if self._tw_sort_key == key else False
            self._tw_sort_key = key
            self._tw_results = self._sort_results(self._tw_results, self._tw_sort_key, self._tw_sort_reverse)
            self._render_twitter_results()
        elif table_id == "#files-table":
            mapping: dict[int, FileSortKey] = {0: "name", 1: "size", 2: "modified", 3: "path"}
            key = mapping.get(event.column_index)
            if not key:
                return
            self._file_sort_reverse = not self._file_sort_reverse if self._file_sort_key == key else False
            self._file_sort_key = key
            self._file_entries = self._sort_files(self._file_entries)
            self._render_files_table()

    def _toggle_index(self, idx: int, results: list[Video], table_id: str) -> None:
        if idx >= len(results):
            return
        v = results[idx]
        v.selected = not v.selected
        self.query_one(table_id, DataTable).update_cell_at(
            Coordinate(idx, 0), "☑" if v.selected else "☐"
        )

    def action_select_all(self) -> None:
        if self._active_tab() == "files":
            return
        results = self._current_results()
        t = self.query_one(self._current_table_id(), DataTable)
        for i, v in enumerate(results):
            v.selected = True
            t.update_cell_at(Coordinate(i, 0), "☑")

    def action_clear_selection(self) -> None:
        if self._active_tab() == "files":
            return
        results = self._current_results()
        t = self.query_one(self._current_table_id(), DataTable)
        for i, v in enumerate(results):
            v.selected = False
            t.update_cell_at(Coordinate(i, 0), "☐")

    # ══════════════════════════════════════════════════════════════════════════
    # DOWNLOAD (tab-aware)
    # ══════════════════════════════════════════════════════════════════════════

    def action_download_selected(self) -> None:
        tab = self._active_tab()
        results = self._tw_results if tab == "twitter" else self._porn_results
        queue_inner_id = "#tw-queue-inner" if tab == "twitter" else "#queue-inner"

        selected = [v for v in results if v.selected]
        if not selected:
            self._log("No videos selected.")
            return

        try:
            parallel = max(1, int(self.query_one("#parallel", Input).value or self.settings.parallel_downloads))
        except ValueError:
            parallel = self.settings.parallel_downloads

        self._download_parallel = parallel

        existing = {j.video.id for j in self._all_jobs}
        new_jobs = [DownloadJob(video=v) for v in selected if v.id not in existing]

        if not new_jobs:
            self._log("All selected already in queue.")
            return

        self._all_jobs.extend(new_jobs)
        self._log(f"Queued {len(new_jobs)} video(s)  (parallel={parallel})")

        for job in new_jobs:
            row = JobRow(job)
            self._all_job_rows[job.video.id] = row
            self.query_one(queue_inner_id, Vertical).mount(row)

        self._update_queue_headers()
        self._dispatch_downloads()

    def action_play_selected(self) -> None:
        if self._active_tab() == "files":
            if self._selected_file is None:
                self._log("No highlighted file to play.")
                return
            self._launch_player(str(self._selected_file.path))
            return
        if self._player_video is None:
            self._log("No highlighted result to play.")
            return
        self._player_launch_token += 1
        self._log(f"Resolving stream for [{self._player_video.site}] {self._player_video.title[:55]}")
        self._play_video_stream(self._player_video, self._player_launch_token)

    @on(Button.Pressed, "#btn-play-stream")
    def _play_stream_btn(self) -> None:
        self.action_play_selected()

    @on(Button.Pressed, "#btn-play-file")
    def _play_file_btn(self) -> None:
        if self._player_video is None:
            self._log("No highlighted result to play.")
            return
        local_file = self._find_downloaded_file(self._player_video)
        if not local_file:
            self._log("Selected result is not downloaded yet.")
            self._refresh_player_status()
            return
        self._launch_player(str(local_file))

    @on(Button.Pressed, "#btn-stop-player")
    def _stop_player_btn(self) -> None:
        self._stop_player()

    @on(Button.Pressed, "#btn-files-refresh")
    def _files_refresh_btn(self) -> None:
        self._load_files()

    @on(Button.Pressed, "#btn-files-play")
    def _files_play_btn(self) -> None:
        if self._selected_file is None:
            self._log("No highlighted file to play.")
            return
        self._launch_player(str(self._selected_file.path))

    @on(Button.Pressed, "#btn-settings-reload")
    def _settings_reload_btn(self) -> None:
        self.settings = load_settings()
        self._tmp_dir = self.settings.tmp_path
        self._plex_dir = self.settings.plex_path
        self._download_parallel = max(1, self.settings.parallel_downloads)
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._plex_dir.mkdir(parents=True, exist_ok=True)
        self.theme = self.settings.theme
        self._reload_downloaded_cache()
        self._populate_settings_controls()
        self._refresh_filter_summary()
        self.query_one("#parallel", Input).value = str(self.settings.parallel_downloads)
        self._load_files()
        self._refresh_player_status()
        self._set_settings_status("Settings reloaded.")
        self._log("Settings reloaded from disk.")

    @on(Button.Pressed, "#btn-settings-save")
    def _settings_save_btn(self) -> None:
        theme = str(self.query_one("#settings-theme", Select).value or self.settings.theme)
        plex_dir_raw = self.query_one("#settings-plex-dir", Input).value.strip()
        tmp_dir_raw = self.query_one("#settings-tmp-dir", Input).value.strip()
        try:
            parallel = max(1, int(self.query_one("#settings-parallel", Input).value or "1"))
        except ValueError:
            self._set_settings_status("Parallel downloads must be a number.")
            return
        if not plex_dir_raw or not tmp_dir_raw:
            self._set_settings_status("Download and temporary directories are required.")
            return

        new_settings = AppSettings(
            theme=theme,
            thumbnails_enabled=self.query_one("#settings-thumbnails", Checkbox).value,
            video_previews_enabled=self.query_one("#settings-video-previews", Checkbox).value,
            plex_dir=str(Path(plex_dir_raw).expanduser()),
            tmp_dir=str(Path(tmp_dir_raw).expanduser()),
            parallel_downloads=parallel,
            search_filters=self.settings.search_filters,
        )

        try:
            new_settings.tmp_path.mkdir(parents=True, exist_ok=True)
            new_settings.plex_path.mkdir(parents=True, exist_ok=True)
            save_settings(new_settings)
        except Exception as exc:
            self._set_settings_status(f"Failed to save settings: {exc}")
            return

        self.settings = new_settings
        self._tmp_dir = self.settings.tmp_path
        self._plex_dir = self.settings.plex_path
        self._download_parallel = max(1, self.settings.parallel_downloads)
        self.theme = self.settings.theme
        self._reload_downloaded_cache()
        self._refresh_filter_summary()
        self.query_one("#parallel", Input).value = str(self.settings.parallel_downloads)
        self._load_files()
        self._refresh_player_status()
        self._set_settings_status("Settings saved.")
        self._log(
            f"Settings saved. theme={self.settings.theme} parallel={self.settings.parallel_downloads} "
            f"plex={self._plex_dir} tmp={self._tmp_dir}"
        )

    def _dispatch_downloads(self) -> None:
        active = sum(
            1 for job in self._all_jobs
            if job.status in ("downloading", "paused") or job.worker_started
        )
        available_slots = max(0, self._download_parallel - active)
        if available_slots == 0:
            return

        queued = [job for job in self._all_jobs if job.status == "queued" and not job.worker_started]
        for job in queued[:available_slots]:
            job.worker_started = True
            row = self._all_job_rows.get(job.video.id)
            if row:
                row.refresh_display()
            self._run_job(job)
        self._update_queue_headers()

    @work(thread=True)
    def _run_job(self, job: DownloadJob) -> None:
        if job.status == "failed":
            job.worker_started = False
            self.call_from_thread(self._dispatch_downloads)
            return

        def progress_cb():
            row = self._all_job_rows.get(job.video.id)
            if row:
                self.call_from_thread(row.refresh_display)
            self.call_from_thread(self._update_queue_headers)

        def done_cb(finished: DownloadJob):
            finished.worker_started = False
            if finished.status == "done":
                self._downloaded.add(finished.video.id)
                norm = _normalize_title(finished.video.title)
                if norm:
                    self._downloaded_titles.add(norm)
            row = self._all_job_rows.get(finished.video.id)
            if row:
                self.call_from_thread(row.refresh_display)
            status = "✓" if finished.status == "done" else "✗"
            self.call_from_thread(
                self._log,
                f"{status}  [{finished.video.site}] {finished.video.title[:55]}"
            )
            self.call_from_thread(self._update_queue_headers)
            self.call_from_thread(self._dispatch_downloads)
            if finished.status == "done":
                self.call_from_thread(self._schedule_remove_done, finished)
            else:
                self.call_from_thread(self._schedule_remove_failed, finished)

        run_download(job, self._tmp_dir, self._plex_dir, progress_cb, done_cb)

    # ── Retry handler ─────────────────────────────────────────────────────────
    def on_job_row_retry_requested(self, event: JobRow.RetryRequested) -> None:
        job = event.row._job
        job.status   = "queued"
        job.progress = 0.0
        job.speed    = ""
        job.eta      = ""
        job.worker_started = False
        event.row.refresh_display()
        self._log(f"↻ retrying [{job.video.site}] {job.video.title[:55]}")
        self._update_queue_headers()
        self._dispatch_downloads()

    # ── Manual stop handler ──────────────────────────────────────────────────
    def on_job_row_stop_requested(self, event: JobRow.StopRequested) -> None:
        job = event.row._job
        job.worker_started = False
        self._log(f"■ stopped [{job.video.site}] {job.video.title[:55]}")
        self._update_queue_headers()
        self._schedule_remove_failed(job)
        self._dispatch_downloads()

    # ── Queue cleanup ─────────────────────────────────────────────────────────
    def _schedule_remove_done(self, job: DownloadJob) -> None:
        self.set_timer(2.0, lambda: self._move_to_downloaded(job))

    def _schedule_remove_failed(self, job: DownloadJob) -> None:
        self.set_timer(5.0, lambda: self._purge_row(job))

    def _purge_row(self, job: DownloadJob) -> None:
        row = self._all_job_rows.pop(job.video.id, None)
        if row:
            row.remove()
        self._all_jobs = [j for j in self._all_jobs if j.video.id != job.video.id]
        self._update_queue_headers()
        self._dispatch_downloads()

    def _move_to_downloaded(self, job: DownloadJob) -> None:
        row = self._all_job_rows.pop(job.video.id, None)
        if row:
            row.remove()
        self._all_jobs = [j for j in self._all_jobs if j.video.id != job.video.id]

        is_twitter = job.video.site == "Twitter"
        inner_id = "#tw-downloaded-inner" if is_twitter else "#downloaded-inner"
        panel_id = "#tw-downloaded-panel" if is_twitter else "#downloaded-panel"
        header_id = "#tw-downloaded-header" if is_twitter else "#downloaded-header"

        color = SITE_COLOR.get(job.video.site, "white")
        size  = _fmt_size(job.file_size) if job.file_size else ""
        inner = self.query_one(inner_id, Vertical)
        inner.mount(Static(
            f"✓ [{color}]{job.video.site}[/]  {job.video.title[:50]}"
            f"  [dim]{job.video.duration_str}  {size}[/]",
            classes="dl-entry",
        ))
        self.query_one(panel_id, ScrollableContainer).scroll_end(animate=False)

        if is_twitter:
            self._tw_dl_count += 1
            self.query_one(header_id, Static).update(f"  ✓  Downloaded  ({self._tw_dl_count})")
        else:
            self._porn_dl_count += 1
            self.query_one(header_id, Static).update(f"  ✓  Downloaded  ({self._porn_dl_count})")

        self._update_queue_headers()
        self._load_files()
        if self._player_video and self._player_video.id == job.video.id:
            self._set_player_target(job.video)
        self._dispatch_downloads()

    # ── Stop All ──────────────────────────────────────────────────────────────
    def action_stop_all(self) -> None:
        stopped = 0
        stopped_jobs: list[DownloadJob] = []
        for job in self._all_jobs:
            if job.status in ("downloading", "paused", "queued"):
                if job.proc:
                    if job.status == "paused":
                        try:
                            os.killpg(os.getpgid(job.proc.pid), signal.SIGCONT)
                        except ProcessLookupError:
                            pass
                    job.proc.terminate()
                job.status = "failed"
                job.eta    = "stopped"
                job.worker_started = False
                row = self._all_job_rows.get(job.video.id)
                if row:
                    row.refresh_display()
                stopped += 1
                stopped_jobs.append(job)
        if stopped:
            self._log(f"Stopped {stopped} download(s).")
            self._update_queue_headers()
            for job in stopped_jobs:
                self._schedule_remove_failed(job)
            self._dispatch_downloads()
        else:
            self._log("No active downloads to stop.")

    # ── Clean tmp ─────────────────────────────────────────────────────────────
    def action_clean_tmp(self) -> None:
        moved, deleted = cleanup_tmp(self._tmp_dir, self._plex_dir)
        self._log(f"tmp/ cleanup: {moved} mp4 → plex, {deleted} partials deleted.")

    # ── Quit with guard ───────────────────────────────────────────────────────
    def action_quit_app(self) -> None:
        active = any(j.status in ("downloading", "paused", "queued") for j in self._all_jobs)
        if active:
            self.push_screen(QuitScreen(), self._quit_callback)
        else:
            self._stop_player(log_message=False)
            self.exit()

    def _quit_callback(self, confirmed: Optional[bool]) -> None:
        if confirmed:
            for job in self._all_jobs:
                if job.proc and job.status in ("downloading", "paused"):
                    job.proc.terminate()
            self._stop_player(log_message=False)
            self.exit()

    # ── Help ──────────────────────────────────────────────────────────────────
    def action_help(self) -> None:
        self.push_screen(HelpScreen())


def run_tui() -> int:
    FetchTUI().run()
    return 0


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="fetcher", description="Fetcher v1.0 CLI and TUI")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("tui", help="Launch the Textual UI")

    search = sub.add_parser("search", help="Search and print matching videos")
    search.add_argument("query")
    search.add_argument("--site", choices=SITES + ["Twitter"], default="All Sites")
    search.add_argument("--pages", type=int)
    search.add_argument("--min-duration", type=int, dest="min_duration")
    search.add_argument("--max-duration", type=int, dest="max_duration")
    search.add_argument("--min-quality", choices=["any", "360", "480", "720", "1080"])
    search.add_argument("--min-rating", type=int)
    search.add_argument("--hd-only", action="store_true")
    search.add_argument("--exclude")
    search.add_argument("--count", type=int, default=40, help="Twitter result depth")
    search.add_argument("--limit", type=int)
    search.add_argument("--json", action="store_true")

    download = sub.add_parser("download", help="Download one or more explicit video targets")
    download.add_argument("targets", nargs="+")
    download.add_argument("--site", choices=["PornHub", "xHamster", "RedTube", "Eporner", "Twitter"], default="PornHub")
    download.add_argument("--output-dir")
    download.add_argument("--tmp-dir")

    batch = sub.add_parser("batch", help="Search and download the top matching results")
    batch.add_argument("query")
    batch.add_argument("--site", choices=SITES + ["Twitter"], default="All Sites")
    batch.add_argument("--pages", type=int)
    batch.add_argument("--min-duration", type=int, dest="min_duration")
    batch.add_argument("--max-duration", type=int, dest="max_duration")
    batch.add_argument("--min-quality", choices=["any", "360", "480", "720", "1080"])
    batch.add_argument("--min-rating", type=int)
    batch.add_argument("--hd-only", action="store_true")
    batch.add_argument("--exclude")
    batch.add_argument("--count", type=int, default=40, help="Twitter result depth")
    batch.add_argument("--limit", type=int, default=5)
    batch.add_argument("--parallel", type=int, default=1)
    batch.add_argument("--output-dir")
    batch.add_argument("--tmp-dir")
    batch.add_argument("--json", action="store_true", help="Print selected results as JSON before downloading")

    return parser


def cli_search(args: Namespace, settings: AppSettings) -> int:
    filters = build_filters_from_args(args, settings)
    results = search_videos(args.query, args.site, filters, twitter_count=args.count)
    results.sort(key=lambda video: video.views, reverse=True)
    if args.json:
        print(json.dumps([video_to_dict(video) for video in (results[:args.limit] if args.limit else results)], indent=2))
    else:
        print_results_table(results, limit=args.limit)
    return 0


def cli_download(args: Namespace, settings: AppSettings) -> int:
    output_dir = Path(args.output_dir).expanduser() if args.output_dir else settings.plex_path
    tmp_dir = Path(args.tmp_dir).expanduser() if args.tmp_dir else settings.tmp_path
    output_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    exit_code = 0
    for target in args.targets:
        video = build_download_video(target, args.site)
        print(f"Downloading [{video.site}] {video.url}")
        job = download_one(video, tmp_dir, output_dir)
        if job.status == "done":
            print(f"Saved to {job.filepath}")
        else:
            print(f"Failed: {job.eta}", file=sys.stderr)
            exit_code = 1
    return exit_code


def cli_batch(args: Namespace, settings: AppSettings) -> int:
    filters = build_filters_from_args(args, settings)
    results = search_videos(args.query, args.site, filters, twitter_count=args.count)
    results.sort(key=lambda video: video.views, reverse=True)
    selected = results[: max(0, args.limit)]
    if not selected:
        print("No results selected for download.", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps([video_to_dict(video) for video in selected], indent=2))

    output_dir = Path(args.output_dir).expanduser() if args.output_dir else settings.plex_path
    tmp_dir = Path(args.tmp_dir).expanduser() if args.tmp_dir else settings.tmp_path
    jobs = run_batch_download(selected, tmp_dir, output_dir, parallel=max(1, args.parallel))

    failures = [job for job in jobs if job.status != "done"]
    for job in jobs:
        prefix = "OK" if job.status == "done" else "FAIL"
        detail = str(job.filepath) if job.filepath else job.eta
        print(f"{prefix}: [{job.video.site}] {job.video.title} -> {detail}")
    return 1 if failures else 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = load_settings()

    if args.command in (None, "tui"):
        return run_tui()
    if args.command == "search":
        return cli_search(args, settings)
    if args.command == "download":
        return cli_download(args, settings)
    if args.command == "batch":
        return cli_batch(args, settings)

    parser.print_help()
    return 1


# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    raise SystemExit(main())
