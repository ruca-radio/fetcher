"""PornHub 18+ age-gate helpers shared by the search and download paths.

PornHub shows an 18+ disclaimer interstitial before its main catalog.  The
site's own Enter control clears it by writing the ``accessAgeDisclaimerPH``
cookie (the value the disclaimer JS uses is ``2``); yt-dlp uses ``1`` for the
same cookie plus ``age_verified``/``accessPH``.  These helpers expose that
cookie set and the page markers needed to recognise a still-gated response.

No third-party dependencies: callers pass any object exposing an httpx-style
``cookies.set(name, value, domain=..., path=...)`` method.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

PORNHUB_AGE_COOKIES = {
    "accessAgeDisclaimerPH": "2",
    "age_verified": "1",
    "accessPH": "1",
    "platform": "pc",
}
PORNHUB_COOKIE_HEADER = "accessAgeDisclaimerPH=2; age_verified=1; accessPH=1"

_AGE_GATE_PATH_MARKERS = (
    "age-verification",
    "age_verification",
    "verify-age",
    "agegate",
)
_SFW_JS_RE = re.compile(r"window\s*\.\s*isSfwPage\s*=\s*true\b", re.IGNORECASE)
_SFW_BODY_CLASS_RE = re.compile(r"""class\s*=\s*["'][^"']*\bsfw-page\b""", re.IGNORECASE)
_RESULTS_LIST_RE = re.compile(
    r"""<ul\b[^>]*\bid\s*=\s*["']videoSearchResult["']""", re.IGNORECASE
)
_NO_RESULTS_RE = re.compile(r"""\bid\s*=\s*["']noResultBigText["']""", re.IGNORECASE)
_AGE_DISCLAIMER_MARKERS = (
    "js-ageDisclaimerModal",
    "ageDisclaimerMainBG",
    "accessAgeDisclaimerPH",
    "js-closeAgeModal",
)


def apply_pornhub_age_cookies(cookies) -> None:
    """Accept the 18+ disclaimer by writing the same cookies the site's JS writes."""
    for domain in ("pornhub.com", ".pornhub.com"):
        for name, value in PORNHUB_AGE_COOKIES.items():
            cookies.set(name, value, domain=domain, path="/")


def is_pornhub_age_gate_url(url: str) -> bool:
    """True when a response URL is PornHub's age-verification interstitial."""
    path = urlparse(str(url or "")).path.lower()
    return any(marker in path for marker in _AGE_GATE_PATH_MARKERS)


def is_pornhub_sfw_page(text: str) -> bool:
    """True when PornHub served its server-side restricted "SFW" catalog."""
    body = str(text or "")
    if _SFW_BODY_CLASS_RE.search(body):
        return True
    return bool(_SFW_JS_RE.search(body))


def is_pornhub_age_gate_page(text: str) -> bool:
    """True for a disclaimer page: disclaimer markup but no results container.

    PornHub mentions ``accessAgeDisclaimerPH`` in the cookie-consent config of
    every page, so an explicit no-results page must not be mistaken for a gate.
    """
    body = str(text or "")
    if _RESULTS_LIST_RE.search(body) or _NO_RESULTS_RE.search(body):
        return False
    return any(marker in body for marker in _AGE_DISCLAIMER_MARKERS)


def pornhub_age_gate_message() -> str:
    """User-facing explanation for gated/restricted PornHub search results."""
    return (
        "PornHub requires 18+ age verification and served its regional restricted "
        "SFW catalog instead of the full search results; Fetcher accepted the 18+ "
        "prompt, but that alone may not unlock the full catalog from this network "
        "or region."
    )


_AGE_WALL_MARKERS = (
    "age-wall-video",
    "age-wall-message",
    "agewall-video",
    'id="agewall"',
    "id='agewall'",
)


def is_age_wall_page(text: str) -> bool:
    """True for a site's age-verification wall page (not the actual video page).

    RedTube and other Aylo sites redirect restricted networks to a homepage
    age wall whose only media is a low-quality promo clip; treating that promo
    as the video would download an unrelated 240p file.
    """
    body = str(text or "").lower()
    return any(marker in body for marker in _AGE_WALL_MARKERS)


_AGE_WALL_REGION_PATTERNS = (
    re.compile(
        r"elected officials in ([A-Z][A-Za-z .,'-]{1,40}?) are requiring us to verify your age"
    ),
    re.compile(
        r"disable access to our website in ([A-Z][A-Za-z .,'-]{1,40}?)(?:\.|</p>|,)"
    ),
)


def age_wall_region(text: str) -> str:
    """Return the region named by an Aylo-style age-wall message, if any."""
    body = str(text or "")
    for pattern in _AGE_WALL_REGION_PATTERNS:
        match = pattern.search(body)
        if match:
            return match.group(1).strip()
    return ""


def age_wall_message(site: str, region: str = "") -> str:
    """User-facing explanation for a site age wall/homepage redirect."""
    where = f" from {region}" if region else " from this network or region"
    requirement = "regional age-verification requirements" if region else "regional age verification or site restrictions"
    return (
        f"{site} has disabled access{where} ({requirement}) and served its "
        "age-verification wall/homepage instead of the requested video. "
        "Fetcher refused to download the low-quality age-wall preview; change "
        "the network or VPN exit to another region and retry."
    )
