# AGENTS.md

## Repository Intent

This repository contains the publishable source for `Fetcher v1.0`, a standalone Textual-based video search and download TUI.

## Guardrails

- Treat the codebase as a general-purpose application, not a machine-specific setup
- Do not commit local credentials, cookies, API keys, downloaded media, logs, or caches
- Prefer portable paths derived from the repository root or user home directory over hard-coded host paths
- Keep scripts and docs aligned with the current standalone app, not older experiments

## Main Entry Points

- `fetchtui.py`: primary TUI application
- `fetch`: launcher script
- `extract_twitter_cookies.py`: helper for local Twitter/X cookie extraction
- `capture_twitter_api.js`: browser helper for debugging Twitter/X search requests
