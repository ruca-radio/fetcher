from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, QSize, QThreadPool, Qt, Signal, Slot
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from fetcher_core.models import Video

from .workers import Task
from .task_pool import DaemonTaskPool

THUMBNAIL_DISPLAY_SIZE = QSize(160, 90)
THUMBNAIL_ROW_HEIGHT = 98


class SearchResultsModel(QAbstractTableModel):
    HEADERS = ("Title", "Source", "Duration", "Views", "Quality")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._videos: list[Video] = []
        self._selected: set[tuple[str, str]] = set()
        self._thumbnails: dict[tuple[str, str], QPixmap] = {}
        self._display_thumbnails: dict[tuple[str, str], QPixmap] = {}

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._videos)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._videos):
            return None
        video = self._videos[index.row()]
        identity = (video.site, video.id)
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            return Qt.CheckState.Checked if identity in self._selected else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DecorationRole and index.column() == 0:
            return self._display_thumbnails.get(identity)
        if role == Qt.ItemDataRole.UserRole:
            return video
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        values = (
            video.title,
            video.site,
            video.duration_str,
            video.views_str,
            f"{video.quality_p}p" if video.quality_p else "",
        )
        return values[index.column()]

    def setData(self, index: QModelIndex, value, role=Qt.ItemDataRole.EditRole) -> bool:
        if role != Qt.ItemDataRole.CheckStateRole or not index.isValid() or index.column() != 0:
            return False
        video = self._videos[index.row()]
        identity = (video.site, video.id)
        if value == Qt.CheckState.Checked or value == Qt.CheckState.Checked.value:
            self._selected.add(identity)
        else:
            self._selected.discard(identity)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        return True

    def flags(self, index: QModelIndex):
        flags = super().flags(index)
        return flags | Qt.ItemFlag.ItemIsUserCheckable if index.isValid() and index.column() == 0 else flags

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return super().headerData(section, orientation, role)

    def set_results(self, videos: Iterable[Video]) -> None:
        self.beginResetModel()
        self._videos = list(videos)
        valid_identities = {(video.site, video.id) for video in self._videos}
        self._selected.intersection_update(valid_identities)
        self._thumbnails = {
            identity: thumbnail for identity, thumbnail in self._thumbnails.items()
            if identity in valid_identities
        }
        self._display_thumbnails = {
            identity: thumbnail for identity, thumbnail in self._display_thumbnails.items()
            if identity in valid_identities
        }
        self.endResetModel()

    def video_at(self, row: int) -> Video:
        return self._videos[row]

    def selected_videos(self) -> tuple[Video, ...]:
        return tuple(video for video in self._videos if (video.site, video.id) in self._selected)

    def set_thumbnail(self, site: str, video_id: str, image: QImage | QPixmap) -> None:
        identity = (site, video_id)
        pixmap = image if isinstance(image, QPixmap) else QPixmap.fromImage(image)
        self._thumbnails[identity] = pixmap
        self._display_thumbnails[identity] = pixmap.scaled(
            THUMBNAIL_DISPLAY_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        for row, video in enumerate(self._videos):
            if (video.site, video.id) == identity:
                index = self.index(row, 0)
                self.dataChanged.emit(index, index, [Qt.ItemDataRole.DecorationRole])
                break

    def thumbnail_at(self, site: str, video_id: str) -> QPixmap | None:
        return self._thumbnails.get((site, video_id))


class DiscoverPage(QWidget):
    preview_ready = Signal(object)
    focused_result = Signal(object, object)
    search_result_ready = Signal(int, object)
    preview_result_ready = Signal(int, object)
    thumbnail_result_ready = Signal(int, str, str, object)

    def __init__(self, search_service=None, preview_service=None, download_manager=None,
                 *, search_filters=None, thread_pool: QThreadPool | None = None,
                 thumbnail_loader=None, parent=None) -> None:
        super().__init__(parent)
        self.search_service = search_service
        self.preview_service = preview_service
        self.download_manager = download_manager
        self.thumbnail_loader = thumbnail_loader or self._load_thumbnail
        self.thread_pool = thread_pool or QThreadPool(self)
        self._search_pool = DaemonTaskPool(max_workers=1)
        self.search_generation = 0
        self.preview_generation = 0
        self.preview_source: str | None = None
        self._pinned_identity: tuple[str, str] | None = None
        self._hovered_identity: tuple[str, str] | None = None
        self._failed_thumbnails: set[tuple[str, str]] = set()
        self._tasks: set[Task] = set()
        self._preview_tasks: set[Task] = set()

        self.query_edit = QLineEdit(self)
        self.query_edit.setPlaceholderText("Search videos")
        self.provider_combo = QComboBox(self)
        self.provider_combo.addItems(["All Sites", "PornHub", "xHamster", "RedTube", "Eporner", "Twitter"])
        self.search_button = QPushButton("Search", self)
        self.min_duration_spin = QSpinBox(self)
        self.min_duration_spin.setRange(0, 1_440)
        self.min_duration_spin.setPrefix("Min ")
        self.min_duration_spin.setSuffix(" min")
        self.max_duration_spin = QSpinBox(self)
        self.max_duration_spin.setRange(0, 1_440)
        self.max_duration_spin.setPrefix("Max ")
        self.max_duration_spin.setSuffix(" min")
        self.min_rating_spin = QSpinBox(self)
        self.min_rating_spin.setRange(0, 100)
        self.min_rating_spin.setPrefix("Rating ")
        self.min_rating_spin.setSuffix("%")
        self.quality_combo = QComboBox(self)
        self.quality_combo.addItems(["any", "720", "1080"])
        self.hd_only_checkbox = QCheckBox("HD only", self)
        self.pages_spin = QSpinBox(self)
        self.pages_spin.setRange(1, 20)
        self.pages_spin.setPrefix("Pages ")
        self.pages_spin.setValue(2)
        self.exclude_edit = QLineEdit(self)
        self.exclude_edit.setPlaceholderText("Exclude phrases")
        if search_filters is not None:
            self.min_duration_spin.setValue(search_filters.min_duration_min)
            self.max_duration_spin.setValue(search_filters.max_duration_min)
            self.min_rating_spin.setValue(search_filters.min_rating)
            self.quality_combo.setCurrentText(search_filters.min_quality)
            self.hd_only_checkbox.setChecked(bool(search_filters.hd_only))
            self.pages_spin.setValue(max(1, int(search_filters.pages or 1)))
            self.exclude_edit.setText(str(search_filters.exclude_phrases or ""))
        self.preview_button = QPushButton("Preview", self)
        self.queue_button = QPushButton("Queue selected", self)
        self.error_label = QLabel("", self)
        self.error_label.setWordWrap(True)
        self.results_model = SearchResultsModel(self)
        self.results_view = QTableView(self)
        self.results_view.setModel(self.results_model)
        self.results_view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.results_view.setIconSize(THUMBNAIL_DISPLAY_SIZE)
        self.results_view.setMouseTracking(True)
        self.results_view.viewport().setMouseTracking(True)
        vertical_header = self.results_view.verticalHeader()
        vertical_header.setMinimumSectionSize(THUMBNAIL_ROW_HEIGHT)
        vertical_header.setDefaultSectionSize(THUMBNAIL_ROW_HEIGHT)
        vertical_header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.results_view.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

        controls = QHBoxLayout()
        controls.addWidget(self.provider_combo)
        controls.addWidget(self.query_edit, 1)
        controls.addWidget(self.search_button)
        controls.addWidget(self.preview_button)
        controls.addWidget(self.queue_button)
        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        filters = QHBoxLayout()
        filters.addWidget(self.min_duration_spin)
        filters.addWidget(self.max_duration_spin)
        filters.addWidget(self.min_rating_spin)
        filters.addWidget(self.quality_combo)
        filters.addWidget(self.hd_only_checkbox)
        filters.addWidget(self.pages_spin)
        filters.addWidget(self.exclude_edit, 1)
        layout.addLayout(filters)
        layout.addWidget(self.results_view, 1)
        layout.addWidget(self.error_label)

        self.search_button.clicked.connect(self.search)
        self.query_edit.returnPressed.connect(self.search)
        self.preview_button.clicked.connect(self.preview_current)
        self.queue_button.clicked.connect(self.queue_selected)
        self.results_view.selectionModel().selectionChanged.connect(self._selection_changed)
        self.results_view.entered.connect(self._result_entered)
        self.results_view.viewport().installEventFilter(self)
        self.search_result_ready.connect(self.accept_search_result)
        self.preview_result_ready.connect(self.accept_preview_result)
        self.thumbnail_result_ready.connect(self._accept_thumbnail)

    def begin_search_generation(self) -> int:
        self.search_generation += 1
        return self.search_generation

    def begin_preview_generation(self) -> int:
        self.preview_generation += 1
        return self.preview_generation

    @Slot()
    def search(self) -> None:
        generation = self.begin_search_generation()
        query = self.query_edit.text().strip()
        if not query or self.search_service is None:
            self.accept_search_result(generation, {"results": [], "warning": "Enter a search query."})
            return
        self.error_label.setText("Searching…")
        task = Task(self.search_service.search, query, self.provider_combo.currentText(), self.search_filters())
        task.signals.result.connect(
            lambda result, token=generation: self.search_result_ready.emit(token, result),
            Qt.ConnectionType.QueuedConnection,
        )
        self._start_task(task, pool=self._search_pool)

    def accept_search_result(self, generation: int, videos: Iterable[Video]) -> None:
        if generation == self.search_generation:
            self.begin_preview_generation()
            self.preview_source = None
            warning = ""
            if isinstance(videos, dict):
                warning = str(videos.get("warning") or "")
                results = list(videos.get("results") or [])
            else:
                results = list(videos)
            if not warning and not results:
                warning = "No videos matched this search."
            self.error_label.setText(warning)
            self._hovered_identity = None
            self._pinned_identity = None
            self._failed_thumbnails.clear()
            self.results_model.set_results(results)
            self._emit_active_preview()
            for video in results:
                if video.thumbnail_url:
                    identity = (video.site, video.id)
                    task = Task(self.thumbnail_loader, video.thumbnail_url)
                    task.signals.result.connect(
                        lambda image, site=video.site, video_id=video.id, token=generation:
                        self.thumbnail_result_ready.emit(token, site, video_id, image),
                        Qt.ConnectionType.QueuedConnection,
                    )
                    task.signals.error.connect(
                        lambda _error, site=identity[0], video_id=identity[1], token=generation:
                        self._thumbnail_failed(token, site, video_id),
                        Qt.ConnectionType.QueuedConnection,
                    )
                    self._start_task(task, report_error=False)

    def search_filters(self) -> dict[str, object]:
        return {
            "min_duration": self.min_duration_spin.value(),
            "max_duration": self.max_duration_spin.value(),
            "min_rating": self.min_rating_spin.value(),
            "quality": self.quality_combo.currentText(),
            "hd_only": self.hd_only_checkbox.isChecked(),
            "exclude": self.exclude_edit.text().strip(),
            "pages": self.pages_spin.value(),
        }

    @Slot()
    def preview_current(self) -> None:
        indexes = self.results_view.selectionModel().selectedRows()
        if not indexes or self.preview_service is None:
            return
        generation = self.begin_preview_generation()
        video = self.results_model.video_at(indexes[0].row())
        task = Task(self.preview_service.resolve, video)
        task.signals.result.connect(
            lambda result, token=generation: self.preview_result_ready.emit(token, result),
            Qt.ConnectionType.QueuedConnection,
        )
        self._preview_tasks.add(task)
        task.signals.finished.connect(lambda task=task: self._preview_tasks.discard(task))
        self._start_task(task)

    def accept_preview_result(self, generation: int, source: str) -> None:
        if generation != self.preview_generation:
            return
        self.preview_source = source
        self.preview_ready.emit(source)

    @Slot()
    def queue_selected(self) -> None:
        if self.download_manager is None:
            return
        for video in self.results_model.selected_videos():
            self._start_task(Task(self.download_manager.submit, video))

    def invalidate(self) -> None:
        self.begin_search_generation()
        self.begin_preview_generation()
        for task in tuple(self._tasks):
            task.cancel()
        self._search_pool.shutdown(cancel_pending=True)

    def set_submissions_enabled(self, enabled: bool) -> None:
        for button in (self.search_button, self.preview_button, self.queue_button):
            button.setEnabled(enabled)

    def eventFilter(self, watched, event) -> bool:
        if watched is self.results_view.viewport() and event.type() == QEvent.Type.Leave:
            self._leave_results()
        return super().eventFilter(watched, event)

    def _start_task(self, task: Task, *, report_error: bool = True, pool=None) -> None:
        self._tasks.add(task)
        if report_error:
            task.signals.error.connect(self._show_task_error, Qt.ConnectionType.QueuedConnection)
        task.signals.finished.connect(
            lambda task=task: self._tasks.discard(task),
            Qt.ConnectionType.QueuedConnection,
        )
        try:
            (pool or self.thread_pool).start(task)
        except Exception as exc:
            task.cancel()
            self._tasks.discard(task)
            self._preview_tasks.discard(task)
            self.error_label.setText(f"Background task could not start: {exc}")

    def _accept_thumbnail(self, generation: int, site: str, video_id: str, image) -> None:
        if generation != self.search_generation:
            return
        identity = (site, video_id)
        if isinstance(image, (QImage, QPixmap)):
            self._failed_thumbnails.discard(identity)
            self.results_model.set_thumbnail(site, video_id, image)
            if identity == self._active_preview_identity():
                self._emit_active_preview()

    def _thumbnail_failed(self, generation: int, site: str, video_id: str) -> None:
        if generation != self.search_generation:
            return
        identity = (site, video_id)
        self._failed_thumbnails.add(identity)
        if identity == self._active_preview_identity():
            self._emit_active_preview()

    def _active_preview_identity(self) -> tuple[str, str] | None:
        return self._hovered_identity or self._pinned_identity

    def _video_for_identity(self, identity: tuple[str, str] | None) -> Video | None:
        if identity is None:
            return None
        for row in range(self.results_model.rowCount()):
            video = self.results_model.video_at(row)
            if (video.site, video.id) == identity:
                return video
        return None

    def _emit_active_preview(self) -> None:
        if self.preview_source is not None:
            return
        video = self._video_for_identity(self._active_preview_identity())
        if video is None:
            self.focused_result.emit(None, None)
            return
        identity = (video.site, video.id)
        pixmap = self.results_model.thumbnail_at(video.site, video.id)
        if pixmap is None and identity in self._failed_thumbnails:
            pixmap = QPixmap()
        self.focused_result.emit(video, pixmap)

    def _result_entered(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        video = self.results_model.video_at(index.row())
        identity = (video.site, video.id)
        if self._hovered_identity == identity:
            return
        self._hovered_identity = identity
        self._emit_active_preview()

    def _leave_results(self) -> None:
        if self._hovered_identity is None:
            return
        self._hovered_identity = None
        self._emit_active_preview()

    @Slot()
    def _selection_changed(self) -> None:
        self.begin_preview_generation()
        self.preview_source = None
        for task in tuple(self._preview_tasks):
            task.cancel()
        indexes = self.results_view.selectionModel().selectedRows()
        if not indexes:
            self._pinned_identity = None
            if self._hovered_identity is None:
                self._emit_active_preview()
            return
        video = self.results_model.video_at(indexes[0].row())
        self._pinned_identity = (video.site, video.id)
        if self._hovered_identity is None:
            self._emit_active_preview()

    @Slot(object)
    def _show_task_error(self, error) -> None:
        self.error_label.setText(str(error[1]))

    @staticmethod
    def _load_thumbnail(url: str) -> QImage:
        import httpx

        response = httpx.get(url, timeout=10, follow_redirects=True)
        response.raise_for_status()
        image = QImage()
        if not image.loadFromData(response.content):
            raise ValueError("Unable to decode thumbnail")
        return image
