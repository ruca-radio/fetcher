# Fetcher Ubuntu GUI Design

## Objective

Replace Fetcher's browser UI with a native Ubuntu-compatible PySide6 application while preserving the Textual TUI and CLI. All interfaces will share the same search, download, settings, media, and storage services. The resulting application must remain portable, keep private state outside source control, and support local or SSH/SFTP-backed Plex storage without embedding credentials.

## Scope

This pass will:

- add a native PySide6 desktop GUI;
- preserve the existing TUI and CLI entry points;
- remove the browser server, embedded web frontend, and `fetch-web` launcher;
- retain and harden the existing search and layered download behavior;
- add a desktop download queue with progress, stop, retry, and completed states;
- add a media library and file manager for staging and Plex storage;
- support configurable temporary, local output, and SFTP Plex locations;
- add local media playback with seek/scrub, volume, play/pause, and metadata;
- preview remote SFTP media through a bounded local cache;
- remove exploratory scripts and generated/session artifacts that are not product code;
- split the current monolith only along interfaces needed by the GUI, avoiding a full rewrite.

This pass will not add a database, cloud account system, embedded passwords, remote web access, transcoding server, or Plex API integration.

## Architecture

Fetcher will use a shared Python application layer with three thin front ends:

1. `fetcher_core` owns settings, models, search orchestration, downloads, storage, and media inspection.
2. `fetchtui.py` remains the Textual interface and imports shared services as they are extracted.
3. `fetcher_gui` provides the PySide6 desktop interface and invokes the same service APIs on background workers.
4. CLI commands continue to invoke the shared services directly.

Extraction will be incremental. Search/download behavior that already has regression coverage moves first. TUI-only widgets remain in `fetchtui.py`. The obsolete HTTP handler and embedded HTML/JavaScript are deleted once the GUI provides equivalent behavior.

## Desktop Experience

The GUI uses a conventional desktop shell with a left navigation rail and four workspaces:

- **Discover:** provider selector, query and filters, thumbnail result grid/table, preview action, and multi-select queueing.
- **Downloads:** active and historical jobs with progress, speed, ETA, target, stop, retry, reveal, and clear-completed actions.
- **Library:** two-pane file manager for local staging and Plex storage, with preview, rename, delete, copy, move, refresh, and metadata display.
- **Settings:** temporary directory, local staging directory, local or SFTP Plex destination, parallel downloads, fragments, filename template, provider options, theme, and executable diagnostics.

The app opens to Discover and keeps downloads alive while the user moves between workspaces. Destructive file actions require confirmation. Status messages identify the affected file or job and preserve useful provider errors.

## Media Preview and Scrubbing

The GUI uses Qt Multimedia (`QMediaPlayer`, `QAudioOutput`, and `QVideoWidget`) for local playback. The player exposes play/pause, a position slider, elapsed/duration labels, volume, mute, and full-screen playback. Slider dragging seeks without continuously restarting playback.

For search results, Fetcher first resolves a playable stream using the existing resolver and loads it into the player. Resolution happens off the UI thread and can be cancelled by selecting another item.

For SFTP files, Fetcher downloads the selected file into a bounded cache beneath `.fetcher/preview-cache` before playback. Cache entries use stable path-derived names, partial transfers are never offered to the player, and cache cleanup enforces configurable size and age limits. This avoids mounting assumptions while keeping SSH credentials in the user's normal agent or SSH configuration.

## Storage and File Management

Storage is represented by a small interface supporting list, stat, copy-in, copy-out, move, rename, delete, and preview materialization.

- `LocalStorage` confines all paths beneath its configured root using resolved-path checks.
- `SftpStorage` uses the system `sftp` client in non-interactive batch mode, bounded by timeouts. It accepts the existing portable `sftp:` setting and SSH config/keys but stores no password.

The Library workspace shows staging and Plex locations independently. Copy and move work between local and remote locations. Operations run in background workers, report failures verbatim, refresh only affected panes, and never silently overwrite an existing target. Rename accepts a filename only, not a path. Deletes require explicit confirmation.

Directory settings are validated when saved. An unavailable Plex target does not prevent local downloads: completed media remains in staging with a visible warning and can be transferred later.

## Download and Search Behavior

Existing provider search, filtering, deduplication, 720p floor, fallback download pipeline, cancellation, and failure preservation remain intact. GUI jobs use Qt signals to receive immutable progress snapshots; worker threads never mutate widgets directly.

Job history persists to a small JSON state file using atomic replacement. On startup, jobs previously marked active become interrupted and can be retried. A failed worker always reaches a terminal state. File finalization uses collision-safe targets and preserves partial data only when it can support a resume.

## Configuration

Settings remain under `.fetcher/settings.json` for source runs and are read through a typed settings service. Existing keys are migrated without discarding unknown safe values. Paths expand `~` but are stored portably when they reside beneath the repository or user home.

The GUI exposes diagnostics for `yt-dlp`, `ffmpeg`, `ffprobe`, `mpv`, and `sftp`. Missing optional tools disable only dependent actions and show corrective guidance. PySide6 is a GUI dependency declared by the project; the TUI and CLI continue to start without importing PySide6.

## Cleanup

The implementation will delete:

- `fetch-web` and the embedded HTTP/web UI code after GUI parity exists;
- `test_search.py`, `test_search2.py`, and `test_search3.py`, which are exploratory live-network scripts rather than deterministic tests;
- generated session directories and caches from the working tree while retaining ignore rules;
- dead imports, duplicate helpers, and web-only settings proven unused after removal.

`capture_twitter_api.js` and `extract_twitter_cookies.py` remain because they are documented provider-debugging/setup tools. Existing user-authored uncommitted functionality is preserved unless directly superseded by the GUI.

## Error Handling and Safety

- UI threads perform no network, subprocess, or large filesystem operation.
- Every background operation emits success, failure, and cancellation outcomes.
- Local file paths are root-confined before reading or mutation.
- SFTP commands quote batch arguments and reject path traversal/control characters.
- Settings and job history writes are atomic.
- No cookies, credentials, media, logs, cache files, or local settings enter Git.
- Application shutdown requests cancellation, stops accepting new work, and gives active workers a bounded grace period.

## Testing and Verification

All behavior changes follow red-green-refactor development. Deterministic tests cover:

- settings migration and validation;
- local and SFTP path safety;
- file collision and destructive-action rules;
- download state transitions, cancellation, interruption recovery, and error preservation;
- preview cache completion, eviction, and cleanup;
- GUI model/controller behavior with temporary directories and stub services;
- launcher behavior when PySide6 or optional media tools are unavailable;
- continued TUI and CLI startup without importing GUI dependencies.

Verification includes the full automated suite, Python compilation, package build, CLI smoke tests, headless Qt GUI startup, and a manual Ubuntu GUI checklist covering search, queueing, local preview with scrubbing, local file operations, and configured-storage failure behavior. Live provider and SFTP checks are reported separately because they depend on external services and local SSH configuration.

## Success Criteria

- `fetcher gui` and the source launcher open a responsive native application on Ubuntu.
- TUI and CLI continue to work independently.
- The web server and web launcher are gone.
- Search results can be previewed and queued without freezing the GUI.
- Local media can be played and scrubbed.
- Staging and Plex files can be safely listed, copied, moved, renamed, deleted, and previewed.
- Local and SFTP Plex configurations fail visibly and recoverably.
- Interrupted and failed downloads never remain indefinitely queued.
- Deterministic tests and packaging checks pass with no tracked private/runtime artifacts.
