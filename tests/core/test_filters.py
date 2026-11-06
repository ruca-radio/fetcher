from __future__ import annotations

import pytest

from fetcher_core.filters import (
    dedupe_videos,
    is_known_censored_media_url,
    looks_censored,
    video_matches_filters,
)
from fetcher_core.models import SearchFilters, Video


def make_video(**overrides) -> Video:
    data = {
        "id": "id1",
        "title": "Normal Title",
        "duration_s": 10 * 60,
        "views": 100,
        "url": "https://example.com/video/id1",
        "site": "Example",
        "rating_percent": 90,
        "quality_p": 720,
        "is_hd": True,
    }
    data.update(overrides)
    return Video(**data)


def test_video_matches_filters_duration_min_max_bounds() -> None:
    filters = SearchFilters(min_duration_min=5, max_duration_min=10, min_quality="any")

    assert video_matches_filters(make_video(duration_s=5 * 60), filters) is True
    assert video_matches_filters(make_video(duration_s=(10 * 60)), filters) is True
    assert video_matches_filters(make_video(duration_s=(5 * 60) - 1), filters) is False
    assert video_matches_filters(make_video(duration_s=(10 * 60) + 1), filters) is False


def test_video_matches_filters_hd_only_only_rejects_explicit_false() -> None:
    filters = SearchFilters(hd_only=True, min_quality="any")

    assert video_matches_filters(make_video(is_hd=False), filters) is False
    assert video_matches_filters(make_video(is_hd=None), filters) is True


def test_video_matches_filters_min_quality_current_behavior() -> None:
    filters = SearchFilters(min_quality="720")

    assert video_matches_filters(make_video(quality_p=480, is_hd=True), filters) is False
    assert video_matches_filters(make_video(quality_p=720, is_hd=False), filters) is True
    assert video_matches_filters(make_video(quality_p=None, is_hd=False), filters) is False
    assert video_matches_filters(make_video(quality_p=None, is_hd=None), filters) is True


def test_video_matches_filters_min_rating_ignores_missing_rating() -> None:
    filters = SearchFilters(min_rating=80, min_quality="any")

    assert video_matches_filters(make_video(rating_percent=None), filters) is True
    assert video_matches_filters(make_video(rating_percent=79), filters) is False
    assert video_matches_filters(make_video(rating_percent=80), filters) is True


def test_video_matches_filters_exclude_phrase_case_insensitive_substring() -> None:
    filters = SearchFilters(exclude_phrases="foo, BIG Phrase", min_quality="any")

    assert video_matches_filters(make_video(title="a big phrase appears"), filters) is False
    assert video_matches_filters(make_video(title="unrelated title"), filters) is True


def test_dedupe_videos_drops_duplicate_ids_and_normalized_titles_preserving_order() -> None:
    videos = [
        make_video(id="1", title="Alpha One"),
        make_video(id="1", title="Different Title"),
        make_video(id="2", title="Alpha-One!!!"),
        make_video(id="3", title="Beta Two"),
    ]

    assert dedupe_videos(videos) == [videos[0], videos[3]]


def test_dedupe_videos_keeps_the_same_id_from_different_sites() -> None:
    first = make_video(id="1", site="PornHub", title="Alpha")
    second = make_video(id="1", site="Eporner", title="Beta")

    assert dedupe_videos([first, second]) == [first, second]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("273 7 605 [censored]", True),
        ("[Reducing Mosaic] x", True),
        ("JAV Mosaic Removed UNCENSORED", False),
        ("blurred lines of a busty sinner", False),
        ("normal title", False),
    ],
)
def test_looks_censored_truth_table(text: str, expected: bool) -> None:
    assert looks_censored(text) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://cdn.example.com/sfw/video.mp4", True),
        ("https://cdn.example.com/censored/video.mp4", True),
        ("https://cdn.example.com/blurred/video.mp4", True),
        ("https://cdn.example.com/media/video.mp4", False),
    ],
)
def test_is_known_censored_media_url(url: str, expected: bool) -> None:
    assert is_known_censored_media_url(url) is expected
