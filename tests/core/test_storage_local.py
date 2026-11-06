from pathlib import Path, PurePosixPath

import pytest

from fetcher_core.storage import LocalStorage, StoragePathError, TargetExistsError


def test_local_storage_lists_and_copies_without_overwrite(tmp_path: Path) -> None:
    root = tmp_path / "root"
    source = tmp_path / "movie.mp4"
    source.write_bytes(b"video")
    storage = LocalStorage(root)

    entry = storage.copy_in(source, PurePosixPath("nested/movie.mp4"))

    assert entry.relpath == PurePosixPath("nested/movie.mp4")
    assert storage.list(PurePosixPath("nested")) == [entry]
    with pytest.raises(TargetExistsError):
        storage.copy_in(source, entry.relpath)


@pytest.mark.parametrize("value", [PurePosixPath("/absolute"), PurePosixPath("../escape")])
def test_local_storage_rejects_unsafe_relative_paths(tmp_path: Path, value: PurePosixPath) -> None:
    storage = LocalStorage(tmp_path / "root")
    with pytest.raises(StoragePathError):
        storage.delete(value)


def test_local_storage_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "movie.mp4").write_bytes(b"secret")
    (root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(StoragePathError):
        LocalStorage(root).materialize_preview(PurePosixPath("link/movie.mp4"))


def test_local_storage_list_does_not_follow_file_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside.mp4"
    root.mkdir()
    outside.write_bytes(b"secret")
    (root / "linked.mp4").symlink_to(outside)

    assert LocalStorage(root).list() == []


def test_local_storage_copy_out_collision_preserves_source(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "movie.mp4"
    source.write_bytes(b"source")
    destination = tmp_path / "destination.mp4"
    destination.write_bytes(b"existing")

    with pytest.raises(TargetExistsError):
        LocalStorage(root).copy_out(PurePosixPath("movie.mp4"), destination)

    assert source.read_bytes() == b"source"
    assert destination.read_bytes() == b"existing"


def test_local_storage_rename_requires_filename_and_no_collision(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "one.mp4").write_bytes(b"one")
    (root / "two.mp4").write_bytes(b"two")
    storage = LocalStorage(root)

    with pytest.raises(StoragePathError):
        storage.rename(PurePosixPath("one.mp4"), "nested/new.mp4")
    with pytest.raises(TargetExistsError):
        storage.rename(PurePosixPath("one.mp4"), "two.mp4")
    assert (root / "one.mp4").exists()


def test_local_storage_stat_and_move(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "one.mp4").write_bytes(b"one")
    storage = LocalStorage(root)

    assert storage.stat(PurePosixPath("one.mp4")).size == 3
    moved = storage.move(PurePosixPath("one.mp4"), PurePosixPath("nested/two.mp4"))

    assert moved.relpath == PurePosixPath("nested/two.mp4")
    assert not (root / "one.mp4").exists()
    assert (root / "nested/two.mp4").read_bytes() == b"one"


def test_local_storage_move_collision_preserves_source(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "source.mp4").write_bytes(b"source")
    (root / "target.mp4").write_bytes(b"target")

    with pytest.raises(TargetExistsError):
        LocalStorage(root).move(PurePosixPath("source.mp4"), PurePosixPath("target.mp4"))

    assert (root / "source.mp4").read_bytes() == b"source"
    assert (root / "target.mp4").read_bytes() == b"target"


def test_local_copy_in_losing_publication_race_does_not_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    storage = LocalStorage(root)
    real_link = __import__("os").link

    def racing_link(src: Path, dst: Path) -> None:
        Path(dst).write_bytes(b"racer")
        real_link(src, dst)

    monkeypatch.setattr("fetcher_core.storage.os.link", racing_link)
    with pytest.raises(TargetExistsError):
        storage.copy_in(source, PurePosixPath("target.mp4"))
    assert (root / "target.mp4").read_bytes() == b"racer"


def test_local_copy_out_losing_publication_race_does_not_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "source.mp4").write_bytes(b"source")
    destination = tmp_path / "destination.mp4"
    storage = LocalStorage(root)
    real_link = __import__("os").link

    def racing_link(src: Path, dst: Path) -> None:
        Path(dst).write_bytes(b"racer")
        real_link(src, dst)

    monkeypatch.setattr("fetcher_core.storage.os.link", racing_link)
    with pytest.raises(TargetExistsError):
        storage.copy_out(PurePosixPath("source.mp4"), destination)
    assert destination.read_bytes() == b"racer"
    assert (root / "source.mp4").exists()


def test_local_rename_losing_publication_race_preserves_both_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.mp4"
    source.write_bytes(b"source")
    storage = LocalStorage(root)
    real_link = __import__("os").link

    def racing_link(src: Path, dst: Path) -> None:
        Path(dst).write_bytes(b"racer")
        real_link(src, dst)

    monkeypatch.setattr("fetcher_core.storage.os.link", racing_link)
    with pytest.raises(TargetExistsError):
        storage.rename(PurePosixPath("source.mp4"), "target.mp4")
    assert source.read_bytes() == b"source"
    assert (root / "target.mp4").read_bytes() == b"racer"
