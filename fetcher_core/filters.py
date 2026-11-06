from __future__ import annotations

import re

from fetcher_core.models import SearchFilters, Video


CENSORED_MEDIA_MARKERS = ("/sfw/", "/censored/", "/blurred/")
EPORNER_CENSORED_MARKERS = ("censored", "mosaic")
EPORNER_UNCENSORED_MARKERS = ("uncensored", "mosaic removed", "mosaic-free", "mosaic free", "no mosaic", "without mosaic", "decensored", "de-censored")


def is_known_censored_media_url(url: str) -> bool:
    lowered = url.lower()
    return any(marker in lowered for marker in CENSORED_MEDIA_MARKERS)


def looks_censored(text: str) -> bool:
    """Heuristic for EPorner titles/keywords that mark censored or mosaic uploads."""
    lowered = text.lower()
    if any(marker in lowered for marker in EPORNER_UNCENSORED_MARKERS):
        return False
    return any(marker in lowered for marker in EPORNER_CENSORED_MARKERS)


def normalize_title(title: str) -> str:
    """Lowercase, strip non-alphanumeric for fuzzy title matching."""
    return re.sub(r"[^a-z0-9]", "", title.lower())


def split_exclude_phrases(raw: str) -> list[str]:
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def video_matches_filters(video: Video, filters: SearchFilters) -> bool:
    if video.duration_s < filters.min_duration_s:
        return False
    if filters.max_duration_min > 0 and video.duration_s > filters.max_duration_s:
        return False
    if filters.min_rating > 0 and video.rating_percent is not None and video.rating_percent < filters.min_rating:
        return False
    if filters.hd_only and video.is_hd is False:
        return False
    if filters.min_quality != "any":
        min_quality = int(filters.min_quality)
        if video.quality_p is not None and video.quality_p < min_quality:
            return False
        if video.quality_p is None and video.is_hd is False and min_quality >= 720:
            return False
    if (
        filters.exclude_censored
        and video.site == "Eporner"
        and looks_censored(f"{video.title} {getattr(video, 'keywords', '')}")
    ):
        return False
    title_lower = video.title.lower()
    for phrase in split_exclude_phrases(filters.exclude_phrases):
        if phrase in title_lower:
            return False
    return True


def dedupe_videos(videos: list[Video]) -> list[Video]:
    seen_ids: set[tuple[str, str]] = set()
    seen_titles: set[str] = set()
    unique: list[Video] = []
    for video in videos:
        norm = normalize_title(video.title)
        # Numeric ids repeat across providers. Identity is the provider plus the id.
        identity = (video.site, video.id)
        if identity in seen_ids:
            continue
        if norm and norm in seen_titles:
            continue
        seen_ids.add(identity)
        if norm:
            seen_titles.add(norm)
        unique.append(video)
    return unique
