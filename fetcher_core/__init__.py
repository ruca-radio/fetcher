"""UI-independent Fetcher application primitives."""

from .models import AppSettings, DownloadJob, FileEntry, SearchFilters, Video
from .settings import SettingsLoadResult, SettingsStore

__all__ = [
    "AppSettings",
    "DownloadJob",
    "FileEntry",
    "SearchFilters",
    "SettingsLoadResult",
    "SettingsStore",
    "Video",
]
