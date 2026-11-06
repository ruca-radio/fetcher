"""Append-only text log of finished downloads, shared by the TUI and GUI."""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable

from fetcher_core.filters import normalize_title
from fetcher_core.models import STATE_DIR, Video

DOWNLOADS_LOG = STATE_DIR / "downloads.txt"
LEGACY_DOWNLOADED_IDS = STATE_DIR / "downloaded_ids.txt"
# Same set as fetcher_core.download.MEDIA_EXTENSIONS. Kept here so the log
# reader does not import the downloader.
_MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
_LOCK = threading.RLock()


def downloads_log_path(legacy_ids_path: Path) -> Path:
    """Readable log that lives beside the legacy id list."""
    return Path(legacy_ids_path).with_name("downloads.txt")


def record_download(
    video: Video,
    filepath: Path | None,
    *,
    log_path: Path,
    legacy_path: Path | None,
) -> None:
    """Append one finished download. A log failure must not fail the download."""
    video_id = _field(video.id)
    if not video_id:
        return
    line = "\t".join((
        datetime.now().astimezone().isoformat(timespec="seconds"),
        _field(video.site),
        video_id,
        _field(video.title),
        _field(video.url),
        _field(filepath),
    )) + "\n"
    with _LOCK:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not log_path.exists() or log_path.stat().st_size == 0
        with log_path.open("a", encoding="utf-8") as handle:
            if new_file:
                handle.write("# recorded_at\tsite\tid\ttitle\turl\tpath\n")
            handle.write(line)
        if legacy_path is not None:
            legacy_path.parent.mkdir(parents=True, exist_ok=True)
            with legacy_path.open("a", encoding="utf-8") as handle:
                handle.write(video_id + "\n")


def video_is_downloaded(video: Video, ids: set[str], titles: set[str]) -> bool:
    if video.id and video.id in ids:
        return True
    title = normalize_title(video.title)
    return bool(title) and title in titles


def without_downloaded(videos: Iterable[Video], ids: set[str], titles: set[str]) -> list[Video]:
    return [video for video in videos if not video_is_downloaded(video, ids, titles)]


class DownloadCatalog:
    """Ids and titles already downloaded, from the log and the library tree."""

    def __init__(
        self,
        log_path: Path,
        legacy_path: Path | None = None,
        library_dirs: Iterable[Path] | None = None,
    ) -> None:
        self.log_path = Path(log_path)
        self.legacy_path = Path(legacy_path) if legacy_path is not None else None
        self.library_dirs = [Path(path) for path in (library_dirs or [])]
        self.ids: set[str] = set()
        self.titles: set[str] = set()

    def reload(self) -> None:
        ids: set[str] = set()
        titles: set[str] = set()
        with _LOCK:
            _read_log(self.log_path, ids, titles)
            if self.legacy_path is not None:
                _read_id_list(self.legacy_path, ids)
            for directory in self.library_dirs:
                _scan_library(directory, ids, titles)
            self.ids = ids
            self.titles = titles

    def record(self, video: Video, filepath: Path | None = None) -> None:
        record_download(video, filepath, log_path=self.log_path, legacy_path=self.legacy_path)
        self.reload()

    def contains(self, video: Video) -> bool:
        return video_is_downloaded(video, self.ids, self.titles)


def _field(value: object) -> str:
    return str(value or "").replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, UnicodeError):
        return []


def _remember_id(ids: set[str], video_id: str) -> None:
    token = video_id.strip()
    if token:
        ids.add(token)


def _remember_title(titles: set[str], title: str) -> None:
    token = normalize_title(title)
    if token:
        titles.add(token)


def _read_log(path: Path, ids: set[str], titles: set[str]) -> None:
    if not path.exists():
        return
    for line in _read_lines(path):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split("\t")
        if len(parts) == 1:
            _remember_id(ids, parts[0])
            continue
        if len(parts) < 3:
            continue
        _remember_id(ids, parts[2])
        if len(parts) >= 4:
            _remember_title(titles, parts[3])


def _read_id_list(path: Path, ids: set[str]) -> None:
    if not path.exists():
        return
    for line in _read_lines(path):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            _remember_id(ids, stripped.split("\t", 1)[0])


def _scan_library(directory: Path, ids: set[str], titles: set[str]) -> None:
    if not directory.exists():
        return
    try:
        paths = directory.rglob("*")
    except OSError:
        return
    for path in paths:
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        if path.suffix.lower() not in _MEDIA_EXTENSIONS or ".part" in path.name:
            continue
        video_id, title = _filename_identity(path.stem)
        if video_id:
            _remember_id(ids, video_id)
        elif title:
            # Plain filenames keep the old full-stem title match. Bracketed
            # names contribute only the id so "Untitled [id]" does not mark
            # every Untitled result as downloaded.
            _remember_title(titles, title)


def _filename_identity(stem: str) -> tuple[str, str]:
    if stem.endswith("]") and "[" in stem:
        title, _, bracket = stem.rpartition("[")
        video_id = bracket[:-1].strip()
        if video_id:
            return video_id, title.strip()
    return "", stem
