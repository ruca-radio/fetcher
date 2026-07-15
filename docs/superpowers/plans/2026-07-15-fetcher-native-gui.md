# Fetcher Native GUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Fetcher's browser UI with a native PySide6 Ubuntu GUI while preserving and hardening the TUI, CLI, downloads, previews, and local/SFTP Plex file management.

**Architecture:** Extract stable service boundaries from `fetchtui.py` into `fetcher_core`, then build `fetcher_gui` as a Qt-only adapter loaded lazily by the `gui` command. Storage, queueing, settings, and preview caching remain UI-independent and are tested without network access; the old HTTP server is removed only after GUI parity exists.

**Tech Stack:** Python 3.11+, PySide6/Qt Multimedia, Textual, httpx, yt-dlp, ffmpeg/ffprobe, system OpenSSH `sftp`, unittest/pytest, pytest-qt, Hatch.

---

## File Structure

- `fetcher_core/models.py` — shared immutable/value models and existing job model.
- `fetcher_core/settings.py` — defaults, migration, validation, atomic persistence, runtime paths.
- `fetcher_core/storage.py` — safe local and SFTP storage backends.
- `fetcher_core/preview.py` — collision-safe bounded preview cache.
- `fetcher_core/jobs.py` — download manager, snapshots, history, cancellation/retry.
- `fetcher_gui/app.py` — lazy Qt startup and service composition.
- `fetcher_gui/main_window.py` — navigation shell and shutdown.
- `fetcher_gui/workers.py` — reusable background-task bridge.
- `fetcher_gui/player.py` — Qt Multimedia playback and seeking.
- `fetcher_gui/discover.py` — search/results/queue workspace.
- `fetcher_gui/downloads.py` — job workspace.
- `fetcher_gui/library.py` — two-pane local/Plex file manager.
- `fetcher_gui/settings.py` — paths, SFTP, concurrency, diagnostics.
- `tests/` — deterministic core and headless Qt coverage.
- `fetchtui.py` — retained TUI/CLI plus existing provider/downloader implementation during incremental extraction.

### Task 1: Shared models and safe settings

**Files:**
- Create: `fetcher_core/__init__.py`
- Create: `fetcher_core/models.py`
- Create: `fetcher_core/settings.py`
- Create: `tests/core/test_settings.py`
- Modify: `fetchtui.py`

- [ ] **Step 1: Write failing settings tests**

Cover legacy JSON loading, unknown-key retention, atomic replacement, corrupt JSON reporting, range validation, `~` expansion, and SFTP parsing. The target API is:

```python
store = SettingsStore(path, defaults=AppSettings())
loaded = store.load()
validated = store.validate(loaded.settings)
store.save(validated)
assert loaded.unknown == {"future_key": "preserved"}
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/core/test_settings.py -q`
Expected: FAIL because `fetcher_core.settings` does not exist.

- [ ] **Step 3: Extract models and implement settings**

Move shared dataclasses without changing their public fields. Implement `SettingsStore` with a sibling temporary file, `flush`/`fsync`, and `os.replace`; clamp pages to `1..20`, parallel downloads to `1..16`, and fragments to `1..32`. Reject control characters and malformed SFTP specifications. Preserve unknown JSON keys on save.

- [ ] **Step 4: Re-export compatibility names**

Import the extracted names into `fetchtui.py` so current TUI/CLI callers and existing tests remain valid. Remove import-time creation of user directories; create them only when settings/runtime services initialize.

- [ ] **Step 5: Verify GREEN and regression suite**

Run: `python -m pytest tests/core/test_settings.py test_extract_twitter_cookies.py test_fetcher_runtime.py -q`
Expected: all pass.

### Task 2: Safe storage and preview cache

**Files:**
- Create: `fetcher_core/storage.py`
- Create: `fetcher_core/preview.py`
- Create: `tests/core/test_storage_local.py`
- Create: `tests/core/test_storage_sftp.py`
- Create: `tests/core/test_preview_cache.py`

- [ ] **Step 1: Write failing storage safety tests**

Use temporary directories and a fake SFTP runner to require this protocol:

```python
class StorageBackend(Protocol):
    def list(self, rel_dir: PurePosixPath = PurePosixPath()) -> list[StorageEntry]: ...
    def copy_in(self, source: Path, relpath: PurePosixPath, *, overwrite: bool = False) -> StorageEntry: ...
    def copy_out(self, relpath: PurePosixPath, destination: Path, *, overwrite: bool = False) -> Path: ...
    def rename(self, relpath: PurePosixPath, filename: str, *, overwrite: bool = False) -> StorageEntry: ...
    def delete(self, relpath: PurePosixPath) -> None: ...
    def materialize_preview(self, relpath: PurePosixPath) -> Path: ...
```

Require rejection of absolute paths, `.`/`..`, symlink escape, CR/LF/NUL/control characters, path-bearing rename names, and target collisions. Require move/copy failures to leave the source intact.

- [ ] **Step 2: Verify storage RED**

Run: `python -m pytest tests/core/test_storage_local.py tests/core/test_storage_sftp.py -q`
Expected: FAIL because storage backends do not exist.

- [ ] **Step 3: Implement LocalStorage and SftpStorage**

Use resolved-root confinement for every local operation. For SFTP, validate all values before generating batch text, honor normal SSH configuration, use batch mode and bounded connection/operation timeouts, preserve stderr, and never silently overwrite. Transfer into a temporary target and publish/delete source only after success.

- [ ] **Step 4: Write preview-cache tests and verify RED**

Require SHA-256 keys derived from storage identity plus normalized path, `.partial` download then atomic replace, cleanup after producer failure, distinct entries for equal basenames, and deterministic age/size eviction using an injected clock.

Run: `python -m pytest tests/core/test_preview_cache.py -q`
Expected: FAIL because `PreviewCache` does not exist.

- [ ] **Step 5: Implement PreviewCache and verify GREEN**

Implement `materialize(key, suffix, producer)`, `cleanup()`, and active-partial protection. Run all three task test files and expect all pass.

### Task 3: Reliable shared download manager

**Files:**
- Create: `fetcher_core/jobs.py`
- Create: `tests/core/test_download_manager.py`
- Modify: `fetchtui.py`
- Modify: `test_fetcher_runtime.py`

- [ ] **Step 1: Write failing job lifecycle tests**

Test duplicate rejection, `queued -> downloading -> done`, worker exception to `failed`, stop/cancel, retry, bounded executor behavior, immutable snapshots, and restart conversion of active jobs to `interrupted`. Use an injected synchronous fake downloader.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/core/test_download_manager.py -q`
Expected: FAIL because `DownloadManager` and `JobHistoryStore` do not exist.

- [ ] **Step 3: Implement queue and history**

`DownloadManager` owns the executor and guarantees every submitted job reaches a terminal/interrupted state and clears `worker_started` in `finally`. `JobHistoryStore` persists `.fetcher/jobs.json` atomically. Subscribers receive copied `JobSnapshot` values; no UI reads mutable jobs across threads.

- [ ] **Step 4: Integrate TUI-compatible downloader callbacks**

Retain the current `run_download()` fallback ordering and callbacks. Port the web-worker exception regression to `DownloadManager`, then remove its dependence on `FetcherWebServer`.

- [ ] **Step 5: Verify GREEN**

Run: `python -m pytest tests/core/test_download_manager.py test_fetcher_runtime.py -q`
Expected: all pass.

### Task 4: Qt infrastructure and media player

**Files:**
- Create: `fetcher_gui/__init__.py`
- Create: `fetcher_gui/workers.py`
- Create: `fetcher_gui/player.py`
- Create: `tests/gui/test_workers.py`
- Create: `tests/gui/test_player.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Add GUI/dev extras and install them**

Declare `gui = ["PySide6>=6.8,<7"]` and test tooling under a development extra. Install with `.venv/bin/python -m pip install -e '.[gui,test]'`.

- [ ] **Step 2: Write failing Qt tests**

Test `Task` success/error/cancel signals and player scrubbing semantics through an injected player adapter: position updates do not move the slider while dragging, `sliderReleased` seeks exactly once, and an unseekable source disables the slider.

- [ ] **Step 3: Verify RED**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/gui/test_workers.py tests/gui/test_player.py -q`
Expected: FAIL because GUI modules do not exist.

- [ ] **Step 4: Implement workers and MediaPlayerWidget**

Use `QRunnable` plus a separate signal object. `MediaPlayerWidget` owns `QMediaPlayer`, `QAudioOutput`, `QVideoWidget`, play/pause, mute/volume, position/duration labels, a scrub slider, and full-screen action. Local paths use `QUrl.fromLocalFile`; URLs use `QUrl` directly; errors are visible.

- [ ] **Step 5: Verify GREEN and import isolation**

Run Qt tests, then run a subprocess test that blocks `PySide6` imports while importing `fetchtui` and displaying CLI help. Both must pass.

### Task 5: Native GUI shell and workspaces

**Files:**
- Create: `fetcher_gui/app.py`
- Create: `fetcher_gui/main_window.py`
- Create: `fetcher_gui/discover.py`
- Create: `fetcher_gui/downloads.py`
- Create: `fetcher_gui/library.py`
- Create: `fetcher_gui/settings.py`
- Create: `tests/gui/test_models.py`
- Create: `tests/gui/test_main_window.py`

- [ ] **Step 1: Write failing headless GUI/model tests**

Require a `MainWindow` with Discover, Downloads, Library, and Settings navigation; deterministic result/job/storage models; stale search/preview generation suppression; confirmation callbacks for destructive operations; and responsive construction with stub services.

- [ ] **Step 2: Verify RED**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/gui/test_models.py tests/gui/test_main_window.py -q`
Expected: FAIL because pages and shell do not exist.

- [ ] **Step 3: Implement Discover and Downloads**

Discover provides query, provider/filter controls, thumbnail results, multi-selection, preview, and queue actions. Searches and stream resolution use background tasks with monotonically increasing generation IDs. Downloads renders immutable snapshots and exposes stop, retry, reveal, and clear-completed actions.

- [ ] **Step 4: Implement Library and Settings**

Library has local staging and Plex panes with refresh, preview, copy, move, rename, delete, and metadata; operations run off-thread, refuse overwrite, and refresh affected panes. Settings configures temp/staging/local-or-SFTP Plex paths, concurrency, filename template, provider preferences, cache limits, and executable diagnostics.

- [ ] **Step 5: Implement shell/shutdown and verify GREEN**

The shell retains the player while navigating. Shutdown disables submissions, invalidates generations, requests cancellation, persists interrupted jobs, waits a bounded grace period, and closes. Run the task tests and expect all pass.

### Task 6: Launching, web removal, cleanup, and documentation

**Files:**
- Create: `fetch-gui`
- Modify: `fetch`
- Modify: `fetchtui.py`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `.gitignore`
- Delete: `fetch-web`
- Delete: `test_search.py`
- Delete: `test_search2.py`
- Delete: `test_search3.py`
- Create: `tests/test_launchers.py`
- Create: `tests/test_import_boundaries.py`

- [ ] **Step 1: Write failing launcher/import tests**

Require `fetcher gui`, executable `fetch-gui`, repo-venv preference, actionable missing-PySide6 error, unchanged `tui/search/download/batch` parsing, and no PySide6 import during TUI/CLI startup.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_launchers.py tests/test_import_boundaries.py -q`
Expected: FAIL because GUI launch paths do not exist and web paths remain.

- [ ] **Step 3: Wire GUI lazily and remove web implementation**

Add the `gui` parser branch and import `fetcher_gui.app` only inside it. Delete `WEB_HTML`, `FetcherWebServer`, `run_web`, HTTP imports, `web` parser/dispatch, and `fetch-web` after their reusable settings/storage/job behavior is covered by core services.

- [ ] **Step 4: Delete obsolete artifacts and update docs/package data**

Delete the three exploratory live-network scripts and generated session/cache directories from the working tree. Retain ignore rules for `.fetcher/`, `session-*/`, caches, media, logs, cookies, and preview data. Include both packages and launchers in the wheel. Rewrite README installation, GUI usage, SFTP/SSH expectations, TUI/CLI usage, preview limitations, and troubleshooting.

- [ ] **Step 5: Verify GREEN**

Run launcher/import tests, `python -m compileall -q fetcher_core fetcher_gui fetchtui.py`, and CLI help smoke tests. Expect all pass.

### Task 7: Full verification and review

**Files:**
- Modify only files required by verified failures.

- [ ] **Step 1: Run complete automated suite**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q`
Expected: zero failures.

- [ ] **Step 2: Run packaging and dependency checks**

Run: `.venv/bin/python -m build`, install the wheel into a temporary virtual environment, and smoke `fetcher --help`, `fetcher tui --help`, and `fetcher gui --help`.

- [ ] **Step 3: Run GUI smoke and media fixture check**

Construct/show/close `MainWindow` offscreen. On Ubuntu desktop, open a generated known-good H.264/AAC MP4, play/pause, scrub, change volume, and enter/exit full screen. Record codec/backend limitations separately from unit results.

- [ ] **Step 4: Run storage and queue smoke checks**

Using temporary local roots, exercise list/copy/move/rename/delete/preview and collision refusal. Exercise an unreachable SFTP setting and confirm bounded visible failure. Queue a controlled local 720p fixture, verify completion, stop/retry, and interrupted-history recovery.

- [ ] **Step 5: Review repository hygiene and diff**

Run `git status -sb`, `git diff --check`, a tracked-file scan for credentials/host paths/media/logs, and a final requirements-to-tests checklist. Request independent spec compliance and code-quality reviews; resolve all Critical/Important findings before completion.
