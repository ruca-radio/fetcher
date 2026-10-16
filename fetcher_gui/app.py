from __future__ import annotations

import importlib
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

from PySide6.QtWidgets import QApplication, QMessageBox

from fetcher_core.jobs import JobHistoryStore
from fetcher_core.models import AppSettings, PREVIEW_CACHE_DIR, SearchFilters, STATE_DIR
from fetcher_core.preview import PreviewCache
from fetcher_core.settings import SettingsStore, is_sftp_storage, parse_sftp_storage, prepare_runtime_settings
from fetcher_core.storage import LocalStorage, SftpStorage

from .main_window import DaemonTaskPool, MainWindow
from .settings import apply_theme


class SearchAdapter:
    def __init__(self, module, settings: AppSettings) -> None:
        self.module = module
        self.settings = settings

    def search(self, query: str, provider: str, values: dict[str, object]):
        base = self.settings.search_filters
        filters = SearchFilters(
            min_duration_min=int(values.get("min_duration", base.min_duration_min) or 0),
            max_duration_min=int(values.get("max_duration", base.max_duration_min) or 0),
            min_quality=str(values.get("quality", base.min_quality) or base.min_quality),
            min_rating=int(values.get("min_rating", base.min_rating) or 0),
            hd_only=bool(values.get("hd_only", base.hd_only)),
            pages=int(values.get("pages", base.pages) or base.pages),
            exclude_phrases=str(values.get("exclude", base.exclude_phrases) or ""),
            exclude_censored=bool(values.get("exclude_censored", base.exclude_censored)),
        )
        errors = getattr(self.module, "LAST_PROVIDER_ERRORS", None)
        if isinstance(errors, dict):
            errors.clear()
        results = list(self.module.search_videos(query, provider, filters, twitter_count=40) or [])
        warning_parts = []
        if isinstance(errors, dict):
            warning_parts.extend(f"{site}: {message}" for site, message in sorted(errors.items()) if message)
        if not results:
            warning_parts.append("No videos matched this search.")
        return {"results": results, "warning": " ".join(warning_parts)}


class PreviewAdapter:
    def __init__(self, module) -> None:
        self.module = module

    def resolve(self, video):
        urls, error = self.module.resolve_stream_urls(video)
        if urls:
            return urls[0]
        raise RuntimeError(error or "No playable media stream was found.")


class CachedStorage:
    def __init__(self, backend, cache: PreviewCache) -> None:
        self.backend = backend
        self.cache = cache

    def __getattr__(self, name: str):
        return getattr(self.backend, name)

    def materialize_preview(self, relpath: PurePosixPath) -> Path:
        return self.cache.materialize(
            self.backend.identity,
            relpath,
            lambda destination: self.backend.copy_out(relpath, destination, overwrite=False),
        )


class ProductionSettingsService:
    def __init__(self, store: SettingsStore, settings: AppSettings) -> None:
        self.store = store
        self.settings = settings

    def save(self, settings: AppSettings) -> None:
        self.store.save(settings)
        self.settings = self.store.validate(settings)

    def load_gui_preferences(self) -> dict[str, object]:
        return self.store.load_gui_preferences()

    def save_gui_preferences(self, preferences: dict[str, object]) -> dict[str, object]:
        return self.store.save_gui_preferences(preferences)

    def save_all(self, settings: AppSettings, preferences: dict[str, object]) -> dict[str, object]:
        normalized = self.store.save_all(settings, preferences)
        self.settings = self.store.validate(settings)
        return normalized

    def validate(self, settings: AppSettings, preferences: dict[str, object]):
        validated = self.store.validate(settings)
        validate_preferences = getattr(self.store, "validate_gui_preferences", None)
        normalized = validate_preferences(preferences) if validate_preferences is not None else dict(preferences)
        SettingsPageValidator.validate_writable_paths(validated, normalized)
        return validated, normalized


class SettingsPageValidator:
    @staticmethod
    def validate_writable_paths(settings: AppSettings, preferences: dict[str, object]) -> None:
        staging = str(preferences.get("staging_dir") or "").strip()
        if not staging:
            raise ValueError("Staging directory cannot be empty")
        paths = [Path(settings.tmp_dir), Path(staging)]
        if not is_sftp_storage(settings.plex_dir):
            paths.append(Path(settings.plex_dir))
        for path in paths:
            if not str(path):
                raise ValueError("Configured directory cannot be empty")
            path = path.expanduser()
            path.mkdir(parents=True, exist_ok=True)
            probe = None
            try:
                with tempfile.NamedTemporaryFile(prefix=".fetcher-gui-write-test-", dir=path, delete=False) as handle:
                    probe = Path(handle.name)
            except Exception as exc:
                raise ValueError(f"Directory is not writable: {path}: {exc}") from exc
            finally:
                if probe is not None:
                    probe.unlink(missing_ok=True)


def _diagnostics(module) -> dict[str, str]:
    commands = {name: shutil.which(name) or "not found" for name in ("ffmpeg", "ffprobe", "mpv", "sftp", "aria2c")}
    if commands["aria2c"] == "not found":
        sibling = Path(sys.executable).parent / ("aria2c.exe" if sys.platform == "win32" else "aria2c")
        if sibling.is_file():
            commands["aria2c"] = str(sibling)
    ytdlp, error = module.resolve_ytdlp_command()
    commands["yt-dlp"] = " ".join(ytdlp) if ytdlp else (error or "not found")
    warning_fn = getattr(module, "ytdlp_version_warning", None)
    if callable(warning_fn):
        try:
            freshness = warning_fn()
        except Exception as exc:
            freshness = f"yt-dlp version check failed: {exc}"
        if freshness:
            commands["yt-dlp freshness"] = str(freshness)
        else:
            version_fn = getattr(module, "ytdlp_version", None)
            version = None
            if callable(version_fn):
                try:
                    version = version_fn()
                except Exception:
                    version = None
            commands["yt-dlp freshness"] = (
                f"yt-dlp {version} is up to date" if version else "yt-dlp is up to date"
            )
    return commands


def compose_services(*, state_dir: Path = STATE_DIR, fetchtui_module=None) -> dict[str, Any]:
    """Compose production services without importing the TUI until GUI startup."""

    module = fetchtui_module or importlib.import_module("fetchtui")
    state_dir = Path(state_dir)
    store = SettingsStore(state_dir / "settings.json", defaults=AppSettings())
    custom_empty_state = state_dir != STATE_DIR and not store.path.exists()
    loaded = store.load()
    if loaded.errors:
        raise RuntimeError("; ".join(loaded.errors))
    initial_settings = loaded.settings
    if custom_empty_state:
        initial_settings = replace(
            initial_settings,
            tmp_dir=str(state_dir / "tmp"),
            plex_dir=str(state_dir / "storage"),
        )
    settings, tmp_dir, output_dir, warnings = prepare_runtime_settings(initial_settings)
    preferences = store.load_gui_preferences()
    staging_dir = (
        state_dir / "downloads"
        if custom_empty_state
        else Path(str(preferences["staging_dir"])).expanduser()
    )
    staging = LocalStorage(staging_dir)

    cache_root = PREVIEW_CACHE_DIR if state_dir == STATE_DIR else state_dir / "preview-cache"
    cache = PreviewCache(
        cache_root,
        max_bytes=int(preferences["cache_size_mib"]) * 1024 * 1024,
        max_age=int(preferences["cache_age_days"]) * 24 * 60 * 60,
    )
    if is_sftp_storage(settings.plex_dir):
        spec = parse_sftp_storage(settings.plex_dir)
        if spec is None:
            raise RuntimeError("The configured SFTP storage target is invalid.")
        remote = SftpStorage(
            spec["host"], PurePosixPath(spec["path"]), user=spec["user"],
            port=int(spec["port"]) if spec["port"] else None,
        )
        plex_storage = CachedStorage(remote, cache)
    else:
        plex_storage = LocalStorage(output_dir)

    manager = module.create_download_manager(
        settings, tmp_dir, staging_dir,
        history_store=JobHistoryStore(state_dir / "jobs.json"),
    )
    return {
        "search_service": SearchAdapter(module, settings),
        "preview_service": PreviewAdapter(module),
        "download_manager": manager,
        "local_storage": staging,
        "plex_storage": plex_storage,
        "settings_service": ProductionSettingsService(store, settings),
        "diagnostics": lambda: _diagnostics(module),
        "thread_pool": DaemonTaskPool(),
        "startup_warnings": tuple(warnings),
        "startup_theme": str(preferences["theme"]),
    }


def run_gui(argv: list[str] | None = None, **services) -> int:
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    try:
        if not services:
            services = compose_services()
        warnings = services.pop("startup_warnings", ())
        apply_theme(str(services.pop("startup_theme", "System")))
        window = MainWindow(**services)
        if warnings:
            window.statusBar().showMessage("; ".join(warnings))
    except Exception as exc:
        message = f"Fetcher could not start: {exc}\nCheck settings.json in the Fetcher data directory and required tools."
        print(message, file=sys.stderr)
        QMessageBox.critical(None, "Fetcher startup failed", message)
        return 1
    app.aboutToQuit.connect(window.close)
    window.show()
    return app.exec()
