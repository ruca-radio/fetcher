from __future__ import annotations

from pathlib import Path, PurePosixPath

from PySide6.QtCore import QModelIndex, QSize, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication

from fetcher_core.jobs import JobSnapshot
from fetcher_core.models import Video
from fetcher_core.storage import StorageEntry
from fetcher_gui.discover import SearchResultsModel
from fetcher_gui.downloads import DownloadJobsModel, DownloadsPage
from fetcher_gui.library import StorageTableModel


def sample_video(video_id: str = "one") -> Video:
    return Video(video_id, f"Title {video_id}", 125, 12_345, f"https://example/{video_id}", "Eporner")


def snapshot(status: str = "downloading") -> JobSnapshot:
    return JobSnapshot(
        id="job-1", title="Example", site="Eporner", url="https://example/video",
        thumbnail_url=None, status=status, progress=37.5, speed="2 MiB/s",
        message="00:10", filepath=Path("/tmp/example.mp4") if status == "done" else None,
        file_size=0, downloaded_bytes=10, worker_started=status == "downloading",
    )


def test_search_results_model_supports_selection_and_display() -> None:
    model = SearchResultsModel()
    model.set_results([sample_video()])

    assert model.rowCount() == 1
    assert model.data(model.index(0, 0), Qt.ItemDataRole.DisplayRole) == "Title one"
    assert model.setData(model.index(0, 0), Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
    assert model.selected_videos() == (sample_video(),)


def test_search_results_model_accepts_raw_qt_check_state_integer() -> None:
    model = SearchResultsModel()
    video = sample_video()
    model.set_results([video])

    assert model.setData(model.index(0, 0), Qt.CheckState.Checked.value, Qt.ItemDataRole.CheckStateRole)
    assert model.selected_videos() == (video,)


def test_search_results_model_keys_selection_and_thumbnails_by_site_and_id() -> None:
    app = QApplication.instance() or QApplication([])
    model = SearchResultsModel()
    eporner = sample_video("shared")
    redtube = Video("shared", "RedTube", 1, 1, "https://redtube/shared", "RedTube")
    model.set_results([eporner, redtube])
    model.setData(model.index(0, 0), Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
    eporner_thumbnail = QPixmap(160, 90)
    eporner_thumbnail.fill(Qt.GlobalColor.red)
    redtube_thumbnail = QPixmap(160, 90)
    redtube_thumbnail.fill(Qt.GlobalColor.blue)

    model.set_thumbnail("Eporner", "shared", eporner_thumbnail)
    model.set_thumbnail("RedTube", "shared", redtube_thumbnail)

    assert model.selected_videos() == (eporner,)
    assert model.data(model.index(0, 0), Qt.ItemDataRole.DecorationRole).toImage().pixelColor(0, 0) == QColor(Qt.GlobalColor.red)
    assert model.data(model.index(1, 0), Qt.ItemDataRole.DecorationRole).toImage().pixelColor(0, 0) == QColor(Qt.GlobalColor.blue)


def test_search_results_model_scales_thumbnails_to_display_size() -> None:
    app = QApplication.instance() or QApplication([])
    model = SearchResultsModel()
    video = sample_video()
    model.set_results([video])
    source = QPixmap(320, 180)
    source.fill(Qt.GlobalColor.red)

    model.set_thumbnail(video.site, video.id, source)

    displayed = model.data(model.index(0, 0), Qt.ItemDataRole.DecorationRole)
    assert displayed.size() == QSize(160, 90)
    assert model.thumbnail_at(video.site, video.id).size() == QSize(320, 180)


def test_search_results_model_prunes_thumbnails_removed_from_results() -> None:
    app = QApplication.instance() or QApplication([])
    model = SearchResultsModel()
    video = sample_video()
    model.set_results([video])
    model.set_thumbnail(video.site, video.id, QPixmap(2, 2))

    model.set_results([])

    assert model.thumbnail_at(video.site, video.id) is None


def test_download_model_replaces_immutable_snapshots() -> None:
    model = DownloadJobsModel()
    model.set_snapshots([snapshot()])

    assert model.rowCount() == 1
    assert model.snapshot_at(0).status == "downloading"
    assert model.data(model.index(0, 3)) == "37.5%"
    model.set_snapshots([snapshot("done")])
    assert model.data(model.index(0, 2)) == "done"


def test_download_failure_exposes_complete_error_in_tooltip():
    from dataclasses import replace
    model = DownloadJobsModel()
    failure = replace(snapshot("failed"), message="Provider blocked extraction. " * 20)
    model.set_snapshots([failure])
    assert model.data(model.index(0, 5), Qt.ItemDataRole.ToolTipRole) == failure.message


def test_progress_keeps_the_selected_download_row(qtbot):
    from dataclasses import replace
    page = DownloadsPage()
    qtbot.addWidget(page)
    page._accept_snapshot(snapshot())
    page.view.selectRow(0)
    page._accept_snapshot(replace(snapshot(), progress=55, revision=2))
    assert page.view.currentIndex().row() == 0
    assert page.view.selectionModel().selectedRows()[0].row() == 0


def test_failed_download_shows_full_message_below_table(qtbot):
    from dataclasses import replace
    page = DownloadsPage()
    qtbot.addWidget(page)
    message = "Provider failed: browser stream is unavailable. " * 5
    page._accept_snapshot(replace(snapshot("failed"), message=message))
    assert message in page.details_label.text()


def test_download_page_rejects_stale_revision() -> None:
    app = QApplication.instance() or QApplication([])
    class Jobs:
        def snapshots(self):
            return ()

        def subscribe(self, callback):
            return lambda: None

    page = DownloadsPage(Jobs())
    current = snapshot()
    current = JobSnapshot(**{**current.__dict__, "revision": 5})
    stale = JobSnapshot(**{**current.__dict__, "title": "stale", "revision": 4})
    page.set_snapshots([current])

    page.snapshot_received.emit(stale)

    assert page.model.snapshot_at(0).title == "Example"
    page.close()


def test_download_page_clear_tombstones_delayed_terminal_snapshot() -> None:
    app = QApplication.instance() or QApplication([])
    done = snapshot("done")
    done = JobSnapshot(**{**done.__dict__, "revision": 7})

    class Jobs:
        current = (done,)

        def snapshots(self):
            return self.current

        def subscribe(self, callback):
            return lambda: None

        def clear_completed(self):
            self.current = ()

    page = DownloadsPage(Jobs())
    page.clear_completed()

    page.snapshot_received.emit(done)

    assert page.model.rowCount() == 0
    page.close()


def test_storage_model_exposes_relative_paths_without_mutating_entries() -> None:
    entry = StorageEntry(PurePosixPath("folder/video.mp4"), 1_048_576, "today")
    model = StorageTableModel("Local staging")
    model.set_entries([entry])

    assert model.headerData(0, Qt.Orientation.Horizontal) == "Name"
    assert model.data(model.index(0, 0), Qt.ItemDataRole.DisplayRole) == "folder/video.mp4"
    assert model.entry_at(0) is entry
    assert model.entry_at(QModelIndex()) is None
