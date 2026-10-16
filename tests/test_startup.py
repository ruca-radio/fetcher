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


def test_harden_state_never_raises(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("hardening unavailable")

    monkeypatch.setattr(fetchtui, "harden_state_dir", boom)
    assert fetchtui._harden_state() == []


def test_gui_diagnostics_includes_ytdlp_freshness() -> None:
    from fetcher_gui.app import _diagnostics

    diagnostics = _diagnostics(fetchtui)
    assert "yt-dlp freshness" in diagnostics
