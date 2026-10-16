from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest

from fetcher_core import download
from fetcher_core.models import DownloadJob, DownloadPolicy, Video


def video(site: str = "Eporner", identifier: str = "abc123") -> Video:
    return Video(identifier, "A / Bad: Title?!", 120, 0, f"https://example.test/watch/{identifier}", site)


def test_build_ytdlp_download_cmd_golden(monkeypatch) -> None:
    monkeypatch.setattr(download.shutil, "which", lambda name: None)
    monkeypatch.setattr(download.sys, "executable", "/nonexistent/python")
    job = DownloadJob(video=video("Twitter", "tw123"))
    cmd = download._build_ytdlp_download_cmd(
        ["python", "-m", "yt_dlp"],
        job,
        Path("/tmp/job"),
        "%(title).180B [%(id)s].%(ext)s",
        download.YTDLP_FORMAT,
        4,
    )

    assert cmd == [
        "python", "-m", "yt_dlp",
        "-o", "/tmp/job/%(title).180B [%(id)s].%(ext)s",
        "-f", "bestvideo[height>=720]+bestaudio/best[height>=720]",
        "--merge-output-format", "mkv",
        "--check-formats",
        "--no-playlist", "--newline", "--progress", "--no-warnings",
        "--retries", "3", "--fragment-retries", "5",
        "--continue", "--part",
        "--concurrent-fragments", "4",
        "--print", "after_move:__FETCHER_DEST__:%(filepath)s",
        "--socket-timeout", "20",
        "--cookies-from-browser", "chrome",
        "https://example.test/watch/tw123",
    ]


def test_eporner_command_keeps_search_title_on_embed_url() -> None:
    item = video()
    item.title = "100% take: one"
    cmd = download._build_ytdlp_download_cmd(
        ["python", "-m", "yt_dlp"],
        DownloadJob(video=item),
        Path("/tmp/job"),
        "%(title).180B [%(id)s].%(ext)s",
        download.YTDLP_FORMAT,
        1,
    )

    assert cmd[cmd.index("--parse-metadata") + 1] == r"100%% take\: one:%(title)s"
    assert cmd[-1] == "https://www.eporner.com/embed/abc123/"


def test_safe_media_stem_job_temp_dir_and_no_ffprobe(monkeypatch, tmp_path: Path) -> None:
    v = video("Bad Site!", "id/with spaces")
    assert download._safe_media_stem(v) == "A Bad Title [id/with spaces]"
    assert download._job_temp_dir(tmp_path, v) == tmp_path / "id-with-spaces"
    monkeypatch.setattr(download, "FFPROBE", None)
    assert download._probe_video_height(tmp_path / "missing.mkv") is None
    assert download._probe_video_duration(tmp_path / "missing.mkv") is None


def test_finalize_rejects_short_eporner_preview_and_deletes(monkeypatch, tmp_path: Path) -> None:
    dest = tmp_path / "tmp" / "clip.mkv"
    dest.parent.mkdir()
    dest.write_bytes(b"x")
    job = DownloadJob(video=video("Eporner"))
    monkeypatch.setattr(download, "_probe_video_height", lambda path: 1080)
    monkeypatch.setattr(download, "_probe_video_duration", lambda path: 50.0)

    ok = download._finalize_downloaded_file(
        job, dest, dest.parent, tmp_path / "out", False,
        DownloadPolicy(min_height=0, preview_ratio=0.6, preview_min_seconds=90),
    )

    assert not ok
    assert job.status == "failed"
    assert "blurred preview" in job.eta
    assert not dest.exists()


def test_finalize_accepts_correct_length_eporner_file(monkeypatch, tmp_path: Path) -> None:
    dest = tmp_path / "tmp" / "clip.mkv"
    dest.parent.mkdir()
    dest.write_bytes(b"x")
    job = DownloadJob(video=video("Eporner"))
    monkeypatch.setattr(download, "DOWNLOADED_IDS", tmp_path / "state" / "downloaded_ids.txt")
    monkeypatch.setattr(download, "_probe_video_height", lambda path: 1080)
    monkeypatch.setattr(download, "_probe_video_duration", lambda path: 120.0)

    ok = download._finalize_downloaded_file(
        job, dest, dest.parent, tmp_path / "out", False,
        DownloadPolicy(min_height=0, preview_ratio=0.6, preview_min_seconds=90),
    )

    assert ok
    assert job.status == "done"
    assert job.filepath == tmp_path / "out" / "clip.mkv"
    assert job.filepath.exists()


def test_run_download_keeps_primary_ytdlp_error_when_generic_fallback_masks_it(monkeypatch, tmp_path: Path) -> None:
    job = DownloadJob(video=video("PornHub", "ph1"))
    calls = {"n": 0}

    def fake_run_process(job, cmd, progress_cb, *, expected_dest=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return 1, None, deque(["ERROR: PornHub said no"], maxlen=12)
        return 1, None, deque(["ERROR: Unsupported URL: generic"], maxlen=12)

    monkeypatch.setattr(download, "resolve_ytdlp_command", lambda: (["yt-dlp"], None))
    monkeypatch.setattr(download, "_run_download_process", fake_run_process)
    monkeypatch.setattr(download, "_resolve_direct_download_urls", lambda video, cmd, *, force_generic: ([], "Unsupported URL"))
    monkeypatch.setattr(download, "ytdlp_version_warning", lambda: None)

    download.run_download(job, tmp_path / "tmp", tmp_path / "out", lambda: None, lambda job: None)

    assert job.status == "failed"
    assert job.eta == "ERROR: PornHub said no"


def test_ytdlp_version_warning(monkeypatch) -> None:
    today = download.date.today().strftime("%Y.%m.%d")
    monkeypatch.setattr(download, "ytdlp_version", lambda: today)
    assert download.ytdlp_version_warning() is None

    monkeypatch.setattr(download, "ytdlp_version", lambda: "2024.01.01")
    warning = download.ytdlp_version_warning()
    assert warning is not None
    assert warning.startswith("yt-dlp 2024.01.01 is ")
    assert warning.endswith("+ days old; update it if downloads fail")


def test_job_temp_dir_matches_original_fetchtui_semantics(tmp_path: Path) -> None:
    # id-less video falls back to the sanitized title
    titled = Video("", "A / Bad: Title?!", 120, 0, "https://example.test/watch/x", "Eporner")
    assert download._job_temp_dir(tmp_path, titled) == tmp_path / "A-Bad-Title"

    # very long (e.g. URL-derived) ids are capped at 80 characters
    capped = download._job_temp_dir(tmp_path, video("Eporner", "a" * 200))
    assert capped == tmp_path / ("a" * 80)
    assert len(capped.name) == 80

    # empty id and title fall back to "job"
    empty = Video("", "", 0, 0, "https://example.test/watch/y", "Eporner")
    assert download._job_temp_dir(tmp_path, empty) == tmp_path / "job"


def test_ytdlp_format_derives_from_policy_min_height() -> None:
    assert download._ytdlp_format(720) == download.YTDLP_FORMAT
    assert "height>=1080" in download._ytdlp_format(1080)
    assert download._ytdlp_format(1080) == "bestvideo[height>=1080]+bestaudio/best[height>=1080]"


def test_run_download_first_attempt_uses_policy_height(monkeypatch, tmp_path: Path) -> None:
    job = DownloadJob(video=video("Eporner", "ep1"))
    selectors: list[str] = []

    def fake_build_cmd(ytdlp_cmd, job, job_tmp_dir, filename_template, format_selector, concurrent_fragments, *, force_generic=False):
        selectors.append(format_selector)
        return [*ytdlp_cmd, "unused"]

    monkeypatch.setattr(download, "resolve_ytdlp_command", lambda: (["yt-dlp"], None))
    monkeypatch.setattr(download, "_build_ytdlp_download_cmd", fake_build_cmd)
    monkeypatch.setattr(
        download,
        "_run_download_process",
        lambda job, cmd, progress_cb, *, expected_dest=None: (1, None, deque(["ERROR: no formats"], maxlen=12)),
    )
    monkeypatch.setattr(download, "_resolve_direct_download_urls", lambda video, cmd, *, force_generic: ([], "no urls"))
    monkeypatch.setattr(download, "ytdlp_version_warning", lambda: None)

    download.run_download(
        job, tmp_path / "tmp", tmp_path / "out", lambda: None, lambda job: None,
        policy=DownloadPolicy(min_height=1080),
    )

    assert "height>=1080" in selectors[0]
    assert selectors[1] == download.YTDLP_FLEX_FORMAT
    assert selectors[2] == download.YTDLP_FLEX_FORMAT
    assert job.status == "failed"


def test_cancelled_download_does_not_launch_fallbacks(monkeypatch, tmp_path):
    job = DownloadJob(video=video())
    attempts = []

    def stopped(job, cmd, progress_cb, **kwargs):
        attempts.append(cmd)
        job.status = "cancelled"
        return -15, None, deque(["terminated"])

    monkeypatch.setattr(download, "resolve_ytdlp_command", lambda: (["yt-dlp"], None))
    monkeypatch.setattr(download, "_run_download_process", stopped)
    monkeypatch.setattr(download, "_resolve_direct_download_urls",
                        lambda *args, **kw: pytest.fail("cancelled download resolved another stream"))
    completed = []
    download.run_download(job, tmp_path / "tmp", tmp_path / "out", lambda: None, completed.append)

    assert len(attempts) == 1
    assert job.status == "cancelled"
    assert completed == [job]


@pytest.mark.parametrize("message", [
    "ERROR: [PornHub] sample: No video formats found!",
    "ERROR: [PornHub] sample: Unable to extract video URL",
])
def test_pornhub_runtime_extraction_handles_current_extractor_failures(monkeypatch, message):
    import subprocess

    item = video("PornHub")
    monkeypatch.setattr(download.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a[0], 1, "", message))
    calls = []
    monkeypatch.setattr(download, "_resolve_pornhub_browser_urls",
                        lambda v: (calls.append(v) or ["https://media.test/720.mp4"], None))

    urls, error = download._resolve_direct_download_urls(item, ["yt-dlp"], force_generic=False)

    assert urls == ["https://media.test/720.mp4"]
    assert error is None
    assert calls == [item]


def test_aria2_accelerates_only_http_transfers_when_available(monkeypatch, tmp_path):
    monkeypatch.setattr(download.shutil, "which", lambda name: "/usr/bin/aria2c" if name == "aria2c" else None)
    cmd = download._build_ytdlp_download_cmd(
        ["yt-dlp"], DownloadJob(video()), tmp_path, "%(id)s.%(ext)s", download.YTDLP_FORMAT, 4,
    )

    from yt_dlp import parse_options
    assert parse_options(cmd[1:]).ydl_opts["external_downloader"] == {"http": "/usr/bin/aria2c"}
    args = cmd[cmd.index("--downloader-args") + 1]
    assert "-x4" in args and "-s4" in args
    assert "--file-allocation=none" in args
    assert "--socket-timeout" in cmd


def test_aria2_progress_is_visible_to_the_ui(monkeypatch, tmp_path):
    class Process:
        stdout = iter(["[#abcd 2MiB/8MiB(25%) CN:4 DL:2MiB ETA:3s]\n"])
        returncode = 0
        def wait(self): return 0

    monkeypatch.setattr(download.subprocess, "Popen", lambda *a, **kw: Process())
    job = DownloadJob(video())
    updates = []
    download._run_download_process(job, ["aria2c"], lambda: updates.append(job.progress))

    assert job.progress == 25
    assert job.speed == "2MiB/s"
    assert job.eta == "3s"
    assert 25 in updates


def test_pornhub_html_parser_handles_multiline_player_data():
    markup = '''<script>var flashvars_42 = {
        "mediaDefinitions": [
            {"quality":"1080", "videoUrl":"https://cdn.test/high.mp4"},
            {"quality":"720", "videoUrl":"https://cdn.test/low.mp4"}
        ]
    };</script>'''
    assert download._pornhub_media_urls_from_html(markup) == [
        (1080, "https://cdn.test/high.mp4"), (720, "https://cdn.test/low.mp4"),
    ]


def test_browser_fallback_reads_evaluated_player_data(monkeypatch):
    from selenium import webdriver

    class Browser:
        page_source = "<html>player data is created by JavaScript</html>"
        closed = False
        def set_page_load_timeout(self, seconds): pass
        def set_script_timeout(self, seconds): pass
        def get(self, url): pass
        def execute_async_script(self, script):
            return [{"quality": "1080", "videoUrl": "https://cdn.test/full.mp4"}]
        def quit(self): self.closed = True

    browser = Browser()
    monkeypatch.setattr(webdriver, "Chrome", lambda **kw: browser)
    urls, error = download._resolve_pornhub_browser_urls(video("PornHub"))
    assert urls == ["https://cdn.test/full.mp4"]
    assert error is None
    assert browser.closed


@pytest.mark.parametrize("data", ["null", "[]", "42", '"bad"'])
def test_player_parser_ignores_non_object_values(data):
    assert download._pornhub_media_urls_from_html(f"var flashvars_42 = {data};") == []


def test_runtime_urls_reject_non_http_and_blurred_media():
    definitions = [
        {"videoUrl": "javascript:alert(1)"},
        {"videoUrl": "https://cdn.test/blurred/preview.mp4", "quality": "1080"},
        {"videoUrl": "https://cdn.test/full.mp4", "quality": "720"},
        {"videoUrl": "https://cdn.test/full.mp4", "quality": "720"},
        {"videoUrl": "https://cdn.test/video/get_media?key=abc"},
    ]
    assert download._media_urls_from_definitions(definitions) == [(720, "https://cdn.test/full.mp4")]


def test_runtime_stream_is_tried_before_repeating_failed_site_extraction(monkeypatch, tmp_path):
    job = DownloadJob(video("PornHub"))
    urls = []
    monkeypatch.setattr(download, "resolve_ytdlp_command", lambda: (["yt-dlp"], None))
    monkeypatch.setattr(download, "_resolve_pornhub_browser_urls",
                        lambda v: (["https://cdn.test/full.mp4"], None))

    def run(job, cmd, progress, **kwargs):
        urls.append(cmd[-1])
        if cmd[-1] == "https://cdn.test/full.mp4":
            assert cmd[cmd.index("--referer") + 1] == job.video.url
            return 0, tmp_path / "saved.mp4", deque()
        return 1, None, deque(["ERROR: [PornHub] sample: No video formats found!"])

    def finalize(job, *args):
        job.status = "done"
        return True

    monkeypatch.setattr(download, "_run_download_process", run)
    monkeypatch.setattr(download, "_finalize_downloaded_file", finalize)
    download.run_download(job, tmp_path / "tmp", tmp_path / "out", lambda: None, lambda j: None)
    assert urls == [job.video.url, "https://cdn.test/full.mp4"]
    assert job.status == "done"


def test_rejected_low_quality_file_is_not_downloaded_repeatedly(monkeypatch, tmp_path):
    job = DownloadJob(video("Test"))
    attempts = []
    def run(job, cmd, progress, **kwargs):
        attempts.append(cmd)
        return 0, tmp_path / "low.mp4", deque()
    def reject(job, *args):
        job.status = "failed"
        job.eta = "downloaded format is 480p; Fetcher requires 720p+"
        return False
    monkeypatch.setattr(download, "resolve_ytdlp_command", lambda: (["yt-dlp"], None))
    monkeypatch.setattr(download, "_run_download_process", run)
    monkeypatch.setattr(download, "_finalize_downloaded_file", reject)
    monkeypatch.setattr(download, "_resolve_direct_download_urls", lambda *a, **kw: ([], "no urls"))
    monkeypatch.setattr(download, "ytdlp_version_warning", lambda: None)
    download.run_download(job, tmp_path / "tmp", tmp_path / "out", lambda: None, lambda j: None)
    assert len(attempts) == 1
    assert "480p" in job.eta
