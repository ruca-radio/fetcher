from __future__ import annotations

from argparse import Namespace
from unittest.mock import patch

import httpx
import pytest

import fetcher_core.providers as providers
from fetcher_core import age_gate
from fetcher_core.models import AppSettings, SearchFilters, Video
from fetcher_core.providers import build_filters_from_args


class _ClientContext:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, exc_type, exc, tb):
        return False


class _JsonResponse:
    headers = {"content-type": "application/json"}
    url = "https://www.eporner.com/api/v2/video/search/"
    text = ""
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def test_pornhub_search_records_html_timeout_instead_of_raising() -> None:
    class TimeoutClient:
        cookies = {}

        def get(self, *args, **kwargs):
            raise TimeoutError("html timed out")

    providers.LAST_PROVIDER_ERRORS.clear()
    with patch.object(providers, "build_client", return_value=_ClientContext(TimeoutClient())):
        assert providers.search_pornhub("query", 0, 9999, 1) == []

    assert "PornHub" in providers.LAST_PROVIDER_ERRORS
    assert "html timed out" in providers.LAST_PROVIDER_ERRORS["PornHub"]


def test_search_videos_records_provider_exception_and_keeps_other_sites(monkeypatch) -> None:
    def broken(*args):
        raise RuntimeError("blocked")

    def working(*args):
        return [Video("ok", "Okay", 120, 1, "https://example/ok", "Eporner", quality_p=1080)]

    monkeypatch.setitem(providers.SITE_FN, "PornHub", broken)
    monkeypatch.setitem(providers.SITE_FN, "Eporner", working)
    monkeypatch.setitem(providers.SITE_FN, "xHamster", lambda *args: [])
    monkeypatch.setitem(providers.SITE_FN, "RedTube", lambda *args: [])
    providers.LAST_PROVIDER_ERRORS.clear()

    results = providers.search_videos("query", "All Sites", SearchFilters(min_quality="any"))

    assert [video.id for video in results] == ["ok"]
    assert providers.LAST_PROVIDER_ERRORS["PornHub"] == "RuntimeError: blocked"


def test_eporner_censored_filter_is_controlled_by_search_filters() -> None:
    payload = {
        "videos": [
            {
                "id": "c1",
                "title": "Mosaic upload",
                "keywords": "censored",
                "length_sec": 120,
                "views": 5,
                "url": "/video-c1/title/",
                "quality": "1080p",
            }
        ]
    }

    class EpornerClient:
        def get(self, *args, **kwargs):
            return _JsonResponse(payload)

    providers.LAST_PROVIDER_ERRORS.clear()
    with patch.object(providers, "build_client", return_value=_ClientContext(EpornerClient())):
        direct = providers.search_eporner("query", 0, 9999, 1)
        default_filtered = providers.search_videos("query", "Eporner", SearchFilters())
        included = providers.search_videos(
            "query",
            "Eporner",
            SearchFilters(exclude_censored=False),
        )

    assert len(direct) == 1
    assert direct[0].keywords == "censored"
    assert default_filtered == []
    assert [video.id for video in included] == ["c1"]


def _cli_namespace(**overrides) -> Namespace:
    """Attribute set produced by the `fetcher search` subcommand parser."""
    values = {
        "query": "query",
        "site": "All Sites",
        "pages": None,
        "min_duration": None,
        "max_duration": None,
        "min_quality": None,
        "min_rating": None,
        "hd_only": False,
        "exclude": None,
        "count": 40,
        "limit": None,
        "json": False,
    }
    values.update(overrides)
    return Namespace(**values)


def test_build_filters_from_args_returns_search_filters() -> None:
    filters = build_filters_from_args(_cli_namespace(), AppSettings())
    assert isinstance(filters, SearchFilters)
    assert filters.min_quality == "720"

    explicit = build_filters_from_args(_cli_namespace(min_quality="1080"), AppSettings())
    assert explicit.min_quality == "1080"


def test_build_filters_from_args_carries_persisted_censored_preference() -> None:
    default_filters = build_filters_from_args(_cli_namespace(), AppSettings())
    assert default_filters.exclude_censored is True

    disabled = AppSettings(search_filters=SearchFilters(exclude_censored=False))
    disabled_filters = build_filters_from_args(_cli_namespace(), disabled)
    assert disabled_filters.exclude_censored is False


def test_eporner_malformed_row_is_skipped_and_page_continues() -> None:
    payload = {
        "videos": [
            {
                "id": "ok1",
                "title": "First",
                "length_sec": 100,
                "views": 1,
                "url": "/video-ok1/first/",
                "keywords": "",
            },
            {
                "id": "bad",
                "title": "Broken",
                "length_sec": "not-a-number",
                "url": "/video-bad/broken/",
            },
            {
                "id": "ok2",
                "title": "Second",
                "length_sec": 200,
                "views": 2,
                "url": "/video-ok2/second/",
                "keywords": "",
            },
        ]
    }

    class EpornerClient:
        def get(self, *args, **kwargs):
            return _JsonResponse(payload)

    providers.LAST_PROVIDER_ERRORS.clear()
    with patch.object(providers, "build_client", return_value=_ClientContext(EpornerClient())):
        results = providers.search_eporner("query", 0, 9999, 1)

    assert [video.id for video in results] == ["ok1", "ok2"]
    assert "Eporner" not in providers.LAST_PROVIDER_ERRORS


def test_redtube_missing_quality_metadata_does_not_claim_sd(monkeypatch):
    class Client:
        def get(self, *args, **kwargs):
            return _JsonResponse({"videos": [{"video": {
                "video_id": "42", "title": "Example", "duration": "2:00",
                "url": "https://www.redtube.com/42", "views": 100,
            }}]})
    monkeypatch.setattr(providers, "build_client", lambda **kwargs: _ClientContext(Client()))
    rows = providers.search_videos("query", "RedTube", SearchFilters(min_quality="720", hd_only=True))
    assert len(rows) == 1
    assert rows[0].quality_p is None
    assert rows[0].is_hd is None


def _pornhub_search(monkeypatch, markup, *, redirect=None, pages=1):
    monkeypatch.setattr(providers, "LAST_PROVIDER_ERRORS", {})

    def respond(request):
        if request.url.path == "/webmasters/search":
            return httpx.Response(503, text="API unavailable")
        if redirect and request.url.path == "/video/search" and str(request.url) != redirect:
            return httpx.Response(302, headers={"location": redirect})
        return httpx.Response(200, text=markup)

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(providers, "build_client", lambda **kwargs: httpx.Client(transport=transport))
    return providers.search_pornhub("needle", 0, 9999, pages)


def _pornhub_card(vid, title, *, thumb="", duration="2:00"):
    return (
        f'<li class="videoblock" data-video-vkey="{vid}">'
        f'<a href="/view_video.php?viewkey={vid}" title="{title}">'
        f'<img src="{thumb}"></a><var class="duration">{duration}</var></li>'
    )


def _pornhub_gate_page() -> str:
    """The 18+ disclaimer interstitial: no results list, disclaimer markup only."""
    return (
        '<html><body class="logged-out">'
        '<div id="ageDisclaimerMainBG">'
        '<div class="js-ageDisclaimerModal">You must be 18 or older to enter.</div>'
        '<a href="#" class="js-closeAgeModal">Enter</a>'
        '</div></body></html>'
    )


def test_age_gate_exposes_site_cookie_values() -> None:
    assert age_gate.PORNHUB_AGE_COOKIES == {
        "accessAgeDisclaimerPH": "2",
        "age_verified": "1",
        "accessPH": "1",
        "platform": "pc",
    }
    assert age_gate.PORNHUB_COOKIE_HEADER == (
        "accessAgeDisclaimerPH=2; age_verified=1; accessPH=1"
    )


def test_apply_pornhub_age_cookies_sets_values_for_both_domains() -> None:
    jar = httpx.Cookies()

    age_gate.apply_pornhub_age_cookies(jar)

    stored = {(cookie.domain, cookie.name): cookie.value for cookie in jar.jar}
    for domain in ("pornhub.com", ".pornhub.com"):
        for name, value in age_gate.PORNHUB_AGE_COOKIES.items():
            assert stored[(domain, name)] == value


@pytest.mark.parametrize("url", [
    "https://www.pornhub.com/age-verification",
    "https://www.pornhub.com/age_verification",
    "https://www.pornhub.com/verify-age",
    "https://www.pornhub.com/agegate",
    "https://www.pornhub.com/age-verification?redirect=%2Fvideo%2Fsearch",
])
def test_is_pornhub_age_gate_url_matches_gate_paths(url: str) -> None:
    assert age_gate.is_pornhub_age_gate_url(url) is True


@pytest.mark.parametrize("url", [
    "https://www.pornhub.com/video/search?search=needle&page=1",
    "https://www.pornhub.com/view_video.php?viewkey=ph1",
])
def test_is_pornhub_age_gate_url_ignores_catalog_urls(url: str) -> None:
    assert age_gate.is_pornhub_age_gate_url(url) is False


@pytest.mark.parametrize("text", [
    '<script>window.isSfwPage = true;</script>',
    '<script>window.isSfwPage=true</script>',
    '<script type="text/javascript">window.isSfwPage  =  true;</script>',
    '<body class="sfw-page logged-out">',
    '<body class="logged-out sfw-page">',
])
def test_is_pornhub_sfw_page_detects_restricted_catalog(text: str) -> None:
    assert age_gate.is_pornhub_sfw_page(text) is True


@pytest.mark.parametrize("text", [
    '<script>window.isSfwPage = false;</script>',
    '<body class="logged-out">',
    '<ul id="videoSearchResult"></ul>',
    "",
])
def test_is_pornhub_sfw_page_ignores_full_catalog(text: str) -> None:
    assert age_gate.is_pornhub_sfw_page(text) is False


def test_is_pornhub_age_gate_page_detects_disclaimer_without_results() -> None:
    assert age_gate.is_pornhub_age_gate_page(_pornhub_gate_page()) is True
    assert age_gate.is_pornhub_age_gate_page(
        '<div class="js-ageDisclaimerModal">18+</div>'
    ) is True
    # The real catalog also ships the disclaimer markup; the results list
    # proves the page is not gated.
    assert age_gate.is_pornhub_age_gate_page(
        '<div class="js-ageDisclaimerModal">18+</div>'
        '<ul id="videoSearchResult"><li class="videoblock"></li></ul>'
    ) is False


def test_is_pornhub_age_gate_page_ignores_sitewide_consent_and_no_results_pages() -> None:
    # accessAgeDisclaimerPH appears in the cookie-consent config of every
    # PornHub page, including a legitimate "no results" page.
    consent = '{"pornhub.com":{"accessAgeDisclaimerPH":"essential","accessPH":"essential"}}'
    assert age_gate.is_pornhub_age_gate_page(
        consent + '<div id="noResultBigText">No Search Results.</div>'
    ) is False


def test_pornhub_age_gate_message_is_actionable() -> None:
    message = age_gate.pornhub_age_gate_message()
    lowered = message.lower()
    assert "18+" in message
    assert "age verification" in lowered
    assert "restricted" in lowered
    assert "sfw" in lowered
    assert "may not unlock the full catalog" in lowered


def test_pornhub_search_sends_age_verification_cookies(monkeypatch):
    cookie_headers: list[str] = []
    markup = '<ul id="videoSearchResult">' + _pornhub_card("phHit", "needle result") + '</ul>'

    def respond(request):
        cookie_headers.append(request.headers.get("cookie", ""))
        return httpx.Response(200, text=markup)

    monkeypatch.setattr(providers, "LAST_PROVIDER_ERRORS", {})
    monkeypatch.setattr(
        providers,
        "build_client",
        lambda **kwargs: httpx.Client(transport=httpx.MockTransport(respond)),
    )

    rows = providers.search_pornhub("needle", 0, 9999, 1)

    assert [row.id for row in rows] == ["phHit"]
    assert cookie_headers, "the first search request must be sent with age cookies"
    first = cookie_headers[0]
    assert "accessAgeDisclaimerPH=2" in first
    assert "age_verified=1" in first
    assert "accessPH=1" in first


def test_pornhub_age_gate_redirect_is_retried_once_with_cookies(monkeypatch):
    search_requests: list[str] = []
    markup = '<ul id="videoSearchResult">' + _pornhub_card("phHit", "needle result") + '</ul>'

    def respond(request):
        if request.url.path == "/age-verification":
            return httpx.Response(200, text=_pornhub_gate_page())
        if request.url.path == "/video/search":
            search_requests.append(str(request.url))
            if len(search_requests) == 1:
                return httpx.Response(
                    302, headers={"location": "https://www.pornhub.com/age-verification"}
                )
            return httpx.Response(200, text=markup)
        return httpx.Response(503, text="API unavailable")

    monkeypatch.setattr(providers, "LAST_PROVIDER_ERRORS", {})
    monkeypatch.setattr(
        providers,
        "build_client",
        lambda **kwargs: httpx.Client(transport=httpx.MockTransport(respond)),
    )

    rows = providers.search_pornhub("needle", 0, 9999, 1)

    assert [row.id for row in rows] == ["phHit"]
    assert len(search_requests) == 2, "the gated search URL must be retried exactly once"
    assert "PornHub" not in providers.LAST_PROVIDER_ERRORS


def test_pornhub_persistent_age_gate_page_reports_error_without_rows(monkeypatch):
    requested: list[str] = []

    def respond(request):
        requested.append(str(request.url))
        return httpx.Response(200, text=_pornhub_gate_page())

    monkeypatch.setattr(providers, "LAST_PROVIDER_ERRORS", {})
    monkeypatch.setattr(
        providers,
        "build_client",
        lambda **kwargs: httpx.Client(transport=httpx.MockTransport(respond)),
    )

    assert providers.search_pornhub("needle", 0, 9999, 1) == []

    assert len(requested) == 2, "the age-gated request must be retried exactly once"
    message = providers.LAST_PROVIDER_ERRORS["PornHub"]
    assert "18+" in message
    assert "age verification" in message.lower()


def test_pornhub_search_excludes_cards_outside_search_results(monkeypatch):
    markup = (
        '<aside>' + _pornhub_card("phAd", "Unrelated recommendation") + '</aside>'
        '<ul id="videoSearchResult">' + _pornhub_card("phHit", "needle result") + '</ul>'
        '<ul id="recommended">' + _pornhub_card("phOther", "Popular upload") + '</ul>'
    )
    rows = _pornhub_search(monkeypatch, markup)
    assert [(row.id, row.title) for row in rows] == [("phHit", "needle result")]


def test_pornhub_search_keeps_metadata_with_its_own_card(monkeypatch):
    markup = '''<ul id="videoSearchResult">
      <li class="advertisement" title="Unrelated banner"><img src="https://example.invalid/ad.jpg"></li>
      <li class="videoblock"><a href="/view_video.php?viewkey=phFirst" title="First needle">
        <img src="https://example.invalid/first.jpg"></a><var class="duration">1:30</var></li>
      <li class="videoblock"><a href="/view_video.php?viewkey=phSecond" title="Second needle">
        <img src="https://example.invalid/second.jpg"></a><var class="duration">3:00</var></li>
    </ul>'''
    rows = _pornhub_search(monkeypatch, markup)
    assert [(row.id, row.title, row.duration_s, row.thumbnail_url) for row in rows] == [
        ("phFirst", "First needle", 90, "https://example.invalid/first.jpg"),
        ("phSecond", "Second needle", 180, "https://example.invalid/second.jpg"),
    ]


@pytest.mark.parametrize("redirect", [
    "https://www.pornhub.com/video",
    "https://www.pornhub.com/video/search?search=other&page=1",
    "https://www.pornhub.com/video/search?page=1",
    "https://example.invalid/video/search?search=needle&page=1",
])
def test_pornhub_search_rejects_responses_outside_requested_search(monkeypatch, redirect):
    markup = '<ul id="videoSearchResult">' + _pornhub_card("phWrong", "Unrelated upload") + '</ul>'
    assert _pornhub_search(monkeypatch, markup, redirect=redirect) == []
    assert "search" in providers.LAST_PROVIDER_ERRORS["PornHub"]


def test_pornhub_search_rejects_age_verification_redirect(monkeypatch):
    markup = '<ul id="videoSearchResult">' + _pornhub_card("phWrong", "Unrelated upload") + '</ul>'
    assert _pornhub_search(monkeypatch, markup, redirect="https://www.pornhub.com/age-verification") == []
    assert "18+" in providers.LAST_PROVIDER_ERRORS["PornHub"]
    assert "age verification" in providers.LAST_PROVIDER_ERRORS["PornHub"].lower()


def test_pornhub_search_reports_missing_results_container(monkeypatch):
    assert _pornhub_search(monkeypatch, _pornhub_card("phWrong", "Generic upload")) == []
    assert "results" in providers.LAST_PROVIDER_ERRORS["PornHub"]


@pytest.mark.parametrize("contents", [
    '<ul id="videoSearchResult">' + _pornhub_card("phHit", "Needle result") + '</ul>',
    '<div id="noResultBigText">No Search Results.</div>',
])
def test_pornhub_sfw_catalog_is_reported_as_age_gate_error_without_rows(monkeypatch, contents):
    markup = '<script>window.isSfwPage = true;</script>' + contents

    rows = _pornhub_search(monkeypatch, markup)

    assert rows == [], "unrelated SFW catalog rows must never be presented as matches"
    message = providers.LAST_PROVIDER_ERRORS["PornHub"]
    assert "18+" in message
    assert "SFW" in message
    assert "restricted" in message
    assert "age verification" in message.lower()
    assert "PornHub" not in getattr(providers, "LAST_PROVIDER_WARNINGS", {})


def test_pornhub_sfw_catalog_does_not_fall_back_to_legacy_api(monkeypatch):
    requested: list[str] = []
    markup = '<script>window.isSfwPage = true;</script>' + (
        '<ul id="videoSearchResult">' + _pornhub_card("phHit", "Needle result") + '</ul>'
    )

    def respond(request):
        requested.append(request.url.path)
        return httpx.Response(200, text=markup)

    monkeypatch.setattr(providers, "LAST_PROVIDER_ERRORS", {})
    monkeypatch.setattr(
        providers,
        "build_client",
        lambda **kwargs: httpx.Client(transport=httpx.MockTransport(respond)),
    )

    assert providers.search_pornhub("needle", 0, 9999, 1) == []

    assert requested == ["/video/search"]
    assert "PornHub" in providers.LAST_PROVIDER_ERRORS


def test_pornhub_full_catalog_search_clears_stale_restriction_diagnostics(monkeypatch):
    markup = '<ul id="videoSearchResult">' + _pornhub_card("phHit", "Needle result") + '</ul>'
    monkeypatch.setattr(
        providers, "LAST_PROVIDER_ERRORS", {"PornHub": "stale age gate error"}
    )
    monkeypatch.setattr(
        providers, "LAST_PROVIDER_WARNINGS", {"PornHub": "stale SFW restriction warning"}
    )
    monkeypatch.setattr(
        providers,
        "build_client",
        lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=markup))
        ),
    )

    rows = providers.search_pornhub("needle", 0, 9999, 1)

    assert [row.id for row in rows] == ["phHit"]
    assert "PornHub" not in providers.LAST_PROVIDER_ERRORS
    assert "PornHub" not in providers.LAST_PROVIDER_WARNINGS


def test_pornhub_empty_results_do_not_include_recommendations(monkeypatch):
    markup = '<ul id="videoSearchResult"></ul><aside>' + _pornhub_card("phOther", "Popular upload") + '</aside>'
    assert _pornhub_search(monkeypatch, markup) == []


@pytest.mark.parametrize("status", [200, 404])
def test_pornhub_explicit_no_matches_does_not_use_legacy_api(monkeypatch, status):
    requested = []
    markup = (
        '<script>var consent = {"pornhub.com":{"accessAgeDisclaimerPH":"essential"}};</script>'
        '<div class="noResultsWrapper"><div id="noResultBigText">No Search Results.</div></div>'
        '<ul id="singleFeedSection">' + _pornhub_card("phOther", "Popular upload") + '</ul>'
    )

    def respond(request):
        requested.append(request.url.path)
        if request.url.path == "/webmasters/search":
            return httpx.Response(200, json={"videos": [{
                "video_id": "phWrong", "title": "Unrelated upload", "duration": "2:00",
            }]})
        return httpx.Response(status, text=markup)

    monkeypatch.setattr(providers, "LAST_PROVIDER_ERRORS", {})
    monkeypatch.setattr(providers, "build_client", lambda **kwargs: httpx.Client(transport=httpx.MockTransport(respond)))

    assert providers.search_pornhub("needle", 0, 9999, 5) == []
    assert requested == ["/video/search"]
    assert "PornHub" not in providers.LAST_PROVIDER_ERRORS


def test_pornhub_stops_pagination_at_explicit_no_matches(monkeypatch):
    requested = []

    def respond(request):
        requested.append(str(request.url))
        if request.url.params.get("page") == "1":
            return httpx.Response(200, text='<ul id="videoSearchResult">' + _pornhub_card("phHit", "Needle result") + '</ul>')
        return httpx.Response(404, text='<div id="noResultBigText">No Search Results.</div>')

    monkeypatch.setattr(providers, "LAST_PROVIDER_ERRORS", {})
    monkeypatch.setattr(providers, "build_client", lambda **kwargs: httpx.Client(transport=httpx.MockTransport(respond)))

    assert [row.id for row in providers.search_pornhub("needle", 0, 9999, 20)] == ["phHit"]
    assert len(requested) == 2
    assert "PornHub" not in providers.LAST_PROVIDER_ERRORS


@pytest.mark.parametrize("status,markup", [
    (503, '<div id="noResultBigText">No Search Results.</div>'),
    (200, '<script>var template = \'<div id="noResultBigText">No Search Results.</div>\';</script>'),
])
def test_pornhub_no_matches_marker_does_not_hide_invalid_responses(status, markup):
    providers.LAST_PROVIDER_ERRORS.clear()
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, text=markup))) as client:
        assert providers._search_pornhub_html(client, "needle", 0, 9999, 1) == []
    assert "PornHub" in providers.LAST_PROVIDER_ERRORS


def test_pornhub_search_reads_html_attributes_and_nested_card_content(monkeypatch):
    markup = '''<ul id='videoSearchResult'>
      <li data-video-id='123' data-video-vkey='phHit'>
        <a href='/view_video.php?viewkey=phHit' title='Needle &amp; thread'>
          <img src='https://example.invalid/placeholder.jpg' data-mediumthumb='https://example.invalid/hit.jpg'></a>
        <var class='bgShadeEffect duration tooltipTrig'><span>2:30</span></var>
        <span class='views'><i></i><var>1,234</var></span>
        <ul><li title='Menu item'>Save</li></ul>
      </li>
    </ul>'''
    rows = _pornhub_search(monkeypatch, markup)
    assert [(row.id, row.title, row.duration_s, row.views, row.thumbnail_url) for row in rows] == [
        ("phHit", "Needle & thread", 150, 1234, "https://example.invalid/hit.jpg"),
    ]


@pytest.mark.parametrize("final_url,status", [
    ("https://example.invalid/webmasters/search?search=needle&page=1", 200),
    ("https://www.pornhub.com/webmasters/search?search=other&page=1", 200),
    ("https://www.pornhub.com/webmasters/search?search=needle&page=1", 503),
])
def test_pornhub_legacy_api_rejects_invalid_search_responses(monkeypatch, final_url, status):
    def respond(request):
        if request.url.path == "/video/search":
            return httpx.Response(200, text="Search unavailable")
        if str(request.url) != final_url:
            return httpx.Response(302, headers={"location": final_url})
        return httpx.Response(status, json={"videos": [{
            "video_id": "phWrong", "title": "Unrelated upload", "duration": "2:00",
        }]})

    monkeypatch.setattr(providers, "LAST_PROVIDER_ERRORS", {})
    monkeypatch.setattr(providers, "build_client", lambda **kwargs: httpx.Client(transport=httpx.MockTransport(respond)))
    assert providers.search_pornhub("needle", 0, 9999, 1) == []


def test_pornhub_search_ignores_external_video_links(monkeypatch):
    markup = '''<ul id="videoSearchResult"><li class="videoblock">
      <a href="https://example.invalid/view_video.php?viewkey=phWrong" title="Advertisement"></a>
    </li></ul>'''
    assert _pornhub_search(monkeypatch, markup) == []


def test_pornhub_search_keeps_duration_filters_and_deduplicates_pages(monkeypatch):
    markup = '<ul id="videoSearchResult">' + _pornhub_card("phHit", "needle result") + '</ul>'
    assert [row.id for row in _pornhub_search(monkeypatch, markup, pages=2)] == ["phHit"]
    assert providers.search_pornhub("needle", 180, 9999, 1) == []


@pytest.mark.parametrize("site,expected", [
    ("PornHub", "https://www.pornhub.com/view_video.php?viewkey=ph123"),
    ("xHamster", "https://xhamster.com/videos/xh123"),
    ("RedTube", "https://www.redtube.com/123"),
    ("Eporner", "https://www.eporner.com/video-abc123/"),
])
def test_normalize_download_target_expands_bare_ids(site, expected):
    video_id, url = providers.normalize_download_target("ph123" if site == "PornHub" else ("xh123" if site == "xHamster" else ("123" if site == "RedTube" else "abc123")), site)
    assert url == expected
    assert video_id


def test_normalize_download_target_passes_full_urls_through():
    url = "https://spankbang.com/abc/video/example"
    assert providers.normalize_download_target(url, "All Sites") == (url, url)


def test_normalize_download_target_leaves_unknown_sites_untouched():
    assert providers.normalize_download_target("bare-id", "All Sites") == ("bare-id", "bare-id")


def test_search_twitter_records_missing_cookie_error(tmp_path):
    providers.LAST_PROVIDER_ERRORS.pop("Twitter", None)
    assert providers.search_twitter("cats", 5, tmp_path / "missing.json") == []
    assert "cookie" in providers.LAST_PROVIDER_ERRORS["Twitter"].lower()


def test_search_twitter_records_missing_auth_token(tmp_path):
    import json

    cookies = tmp_path / "cookies.json"
    cookies.write_text(json.dumps({"ct0": "only-csrf"}))
    providers.LAST_PROVIDER_ERRORS.pop("Twitter", None)
    assert providers.search_twitter("cats", 5, cookies) == []
    assert "auth_token" in providers.LAST_PROVIDER_ERRORS["Twitter"]


def test_search_twitter_records_webdriver_failure(tmp_path, monkeypatch):
    import json

    import selenium.webdriver as webdriver

    cookies = tmp_path / "cookies.json"
    cookies.write_text(json.dumps({"auth_token": "token-value"}))

    class _Boom:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("chrome is not installed")

    monkeypatch.setattr(webdriver, "Chrome", _Boom)
    providers.LAST_PROVIDER_ERRORS.pop("Twitter", None)
    assert providers.search_twitter("cats", 5, cookies) == []
    assert "RuntimeError" in providers.LAST_PROVIDER_ERRORS["Twitter"]


def _hd_video(vid: str = "sx1") -> Video:
    return Video(id=vid, title="Wider web result", duration_s=120, views=10, url="https://example.test/v", site="SpankBang", quality_p=1080, is_hd=True)


def test_search_videos_searxng_disabled_records_error():
    providers.LAST_PROVIDER_ERRORS.pop("SearXNG", None)
    settings = AppSettings(searxng_enabled=False)
    assert providers.search_videos("q", "SearXNG", SearchFilters(), settings=settings) == []
    assert "SearXNG" in providers.LAST_PROVIDER_ERRORS


def test_search_videos_searxng_uses_provider_when_enabled(monkeypatch):
    monkeypatch.setattr(providers, "search_searxng", lambda query, settings, pages, limit=0: [_hd_video()])
    settings = AppSettings(searxng_enabled=True)
    rows = providers.search_videos("q", "SearXNG", SearchFilters(), settings=settings)
    assert [row.id for row in rows] == ["sx1"]


def test_search_videos_all_sites_includes_searxng_when_enabled(monkeypatch):
    monkeypatch.setattr(providers, "SITE_FN", {})
    monkeypatch.setattr(providers, "search_searxng", lambda query, settings, pages, limit=0: [_hd_video()])
    settings = AppSettings(searxng_enabled=True)
    assert [row.id for row in providers.search_videos("q", "All Sites", SearchFilters(), settings=settings)] == ["sx1"]


def test_search_videos_all_sites_skips_searxng_when_disabled(monkeypatch):
    def _fail(*args, **kwargs):
        raise AssertionError("SearXNG must not run while disabled")

    monkeypatch.setattr(providers, "SITE_FN", {})
    monkeypatch.setattr(providers, "search_searxng", _fail)
    settings = AppSettings(searxng_enabled=False)
    assert providers.search_videos("q", "All Sites", SearchFilters(), settings=settings) == []


def test_pornhub_search_sends_stored_provider_session(monkeypatch, tmp_path):
    from fetcher_core import provider_login

    monkeypatch.setattr(provider_login, "COOKIES_DIR", tmp_path / "cookies")
    provider_login.save_cookies(
        "PornHub",
        [{"name": "sessid", "value": "abc", "domain": ".pornhub.com", "path": "/"}],
    )
    seen_cookies: list[str] = []

    def respond(request):
        seen_cookies.append(request.headers.get("cookie", ""))
        if request.url.path == "/webmasters/search":
            return httpx.Response(503, text="API unavailable")
        return httpx.Response(200, text='<ul id="videoSearchResult">' + _pornhub_card("phHit", "needle result") + '</ul>')

    monkeypatch.setattr(providers, "LAST_PROVIDER_ERRORS", {})
    monkeypatch.setattr(providers, "build_client", lambda **kwargs: httpx.Client(transport=httpx.MockTransport(respond)))

    rows = providers.search_pornhub("needle", 0, 9999, 1)

    assert [row.id for row in rows] == ["phHit"]
    assert any("sessid=abc" in header for header in seen_cookies)
