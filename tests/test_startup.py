from __future__ import annotations

from types import SimpleNamespace

import fetchtui
from fetcher_core.models import SearchFilters


class _Widget:
    def __init__(self, value: object) -> None:
        self.value = value


class _SaveEvent(SimpleNamespace):
    button: SimpleNamespace


def _save_event() -> _SaveEvent:
    return SimpleNamespace(button=SimpleNamespace(name="save"))


def test_ytdlp_version_warning_reexport_is_callable() -> None:
    assert callable(fetchtui.ytdlp_version_warning)
    assert callable(fetchtui.ytdlp_version)


def test_cli_search_reports_catalog_warning_without_failing(monkeypatch, capsys) -> None:
    warnings = {"Eporner": "stale warning"}
    monkeypatch.setattr(fetchtui, "LAST_PROVIDER_WARNINGS", warnings, raising=False)

    def search(*args, **kwargs):
        warnings["PornHub"] = "Restricted SFW catalog"
        return [fetchtui.Video("hit", "Result", 120, 0, "https://example.test/hit", "PornHub")]

    monkeypatch.setattr(fetchtui, "search_videos", search)
    args = fetchtui.build_parser().parse_args(["search", "needle", "--site", "PornHub", "--json"])

    assert fetchtui.cli_search(args, fetchtui.AppSettings()) == 0
    captured = capsys.readouterr()
    assert "PornHub: Restricted SFW catalog" in captured.err
    assert "stale warning" not in captured.err


def test_tui_search_logs_catalog_warning_with_results(monkeypatch) -> None:
    warnings = {}
    monkeypatch.setattr(fetchtui, "LAST_PROVIDER_WARNINGS", warnings, raising=False)
    video = fetchtui.Video("hit", "Result", 120, 0, "https://example.test/hit", "PornHub")

    def search(*args):
        warnings["PornHub"] = "Restricted SFW catalog"
        return [video]

    messages = []
    populated = []
    app = SimpleNamespace(
        _log=messages.append,
        _video_matches_search_filters=lambda _: True,
        _porn_populate=lambda rows, *_: populated.extend(rows),
        call_from_thread=lambda function, *args: function(*args),
    )
    monkeypatch.setattr(fetchtui, "_search_provider", search)

    fetchtui.FetchTUI._porn_search_worker.__wrapped__(app, "needle", "PornHub", 0, 9999, 1)

    assert populated == [video]
    assert any("PornHub: Restricted SFW catalog" in message for message in messages)


def test_cli_batch_reports_catalog_warning_before_empty_selection(monkeypatch, capsys) -> None:
    warnings = {}
    monkeypatch.setattr(fetchtui, "LAST_PROVIDER_WARNINGS", warnings)

    def search(*args, **kwargs):
        warnings["PornHub"] = "Restricted SFW catalog"
        return []

    monkeypatch.setattr(fetchtui, "search_videos", search)
    args = fetchtui.build_parser().parse_args(["batch", "needle", "--site", "PornHub"])

    assert fetchtui.cli_batch(args, fetchtui.AppSettings()) == 1
    assert "PornHub: Restricted SFW catalog" in capsys.readouterr().err


def test_search_filter_screen_round_trips_exclude_censored() -> None:
    screen = fetchtui.SearchFilterScreen(SearchFilters(exclude_censored=False))
    widgets = {
        "#filter-min-duration": _Widget("0"),
        "#filter-max-duration": _Widget("0"),
        "#filter-min-quality": _Widget("720"),
        "#filter-min-rating": _Widget("0"),
        "#filter-hd-only": _Widget(False),
        "#filter-exclude-censored": _Widget(False),
        "#filter-pages": _Widget("2"),
        "#filter-exclude-phrases": _Widget(""),
        "#filter-status": _Widget(""),
    }
    screen.query_one = lambda selector, cls=None: widgets[selector]  # type: ignore[assignment]
    dismissed: list[object] = []
    screen.dismiss = lambda result: dismissed.append(result)  # type: ignore[assignment]

    screen.on_button_pressed(_save_event())

    assert dismissed and isinstance(dismissed[0], SearchFilters)
    assert dismissed[0].exclude_censored is False


def test_search_filter_screen_round_trips_exclude_censored_enabled() -> None:
    screen = fetchtui.SearchFilterScreen(SearchFilters(exclude_censored=True))
    widgets = {
        "#filter-min-duration": _Widget("0"),
        "#filter-max-duration": _Widget("0"),
        "#filter-min-quality": _Widget("720"),
        "#filter-min-rating": _Widget("0"),
        "#filter-hd-only": _Widget(False),
        "#filter-exclude-censored": _Widget(True),
        "#filter-pages": _Widget("2"),
        "#filter-exclude-phrases": _Widget(""),
        "#filter-status": _Widget(""),
    }
    screen.query_one = lambda selector, cls=None: widgets[selector]  # type: ignore[assignment]
    dismissed: list[object] = []
    screen.dismiss = lambda result: dismissed.append(result)  # type: ignore[assignment]

    screen.on_button_pressed(_save_event())

    assert dismissed and isinstance(dismissed[0], SearchFilters)
    assert dismissed[0].exclude_censored is True


def test_cli_search_accepts_searxng_site(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def search(query, site, filters, twitter_count=40, settings=None):
        captured["site"] = site
        captured["settings"] = settings
        return []

    monkeypatch.setattr(fetchtui, "search_videos", search)
    args = fetchtui.build_parser().parse_args(["search", "needle", "--site", "SearXNG"])

    assert fetchtui.cli_search(args, fetchtui.AppSettings()) == 0
    assert captured["site"] == "SearXNG"
    assert captured["settings"] is not None


def test_harden_state_never_raises(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("hardening unavailable")

    monkeypatch.setattr(fetchtui, "harden_state_dir", boom)
    assert fetchtui._harden_state() == []


def test_gui_diagnostics_includes_ytdlp_freshness() -> None:
    from fetcher_gui.app import _diagnostics

    diagnostics = _diagnostics(fetchtui)
    assert "yt-dlp freshness" in diagnostics


class _Select:
    def __init__(self, value: object = "") -> None:
        self.value = value
        self.options: list | None = None

    def set_options(self, options: list) -> None:
        self.options = options


def _settings_app_stub(settings: fetchtui.AppSettings) -> SimpleNamespace:
    return SimpleNamespace(
        settings=settings,
        available_themes={"textual-dark": object()},
        theme="textual-dark",
        _download_parallel=1,
        _tmp_dir=settings.tmp_dir,
        _plex_dir=settings.plex_dir,
        _set_settings_status=lambda _msg: None,
        _log=lambda _msg: None,
        _reload_downloaded_cache=lambda: None,
        _refresh_filter_summary=lambda: None,
        _load_files=lambda: None,
        _refresh_player_status=lambda: None,
    )


def test_settings_tab_populates_min_download_height() -> None:
    for height in (0, 1080):
        app = _settings_app_stub(fetchtui.AppSettings(min_download_height=height))
        min_height = _Widget("720")
        widgets = {
            "#settings-theme": _Select("textual-dark"),
            "#settings-thumbnails": _Widget(False),
            "#settings-video-previews": _Widget(False),
            "#settings-plex-dir": _Widget("/tmp/plex"),
            "#settings-tmp-dir": _Widget("/tmp/tmp"),
            "#settings-parallel": _Widget("2"),
            "#settings-min-height": min_height,
        }
        app.query_one = lambda selector, cls=None: widgets[selector]  # type: ignore[assignment]

        fetchtui.FetchTUI._populate_settings_controls(app)

        assert min_height.value == str(height)


def test_settings_tab_round_trips_min_download_height(monkeypatch) -> None:
    saved: list[fetchtui.AppSettings] = []
    monkeypatch.setattr(
        fetchtui, "prepare_runtime_settings", lambda settings: (settings, settings.tmp_dir, settings.plex_dir, [])
    )
    monkeypatch.setattr(fetchtui, "save_settings", lambda settings: saved.append(settings))

    for height in (0, 1080):
        app = _settings_app_stub(fetchtui.AppSettings())
        widgets = {
            "#settings-theme": _Widget("textual-dark"),
            "#settings-thumbnails": _Widget(False),
            "#settings-video-previews": _Widget(False),
            "#settings-plex-dir": _Widget("/tmp/plex"),
            "#settings-tmp-dir": _Widget("/tmp/tmp"),
            "#settings-parallel": _Widget("2"),
            "#settings-min-height": _Widget(str(height)),
            "#parallel": _Widget("2"),
        }
        app.query_one = lambda selector, cls=None: widgets[selector]  # type: ignore[assignment]

        fetchtui.FetchTUI._settings_save_btn(app)

        assert saved[-1].min_download_height == height


def test_settings_tab_rejects_non_numeric_min_download_height(monkeypatch) -> None:
    saved: list[fetchtui.AppSettings] = []
    monkeypatch.setattr(fetchtui, "save_settings", lambda settings: saved.append(settings))
    statuses: list[str] = []
    app = _settings_app_stub(fetchtui.AppSettings())
    app._set_settings_status = statuses.append  # type: ignore[method-assign]
    widgets = {
        "#settings-theme": _Widget("textual-dark"),
        "#settings-thumbnails": _Widget(False),
        "#settings-video-previews": _Widget(False),
        "#settings-plex-dir": _Widget("/tmp/plex"),
        "#settings-tmp-dir": _Widget("/tmp/tmp"),
        "#settings-parallel": _Widget("2"),
        "#settings-min-height": _Widget("nope"),
    }
    app.query_one = lambda selector, cls=None: widgets[selector]  # type: ignore[assignment]

    fetchtui.FetchTUI._settings_save_btn(app)

    assert saved == []
    assert statuses and "must be a number" in statuses[0]
