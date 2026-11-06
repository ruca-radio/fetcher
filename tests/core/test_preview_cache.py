from pathlib import Path, PurePosixPath
import threading

import pytest

from fetcher_core.preview import PreviewCache


class Clock:
    def __init__(self, now: float = 1000) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_preview_cache_distinguishes_equal_basenames(tmp_path: Path) -> None:
    cache = PreviewCache(tmp_path, max_bytes=100, max_age=1000, clock=Clock())
    first = cache.materialize("nas", PurePosixPath("a/movie.mp4"), lambda p: p.write_bytes(b"a"))
    second = cache.materialize("nas", PurePosixPath("b/movie.mp4"), lambda p: p.write_bytes(b"b"))
    assert first != second
    assert first.read_bytes() == b"a"
    assert second.read_bytes() == b"b"


def test_preview_cache_failure_removes_partial_and_never_publishes(tmp_path: Path) -> None:
    cache = PreviewCache(tmp_path, max_bytes=100, max_age=1000, clock=Clock())

    def fail(path: Path) -> None:
        path.write_bytes(b"partial")
        raise RuntimeError("transfer failed")

    with pytest.raises(RuntimeError, match="transfer failed"):
        cache.materialize("nas", PurePosixPath("movie.mp4"), fail)
    assert list(tmp_path.iterdir()) == []


def test_preview_cache_publishes_by_atomic_replace(tmp_path: Path) -> None:
    cache = PreviewCache(tmp_path, max_bytes=100, max_age=1000, clock=Clock())
    observed: list[Path] = []

    def produce(path: Path) -> None:
        observed.append(path)
        assert path.suffix == ".partial"
        path.write_bytes(b"complete")

    result = cache.materialize("nas", PurePosixPath("movie.mp4"), produce)
    assert observed[0] != result
    assert result.read_bytes() == b"complete"
    assert not observed[0].exists()


def test_preview_cache_cleanup_is_deterministic_by_age_then_size(tmp_path: Path) -> None:
    clock = Clock()
    cache = PreviewCache(tmp_path, max_bytes=5, max_age=10, clock=clock)
    old = cache.materialize("nas", PurePosixPath("old.mp4"), lambda p: p.write_bytes(b"old"))
    clock.now += 5
    middle = cache.materialize("nas", PurePosixPath("middle.mp4"), lambda p: p.write_bytes(b"mid"))
    assert not old.exists()  # size eviction removes oldest
    assert middle.exists()
    clock.now += 11
    cache.cleanup()
    assert not middle.exists()  # age eviction


def test_preview_cache_cleanup_protects_active_partial(tmp_path: Path) -> None:
    cache = PreviewCache(tmp_path, max_bytes=0, max_age=0, clock=Clock())

    def produce(path: Path) -> None:
        path.write_bytes(b"active")
        cache.cleanup()
        assert path.exists()

    cache.materialize("nas", PurePosixPath("movie.mp4"), produce)


def test_preview_cache_returns_existing_new_result_even_when_larger_than_limit(tmp_path: Path) -> None:
    cache = PreviewCache(tmp_path, max_bytes=1, max_age=1000, clock=Clock())

    result = cache.materialize("nas", PurePosixPath("movie.mp4"), lambda p: p.write_bytes(b"oversize"))

    assert result.exists()
    assert result.read_bytes() == b"oversize"


def test_preview_cache_coordinates_concurrent_same_key_producers(tmp_path: Path) -> None:
    cache = PreviewCache(tmp_path, max_bytes=100, max_age=1000, clock=Clock())
    started = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()
    results: list[Path] = []

    def producer(path: Path) -> None:
        nonlocal calls
        with calls_lock:
            calls += 1
        started.set()
        assert release.wait(timeout=2)
        path.write_bytes(b"complete")

    def run() -> None:
        results.append(cache.materialize("nas", PurePosixPath("movie.mp4"), producer))

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    assert started.wait(timeout=2)
    second.start()
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive() and not second.is_alive()
    assert calls == 1
    assert results[0] == results[1]
    assert results[0].read_bytes() == b"complete"
