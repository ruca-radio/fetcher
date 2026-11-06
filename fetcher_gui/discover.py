from __future__ import annotations

import inspect
import threading
from collections.abc import Iterable

from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, QSize, QThreadPool, Qt, Signal, Slot
from PySide6.QtGui import QColor, QImage, QPixmap
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

from fetcher_core.download_log import without_downloaded
from fetcher_core.models import Video

from .workers import Task
from .task_pool import DaemonTaskPool

THUMBNAIL_DISPLAY_SIZE = QSize(160, 90)
THUMBNAIL_ROW_HEIGHT = 98


def _accepts_keyword(fn, name: str) -> bool:
    try:
        parameters = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()):
        return True
    return name in parameters


class SearchResultsModel(QAbstractTableModel):
    HEADERS = ("Title", "Source", "Duration", "Views", "Quality")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._videos: list[Video] = []
        self._selected: set[tuple[str, str]] = set()
        self._thumbnails: dict[tuple[str, str], QPixmap] = {}
        self._display_thumbnails: dict[tuple[str, str], QPixmap] = {}
        self._is_downloaded = lambda _video: False

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
        downloaded = self._is_downloaded(video)
        if role == Qt.ItemDataRole.ForegroundRole:
            return QColor(138, 138, 138) if downloaded else None
        if role == Qt.ItemDataRole.ToolTipRole and downloaded:
            return "Already downloaded"
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

    def set_downloaded(self, predicate) -> None:
        self._is_downloaded = predicate
        if not self._videos:
            return
        self.dataChanged.emit(
            self.index(0, 0),
            self.index(self.rowCount() - 1, self.columnCount() - 1),
            [Qt.ItemDataRole.ForegroundRole, Qt.ItemDataRole.ToolTipRole],
        )

    def set_results(self, videos: Iterable[Video], *, retain: Iterable[tuple[str, str]] | None = None) -> None:
        self.beginResetModel()
        self._videos = list(videos)
        visible_identities = {(video.site, video.id) for video in self._videos}
        retained_identities = set(visible_identities)
        if retain is not None:
            retained_identities.update(retain)
        self._selected.intersection_update(visible_identities)
        valid_identities = retained_identities
        self._thumbnails = {
            identity: thumbnail for identity, thumbnail in self._thumbnails.items()
            if identity in valid_identities
        }
        self._display_thumbnails = {
            identity: thumbnail for identity, thumbnail in self._display_thumbnails.items()
            if identity in valid_identities
        }
        self.endResetModel()

    def update_results(self, videos: Iterable[Video], *, retain: Iterable[tuple[str, str]] | None = None) -> None:
        """Append when the new list extends the current one; otherwise replace it."""

        videos = list(videos)
        if videos[:len(self._videos)] == self._videos:
            start = len(self._videos)
            if start == len(videos):
                return
            self.beginInsertRows(QModelIndex(), start, len(videos) - 1)
            self._videos = videos
            self.endInsertRows()
            return
        self.set_results(videos, retain=retain)

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
    download_snapshot_received = Signal(object)

    def __init__(self, search_service=None, preview_service=None, download_manager=None,
                 *, search_filters=None, thread_pool: QThreadPool | None = None,
                 thumbnail_loader=None, download_catalog=None, parent=None) -> None:
        super().__init__(parent)
        self.search_service = search_service
        self.preview_service = preview_service
        self.download_manager = download_manager
        self.download_catalog = download_catalog
        self.thumbnail_loader = thumbnail_loader or self._load_thumbnail
        self.thread_pool = thread_pool or QThreadPool(self)
        self._search_pool = DaemonTaskPool(max_workers=2)
        self.search_generation = 0
        self.preview_generation = 0
        self.preview_source: str | None = None
        self._pinned_identity: tuple[str, str] | None = None
        self._hovered_identity: tuple[str, str] | None = None
        self._failed_thumbnails: set[tuple[str, str]] = set()
        self._thumbnail_ids: set[tuple[str, str]] = set()
        self._raw_results: list[Video] = []
        self._search_warning = ""
        self._search_cancel = threading.Event()
        self._search_final_generation = -1
        self._download_unsubscribe = None
        self._tasks: set[Task] = set()
        self._preview_tasks: set[Task] = set()

        self.query_edit = QLineEdit(self)
        self.query_edit.setPlaceholderText("Search videos")
        self.provider_combo = QComboBox(self)
        self.provider_combo.addItems(["All Sites", "PornHub", "xHamster", "RedTube", "Eporner", "SearXNG", "Twitter"])
        self.search_button = QPushButton("Search", self)
        self.clear_button = QPushButton("Clear", self)
        self.clear_button.setObjectName("btn-clear")
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
        self.hide_downloaded_checkbox = QCheckBox("Hide downloaded", self)
        self.hide_downloaded_checkbox.setChecked(True)
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
        controls.addWidget(self.clear_button)
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
        filters.addWidget(self.hide_downloaded_checkbox)
        filters.addWidget(self.pages_spin)
        filters.addWidget(self.exclude_edit, 1)
        layout.addLayout(filters)
        layout.addWidget(self.results_view, 1)
        layout.addWidget(self.error_label)

        self.search_button.clicked.connect(self.search)
        self.clear_button.clicked.connect(self.clear_search)
        self.query_edit.returnPressed.connect(self.search)
        self.hide_downloaded_checkbox.toggled.connect(self._apply_download_filter)
        self.download_snapshot_received.connect(self._on_download_snapshot)
        if download_manager is not None and hasattr(download_manager, "subscribe"):
            self._download_unsubscribe = download_manager.subscribe(self.download_snapshot_received.emit)
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

    def _cancel_active_search(self) -> None:
        self._search_cancel.set()
        self._search_pool.clear()

    def _reset_search_view(self, status: str) -> None:
        self.begin_preview_generation()
        self.preview_source = None
        self._hovered_identity = None
        self._pinned_identity = None
        self._failed_thumbnails.clear()
        self._thumbnail_ids.clear()
        self._raw_results = []
        self._search_warning = ""
        self._search_final_generation = -1
        self.results_model.set_results([])
        self.error_label.setText(status)
        self._emit_active_preview()

    @Slot()
    def clear_search(self) -> None:
        self._cancel_active_search()
        self._search_cancel = threading.Event()
        self.begin_search_generation()
        self.query_edit.clear()
        self._reset_search_view("")
        self.search_button.setEnabled(True)

    @Slot()
    def search(self) -> None:
        self._cancel_active_search()
        self._search_cancel = threading.Event()
        generation = self.begin_search_generation()
        query = self.query_edit.text().strip()
        if not query or self.search_service is None:
            self.accept_search_result(generation, {"results": [], "warning": "Enter a search query."})
            return
        self._reset_search_view("Searching…")
        search = self.search_service.search
        arguments: dict[str, object] = {}
        if _accepts_keyword(search, "on_progress"):
            def on_progress(payload: object, token: int = generation) -> None:
                self.search_result_ready.emit(token, payload)
            arguments["on_progress"] = on_progress
        if _accepts_keyword(search, "cancel_event"):
            arguments["cancel_event"] = self._search_cancel
        task = Task(search, query, self.provider_combo.currentText(), self.search_filters(), **arguments)
        task.signals.result.connect(
            lambda result, token=generation: self.search_result_ready.emit(token, result),
            Qt.ConnectionType.QueuedConnection,
        )
        task.signals.error.connect(
            lambda error, token=generation: self._show_search_error(token, error),
            Qt.ConnectionType.QueuedConnection,
        )
        self._start_task(task, report_error=False, pool=self._search_pool)

    def accept_search_result(self, generation: int, videos: Iterable[Video]) -> None:
        if generation != self.search_generation:
            return
        partial = False
        warning = ""
        if isinstance(videos, dict):
            partial = bool(videos.get("partial"))
            warning = str(videos.get("warning") or "")
            results = list(videos.get("results") or [])
        else:
            results = list(videos)
        if partial and generation == self._search_final_generation:
            return
        if partial and not results:
            return
        if not partial:
            self._search_final_generation = generation
        if not warning and not results and not partial:
            warning = "No videos matched this search."
        self._reload_download_catalog()
        visible = self._without_downloaded(results)
        new_ids = [(video.site, video.id) for video in visible]
        previous_ids = [(video.site, video.id) for video in self.results_model._videos]
        same_rows = new_ids == previous_ids and bool(previous_ids)
        if same_rows:
            self._raw_results = results
            self._search_warning = warning
            if not partial:
                self.error_label.setText(self._result_message(warning, results, visible))
            return
        if not partial and new_ids != previous_ids:
            self.begin_preview_generation()
            self.preview_source = None
            self._hovered_identity = None
            self._pinned_identity = None
            self._failed_thumbnails.clear()
        visible_ids = set(new_ids)
        if self._pinned_identity not in visible_ids:
            self._pinned_identity = None
        if self._hovered_identity not in visible_ids:
            self._hovered_identity = None
        self._raw_results = results
        self._search_warning = warning
        message = self._result_message(warning, results, visible)
        if partial:
            message = f"Searching… {message}".strip()
        self.error_label.setText(message)
        self.results_model.set_downloaded(self._video_is_downloaded)
        retained = [(video.site, video.id) for video in results]
        if partial or visible[:len(self.results_model._videos)] == self.results_model._videos:
            self.results_model.update_results(visible, retain=retained)
        else:
            self.results_model.set_results(visible, retain=retained)
        self._thumbnail_ids.intersection_update(retained)
        self._emit_active_preview()
        for video in results:
            if not video.thumbnail_url:
                continue
            identity = (video.site, video.id)
            if identity in self._thumbnail_ids or self.results_model.thumbnail_at(*identity) is not None:
                self._thumbnail_ids.add(identity)
                continue
            self._thumbnail_ids.add(identity)
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
        task.signals.error.connect(
            lambda error, token=generation: self._show_preview_error(token, error),
            Qt.ConnectionType.QueuedConnection,
        )
        self._preview_tasks.add(task)
        task.signals.finished.connect(lambda task=task: self._preview_tasks.discard(task))
        self._start_task(task, report_error=False)

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

    def _reload_download_catalog(self) -> None:
        if self.download_catalog is not None:
            self.download_catalog.reload()

    def _video_is_downloaded(self, video: Video) -> bool:
        catalog = self.download_catalog
        return False if catalog is None else catalog.contains(video)

    def _without_downloaded(self, videos: list[Video]) -> list[Video]:
        if self.download_catalog is None or not self.hide_downloaded_checkbox.isChecked():
            return list(videos)
        return without_downloaded(videos, self.download_catalog.ids, self.download_catalog.titles)

    def _result_message(self, warning: str, raw: list[Video], visible: list[Video]) -> str:
        hidden = len(raw) - len(visible) if self.hide_downloaded_checkbox.isChecked() else 0
        if raw and not visible and hidden:
            note = "Every result is already downloaded. Uncheck Hide downloaded to show them."
            if warning and warning != "No videos matched this search.":
                return f"{warning} {note}"
            return note
        if hidden:
            note = f"{hidden} downloaded hidden."
            return f"{warning} {note}".strip() if warning else note
        return warning

    def _apply_download_filter(self, _checked: bool = False) -> None:
        raw = self._raw_results
        self._reload_download_catalog()
        visible = self._without_downloaded(raw)
        self.results_model.set_downloaded(self._video_is_downloaded)
        self.results_model.set_results(visible, retain=[(video.site, video.id) for video in raw])
        self.error_label.setText(self._result_message(self._search_warning, raw, visible))
        self._emit_active_preview()

    def _on_download_snapshot(self, snapshot) -> None:
        if getattr(snapshot, "status", "") != "done" or self.download_catalog is None:
            return
        self._apply_download_filter()

    def invalidate(self) -> None:
        unsubscribe = self._download_unsubscribe
        self._download_unsubscribe = None
        if unsubscribe is not None:
            unsubscribe()
        self.begin_search_generation()
        self.begin_preview_generation()
        for task in tuple(self._tasks):
            task.cancel()
        self._search_pool.shutdown(cancel_pending=True)

    def set_submissions_enabled(self, enabled: bool) -> None:
        for button in (self.search_button, self.clear_button, self.preview_button, self.queue_button):
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

    def _show_search_error(self, generation: int, error) -> None:
        if generation != self.search_generation:
            return
        self._show_task_error(error)

    def _show_preview_error(self, generation: int, error) -> None:
        if generation != self.preview_generation:
            return
        self._show_task_error(error)

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
