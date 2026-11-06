from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class RuntimePaths:
    application_root: Path
    mutable_root: Path
    state_dir: Path
    cache_dir: Path
    tmp_dir: Path
    preview_cache_dir: Path


def _xdg_root(environ: Mapping[str, str], name: str, fallback: Path) -> Path:
    configured = environ.get(name)
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_absolute():
            return candidate
    return fallback


def resolve_runtime_paths(
    application_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> RuntimePaths:
    """Resolve mutable paths without touching the filesystem."""
    root = Path(application_root).resolve()
    environment = os.environ if environ is None else environ
    user_home = Path.home() if home is None else Path(home)

    if (root / "pyproject.toml").is_file():
        mutable_root = root
        state_dir = root / ".fetcher"
        cache_dir = state_dir
        tmp_dir = root / "tmp"
    else:
        data_home = _xdg_root(environment, "XDG_DATA_HOME", user_home / ".local" / "share")
        cache_home = _xdg_root(environment, "XDG_CACHE_HOME", user_home / ".cache")
        state_dir = data_home / "fetcher"
        mutable_root = state_dir
        cache_dir = cache_home / "fetcher"
        tmp_dir = cache_dir / "tmp"

    return RuntimePaths(
        application_root=root,
        mutable_root=mutable_root,
        state_dir=state_dir,
        cache_dir=cache_dir,
        tmp_dir=tmp_dir,
        preview_cache_dir=cache_dir / "preview-cache",
    )
