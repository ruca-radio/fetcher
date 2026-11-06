from __future__ import annotations

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
from .task_pool import DaemonTaskPool


class MainWindow(QMainWindow):
    def __init__(self, *, search_service=None, preview_service=None, download_manager=None,
                 local_storage=None, plex_storage=None, settings_service=None, diagnostics=None,
                 player=None, thread_pool: QThreadPool | None = None, shutdown_grace_ms: int = 1_000,
                 confirm=None, download_catalog=None,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Fetcher")
        self.resize(1180, 760)
        self.thread_pool = thread_pool or DaemonTaskPool()
        self.download_manager = download_manager
        self.shutdown_grace_ms = max(0, shutdown_grace_ms)
        self._shutting_down = False
        self._library_loaded = False
        self.player = player or MediaPlayerWidget(self)
        persisted_settings = getattr(settings_service, "settings", None)
        search_filters = getattr(persisted_settings, "search_filters", None)
        self.discover_page = DiscoverPage(
            search_service, preview_service, download_manager, search_filters=search_filters,
            thread_pool=self.thread_pool, download_catalog=download_catalog, parent=self,
        )
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
            button.clicked.connect(lambda _checked=False, index=index: self._navigate(index))
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
        self.discover_page.focused_result.connect(self.player.show_thumbnail)
        self.library_page.preview_ready.connect(self.player.set_source)

    def _navigate(self, index: int) -> None:
        self.workspace_stack.setCurrentIndex(index)
        if index == 2 and not self._library_loaded:
            self._library_loaded = True
            self.library_page.refresh()

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
