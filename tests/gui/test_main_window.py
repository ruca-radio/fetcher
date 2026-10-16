from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath
import threading
import time
from types import SimpleNamespace

from PySide6.QtCore import QEvent, QSize, QThreadPool, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication, QHeaderView, QWidget

from fetcher_core.jobs import JobSnapshot
from fetcher_core.models import AppSettings, SearchFilters, Video
from fetcher_core.storage import StorageEntry, TargetExistsError
from fetcher_gui.discover import DiscoverPage
from fetcher_gui.downloads import DownloadsPage
from fetcher_gui.library import LibraryPage
from fetcher_gui.main_window import MainWindow
from fetcher_gui.main_window import DaemonTaskPool
from fetcher_gui.app import compose_services
from fetcher_gui import app as gui_app
from fetcher_gui.settings import SettingsPage
from fetcher_gui.settings import apply_theme


class StubSearch:
    def search(self, query: str, provider: str, filters: dict[str, object]):
        return [Video(query, query, 60, 1, f"https://example/{query}", provider)]


class StubPreview:
    def resolve(self, video: Video):
        return f"https://media.example/{video.id}.m3u8"


class StubJobs:
    def __init__(self) -> None:
        self.stopped: list[str] = []
        self.retried: list[str] = []
        self.submitted: list[Video] = []
        self.closed: list[tuple[bool, bool]] = []
        self.interrupted = False
        self.cleared = False
        self.unsubscribed = False

    def snapshots(self):
        return ()

    def subscribe(self, _callback):
        return lambda: setattr(self, "unsubscribed", True)

    def submit(self, video):
        self.submitted.append(video)

    def stop(self, job_id):
        self.stopped.append(job_id)

    def retry(self, job_id):
        self.retried.append(job_id)

    def persist_interrupted(self):
        self.interrupted = True

    def close(self, *, wait, cancel_futures):
        self.closed.append((wait, cancel_futures))

    def clear_completed(self):
        self.cleared = True


class StubStorage:
    def __init__(self, name: str, entries=()) -> None:
        self.name = name
        self.entries = list(entries)
        self.deleted: list[PurePosixPath] = []
        self.copied: list[tuple[Path, PurePosixPath, bool]] = []
        self.renamed: list[tuple[PurePosixPath, str, bool]] = []
        self.list_calls = 0

    def list(self):
        self.list_calls += 1
        return list(self.entries)

    def materialize_preview(self, relpath):
        return Path("/tmp") / relpath.name

    def copy_in(self, source, relpath, *, overwrite=False):
        if any(entry.relpath == relpath for entry in self.entries):
            raise TargetExistsError(str(relpath))
        self.copied.append((source, relpath, overwrite))
        return StorageEntry(relpath, 1)

    def delete(self, relpath):
        self.deleted.append(relpath)

    def rename(self, relpath, filename, *, overwrite=False):
        self.renamed.append((relpath, filename, overwrite))
        return StorageEntry(PurePosixPath(filename), 1)


class StubSettings:
    def __init__(self) -> None:
        self.settings = AppSettings()
        self.saved: list[AppSettings] = []
        self.gui_preferences = {
            "staging_dir": "/old/staging", "cache_size_mib": 256,
            "cache_age_days": 7, "theme": "System",
        }
        self.saved_gui_preferences: list[dict[str, object]] = []

    def save(self, settings):
        self.saved.append(settings)

    def load_gui_preferences(self):
        return dict(self.gui_preferences)

    def save_gui_preferences(self, preferences):
        self.saved_gui_preferences.append(dict(preferences))

    def validate(self, settings, preferences):
        return settings, dict(preferences)


class StubPlayer(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[Path | str] = []
        self.thumbnails: list[tuple[object, object]] = []

    def set_source(self, source):
        self.sources.append(source)

    def show_thumbnail(self, video, pixmap=None):
        self.thumbnails.append((video, pixmap))


class SaturatedPool:
    def start(self, _task):
        raise RuntimeError("task pool queue is full")


def test_main_window_has_four_workspaces_and_retains_player(qtbot) -> None:
    player = StubPlayer()
    window = MainWindow(search_service=StubSearch(), preview_service=StubPreview(),
                        download_manager=StubJobs(), local_storage=StubStorage("local"),
                        plex_storage=StubStorage("plex"), settings_service=StubSettings(), player=player)
    qtbot.addWidget(window)

    assert [button.text() for button in window.navigation_buttons] == [
        "Discover", "Downloads", "Library", "Settings"
    ]
    assert window.workspace_stack.count() == 4
    window.navigation_buttons[2].click()
    assert window.player is player
    assert window.library_page.confirm is not None
    window.library_page.preview_ready.emit(Path("/tmp/library.mp4"))
    assert player.sources == [Path("/tmp/library.mp4")]


def test_main_window_initializes_discover_from_persisted_search_filters(qtbot) -> None:
    settings = StubSettings()
    settings.settings = replace(
        settings.settings,
        search_filters=replace(
            settings.settings.search_filters,
            min_duration_min=5,
            max_duration_min=30,
            min_rating=80,
            min_quality="1080",
            hd_only=True,
            pages=3,
            exclude_phrases="oral, anal",
        ),
    )

    window = MainWindow(download_manager=StubJobs(), local_storage=StubStorage("local"),
                        plex_storage=StubStorage("plex"), settings_service=settings)
    qtbot.addWidget(window)

    assert window.discover_page.search_filters() == {
        "min_duration": 5, "max_duration": 30, "min_rating": 80, "quality": "1080",
        "hd_only": True, "exclude": "oral, anal", "pages": 3,
    }


def test_library_refreshes_once_when_first_navigated_to(qtbot) -> None:
    local, plex = StubStorage("local"), StubStorage("plex")
    window = MainWindow(download_manager=StubJobs(), local_storage=local, plex_storage=plex,
                        settings_service=StubSettings(), thread_pool=QThreadPool())
    qtbot.addWidget(window)

    assert local.list_calls == plex.list_calls == 0
    window.navigation_buttons[2].click()
    qtbot.waitUntil(lambda: local.list_calls == plex.list_calls == 1, timeout=2_000)
    window.navigation_buttons[0].click()
    window.navigation_buttons[2].click()

    assert local.list_calls == plex.list_calls == 1


def test_discover_ignores_stale_search_and_preview_results(qtbot) -> None:
    page = DiscoverPage(StubSearch(), StubPreview(), StubJobs(), thread_pool=QThreadPool())
    qtbot.addWidget(page)
    old = page.begin_search_generation()
    current = page.begin_search_generation()
    stale_video = Video("old", "Old", 1, 1, "https://old", "Eporner")
    new_video = Video("new", "New", 1, 1, "https://new", "Eporner")

    page.accept_search_result(old, [stale_video])
    page.accept_search_result(current, [new_video])
    old_preview = page.begin_preview_generation()
    new_preview = page.begin_preview_generation()
    page.accept_preview_result(old_preview, "https://old-stream")
    page.accept_preview_result(new_preview, "https://new-stream")

    assert page.results_model.video_at(0).id == "new"
    assert page.preview_source == "https://new-stream"


def test_discover_exposes_quality_duration_and_rating_filters(qtbot) -> None:
    page = DiscoverPage(StubSearch(), StubPreview(), StubJobs(), thread_pool=QThreadPool())
    qtbot.addWidget(page)
    page.min_duration_spin.setValue(5)
    page.max_duration_spin.setValue(30)
    page.min_rating_spin.setValue(80)
    page.quality_combo.setCurrentText("1080")
    page.hd_only_checkbox.setChecked(True)
    page.exclude_edit.setText("oral, anal")
    page.pages_spin.setValue(3)

    assert page.search_filters() == {
        "min_duration": 5, "max_duration": 30, "min_rating": 80, "quality": "1080",
        "hd_only": True, "exclude": "oral, anal", "pages": 3,
    }


def test_discover_table_fits_160x90_thumbnails(qtbot) -> None:
    page = DiscoverPage(StubSearch(), StubPreview(), StubJobs(), thread_pool=QThreadPool())
    qtbot.addWidget(page)
    page.show()
    page.results_model.set_results([Video("one", "One", 120, 1000, "https://one", "Eporner")])

    assert page.results_view.iconSize() == QSize(160, 90)
    assert page.results_view.verticalHeader().defaultSectionSize() >= 90
    assert page.results_view.rowHeight(0) >= 90
    assert page.results_view.horizontalHeader().sectionResizeMode(0) == QHeaderView.ResizeMode.Stretch


def test_discover_clicking_a_row_previews_thumbnail_without_resolving_media(qtbot) -> None:
    preview = StubPreview()
    resolved: list[Video] = []
    original = preview.resolve
    preview.resolve = lambda video: resolved.append(video) or original(video)
    page = DiscoverPage(StubSearch(), preview, StubJobs(), thread_pool=QThreadPool())
    qtbot.addWidget(page)
    video = Video("one", "One", 1, 1, "https://one", "Eporner", thumbnail_url="https://thumb")
    pixmap = QPixmap(160, 90)
    pixmap.fill(Qt.GlobalColor.red)
    focused: list[tuple[object, object]] = []
    page.focused_result.connect(lambda item, image: focused.append((item, image)))
    page.results_model.set_results([video])
    page.results_model.set_thumbnail(video.site, video.id, pixmap)

    page.results_view.selectRow(0)

    assert resolved == []
    assert focused
    shown_video, shown_pixmap = focused[-1]
    assert shown_video.id == "one"
    assert shown_pixmap is not None
    assert not shown_pixmap.isNull()


def test_discover_hover_previews_another_result_and_leave_restores_pinned(qtbot) -> None:
    page = DiscoverPage(StubSearch(), StubPreview(), StubJobs(), thread_pool=QThreadPool())
    qtbot.addWidget(page)
    pinned = Video("pinned", "Pinned", 1, 1, "https://pinned", "Eporner", thumbnail_url="https://p")
    hovered = Video("hovered", "Hovered", 1, 1, "https://hovered", "Eporner", thumbnail_url="https://h")
    pinned_thumb, hovered_thumb = QPixmap(160, 90), QPixmap(160, 90)
    pinned_thumb.fill(Qt.GlobalColor.red)
    hovered_thumb.fill(Qt.GlobalColor.blue)
    page.results_model.set_results([pinned, hovered])
    page.results_model.set_thumbnail(pinned.site, pinned.id, pinned_thumb)
    page.results_model.set_thumbnail(hovered.site, hovered.id, hovered_thumb)
    focused: list[str] = []
    page.focused_result.connect(lambda video, _pixmap: focused.append(video.id if video else ""))
    page.results_view.selectRow(0)

    page.results_view.entered.emit(page.results_model.index(1, 0))
    QApplication.sendEvent(page.results_view.viewport(), QEvent(QEvent.Type.Leave))

    assert focused[-3:] == ["pinned", "hovered", "pinned"]
    assert page.results_view.selectionModel().selectedRows()[0].row() == 0


def test_discover_late_thumbnail_updates_active_preview(qtbot) -> None:
    page = DiscoverPage(StubSearch(), StubPreview(), StubJobs(), thread_pool=QThreadPool())
    qtbot.addWidget(page)
    video = Video("late", "Late", 1, 1, "https://late", "Eporner", thumbnail_url="https://image")
    focused: list[tuple[object, object]] = []
    page.focused_result.connect(lambda item, image: focused.append((item, image)))
    generation = page.begin_search_generation()
    page.results_model.set_results([video])
    page.results_view.selectRow(0)
    assert focused[-1][1] is None

    page._accept_thumbnail(generation, video.site, video.id, QPixmap(160, 90))

    assert focused[-1][0].id == "late"
    assert focused[-1][1] is not None
    assert not focused[-1][1].isNull()


def test_hover_and_late_thumbnails_do_not_interrupt_preview_playback(qtbot):
    page = DiscoverPage(StubSearch(), StubPreview(), StubJobs())
    qtbot.addWidget(page)
    item = Video("one", "One", 1, 1, "https://one", "Test")
    page.results_model.set_results([item])
    page.results_view.selectRow(0)
    page.accept_preview_result(page.preview_generation, "https://media.test/full.mp4")
    focused = []
    page.focused_result.connect(lambda *args: focused.append(args))
    page._result_entered(page.results_model.index(0, 0))
    page._accept_thumbnail(page.search_generation, item.site, item.id, QPixmap(160, 90))
    page._leave_results()
    assert focused == []


def test_search_adapter_preserves_disabled_censored_filter():
    seen = []
    module = SimpleNamespace(search_videos=lambda query, provider, filters, **kwargs: seen.append(filters) or [])
    settings = AppSettings(search_filters=SearchFilters(exclude_censored=False))
    gui_app.SearchAdapter(module, settings).search("query", "Eporner", {})
    assert seen[0].exclude_censored is False


def test_discover_search_pool_can_shutdown_without_waiting_for_network(qtbot):
    started = threading.Event()
    release = threading.Event()
    class Search:
        def search(self, *args):
            started.set()
            release.wait(2)
            return []
    page = DiscoverPage(Search(), StubPreview(), StubJobs())
    qtbot.addWidget(page)
    page.query_edit.setText("query")
    page.search()
    qtbot.waitUntil(started.is_set)
    try:
        assert isinstance(page._search_pool, DaemonTaskPool)
        start = time.monotonic()
        page.invalidate()
        assert time.monotonic() - start < 0.2
    finally:
        release.set()


def test_discover_equal_ids_from_different_providers_preview_correct_thumbnail(qtbot) -> None:
    page = DiscoverPage(StubSearch(), StubPreview(), StubJobs(), thread_pool=QThreadPool())
    qtbot.addWidget(page)
    eporner = Video("shared", "Eporner", 1, 1, "https://eporner/shared", "Eporner")
    redtube = Video("shared", "RedTube", 1, 1, "https://redtube/shared", "RedTube")
    eporner_thumb, redtube_thumb = QPixmap(160, 90), QPixmap(160, 90)
    eporner_thumb.fill(Qt.GlobalColor.red)
    redtube_thumb.fill(Qt.GlobalColor.blue)
    page.results_model.set_results([eporner, redtube])
    page.results_model.set_thumbnail("Eporner", "shared", eporner_thumb)
    page.results_model.set_thumbnail("RedTube", "shared", redtube_thumb)
    focused: list[tuple[str, QColor]] = []
    page.focused_result.connect(
        lambda video, pixmap: focused.append((video.site, pixmap.toImage().pixelColor(0, 0)))
    )

    page.results_view.selectRow(0)
    page.results_view.selectRow(1)

    assert focused[-2] == ("Eporner", QColor(Qt.GlobalColor.red))
    assert focused[-1] == ("RedTube", QColor(Qt.GlobalColor.blue))


def test_discover_thumbnail_failure_uses_placeholder_without_search_error(qtbot) -> None:
    def fail(_url: str):
        raise ValueError("Unable to decode thumbnail")

    page = DiscoverPage(StubSearch(), StubPreview(), StubJobs(), thread_pool=QThreadPool(),
                        thumbnail_loader=fail)
    qtbot.addWidget(page)
    video = Video("bad", "Bad", 1, 1, "https://bad", "Eporner", thumbnail_url="https://image")
    focused: list[tuple[object, object]] = []
    page.focused_result.connect(lambda item, image: focused.append((item, image)))
    page.accept_search_result(page.begin_search_generation(), [video])
    page.results_view.selectRow(0)
    qtbot.waitUntil(
        lambda: any(item[1] is not None and item[1].isNull() for item in focused),
        timeout=2_000,
    )

    assert page.error_label.text() == ""


def test_main_window_routes_focused_result_to_player_thumbnail(qtbot) -> None:
    player = StubPlayer()
    window = MainWindow(search_service=StubSearch(), preview_service=StubPreview(),
                        download_manager=StubJobs(), local_storage=StubStorage("local"),
                        plex_storage=StubStorage("plex"), settings_service=StubSettings(),
                        player=player, thread_pool=QThreadPool())
    qtbot.addWidget(window)
    video = Video("one", "One", 1, 1, "https://one", "Eporner")
    pixmap = QPixmap(160, 90)
    pixmap.fill(Qt.GlobalColor.red)
    window.discover_page.results_model.set_results([video])
    window.discover_page.results_model.set_thumbnail(video.site, video.id, pixmap)

    window.discover_page.results_view.selectRow(0)

    assert player.sources == []
    assert player.thumbnails
    assert player.thumbnails[-1][0].id == "one"
    assert player.thumbnails[-1][1] is not None


def test_discover_search_shows_searching_status_then_results(qtbot) -> None:
    started = threading.Event()
    release = threading.Event()

    class SlowSearch:
        def search(self, query: str, provider: str, filters: dict[str, object]):
            started.set()
            release.wait(timeout=2)
            return [Video(query, query, 60, 1, f"https://example/{query}", provider)]

    page = DiscoverPage(SlowSearch(), StubPreview(), StubJobs(), thread_pool=QThreadPool())
    qtbot.addWidget(page)
    page.query_edit.setText("blonde")

    page.search()
    qtbot.waitUntil(started.is_set, timeout=2_000)
    assert "searching" in page.error_label.text().lower()
    release.set()
    qtbot.waitUntil(lambda: page.results_model.rowCount() == 1, timeout=2_000)
    assert page.results_model.video_at(0).id == "blonde"


def test_discover_empty_search_results_are_visible(qtbot) -> None:
    class EmptySearch:
        def search(self, query: str, provider: str, filters: dict[str, object]):
            return []

    page = DiscoverPage(EmptySearch(), StubPreview(), StubJobs(), thread_pool=QThreadPool())
    qtbot.addWidget(page)
    page.query_edit.setText("blonde")
    page.search()
    qtbot.waitUntil(lambda: "no video" in page.error_label.text().lower(), timeout=2_000)
    assert page.results_model.rowCount() == 0


def test_discover_search_is_not_blocked_by_busy_thumbnail_pool(qtbot) -> None:
    gate = threading.Event()

    class ReadySearch:
        def search(self, query: str, provider: str, filters: dict[str, object]):
            return [Video(query, query, 60, 1, f"https://example/{query}", provider)]

    pool = DaemonTaskPool(max_workers=1, max_pending=2)
    page = DiscoverPage(ReadySearch(), StubPreview(), StubJobs(), thread_pool=pool)
    qtbot.addWidget(page)
    pool.start(lambda: gate.wait())
    qtbot.waitUntil(lambda: pool.activeThreadCount() == 1, timeout=2_000)
    page.query_edit.setText("blonde")

    page.search()
    qtbot.waitUntil(lambda: page.results_model.rowCount() == 1, timeout=2_000)
    gate.set()


def test_search_adapter_returns_provider_errors_with_empty_results() -> None:
    class Module:
        LAST_PROVIDER_ERRORS: dict[str, str] = {}

        def search_videos(self, query, provider, filters, twitter_count=40):
            self.LAST_PROVIDER_ERRORS["PornHub"] = "regional access blocked"
            return []

    result = gui_app.SearchAdapter(Module(), AppSettings()).search(
        "blonde", "PornHub",
        {"min_duration": 0, "max_duration": 0, "min_rating": 0, "quality": "any"},
    )

    assert result["results"] == []
    assert "PornHub" in result["warning"]
    assert "blocked" in result["warning"]


def test_discover_loads_thumbnails_reports_errors_and_queues_off_thread(qtbot) -> None:
    jobs = StubJobs()
    worker_threads: list[int] = []
    page = DiscoverPage(StubSearch(), StubPreview(), jobs, thread_pool=QThreadPool(),
                        thumbnail_loader=lambda _url: QPixmap(2, 2).toImage())
    qtbot.addWidget(page)
    video = Video("thumb", "Thumb", 1, 1, "https://video", "Eporner", thumbnail_url="https://image")
    page.accept_search_result(page.begin_search_generation(), [video])
    qtbot.waitUntil(lambda: page.results_model.thumbnail_at(video.site, video.id) is not None, timeout=2_000)
    assert page.results_model.data(page.results_model.index(0, 0), Qt.ItemDataRole.DecorationRole) is not None

    page.results_model.setData(page.results_model.index(0, 0), Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
    original_submit = jobs.submit
    jobs.submit = lambda item: (worker_threads.append(threading.get_ident()), original_submit(item))[1]
    page.queue_selected()
    qtbot.waitUntil(lambda: bool(jobs.submitted), timeout=2_000)
    assert worker_threads[0] != threading.get_ident()

    page._show_task_error((RuntimeError, RuntimeError("provider unavailable"), "trace"))
    assert "provider unavailable" in page.error_label.text()


def test_discover_thumbnail_saturation_is_visible_and_does_not_escape(qtbot) -> None:
    page = DiscoverPage(StubSearch(), StubPreview(), StubJobs(), thread_pool=SaturatedPool())
    qtbot.addWidget(page)
    video = Video("thumb", "Thumb", 1, 1, "url", "Eporner", thumbnail_url="https://image")

    page.accept_search_result(page.begin_search_generation(), [video])

    assert page.results_model.rowCount() == 1
    assert "queue is full" in page.error_label.text().lower()
    assert page._tasks == set()


def test_discover_selection_change_invalidates_pending_preview(qtbot) -> None:
    page = DiscoverPage(StubSearch(), StubPreview(), StubJobs(), thread_pool=QThreadPool())
    qtbot.addWidget(page)
    page.results_model.set_results([
        Video("a", "A", 1, 1, "https://a", "Eporner"),
        Video("b", "B", 1, 1, "https://b", "Eporner"),
    ])
    generation = page.begin_preview_generation()
    page.results_view.selectRow(1)
    qtbot.waitUntil(lambda: page.preview_generation > generation)
    page.accept_preview_result(generation, "https://stale")
    assert page.preview_source is None


def test_discover_displays_provider_warning_from_search_result(qtbot) -> None:
    page = DiscoverPage(StubSearch(), StubPreview(), StubJobs(), thread_pool=QThreadPool())
    qtbot.addWidget(page)
    video = Video("one", "One", 1, 1, "https://one", "Eporner")
    generation = page.begin_search_generation()

    page.accept_search_result(generation, {"results": [video], "warning": "provider blocked"})

    assert page.results_model.video_at(0) is video
    assert "provider blocked" in page.error_label.text()


def test_discover_success_clears_stale_errors_and_warning_replaces_them(qtbot) -> None:
    page = DiscoverPage(StubSearch(), StubPreview(), StubJobs(), thread_pool=QThreadPool())
    qtbot.addWidget(page)
    page.error_label.setText("stale error")
    generation = page.begin_search_generation()

    page.accept_search_result(generation, [])
    assert "no video" in page.error_label.text().lower()

    page.error_label.setText("another stale error")
    page.accept_search_result(generation, {"results": [], "warning": "provider warning"})
    assert page.error_label.text() == "provider warning"


def test_download_actions_delegate_stop_and_retry(qtbot) -> None:
    jobs = StubJobs()
    page = DownloadsPage(jobs)
    qtbot.addWidget(page)
    downloading = JobSnapshot("a", "A", "site", "url", None, "downloading", 1, "", "", None, 0, 0, True)
    failed = replace(downloading, id="b", status="failed", worker_started=False)
    page.set_snapshots([downloading, failed])

    page.stop_row(0)
    page.retry_row(1)

    qtbot.waitUntil(lambda: jobs.stopped == ["a"] and jobs.retried == ["b"], timeout=2_000)


def test_downloads_discard_out_of_order_older_revision(qtbot) -> None:
    page = DownloadsPage(StubJobs())
    qtbot.addWidget(page)
    current = JobSnapshot("a", "new", "site", "url", None, "downloading", 50, "", "",
                          None, 0, 0, True, revision=5)
    stale = replace(current, title="stale", progress=10, revision=4)
    page.set_snapshots([current])

    page.snapshot_received.emit(stale)

    assert page.model.snapshot_at(0).title == "new"
    assert page.model.snapshot_at(0).revision == 5


def test_downloads_show_eta_target_reveal_clear_and_unsubscribe(qtbot, tmp_path) -> None:
    jobs = StubJobs()
    revealed: list[Path] = []
    page = DownloadsPage(jobs, reveal=lambda path: revealed.append(path) or True)
    qtbot.addWidget(page)
    target = tmp_path / "video.mp4"
    done = replace(JobSnapshot("a", "A", "site", "url", None, "done", 100, "", "12s",
                               target, 1, 1, False))
    page.set_snapshots([done])

    assert "ETA" in page.model.HEADERS
    assert "Target" in page.model.HEADERS
    page.reveal_row(0)
    page.clear_completed()

    assert revealed == [target]
    qtbot.waitUntil(lambda: jobs.cleared, timeout=2_000)
    assert jobs.cleared
    page.close()
    assert jobs.unsubscribed


def test_downloads_clear_rejects_delayed_terminal_snapshot(qtbot) -> None:
    jobs = StubJobs()
    page = DownloadsPage(jobs)
    qtbot.addWidget(page)
    done = JobSnapshot("a", "A", "site", "url", None, "done", 100, "", "", None, 0, 0, False, revision=7)
    page.set_snapshots([done])

    page.clear_completed()
    page.snapshot_received.emit(done)

    assert page.model.rowCount() == 0


def test_downloads_reports_when_history_clear_api_is_unavailable(qtbot) -> None:
    jobs = StubJobs()
    jobs.clear_completed = None
    page = DownloadsPage(jobs)
    qtbot.addWidget(page)

    page.clear_completed()

    assert "does not support" in page.status_label.text()


def test_download_mutations_are_nonblocking_and_run_off_gui_thread(qtbot) -> None:
    jobs = StubJobs()
    mutation_threads: list[int] = []
    main_thread = threading.get_ident()

    def slow_stop(job_id):
        mutation_threads.append(threading.get_ident())
        time.sleep(0.05)
        jobs.stopped.append(job_id)

    def slow_retry(job_id):
        mutation_threads.append(threading.get_ident())
        time.sleep(0.05)
        jobs.retried.append(job_id)

    def slow_clear():
        mutation_threads.append(threading.get_ident())
        time.sleep(0.05)
        jobs.cleared = True

    jobs.stop, jobs.retry, jobs.clear_completed = slow_stop, slow_retry, slow_clear
    pool = DaemonTaskPool(max_workers=2)
    page = DownloadsPage(jobs, task_pool=pool)
    qtbot.addWidget(page)
    downloading = JobSnapshot("a", "A", "site", "url", None, "downloading", 1, "", "", None, 0, 0, True)
    failed = replace(downloading, id="b", status="failed", worker_started=False)
    page.set_snapshots([downloading, failed])

    started = time.monotonic()
    page.stop_row(0)
    page.retry_row(1)
    page.clear_completed()
    assert time.monotonic() - started < 0.03
    qtbot.waitUntil(lambda: jobs.cleared and jobs.stopped == ["a"] and jobs.retried == ["b"], timeout=2_000)

    assert mutation_threads and all(thread_id != main_thread for thread_id in mutation_threads)
    assert "complete" in page.status_label.text().lower()


def test_library_confirms_delete_and_never_overwrites(qtbot) -> None:
    entry = StorageEntry(PurePosixPath("video.mp4"), 10)
    local = StubStorage("local", [entry])
    plex = StubStorage("plex", [entry])
    confirmations: list[str] = []
    page = LibraryPage(local, plex, confirm=lambda message: confirmations.append(message) or True,
                       thread_pool=QThreadPool())
    qtbot.addWidget(page)
    page.set_entries("local", [entry])
    page.local_view.selectRow(0)

    page.delete_selected("local")
    page.copy_selected("local", "plex")
    qtbot.waitUntil(lambda: bool(local.deleted), timeout=2_000)
    qtbot.waitUntil(lambda: bool(page.error_label.text()), timeout=2_000)

    assert confirmations and "video.mp4" in confirmations[0]
    assert local.deleted == [entry.relpath]
    assert plex.copied == []
    assert "exists" in page.error_label.text().lower()


def test_library_preview_rename_and_move_confirmation(qtbot) -> None:
    entry = StorageEntry(PurePosixPath("video.mp4"), 10)
    local = StubStorage("local", [entry])
    plex = StubStorage("plex")
    confirmations: list[str] = []
    page = LibraryPage(local, plex, confirm=lambda message: confirmations.append(message) or False,
                       thread_pool=QThreadPool())
    qtbot.addWidget(page)
    page.set_entries("local", [entry])
    page.local_view.selectRow(0)
    previews: list[Path] = []
    page.preview_ready.connect(previews.append)

    page.preview_selected("local")
    page.rename_selected("local", "renamed.mp4")
    page.copy_selected("local", "plex", move=True)
    qtbot.waitUntil(lambda: bool(previews), timeout=2_000)

    assert previews == [Path("/tmp/video.mp4")]
    assert local.renamed == [(entry.relpath, "renamed.mp4", False)]
    assert confirmations and "Move" in confirmations[-1]
    assert plex.copied == []
    assert local.deleted == []


def test_library_supports_plex_to_local_and_refreshes_only_affected_panes(qtbot) -> None:
    entry = StorageEntry(PurePosixPath("remote.mp4"), 10)
    local = StubStorage("local")
    plex = StubStorage("plex", [entry])
    page = LibraryPage(local, plex, confirm=lambda _message: True, thread_pool=QThreadPool())
    qtbot.addWidget(page)
    page.set_entries("plex", [entry])
    page.plex_view.selectRow(0)

    page.copy_selected("plex", "local")
    qtbot.waitUntil(lambda: bool(local.copied), timeout=2_000)
    qtbot.waitUntil(lambda: local.list_calls == 1, timeout=2_000)

    assert local.copied[0][1] == entry.relpath
    assert local.list_calls == 1
    assert plex.list_calls == 0


def test_library_transfer_labels_follow_selected_direction(qtbot) -> None:
    entry = StorageEntry(PurePosixPath("video.mp4"), 1)
    page = LibraryPage(StubStorage("local"), StubStorage("plex"), thread_pool=QThreadPool())
    qtbot.addWidget(page)
    page.set_entries("local", [entry])
    page.set_entries("plex", [entry])

    page.local_view.selectRow(0)
    assert page.copy_button.text() == "Copy to Plex"
    assert page.move_button.text() == "Move to Plex"

    page.local_view.clearSelection()
    page.plex_view.selectRow(0)
    assert page.copy_button.text() == "Copy to Local"
    assert page.move_button.text() == "Move to Local"


def test_library_active_side_follows_the_selected_pane(qtbot) -> None:
    entry = StorageEntry(PurePosixPath("same.mp4"), 1)
    page = LibraryPage(StubStorage("local"), StubStorage("plex"), thread_pool=QThreadPool())
    qtbot.addWidget(page)
    page.set_entries("local", [entry])
    page.set_entries("plex", [entry])

    page.local_view.selectRow(0)
    assert page._active_side() == "local"

    page.local_view.clearSelection()
    page.plex_view.selectRow(0)
    assert page._active_side() == "plex"


def test_library_plex_preview_rename_and_delete(qtbot) -> None:
    entry = StorageEntry(PurePosixPath("remote.mp4"), 10)
    plex = StubStorage("plex", [entry])
    page = LibraryPage(StubStorage("local"), plex, confirm=lambda _message: True,
                       thread_pool=QThreadPool())
    qtbot.addWidget(page)
    page.set_entries("plex", [entry])
    page.plex_view.selectRow(0)
    previews: list[Path] = []
    page.preview_ready.connect(previews.append)

    page.preview_button.click()
    page.rename_selected("plex", "renamed.mp4")
    qtbot.waitUntil(lambda: bool(previews) and bool(plex.renamed), timeout=2_000)
    page.set_entries("plex", [entry])
    page.plex_view.selectRow(0)
    page.delete_button.click()
    qtbot.waitUntil(lambda: bool(plex.deleted), timeout=2_000)

    assert previews == [Path("/tmp/remote.mp4")]
    assert plex.renamed == [(entry.relpath, "renamed.mp4", False)]
    assert plex.deleted == [entry.relpath]


def test_library_saturation_is_visible_and_cleans_task_tracking(qtbot) -> None:
    page = LibraryPage(StubStorage("local"), StubStorage("plex"),
                       thread_pool=SaturatedPool())
    qtbot.addWidget(page)

    page.refresh_side("local")

    assert "queue is full" in page.error_label.text().lower()
    assert page._tasks == set()


def test_settings_form_saves_paths_concurrency_cache_and_runs_diagnostics(qtbot) -> None:
    service = StubSettings()
    diagnostics_calls: list[bool] = []
    page = SettingsPage(service, diagnostics=lambda: diagnostics_calls.append(True) or {"yt-dlp": "ok"},
                        thread_pool=QThreadPool())
    qtbot.addWidget(page)
    page.storage_edit.setText("sftp:host=nas/media")
    page.temp_edit.setText("/tmp/fetcher")
    page.staging_edit.setText("/tmp/staging")
    page.parallel_spin.setValue(4)
    page.fragments_spin.setValue(8)
    page.cache_size_spin.setValue(512)
    page.cache_age_spin.setValue(14)
    page.theme_combo.setCurrentText("Dark")
    saved_signals: list[bool] = []
    restart_messages: list[str] = []
    page.settings_saved.connect(lambda: saved_signals.append(True))
    page.restart_required.connect(restart_messages.append)

    page.save_settings()
    page.run_diagnostics()
    qtbot.waitUntil(lambda: bool(diagnostics_calls), timeout=2_000)
    qtbot.waitUntil(lambda: "yt-dlp" in page.diagnostics_label.text(), timeout=2_000)

    assert service.saved[-1].plex_dir == "sftp:host=nas/media"
    assert service.saved[-1].tmp_dir == "/tmp/fetcher"
    assert page.staging_edit.text() == "/tmp/staging"
    assert service.saved[-1].parallel_downloads == 4
    assert service.saved[-1].concurrent_fragments == 8
    assert page.cache_settings() == (512, 14)
    assert service.saved_gui_preferences[-1] == {
        "staging_dir": "/tmp/staging", "cache_size_mib": 512,
        "cache_age_days": 14, "theme": "Dark",
    }
    assert saved_signals == [True]
    assert restart_messages and "restart" in restart_messages[0].lower()
    assert "saved" in page.status_label.text().lower()
    assert "restart required" in page.status_label.text().lower()


def test_settings_validation_failure_does_not_persist_and_is_visible(qtbot) -> None:
    service = StubSettings()
    service.validate = lambda _settings, _preferences: (_ for _ in ()).throw(ValueError("staging is not writable"))
    page = SettingsPage(service)
    qtbot.addWidget(page)
    page.staging_edit.setText("/forbidden")

    page.save_settings()

    assert service.saved == []
    assert service.saved_gui_preferences == []
    assert "staging is not writable" in page.status_label.text()
    assert "failed" in page.status_label.text().lower()


def test_settings_theme_applies_live_and_system_restores_application(qtbot) -> None:
    service = StubSettings()
    page = SettingsPage(service)
    qtbot.addWidget(page)

    page.theme_combo.setCurrentText("Dark")
    page.save_settings()
    from PySide6.QtWidgets import QApplication
    application = QApplication.instance()
    assert application.property("fetcher-theme") == "Dark"
    assert application.styleSheet()

    page.theme_combo.setCurrentText("System")
    page.save_settings()
    assert application.property("fetcher-theme") == "System"
    assert application.styleSheet() == ""


def test_settings_diagnostics_saturation_is_visible_and_cleans_task_tracking(qtbot) -> None:
    page = SettingsPage(StubSettings(), diagnostics=lambda: {}, thread_pool=SaturatedPool())
    qtbot.addWidget(page)

    page.run_diagnostics()

    assert "queue is full" in page.diagnostics_label.text().lower()
    assert page._tasks == set()


def test_close_is_bounded_and_invalidates_background_work(qtbot) -> None:
    jobs = StubJobs()
    pool = QThreadPool()
    window = MainWindow(search_service=StubSearch(), preview_service=StubPreview(),
                        download_manager=jobs, local_storage=StubStorage("local"),
                        plex_storage=StubStorage("plex"), settings_service=StubSettings(),
                        thread_pool=pool, shutdown_grace_ms=10)
    qtbot.addWidget(window)
    generation = window.discover_page.begin_search_generation()

    window.close()

    assert window.discover_page.search_generation > generation
    assert jobs.interrupted
    assert jobs.closed == [(False, True)]
    assert jobs.unsubscribed


def test_close_disables_submissions_and_does_not_block_on_running_pool(qtbot) -> None:
    jobs = StubJobs()
    pool = QThreadPool()
    pool.start(lambda: time.sleep(0.3))
    window = MainWindow(search_service=StubSearch(), preview_service=StubPreview(),
                        download_manager=jobs, local_storage=StubStorage("local"),
                        plex_storage=StubStorage("plex"), settings_service=StubSettings(),
                        thread_pool=pool, shutdown_grace_ms=1)
    qtbot.addWidget(window)

    started = time.monotonic()
    window.close()
    elapsed = time.monotonic() - started

    assert elapsed < 0.1
    assert not window.discover_page.search_button.isEnabled()
    assert not window.downloads_page.stop_button.isEnabled()
    assert pool.parent() is None


def test_default_daemon_pool_close_never_retains_or_waits_for_blocked_task(qtbot) -> None:
    blocker = threading.Event()
    window = MainWindow(search_service=StubSearch(), preview_service=StubPreview(),
                        download_manager=StubJobs(), local_storage=StubStorage("local"),
                        plex_storage=StubStorage("plex"), settings_service=StubSettings(),
                        shutdown_grace_ms=1)
    qtbot.addWidget(window)
    assert isinstance(window.thread_pool, DaemonTaskPool)
    window.thread_pool.start(lambda: blocker.wait())
    qtbot.waitUntil(lambda: window.thread_pool.activeThreadCount() == 1, timeout=2_000)

    started = time.monotonic()
    window.close()

    assert time.monotonic() - started < 0.1
    assert window.thread_pool.activeThreadCount() == 1


def test_daemon_task_pool_has_fixed_concurrency_and_bounded_queue() -> None:
    pool = DaemonTaskPool(max_workers=2, max_pending=4)
    gate = threading.Event()
    lock = threading.Lock()
    running = 0
    maximum = 0

    def blocked_task():
        nonlocal running, maximum
        with lock:
            running += 1
            maximum = max(maximum, running)
        gate.wait()
        with lock:
            running -= 1

    for _ in range(2):
        pool.start(blocked_task)
    deadline = time.monotonic() + 1
    while pool.activeThreadCount() < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    for _ in range(4):
        pool.start(blocked_task)

    assert pool.worker_count == 2
    assert pool.activeThreadCount() == 2
    assert maximum == 2
    try:
        pool.start(blocked_task)
    except RuntimeError as exc:
        assert "queue is full" in str(exc).lower()
    else:
        raise AssertionError("bounded pool accepted work beyond max_pending")
    gate.set()
    assert pool.waitForDone(2_000)


def test_diagnostics_reports_stale_ytdlp_warning() -> None:
    warning = "yt-dlp 2025.01.01 is 42 days old; update it if downloads fail"
    fake = SimpleNamespace(
        resolve_ytdlp_command=lambda: (["yt-dlp"], None),
        ytdlp_version_warning=lambda: warning,
        ytdlp_version=lambda: "2025.01.01",
    )

    commands = gui_app._diagnostics(fake)

    assert "yt-dlp freshness" in commands
    assert warning in commands["yt-dlp freshness"]


def test_diagnostics_reports_current_ytdlp_when_no_warning() -> None:
    fake = SimpleNamespace(
        resolve_ytdlp_command=lambda: (["yt-dlp"], None),
        ytdlp_version_warning=lambda: None,
        ytdlp_version=lambda: "2026.10.01",
    )

    commands = gui_app._diagnostics(fake)

    freshness = commands["yt-dlp freshness"]
    assert "2026.10.01" in freshness
    assert "up to date" in freshness


def test_diagnostics_survives_module_without_ytdlp_version_helpers() -> None:
    fake = SimpleNamespace(resolve_ytdlp_command=lambda: (["yt-dlp"], None))

    commands = gui_app._diagnostics(fake)

    assert commands["yt-dlp"] == "yt-dlp"
    assert "yt-dlp freshness" not in commands


def test_diagnostics_survives_raising_ytdlp_warning() -> None:
    def boom() -> str:
        raise RuntimeError("boom")

    fake = SimpleNamespace(
        resolve_ytdlp_command=lambda: (["yt-dlp"], None),
        ytdlp_version_warning=boom,
    )

    commands = gui_app._diagnostics(fake)

    assert "yt-dlp freshness" in commands
    assert "version check failed" in commands["yt-dlp freshness"]


def test_production_composition_builds_callable_non_null_services(tmp_path) -> None:
    searched: list[tuple[str, str, object]] = []
    created_managers: list[tuple[object, Path, Path]] = []

    class FakeManager(StubJobs):
        pass

    fake_fetchtui = SimpleNamespace(
        search_videos=lambda query, provider, filters, twitter_count=40:
            searched.append((query, provider, filters)) or [Video("one", "One", 1, 1, "url", provider)],
        resolve_stream_urls=lambda _video: (["https://media/stream.m3u8"], None),
        create_download_manager=lambda settings, temp, output, history_store=None:
            created_managers.append((settings, temp, output)) or FakeManager(),
        resolve_ytdlp_command=lambda: (["yt-dlp"], None),
    )

    services = compose_services(state_dir=tmp_path, fetchtui_module=fake_fetchtui)

    required = ("search_service", "preview_service", "download_manager", "local_storage",
                "plex_storage", "settings_service", "diagnostics", "thread_pool")
    assert all(services[name] is not None for name in required)
    assert services["startup_theme"] == "System"
    assert services["search_service"].search("query", "Eporner", {"min_duration": 2})
    assert services["preview_service"].resolve(Video("one", "One", 1, 1, "url", "Eporner"))
    assert callable(services["diagnostics"])
    assert created_managers and searched


def test_run_gui_reports_actionable_startup_composition_failure(qtbot, monkeypatch) -> None:
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(gui_app, "compose_services", lambda: (_ for _ in ()).throw(RuntimeError("bad storage")))
    monkeypatch.setattr(gui_app.QMessageBox, "critical",
                        lambda _parent, title, message: messages.append((title, message)))

    result = gui_app.run_gui([])

    assert result == 1
    assert messages and messages[0][0] == "Fetcher startup failed"
    assert "bad storage" in messages[0][1]
    assert "settings.json" in messages[0][1]
