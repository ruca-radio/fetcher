from __future__ import annotations

import os
import stat

from fetcher_core.state import harden_state_dir


def test_harden_state_dir_tightens_files(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o755)
    cookie_file = state_dir / "twitter_cookies.json"
    cookie_file.write_text("{}")
    os.chmod(cookie_file, 0o644)

    notes = harden_state_dir(state_dir, secret_files=[cookie_file])

    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(cookie_file.stat().st_mode) == 0o600
    assert any(str(state_dir) in note for note in notes)
    assert any(str(cookie_file) in note for note in notes)


def test_harden_state_dir_already_correct_returns_empty(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    cookie_file = state_dir / "twitter_cookies.json"
    cookie_file.write_text("{}")
    os.chmod(cookie_file, 0o600)

    assert harden_state_dir(state_dir, secret_files=[cookie_file]) == []


def test_harden_state_dir_missing_paths_are_noop(tmp_path):
    state_dir = tmp_path / "missing-state"
    cookie_file = state_dir / "twitter_cookies.json"

    assert harden_state_dir(state_dir, secret_files=[cookie_file]) == []
    assert not state_dir.exists()
    assert not cookie_file.exists()


def test_harden_state_dir_chmod_errors_do_not_raise(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o755)
    cookie_file = state_dir / "twitter_cookies.json"
    cookie_file.write_text("{}")
    os.chmod(cookie_file, 0o644)

    def raise_oserror(path, mode):
        raise OSError("permission denied")

    monkeypatch.setattr(os, "chmod", raise_oserror)

    assert harden_state_dir(state_dir, secret_files=[cookie_file]) == []
