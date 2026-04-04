# Fetcher v1.0

Terminal UI for searching, previewing, and downloading videos from multiple sites with a persistent queue.

## Current Scope

- Search across PornHub, xHamster, RedTube, and Eporner
- Search Twitter/X media results after local cookie import
- Queue downloads and run them sequentially or in parallel batches
- Play highlighted streams in `mpv`
- Browse downloaded files from inside the TUI

## Requirements

- Python 3.11+
- `yt-dlp`
- `mpv`
- Google Chrome if you want Twitter/X cookie import

Install Python dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
./fetch
```

Or:

```bash
python3 fetchtui.py
```

## Runtime State

Fetcher stores local state in `./.fetcher/`:

- `settings.json`
- `downloaded_ids.txt`
- `twitter_cookies.json`

Temporary files are written to `./tmp/` by default.

The default output directory is `~/Downloads/fetcher`, but you can change it in the Settings tab.

## Twitter/X Login

Fetcher does not embed credentials. To enable Twitter/X search:

```bash
python3 extract_twitter_cookies.py
```

Or use the in-app login dialog and paste an `auth_token` or cookie JSON.

## Publishing Notes

- Runtime state, cookies, logs, caches, downloads, and bundled binaries are intentionally gitignored
- `yt-dlp` is expected from the local checkout only if you explicitly place a copy beside `fetchtui.py`
- This repository is prepared as an app codebase, not as a personal media archive
