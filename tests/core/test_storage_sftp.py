import subprocess
import re
from pathlib import Path, PurePosixPath

import pytest

from fetcher_core.storage import SftpStorage, StoragePathError, TargetExistsError


class FakeRunner:
    def __init__(self, outputs: list[subprocess.CompletedProcess[str]] | None = None) -> None:
        self.outputs = outputs or []
        self.calls: list[tuple[list[str], str, float]] = []

    def __call__(self, argv: list[str], batch: str, timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, batch, timeout))
        return self.outputs.pop(0) if self.outputs else subprocess.CompletedProcess(argv, 0, "", "")


def test_sftp_uses_ssh_config_batch_mode_and_bounded_timeout() -> None:
    runner = FakeRunner([subprocess.CompletedProcess([], 0, "-rw-r--r-- 1 u g 5 Jan 01 00:00 movie.mp4\n", "")])
    storage = SftpStorage("host", PurePosixPath("/media"), runner=runner, operation_timeout=12)

    entries = storage.list()

    argv, batch, timeout = runner.calls[0]
    assert "-F" not in argv
    assert "-oBatchMode=yes" in argv
    assert "-oConnectTimeout=8" in argv
    assert timeout == 12
    assert batch == 'ls -l "/media"\n'
    assert entries[0].relpath == PurePosixPath("movie.mp4")


@pytest.mark.parametrize("value", ["../x", "/x", "a\nb", "a\rb", "a\0b"])
def test_sftp_rejects_unsafe_paths(value: str) -> None:
    with pytest.raises(StoragePathError):
        SftpStorage("host", PurePosixPath("/media"), runner=FakeRunner()).delete(PurePosixPath(value))


def test_sftp_rename_rejects_path_bearing_name() -> None:
    storage = SftpStorage("host", PurePosixPath("/media"), runner=FakeRunner())
    with pytest.raises(StoragePathError):
        storage.rename(PurePosixPath("old.mp4"), "nested/new.mp4")


def test_sftp_copy_in_checks_collision_before_upload_and_preserves_source(tmp_path: Path) -> None:
    source = tmp_path / "movie.mp4"
    source.write_bytes(b"video")
    runner = FakeRunner([subprocess.CompletedProcess([], 0, "-rw-r--r-- 1 u g 5 Jan 01 00:00 movie.mp4\n", "")])
    storage = SftpStorage("host", PurePosixPath("/media"), runner=runner)

    with pytest.raises(TargetExistsError):
        storage.copy_in(source, PurePosixPath("movie.mp4"))

    assert source.exists()
    assert len(runner.calls) == 1


def test_sftp_failure_preserves_stderr(tmp_path: Path) -> None:
    source = tmp_path / "movie.mp4"
    source.write_bytes(b"video")
    runner = FakeRunner([
        subprocess.CompletedProcess([], 0, "", ""),
        subprocess.CompletedProcess([], 1, "", "permission denied"),
        subprocess.CompletedProcess([], 0, "", ""),
    ])
    storage = SftpStorage("host", PurePosixPath("/media"), runner=runner)

    with pytest.raises(RuntimeError, match="permission denied"):
        storage.copy_in(source, PurePosixPath("movie.mp4"))
    assert source.exists()


def test_sftp_stat_and_move() -> None:
    runner = FakeRunner([
        subprocess.CompletedProcess([], 0, "-rw-r--r-- 1 u g 5 Jan 01 00:00 old.mp4\n", ""),
        subprocess.CompletedProcess([], 0, "", ""),
        subprocess.CompletedProcess([], 0, "-rw-r--r-- 1 u g 5 Jan 01 00:00 old.mp4\n", ""),
    ])
    storage = SftpStorage("host", PurePosixPath("/media"), runner=runner)

    assert storage.stat(PurePosixPath("old.mp4")).size == 5
    moved = storage.move(PurePosixPath("old.mp4"), PurePosixPath("new.mp4"))

    assert moved.relpath == PurePosixPath("new.mp4")
    assert moved.size is None
    assert 'rename "/media/old.mp4" "/media/new.mp4"' in runner.calls[-1][1]


def test_sftp_move_publication_failure_preserves_source() -> None:
    runner = FakeRunner([
        subprocess.CompletedProcess([], 0, "", ""),
        subprocess.CompletedProcess([], 1, "", "publish failed"),
    ])
    storage = SftpStorage("host", PurePosixPath("/media"), runner=runner)

    with pytest.raises(RuntimeError, match="publish failed"):
        storage.move(PurePosixPath("source.mp4"), PurePosixPath("target.mp4"))

    assert all('rm "/media/source.mp4"' not in batch for _, batch, _ in runner.calls)


def test_sftp_overwrite_restores_old_target_when_publication_fails(tmp_path: Path) -> None:
    source = tmp_path / "movie.mp4"
    source.write_bytes(b"new")
    runner = FakeRunner([
        subprocess.CompletedProcess([], 0, "-rw-r--r-- 1 u g 3 Jan 01 00:00 movie.mp4\n", ""),
        subprocess.CompletedProcess([], 0, "", ""),
        subprocess.CompletedProcess([], 0, "", ""),
        subprocess.CompletedProcess([], 1, "", "publish failed"),
        subprocess.CompletedProcess([], 0, "", ""),
        subprocess.CompletedProcess([], 0, "", ""),
    ])
    storage = SftpStorage("host", PurePosixPath("/media"), runner=runner)

    with pytest.raises(RuntimeError, match="publish failed"):
        storage.copy_in(source, PurePosixPath("movie.mp4"), overwrite=True)

    backup_batch = runner.calls[2][1]
    assert 'rm "/media/movie.mp4"' not in backup_batch
    assert 'rename "/media/movie.mp4" "/media/movie.mp4.' in backup_batch
    publication_batch = runner.calls[3][1]
    assert 'rm "/media/movie.mp4"' not in publication_batch
    assert '.partial" "/media/movie.mp4"' in publication_batch
    recovery_batch = runner.calls[4][1]
    assert 'rename "/media/movie.mp4.' in recovery_batch
    assert recovery_batch.splitlines()[0].endswith('"/media/movie.mp4"')


def test_sftp_rename_overwrite_restores_old_target_when_publication_fails() -> None:
    runner = FakeRunner([
        subprocess.CompletedProcess([], 0, "-rw-r--r-- 1 u g 3 Jan 01 00:00 new.mp4\n", ""),
        subprocess.CompletedProcess([], 0, "", ""),
        subprocess.CompletedProcess([], 1, "", "publish failed"),
        subprocess.CompletedProcess([], 0, "", ""),
    ])
    storage = SftpStorage("host", PurePosixPath("/media"), runner=runner)

    with pytest.raises(RuntimeError, match="publish failed"):
        storage.rename(PurePosixPath("old.mp4"), "new.mp4", overwrite=True)

    backup_batch = runner.calls[1][1]
    assert 'rm "/media/new.mp4"' not in backup_batch
    assert 'rename "/media/new.mp4" "/media/new.mp4.' in backup_batch
    publication_batch = runner.calls[2][1]
    assert 'rename "/media/old.mp4" "/media/new.mp4"' in publication_batch
    recovery_batch = runner.calls[3][1]
    assert recovery_batch.startswith('rename "/media/new.mp4.')
    assert recovery_batch.rstrip().endswith('"/media/new.mp4"')


@pytest.mark.parametrize(
    ("host", "user"),
    [("-oProxyCommand=bad", ""), ("host", "-bad"), ("bad host", ""), ("host@other", ""), ("host", "bad@user")],
)
def test_sftp_rejects_unsafe_connection_identities(host: str, user: str) -> None:
    with pytest.raises(StoragePathError):
        SftpStorage(host, PurePosixPath("/media"), user=user, runner=FakeRunner())


def test_sftp_upload_failure_runs_independent_partial_cleanup(tmp_path: Path) -> None:
    source = tmp_path / "movie.mp4"
    source.write_bytes(b"new")
    runner = FakeRunner([
        subprocess.CompletedProcess([], 0, "-rw-r--r-- 1 u g 3 Jan 01 00:00 movie.mp4\n", ""),
        subprocess.CompletedProcess([], 1, "", "upload failed"),
        subprocess.CompletedProcess([], 0, "", ""),
    ])
    storage = SftpStorage("host", PurePosixPath("/media"), runner=runner)

    with pytest.raises(RuntimeError, match="upload failed"):
        storage.copy_in(source, PurePosixPath("movie.mp4"), overwrite=True)

    assert len(runner.calls) == 3
    cleanup = runner.calls[2][1]
    assert re.fullmatch(r'rm "/media/movie\.mp4\.[0-9a-f]+\.partial"\n', cleanup)
    assert ".backup" not in cleanup
