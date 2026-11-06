from __future__ import annotations

import json
import stat
import threading
from pathlib import Path

import httpx
import pytest

from fetcher_core import provider_login


@pytest.fixture(autouse=True)
def _isolated_legacy_twitter(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_login, "LEGACY_TWITTER_COOKIES", tmp_path / "isolated_twitter_cookies.json")
    monkeypatch.setattr(provider_login, "LEGACY_TWITTER_NETSCAPE", tmp_path / "isolated_twitter_cookies.net.txt")


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
    assert "saved 1 session cookie" in result.message.lower()
    assert "test session" in result.message.lower()
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


def _mock_http(monkeypatch, handler):
    import httpx

    monkeypatch.setattr(
        provider_login, "build_client",
        lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=kwargs.get("follow_redirects", False),
        ),
    )


def _store(site: str, **cookies) -> None:
    provider_login.save_cookies(
        site,
        [{"name": name, "value": value, "domain": f".{site.lower()}.com", "path": "/"} for name, value in cookies.items()],
    )


def test_test_session_reports_missing_without_store(cookies_dir):
    check = provider_login.test_session("PornHub")

    assert check.status == "missing"
    assert "no stored session" in check.message.lower()


@pytest.mark.parametrize(
    ("page", "expected_status"),
    [
        ("<script>var isLoggedInUser = 1;</script>", "valid"),
        ("<script>var isLoggedInUser = 0;</script>", "invalid"),
        ('<video class="age-wall-video" src="promo.mp4"></video>', "unknown"),
        ("<body>nothing to see</body>", "unknown"),
    ],
)
def test_aylo_session_probe(monkeypatch, cookies_dir, page, expected_status):
    _store("PornHub", sessid="abc")
    _mock_http(monkeypatch, lambda request: httpx.Response(200, text=page, request=request))

    check = provider_login.test_session("PornHub")

    assert check.status == expected_status
    assert check.site == "PornHub"


@pytest.mark.parametrize(("marker", "expected_status"), [("true", "valid"), ("false", "invalid")])
def test_xhamster_session_probe(monkeypatch, cookies_dir, marker, expected_status):
    _store("xHamster", sessid="abc")
    _mock_http(
        monkeypatch,
        lambda request: httpx.Response(200, text=f'{{"isAuth":{marker}}}', request=request),
    )

    check = provider_login.test_session("xHamster")

    assert check.status == expected_status


def test_twitter_session_probe_needs_auth_token(monkeypatch, cookies_dir):
    _store("Twitter", ct0="csrf-only")
    _mock_http(monkeypatch, lambda request: httpx.Response(200, text="home", request=request))

    check = provider_login.test_session("Twitter")

    assert check.status == "invalid"
    assert "auth_token" in check.message


def test_twitter_session_probe_accepts_logged_in_home(monkeypatch, cookies_dir):
    _store("Twitter", auth_token="token")
    _mock_http(monkeypatch, lambda request: httpx.Response(200, text="home", request=request))

    check = provider_login.test_session("Twitter")

    assert check.status == "valid"


def test_twitter_session_probe_rejects_login_redirect(monkeypatch, cookies_dir):
    _store("Twitter", auth_token="token")

    def handler(request):
        if request.url.path == "/home":
            return httpx.Response(302, headers={"location": "https://x.com/i/flow/login"}, request=request)
        return httpx.Response(200, text="login page", request=request)

    _mock_http(monkeypatch, handler)

    check = provider_login.test_session("Twitter")

    assert check.status == "invalid"
    assert "not logged in" in check.message


def test_test_session_reports_network_failure_as_unknown(monkeypatch, cookies_dir):
    _store("PornHub", sessid="abc")

    def fail(request):
        raise httpx.ConnectError("no network", request=request)

    _mock_http(monkeypatch, fail)

    check = provider_login.test_session("PornHub")

    assert check.status == "unknown"
    assert "failed" in check.message.lower()


def test_test_session_uses_legacy_twitter_auth_token(monkeypatch, cookies_dir):
    provider_login.LEGACY_TWITTER_COOKIES.parent.mkdir(parents=True, exist_ok=True)
    provider_login.LEGACY_TWITTER_COOKIES.write_text('{"auth_token": "legacy-token"}', encoding="utf-8")
    _mock_http(monkeypatch, lambda request: httpx.Response(200, text="home", request=request))

    check = provider_login.test_session("Twitter")

    assert check.status == "valid"
    assert provider_login.twitter_authenticated() is True


def test_test_session_legacy_token_is_not_shadowed_by_anonymous_cookies(monkeypatch, cookies_dir):
    _store("Twitter", guest_id="anon")
    provider_login.LEGACY_TWITTER_COOKIES.parent.mkdir(parents=True, exist_ok=True)
    provider_login.LEGACY_TWITTER_COOKIES.write_text(
        '{"auth_token": "legacy-token", "ct0": "csrf"}',
        encoding="utf-8",
    )
    seen: list[str] = []

    def handler(request):
        seen.append(request.headers.get("cookie", ""))
        return httpx.Response(200, text="home", request=request)

    _mock_http(monkeypatch, handler)

    check = provider_login.test_session("Twitter")

    assert check.status == "valid"
    assert seen
    assert "auth_token=legacy-token" in seen[0]
    assert "guest_id" not in seen[0]


def test_aylo_explicit_logged_out_beats_shared_menu_markup(monkeypatch, cookies_dir):
    _store("PornHub", sessid="abc")
    page = (
        '<div id="profileMenuDropdown"></div><i class="ph-icon-logout"></i>'
        "<script>var isLoggedInUser = 0;</script>"
    )
    _mock_http(monkeypatch, lambda request: httpx.Response(200, text=page, request=request))

    check = provider_login.test_session("PornHub")

    assert check.status == "invalid"


def test_clear_cookies_forgets_legacy_twitter_session(cookies_dir):
    provider_login.LEGACY_TWITTER_COOKIES.parent.mkdir(parents=True, exist_ok=True)
    provider_login.LEGACY_TWITTER_COOKIES.write_text('{"auth_token": "legacy-token"}', encoding="utf-8")
    provider_login.LEGACY_TWITTER_NETSCAPE.write_text("# netscape\n", encoding="utf-8")

    assert provider_login.twitter_authenticated() is True
    assert provider_login.clear_cookies("Twitter") is True
    assert provider_login.twitter_authenticated() is False
    assert not provider_login.LEGACY_TWITTER_COOKIES.exists()
    assert not provider_login.LEGACY_TWITTER_NETSCAPE.exists()
