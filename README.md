# Fetcher v1.0

Fetcher is a standalone video search, preview, and download application. It ships with a native PySide6 desktop GUI, a full-screen Textual TUI, and a scriptable CLI built on shared services.

## Highlights

- Multi-site search across PornHub, xHamster, RedTube, and Eporner
- Optional SearXNG web search (opt-in) that surfaces additional tube sites and resolves them through yt-dlp
- Twitter/X media search via local cookie import
- Queue-based downloads with configurable parallelism
- Browser-impersonated extraction (curl_cffi via yt-dlp `--impersonate`) plus an any-site Selenium fallback, so more searched videos resolve to a playable stream
- Native desktop discovery, downloads, library, settings, and media playback
- In-app playback through `mpv`
- Downloaded-file browser inside the TUI
- Headless CLI for search, direct downloads, and search-then-download batch workflows
- Portable checkout state and XDG-compliant installed-app state

## Requirements

- Python 3.11+
- `yt-dlp`
- `mpv` for playback
- PySide6 and Qt Multimedia for the native GUI
- `ffmpeg` and `ffprobe` for fallback downloads and media inspection
- The system `sftp` command for SFTP-backed storage
- Google Chrome for provider sign-in, the browser fallback, and Twitter/X cookie extraction

## Installation

### Option 1: Run from source

```bash
git clone https://github.com/ruca-radio/fetcher.git
cd fetcher
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Optional: accelerated MP4 transfers (bundles aria2 for supported platforms)
pip install '.[fast]'
```

Make sure `yt-dlp` and `mpv` are available in your `PATH`.

### Option 2: Install as a package

```bash
git clone https://github.com/ruca-radio/fetcher.git
cd fetcher
python3 -m venv .venv
source .venv/bin/activate
pip install '.[gui,fast]'
```

This installs the `fetcher` and `fetcher-gui` commands. To install only the TUI and CLI dependencies, use `pip install .`.

With uv, use `uv sync --extra gui --extra fast` (add `--extra test` for development).

HTTP/HTTPS video files use aria2 when installed, with up to 16 connections controlled by the concurrent fragments setting. HLS streams keep yt-dlp's native fragment downloader. Without aria2, downloads use yt-dlp's built-in downloader. Actual speed still depends on the media host and your connection.

If PornHub, RedTube, or xHamster extraction fails, Fetcher can start a separate headless Chrome session and read the running player's stream data. This requires Chrome and Selenium; it does not use your normal Chrome profile. Site login, regional restrictions, and unavailable videos can still prevent extraction. The Downloads page shows the full failure message below the table and in the ETA tooltip.

Completed media must meet `min_download_height` (720p by default); previews and in-app playback are exempt and use the best available stream. Fetcher never selects a known sub-`min_download_height` format: it drives every yt-dlp selector, the direct-URL resolver, and the browser player-data reader from that floor, so when no eligible stream exists the job fails fast with a clear message instead of downloading a low-quality file and deleting it afterwards. Search metadata that omits quality stays unknown until the download stage resolves the site's actual formats; resolved fallback URLs are inspected with `ffprobe` before a transfer starts, and the final `ffprobe` check remains a safety net.

## Quick Start

Launch the native GUI from a source checkout:

```bash
./fetch-gui
```

Or through the shared launcher/package command:

```bash
./fetch gui
fetcher gui
fetcher-gui
```

Launch the TUI:

```bash
./fetch
```

Or, after package install:

```bash
fetcher
```

By default, running `fetcher` with no subcommand starts the TUI.

The GUI is imported only for GUI commands, so the TUI and CLI remain usable when PySide6 is not installed.

## CLI Usage

### Search

Search across all supported sites:

```bash
fetcher search "query"
```

Search one site only and limit results:

```bash
fetcher search "query" --site PornHub --limit 10
```

Return JSON:

```bash
fetcher search "query" --json
```

Useful search options:

- `--site {All Sites,PornHub,xHamster,RedTube,Eporner,SearXNG,Twitter}` (`SearXNG` requires enabling SearXNG in settings)
- `--pages N`
- `--min-duration MINUTES`
- `--max-duration MINUTES`
- `--min-quality {720,1080}`
- `--min-rating N`
- `--hd-only`
- `--exclude "phrase1,phrase2"`
- `--limit N`

Twitter/X search also supports:

- `--count N`

### Download Explicit Targets

Download one or more direct targets:

```bash
fetcher download "https://www.pornhub.com/view_video.php?viewkey=XXXX"
```

Download multiple targets:

```bash
fetcher download URL1 URL2 URL3
```

If you provide a bare ID instead of a full URL, set `--site` so Fetcher can expand it into the canonical watch URL. Bare IDs are expanded for `PornHub`, `xHamster`, `RedTube`, and `Eporner`; other sites require a full URL.

Optional download arguments:

- `--site {PornHub,xHamster,RedTube,Eporner,Twitter}`
- `--output-dir PATH`
- `--tmp-dir PATH`
- `--min-height PIXELS` — overrides `min_download_height` for this command; `0` disables the floor

Provider sessions:

- `fetcher login --site <PornHub|RedTube|xHamster|Twitter>` — sign in through a Chrome window
- `fetcher logout --site <provider>` — forget the stored session
- `fetcher session-status [--site <provider>]` — check stored sessions (exit 0 only when every checked session is valid)

### Batch Search + Download

Search, take the top matching items, and download them:

```bash
fetcher batch "query" --site PornHub --limit 5
```

Run multiple downloads in parallel:

```bash
fetcher batch "query" --site "All Sites" --limit 8 --parallel 3
```

Print the selected batch as JSON before downloading:

```bash
fetcher batch "query" --limit 5 --json
```

Batch downloads accept the same `--min-height PIXELS` override as `download`.

## TUI Features

The Textual interface includes:

- Search tabs for video sites and Twitter/X
- Row selection and batch queueing
- Configurable parallel downloads
- Inline queue status and completed-download panels
- Playback controls for highlighted items
- A local file browser for the configured output directory
- Settings for theme, output path, temp path, thumbnails, previews, search defaults, and provider sign-in

## Native GUI

The desktop interface provides Discover, Downloads, Library, and Settings workspaces. Search and file operations run outside the UI thread, downloads retain stop/retry state, and the player supports local playback, seeking, volume, mute, and full screen. Settings includes provider sign-in for PornHub, RedTube, xHamster, and Twitter/X.

Local storage paths are used directly. An SFTP destination uses the portable form:

```text
sftp:user=media@host=nas.example.local:22/library/videos
```

Fetcher uses the system `sftp` client in batch mode. Configure SSH keys and host verification through your normal SSH configuration; Fetcher does not store SSH passwords. Downloads complete in local staging first, so an unavailable remote destination does not discard finished media.

Remote previews are materialized under `.fetcher/preview-cache` in a source checkout and `$XDG_CACHE_HOME/fetcher/preview-cache` after package installation. The cache is bounded by configured age and size limits, but previewing a large remote file can still require transferring the full file before playback begins. Provider search-result previews depend on the provider exposing a resolvable stream and may be unavailable.

## Provider Sign-In

PornHub, RedTube, xHamster, and Twitter/X may require a signed-in session (including an accepted age prompt) before they expose their full catalog, player data, or downloads. Instead of copying cookies by hand:

- TUI: open the **Settings** tab → **Provider Sign-ins** → **Sign in**. The Twitter/X tab's **Login** button now starts the same browser sign-in.
- GUI: **Settings** → **Provider Sign-ins** → **Sign in**.
- CLI: `fetcher login --site PornHub` (also `RedTube`, `xHamster`, `Twitter`), and `fetcher logout --site PornHub` to forget the session.

Fetcher opens a visible Chrome window on the provider's sign-in page. Log in there (including any 2FA), then close the window; Fetcher captures the resulting session cookies. They are stored owner-only under `.fetcher/cookies` in a source checkout (or `$XDG_DATA_HOME/fetcher/cookies` after package installation), never logged, and used by search requests, yt-dlp downloads, previews, and the browser fallback.

Each row also has a **Test** button (CLI: `fetcher session-status [--site SITE]`) that performs a lightweight authenticated check and reports whether the stored session is still logged in. Sites that hide their login state behind a regional age wall are reported as "could not be verified" rather than guessed. **Signing in does not bypass regional age-verification blocks**; see Troubleshooting.

## Twitter/X Setup

Fetcher does not store credentials in source control. To enable Twitter/X search locally:

```bash
python3 extract_twitter_cookies.py
```

This writes cookie state to:

```text
./.fetcher/twitter_cookies.json
```

For compatibility you can still import `auth_token` manually by writing `.fetcher/twitter_cookies.json` (for example with `extract_twitter_cookies.py`); the TUI login dialog now uses the browser sign-in described above instead of pasting tokens.

Fetcher uses this stored cookie state for both Twitter/X search and downloads. For downloads it converts the JSON into a temporary Netscape cookie file for yt-dlp, so Twitter/X downloads no longer require a Chrome profile (Fetcher only falls back to `--cookies-from-browser chrome` when no stored `auth_token` is available).

The manual token import remains supported for compatibility, but the **Provider Sign-ins → Twitter** browser flow (or the Twitter tab's **Login** button) is now the normal way to authenticate and stores the full browser session instead of only `auth_token`.

## Runtime State

In a source checkout, Fetcher stores local runtime state in:

```text
./.fetcher/
```

This directory includes:

- `settings.json`
- `downloaded_ids.txt`
- `twitter_cookies.json`
- `cookies/` — owner-only provider sign-in sessions (PornHub, RedTube, xHamster, Twitter/X)

A few `settings.json` values are user-tunable:

- `min_download_height` (default `720`) — quality floor for downloads. It drives every yt-dlp format selector, the direct-URL resolver, and the browser player-data reader, so Fetcher fails fast rather than downloading sub-floor media. Set it to `0` (no minimum) to download sites that only serve sub-720p media. This is editable in both the TUI and GUI settings screens.
- `eporner_preview_ratio` (default `0.6`) and `eporner_preview_min_seconds` (default `90`) — the Eporner blurred/teaser-preview guard: when a listing is at least `eporner_preview_min_seconds` long, a stream shorter than `eporner_preview_ratio` × the listed duration is rejected.
- `search_filters.exclude_censored` (default `true`) — skips Eporner censored/mosaic uploads; also toggleable in the TUI search-filter screen.

These can be edited directly in `settings.json`.

After package installation, mutable state is stored under `$XDG_DATA_HOME/fetcher` (normally `~/.local/share/fetcher`) and caches and temporary files under `$XDG_CACHE_HOME/fetcher` (normally `~/.cache/fetcher`). Fetcher does not attempt to write into read-only site-packages directories.

In a source checkout, temporary files default to:

```text
./tmp/
```

Downloaded output defaults to:

```text
~/Downloads/fetcher
```

Both paths can be changed in the GUI or TUI and overridden in CLI commands.

## Troubleshooting

- If provider sign-in fails to capture a session, confirm Chrome is installed and that you completed the login before closing the window; the session files live under `.fetcher/cookies` and can be cleared with **Sign out** or `fetcher logout --site <provider>`. Signing in still cannot unlock a network/region that the site blocks outright.
- If PornHub search reports an 18+ age-verification restriction or its restricted SFW catalog, PornHub is refusing the full catalog to this network/region. Fetcher sends the same age-gate cookies a browser's 18+ "Enter" control sets (`accessAgeDisclaimerPH`, `age_verified`, `accessPH`), retries gated requests, and accepts the prompt in its headless-Chrome fallback, but the prompt alone cannot unlock PornHub's server-side restricted catalog. Change the VPN or network exit to a non-restricted region and retry.
- If a video extracts on the site but fails in Fetcher, the download stages fall back through yt-dlp (with curl_cffi browser impersonation when available), then a headless-Chrome player-data reader that now works for any host, and finally a direct ffmpeg copy. A failure message always names the last stage that was tried.
- If a RedTube (or another Aylo site) download reports that the site "has disabled access from <region> (regional age-verification requirements) and served its age-verification wall/homepage instead of the requested video", the site redirected this network to its age wall. The real video page and its HD streams are unavailable from that region, and the age-wall page only contains a low-quality promo clip; Fetcher refuses to download the promo. Change the network/VPN exit to a region where the site serves the full catalog and retry.
- If a download fails with a message that only sub-`min_download_height` media is available, Fetcher refused to transfer it. Lower `min_download_height` (0 disables the floor) in the TUI or GUI settings if a site only serves sub-720p media; the download no longer wastes bandwidth on a rejected file.
- If `fetcher gui` reports that PySide6 is missing, run `python -m pip install 'fetcher-v1[gui]'` (or `python -m pip install '.[gui]'` from a checkout).
- If video playback is unavailable, install your distribution's Qt Multimedia runtime and verify `ffprobe` can inspect the file. The TUI uses `mpv` separately.
- If SFTP storage is unavailable, first confirm `sftp host` connects non-interactively with the same user account. Finished downloads remain in local staging for a later transfer.
- A missing provider preview is not necessarily a failed search or download; preview availability depends on a provider exposing a playable stream.

## Development

Basic checks:

```bash
python3 -m compileall -q fetcher_core fetcher_gui fetchtui.py
python3 -m pytest -q
bash -n fetch fetch-gui
```

Run directly during development:

```bash
python3 fetchtui.py
python3 fetchtui.py search "query" --limit 5
```

## Project Files

- `fetchtui.py`: TUI app plus CLI entrypoint
- `fetcher_core/`: shared settings, storage, preview, and download services
- `fetcher_gui/`: native PySide6 desktop application
- `fetch`: TUI/CLI launcher script
- `fetch-gui`: native GUI launcher script
- `extract_twitter_cookies.py`: helper for local Twitter/X auth import
- `capture_twitter_api.js`: browser debugging helper
- `pyproject.toml`: packaging metadata and console script definition

## License

MIT. See [LICENSE](LICENSE).
