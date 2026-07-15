from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional

from .models import (
    AppSettings,
    DEFAULT_EXCLUDE_PHRASES,
    DEFAULT_MIN_QUALITY,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_SEARXNG_URL,
    DEFAULT_TMP_DIR,
    MUTABLE_ROOT,
    STATE_DIR,
    SearchFilters,
)

DEFAULT_LOCAL_COMPLETED_DIR = STATE_DIR / "downloads"
DEFAULT_GUI_PREFERENCES: dict[str, object] = {
    "staging_dir": str(DEFAULT_LOCAL_COMPLETED_DIR),
    "cache_size_mib": 512,
    "cache_age_days": 14,
    "theme": "System",
}


KNOWN_KEYS = {field.name for field in AppSettings.__dataclass_fields__.values()}


@dataclass(frozen=True)
class SettingsLoadResult:
    settings: AppSettings
    unknown: dict[str, Any]
    errors: tuple[str, ...] = ()


def _contains_control_characters(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _expand_portable_path(value: str) -> str:
    if value == "." or value.startswith("./"):
        relative = value.removeprefix("./")
        return str((MUTABLE_ROOT / relative).resolve())
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = MUTABLE_ROOT / path
    return str(path.resolve())


def _portable_path(value: str) -> str:
    path = Path(value).expanduser().resolve()
    try:
        relative = path.relative_to(MUTABLE_ROOT.resolve())
        return "." if not relative.parts else f"./{relative.as_posix()}"
    except ValueError:
        pass
    try:
        relative = path.relative_to(Path.home().resolve())
        return "~" if not relative.parts else f"~/{relative.as_posix()}"
    except ValueError:
        return str(path)


def _clamped_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _normalize_gui_preferences(preferences: object) -> dict[str, object]:
    raw = preferences if isinstance(preferences, dict) else {}
    staging_raw = str(raw.get("staging_dir") or DEFAULT_GUI_PREFERENCES["staging_dir"])
    if _contains_control_characters(staging_raw):
        staging_raw = str(DEFAULT_GUI_PREFERENCES["staging_dir"])
    theme = str(raw.get("theme") or DEFAULT_GUI_PREFERENCES["theme"])
    if theme not in {"System", "Light", "Dark"}:
        theme = "System"
    return {
        "staging_dir": _expand_portable_path(staging_raw),
        "cache_size_mib": _clamped_int(raw.get("cache_size_mib"), 512, 1, 102_400),
        "cache_age_days": _clamped_int(raw.get("cache_age_days"), 14, 1, 365),
        "theme": theme,
    }


def _probe_writable_directory(path: Path) -> None:
    """Ensure a directory is creatable and writable without altering existing files."""
    path.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".fetcher-write-test-", dir=path):
        pass


def normalize_min_quality(value: object, *, allow_any: bool = False):
    text = str(value or "").strip()
    if allow_any and text == "any":
        return "any"
    return text if text in {"720", "1080"} else DEFAULT_MIN_QUALITY


def parse_sftp_storage(raw: str) -> Optional[dict[str, str]]:
    text = raw.strip()
    if not text.startswith("sftp:") or _contains_control_characters(text):
        return None
    body = text.removeprefix("sftp:").strip()
    user = ""
    port = ""
    if body.startswith("user="):
        user, separator, body = body.removeprefix("user=").partition("@")
        if not separator or not user:
            return None
    host_part = body.removeprefix("host=") if body.startswith("host=") else body
    host, slash, remote_path = host_part.partition("/")
    if ":" in host:
        host, port = host.rsplit(":", 1)
        if not port.isdigit() or not (1 <= int(port) <= 65535):
            return None
    if not host or not slash or not remote_path.strip("/"):
        return None
    return {"user": user, "host": host, "port": port, "path": "/" + remote_path.strip("/")}


def is_sftp_storage(raw: str) -> bool:
    return parse_sftp_storage(raw) is not None


def _ensure_writable_dir(path: Path, fallback: Path, label: str) -> tuple[Path, Optional[str]]:
    primary = path.expanduser()
    fallback = fallback.expanduser()

    def prepare(candidate: Path) -> None:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".fetcher-write-test"
        probe.touch()
        probe.unlink()

    try:
        prepare(primary)
        return primary, None
    except Exception as primary_exc:
        try:
            prepare(fallback)
            return fallback, f"{label} directory {primary} is unavailable ({primary_exc}); using {fallback} instead."
        except Exception as fallback_exc:
            raise RuntimeError(
                f"Unable to prepare {label.lower()} directory. primary={primary} error={primary_exc}; "
                f"fallback={fallback} error={fallback_exc}"
            ) from fallback_exc


def resolve_runtime_dirs(settings: AppSettings) -> tuple[Path, Path, list[str]]:
    warnings: list[str] = []
    tmp_dir, tmp_warning = _ensure_writable_dir(settings.tmp_path, STATE_DIR / "tmp", "Temporary")
    if is_sftp_storage(settings.plex_dir):
        output_dir, output_warning = _ensure_writable_dir(
            DEFAULT_LOCAL_COMPLETED_DIR, DEFAULT_LOCAL_COMPLETED_DIR, "Local completed"
        )
        warnings.append("Remote SFTP storage configured; completed downloads stage locally until copied or moved to storage.")
    else:
        output_dir, output_warning = _ensure_writable_dir(
            settings.plex_path, DEFAULT_LOCAL_COMPLETED_DIR, "Download"
        )
    if tmp_warning:
        warnings.append(tmp_warning)
    if output_warning:
        warnings.append(output_warning)
    return tmp_dir, output_dir, warnings


def prepare_runtime_settings(
    settings: AppSettings,
) -> tuple[AppSettings, Path, Path, list[str]]:
    """Validate settings and resolve the local directories used by front ends."""
    validated = SettingsStore(Path("settings.json"), defaults=AppSettings()).validate(settings)
    tmp_dir, output_dir, warnings = resolve_runtime_dirs(validated)
    return validated, tmp_dir, output_dir, warnings


class SettingsStore:
    def __init__(
        self,
        path: Path,
        *,
        defaults: AppSettings,
        path_probe: Optional[Callable[[Path], None]] = None,
    ) -> None:
        self.path = Path(path)
        self.defaults = defaults
        self._unknown: dict[str, Any] = {}
        self._path_probe = path_probe or _probe_writable_directory

    def load(self) -> SettingsLoadResult:
        if not self.path.exists():
            self._unknown = {}
            return SettingsLoadResult(replace(self.defaults), {})
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("settings root must be an object")
        except Exception as exc:
            self._unknown = {}
            return SettingsLoadResult(replace(self.defaults), {}, (f"Unable to read settings: {exc}",))

        self._unknown = {key: value for key, value in data.items() if key not in KNOWN_KEYS}
        search_data = data.get("search_filters") if isinstance(data.get("search_filters"), dict) else {}
        try:
            settings = AppSettings(
                theme=str(data.get("theme", self.defaults.theme)),
                thumbnails_enabled=bool(data.get("thumbnails_enabled", self.defaults.thumbnails_enabled)),
                video_previews_enabled=bool(data.get("video_previews_enabled", self.defaults.video_previews_enabled)),
                plex_dir=str(data.get("plex_dir", self.defaults.plex_dir)),
                tmp_dir=str(data.get("tmp_dir", self.defaults.tmp_dir)),
                parallel_downloads=int(data.get("parallel_downloads", self.defaults.parallel_downloads)),
                searxng_url=str(data.get("searxng_url", self.defaults.searxng_url)),
                searxng_enabled=bool(data.get("searxng_enabled", self.defaults.searxng_enabled)),
                searxng_categories=str(data.get("searxng_categories", self.defaults.searxng_categories)),
                naming_template=str(data.get("naming_template", self.defaults.naming_template)),
                organize_by_site=bool(data.get("organize_by_site", self.defaults.organize_by_site)),
                concurrent_fragments=int(data.get("concurrent_fragments", self.defaults.concurrent_fragments)),
                search_filters=SearchFilters(
                    min_duration_min=int(search_data.get("min_duration_min", self.defaults.search_filters.min_duration_min) or 0),
                    max_duration_min=int(search_data.get("max_duration_min", self.defaults.search_filters.max_duration_min) or 0),
                    min_quality=normalize_min_quality(search_data.get("min_quality", self.defaults.search_filters.min_quality)),
                    min_rating=int(search_data.get("min_rating", self.defaults.search_filters.min_rating) or 0),
                    hd_only=bool(search_data.get("hd_only", self.defaults.search_filters.hd_only)),
                    pages=int(search_data.get("pages", self.defaults.search_filters.pages) or 1),
                    exclude_phrases=str(search_data.get("exclude_phrases", self.defaults.search_filters.exclude_phrases)),
                ),
            )
        except (TypeError, ValueError) as exc:
            return SettingsLoadResult(replace(self.defaults), self._unknown.copy(), (f"Invalid settings: {exc}",))
        try:
            validated = self.validate(settings)
        except (TypeError, ValueError) as exc:
            return SettingsLoadResult(
                replace(self.defaults),
                self._unknown.copy(),
                (f"Invalid settings: {exc}",),
            )
        return SettingsLoadResult(validated, self._unknown.copy())

    def validate(self, settings: AppSettings) -> AppSettings:
        plex_dir = settings.plex_dir.strip()
        if _contains_control_characters(plex_dir) or _contains_control_characters(settings.tmp_dir):
            raise ValueError("Settings paths cannot contain control characters")
        if plex_dir.startswith("sftp:"):
            if parse_sftp_storage(plex_dir) is None:
                raise ValueError("Malformed SFTP storage specification")
        else:
            plex_dir = _expand_portable_path(plex_dir)
        tmp_dir = _expand_portable_path(settings.tmp_dir)
        filters = replace(
            settings.search_filters,
            min_duration_min=max(0, settings.search_filters.min_duration_min),
            max_duration_min=max(0, settings.search_filters.max_duration_min),
            min_rating=max(0, min(100, settings.search_filters.min_rating)),
            pages=max(1, min(20, settings.search_filters.pages)),
            min_quality=normalize_min_quality(settings.search_filters.min_quality),
        )
        return replace(
            settings,
            plex_dir=plex_dir,
            tmp_dir=tmp_dir,
            parallel_downloads=max(1, min(16, settings.parallel_downloads)),
            concurrent_fragments=max(1, min(32, settings.concurrent_fragments)),
            search_filters=filters,
        )

    def validate_runtime_paths(
        self,
        settings: AppSettings,
        gui_preferences: Optional[dict[str, object]] = None,
    ) -> tuple[AppSettings, dict[str, object], list[str]]:
        validated = self.validate(settings)
        preferences = _normalize_gui_preferences(gui_preferences)
        warnings: list[str] = []
        paths: list[tuple[str, Path]] = [("Temporary directory", validated.tmp_path)]
        if is_sftp_storage(validated.plex_dir):
            warnings.append(
                "SFTP syntax is valid; remote reachability is checked separately and does not block saving."
            )
        else:
            paths.append(("Download directory", validated.plex_path))
        if gui_preferences is not None:
            paths.append(("GUI staging directory", Path(str(preferences["staging_dir"]))))
        for label, path in paths:
            try:
                self._path_probe(path)
            except Exception as exc:
                raise ValueError(f"{label} {path} is not safely writable: {exc}") from exc
        return validated, preferences, warnings

    def load_gui_preferences(self) -> dict[str, object]:
        self.load()
        return _normalize_gui_preferences(self._unknown.get("gui"))

    def save_gui_preferences(self, preferences: dict[str, object]) -> dict[str, object]:
        loaded = self.load()
        normalized = _normalize_gui_preferences(preferences)
        existing_gui = self._unknown.get("gui")
        self._unknown["gui"] = {
            **(existing_gui if isinstance(existing_gui, dict) else {}),
            **normalized,
            "staging_dir": _portable_path(str(normalized["staging_dir"])),
        }
        self.save(loaded.settings)
        return normalized

    def save(self, settings: AppSettings) -> None:
        existing_gui = self._unknown.get("gui")
        validated, _, _ = self.validate_runtime_paths(
            settings,
            existing_gui if isinstance(existing_gui, dict) else None,
        )
        payload = {**self._unknown, **asdict(validated)}
        if not is_sftp_storage(validated.plex_dir):
            payload["plex_dir"] = _portable_path(validated.plex_dir)
        payload["tmp_dir"] = _portable_path(validated.tmp_dir)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
            if os.name == "posix" and hasattr(os, "O_DIRECTORY"):
                directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise
