from __future__ import annotations

from types import SimpleNamespace

import pytest

import fetchtui
from fetcher_core.models import DownloadJob, SearchFilters, Video


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


def _runtime_dirs_stub(monkeypatch, tmp_path):
    monkeypatch.setattr(
        fetchtui, "resolve_runtime_dirs",
        lambda settings: (tmp_path / "tmp", tmp_path / "out", []),
    )


def test_cli_download_uses_configured_policy_by_default(monkeypatch, tmp_path, capsys) -> None:
    _runtime_dirs_stub(monkeypatch, tmp_path)
    captured = {}

    def fake_download(video, tmp_dir, output_dir, policy):
        captured["policy"] = policy
        return SimpleNamespace(status="done", filepath=tmp_path / "out" / "clip.mkv")

    monkeypatch.setattr(fetchtui, "download_one", fake_download)
    args = fetchtui.build_parser().parse_args(["download", "https://example.test/v"])

    assert fetchtui.cli_download(args, fetchtui.AppSettings(min_download_height=1080)) == 0
    assert captured["policy"].min_height == 1080


def test_cli_download_min_height_overrides_the_configured_floor(monkeypatch, tmp_path) -> None:
    _runtime_dirs_stub(monkeypatch, tmp_path)
    captured = {}

    def fake_download(video, tmp_dir, output_dir, policy):
        captured["policy"] = policy
        return SimpleNamespace(status="done", filepath=tmp_path / "out" / "clip.mkv")

    monkeypatch.setattr(fetchtui, "download_one", fake_download)
    args = fetchtui.build_parser().parse_args(
        ["download", "https://example.test/v", "--min-height", "1080"]
    )

    assert fetchtui.cli_download(args, fetchtui.AppSettings(min_download_height=720)) == 0
    assert captured["policy"].min_height == 1080


def test_cli_batch_min_height_zero_disables_the_floor(monkeypatch, tmp_path) -> None:
    _runtime_dirs_stub(monkeypatch, tmp_path)
    video = Video("v1", "clip", 60, 0, "https://example.test/v", "Eporner")
    monkeypatch.setattr(fetchtui, "_cli_search_results", lambda args, settings: [video])
    captured = {}

    def fake_batch(videos, tmp_dir, output_dir, parallel, policy):
        captured["policy"] = policy
        return [DownloadJob(video=video, status="done")]

    monkeypatch.setattr(fetchtui, "run_batch_download", fake_batch)
    args = fetchtui.build_parser().parse_args(["batch", "query", "--min-height", "0"])

    assert fetchtui.cli_batch(args, fetchtui.AppSettings(min_download_height=720)) == 0
    assert captured["policy"].min_height == 0


@pytest.mark.parametrize("value", ["-1", "5000", "nope", "1.5"])
def test_cli_min_height_rejects_invalid_values(value) -> None:
    with pytest.raises(SystemExit):
        fetchtui.build_parser().parse_args(["download", "https://example.test/v", "--min-height", value])


def test_cli_login_opens_browser_sign_in(monkeypatch, capsys) -> None:
    calls = {}

    class Result:
        saved = True
        message = "PornHub: signed in; saved 2 session cookie(s)."

    def fake_sign_in(site, *, timeout_s=600, on_status=None, **kwargs):
        calls["site"] = site
        calls["timeout"] = timeout_s
        if on_status is not None:
            on_status("opening Chrome")
        return Result()

    monkeypatch.setattr(fetchtui.provider_login, "sign_in", fake_sign_in)
    args = fetchtui.build_parser().parse_args(["login", "--site", "PornHub", "--timeout", "30"])

    assert fetchtui.cli_login(args, fetchtui.AppSettings()) == 0
    assert calls == {"site": "PornHub", "timeout": 30}
    captured = capsys.readouterr()
    assert "opening Chrome" in captured.err
    assert "signed in" in captured.out


def test_cli_login_failure_returns_nonzero(monkeypatch, capsys) -> None:
    class Result:
        saved = False
        message = "PornHub: no session cookies were captured; nothing saved."

    monkeypatch.setattr(fetchtui.provider_login, "sign_in", lambda *args, **kwargs: Result())
    args = fetchtui.build_parser().parse_args(["login", "--site", "PornHub"])

    assert fetchtui.cli_login(args, fetchtui.AppSettings()) == 1
    assert "nothing saved" in capsys.readouterr().err


def test_cli_logout_clears_stored_session(monkeypatch, capsys) -> None:
    monkeypatch.setattr(fetchtui.provider_login, "clear_cookies", lambda site: True)
    args = fetchtui.build_parser().parse_args(["logout", "--site", "RedTube"])

    assert fetchtui.cli_logout(args, fetchtui.AppSettings()) == 0
    assert "session cleared" in capsys.readouterr().out


def test_tui_settings_shows_provider_signin_status(monkeypatch) -> None:
    class _StaticStub:
        def __init__(self):
            self.value = ""

        def update(self, value):
            self.value = value

    class _ButtonStub:
        def __init__(self):
            self.disabled = None

    class _AppStub:
        def __init__(self):
            self.widgets = {}
            for site in fetchtui.provider_login.SIGN_IN_SITES:
                slug = site.lower()
                self.widgets[f"#settings-signin-{slug}"] = _StaticStub()
                self.widgets[f"#btn-signout-{slug}"] = _ButtonStub()

        def query_one(self, selector, *args, **kwargs):
            return self.widgets[selector]

    monkeypatch.setattr(
        fetchtui.provider_login, "cookie_count",
        lambda site: 3 if site == "PornHub" else 0,
    )
    app = _AppStub()

    fetchtui.FetchTUI._refresh_provider_signin_status(app)

    assert app.widgets["#settings-signin-pornhub"].value == "Signed in (3 cookies)"
    assert app.widgets["#btn-signout-pornhub"].disabled is False
    assert app.widgets["#settings-signin-redtube"].value == "Not signed in"
    assert app.widgets["#btn-signout-redtube"].disabled is True


def test_tui_provider_signin_finished_resets_controls(monkeypatch) -> None:
    class _StaticStub:
        def __init__(self):
            self.value = ""

        def update(self, value):
            self.value = value

    class _ButtonStub:
        def __init__(self):
            self.label = "x"
            self.disabled = None

    class _AppStub:
        def __init__(self):
            self.widgets = {}
            for site in fetchtui.provider_login.SIGN_IN_SITES:
                slug = site.lower()
                self.widgets[f"#settings-signin-{slug}"] = _StaticStub()
                self.widgets[f"#btn-signin-{slug}"] = _ButtonStub()
                self.widgets[f"#btn-signout-{slug}"] = _ButtonStub()
            self.statuses = []
            self.logs = []
            self._signin_site = "PornHub"
            self._signin_cancel = None

        def query_one(self, selector, *args, **kwargs):
            return self.widgets[selector]

        def _set_settings_status(self, message):
            self.statuses.append(message)

        def _log(self, message):
            self.logs.append(message)

        def _refresh_provider_signin_status(self):
            return fetchtui.FetchTUI._refresh_provider_signin_status(self)

        def _provider_signin_buttons(self):
            return fetchtui.FetchTUI._provider_signin_buttons(self)

    monkeypatch.setattr(fetchtui.provider_login, "cookie_count", lambda site: 4 if site == "PornHub" else 0)
    app = _AppStub()
    result = SimpleNamespace(message="PornHub: signed in; saved 4 session cookie(s).")

    fetchtui.FetchTUI._provider_signin_finished(app, result)

    assert app._signin_site is None
    assert all(app.widgets[f"#btn-signin-{site.lower()}"].label == "Sign in" for site in fetchtui.provider_login.SIGN_IN_SITES)
    assert app.widgets["#settings-signin-pornhub"].value == "Signed in (4 cookies)"
    assert app.statuses == ["PornHub: signed in; saved 4 session cookie(s)."]
    assert app.logs == ["PornHub: signed in; saved 4 session cookie(s)."]


def test_tui_provider_signout_clears_session(monkeypatch) -> None:
    class _StaticStub:
        def __init__(self):
            self.value = "Signed in (2 cookies)"

        def update(self, value):
            self.value = value

    class _ButtonStub:
        def __init__(self):
            self.label = "Sign in"
            self.disabled = None

    class _AppStub:
        def __init__(self):
            self.widgets = {}
            for site in fetchtui.provider_login.SIGN_IN_SITES:
                slug = site.lower()
                self.widgets[f"#settings-signin-{slug}"] = _StaticStub()
                self.widgets[f"#btn-signin-{slug}"] = _ButtonStub()
                self.widgets[f"#btn-signout-{slug}"] = _ButtonStub()
            self.statuses = []
            self.logs = []

        def query_one(self, selector, *args, **kwargs):
            return self.widgets[selector]

        def _set_settings_status(self, message):
            self.statuses.append(message)

        def _log(self, message):
            self.logs.append(message)

        def _refresh_provider_signin_status(self):
            return fetchtui.FetchTUI._refresh_provider_signin_status(self)

        def _provider_signin_buttons(self):
            return fetchtui.FetchTUI._provider_signin_buttons(self)

    monkeypatch.setattr(fetchtui.provider_login, "cookie_count", lambda site: 0)
    monkeypatch.setattr(fetchtui.provider_login, "clear_cookies", lambda site: True)
    app = _AppStub()

    fetchtui.FetchTUI._signout_provider(app, "RedTube")

    assert app.statuses == ["RedTube: session cleared."]
    assert app.logs == ["RedTube: session cleared."]
    assert app.widgets["#settings-signin-redtube"].value == "Not signed in"
    assert app.widgets["#btn-signout-redtube"].disabled is True


def test_cli_logout_survives_corrupt_cookie_directory(monkeypatch, tmp_path, capsys) -> None:
    from fetcher_core import provider_login

    monkeypatch.setattr(provider_login, "COOKIES_DIR", tmp_path / "cookies")
    provider_login.cookie_path("PornHub").mkdir(parents=True)
    args = fetchtui.build_parser().parse_args(["logout", "--site", "PornHub"])

    assert fetchtui.cli_logout(args, fetchtui.AppSettings()) == 0
    assert "session cleared" in capsys.readouterr().out


def test_cli_login_reports_start_failure_without_traceback(monkeypatch, capsys) -> None:
    def explode(*args, **kwargs):
        raise RuntimeError("chrome missing")

    monkeypatch.setattr(fetchtui.provider_login, "sign_in", explode)
    args = fetchtui.build_parser().parse_args(["login", "--site", "PornHub"])

    assert fetchtui.cli_login(args, fetchtui.AppSettings()) == 1
    assert "sign-in failed" in capsys.readouterr().err


def test_cli_session_status_reports_each_provider(monkeypatch, capsys) -> None:
    messages = {
        "PornHub": ("valid", "PornHub: the stored session is logged in."),
        "RedTube": ("unknown", "RedTube: the site served its age wall."),
        "xHamster": ("missing", "xHamster: no stored session; sign in first."),
        "Twitter": ("invalid", "Twitter: the stored session is not logged in."),
    }

    def fake_test(site):
        status, message = messages[site]
        return SimpleNamespace(site=site, status=status, message=message)

    monkeypatch.setattr(fetchtui.provider_login, "test_session", fake_test)
    args = fetchtui.build_parser().parse_args(["session-status"])

    assert fetchtui.cli_session_status(args, fetchtui.AppSettings()) == 1
    captured = capsys.readouterr()
    assert "PornHub: the stored session is logged in." in captured.out
    assert "RedTube: the site served its age wall." in captured.out

    monkeypatch.setattr(
        fetchtui.provider_login, "test_session",
        lambda site: SimpleNamespace(site=site, status="valid", message=f"{site}: ok."),
    )
    args = fetchtui.build_parser().parse_args(["session-status", "--site", "PornHub"])
    assert fetchtui.cli_session_status(args, fetchtui.AppSettings()) == 0
    assert capsys.readouterr().out.strip() == "PornHub: ok."


def test_tui_provider_session_test_finished_updates_status() -> None:
    class _StaticStub:
        def __init__(self):
            self.value = ""

        def update(self, value):
            self.value = value

    class _AppStub:
        def __init__(self):
            self.widgets = {"#settings-signin-pornhub": _StaticStub()}
            self.statuses = []
            self.logs = []

        def query_one(self, selector, *args, **kwargs):
            return self.widgets[selector]

        def _set_settings_status(self, message):
            self.statuses.append(message)

        def _log(self, message):
            self.logs.append(message)

    app = _AppStub()
    check = SimpleNamespace(site="PornHub", status="valid", message="PornHub: the stored session is logged in.")

    fetchtui.FetchTUI._provider_session_test_finished(app, check)

    assert app.widgets["#settings-signin-pornhub"].value == check.message
    assert app.statuses == [check.message]
    assert app.logs == [check.message]


def test_tui_twitter_signin_finished_updates_login_button(monkeypatch) -> None:
    class _ButtonStub:
        def __init__(self):
            self.label = "Login"
            self.variant = "warning"
            self.disabled = True

    class _StaticStub:
        def __init__(self):
            self.value = ""

        def update(self, value):
            self.value = value

    class _AppStub:
        def __init__(self):
            self._tw_signin_active = True
            self._tw_logged_in = False
            self.button = _ButtonStub()
            self.status = _StaticStub()
            self.logs = []

        def query_one(self, selector, *args, **kwargs):
            return self.button if selector == "#tw-btn-login" else self.status

        def _log(self, message):
            self.logs.append(message)

    monkeypatch.setattr(fetchtui.provider_login, "twitter_authenticated", lambda: True)
    app = _AppStub()
    result = SimpleNamespace(saved=True, message="Twitter: signed in; saved 5 session cookie(s).")

    fetchtui.FetchTUI._tw_signin_finished(app, result)

    assert app._tw_signin_active is False
    assert app._tw_logged_in is True
    assert app.button.label == "✓ Logged in"
    assert app.button.variant == "success"
    assert app.button.disabled is False
    assert app.status.value == result.message
    assert app.logs == [result.message]
