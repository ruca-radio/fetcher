from __future__ import annotations

import json
import stat
import threading
from pathlib import Path

import httpx
import pytest

from fetcher_core import provider_login


@pytest.fixture()
def cookies_dir(tmp_path, monkeypatch):
    directory = tmp_path / "cookies"
    monkeypatch.setattr(provider_login, "COOKIES_DIR", directory)
    return directory


def _cookie(name: str, value: str, domain: str = ".pornhub.com", **overrides):
    cookie = {
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
        "secure": True,
        "expiry": 1_900_000_000,
    }
    cookie.update(overrides)
    return cookie


def test_save_and_load_cookies_round_trip_with_private_permissions(cookies_dir):
    saved = provider_login.save_cookies(
        "PornHub",
        [
            _cookie("sessid", "abc"),
            _cookie("accessAgeDisclaimerPH", "2", domain=".www.pornhub.com"),
            _cookie("unrelated", "nope", domain=".example.com"),
            {"name": "", "value": "empty-name"},
            {"name": "missing-value"},
        ],
    )

    assert saved == 2
    path = cookies_dir / "pornhub.json"
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(cookies_dir.stat().st_mode) == 0o700
    assert json.loads(path.read_text())["site"] == "PornHub"

    loaded = provider_login.load_cookies("PornHub")
    assert {(cookie["name"], cookie["domain"]) for cookie in loaded} == {
        ("sessid", ".pornhub.com"),
        ("accessAgeDisclaimerPH", ".www.pornhub.com"),
    }
    assert provider_login.cookie_count("PornHub") == 2
    assert provider_login.is_signed_in("PornHub") is True


def test_load_cookies_tolerates_corrupt_state(cookies_dir):
    path = provider_login.cookie_path("RedTube")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")

    assert provider_login.load_cookies("RedTube") == []
    assert provider_login.is_signed_in("RedTube") is False


def test_clear_cookies_removes_session_state(cookies_dir):
    provider_login.save_cookies("PornHub", [_cookie("sessid", "abc")])
    assert provider_login.clear_cookies("PornHub") is True
    assert provider_login.clear_cookies("PornHub") is False
    assert provider_login.load_cookies("PornHub") == []


def test_cookie_header_deduplicates_names(cookies_dir):
    provider_login.save_cookies(
        "PornHub",
        [
            _cookie("sessid", "first", domain=".pornhub.com"),
            _cookie("sessid", "second", domain=".www.pornhub.com"),
            _cookie("platform", "pc"),
        ],
    )

    header = provider_login.cookie_header("PornHub")
    assert "sessid=second" in header
    assert "sessid=first" not in header
    assert "platform=pc" in header


def test_netscape_cookie_file_matches_ytdlp_format(cookies_dir):
    provider_login.save_cookies("xHamster", [_cookie("session", "token", domain=".xhamster.com")])

    path = provider_login.netscape_cookie_file("xHamster")

    assert path is not None and path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Netscape HTTP Cookie File")
    assert ".xhamster.com\tTRUE\t/\tTRUE\t1900000000\tsession\ttoken" in text


def test_netscape_cookie_file_is_none_without_session(cookies_dir):
    assert provider_login.netscape_cookie_file("RedTube") is None


def test_apply_to_httpx_attaches_stored_cookies(cookies_dir):
    provider_login.save_cookies("PornHub", [_cookie("sessid", "abc")])
    client = httpx.Client()

    applied = provider_login.apply_to_httpx(client, "PornHub")

    assert applied == 1
    assert any(cookie.name == "sessid" and cookie.value == "abc" for cookie in client.cookies.jar)


def test_apply_to_httpx_tolerates_plain_mapping():
    class Client:
        cookies = {}

    assert provider_login.apply_to_httpx(Client(), "PornHub") == 0


def test_apply_to_selenium_builds_browser_cookies(cookies_dir):
    provider_login.save_cookies(
        "RedTube",
        [_cookie("session", "token", domain=".redtube.com", secure=False, expiry=None)],
    )
    added: list[dict[str, object]] = []

    class Driver:
        def add_cookie(self, cookie):
            added.append(cookie)

    applied = provider_login.apply_to_selenium(Driver(), "RedTube")

    assert applied == 1
    assert added[0]["name"] == "session"
    assert added[0]["domain"] == ".redtube.com"
    assert added[0]["secure"] is False
    assert "expiry" not in added[0]


def _fake_chrome(monkeypatch, *, cookies, closed_after=None, raise_on_launch=False):
    import selenium.webdriver as webdriver

    state = {"quit": False, "gets": []}

    class Driver:
        def set_page_load_timeout(self, seconds):
            pass

        def get(self, url):
            state["gets"].append(url)

        def get_cookies(self):
            if closed_after is not None and len(state["gets"]) > closed_after:
                raise RuntimeError("window closed")
            return list(cookies)

        def quit(self):
            state["quit"] = True

    def launch(**kwargs):
        if raise_on_launch:
            raise RuntimeError("chrome missing")
        return Driver()

    monkeypatch.setattr(webdriver, "Chrome", launch)
    return state


def test_sign_in_saves_cookies_when_done(monkeypatch, cookies_dir):
    done = threading.Event()
    done.set()
    state = _fake_chrome(monkeypatch, cookies=[_cookie("sessid", "abc")])

    result = provider_login.sign_in("PornHub", done=done, timeout_s=1)

    assert result.saved is True
    assert result.cookie_count == 1
    assert "signed in" in result.message.lower()
    assert provider_login.is_signed_in("PornHub") is True
    assert state["quit"] is True


def test_sign_in_cancel_saves_nothing(monkeypatch, cookies_dir):
    cancel = threading.Event()
    cancel.set()
    _fake_chrome(monkeypatch, cookies=[_cookie("sessid", "abc")])

    result = provider_login.sign_in("PornHub", cancel=cancel, timeout_s=1)

    assert result.saved is False
    assert result.cookie_count == 0
    assert "cancelled" in result.message.lower()
    assert provider_login.is_signed_in("PornHub") is False


def test_sign_in_without_captured_cookies_reports_failure(monkeypatch, cookies_dir):
    done = threading.Event()
    done.set()
    _fake_chrome(monkeypatch, cookies=[])

    result = provider_login.sign_in("RedTube", done=done, timeout_s=1)

    assert result.saved is False
    assert "no session cookies" in result.message.lower()


def test_sign_in_browser_launch_failure_is_reported(monkeypatch, cookies_dir):
    _fake_chrome(monkeypatch, cookies=[], raise_on_launch=True)

    result = provider_login.sign_in("xHamster", timeout_s=1)

    assert result.saved is False
    assert "could not start" in result.message.lower()


def test_unsupported_site_is_rejected():
    with pytest.raises(ValueError):
        provider_login.load_cookies("Eporner")


def test_clear_cookies_removes_a_corrupt_directory(cookies_dir):
    corrupted = provider_login.cookie_path("PornHub")
    corrupted.mkdir(parents=True)

    assert provider_login.clear_cookies("PornHub") is True
    assert not corrupted.exists()


def test_sign_in_reports_cookie_save_failure(monkeypatch, cookies_dir):
    done = threading.Event()
    done.set()
    _fake_chrome(monkeypatch, cookies=[_cookie("sessid", "abc")])
    monkeypatch.setattr(provider_login, "save_cookies", lambda site, cookies: (_ for _ in ()).throw(OSError("disk full")))

    result = provider_login.sign_in("PornHub", done=done, timeout_s=1)

    assert result.saved is False
    assert "could not save" in result.message.lower()
