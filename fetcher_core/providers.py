from __future__ import annotations

import base64
import hashlib
import html
import json
import math
import re
from argparse import Namespace
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote_plus, urlparse
from xml.etree.ElementTree import Element, SubElement

from fetcher_core.age_gate import (
    apply_pornhub_age_cookies,
    is_pornhub_age_gate_page,
    is_pornhub_age_gate_url,
    is_pornhub_sfw_page,
    pornhub_age_gate_message,
)
from fetcher_core import provider_login
from fetcher_core.filters import dedupe_videos, video_matches_filters
from fetcher_core.http import build_client
from fetcher_core.models import AppSettings, STATE_DIR, SearchFilters, Video
from fetcher_core.settings import normalize_min_quality

SITES = ["All Sites", "PornHub", "xHamster", "RedTube", "Eporner", "SearXNG"]
TWITTER_COOKIES = STATE_DIR / "twitter_cookies.json"
LAST_PROVIDER_ERRORS: dict[str, str] = {}
LAST_PROVIDER_WARNINGS: dict[str, str] = {}

def _record_provider_error(site: str, message: str) -> None:
    LAST_PROVIDER_ERRORS[site] = message


def _clear_provider_error(site: str) -> None:
    LAST_PROVIDER_ERRORS.pop(site, None)


def _format_provider_exception(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return f"{exc.__class__.__name__}: {message}"


def _search_provider(site: str, query: str, min_s: int, max_s: int, pages: int) -> list[Video]:
    try:
        return SITE_FN[site](query, min_s, max_s, pages)
    except Exception as exc:
        _record_provider_error(site, _format_provider_exception(exc))
        return []

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


def _clean_thumbnail_url(url: object) -> Optional[str]:
    if not url:
        return None
    if isinstance(url, dict):
        url = url.get("src") or url.get("url") or url.get("thumb")
    elif isinstance(url, list):
        for item in url:
            cleaned = _clean_thumbnail_url(item)
            if cleaned:
                return cleaned
        return None
    text = str(url).replace("\\/", "/")
    # Some xHamster search rows expose explicitly blurred "sfw" thumbnails.
    # Those thumbnails are also a good signal that the row itself should not be
    # auto-selected for downloads in this app.
    if "/sfw/" in text:
        return None
    return html.unescape(text)


def _first_clean_thumbnail(*urls: object) -> Optional[str]:
    for url in urls:
        cleaned = _clean_thumbnail_url(url)
        if cleaned:
            return cleaned
    return None


def _parse_int(value: object) -> int:
    try:
        return int(str(value).replace(",", "").strip() or "0")
    except Exception:
        return 0


def _least_factor(n: int) -> int:
    if n == 0:
        return 0
    if n * n < 2:
        return 1
    if n % 2 == 0:
        return 2
    if n % 3 == 0:
        return 3
    if n % 5 == 0:
        return 5
    limit = math.isqrt(n)
    for i in range(7, limit + 1, 30):
        for offset in (0, 4, 6, 10, 12, 16, 22, 24):
            candidate = i + offset
            if candidate <= limit and n % candidate == 0:
                return candidate
    return n


def _solve_pornhub_key_challenge(text: str) -> Optional[str]:
    if "document.cookie=\"KEY=\"" not in text or "function leastFactor" not in text:
        return None
    cleaned = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    p_match = re.search(r"var\s+p\s*=\s*(\d+)", cleaned)
    s_match = re.search(r"var\s+s\s*=\s*(\d+)", cleaned)
    tail_match = re.search(r'KEY="\+n\+"\*"\+p/n\+":"\+s\+":(\d+):(\d+);path=/', cleaned)
    if not p_match or not s_match or not tail_match:
        return None

    p = int(p_match.group(1))
    s = int(s_match.group(1))
    conditional_re = re.compile(
        r"if\s*\(\(s\s*>>\s*(\d+)\)\s*&\s*1\)\s*"
        r"p\s*([+-])=\s*(\d+)\s*\*\s*(\d+)\s*;\s*"
        r"else\s*p\s*([+-])=\s*(\d+)\s*\*\s*(\d+)\s*;",
        re.DOTALL,
    )
    for match in conditional_re.finditer(cleaned):
        bit = int(match.group(1))
        if (s >> bit) & 1:
            op, left, right = match.group(2), match.group(3), match.group(4)
        else:
            op, left, right = match.group(5), match.group(6), match.group(7)
        value = int(left) * int(right)
        p = p + value if op == "+" else p - value

    remaining = conditional_re.sub("", cleaned)
    for match in re.finditer(r"p\s*([+-])=\s*(\d+)(?:\s*\*\s*(\d+))?\s*;", remaining):
        value = int(match.group(2)) * (int(match.group(3)) if match.group(3) else 1)
        p = p + value if match.group(1) == "+" else p - value

    factor = _least_factor(p)
    return f"{factor}*{p // factor}:{s}:{tail_match.group(1)}:{tail_match.group(2)}"


def _apply_site_session(client, site: str) -> None:
    """Attach stored provider sign-in cookies to an HTTP client."""
    try:
        provider_login.apply_to_httpx(client, site)
    except Exception:
        # Test doubles or providers without a sign-in flow.
        pass


def _apply_pornhub_age_cookies(client) -> None:
    """Best-effort: accept PornHub's 18+ disclaimer on this client's cookie jar."""
    try:
        apply_pornhub_age_cookies(client.cookies)
    except Exception:
        # Test doubles/mappings may not expose an httpx-style cookie jar.
        pass


def _pornhub_is_gated_response(response) -> bool:
    """True when the response is still behind the 18+ interstitial.

    The restricted SFW catalog is *not* an age-gate page: it always renders the
    disclaimer markup, yet its rows must be rejected differently (server-side
    regional restriction cannot be unlocked by accepting the prompt).
    """
    text = getattr(response, "text", "") or ""
    if is_pornhub_sfw_page(text):
        return False
    return is_pornhub_age_gate_url(str(getattr(response, "url", ""))) or is_pornhub_age_gate_page(text)


def _pornhub_get(client, url: str):
    _apply_pornhub_age_cookies(client)
    response = client.get(url, follow_redirects=True)
    key = _solve_pornhub_key_challenge(response.text)
    if key:
        host = urlparse(str(response.url)).hostname or "www.pornhub.com"
        client.cookies.set("KEY", key, domain=host, path="/")
        client.cookies.set("KEY", key, domain="pornhub.com", path="/")
        response = client.get(url, follow_redirects=True)
    if _pornhub_is_gated_response(response):
        # Perform the same 18+ acceptance the site's Enter control performs
        # (accessAgeDisclaimerPH=2), then retry the originally requested URL
        # exactly once.
        _apply_pornhub_age_cookies(client)
        try:
            retried = client.get(url, follow_redirects=True)
        except Exception:
            _record_provider_error("PornHub", pornhub_age_gate_message())
            return response
        if _pornhub_is_gated_response(retried):
            _record_provider_error("PornHub", pornhub_age_gate_message())
        return retried
    return response


class _PornHubSearchParser(HTMLParser):
    """Read only the results list, preserving individual card boundaries."""

    _VOID_TAGS = frozenset({
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: Optional[Element] = None
        self.no_results = False
        self.is_sfw = False
        self._in_script = False
        self._stack: list[Element] = []

    def handle_starttag(self, tag, attrs) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "script":
            self._in_script = True
        if tag == "div" and attributes.get("id") == "noResultBigText":
            self.no_results = True
        if self._stack:
            node = SubElement(self._stack[-1], tag, attributes)
        elif self.results is None and tag == "ul" and attributes.get("id") == "videoSearchResult":
            node = self.results = Element(tag, attributes)
        else:
            return
        if tag not in self._VOID_TAGS:
            self._stack.append(node)

    def handle_endtag(self, tag) -> None:
        if tag == "script":
            self._in_script = False
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                break

    def handle_startendtag(self, tag, attrs) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data) -> None:
        if self._in_script and re.search(r"\bwindow\.isSfwPage\s*=\s*true\b", data):
            self.is_sfw = True
        if not self._stack:
            return
        node = self._stack[-1]
        if len(node):
            node[-1].tail = (node[-1].tail or "") + data
        else:
            node.text = (node.text or "") + data


def _search_pornhub_html(client, query: str, min_s: int, max_s: int, pages: int) -> list[Video]:
    out: list[Video] = []
    seen: set[str] = set()
    gated = False
    for page in range(1, pages + 1):
        url = f"https://www.pornhub.com/video/search?search={quote_plus(query)}&page={page}"
        try:
            response = _pornhub_get(client, url)
        except Exception as exc:
            _record_provider_error("PornHub", _format_provider_exception(exc))
            continue
        if is_pornhub_sfw_page(response.text):
            # Server-side regional restriction: PornHub serves unrelated
            # "SFW US States" rows in place of real matches, and accepting the
            # 18+ prompt cannot unlock the full catalog. Never surface those
            # rows as search matches.
            _record_provider_error("PornHub", pornhub_age_gate_message())
            gated = True
            break
        if _pornhub_is_gated_response(response):
            # _pornhub_get already accepted the 18+ prompt and retried once;
            # the interstitial is still being served, so report it.
            _record_provider_error("PornHub", pornhub_age_gate_message())
            gated = True
            break
        final_url = urlparse(str(response.url))
        if (
            (response.status_code >= 400 and response.status_code != 404)
            or final_url.scheme != "https"
            or final_url.hostname not in {"www.pornhub.com", "pornhub.com"}
            or final_url.path.rstrip("/") != "/video/search"
            or parse_qs(final_url.query).get("search") != [query]
            or _solve_pornhub_key_challenge(response.text)
        ):
            _record_provider_error(
                "PornHub",
                "search page redirected away from results; this is usually regional access, age verification, or anti-bot gating",
            )
            continue
        parser = _PornHubSearchParser()
        parser.feed(response.text)
        parser.close()
        if parser.no_results:
            _clear_provider_error("PornHub")
            break
        if response.status_code == 404:
            _record_provider_error("PornHub", "search returned HTTP 404 without a no-results page")
            continue
        if parser.results is None:
            _record_provider_error("PornHub", "search results list missing; the page may be gated or its layout has changed")
            continue
        _clear_provider_error("PornHub")
        LAST_PROVIDER_WARNINGS.pop("PornHub", None)
        for card in parser.results.findall("li"):
            if not (
                card.get("data-video-vkey") or card.get("data-video-id")
                or "videoblock" in card.get("class", "").split()
            ):
                continue
            link = None
            vid = ""
            for anchor in card.iter("a"):
                target = urlparse(anchor.get("href", ""))
                candidate = parse_qs(target.query).get("viewkey", [""])[0]
                if (
                    target.path == "/view_video.php"
                    and target.scheme in {"", "https"}
                    and target.hostname in {None, "www.pornhub.com", "pornhub.com"}
                    and re.fullmatch(r"[a-zA-Z0-9]+", candidate)
                    and (not card.get("data-video-vkey") or card.get("data-video-vkey") == candidate)
                ):
                    link, vid = anchor, candidate
                    break
            if link is None or vid in seen:
                continue
            seen.add(vid)
            title = link.get("title") or link.get("data-title") or "".join(link.itertext()).strip() or vid
            duration = next((node for node in card.iter() if "duration" in node.get("class", "").split()), None)
            dur = _parse_dur("".join(duration.itertext()).strip()) if duration is not None else 0
            if dur and not (min_s <= dur <= max_s):
                continue
            views = next((node for node in card.iter() if "views" in node.get("class", "").split()), None)
            thumbnail = next(card.iter("img"), None)
            out.append(Video(
                id=vid,
                title=title,
                duration_s=dur,
                views=_parse_int("".join(views.itertext()).replace("views", "")) if views is not None else 0,
                url=f"https://www.pornhub.com/view_video.php?viewkey={vid}",
                site="PornHub",
                thumbnail_url=_first_clean_thumbnail(
                    thumbnail.get("data-mediumthumb"), thumbnail.get("data-thumb_url"),
                    thumbnail.get("data-src"), thumbnail.get("src"),
                ) if thumbnail is not None else None,
            ))
    if out and not gated:
        _clear_provider_error("PornHub")
    return out


def search_pornhub(query: str, min_s: int, max_s: int, pages: int) -> list[Video]:
    out, seen = [], set()
    _clear_provider_error("PornHub")
    LAST_PROVIDER_WARNINGS.pop("PornHub", None)
    with build_client(timeout=20) as c:
        _apply_pornhub_age_cookies(c)
        _apply_site_session(c, "PornHub")
        html_results = _search_pornhub_html(c, query, min_s, max_s, pages)
        if html_results or "PornHub" not in LAST_PROVIDER_ERRORS:
            return html_results
        if LAST_PROVIDER_ERRORS.get("PornHub") == pornhub_age_gate_message():
            # 18+ interstitial or server-side SFW restriction: the legacy
            # webmasters API sits behind the same gate, so do not retry it.
            return html_results
        for page in range(1, pages + 1):
            url = f"https://www.pornhub.com/webmasters/search?search={quote_plus(query)}&page={page}"
            try:
                response = _pornhub_get(c, url)
                content_type = response.headers.get("content-type", "").lower()
                final_url = urlparse(str(response.url))
                if (
                    response.status_code >= 400
                    or final_url.scheme != "https"
                    or final_url.hostname not in {"www.pornhub.com", "pornhub.com"}
                    or final_url.path.rstrip("/") != "/webmasters/search"
                    or parse_qs(final_url.query).get("search") != [query]
                    or "json" not in content_type
                ):
                    _record_provider_error("PornHub", "legacy webmaster API unavailable; trying HTML search fallback")
                    return _search_pornhub_html(c, query, min_s, max_s, pages)
                try:
                    payload = response.json()
                except ValueError:
                    _record_provider_error("PornHub", "legacy webmaster API returned non-JSON; trying HTML search fallback")
                    return _search_pornhub_html(c, query, min_s, max_s, pages)
                for v in payload.get("videos", []):
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
                            thumbnail_url=_first_clean_thumbnail(v.get("default_thumb"), v.get("thumb")),
                            rating_percent=_parse_percent(v.get("rating")),
                            quality_p=_parse_quality(v.get("quality")) or (720 if hd else 480),
                            is_hd=hd))
                _clear_provider_error("PornHub")
            except Exception as exc:
                _record_provider_error("PornHub", str(exc))
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
    skipped_blurred = 0
    slug = re.sub(r"\s+", "-", query.strip())
    with build_client(timeout=20, follow_redirects=True) as c:
        _apply_site_session(c, "xHamster")
        for page in range(1, pages + 1):
            url = f"https://xhamster.com/search/{slug}" + (f"?page={page}" if page > 1 else "")
            try:
                for v in _xh_parse(c.get(url).text):
                    vid = str(v.get("id", ""))
                    if not vid or vid in seen:
                        continue
                    thumbnail = _first_clean_thumbnail(
                        v.get("previewThumbURL"),
                        v.get("imageURL"),
                        v.get("thumbURL169"),
                        v.get("thumbURL"),
                    )
                    if v.get("isBlurredText") and thumbnail is None:
                        skipped_blurred += 1
                    page_url = str(v.get("pageURL", "")).replace("\\/", "/")
                    if not page_url:
                        continue
                    seen.add(vid)
                    dur = int(v.get("duration", 0))
                    if min_s <= dur <= max_s:
                        quality = _parse_quality(v.get("quality")) or _parse_quality(v.get("videoQuality"))
                        hd_flag = v.get("isHD")
                        out.append(Video(id=vid, title=v.get("title", ""),
                            duration_s=dur, views=int(v.get("viewCount", 0) or 0),
                            url=page_url,
                            site="xHamster",
                            thumbnail_url=thumbnail,
                            rating_percent=_parse_percent(v.get("rating")),
                            quality_p=quality,
                            is_hd=bool(hd_flag) if hd_flag is not None else (quality >= 720 if quality is not None else None)))
                if out:
                    _clear_provider_error("xHamster")
            except Exception as exc:
                _record_provider_error("xHamster", str(exc))
    if not out and skipped_blurred:
        _record_provider_error("xHamster", f"skipped {skipped_blurred} blurred/search-only rows that are not downloadable")
    return out


def search_redtube(query: str, min_s: int, max_s: int, pages: int) -> list[Video]:
    out, seen = [], set()
    with build_client(timeout=20) as c:
        _apply_site_session(c, "RedTube")
        for page in range(1, pages + 1):
            url = (f"https://api.redtube.com/?data=redtube.Videos.searchVideos"
                   f"&search={quote_plus(query)}&output=json&page={page}&limit=20"
                   f"&thumbsize=big")
            try:
                for entry in c.get(url).json().get("videos", []):
                    v = entry.get("video", {})
                    vid = str(v.get("video_id", ""))
                    if not vid or vid in seen:
                        continue
                    seen.add(vid)
                    dur = _parse_dur(str(v.get("duration", "0:00")))
                    if min_s <= dur <= max_s:
                        # The RedTube API may omit quality metadata entirely.
                        # Unknown quality must remain eligible for later probing.
                        hd = bool(v["hd"]) if "hd" in v else None
                        out.append(Video(id=vid, title=v.get("title", ""),
                            duration_s=dur, views=int(v.get("views", 0) or 0),
                            url=v.get("url", f"https://www.redtube.com/{vid}"),
                            site="RedTube",
                            thumbnail_url=_first_clean_thumbnail(v.get("default_thumb"), v.get("thumb")),
                            rating_percent=_parse_percent(v.get("rating")),
                            quality_p=_parse_quality(v.get("quality")) or (720 if hd else 480 if hd is False else None),
                            is_hd=hd))
                _clear_provider_error("RedTube")
            except Exception as exc:
                _record_provider_error("RedTube", str(exc))
    return out


def search_eporner(query: str, min_s: int, max_s: int, pages: int) -> list[Video]:
    out, seen = [], set()
    with build_client(timeout=20) as c:
        for page in range(1, pages + 1):
            url = (f"https://www.eporner.com/api/v2/video/search/"
                   f"?query={quote_plus(query)}&per_page=20&page={page}"
                   f"&min_duration={min_s}&max_duration={max_s}&format=json")
            try:
                for v in c.get(url).json().get("videos", []):
                    try:
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
                                thumbnail_url=_first_clean_thumbnail(v.get("default_thumb"), v.get("thumb")),
                                rating_percent=_parse_percent(v.get("rate") or v.get("rating")),
                                quality_p=quality,
                                is_hd=(quality >= 720) if quality is not None else None,
                                keywords=v.get("keywords", "")))
                    except Exception:
                        # A single malformed row must not abort the rest of the page.
                        continue
                _clear_provider_error("Eporner")
            except Exception as exc:
                _record_provider_error("Eporner", str(exc))
    return out


def _site_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "pornhub" in host:
        return "PornHub"
    if "xhamster" in host:
        return "xHamster"
    if "redtube" in host:
        return "RedTube"
    if "eporner" in host:
        return "Eporner"
    return host.removeprefix("www.") or "SearXNG"


def _unwrap_nudevista_url(url: str) -> str:
    parsed = urlparse(url)
    if "nudevista.com" not in parsed.netloc.lower():
        return url
    parts = parsed.path.split("/")
    if len(parts) < 3 or parts[1] != "video":
        return url
    encoded = parts[2].split("-", 1)[0]
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8", "replace")
    except Exception:
        return url
    match = re.search(r"https?://[^\s]+", decoded)
    if not match:
        return url
    target = match.group(0).replace("#", "-").rstrip("/")
    return target or url


def _resolved_searxng_url(raw_url: str, base_url: str) -> Optional[str]:
    parsed = urlparse(raw_url)
    base_host = urlparse(base_url).netloc.lower()
    if parsed.netloc.lower() == base_host:
        return None
    if parsed.path.startswith("/search"):
        return None
    return _unwrap_nudevista_url(raw_url)


def search_searxng(query: str, settings: AppSettings, pages: int, limit: int = 0) -> list[Video]:
    base_url = settings.searxng_url.strip().rstrip("/")
    if not base_url:
        return []
    out: list[Video] = []
    seen: set[str] = set()
    params = {
        "q": query,
        "format": "json",
        "pageno": "1",
    }
    if settings.searxng_categories.strip():
        params["categories"] = settings.searxng_categories.strip()
    with build_client(timeout=25, follow_redirects=True) as client:
        for page in range(1, max(1, pages) + 1):
            params["pageno"] = str(page)
            try:
                response = client.get(f"{base_url}/search", params=params)
                response.raise_for_status()
                data = response.json()
            except Exception as exc:
                _record_provider_error("SearXNG", str(exc))
                break
            for item in data.get("results", []):
                if not isinstance(item, dict):
                    continue
                raw_url = str(item.get("url") or "").strip()
                if not raw_url.startswith(("http://", "https://")) or raw_url in seen:
                    continue
                source_url = _resolved_searxng_url(raw_url, base_url)
                if not source_url or source_url in seen:
                    continue
                seen.add(source_url)
                title = html.unescape(str(item.get("title") or source_url))
                thumb = _first_clean_thumbnail(
                    item.get("thumbnail"),
                    item.get("img_src"),
                    item.get("content_img_src"),
                )
                author = str(item.get("author") or "").strip()
                site = _site_from_url(source_url)
                if site == "SearXNG" and author:
                    site = author[:1].upper() + author[1:]
                digest = hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:16]
                out.append(Video(
                    id=f"searxng-{digest}",
                    title=title,
                    duration_s=_parse_dur(str(item.get("length") or "")),
                    views=0,
                    url=source_url,
                    site=site or "SearXNG",
                    thumbnail_url=thumb,
                ))
                if limit > 0 and len(out) >= limit:
                    _clear_provider_error("SearXNG")
                    return out
    if out:
        _clear_provider_error("SearXNG")
    return out


SITE_FN = {
    "PornHub":  search_pornhub,
    "xHamster": search_xhamster,
    "RedTube":  search_redtube,
    "Eporner":  search_eporner,
}

def _searxng_results(query: str, filters: SearchFilters, settings: Optional[AppSettings]) -> list[Video]:
    if settings is None or not settings.searxng_enabled:
        _record_provider_error("SearXNG", "SearXNG is disabled; enable it in settings to search the wider web")
        return []
    found = search_searxng(query, settings, filters.pages)
    return [video for video in found if video_matches_filters(video, filters)]


def search_videos(query: str, site: str, filters: SearchFilters, twitter_count: int = 40, settings: Optional[AppSettings] = None) -> list[Video]:
    if site == "Twitter":
        return dedupe_videos(search_twitter(query, twitter_count, TWITTER_COOKIES))
    if site == "SearXNG":
        return dedupe_videos(_searxng_results(query, filters, settings))

    sites = list(SITE_FN) if site == "All Sites" else [site]
    results: list[Video] = []
    for name in sites:
        found = _search_provider(name, query, filters.min_duration_s, filters.max_duration_s, filters.pages)
        results.extend(video for video in found if video_matches_filters(video, filters))
    if site == "All Sites" and settings is not None and settings.searxng_enabled:
        results.extend(_searxng_results(query, filters, settings))
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
        "thumbnail_url": video.thumbnail_url,
    }


def build_filters_from_args(args: Namespace, settings: AppSettings) -> SearchFilters:
    base = settings.search_filters
    return SearchFilters(
        min_duration_min=args.min_duration if args.min_duration is not None else base.min_duration_min,
        max_duration_min=args.max_duration if args.max_duration is not None else base.max_duration_min,
        min_quality=normalize_min_quality(args.min_quality if args.min_quality is not None else base.min_quality),
        min_rating=args.min_rating if args.min_rating is not None else base.min_rating,
        hd_only=args.hd_only if getattr(args, "hd_only", False) else base.hd_only,
        pages=args.pages if args.pages is not None else base.pages,
        exclude_phrases=args.exclude if args.exclude is not None else base.exclude_phrases,
        exclude_censored=base.exclude_censored,
    )


_BARE_ID_TEMPLATES = {
    "PornHub": "https://www.pornhub.com/view_video.php?viewkey={id}",
    "xHamster": "https://xhamster.com/videos/{id}",
    "RedTube": "https://www.redtube.com/{id}",
    "Eporner": "https://www.eporner.com/video-{id}/",
}


def normalize_download_target(target: str, site: str) -> tuple[str, str]:
    if re.match(r"^https?://", target):
        return target, target
    template = _BARE_ID_TEMPLATES.get(site)
    if template:
        return target, template.format(id=target)
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

    _clear_provider_error("Twitter")

    # Prefer the Settings browser sign-in session; the legacy auth_token file
    # remains supported for users who imported it manually.
    stored_cookies = provider_login.load_cookies("Twitter") if provider_login.is_signed_in("Twitter") else []
    stored_auth_token = next(
        (str(cookie["value"]) for cookie in stored_cookies if cookie.get("name") == "auth_token"),
        "",
    )
    legacy: dict = {}
    if cookies_file.exists():
        try:
            payload = json.loads(cookies_file.read_text())
            legacy = payload if isinstance(payload, dict) else {}
        except Exception as exc:
            if not stored_cookies:
                _record_provider_error("Twitter", f"unreadable Twitter cookie file: {exc}")
                return []
    auth_token = stored_auth_token or str(legacy.get("auth_token") or "")
    if not auth_token:
        if not stored_cookies and not cookies_file.exists():
            _record_provider_error(
                "Twitter",
                "no Twitter session; use Settings → Provider Sign-ins → Twitter, "
                "or run extract_twitter_cookies.py",
            )
        else:
            _record_provider_error(
                "Twitter",
                "stored Twitter session has no auth_token; sign in again",
            )
        return []

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = None
    out: list[Video] = []
    seen: set[str] = set()

    try:
        driver = webdriver.Chrome(options=opts)
        driver.get("https://x.com")
        _time.sleep(2)
        if stored_cookies:
            provider_login.apply_to_selenium(driver, "Twitter")
        if not stored_auth_token:
            driver.add_cookie({
                "name": "auth_token", "value": auth_token,
                "domain": ".x.com", "path": "/",
            })
            ct0 = str(legacy.get("ct0") or "")
            if ct0:
                driver.add_cookie({"name": "ct0", "value": ct0, "domain": ".x.com", "path": "/"})

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
                    if not has_vid:
                        continue
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
    except Exception as exc:
        _record_provider_error("Twitter", _format_provider_exception(exc))
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    if out:
        _clear_provider_error("Twitter")
    elif "Twitter" not in LAST_PROVIDER_ERRORS:
        _record_provider_error("Twitter", "no media results captured; Chrome may be missing or the query returned nothing")

    return out
