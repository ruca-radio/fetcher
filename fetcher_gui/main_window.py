from __future__ import annotations

import threading
import time
from queue import Empty, Full, Queue

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .discover import DiscoverPage
from .downloads import DownloadsPage
from .library import LibraryPage
from .player import MediaPlayerWidget
from .settings import SettingsPage


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


class MainWindow(QMainWindow):
    def __init__(self, *, search_service=None, preview_service=None, download_manager=None,
                 local_storage=None, plex_storage=None, settings_service=None, diagnostics=None,
                 player=None, thread_pool: QThreadPool | None = None, shutdown_grace_ms: int = 1_000,
                 confirm=None,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Fetcher")
        self.resize(1180, 760)
        self.thread_pool = thread_pool or DaemonTaskPool()
        self.download_manager = download_manager
        self.shutdown_grace_ms = max(0, shutdown_grace_ms)
        self._shutting_down = False
        self.player = player or MediaPlayerWidget(self)
        self.discover_page = DiscoverPage(search_service, preview_service, download_manager,
                                          thread_pool=self.thread_pool, parent=self)
        self.downloads_page = DownloadsPage(download_manager, self, task_pool=self.thread_pool)
        self.library_page = LibraryPage(
            local_storage, plex_storage, confirm=confirm or self._confirm,
            thread_pool=self.thread_pool, parent=self,
        )
        self.settings_page = SettingsPage(settings_service, diagnostics=diagnostics,
                                          thread_pool=self.thread_pool, parent=self)
        self.workspace_stack = QStackedWidget(self)
        for page in (self.discover_page, self.downloads_page, self.library_page, self.settings_page):
            self.workspace_stack.addWidget(page)
        self.navigation_buttons: list[QPushButton] = []
        navigation = QVBoxLayout()
        for index, name in enumerate(("Discover", "Downloads", "Library", "Settings")):
            button = QPushButton(name, self)
            button.clicked.connect(lambda _checked=False, index=index: self.workspace_stack.setCurrentIndex(index))
            navigation.addWidget(button)
            self.navigation_buttons.append(button)
        navigation.addStretch(1)
        center = QHBoxLayout()
        center.addLayout(navigation)
        center.addWidget(self.workspace_stack, 1)
        layout = QVBoxLayout()
        layout.addLayout(center, 3)
        layout.addWidget(self.player, 2)
        container = QWidget(self)
        container.setLayout(layout)
        self.setCentralWidget(container)
        self.discover_page.preview_ready.connect(self.player.set_source)
        self.library_page.preview_ready.connect(self.player.set_source)

    def _confirm(self, message: str) -> bool:
        return QMessageBox.question(
            self, "Confirm file operation", message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes

    def closeEvent(self, event) -> None:
        if not self._shutting_down:
            self._shutting_down = True
            self.discover_page.set_submissions_enabled(False)
            self.downloads_page.set_submissions_enabled(False)
            self.library_page.set_submissions_enabled(False)
            self.settings_page.set_submissions_enabled(False)
            for button in self.navigation_buttons:
                button.setEnabled(False)
            self.discover_page.invalidate()
            self.library_page.invalidate()
            self.settings_page.invalidate()
            self.downloads_page.shutdown()
            shutdown_pool = getattr(self.thread_pool, "shutdown", None)
            if shutdown_pool is not None:
                shutdown_pool(cancel_pending=True)
            else:
                self.thread_pool.clear()
            if self.download_manager is not None:
                persist = getattr(self.download_manager, "persist_interrupted", None)
                if persist is not None:
                    persist()
                self.download_manager.close(wait=False, cancel_futures=True)
            self.thread_pool.waitForDone(self.shutdown_grace_ms)
        event.accept()
