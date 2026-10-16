from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from fetcher_core.jobs import DownloadManager, DuplicateJobError, JobHistoryStore, terminate_process_group
from fetcher_core.models import DownloadJob, Video


def video(identifier: str = "one") -> Video:
    return Video(identifier, identifier.title(), 0, 0, f"https://example.test/{identifier}", "Test")


def successful_downloader(job: DownloadJob, progress, done) -> None:
    assert job.status == "downloading"
    job.progress = 42.0
    progress()
    job.status = "done"
    job.filepath = Path(f"/tmp/{job.video.id}.mp4")
    done(job)


def test_job_moves_from_queued_through_downloading_to_done_and_notifies() -> None:
    seen: list[tuple[str, bool]] = []
    manager = DownloadManager(successful_downloader, max_workers=1)
    manager.subscribe(lambda snapshot: seen.append((snapshot.status, snapshot.worker_started)))

    queued = manager.submit(video())
    manager.wait()

    assert queued.status == "queued"
    assert [status for status, _ in seen] == ["queued", "downloading", "downloading", "done", "done"]
    assert seen[-2:] == [("done", True), ("done", False)]
    assert manager.get("one").status == "done"
    assert manager.get("one").worker_started is False


def test_frequent_progress_does_not_fsync_history_for_every_update(tmp_path):
    class CountingStore(JobHistoryStore):
        saves = 0
        def save(self, jobs):
            self.saves += 1
            super().save(jobs)

    store = CountingStore(tmp_path / "jobs.json")

    def transfer(job, progress, done):
        for percent in range(100):
            job.progress = percent
            progress()
        job.status = "done"
        done(job)

    manager = DownloadManager(transfer, history_store=store)
    manager.submit(video())
    manager.wait()
    assert store.load()[0].status == "done"
    assert store.saves < 10
    manager.close()


def test_duplicate_active_job_is_rejected() -> None:
    release = threading.Event()
    manager = DownloadManager(lambda job, progress, done: release.wait(2), max_workers=1)
    manager.submit(video())
    with pytest.raises(DuplicateJobError):
        manager.submit(video())
    release.set()
    manager.wait()


def test_worker_exception_becomes_failed_and_clears_worker_started() -> None:
    def explode(job, progress, done):
        raise RuntimeError("boom")

    manager = DownloadManager(explode, max_workers=1)
    manager.submit(video())
    manager.wait()

    snapshot = manager.get("one")
    assert snapshot.status == "failed"
    assert "boom" in snapshot.message
    assert snapshot.worker_started is False


def test_subscriber_exception_cannot_strand_worker() -> None:
    manager = DownloadManager(successful_downloader, max_workers=1)

    def broken_subscriber(snapshot):
        if snapshot.status == "downloading":
            raise RuntimeError("bad UI subscriber")

    manager.subscribe(broken_subscriber)
    manager.submit(video())
    manager.wait()

    assert manager.get("one").status == "done"
    assert manager.get("one").worker_started is False


def test_subscriber_is_invoked_without_manager_lock_held() -> None:
    manager = DownloadManager(successful_downloader, max_workers=1)
    callback_checked = threading.Event()

    def subscriber(snapshot):
        if snapshot.status != "queued":
            return
        reader_done = threading.Event()
        reader = threading.Thread(target=lambda: (manager.snapshots(), reader_done.set()))
        reader.start()
        assert reader_done.wait(0.5), "subscriber ran while manager lock was held"
        reader.join()
        callback_checked.set()

    manager.subscribe(subscriber)
    manager.submit(video())
    manager.wait()
    assert callback_checked.is_set()


def test_notifications_have_monotonic_revisions_when_stop_overtakes_progress() -> None:
    progress_called = threading.Event()
    release = threading.Event()
    seen = []

    def downloader(job, progress, done):
        job.progress = 25
        progress()
        progress_called.set()
        release.wait(2)

    manager = DownloadManager(downloader, max_workers=1)

    def slow_subscriber(snapshot):
        if snapshot.status == "downloading" and snapshot.progress == 25:
            release.wait(0.05)
        seen.append(snapshot)

    manager.subscribe(slow_subscriber)
    manager.submit(video())
    assert progress_called.wait(1)
    manager.stop("one")
    release.set()
    manager.wait()

    assert [item.revision for item in seen] == sorted(item.revision for item in seen)
    cancelled_revision = min(item.revision for item in seen if item.status == "cancelled")
    assert all(item.status != "downloading" for item in seen if item.revision > cancelled_revision)


def test_queued_subscriber_can_wait_and_close_reentrantly() -> None:
    callback_done = threading.Event()
    manager = DownloadManager(successful_downloader, max_workers=1)

    def subscriber(snapshot):
        if snapshot.status == "queued":
            manager.wait()
            manager.close()
            callback_done.set()

    manager.subscribe(subscriber)
    manager.submit(video())
    assert callback_done.wait(1)
    assert manager.get("one").status == "done"


def test_stop_cancels_a_running_job() -> None:
    started = threading.Event()
    release = threading.Event()

    def downloader(job, progress, done):
        started.set()
        release.wait(2)
        job.status = "done"
        done(job)

    manager = DownloadManager(downloader, max_workers=1)
    manager.submit(video())
    assert started.wait(1)
    stopped = manager.stop("one")
    release.set()
    manager.wait()

    assert stopped.status == "cancelled"
    assert manager.get("one").status == "cancelled"
    assert manager.get("one").worker_started is False


def test_terminate_process_group_resumes_paused_then_escalates(monkeypatch) -> None:
    calls = []

    class FakeProcess:
        pid = 123

        def poll(self):
            return None

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            if len([call for call in calls if call[0] == "wait"]) == 1:
                raise subprocess.TimeoutExpired("fake", timeout)
            return -signal.SIGKILL

    monkeypatch.setattr(os, "getpgid", lambda pid: 456)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append(("killpg", pgid, sig)))

    terminate_process_group(FakeProcess(), paused=True, timeout=0.01)

    assert calls[0] == ("killpg", 456, signal.SIGCONT)
    assert calls[1] == ("killpg", 456, signal.SIGTERM)
    assert ("killpg", 456, signal.SIGKILL) in calls


def test_terminate_process_group_ignores_missing_process(monkeypatch) -> None:
    class FakeProcess:
        pid = 123

        def poll(self):
            return None

    monkeypatch.setattr(os, "getpgid", lambda pid: (_ for _ in ()).throw(ProcessLookupError()))
    terminate_process_group(FakeProcess())


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
def test_terminate_process_group_stops_real_parent_and_child() -> None:
    proc = subprocess.Popen(
        ["sh", "-c", "sleep 30 & wait"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        terminate_process_group(proc, timeout=0.5)
        assert proc.poll() is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(proc.pid, 0)
    finally:
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=1)


@pytest.mark.parametrize("operation,expected_status", [("stop", "cancelled"), ("close", "interrupted")])
def test_manager_stop_and_close_terminate_paused_process_group(monkeypatch, operation, expected_status) -> None:
    started = threading.Event()
    release = threading.Event()
    fake_process = object()
    terminated = []

    def downloader(job, progress, done):
        job.proc = fake_process
        job.status = "paused"
        progress()
        started.set()
        release.wait(2)

    monkeypatch.setattr(
        "fetcher_core.jobs.terminate_process_group",
        lambda proc, **kwargs: terminated.append((proc, kwargs)),
    )
    manager = DownloadManager(downloader, max_workers=1)
    manager.submit(video())
    assert started.wait(1)

    if operation == "stop":
        manager.stop("one")
    else:
        manager.close(wait=False, cancel_futures=True)

    assert terminated == [(fake_process, {"paused": True})]
    assert manager.get("one").status == expected_status
    release.set()
    manager.wait()


def test_stopped_worker_cannot_corrupt_immediate_retry() -> None:
    old_started = threading.Event()
    release_old = threading.Event()
    attempts = 0

    def downloader(job, progress, done):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            old_started.set()
            release_old.wait(2)
            job.status = "failed"
            job.eta = "late old failure"
            done(job)
            return
        job.status = "done"
        job.eta = "new run completed"
        done(job)

    manager = DownloadManager(downloader, max_workers=2)
    manager.submit(video())
    assert old_started.wait(1)
    manager.stop("one")
    manager.retry("one")
    release_old.set()
    manager.wait()

    snapshot = manager.get("one")
    assert snapshot.status == "done"
    assert snapshot.message == "new run completed"
    assert snapshot.worker_started is False


def test_failed_job_can_be_retried() -> None:
    attempts = 0

    def downloader(job, progress, done):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary")
        job.status = "done"
        done(job)

    manager = DownloadManager(downloader, max_workers=1)
    manager.submit(video())
    manager.wait()
    retried = manager.retry("one")
    manager.wait()

    assert retried.status == "queued"
    assert manager.get("one").status == "done"
    assert attempts == 2


def test_executor_never_runs_more_than_configured_workers() -> None:
    lock = threading.Lock()
    release = threading.Event()
    active = peak = 0
    both_started = threading.Event()

    def downloader(job, progress, done):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                both_started.set()
        release.wait(2)
        with lock:
            active -= 1
        job.status = "done"

    manager = DownloadManager(downloader, max_workers=2)
    for number in range(5):
        manager.submit(video(str(number)))
    assert both_started.wait(1)
    release.set()
    manager.wait()

    assert peak == 2


def test_snapshots_are_immutable_copies() -> None:
    manager = DownloadManager(successful_downloader, max_workers=1)
    queued = manager.submit(video())
    with pytest.raises(FrozenInstanceError):
        queued.status = "done"  # type: ignore[misc]
    manager.wait()
    assert queued.status == "queued"
    assert manager.get("one").status == "done"


def test_history_is_atomic_and_active_jobs_are_interrupted_on_restart(tmp_path: Path) -> None:
    path = tmp_path / ".fetcher" / "jobs.json"
    store = JobHistoryStore(path)
    job = DownloadJob(video=video(), status="downloading", worker_started=True)

    store.save([job])
    raw = json.loads(path.read_text())
    assert raw["jobs"][0]["status"] == "downloading"
    assert not path.with_suffix(".json.tmp").exists()

    restored = store.load()
    assert restored[0].status == "interrupted"
    assert restored[0].worker_started is False
    assert "restart" in restored[0].eta


def test_close_cancel_futures_terminalizes_and_persists_queued_jobs(tmp_path: Path) -> None:
    release = threading.Event()
    store = JobHistoryStore(tmp_path / "jobs.json")
    manager = DownloadManager(lambda job, progress, done: release.wait(2), max_workers=1, history_store=store)
    manager.submit(video("running"))
    manager.submit(video("queued"))

    manager.close(wait=False, cancel_futures=True)

    queued = manager.get("queued")
    assert queued.status in {"cancelled", "interrupted"}
    assert queued.worker_started is False
    persisted = {job.video.id: job for job in store.load()}
    assert persisted["queued"].status in {"cancelled", "interrupted"}
    release.set()
    manager.wait()


def test_history_failure_rolls_back_submit_without_ghost_job() -> None:
    class FailingHistory:
        def load(self):
            return []

        def save(self, jobs):
            raise OSError("disk full")

    manager = DownloadManager(successful_downloader, max_workers=1, history_store=FailingHistory())
    with pytest.raises(OSError, match="disk full"):
        manager.submit(video())

    assert manager.snapshots() == ()
    manager.close()


def test_history_failure_during_worker_transition_terminalizes_cleanly() -> None:
    class FailsAfterSubmit:
        def __init__(self):
            self.saves = 0

        def load(self):
            return []

        def save(self, jobs):
            self.saves += 1
            if self.saves > 1:
                raise OSError("disk disappeared")

    manager = DownloadManager(successful_downloader, max_workers=1, history_store=FailsAfterSubmit())
    manager.submit(video())
    manager.wait()

    snapshot = manager.get("one")
    assert snapshot.status == "failed"
    assert "history save failed" in snapshot.message
    assert snapshot.worker_started is False


def test_completed_run_bookkeeping_stays_bounded_across_many_jobs_and_retries() -> None:
    def fail(job, progress, done):
        raise RuntimeError("retry me")

    manager = DownloadManager(fail, max_workers=4)
    for number in range(30):
        manager.submit(video(str(number)))
    manager.wait()
    for number in range(30):
        manager.retry(str(number))
    manager.wait()

    assert manager._futures == {}
    assert manager._terminal_requests == {}


def test_clear_completed_removes_only_terminal_jobs_and_persists(tmp_path: Path) -> None:
    release = threading.Event()
    active_started = threading.Event()
    store = JobHistoryStore(tmp_path / "jobs.json")

    def downloader(job, progress, done):
        if job.video.id == "active":
            active_started.set()
            release.wait(2)
            return
        job.status = "done"
        done(job)

    manager = DownloadManager(downloader, max_workers=2, history_store=store)
    manager.submit(video("done"))
    manager.wait()
    manager.submit(video("active"))
    assert active_started.wait(1)

    removed = manager.clear_completed()

    assert removed == 1
    assert [job.id for job in manager.snapshots()] == ["active"]
    assert [job.video.id for job in store.load()] == ["active"]
    assert "done" not in manager._generations
    assert "done" not in manager._job_revisions
    release.set()
    manager.wait()


def test_clear_completed_covers_all_terminal_statuses(tmp_path: Path) -> None:
    jobs = [DownloadJob(video=video(status), status=status) for status in ("done", "failed", "cancelled", "interrupted")]
    jobs.append(DownloadJob(video=video("queued"), status="queued"))
    store = JobHistoryStore(tmp_path / "jobs.json")
    store.save(jobs)
    manager = DownloadManager(successful_downloader, history_store=store)

    assert manager.clear_completed() == 5
    assert manager.snapshots() == ()


def test_clear_completed_rolls_back_when_history_persistence_fails() -> None:
    class FailingHistory:
        def load(self):
            return [DownloadJob(video=video("done"), status="done")]

        def save(self, jobs):
            raise OSError("read only")

    manager = DownloadManager(successful_downloader, history_store=FailingHistory())

    with pytest.raises(OSError, match="read only"):
        manager.clear_completed()

    assert [snapshot.id for snapshot in manager.snapshots()] == ["done"]
