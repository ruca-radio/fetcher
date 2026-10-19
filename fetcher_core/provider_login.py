"""Interactive provider sign-in and session-cookie storage.

Some providers only expose their full catalog, player data, or downloads when a
session is signed in (or has accepted their age prompt).  Asking users to copy
cookies by hand is fragile, so Fetcher can open a visible Chrome window on the
provider's sign-in page, let the user authenticate normally, and capture the
resulting session cookies when the window closes.

Captured cookies are credentials: they live under ``STATE_DIR/cookies`` with
owner-only permissions, are never logged, and are consumed by the httpx
providers, yt-dlp downloads, previews, and the browser fallback.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from .models import STATE_DIR

COOKIES_DIR = STATE_DIR / "cookies"

# Providers where a browser sign-in unlocks additional content.  Eporner has
# no account wall, and Twitter keeps its existing token/cookie import flow.
SIGN_IN_SITES: tuple[str, ...] = ("PornHub", "RedTube", "xHamster")

SITE_LOGIN_URLS: dict[str, str] = {
    "PornHub": "https://www.pornhub.com/login",
    "RedTube": "https://www.redtube.com/login",
    "xHamster": "https://xhamster.com/login",
}
SITE_HOME_URLS: dict[str, str] = {
    "PornHub": "https://www.pornhub.com/",
    "RedTube": "https://www.redtube.com/",
    "xHamster": "https://xhamster.com/",
}
SITE_COOKIE_DOMAINS: dict[str, tuple[str, ...]] = {
    "PornHub": ("pornhub.com", "pornhub.org"),
    "RedTube": ("redtube.com", "redtube.com.br"),
    "xHamster": ("xhamster.com", "xhamster.desi", "xhamster.xxx"),
}

_POLL_SECONDS = 0.5


@dataclass(frozen=True)
class SignInResult:
    """Outcome of one interactive sign-in attempt."""

    site: str
    saved: bool
    cookie_count: int
    message: str


def _slug(site: str) -> str:
    return site.lower().replace(" ", "-")


def _normalize_site(site: str) -> str:
    for known in SIGN_IN_SITES:
        if known.casefold() == str(site).casefold():
            return known
    raise ValueError(f"Unsupported provider sign-in site: {site!r}")


def cookie_path(site: str) -> Path:
    return COOKIES_DIR / f"{_slug(_normalize_site(site))}.json"


def _netscape_path(site: str) -> Path:
    return COOKIES_DIR / f"{_slug(_normalize_site(site))}.netscape.txt"


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _cookie_domain_allowed(domain: str, site: str) -> bool:
    normalized = str(domain or "").lstrip(".").lower()
    if not normalized:
        return False
    return any(normalized == root or normalized.endswith("." + root) for root in SITE_COOKIE_DOMAINS[site])


def _normalize_cookie(cookie: object, site: str) -> Optional[dict[str, object]]:
    if not isinstance(cookie, dict):
        return None
    name = str(cookie.get("name") or "").strip()
    value = cookie.get("value")
    if not name or not isinstance(value, str):
        return None
    raw_domain = str(cookie.get("domain") or "").strip()
    domain = raw_domain or "." + SITE_COOKIE_DOMAINS[site][0]
    if not _cookie_domain_allowed(domain, site):
        return None
    path = str(cookie.get("path") or "/")
    expiry = cookie.get("expiry") or cookie.get("expires")
    try:
        expiry_value = int(expiry) if expiry else None
    except (TypeError, ValueError):
        expiry_value = None
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
        "secure": bool(cookie.get("secure")),
        "expiry": expiry_value,
    }


def save_cookies(site: str, cookies: object) -> int:
    """Persist browser cookies for ``site`` and return the saved count.

    Only cookies belonging to the provider's domains are kept, duplicates are
    collapsed, and the file is written with owner-only permissions.
    """
    site = _normalize_site(site)
    saved: dict[tuple[str, str, str], dict[str, object]] = {}
    if isinstance(cookies, (list, tuple)):
        for cookie in cookies:
            normalized = _normalize_cookie(cookie, site)
            if normalized is None:
                continue
            key = (str(normalized["name"]), str(normalized["domain"]), str(normalized["path"]))
            saved[key] = normalized
    payload = {
        "site": site,
        "saved_at": int(time.time()),
        "cookies": list(saved.values()),
    }
    _write_private(cookie_path(site), json.dumps(payload, indent=2) + "\n")
    try:
        _netscape_path(site).unlink()
    except FileNotFoundError:
        pass
    return len(saved)


def load_cookies(site: str) -> list[dict[str, object]]:
    """Return the stored cookies for ``site``; corrupt state reads as empty."""
    site = _normalize_site(site)
    try:
        payload = json.loads(cookie_path(site).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    cookies = payload.get("cookies") if isinstance(payload, dict) else None
    if not isinstance(cookies, list):
        return []
    normalized = []
    for cookie in cookies:
        item = _normalize_cookie(cookie, site)
        if item is not None:
            normalized.append(item)
    return normalized


def cookie_count(site: str) -> int:
    return len(load_cookies(site))


def is_signed_in(site: str) -> bool:
    return cookie_count(site) > 0


def clear_cookies(site: str) -> bool:
    """Forget a provider session; returns whether any state existed."""
    site = _normalize_site(site)
    removed = False
    for path in (cookie_path(site), _netscape_path(site)):
        try:
            path.unlink()
            removed = True
        except FileNotFoundError:
            continue
        except IsADirectoryError:
            # Corrupt runtime state (a directory where a cookie file belongs).
            shutil.rmtree(path, ignore_errors=True)
            removed = True
        except OSError:
            continue
    return removed


def cookie_header(site: str) -> str:
    """Build a ``name=value; ...`` header from the stored session cookies."""
    headers: dict[str, str] = {}
    for cookie in load_cookies(site):
        headers[str(cookie["name"])] = str(cookie["value"])
    return "; ".join(f"{name}={value}" for name, value in headers.items())


def netscape_cookie_file(site: str) -> Optional[Path]:
    """Materialize stored cookies as a Netscape file for yt-dlp/ffmpeg."""
    site = _normalize_site(site)
    cookies = load_cookies(site)
    if not cookies:
        return None
    lines = ["# Netscape HTTP Cookie File", "# https://curl.se/docs/http-cookies.html"]
    for cookie in cookies:
        domain = str(cookie["domain"])
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        expiry = cookie.get("expiry") or 0
        lines.append(
            "\t".join(
                (
                    domain,
                    include_subdomains,
                    str(cookie.get("path") or "/"),
                    secure,
                    str(expiry),
                    str(cookie["name"]),
                    str(cookie["value"]),
                )
            )
        )
    path = _netscape_path(site)
    _write_private(path, "\n".join(lines) + "\n")
    return path


def apply_to_httpx(client, site: str) -> int:
    """Attach stored cookies to an httpx client; returns the applied count."""
    applied = 0
    cookies = getattr(client, "cookies", None)
    if cookies is None:
        return 0
    for cookie in load_cookies(site):
        try:
            cookies.set(
                str(cookie["name"]),
                str(cookie["value"]),
                domain=str(cookie["domain"]),
                path=str(cookie.get("path") or "/"),
            )
            applied += 1
        except Exception:
            continue
    return applied


def apply_to_selenium(driver, site: str) -> int:
    """Attach stored cookies to a browser already on the provider's domain."""
    applied = 0
    for cookie in load_cookies(site):
        payload: dict[str, object] = {
            "name": str(cookie["name"]),
            "value": str(cookie["value"]),
            "domain": str(cookie["domain"]),
            "path": str(cookie.get("path") or "/"),
            "secure": bool(cookie.get("secure")),
        }
        if cookie.get("expiry"):
            payload["expiry"] = int(cookie["expiry"])
        try:
            driver.add_cookie(payload)
            applied += 1
        except Exception:
            continue
    return applied


def _status(on_status: Optional[Callable[[str], None]], message: str) -> None:
    if on_status is None:
        return
    try:
        on_status(message)
    except Exception:
        pass


def sign_in(
    site: str,
    *,
    done: Optional[threading.Event] = None,
    cancel: Optional[threading.Event] = None,
    timeout_s: float = 600.0,
    on_status: Optional[Callable[[str], None]] = None,
) -> SignInResult:
    """Open Chrome so the user can sign in, then save the session cookies.

    The user logs in in the visible browser window and closes it when done; the
    sign-in is also considered complete when ``done`` is set.  ``cancel`` aborts
    without saving.  A timeout closes the window and saves only what was
    captured so far.
    """
    site = _normalize_site(site)
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except Exception as exc:
        return SignInResult(site, False, 0, f"{site}: browser sign-in is unavailable ({exc}).")

    profile_dir = tempfile.mkdtemp(prefix=f"fetcher-signin-{_slug(site)}-")
    options = Options()
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--new-window")

    driver = None
    snapshot: list[dict[str, object]] = []
    cancelled = False
    window_closed = False
    timed_out = False
    try:
        _status(on_status, f"{site}: opening a Chrome window for sign-in…")
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(30)
        driver.get(SITE_LOGIN_URLS[site])
        _status(
            on_status,
            f"{site}: log in in the Chrome window, then close the window. "
            "Fetcher saves the session when it closes.",
        )
        deadline = time.monotonic() + max(5.0, float(timeout_s))
        while True:
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            try:
                snapshot = driver.get_cookies()
            except Exception:
                window_closed = True
                break
            if done is not None and done.is_set():
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(_POLL_SECONDS)
        if not cancelled and not window_closed and not timed_out:
            # The user kept the window open; refresh the cookie snapshot from
            # the provider home page so all domains are represented.
            try:
                driver.get(SITE_HOME_URLS[site])
                time.sleep(0.5)
                snapshot = driver.get_cookies() or snapshot
            except Exception:
                pass
    except Exception as exc:
        message = f"{site}: sign-in could not start ({exc.__class__.__name__}: {exc})."
        return SignInResult(site, False, 0, message)
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        shutil.rmtree(profile_dir, ignore_errors=True)

    if cancelled:
        return SignInResult(site, False, 0, f"{site}: sign-in cancelled; nothing saved.")
    try:
        count = save_cookies(site, snapshot)
    except Exception as exc:
        return SignInResult(
            site, False, 0,
            f"{site}: could not save the session cookies ({exc.__class__.__name__}: {exc}).",
        )
    if count == 0:
        return SignInResult(
            site, False, 0,
            f"{site}: no session cookies were captured; nothing saved. "
            "Make sure the login completed in the Chrome window.",
        )
    suffix = " (sign-in window timed out; saved the current session)" if timed_out else ""
    return SignInResult(
        site, True, count,
        f"{site}: signed in; saved {count} session cookie(s){suffix}.",
    )
