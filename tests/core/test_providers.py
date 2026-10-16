from __future__ import annotations

from argparse import Namespace
from unittest.mock import patch

import httpx
import pytest

import fetcher_core.providers as providers
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
    "https://www.pornhub.com/age-verification",
    "https://www.pornhub.com/video/search?search=other&page=1",
    "https://www.pornhub.com/video/search?page=1",
    "https://example.invalid/video/search?search=needle&page=1",
])
def test_pornhub_search_rejects_responses_outside_requested_search(monkeypatch, redirect):
    markup = '<ul id="videoSearchResult">' + _pornhub_card("phWrong", "Unrelated upload") + '</ul>'
    assert _pornhub_search(monkeypatch, markup, redirect=redirect) == []
    assert "search" in providers.LAST_PROVIDER_ERRORS["PornHub"]


def test_pornhub_search_reports_missing_results_container(monkeypatch):
    assert _pornhub_search(monkeypatch, _pornhub_card("phWrong", "Generic upload")) == []
    assert "results" in providers.LAST_PROVIDER_ERRORS["PornHub"]


def test_pornhub_empty_results_do_not_include_recommendations(monkeypatch):
    markup = '<ul id="videoSearchResult"></ul><aside>' + _pornhub_card("phOther", "Popular upload") + '</aside>'
    assert _pornhub_search(monkeypatch, markup) == []


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
