from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import PurePosixPath

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QThreadPool, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from fetcher_core.storage import StorageEntry

from .workers import Task


class StorageTableModel(QAbstractTableModel):
    HEADERS = ("Name", "Size", "Modified")

    def __init__(self, label: str, parent=None) -> None:
        super().__init__(parent)
        self.label = label
        self._entries: list[StorageEntry] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._entries)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole or not index.isValid():
            return None
        entry = self._entries[index.row()]
        size = "" if entry.size is None else f"{entry.size / (1024 * 1024):.1f} MiB"
        return (str(entry.relpath), size, entry.modified)[index.column()]

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return super().headerData(section, orientation, role)

    def set_entries(self, entries: Iterable[StorageEntry]) -> None:
        self.beginResetModel()
        self._entries = list(entries)
        self.endResetModel()

    def entry_at(self, row_or_index) -> StorageEntry | None:
        row = row_or_index.row() if isinstance(row_or_index, QModelIndex) else row_or_index
        return self._entries[row] if isinstance(row, int) and 0 <= row < len(self._entries) else None


class LibraryPage(QWidget):
    preview_ready = Signal(object)
    entries_ready = Signal(str, object)
    refresh_requested = Signal(str)

    def __init__(self, local_storage=None, plex_storage=None, *, confirm: Callable[[str], bool] | None = None,
                 thread_pool: QThreadPool | None = None, parent=None) -> None:
        super().__init__(parent)
        self.storages = {"local": local_storage, "plex": plex_storage}
        self.confirm = confirm or (lambda _message: False)
        self.thread_pool = thread_pool or QThreadPool(self)
        self._tasks: set[Task] = set()
        self.local_model = StorageTableModel("Local staging", self)
        self.plex_model = StorageTableModel("Plex storage", self)
        self.local_view = QTableView(self)
        self.plex_view = QTableView(self)
        self.local_view.setModel(self.local_model)
        self.plex_view.setModel(self.plex_model)
        self.error_label = QLabel("", self)
        self.error_label.setWordWrap(True)

        panes = QHBoxLayout()
        for label, view in (("Local staging", self.local_view), ("Plex storage", self.plex_view)):
            box = QVBoxLayout()
            box.addWidget(QLabel(label, self))
            box.addWidget(view)
            panes.addLayout(box)
        self.refresh_button = QPushButton("Refresh", self)
        self.copy_button = QPushButton("Copy to Plex", self)
        self.move_button = QPushButton("Move to Plex", self)
        self.preview_button = QPushButton("Preview", self)
        self.rename_button = QPushButton("Rename", self)
        self.delete_button = QPushButton("Delete", self)
        actions = QHBoxLayout()
        for button in (self.refresh_button, self.preview_button, self.copy_button, self.move_button,
                       self.rename_button, self.delete_button):
            actions.addWidget(button)
        layout = QVBoxLayout(self)
        layout.addLayout(panes, 1)
        layout.addLayout(actions)
        layout.addWidget(self.error_label)
        self.refresh_button.clicked.connect(self.refresh)
        self.copy_button.clicked.connect(lambda: self._transfer_active(False))
        self.move_button.clicked.connect(lambda: self._transfer_active(True))
        self.delete_button.clicked.connect(lambda: self.delete_selected(self._active_side()))
        self.preview_button.clicked.connect(lambda: self.preview_selected(self._active_side()))
        self.rename_button.clicked.connect(self._prompt_rename)
        self.entries_ready.connect(self.set_entries)
        self.refresh_requested.connect(self.refresh_side)

    def set_entries(self, side: str, entries: Iterable[StorageEntry]) -> None:
        (self.local_model if side == "local" else self.plex_model).set_entries(entries)

    @Slot()
    def refresh(self) -> None:
        for side, storage in self.storages.items():
            self.refresh_side(side)

    def refresh_side(self, side: str) -> None:
        storage = self.storages.get(side)
        if storage is None:
            return
        task = Task(storage.list)
        task.signals.result.connect(lambda entries, side=side: self.entries_ready.emit(side, entries))
        self._start(task)

    def delete_selected(self, side: str) -> None:
        entry = self._selected(side)
        storage = self.storages.get(side)
        if entry is None or storage is None or not self.confirm(f"Delete {entry.relpath}?"):
            return
        task = Task(storage.delete, entry.relpath)
        task.signals.result.connect(lambda _result, side=side: self.refresh_requested.emit(side))
        self._start(task)

    def copy_selected(self, source_side: str, target_side: str, *, move: bool = False) -> None:
        entry = self._selected(source_side)
        source = self.storages.get(source_side)
        target = self.storages.get(target_side)
        if entry is None or source is None or target is None:
            return
        if move and not self.confirm(f"Move {entry.relpath} to {target_side} storage?"):
            return

        def operation():
            materialized = source.materialize_preview(entry.relpath)
            result = target.copy_in(materialized, entry.relpath, overwrite=False)
            if move:
                source.delete(entry.relpath)
            return result

        task = Task(operation)
        task.signals.result.connect(
            lambda _result, source_side=source_side, target_side=target_side, move=move:
            self._request_transfer_refresh(source_side, target_side, move)
        )
        self._start(task)

    def preview_selected(self, side: str) -> None:
        entry = self._selected(side)
        storage = self.storages.get(side)
        if entry is None or storage is None:
            return
        task = Task(storage.materialize_preview, entry.relpath)
        task.signals.result.connect(self.preview_ready.emit)
        self._start(task)

    def rename_selected(self, side: str, filename: str) -> None:
        entry = self._selected(side)
        storage = self.storages.get(side)
        clean_name = PurePosixPath(filename).name
        if entry is None or storage is None or not clean_name or clean_name != filename:
            return
        task = Task(storage.rename, entry.relpath, clean_name, overwrite=False)
        task.signals.result.connect(lambda _result, side=side: self.refresh_requested.emit(side))
        self._start(task)

    @Slot()
    def _prompt_rename(self) -> None:
        side = self._active_side()
        entry = self._selected(side)
        if entry is None:
            return
        filename, accepted = QInputDialog.getText(self, "Rename media", "New filename", text=entry.relpath.name)
        if accepted:
            self.rename_selected(side, filename)

    def _active_side(self) -> str:
        if self.local_view.hasFocus():
            return "local"
        if self.plex_view.hasFocus():
            return "plex"
        local_selected = bool(self.local_view.selectionModel().selectedRows())
        plex_selected = bool(self.plex_view.selectionModel().selectedRows())
        if plex_selected and not local_selected:
            return "plex"
        return "local"

    def _transfer_active(self, move: bool) -> None:
        source = self._active_side()
        target = "local" if source == "plex" else "plex"
        self.copy_selected(source, target, move=move)

    def _selected(self, side: str) -> StorageEntry | None:
        view = self.local_view if side == "local" else self.plex_view
        model = self.local_model if side == "local" else self.plex_model
        rows = view.selectionModel().selectedRows()
        return model.entry_at(rows[0]) if rows else None

    def _request_transfer_refresh(self, source_side: str, target_side: str, move: bool) -> None:
        self.refresh_requested.emit(target_side)
        if move:
            self.refresh_requested.emit(source_side)

    def set_submissions_enabled(self, enabled: bool) -> None:
        for button in (self.refresh_button, self.copy_button, self.move_button, self.preview_button,
                       self.rename_button, self.delete_button):
            button.setEnabled(enabled)

    def _start(self, task: Task) -> None:
        self._tasks.add(task)
        task.signals.error.connect(self._show_task_error)
        task.signals.finished.connect(lambda task=task: self._tasks.discard(task))
        try:
            self.thread_pool.start(task)
        except Exception as exc:
            task.cancel()
            self._tasks.discard(task)
            self.error_label.setText(f"Background task could not start: {exc}")

    @Slot(object)
    def _show_task_error(self, error) -> None:
        exception = error[1]
        if isinstance(exception, FileExistsError):
            self.error_label.setText(f"Target already exists: {exception}")
        else:
            self.error_label.setText(str(exception))

    def invalidate(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
