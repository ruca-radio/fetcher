from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import fetchtui


REPO = Path(__file__).resolve().parents[1]


def test_parser_preserves_tui_and_cli_commands_and_adds_gui() -> None:
    parser = fetchtui.build_parser()

    assert parser.parse_args([]).command is None
    assert parser.parse_args(["tui"]).command == "tui"
    assert parser.parse_args(["gui"]).command == "gui"
    assert parser.parse_args(["search", "term"]).command == "search"
    assert parser.parse_args(["download", "https://example.test/video"]).command == "download"
    assert parser.parse_args(["batch", "term"]).command == "batch"


def test_fetch_gui_is_executable() -> None:
    launcher = REPO / "fetch-gui"

    assert launcher.is_file()
    assert os.access(launcher, os.X_OK)


def test_fetch_gui_prefers_repository_virtualenv(tmp_path: Path) -> None:
    launcher = tmp_path / "fetch-gui"
    shutil.copyfile(REPO / "fetch-gui", launcher)
    launcher.chmod(0o755)
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$0\" \"$@\"\n")
    python.chmod(0o755)

    result = subprocess.run(
        [str(launcher), "--example"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(python), str(tmp_path / "fetchtui.py"), "gui", "--example"]


def test_gui_missing_pyside6_has_actionable_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def missing_gui(_argv=None):
        raise ModuleNotFoundError("No module named 'PySide6'", name="PySide6")

    monkeypatch.setattr(fetchtui, "_run_gui", missing_gui, raising=False)

    assert fetchtui.main(["gui"]) == 2
    error = capsys.readouterr().err
    assert "PySide6" in error
    assert "pip install" in error
    assert "gui" in error


def test_installed_gui_entry_point_forwards_command_line_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[str] = []
    monkeypatch.setattr(sys, "argv", ["fetcher-gui", "--help"])
    monkeypatch.setattr(fetchtui, "main", lambda argv: received.extend(argv) or 0)

    assert fetchtui.gui_main() == 0
    assert received == ["gui", "--help"]
