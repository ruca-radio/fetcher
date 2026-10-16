from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from .paths import resolve_runtime_paths

BASE_DIR = Path(__file__).resolve().parent.parent
RUNTIME_PATHS = resolve_runtime_paths(BASE_DIR)
STATE_DIR = RUNTIME_PATHS.state_dir
MUTABLE_ROOT = RUNTIME_PATHS.mutable_root
CACHE_DIR = RUNTIME_PATHS.cache_dir
DEFAULT_TMP_DIR = RUNTIME_PATHS.tmp_dir
PREVIEW_CACHE_DIR = RUNTIME_PATHS.preview_cache_dir
DEFAULT_OUTPUT_DIR = Path.home() / "Downloads" / "fetcher"
DEFAULT_SEARXNG_URL = "https://search.studioddx.com"
DEFAULT_EXCLUDE_PHRASES = ""
DEFAULT_MIN_QUALITY = "720"

SortKey = Literal["title", "duration", "site", "views"]
FileSortKey = Literal["name", "size", "modified", "path"]
QualityFilter = Literal["any", "720", "1080"]


@dataclass
class Video:
    id: str
    title: str
    duration_s: int
    views: int
    url: str
    site: str
    thumbnail_url: Optional[str] = None
    rating_percent: Optional[int] = None
    quality_p: Optional[int] = None
    is_hd: Optional[bool] = None
    selected: bool = False
    keywords: str = ""

    @property
    def duration_str(self) -> str:
        minutes, seconds = divmod(self.duration_s, 60)
        return f"{minutes}:{seconds:02d}"

    @property
    def views_str(self) -> str:
        if self.views >= 1_000_000:
            return f"{self.views / 1_000_000:.1f}M"
        if self.views >= 1_000:
            return f"{self.views / 1_000:.0f}K"
        return str(self.views)


@dataclass
class DownloadJob:
    video: Video
    status: str = "queued"
    progress: float = 0.0
    speed: str = ""
    eta: str = ""
    filepath: Optional[Path] = None
    file_size: int = 0
    downloaded_bytes: int = 0
    proc: Optional[subprocess.Popen] = None
    worker_started: bool = False


@dataclass
class SearchFilters:
    min_duration_min: int = 0
    max_duration_min: int = 0
    min_quality: QualityFilter = DEFAULT_MIN_QUALITY  # type: ignore[assignment]
    min_rating: int = 0
    hd_only: bool = False
    pages: int = 2
    exclude_phrases: str = DEFAULT_EXCLUDE_PHRASES
    exclude_censored: bool = True

    @property
    def min_duration_s(self) -> int:
        return max(0, self.min_duration_min) * 60

    @property
    def max_duration_s(self) -> int:
        return (24 * 60 * 60) if self.max_duration_min <= 0 else self.max_duration_min * 60


@dataclass
class DownloadPolicy:
    min_height: int = 720
    preview_ratio: float = 0.6
    preview_min_seconds: int = 90


@dataclass
class AppSettings:
    theme: str = "textual-dark"
    thumbnails_enabled: bool = True
    video_previews_enabled: bool = True
    plex_dir: str = str(DEFAULT_OUTPUT_DIR)
    tmp_dir: str = str(DEFAULT_TMP_DIR)
    parallel_downloads: int = 3
    searxng_url: str = DEFAULT_SEARXNG_URL
    searxng_enabled: bool = False
    searxng_categories: str = "adult"
    naming_template: str = "%(title).180B [%(id)s].%(ext)s"
    organize_by_site: bool = False
    concurrent_fragments: int = 4
    min_download_height: int = 720
    eporner_preview_ratio: float = 0.6
    eporner_preview_min_seconds: int = 90
    search_filters: SearchFilters = field(default_factory=SearchFilters)

    @property
    def plex_path(self) -> Path:
        return Path(self.plex_dir).expanduser()

    @property
    def tmp_path(self) -> Path:
        return Path(self.tmp_dir).expanduser()

    def download_policy(self) -> DownloadPolicy:
        return DownloadPolicy(
            min_height=self.min_download_height,
            preview_ratio=self.eporner_preview_ratio,
            preview_min_seconds=self.eporner_preview_min_seconds,
        )


@dataclass
class FileEntry:
    path: Path
    size: int
    modified_ts: float

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def modified_str(self) -> str:
        return datetime.fromtimestamp(self.modified_ts).strftime("%Y-%m-%d %H:%M")
