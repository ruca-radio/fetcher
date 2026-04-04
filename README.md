# Fetcher v1.0

Fetcher is a standalone terminal application for searching, previewing, and downloading videos from multiple sources. It ships with a full-screen Textual TUI and a scriptable CLI built on the same search and download engine.

## Highlights

- Multi-site search across PornHub, xHamster, RedTube, and Eporner
- Twitter/X media search via local cookie import
- Queue-based downloads with configurable parallelism
- In-app playback through `mpv`
- Downloaded-file browser inside the TUI
- Headless CLI for search, direct downloads, and search-then-download batch workflows
- Portable local state stored under `./.fetcher/`

## Requirements

- Python 3.11+
- `yt-dlp`
- `mpv` for playback
- Google Chrome if you want Twitter/X cookie extraction

## Installation

### Option 1: Run from source

```bash
git clone https://github.com/ruca-radio/fetcher.git
cd fetcher
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Make sure `yt-dlp` and `mpv` are available in your `PATH`.

### Option 2: Install as a package

```bash
git clone https://github.com/ruca-radio/fetcher.git
cd fetcher
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

This installs the `fetcher` command from `pyproject.toml`.

## Quick Start

Launch the TUI:

```bash
./fetch
```

Or, after package install:

```bash
fetcher
```

By default, running `fetcher` with no subcommand starts the TUI.

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

- `--site {All Sites,PornHub,xHamster,RedTube,Eporner,Twitter}`
- `--pages N`
- `--min-duration MINUTES`
- `--max-duration MINUTES`
- `--min-quality {any,360,480,720,1080}`
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

If you provide a bare ID instead of a full URL, the CLI currently assumes `PornHub` unless you explicitly set `--site`.

Optional download arguments:

- `--site {PornHub,xHamster,RedTube,Eporner,Twitter}`
- `--output-dir PATH`
- `--tmp-dir PATH`

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

## TUI Features

The Textual interface includes:

- Search tabs for video sites and Twitter/X
- Row selection and batch queueing
- Configurable parallel downloads
- Inline queue status and completed-download panels
- Playback controls for highlighted items
- A local file browser for the configured output directory
- Settings for theme, output path, temp path, thumbnails, previews, and search defaults

## Twitter/X Setup

Fetcher does not store credentials in source control. To enable Twitter/X search locally:

```bash
python3 extract_twitter_cookies.py
```

This writes cookie state to:

```text
./.fetcher/twitter_cookies.json
```

You can also use the TUI login dialog and paste either:

- a raw `auth_token`
- a JSON object containing `auth_token`

## Runtime State

Fetcher stores local runtime state in:

```text
./.fetcher/
```

This directory includes:

- `settings.json`
- `downloaded_ids.txt`
- `twitter_cookies.json`

Temporary files default to:

```text
./tmp/
```

Downloaded output defaults to:

```text
~/Downloads/fetcher
```

Both paths can be changed in the TUI or overridden in CLI commands.

## Development

Basic checks:

```bash
python3 -m py_compile fetchtui.py extract_twitter_cookies.py
bash -n fetch
```

Run directly during development:

```bash
python3 fetchtui.py
python3 fetchtui.py search "query" --limit 5
```

## Project Files

- `fetchtui.py`: TUI app plus CLI entrypoint
- `fetch`: local launcher script
- `extract_twitter_cookies.py`: helper for local Twitter/X auth import
- `capture_twitter_api.js`: browser debugging helper
- `pyproject.toml`: packaging metadata and console script definition

## License

MIT. See [LICENSE](LICENSE).
