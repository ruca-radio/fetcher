from __future__ import annotations

from collections.abc import Iterable

from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QThreadPool, Qt, Signal, Slot, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableView, QVBoxLayout, QWidget

from fetcher_core.jobs import JobSnapshot

from .workers import Task


class DownloadJobsModel(QAbstractTableModel):
    HEADERS = ("Title", "Source", "Status", "Progress", "Speed", "ETA", "Target")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._snapshots: list[JobSnapshot] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._snapshots)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        job = self._snapshots[index.row()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return job.message if index.column() == 5 else job.title
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        return (
            job.title, job.site, job.status, f"{job.progress:.1f}%", job.speed,
            job.message, str(job.filepath) if job.filepath else "",
        )[index.column()]

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return super().headerData(section, orientation, role)

    def set_snapshots(self, snapshots: Iterable[JobSnapshot]) -> None:
        self.beginResetModel()
        self._snapshots = list(snapshots)
        self.endResetModel()

    def snapshot_at(self, row: int) -> JobSnapshot:
        return self._snapshots[row]

    def update_snapshot(self, snapshot: JobSnapshot) -> None:
        for row, current in enumerate(self._snapshots):
            if current.id == snapshot.id:
                self._snapshots[row] = snapshot
                self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))
                return
        row = len(self._snapshots)
        self.beginInsertRows(QModelIndex(), row, row)
        self._snapshots.append(snapshot)
        self.endInsertRows()

    def clear_completed(self) -> None:
        self.set_snapshots(job for job in self._snapshots if job.status not in {"done", "failed", "cancelled", "interrupted"})


class DownloadsPage(QWidget):
    snapshot_received = Signal(object)
    operation_succeeded = Signal(str, object)
    operation_failed = Signal(str, object)

    def __init__(self, manager=None, parent=None, *, reveal=None, task_pool=None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.reveal = reveal or (lambda path: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))
        self.task_pool = task_pool or QThreadPool(self)
        self._tasks: set[Task] = set()
        self.model = DownloadJobsModel(self)
        self.view = QTableView(self)
        self.view.setModel(self.model)
        self.view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.view.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.view.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.stop_button = QPushButton("Stop", self)
        self.retry_button = QPushButton("Retry", self)
        self.reveal_button = QPushButton("Reveal", self)
        self.clear_button = QPushButton("Clear completed", self)
        self.status_label = QLabel("", self)
        self.details_label = QLabel("", self)
        self.details_label.setWordWrap(True)
        self.details_label.setTextFormat(Qt.TextFormat.PlainText)
        self.details_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._latest_revisions: dict[str, int] = {}
        self._cleared_revisions: dict[str, int] = {}
        controls = QHBoxLayout()
        for button in (self.stop_button, self.retry_button, self.reveal_button, self.clear_button):
            controls.addWidget(button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.view, 1)
        layout.addLayout(controls)
        layout.addWidget(self.status_label)
        layout.addWidget(self.details_label)
        self.view.selectionModel().currentRowChanged.connect(self._show_selected_job)
        self.snapshot_received.connect(self._accept_snapshot)
        self.operation_succeeded.connect(self._operation_succeeded)
        self.operation_failed.connect(self._operation_failed)
        self.stop_button.clicked.connect(lambda: self.stop_row(self._current_row()))
        self.retry_button.clicked.connect(lambda: self.retry_row(self._current_row()))
        self.reveal_button.clicked.connect(lambda: self.reveal_row(self._current_row()))
        self.clear_button.clicked.connect(self.clear_completed)
        self._unsubscribe = None
        if manager is not None:
            self.set_snapshots(manager.snapshots())
            self._unsubscribe = manager.subscribe(self.snapshot_received.emit)

    def set_snapshots(self, snapshots) -> None:
        authoritative = list(snapshots)
        for snapshot in authoritative:
            self._latest_revisions[snapshot.id] = max(
                snapshot.revision,
                self._latest_revisions.get(snapshot.id, 0),
            )
        self.model.set_snapshots(authoritative)

    def stop_row(self, row: int) -> None:
        if self.manager is not None and row >= 0:
            job_id = self.model.snapshot_at(row).id
            self._submit_operation("Stop", self.manager.stop, job_id)

    def retry_row(self, row: int) -> None:
        if self.manager is not None and row >= 0:
            job_id = self.model.snapshot_at(row).id
            self._submit_operation("Retry", self.manager.retry, job_id)

    def reveal_row(self, row: int) -> None:
        if row < 0:
            return
        target = self.model.snapshot_at(row).filepath
        if target:
            self.reveal(Path(target))

    def clear_completed(self) -> None:
        clear = getattr(self.manager, "clear_completed", None) if self.manager is not None else None
        if clear is None:
            self.status_label.setText("Download history does not support clearing completed jobs.")
            return
        terminal = {"done", "failed", "cancelled", "interrupted"}
        for snapshot in self.model._snapshots:
            if snapshot.status in terminal:
                self._cleared_revisions[snapshot.id] = max(
                    snapshot.revision,
                    self._cleared_revisions.get(snapshot.id, 0),
                )
        self.model.clear_completed()
        self._submit_operation("Clear completed", clear)

    def _current_row(self) -> int:
        return self.view.currentIndex().row()

    def _accept_snapshot(self, snapshot: JobSnapshot) -> None:
        cleared_revision = self._cleared_revisions.get(snapshot.id)
        if cleared_revision is not None:
            if snapshot.revision <= cleared_revision:
                return
            self._cleared_revisions.pop(snapshot.id, None)
        if snapshot.revision < self._latest_revisions.get(snapshot.id, 0):
            return
        self._latest_revisions[snapshot.id] = snapshot.revision
        self.model.update_snapshot(snapshot)
        row = self._current_row()
        if row >= 0:
            self._show_selected_job()
        elif snapshot.status == "failed":
            self.details_label.setText(f"{snapshot.site}: {snapshot.message}")

    def _show_selected_job(self, *_args) -> None:
        row = self._current_row()
        if row < 0:
            self.details_label.clear()
            return
        job = self.model.snapshot_at(row)
        self.details_label.setText(f"{job.site}: {job.message}")

    def closeEvent(self, event) -> None:
        self.shutdown()
        event.accept()

    def shutdown(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        for task in tuple(self._tasks):
            task.cancel()

    def set_submissions_enabled(self, enabled: bool) -> None:
        for button in (self.stop_button, self.retry_button, self.reveal_button, self.clear_button):
            button.setEnabled(enabled)

    def _submit_operation(self, name: str, function, *args) -> None:
        task = Task(function, *args)
        self._tasks.add(task)
        task.signals.result.connect(lambda result, name=name: self.operation_succeeded.emit(name, result))
        task.signals.error.connect(lambda error, name=name: self.operation_failed.emit(name, error))
        task.signals.finished.connect(lambda task=task: self._tasks.discard(task))
        self.status_label.setText(f"{name} in progress…")
        try:
            self.task_pool.start(task)
        except Exception as exc:
            self._tasks.discard(task)
            self.status_label.setText(f"{name} failed: {exc}")

    @Slot(str, object)
    def _operation_succeeded(self, name: str, _result) -> None:
        if self.manager is not None:
            self.set_snapshots(self.manager.snapshots())
        self.status_label.setText(f"{name} complete.")

    @Slot(str, object)
    def _operation_failed(self, name: str, error) -> None:
        if name == "Clear completed" and self.manager is not None:
            self._cleared_revisions.clear()
            self.set_snapshots(self.manager.snapshots())
        self.status_label.setText(f"{name} failed: {error[1]}")
