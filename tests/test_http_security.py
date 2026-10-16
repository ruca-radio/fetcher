from __future__ import annotations

from pathlib import Path

from fetcher_core.http import USER_AGENT, build_client


REPO = Path(__file__).resolve().parents[1]
SKIPPED_DIRS = {".venv", ".git", "tmp", "__pycache__"}
DISABLED_TLS_MARKER = "verify" + "=False"


def test_no_httpx_clients_disable_tls_verification() -> None:
    offenders: list[str] = []

    for path in REPO.rglob("*.py"):
        if any(part in SKIPPED_DIRS for part in path.relative_to(REPO).parts):
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if DISABLED_TLS_MARKER in line:
                offenders.append(f"{path.relative_to(REPO)}:{line_number}")

    assert offenders == []


def test_build_client_sets_fetcher_user_agent() -> None:
    with build_client() as client:
        assert client.headers["User-Agent"] == USER_AGENT
