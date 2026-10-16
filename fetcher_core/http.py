"""Single place where all Fetcher HTTP calls are constructed."""

from __future__ import annotations

import httpx

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/120.0.0.0 Safari/537.36"
)
HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
}
DEFAULT_TIMEOUT: float = 20.0


def build_client(*, timeout: float = DEFAULT_TIMEOUT, follow_redirects: bool = False) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        headers=dict(HEADERS),
        follow_redirects=follow_redirects,
        verify=True,
    )
