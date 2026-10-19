from __future__ import annotations

import importlib.util
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fetcher_core import provider_login
from fetcher_core.age_gate import (
    PORNHUB_COOKIE_HEADER,
    age_wall_message,
    age_wall_region,
    is_age_wall_page,
    is_pornhub_age_gate_page,
    is_pornhub_sfw_page,
    pornhub_age_gate_message,
)
from fetcher_core.filters import is_known_censored_media_url
from fetcher_core.jobs import DownloadManager, JobHistoryStore, terminate_process_group
from fetcher_core.models import AppSettings, BASE_DIR, DownloadJob, DownloadPolicy, STATE_DIR, Video

logger = logging.getLogger(__name__)

DOWNLOADED_IDS = STATE_DIR / "downloaded_ids.txt"
YTDLP = BASE_DIR / "yt-dlp"
if not YTDLP.exists():
    detected_ytdlp = shutil.which("yt-dlp")
    YTDLP = Path(detected_ytdlp) if detected_ytdlp else None

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
MIN_DOWNLOAD_HEIGHT = 720
YTDLP_FORMAT = f"bestvideo[height>={MIN_DOWNLOAD_HEIGHT}]+bestaudio/best[height>={MIN_DOWNLOAD_HEIGHT}]"
YTDLP_FLEX_FORMAT = "bestvideo+bestaudio/best"

# Browser impersonation (borrowed from the sibling Porn_Fetch project): yt-dlp
# can impersonate Chrome's TLS fingerprint, which unblocks many tube sites.
_CURL_CFFI_AVAILABLE = importlib.util.find_spec("curl_cffi") is not None


def _impersonation_args() -> list[str]:
    """yt-dlp --impersonate args, but only when curl_cffi is importable."""
    return ["--impersonate", "chrome"] if _CURL_CFFI_AVAILABLE else []


def _ytdlp_format(min_height: int) -> str:
    """Build the first-attempt format selector from the effective policy height."""
    return f"bestvideo[height>={min_height}]+bestaudio/best[height>={min_height}]"
MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
PARTIAL_EXTENSIONS = {".part", ".ytdl"}
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def resolve_ytdlp_command() -> tuple[Optional[list[str]], Optional[str]]:
    """Resolve yt-dlp from a bundled binary, PATH, or the active Python env."""
    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"], None
    if YTDLP and YTDLP.exists():
        return [str(YTDLP)], None
    if shutil.which("yt-dlp"):
        return ["yt-dlp"], None
    return None, (
        "yt-dlp is not installed. Install it and make sure `yt-dlp` is on your PATH, "
        f"or place a `yt-dlp` binary in {BASE_DIR}."
    )


def ytdlp_version() -> Optional[str]:
    cmd, error = resolve_ytdlp_command()
    if error or not cmd:
        return None
    try:
        proc = subprocess.run([*cmd, "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=5)
    except Exception:
        return None
    first = (proc.stdout or proc.stderr).splitlines()[0].strip() if (proc.stdout or proc.stderr).splitlines() else ""
    return first or None


def ytdlp_version_warning() -> Optional[str]:
    version = ytdlp_version()
    if not version:
        return None
    match = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", version)
    if not match:
        return None
    try:
        released = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None
    age = (date.today() - released).days
    if age > 90:
        return f"yt-dlp {match.group(0)} is {age}+ days old; update it if downloads fail"
    return None


def _job_temp_dir(tmp_dir: Path, video: Video) -> Path:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", video.id or video.title).strip("-") or "job"
    if slug in {".", ".."}:
        slug = "job"
    return tmp_dir / slug[:80]


def _fmt_size(nbytes: int) -> str:
    if nbytes >= 1_073_741_824:
        return f"{nbytes / 1_073_741_824:.1f} GB"
    if nbytes >= 1_048_576:
        return f"{nbytes / 1_048_576:.0f} MB"
    return f"{nbytes / 1024:.0f} KB"


def cleanup_tmp(tmp_dir: Path, plex_dir: Path) -> tuple[int, int]:
    """Move completed media to plex, delete stale partials. Returns (moved, deleted)."""
    moved = deleted = 0
    if not tmp_dir.exists():
        return moved, deleted
    tmp_dir.mkdir(parents=True, exist_ok=True)
    plex_dir.mkdir(parents=True, exist_ok=True)
    for f in tmp_dir.iterdir():
        if f.suffix.lower() in MEDIA_EXTENSIONS and ".part" not in f.name:
            try:
                shutil.move(str(f), str(plex_dir / f.name))
                moved += 1
            except Exception:
                logger.warning("failed to move completed temp media %s", f, exc_info=True)
        elif f.suffix.lower() in PARTIAL_EXTENSIONS or ".part-Frag" in f.name:
            try:
                f.unlink()
                deleted += 1
            except Exception:
                logger.warning("failed to delete stale temp file %s", f, exc_info=True)
    return moved, deleted


TWITTER_COOKIES_JSON = STATE_DIR / "twitter_cookies.json"
TWITTER_COOKIES_NETSCAPE = STATE_DIR / "twitter_cookies.net.txt"
_FAR_FUTURE_EXPIRY = 2147483647


def _twitter_cookie_args() -> list[str]:
    """Prefer a Settings sign-in session, then the legacy cookie file, then Chrome."""
    try:
        provider_session = (
            provider_login.is_signed_in("Twitter")
            and provider_login.has_cookie("Twitter", "auth_token")
        )
        stored = provider_login.netscape_cookie_file("Twitter") if provider_session else None
    except Exception:
        stored = None
    if stored is not None:
        return ["--cookies", str(stored)]
    # Importing TWITTER_COOKIES from fetcher_core.providers would work (providers
    # does not import this module) but drags the HTTP stack in, so derive the
    # path from STATE_DIR directly.
    try:
        data = json.loads(TWITTER_COOKIES_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["--cookies-from-browser", "chrome"]
    if not isinstance(data, dict) or not str(data.get("auth_token") or "").strip():
        return ["--cookies-from-browser", "chrome"]
    lines = ["# Netscape HTTP Cookie File", "# https://curl.se/docs/http-cookies.html"]
    for name, value in data.items():
        token = value if isinstance(value, str) else ""
        if not name or not token:
            continue
        lines.append(f".x.com\tTRUE\t/\tTRUE\t{_FAR_FUTURE_EXPIRY}\t{name}\t{token}")
    try:
        TWITTER_COOKIES_NETSCAPE.parent.mkdir(parents=True, exist_ok=True)
        TWITTER_COOKIES_NETSCAPE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        logger.warning("failed to write Twitter cookie file", exc_info=True)
        return ["--cookies-from-browser", "chrome"]
    return ["--cookies", str(TWITTER_COOKIES_NETSCAPE)]


def _site_ytdlp_args(video: Video) -> list[str]:
    if video.site in provider_login.SIGN_IN_SITES:
        try:
            cookie_file = provider_login.netscape_cookie_file(video.site)
        except Exception:
            cookie_file = None
        if cookie_file is not None:
            return ["--cookies", str(cookie_file)]
    if video.site == "Twitter":
        return _twitter_cookie_args()
    return []


def _eporner_title_args(video: Video) -> list[str]:
    """Eporner embed pages have no title, so keep the title from search."""
    if video.site != "Eporner" and "eporner.com" not in (video.url or ""):
        return []
    title = (video.title or "").strip()
    if not title or title.casefold() == "untitled":
        return []
    literal = title.replace("%", "%%").replace(":", r"\:")
    return ["--parse-metadata", f"{literal}:%(title)s"]


def _provider_download_url(video: Video) -> str:
    if video.site == "Eporner" or "eporner.com" in video.url:
        vid = video.id
        if not vid or vid.startswith("http"):
            match = re.search(r"(?:video-|embed/|hd-porn/)([a-zA-Z0-9]+)", video.url)
            vid = match.group(1) if match else ""
        if vid:
            return f"https://www.eporner.com/embed/{vid}/"
    return video.url


def _safe_media_stem(video: Video) -> str:
    raw = video.title or video.id or "video"
    stem = re.sub(r"[^a-zA-Z0-9._ -]+", "", raw).strip(" ._-")
    stem = re.sub(r"\s+", " ", stem)
    if video.id and video.id not in stem:
        stem = f"{stem} [{video.id}]" if stem else video.id
    return stem[:180] or "video"


def _latest_media_file(job_tmp_dir: Path) -> Optional[Path]:
    candidates = [path for path in job_tmp_dir.rglob("*") if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS and ".part" not in path.name]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def _job_temp_bytes(job_tmp_dir: Path) -> int:
    total = 0
    if not job_tmp_dir.exists():
        return 0
    for path in job_tmp_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() in MEDIA_EXTENSIONS | PARTIAL_EXTENSIONS or ".part" in path.name:
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return total


def _probe_video_height(path: Path) -> Optional[int]:
    if not FFPROBE:
        return None
    proc = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=height", "-of", "json", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if proc.returncode != 0:
        return None
    try:
        streams = json.loads(proc.stdout or "{}").get("streams", [])
        height = streams[0].get("height") if streams else None
        return int(height) if height else None
    except Exception:
        return None


def _probe_video_duration(path: Path) -> Optional[float]:
    if not FFPROBE:
        return None
    proc = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if proc.returncode != 0:
        return None
    try:
        duration = json.loads(proc.stdout or "{}").get("format", {}).get("duration")
        return float(duration) if duration else None
    except Exception:
        return None


def _probe_remote_media_height(url: str, video: Video) -> Optional[int]:
    """Inspect a resolved media URL's height without transferring the whole file.

    The direct/resolved URLs can lack quality metadata (for example a bare HLS
    master URL), so the floor is checked against the stream itself before a
    fallback transfer starts. An uninspectable URL returns None and the final
    local ffprobe check still applies.
    """
    if not FFPROBE or not url or not url.startswith(("http://", "https://")):
        return None
    cmd = [
        FFPROBE, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=height", "-of", "json",
        "-user_agent", HEADERS["User-Agent"],
        "-referer", _provider_download_url(video),
        "-probesize", "5000000", "-analyzeduration", "5000000",
    ]
    cookie_header = _session_cookie_header(video)
    if cookie_header:
        cmd.extend(["-headers", f"Cookie: {cookie_header}\r\n"])
    cmd.append(url)
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, check=False, timeout=20,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        streams = json.loads(proc.stdout or "{}").get("streams", [])
        height = streams[0].get("height") if streams else None
        return int(height) if height else None
    except Exception:
        return None


def _resolved_stream_below_floor(url: str, video: Video, min_height: int) -> Optional[str]:
    """Return a failure message when a resolved stream is known to be too short."""
    if min_height <= 0:
        return None
    height = _probe_remote_media_height(url, video)
    if height is None or height >= min_height:
        return None
    return f"resolved stream is {height}p; Fetcher requires {min_height}p+"


def _download_patterns() -> dict[str, re.Pattern[str]]:
    return {
        "percent": re.compile(r"\[download\].*?([\d.]+)%"),
        "speed": re.compile(r"\bat\s+([\d.]+\s*[^\s]+)"),
        "eta": re.compile(r"\bETA\s+(\S+)"),
        "done": re.compile(r"\[download\]\s+100(?:\.0+)?%\s+of"),
        "destination": re.compile(r"\[download\] Destination: (.+)"),
        "merge": re.compile(r'\[Merger\] Merging formats into "(.+)"'),
        "printed": re.compile(r"__FETCHER_DEST__:(.+)"),
        "ffmpeg_time": re.compile(r"time=(\S+)"),
        "aria": re.compile(
            r"\[#[^\s]+\s+[^\s]+\((\d+)%\).*?\bDL:([^\s\]]+)"
            r"(?:\s+ETA:([^\s\]]+))?"
        ),
    }


def _handle_download_line(
    job: DownloadJob,
    line: str,
    dest: Optional[Path],
    progress_dir: Optional[Path],
    progress_cb,
    patterns: dict[str, re.Pattern[str]],
) -> Optional[Path]:
    if match := patterns["aria"].search(line):
        job.progress = float(match.group(1))
        job.speed = match.group(2) + "/s"
        job.eta = match.group(3) or ""
        if progress_dir:
            job.downloaded_bytes = _job_temp_bytes(progress_dir)
        progress_cb()
    elif patterns["done"].search(line):
        job.progress = 100.0
        job.speed = ""
        job.eta = ""
        if progress_dir:
            job.downloaded_bytes = _job_temp_bytes(progress_dir)
        progress_cb()
    elif match := patterns["percent"].search(line):
        job.progress = float(match.group(1))
        if speed := patterns["speed"].search(line):
            job.speed = speed.group(1)
        if eta := patterns["eta"].search(line):
            job.eta = eta.group(1)
        if progress_dir:
            job.downloaded_bytes = _job_temp_bytes(progress_dir)
        progress_cb()
    elif match := patterns["destination"].search(line):
        dest = Path(match.group(1).strip())
    elif match := patterns["merge"].search(line):
        dest = Path(match.group(1).strip().strip('"'))
    elif match := patterns["printed"].search(line):
        dest = Path(match.group(1).strip())
    elif match := patterns["ffmpeg_time"].search(line):
        job.eta = f"ffmpeg {match.group(1)}"
        if progress_dir:
            job.downloaded_bytes = _job_temp_bytes(progress_dir)
        progress_cb()
    return dest


def _run_download_process(
    job: DownloadJob,
    cmd: list[str],
    progress_cb,
    *,
    expected_dest: Optional[Path] = None,
) -> tuple[int, Optional[Path], deque[str]]:
    if job.status in {"cancelled", "interrupted"}:
        return -15, None, deque([job.status], maxlen=12)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except OSError as exc:
        return 1, None, deque([str(exc)], maxlen=12)

    job.proc = proc
    try:
        progress_cb()
        if job.status in {"cancelled", "interrupted"}:
            terminate_process_group(proc)

        dest = expected_dest
        progress_dir = expected_dest.parent if expected_dest else None
        if progress_dir is None and "-o" in cmd:
            o_index = cmd.index("-o")
            if o_index + 1 < len(cmd) and not cmd[o_index + 1].startswith("-"):
                progress_dir = Path(cmd[o_index + 1]).parent

        patterns = _download_patterns()
        tail: deque[str] = deque(maxlen=12)
        for raw_line in proc.stdout or []:
            line = raw_line.rstrip()
            if line:
                tail.append(line)
            dest = _handle_download_line(
                job,
                line,
                dest,
                progress_dir,
                progress_cb,
                patterns,
            )

        proc.wait()
        return proc.returncode, dest, tail
    except BaseException:
        terminate_process_group(proc)
        raise
    finally:
        job.proc = None
        if proc.stdout is not None:
            proc.stdout.close()


def _failure_from_tail(tail: deque[str], returncode: int) -> str:
    failure_lines = [line for line in reversed(tail) if line and not line.startswith("[download]")]
    return failure_lines[0] if failure_lines else f"exit {returncode}"


def _best_download_failure(failures: list[str], fallback: str) -> str:
    if not failures:
        return fallback
    # A quality-floor or age-wall rejection is more actionable than a later
    # generic extractor error, and explains why no transfer was attempted.
    for failure in failures:
        lowered = failure.lower()
        if "fetcher requires" in lowered or "age-verification" in lowered or "age verification" in lowered:
            return failure
    generic_markers = ("unsupported url", "forcing generic information extractor")
    if fallback and not any(marker in fallback.lower() for marker in generic_markers):
        return fallback
    for failure in failures:
        if failure and not any(marker in failure.lower() for marker in generic_markers):
            return failure
    return fallback or failures[-1]


def _finalize_downloaded_file(job: DownloadJob, dest: Optional[Path], job_tmp_dir: Path, plex_dir: Path, organize_by_site: bool, policy: DownloadPolicy) -> bool:
    if not (dest and dest.exists()):
        dest = _latest_media_file(job_tmp_dir)
    if not (dest and dest.exists()):
        job.status = "failed"; job.eta = "no output file found"
        return False
    height = _probe_video_height(dest)
    if height is not None and height < policy.min_height:
        try:
            dest.unlink()
        except OSError:
            logger.warning("failed to delete low-quality download %s", dest, exc_info=True)
        job.status = "failed"; job.progress = 0.0; job.eta = f"downloaded format is {height}p; Fetcher requires {policy.min_height}p+"
        return False
    if job.video.site == "Eporner" and job.video.duration_s >= policy.preview_min_seconds:
        duration = _probe_video_duration(dest)
        if duration is not None and duration < job.video.duration_s * policy.preview_ratio:
            try:
                dest.unlink()
            except OSError:
                logger.warning("failed to delete Eporner preview download %s", dest, exc_info=True)
            job.status = "failed"; job.progress = 0.0; job.eta = f"Eporner served a {duration:.0f}s stream for a {job.video.duration_s}s video (blurred preview)"
            return False
    final_dir = plex_dir / re.sub(r"[^a-zA-Z0-9._-]+", "-", job.video.site).strip("-") if organize_by_site else plex_dir
    final_dir.mkdir(parents=True, exist_ok=True)
    target = final_dir / dest.name
    try:
        shutil.move(str(dest), str(target))
        job.filepath = target; job.file_size = target.stat().st_size; job.status = "done"; job.progress = 100.0
        quality_note = f" ({height}p)" if height else ""
        job.eta = f"saved{quality_note}"
        DOWNLOADED_IDS.parent.mkdir(parents=True, exist_ok=True)
        with open(DOWNLOADED_IDS, "a") as f:
            f.write(job.video.id + "\n")
        return True
    except Exception as exc:
        job.status = "failed"; job.eta = str(exc)
        return False


def _build_ytdlp_download_cmd(ytdlp_cmd: list[str], job: DownloadJob, job_tmp_dir: Path, filename_template: str, format_selector: str, concurrent_fragments: int, *, force_generic: bool = False) -> list[str]:
    # --print implies quiet mode, which suppresses the [download] lines the UI parses.
    cmd = [*ytdlp_cmd, "-o", str(job_tmp_dir / (filename_template.strip() or "%(title)s.%(ext)s")), "-f", format_selector, "--merge-output-format", "mkv", "--check-formats", "--no-playlist", "--newline", "--progress", "--no-warnings", "--retries", "3", "--fragment-retries", "5", "--continue", "--part", "--concurrent-fragments", str(max(1, concurrent_fragments)), "--print", "after_move:__FETCHER_DEST__:%(filepath)s"]
    cmd.extend(["--socket-timeout", "20"])
    cmd.extend(_impersonation_args())
    aria2 = shutil.which("aria2c")
    if not aria2:
        # Installed entry points may run without the environment's bin on PATH.
        sibling = Path(sys.executable).parent / ("aria2c.exe" if sys.platform == "win32" else "aria2c")
        if sibling.is_file():
            aria2 = str(sibling)
    if aria2 and concurrent_fragments > 1:
        connections = min(16, max(1, concurrent_fragments))
        cmd.extend([
            "--downloader", f"http:{aria2}",
            "--downloader-args", f"aria2c:-x{connections} -s{connections} -k1M --file-allocation=none --summary-interval=1",
        ])
    if force_generic:
        cmd.append("--force-generic-extractor")
    cmd.extend(_eporner_title_args(job.video))
    cmd.extend(_site_ytdlp_args(job.video))
    cmd.append(_provider_download_url(job.video))
    return cmd


def _is_pornhub_phantomjs_error(error: str) -> bool:
    lowered = error.lower()
    return "pornhub" in lowered and "phantomjs" in lowered


_BROWSER_FALLBACK_MARKERS = (
    "phantomjs", "no video formats", "unable to extract", "unsupported url",
    # A height-constrained selector that cannot be satisfied is the normal
    # signal that the site hides its HD formats behind runtime player data.
    "requested format is not available", "format is not available",
    "drm", "hls", "m3u8",
)


def _needs_browser_fallback(video: Video, error: str) -> bool:
    """Any site may hide its player data behind JavaScript, not just PornHub."""
    return any(marker in error.lower() for marker in _BROWSER_FALLBACK_MARKERS)


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


def _pornhub_media_urls_from_html(text: str) -> list[tuple[int, str]]:
    match = re.search(r"(?:var\s+)?flashvars_\d+\s*=\s*", text)
    if not match:
        return []
    try:
        flashvars, _ = json.JSONDecoder().raw_decode(text[match.end():].lstrip())
    except Exception:
        # Truncated/garbled player data can raise more than ValueError.
        return []
    if not isinstance(flashvars, dict):
        return []
    media_definitions = flashvars.get("mediaDefinitions", [])
    return _media_urls_from_definitions(media_definitions)


def _media_urls_from_definitions(media_definitions: object) -> list[tuple[int, str]]:
    urls: list[tuple[int, str]] = []
    seen: set[str] = set()
    if not isinstance(media_definitions, list):
        return []
    for definition in media_definitions:
        if not isinstance(definition, dict):
            continue
        video_url = definition.get("videoUrl")
        if not isinstance(video_url, str) or not video_url.startswith(("https://", "http://")):
            continue
        if "/video/get_media" in video_url or video_url in seen or is_known_censored_media_url(video_url):
            continue
        quality = _parse_quality(definition.get("quality")) or _parse_quality(definition.get("height")) or 0
        urls.append((quality, video_url)); seen.add(video_url)
    return sorted(urls, key=lambda item: (item[0], ".mp4" in item[1]), reverse=True)


# Read evaluated player state in a dedicated browser, including media API calls
# made with that browser's cookies. No code from the extension is bundled.
_BROWSER_MEDIA_SCRIPT = r"""
const done = arguments[arguments.length - 1];
(async () => {
    const definitions = [];
    for (const key of Object.keys(window)) {
        if (/^flashvars_\d+$/.test(key)) {
            definitions.push(...(window[key]?.mediaDefinitions || []));
        }
    }
    const redtube = window.page_params?.generalVideoConfig?.mainRoll?.mediaDefinition;
    if (Array.isArray(redtube)) definitions.push(...redtube);
    const h264 = window.initials?.xplayerSettings?.sources?.standard?.h264;
    if (Array.isArray(h264)) {
        for (const source of h264) definitions.push({videoUrl: source.url, quality: source.quality});
    }
    const hls = window.xplayer?.core?.sourceController?.chromecastSource;
    if (typeof hls === 'string') definitions.push({videoUrl: hls});
    for (const el of document.querySelectorAll('video, video source, source')) {
        const src = el.currentSrc || (el.getAttribute('src') ? el.src : null);
        if (src && src !== location.href && /^https?:/.test(src)) definitions.push({videoUrl: src});
    }
    // Bounded, defensive scan of the raw page for stream URLs (works for
    // arbitrary hosts, e.g. SearXNG-resolved tube sites).
    try {
        const html = document.documentElement.outerHTML;
        const scanned = html.match(/https?:[^"'\\\s]+\.m3u8[^"'\\\s]*/gi) || [];
        const mp4s = html.match(/https?:[^"'\\\s]+\.mp4[^"'\\\s]*/gi) || [];
        for (const url of scanned.slice(0, 20)) definitions.push({videoUrl: url});
        for (const url of mp4s.slice(0, 20)) definitions.push({videoUrl: url});
    } catch (_) {}
    const expanded = await Promise.all(definitions.map(async definition => {
        const url = definition.videoUrl;
        if (typeof url !== 'string') return [];
        if (/\/video\/get_media/.test(url) || definition.remote ||
            (['mp4', 'hls'].includes(definition.format) && !definition.quality && !/\.(mp4|m3u8)(\?|$)/i.test(url))) {
            try {
                const response = await fetch(url, {credentials:'include', signal:AbortSignal.timeout(8000)});
                return response.ok ? await response.json() : [];
            } catch (_) { return []; }
        }
        return [definition];
    }));
    return expanded.flat().filter(item => item && typeof item === 'object');
})().then(done, () => done([]));
"""


def _is_pornhub_download(video: Video) -> bool:
    return video.site == "PornHub" or "pornhub" in urlparse(video.url or "").netloc.lower()


def _session_cookie_header(video: Video) -> str:
    """Combine PornHub's age-gate cookies with a stored provider session."""
    parts: list[str] = []
    if _is_pornhub_download(video):
        parts.append(PORNHUB_COOKIE_HEADER)
    if video.site in provider_login.SIGN_IN_SITES:
        try:
            stored = provider_login.cookie_header(video.site)
        except Exception:
            stored = ""
        if stored:
            parts.append(stored)
    return "; ".join(parts)


def _accept_pornhub_age_prompt(driver) -> None:
    """Click PornHub's 18+ Enter control when the disclaimer is rendered."""
    selectors = (
        "#js-ageDisclaimerModal .js-closeAgeModal",
        "#js-ageDisclaimerModal .buttonOver18",
        "#js-ageDisclaimerModal .js-av-cta.buttonOver18",
    )
    for selector in selectors:
        try:
            elements = driver.find_elements("css selector", selector)
        except Exception:
            continue
        for element in elements:
            try:
                element.click()
                return
            except Exception:
                continue


def _prepare_pornhub_browser_session(driver) -> None:
    """Seed the accepted 18+ cookies before loading a PornHub player page."""
    try:
        driver.get("https://www.pornhub.com/")
    except Exception:
        return
    for name, value in (
        ("accessAgeDisclaimerPH", "2"),
        ("age_verified", "1"),
        ("accessPH", "1"),
        ("platform", "pc"),
    ):
        try:
            driver.add_cookie({
                "name": name, "value": value, "domain": ".pornhub.com", "path": "/",
            })
        except Exception:
            continue
    _accept_pornhub_age_prompt(driver)


def _browser_age_wall_error(video: Video, driver, requested_url: str) -> Optional[str]:
    """Explain a browser session that landed on an age wall/homepage instead."""
    try:
        page = driver.page_source or ""
    except Exception:
        page = ""
    if is_age_wall_page(page):
        return age_wall_message(video.site, age_wall_region(page))
    try:
        final_url = str(driver.current_url or "")
    except Exception:
        final_url = ""
    if not final_url:
        return None
    requested = urlparse(requested_url)
    final = urlparse(final_url)
    requested_host = (requested.hostname or "").lower()
    final_host = (final.hostname or "").lower()
    same_site = (
        requested_host == final_host
        or requested_host.endswith("." + final_host)
        or final_host.endswith("." + requested_host)
    )
    requested_path = (requested.path or "/").rstrip("/") or "/"
    final_path = (final.path or "/").rstrip("/") or "/"
    if same_site and requested_path != "/" and final_path == "/":
        return age_wall_message(video.site, age_wall_region(page))
    return None


def _select_browser_media_url(
    video: Video,
    media_urls: list[tuple[int, str]],
    min_height: int,
) -> tuple[list[str], Optional[str]]:
    """Pick a runtime stream without ever preferring a known sub-floor quality."""
    if min_height <= 0:
        return [media_urls[0][1]], None
    eligible = [url for quality, url in media_urls if quality >= min_height]
    if eligible:
        return [eligible[0]], None
    # Some sites expose a single stream without any quality metadata (for
    # example a bare HLS master URL). Keep it as a last resort; the final
    # ffprobe check still rejects it if it turns out to be below the floor.
    unknown = [url for quality, url in media_urls if quality <= 0]
    if unknown:
        return [unknown[0]], None
    best_height = max(quality for quality, _ in media_urls)
    return [], (
        f"{video.site} exposes only {best_height}p media in the browser player; "
        f"Fetcher requires {min_height}p+"
    )


def _resolve_browser_media_urls(video: Video, min_height: int = 0) -> tuple[list[str], Optional[str]]:
    """Resolve runtime player data when a site's static extractor fails.

    Works for any host, not just the originally supported tube sites. When a
    minimum height is configured, known lower-quality player sources are never
    selected ahead of an eligible one.
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except Exception as exc:
        return [], f"{video.site} browser fallback unavailable: {exc}"
    opts = Options(); opts.add_argument("--headless=new"); opts.add_argument("--no-sandbox"); opts.add_argument("--disable-dev-shm-usage"); opts.add_argument("--disable-gpu")
    driver = None
    try:
        driver = webdriver.Chrome(options=opts)
        driver.set_page_load_timeout(30)
        driver.set_script_timeout(12)
        pornhub = _is_pornhub_download(video)
        requested_url = _provider_download_url(video)
        stored_cookies = (
            provider_login.load_cookies(video.site)
            if video.site in provider_login.SIGN_IN_SITES
            else []
        )
        if pornhub:
            _prepare_pornhub_browser_session(driver)
            if stored_cookies:
                provider_login.apply_to_selenium(driver, video.site)
        elif stored_cookies:
            driver.get(provider_login.SITE_HOME_URLS[video.site])
            provider_login.apply_to_selenium(driver, video.site)
        driver.get(requested_url)
        if pornhub:
            _accept_pornhub_age_prompt(driver)
        else:
            gate_error = _browser_age_wall_error(video, driver, requested_url)
            if gate_error:
                return [], gate_error
        media_urls: list[tuple[int, str]] = []
        deadline = time.monotonic() + 15
        for _ in range(12):
            media_urls = _media_urls_from_definitions(driver.execute_async_script(_BROWSER_MEDIA_SCRIPT))
            if not media_urls:
                media_urls = _pornhub_media_urls_from_html(driver.page_source)
            if media_urls:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        if not media_urls:
            if pornhub:
                try:
                    page = driver.page_source or ""
                except Exception:
                    page = ""
                if is_pornhub_age_gate_page(page) or is_pornhub_sfw_page(page):
                    return [], pornhub_age_gate_message()
            return [], f"{video.site} browser fallback found no media URLs; check site availability in Chrome"
        return _select_browser_media_url(video, media_urls, min_height)
    except Exception as exc:
        return [], f"{video.site} browser fallback failed: {exc}"
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def _resolve_pornhub_browser_urls(video: Video, min_height: int = 0) -> tuple[list[str], Optional[str]]:
    """Backward-compatible dispatcher for :func:`_resolve_browser_media_urls`.

    Kept because existing tests and ``fetchtui`` import/patch this name; it
    delegates at call time so patching either name takes effect.
    """
    return _resolve_browser_media_urls(video, min_height=min_height)


def _download_attempts(min_height: int) -> list[tuple[str, str, bool]]:
    """Return yt-dlp attempts that never select a known sub-floor quality."""
    if min_height > 0:
        selector = _ytdlp_format(min_height)
        return [
            (f"yt-dlp {min_height}p+", selector, False),
            (f"yt-dlp {min_height}p+ generic", selector, True),
        ]
    return [
        ("yt-dlp flexible", YTDLP_FLEX_FORMAT, False),
        ("yt-dlp generic", YTDLP_FLEX_FORMAT, True),
    ]


def _resolve_direct_download_urls(
    video: Video,
    ytdlp_cmd: list[str],
    *,
    force_generic: bool,
    min_height: int = 0,
) -> tuple[list[str], Optional[str]]:
    selector = _ytdlp_format(min_height) if min_height > 0 else YTDLP_FLEX_FORMAT
    cmd = [*ytdlp_cmd, "-g", "--no-playlist", "-f", selector]
    cmd.extend(_impersonation_args())
    if force_generic:
        cmd.append("--force-generic-extractor")
    cmd.extend(_site_ytdlp_args(video)); cmd.append(_provider_download_url(video))
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=60)
    except Exception as exc:
        return [], str(exc)
    urls = [line.strip() for line in proc.stdout.splitlines() if line.strip().startswith("http") and not is_known_censored_media_url(line.strip())]
    if proc.returncode != 0:
        error = (proc.stderr or proc.stdout).strip() or f"yt-dlp exit {proc.returncode}"
        if not force_generic and _needs_browser_fallback(video, error):
            browser_urls, browser_error = _resolve_pornhub_browser_urls(video, min_height=min_height)
            if browser_urls:
                return browser_urls, None
            return urls, browser_error or error
        return urls, error
    return urls, None


def _run_ffmpeg_fallback(
    job: DownloadJob,
    urls: list[str],
    job_tmp_dir: Path,
    progress_cb,
    min_height: int = 0,
) -> tuple[int, Optional[Path], deque[str]]:
    if not FFMPEG:
        return 1, None, deque(["ffmpeg is not installed"], maxlen=12)
    if not urls:
        return 1, None, deque(["no direct media URLs found"], maxlen=12)
    below_floor = _resolved_stream_below_floor(urls[0], job.video, min_height)
    if below_floor:
        return 1, None, deque([below_floor], maxlen=12)
    output = job_tmp_dir / f"{_safe_media_stem(job.video)}.mkv"
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "warning"]
    referer = _provider_download_url(job.video)
    cookie_header = _session_cookie_header(job.video)
    cookie_args = ["-headers", f"Cookie: {cookie_header}\r\n"] if cookie_header else []
    for url in urls[:2]:
        url = url.split("#__youtubedl_smuggle=", 1)[0]
        cmd.extend(["-user_agent", HEADERS["User-Agent"], "-referer", referer])
        cmd.extend(cookie_args)
        cmd.extend(["-i", url])
    if len(urls) >= 2:
        cmd.extend(["-map", "0:v:0", "-map", "1:a:0?"])
    cmd.extend(["-c", "copy", str(output)])
    job.eta = "ffmpeg fallback"
    return _run_download_process(job, cmd, progress_cb, expected_dest=output)


def run_download(job: DownloadJob, tmp_dir: Path, plex_dir: Path, progress_cb, done_cb, filename_template: str = "%(title)s.%(ext)s", organize_by_site: bool = False, concurrent_fragments: int = 1, policy: DownloadPolicy | None = None) -> None:
    """Download with layered yt-dlp/generic/ffmpeg fallbacks, then verify quality."""
    policy = policy or DownloadPolicy()
    if job.status in {"cancelled", "interrupted"}:
        done_cb(job)
        return
    job.status = "downloading"
    job_tmp_dir = _job_temp_dir(tmp_dir, job.video); job_tmp_dir.mkdir(parents=True, exist_ok=True)
    ytdlp_cmd, ytdlp_error = resolve_ytdlp_command()
    if ytdlp_error or not ytdlp_cmd:
        job.status = "failed"; job.eta = ytdlp_error or "yt-dlp is unavailable"; done_cb(job); return
    attempts = _download_attempts(policy.min_height)
    last_failure = ""; failures: list[str] = []
    browser_attempted = False
    for label, format_selector, force_generic in attempts:
        job.eta = label; progress_cb()
        if job.status in {"cancelled", "interrupted"}:
            done_cb(job)
            return
        cmd = _build_ytdlp_download_cmd(ytdlp_cmd, job, job_tmp_dir, filename_template, format_selector, concurrent_fragments, force_generic=force_generic)
        returncode, dest, tail = _run_download_process(job, cmd, progress_cb)
        if job.status in {"cancelled", "interrupted"}:
            done_cb(job)
            return
        if returncode == 0:
            # A completed transfer that fails validation or placement must not be
            # downloaded again through weaker selectors or another backend.
            _finalize_downloaded_file(job, dest, job_tmp_dir, plex_dir, organize_by_site, policy)
            break
        last_failure = job.eta if job.status == "failed" and job.eta else _failure_from_tail(tail, returncode)
        if last_failure:
            failures.append(last_failure)
        if not browser_attempted and _needs_browser_fallback(job.video, last_failure):
            browser_attempted = True
            job.eta = "Reading browser player data"; progress_cb()
            if job.status in {"cancelled", "interrupted"}:
                done_cb(job)
                return
            urls, browser_error = _resolve_pornhub_browser_urls(job.video, min_height=policy.min_height)
            if job.status in {"cancelled", "interrupted"}:
                done_cb(job)
                return
            if urls:
                below_floor = _resolved_stream_below_floor(urls[0], job.video, policy.min_height)
                if job.status in {"cancelled", "interrupted"}:
                    # The bounded ffprobe pre-check can span a user Stop request.
                    done_cb(job)
                    return
                if below_floor:
                    browser_error = below_floor
                else:
                    job.status = "downloading"
                    direct_cmd = _build_ytdlp_download_cmd(
                        ytdlp_cmd, job, job_tmp_dir, filename_template,
                        YTDLP_FLEX_FORMAT, concurrent_fragments,
                    )
                    literal_title = (job.video.title or job.video.id).replace("%", "%%").replace(":", r"\:")
                    direct_cmd[-1:] = [
                        "--referer", _provider_download_url(job.video),
                        "--user-agent", HEADERS["User-Agent"],
                        *(["--add-header", f"Cookie: {PORNHUB_COOKIE_HEADER}"] if _is_pornhub_download(job.video) else []),
                        "--parse-metadata", f"{literal_title}:%(title)s",
                        urls[0],
                    ]
                    returncode, dest, tail = _run_download_process(job, direct_cmd, progress_cb)
                    if job.status in {"cancelled", "interrupted"}:
                        done_cb(job)
                        return
                    if returncode == 0:
                        _finalize_downloaded_file(job, dest, job_tmp_dir, plex_dir, organize_by_site, policy)
                        break
                    browser_error = _failure_from_tail(tail, returncode)
            if browser_error:
                failures.append(browser_error)
        job.status = "downloading"; job.progress = 0.0; job.speed = ""
    else:
        for force_generic in ((True,) if browser_attempted else (False, True)):
            urls, error = _resolve_direct_download_urls(
                job.video, ytdlp_cmd, force_generic=force_generic, min_height=policy.min_height,
            )
            if job.status in {"cancelled", "interrupted"}:
                done_cb(job)
                return
            if not urls:
                last_failure = error or last_failure
                if error:
                    failures.append(error)
                continue
            returncode, dest, tail = _run_ffmpeg_fallback(
                job, urls, job_tmp_dir, progress_cb, min_height=policy.min_height,
            )
            if job.status in {"cancelled", "interrupted"}:
                done_cb(job)
                return
            if returncode == 0:
                _finalize_downloaded_file(job, dest, job_tmp_dir, plex_dir, organize_by_site, policy)
                break
            last_failure = job.eta if job.status == "failed" and job.eta else _failure_from_tail(tail, returncode)
            if last_failure:
                failures.append(last_failure)
            job.status = "downloading"; job.progress = 0.0; job.speed = ""
        else:
            job.status = "failed"; job.eta = _best_download_failure(failures, last_failure or "all download strategies failed")
    if job.status == "failed":
        warning = ytdlp_version_warning()
        if warning:
            job.eta = f"{job.eta}; {warning}" if job.eta else warning
    try:
        if job_tmp_dir.exists() and not any(job_tmp_dir.iterdir()):
            job_tmp_dir.rmdir()
    except OSError:
        pass
    done_cb(job)


def create_download_manager(settings: AppSettings, tmp_dir: Path, output_dir: Path, *, history_store: Optional[JobHistoryStore] = None) -> DownloadManager:
    """Adapt the existing downloader and callbacks to the shared job manager."""
    policy = settings.download_policy()
    def downloader(job: DownloadJob, progress_cb, done_cb) -> None:
        run_download(job, tmp_dir, output_dir, progress_cb, done_cb, settings.naming_template, settings.organize_by_site, settings.concurrent_fragments, policy)
    return DownloadManager(downloader, max_workers=max(1, settings.parallel_downloads), history_store=history_store)


def resolve_stream_urls(video: Video) -> tuple[list[str], Optional[str]]:
    """Resolve direct media URLs for mpv without relying on mpv's yt-dlp hook.

    Preview/playback is not a download: prefer the best available stream even
    when it is below the configured download floor, so sub-720p videos can
    still be previewed instead of failing the quality policy.
    """
    ytdlp_cmd, ytdlp_error = resolve_ytdlp_command()
    if ytdlp_error or not ytdlp_cmd:
        return [], ytdlp_error or "yt-dlp is unavailable"
    cmd = [*ytdlp_cmd, "-g", "--no-playlist", "-f", YTDLP_FLEX_FORMAT]
    cmd.extend(_impersonation_args())
    cmd.extend(_site_ytdlp_args(video))
    cmd.append(_provider_download_url(video))
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    except Exception as exc:
        return [], str(exc)
    urls = [line.strip() for line in proc.stdout.splitlines() if line.strip().startswith("http") and not is_known_censored_media_url(line.strip())]
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip() or f"yt-dlp exit {proc.returncode}"
        return urls, err
    if not urls:
        return [], "yt-dlp returned no uncensored playable media URL"
    return urls, None


def download_one(video: Video, tmp_dir: Path, output_dir: Path, policy: DownloadPolicy | None = None) -> DownloadJob:
    job = DownloadJob(video=video)
    def progress_cb() -> None:
        return
    def done_cb(_: DownloadJob) -> None:
        return
    run_download(job, tmp_dir, output_dir, progress_cb, done_cb, policy=policy or DownloadPolicy())
    return job


def run_batch_download(videos: list[Video], tmp_dir: Path, output_dir: Path, parallel: int, policy: DownloadPolicy | None = None) -> list[DownloadJob]:
    tmp_dir.mkdir(parents=True, exist_ok=True); output_dir.mkdir(parents=True, exist_ok=True)
    jobs: list[DownloadJob] = []
    effective_policy = policy or DownloadPolicy()
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as executor:
        futures = {executor.submit(download_one, video, tmp_dir, output_dir, effective_policy): video for video in videos}
        for future in as_completed(futures):
            jobs.append(future.result())
    return jobs
