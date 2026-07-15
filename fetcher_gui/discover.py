from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QThreadPool, Qt, Signal, Slot
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
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


class SearchResultsModel(QAbstractTableModel):
    HEADERS = ("Title", "Source", "Duration", "Views", "Quality")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._videos: list[Video] = []
        self._selected: set[str] = set()
        self._thumbnails: dict[str, QPixmap] = {}

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._videos)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._videos):
            return None
        video = self._videos[index.row()]
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            return Qt.CheckState.Checked if video.id in self._selected else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DecorationRole and index.column() == 0:
            return self._thumbnails.get(video.id)
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
        video_id = self._videos[index.row()].id
        if value == Qt.CheckState.Checked or value == int(Qt.CheckState.Checked):
            self._selected.add(video_id)
        else:
            self._selected.discard(video_id)
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
        valid_ids = {video.id for video in self._videos}
        self._selected.intersection_update(valid_ids)
        self.endResetModel()

    def video_at(self, row: int) -> Video:
        return self._videos[row]

    def selected_videos(self) -> tuple[Video, ...]:
        return tuple(video for video in self._videos if video.id in self._selected)

    def set_thumbnail(self, video_id: str, image: QImage | QPixmap) -> None:
        pixmap = image if isinstance(image, QPixmap) else QPixmap.fromImage(image)
        self._thumbnails[video_id] = pixmap
        for row, video in enumerate(self._videos):
            if video.id == video_id:
                index = self.index(row, 0)
                self.dataChanged.emit(index, index, [Qt.ItemDataRole.DecorationRole])
                break

    def thumbnail_at(self, video_id: str) -> QPixmap | None:
        return self._thumbnails.get(video_id)


class DiscoverPage(QWidget):
    preview_ready = Signal(object)
    search_result_ready = Signal(int, object)
    preview_result_ready = Signal(int, object)
    thumbnail_result_ready = Signal(int, str, object)

    def __init__(self, search_service=None, preview_service=None, download_manager=None,
                 *, thread_pool: QThreadPool | None = None, thumbnail_loader=None, parent=None) -> None:
        super().__init__(parent)
        self.search_service = search_service
        self.preview_service = preview_service
        self.download_manager = download_manager
        self.thumbnail_loader = thumbnail_loader or self._load_thumbnail
        self.thread_pool = thread_pool or QThreadPool(self)
        self.search_generation = 0
        self.preview_generation = 0
        self.preview_source: str | None = None
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
        self.preview_button = QPushButton("Preview", self)
        self.queue_button = QPushButton("Queue selected", self)
        self.error_label = QLabel("", self)
        self.error_label.setWordWrap(True)
        self.results_model = SearchResultsModel(self)
        self.results_view = QTableView(self)
        self.results_view.setModel(self.results_model)
        self.results_view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)

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
        filters.addStretch(1)
        layout.addLayout(filters)
        layout.addWidget(self.results_view, 1)
        layout.addWidget(self.error_label)

        self.search_button.clicked.connect(self.search)
        self.query_edit.returnPressed.connect(self.search)
        self.preview_button.clicked.connect(self.preview_current)
        self.queue_button.clicked.connect(self.queue_selected)
        self.results_view.selectionModel().selectionChanged.connect(self._selection_changed)
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
            self.accept_search_result(generation, [])
            return
        task = Task(self.search_service.search, query, self.provider_combo.currentText(), self.search_filters())
        task.signals.result.connect(lambda result, token=generation: self.search_result_ready.emit(token, result))
        self._start_task(task)

    def accept_search_result(self, generation: int, videos: Iterable[Video]) -> None:
        if generation == self.search_generation:
            if isinstance(videos, dict):
                warning = str(videos.get("warning") or "")
                if warning:
                    self.error_label.setText(warning)
                results = list(videos.get("results") or [])
            else:
                results = list(videos)
            self.results_model.set_results(results)
            for video in results:
                if video.thumbnail_url:
                    task = Task(self.thumbnail_loader, video.thumbnail_url)
                    task.signals.result.connect(
                        lambda image, video_id=video.id, token=generation:
                        self.thumbnail_result_ready.emit(token, video_id, image)
                    )
                    self._start_task(task)

    def search_filters(self) -> dict[str, object]:
        return {
            "min_duration": self.min_duration_spin.value(),
            "max_duration": self.max_duration_spin.value(),
            "min_rating": self.min_rating_spin.value(),
            "quality": self.quality_combo.currentText(),
        }

    @Slot()
    def preview_current(self) -> None:
        indexes = self.results_view.selectionModel().selectedRows()
        if not indexes or self.preview_service is None:
            return
        generation = self.begin_preview_generation()
        video = self.results_model.video_at(indexes[0].row())
        task = Task(self.preview_service.resolve, video)
        task.signals.result.connect(lambda result, token=generation: self.preview_result_ready.emit(token, result))
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

    def set_submissions_enabled(self, enabled: bool) -> None:
        for button in (self.search_button, self.preview_button, self.queue_button):
            button.setEnabled(enabled)

    def _start_task(self, task: Task) -> None:
        self._tasks.add(task)
        task.signals.error.connect(self._show_task_error)
        task.signals.finished.connect(lambda task=task: self._tasks.discard(task))
        try:
            self.thread_pool.start(task)
        except Exception as exc:
            task.cancel()
            self._tasks.discard(task)
            self._preview_tasks.discard(task)
            self.error_label.setText(f"Background task could not start: {exc}")

    def _accept_thumbnail(self, generation: int, video_id: str, image) -> None:
        if generation == self.search_generation and isinstance(image, (QImage, QPixmap)):
            self.results_model.set_thumbnail(video_id, image)

    @Slot()
    def _selection_changed(self) -> None:
        self.begin_preview_generation()
        self.preview_source = None
        for task in tuple(self._preview_tasks):
            task.cancel()

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
