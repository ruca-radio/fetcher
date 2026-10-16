from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from queue import Queue
from typing import Callable, Iterable, Optional

from .models import STATE_DIR, DownloadJob, Video


ACTIVE_STATUSES = frozenset({"queued", "downloading", "paused"})
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled", "interrupted"})


class DuplicateJobError(ValueError):
    """Raised when an active job already exists for a video id."""


@dataclass(frozen=True)
class JobSnapshot:
    id: str
    title: str
    site: str
    url: str
    thumbnail_url: Optional[str]
    status: str
    progress: float
    speed: str
    message: str
    filepath: Optional[Path]
    file_size: int
    downloaded_bytes: int
    worker_started: bool
    revision: int = 0


Downloader = Callable[[DownloadJob, Callable[[], None], Callable[[DownloadJob], None]], None]
Subscriber = Callable[[JobSnapshot], None]


def terminate_process_group(
    proc: subprocess.Popen,
    *,
    paused: bool = False,
    timeout: float = 1.0,
) -> None:
    """Terminate a downloader session, escalating after a bounded grace period."""
    try:
        if proc.poll() is not None:
            return
    except (ProcessLookupError, PermissionError):
        return

    if os.name == "posix":
        try:
            pgid = os.getpgid(proc.pid)
            if paused:
                os.killpg(pgid, signal.SIGCONT)
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError:
            _terminate_parent(proc, timeout)
            return
        try:
            proc.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, ProcessLookupError, PermissionError):
                pass
        return

    _terminate_parent(proc, timeout)


def _terminate_parent(proc: subprocess.Popen, timeout: float) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except ProcessLookupError:
        return
    except PermissionError:
        return
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, ProcessLookupError, PermissionError):
            pass


class JobHistoryStore:
    """Atomically persist download jobs and restore safe restart state."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else STATE_DIR / "jobs.json"

    def save(self, jobs: Iterable[DownloadJob]) -> None:
        payload = {"version": 1, "jobs": [self._encode(job) for job in jobs]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def load(self) -> list[DownloadJob]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        raw_jobs = payload.get("jobs", [])
        if not isinstance(raw_jobs, list):
            raise ValueError("job history must contain a jobs list")
        jobs = [self._decode(item) for item in raw_jobs if isinstance(item, dict)]
        changed = False
        for job in jobs:
            if job.status in ACTIVE_STATUSES:
                job.status = "interrupted"
                job.eta = "interrupted by application restart"
                job.proc = None
                job.worker_started = False
                changed = True
        if changed:
            self.save(jobs)
        return jobs

    @staticmethod
    def _encode(job: DownloadJob) -> dict[str, object]:
        video = asdict(job.video)
        return {
            "video": video,
            "status": job.status,
            "progress": job.progress,
            "speed": job.speed,
            "eta": job.eta,
            "filepath": str(job.filepath) if job.filepath else None,
            "file_size": job.file_size,
            "downloaded_bytes": job.downloaded_bytes,
            "worker_started": job.worker_started,
        }

    @staticmethod
    def _decode(raw: dict[str, object]) -> DownloadJob:
        video_raw = raw.get("video")
        if not isinstance(video_raw, dict):
            raise ValueError("job history entry is missing video data")
        video = Video(**video_raw)
        filepath = raw.get("filepath")
        return DownloadJob(
            video=video,
            status=str(raw.get("status", "interrupted")),
            progress=float(raw.get("progress", 0.0)),
            speed=str(raw.get("speed", "")),
            eta=str(raw.get("eta", "")),
            filepath=Path(str(filepath)) if filepath else None,
            file_size=int(raw.get("file_size", 0)),
            downloaded_bytes=int(raw.get("downloaded_bytes", 0)),
            worker_started=bool(raw.get("worker_started", False)),
        )


class DownloadManager:
    """Own download execution and publish thread-safe immutable job views."""

    def __init__(
        self,
        downloader: Downloader,
        *,
        max_workers: int = 3,
        history_store: Optional[JobHistoryStore] = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self._downloader = downloader
        self._history = history_store
        self._last_history_save = 0.0
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="fetcher-download")
        self._lock = threading.RLock()
        self._jobs: dict[str, DownloadJob] = {}
        self._futures: dict[tuple[str, int], Future[None]] = {}
        self._generations: dict[str, int] = {}
        self._terminal_requests: dict[tuple[str, int], str] = {}
        self._job_revisions: dict[str, int] = {}
        self._revision = 0
        self._subscribers: list[Subscriber] = []
        self._closed = False
        self._notifications: Queue[Optional[tuple[JobSnapshot, tuple[Subscriber, ...]]]] = Queue()
        self._notification_thread = threading.Thread(
            target=self._dispatch_notifications,
            name="fetcher-job-notifications",
            daemon=True,
        )
        self._notification_thread.start()
        if history_store:
            self._jobs = {job.video.id: job for job in history_store.load()}

    def subscribe(self, subscriber: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(subscriber)

        def unsubscribe() -> None:
            with self._lock:
                if subscriber in self._subscribers:
                    self._subscribers.remove(subscriber)

        return unsubscribe

    def submit(self, video: Video) -> JobSnapshot:
        with self._lock:
            if self._closed:
                raise RuntimeError("download manager is closed")
            current = self._jobs.get(video.id)
            if current and current.status in ACTIVE_STATUSES:
                raise DuplicateJobError(f"active job already exists for {video.id}")
            previous = current
            generation = self._generations.get(video.id, 0) + 1
            job = DownloadJob(video=video)
            self._jobs[video.id] = job
            self._generations[video.id] = generation
            try:
                snapshot, subscribers = self._record_locked(job, strict=True)
            except Exception:
                if previous is None:
                    self._jobs.pop(video.id, None)
                    self._generations.pop(video.id, None)
                else:
                    self._jobs[video.id] = previous
                    self._generations[video.id] = generation - 1
                raise
            start = threading.Event()
            key = (video.id, generation)
            future = self._executor.submit(self._run, job, generation, start)
            self._futures[key] = future
            future.add_done_callback(lambda completed, run_key=key: self._forget_run(run_key, completed))
        self._notify(snapshot, subscribers)
        start.set()
        return snapshot

    def retry(self, video_id: str) -> JobSnapshot:
        with self._lock:
            previous = self._require(video_id)
            if previous.status not in {"failed", "cancelled", "interrupted"}:
                raise ValueError(f"job {video_id} cannot be retried from {previous.status}")
            generation = self._generations.get(video_id, 0) + 1
            job = DownloadJob(video=previous.video)
            self._jobs[video_id] = job
            self._generations[video_id] = generation
            try:
                snapshot, subscribers = self._record_locked(job, strict=True)
            except Exception:
                self._jobs[video_id] = previous
                self._generations[video_id] = generation - 1
                raise
            start = threading.Event()
            key = (video_id, generation)
            future = self._executor.submit(self._run, job, generation, start)
            self._futures[key] = future
            future.add_done_callback(lambda completed, run_key=key: self._forget_run(run_key, completed))
        self._notify(snapshot, subscribers)
        start.set()
        return snapshot

    def stop(self, video_id: str) -> JobSnapshot:
        with self._lock:
            job = self._require(video_id)
            if job.status not in ACTIVE_STATUSES:
                return self._snapshot(job)
            generation = self._generations[video_id]
            future = self._futures.get((video_id, generation))
            if future:
                future.cancel()
            self._terminal_requests[(video_id, generation)] = "cancelled"
            proc = job.proc
            was_paused = job.status == "paused"
            job.status = "cancelled"
            job.eta = "cancelled"
            job.speed = ""
            snapshot, subscribers = self._record_locked(job)
        if proc is not None:
            terminate_process_group(proc, paused=was_paused)
        self._notify(snapshot, subscribers)
        return snapshot

    def get(self, video_id: str) -> JobSnapshot:
        with self._lock:
            return self._snapshot(self._require(video_id))

    def snapshots(self) -> tuple[JobSnapshot, ...]:
        with self._lock:
            return tuple(self._snapshot(job) for job in self._jobs.values())

    def clear_completed(self) -> int:
        """Remove terminal history entries while retaining active run state."""
        with self._lock:
            removed = {
                video_id: job
                for video_id, job in self._jobs.items()
                if job.status in TERMINAL_STATUSES
            }
            if not removed:
                return 0
            previous_generations = {
                video_id: self._generations.get(video_id) for video_id in removed
            }
            previous_revisions = {
                video_id: self._job_revisions.get(video_id) for video_id in removed
            }
            previous_requests = {
                key: status
                for key, status in self._terminal_requests.items()
                if key[0] in removed
            }
            for video_id in removed:
                self._jobs.pop(video_id, None)
                self._generations.pop(video_id, None)
                self._job_revisions.pop(video_id, None)
                if not any(key[0] == video_id for key in self._futures):
                    for key in tuple(self._terminal_requests):
                        if key[0] == video_id:
                            self._terminal_requests.pop(key, None)
            try:
                if self._history:
                    self._history.save(self._jobs.values())
            except Exception:
                self._jobs.update(removed)
                for video_id, generation in previous_generations.items():
                    if generation is not None:
                        self._generations[video_id] = generation
                for video_id, revision in previous_revisions.items():
                    if revision is not None:
                        self._job_revisions[video_id] = revision
                self._terminal_requests.update(previous_requests)
                raise
            return len(removed)

    def wait(self) -> None:
        with self._lock:
            futures = tuple(self._futures.values())
        for future in futures:
            if not future.cancelled():
                future.result()
        if threading.current_thread() is not self._notification_thread:
            self._notifications.join()

    def close(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        notifications: list[tuple[JobSnapshot, tuple[Subscriber, ...]]] = []
        processes: list[tuple[subprocess.Popen, bool]] = []
        save_error: Optional[Exception] = None
        with self._lock:
            self._closed = True
            if cancel_futures:
                for video_id, job in self._jobs.items():
                    if job.status not in ACTIVE_STATUSES:
                        continue
                    generation = self._generations.get(video_id, 0)
                    self._terminal_requests[(video_id, generation)] = "interrupted"
                    future = self._futures.get((video_id, generation))
                    if future:
                        future.cancel()
                    if job.proc is not None:
                        processes.append((job.proc, job.status == "paused"))
                    job.status = "interrupted"
                    job.eta = "interrupted by manager shutdown"
                    job.speed = ""
                    job.worker_started = False
                try:
                    if self._history:
                        self._history.save(self._jobs.values())
                except Exception as exc:
                    save_error = exc
                for job in self._jobs.values():
                    if job.status == "interrupted":
                        notifications.append(self._record_locked(job))
        for proc, was_paused in processes:
            terminate_process_group(proc, paused=was_paused)
        for snapshot, subscribers in notifications:
            self._notify(snapshot, subscribers)
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
        if wait:
            if threading.current_thread() is not self._notification_thread:
                self._notifications.join()
            self._notifications.put(None)
        if save_error:
            raise save_error

    def _run(self, job: DownloadJob, generation: int, start: threading.Event) -> None:
        start.wait()
        with self._lock:
            if not self._is_current_locked(job, generation) or (job.video.id, generation) in self._terminal_requests:
                job.worker_started = False
                return
            job.status = "downloading"
            job.worker_started = True
            snapshot, subscribers = self._record_locked(job)
        self._notify(snapshot, subscribers)

        def progress() -> None:
            with self._lock:
                if not self._is_current_locked(job, generation):
                    return
                self._apply_terminal_request_locked(job, generation)
                current_snapshot, current_subscribers = self._record_locked(job, progress_only=True)
            self._notify(current_snapshot, current_subscribers)

        def done(_: DownloadJob) -> None:
            with self._lock:
                if not self._is_current_locked(job, generation):
                    return
                self._apply_terminal_request_locked(job, generation)
                current_snapshot, current_subscribers = self._record_locked(job)
            self._notify(current_snapshot, current_subscribers)

        try:
            self._downloader(job, progress, done)
            with self._lock:
                if not self._is_current_locked(job, generation):
                    return
                self._apply_terminal_request_locked(job, generation)
                if job.status in {"cancelled", "interrupted"}:
                    pass
                elif job.status not in TERMINAL_STATUSES:
                    job.status = "failed"
                    job.eta = "download worker returned without a terminal status"
        except Exception as exc:
            with self._lock:
                if not self._is_current_locked(job, generation):
                    return
                self._apply_terminal_request_locked(job, generation)
                if job.status not in {"cancelled", "interrupted"}:
                    job.status = "failed"
                    job.progress = 0.0
                    job.speed = ""
                    job.eta = f"download worker failed: {exc}"
                    job.proc = None
        finally:
            final_notification: Optional[tuple[JobSnapshot, tuple[Subscriber, ...]]] = None
            with self._lock:
                if self._is_current_locked(job, generation):
                    self._apply_terminal_request_locked(job, generation)
                    job.worker_started = False
                    final_notification = self._record_locked(job)
            if final_notification:
                self._notify(*final_notification)

    def _is_current_locked(self, job: DownloadJob, generation: int) -> bool:
        return self._jobs.get(job.video.id) is job and self._generations.get(job.video.id) == generation

    def _apply_terminal_request_locked(self, job: DownloadJob, generation: int) -> None:
        requested = self._terminal_requests.get((job.video.id, generation))
        if requested:
            job.status = requested
            job.eta = "cancelled" if requested == "cancelled" else "interrupted by manager shutdown"
            job.speed = ""

    def _require(self, video_id: str) -> DownloadJob:
        try:
            return self._jobs[video_id]
        except KeyError as exc:
            raise KeyError(f"unknown job: {video_id}") from exc

    def _record_locked(self, job: DownloadJob, *, strict: bool = False, progress_only: bool = False) -> tuple[JobSnapshot, tuple[Subscriber, ...]]:
        # Keep live notifications frequent; checkpoint byte/progress changes at most
        # once per second. Queue and terminal transitions are always durable.
        if self._history and (not progress_only or time.monotonic() - self._last_history_save >= 1.0):
            try:
                self._history.save(self._jobs.values())
                self._last_history_save = time.monotonic()
            except Exception as exc:
                if strict:
                    raise
                job.status = "failed"
                job.eta = f"job history save failed: {exc}"
                job.worker_started = False
        self._revision += 1
        self._job_revisions[job.video.id] = self._revision
        snapshot = self._snapshot(job)
        return snapshot, tuple(self._subscribers)

    def _notify(self, snapshot: JobSnapshot, subscribers: tuple[Subscriber, ...]) -> None:
        self._notifications.put((snapshot, subscribers))

    def _dispatch_notifications(self) -> None:
        pending: dict[int, tuple[JobSnapshot, tuple[Subscriber, ...]]] = {}
        next_revision = 1
        while True:
            item = self._notifications.get()
            if item is None:
                self._notifications.task_done()
                return
            pending[item[0].revision] = item
            while next_revision in pending:
                snapshot, subscribers = pending.pop(next_revision)
                for subscriber in subscribers:
                    try:
                        subscriber(snapshot)
                    except Exception:
                        continue
                self._notifications.task_done()
                next_revision += 1

    def _forget_run(self, key: tuple[str, int], future: Future[None]) -> None:
        with self._lock:
            if self._futures.get(key) is future:
                self._futures.pop(key, None)
            self._terminal_requests.pop(key, None)

    def _snapshot(self, job: DownloadJob) -> JobSnapshot:
        return JobSnapshot(
            revision=self._job_revisions.get(job.video.id, 0),
            id=job.video.id,
            title=job.video.title,
            site=job.video.site,
            url=job.video.url,
            thumbnail_url=job.video.thumbnail_url,
            status=job.status,
            progress=job.progress,
            speed=job.speed,
            message=job.eta,
            filepath=Path(job.filepath) if job.filepath else None,
            file_size=job.file_size,
            downloaded_bytes=job.downloaded_bytes,
            worker_started=job.worker_started,
        )
