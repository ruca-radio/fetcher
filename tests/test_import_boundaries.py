from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _probe(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    code = """
import sys
import fetchtui
assert 'PySide6' not in sys.modules
assert not any(name == 'fetcher_gui' or name.startswith('fetcher_gui.') for name in sys.modules)
fetchtui.build_parser().parse_args(ARGUMENTS)
assert 'PySide6' not in sys.modules
assert not any(name == 'fetcher_gui' or name.startswith('fetcher_gui.') for name in sys.modules)
""".replace("ARGUMENTS", repr(arguments))
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )


def test_tui_startup_does_not_import_gui_dependencies() -> None:
    result = _probe(["tui"])
    assert result.returncode == 0, result.stderr


def test_cli_startup_does_not_import_gui_dependencies() -> None:
    result = _probe(["search", "term"])
    assert result.returncode == 0, result.stderr


def test_obsolete_web_command_is_removed() -> None:
    parser = __import__("fetchtui").build_parser()
    result = subprocess.run(
        [sys.executable, "fetchtui.py", "web", "--help"],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "invalid choice" in result.stderr


def test_importing_frontends_does_not_create_runtime_directories() -> None:
    code = """
from pathlib import Path
def fail(*args, **kwargs):
    raise AssertionError(f'import attempted mkdir: {args!r}')
Path.mkdir = fail
import fetcher_core.models
import fetchtui
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
