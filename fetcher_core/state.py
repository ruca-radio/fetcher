from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


def harden_state_dir(state_dir: Path, *, secret_files: Iterable[Path] = ()) -> list[str]:
    """Tighten permissions on existing state paths without creating them."""
    notes: list[str] = []

    _harden_existing_path(state_dir, 0o700, notes, "state directory")
    for path in secret_files:
        _harden_existing_path(path, 0o600, notes, "secret file")

    return notes


def _harden_existing_path(path: Path, mode: int, notes: list[str], label: str) -> None:
    try:
        current_mode = path.stat().st_mode & 0o777
    except (OSError, FileNotFoundError):
        return

    if current_mode == mode:
        return

    try:
        os.chmod(path, mode)
    except (OSError, FileNotFoundError):
        return

    notes.append(f"Hardened {label} permissions: {path} ({current_mode:03o} -> {mode:03o})")
