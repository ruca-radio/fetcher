from __future__ import annotations

import logging
import threading
import time
from queue import Empty, Full, Queue

logger = logging.getLogger(__name__)


class DaemonTaskPool:
    """Small Qt-compatible pool whose stuck tasks cannot block process exit."""

    def __init__(self, *, max_workers: int = 4, max_pending: int = 128) -> None:
        if max_workers < 1 or max_pending < max_workers:
            raise ValueError("max_workers must be positive and max_pending must cover all workers")
        self._condition = threading.Condition()
        self._tasks: dict[int, object] = {}
        self._queue: Queue[tuple[int, object]] = Queue(maxsize=max_pending)
        self._running = 0
        self._next_token = 0
        self._closed = False
        self._workers = tuple(
            threading.Thread(target=self._worker, name=f"fetcher-gui-worker-{index + 1}", daemon=True)
            for index in range(max_workers)
        )
        for worker in self._workers:
            worker.start()

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    def start(self, task) -> None:
        with self._condition:
            if self._closed:
                raise RuntimeError("task pool is closed")
            self._next_token += 1
            token = self._next_token
            self._tasks[token] = task
        try:
            self._queue.put_nowait((token, task))
        except Full as exc:
            with self._condition:
                self._tasks.pop(token, None)
                self._condition.notify_all()
            raise RuntimeError("task pool queue is full") from exc

    def _worker(self) -> None:
        while True:
            token, task = self._queue.get()
            if task is None:
                self._queue.task_done()
                return
            with self._condition:
                if token not in self._tasks:
                    self._queue.task_done()
                    continue
                self._running += 1
            try:
                task.run() if hasattr(task, "run") else task()
            except Exception:
                logger.exception("Fetcher background task failed")
            finally:
                with self._condition:
                    self._running -= 1
                    self._tasks.pop(token, None)
                    self._condition.notify_all()
                self._queue.task_done()

    def clear(self) -> None:
        with self._condition:
            tasks = tuple(self._tasks.values())
        for task in tasks:
            cancel = getattr(task, "cancel", None)
            if cancel is not None:
                cancel()
        while True:
            try:
                token, _task = self._queue.get_nowait()
            except Empty:
                break
            with self._condition:
                self._tasks.pop(token, None)
                self._condition.notify_all()
            self._queue.task_done()

    def shutdown(self, *, cancel_pending: bool = True) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
        if cancel_pending:
            self.clear()
        for _worker in self._workers:
            self._queue.put_nowait((0, None))

    def waitForDone(self, milliseconds: int = -1) -> bool:
        deadline = None if milliseconds < 0 else time.monotonic() + milliseconds / 1000
        with self._condition:
            while self._tasks:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def activeThreadCount(self) -> int:
        with self._condition:
            return self._running

    @staticmethod
    def parent():
        return None


