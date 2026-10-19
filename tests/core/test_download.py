from __future__ import annotations

import json
import subprocess
from collections import deque
from io import StringIO
from pathlib import Path
import sys

import pytest

from fetcher_core import download
from fetcher_core.models import DownloadJob, DownloadPolicy, Video


def video(site: str = "Eporner", identifier: str = "abc123") -> Video:
    return Video(identifier, "A / Bad: Title?!", 120, 0, f"https://example.test/watch/{identifier}", site)


def test_build_ytdlp_download_cmd_golden(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(download.shutil, "which", lambda name: None)
    monkeypatch.setattr(download.sys, "executable", "/nonexistent/python")
    # Keep the Twitter cookie helper on its chrome fallback; the real state
    # dir may hold a cookie file on this machine.
    monkeypatch.setattr(download, "TWITTER_COOKIES_JSON", tmp_path / "twitter_cookies.json")
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
        *download._impersonation_args(),
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


def test_browser_probe_cancel_does_not_start_flex_transfer(monkeypatch, tmp_path):
    job = DownloadJob(video("RedTube", "rt2"))
    commands = []
    completed = []

    def fake_process(job, cmd, progress_cb, **kwargs):
        commands.append(list(cmd))
        return 1, None, deque(["ERROR: Requested format is not available"], maxlen=12)

    def cancel_during_probe(url, item):
        job.status = "cancelled"
        return None

    monkeypatch.setattr(download, "resolve_ytdlp_command", lambda: (["yt-dlp"], None))
    monkeypatch.setattr(download, "_run_download_process", fake_process)
    monkeypatch.setattr(download, "_resolve_browser_media_urls", lambda v, **kwargs: (["https://cdn.test/unknown.mp4"], None))
    monkeypatch.setattr(download, "_probe_remote_media_height", cancel_during_probe)
    monkeypatch.setattr(download, "_resolve_direct_download_urls", lambda *a, **k: ([], "no urls"))
    monkeypatch.setattr(download, "ytdlp_version_warning", lambda: None)

    download.run_download(job, tmp_path / "tmp", tmp_path / "out", lambda: None, completed.append)

    assert job.status == "cancelled"
    assert completed == [job]
    assert len(commands) == 1  # cancelled probe must not spawn the flex direct transfer
    assert "height>=720" in commands[0][commands[0].index("-f") + 1]


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
    monkeypatch.setattr(download, "_resolve_direct_download_urls", lambda video, cmd, *, force_generic, min_height=0: ([], "Unsupported URL"))
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
    monkeypatch.setattr(download, "_resolve_direct_download_urls", lambda video, cmd, *, force_generic, min_height=0: ([], "no urls"))
    monkeypatch.setattr(download, "ytdlp_version_warning", lambda: None)

    download.run_download(
        job, tmp_path / "tmp", tmp_path / "out", lambda: None, lambda job: None,
        policy=DownloadPolicy(min_height=1080),
    )

    assert "height>=1080" in selectors[0]
    assert "height>=1080" in selectors[1]
    assert download.YTDLP_FLEX_FORMAT not in selectors
    assert job.status == "failed"


def test_run_download_allows_flexible_selectors_without_a_quality_floor(monkeypatch, tmp_path: Path) -> None:
    job = DownloadJob(video("Eporner", "ep0"))
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
    monkeypatch.setattr(download, "_resolve_direct_download_urls", lambda video, cmd, *, force_generic, min_height=0: ([], "no urls"))
    monkeypatch.setattr(download, "ytdlp_version_warning", lambda: None)

    download.run_download(
        job, tmp_path / "tmp", tmp_path / "out", lambda: None, lambda job: None,
        policy=DownloadPolicy(min_height=0),
    )

    assert selectors[0] == download.YTDLP_FLEX_FORMAT
    assert selectors[1] == download.YTDLP_FLEX_FORMAT
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
    monkeypatch.setattr(download, "_resolve_browser_media_urls",
                        lambda v, **kwargs: (calls.append(v) or ["https://media.test/720.mp4"], None))

    urls, error = download._resolve_direct_download_urls(item, ["yt-dlp"], force_generic=False)

    assert urls == ["https://media.test/720.mp4"]
    assert error is None
    assert calls == [item]


def test_is_pornhub_download_matches_site_and_url() -> None:
    assert download._is_pornhub_download(video("PornHub")) is True
    arbitrary = Video("x", "t", 0, 0, "https://www.pornhub.com/view_video.php?viewkey=x", "SearXNG")
    assert download._is_pornhub_download(arbitrary) is True
    assert download._is_pornhub_download(video("Eporner")) is False


def test_ffmpeg_fallback_sends_pornhub_age_cookies(monkeypatch, tmp_path):
    captured = {}

    def fake_run(job, cmd, progress_cb, *, expected_dest=None):
        captured["cmd"] = list(cmd)
        return 0, expected_dest, deque()

    monkeypatch.setattr(download, "FFMPEG", "/usr/bin/ffmpeg")
    monkeypatch.setattr(download, "_run_download_process", fake_run)
    job = DownloadJob(video("PornHub", "ph1"))

    download._run_ffmpeg_fallback(job, ["https://cdn.test/a.mp4"], tmp_path, lambda: None)

    cmd = captured["cmd"]
    assert cmd[cmd.index("-cookies") + 1] == download.PORNHUB_COOKIE_HEADER


def test_ffmpeg_fallback_omits_age_cookies_for_other_sites(monkeypatch, tmp_path):
    captured = {}

    def fake_run(job, cmd, progress_cb, *, expected_dest=None):
        captured["cmd"] = list(cmd)
        return 0, expected_dest, deque()

    monkeypatch.setattr(download, "FFMPEG", "/usr/bin/ffmpeg")
    monkeypatch.setattr(download, "_run_download_process", fake_run)

    download._run_ffmpeg_fallback(DownloadJob(video("Eporner", "e1")), ["https://cdn.test/a.mp4"], tmp_path, lambda: None)

    assert "-cookies" not in captured["cmd"]


def test_pornhub_browser_fallback_reports_age_gate_instead_of_no_media(monkeypatch):
    from selenium import webdriver

    class Browser:
        page_source = '<body class="sfw-page logged-out"><div id="ageDisclaimerMainBG"></div></body>'

        def set_page_load_timeout(self, seconds): pass
        def set_script_timeout(self, seconds): pass
        def get(self, url): pass
        def add_cookie(self, cookie): pass
        def find_elements(self, *args): return []
        def execute_async_script(self, script): return []
        def quit(self): pass

    monkeypatch.setattr(webdriver, "Chrome", lambda **kwargs: Browser())

    urls, error = download._resolve_pornhub_browser_urls(video("PornHub"), min_height=720)

    assert urls == []
    assert "18+" in (error or "")
    assert "SFW" in (error or "")


def test_probe_remote_media_height_parses_stream_height(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(cmd, 0, '{"streams":[{"height":1080}]}', "")

    monkeypatch.setattr(download.subprocess, "run", fake_run)

    assert download._probe_remote_media_height("https://cdn.test/a.m3u8", video("PornHub")) == 1080
    assert "-cookies" in captured["cmd"]
    assert download._probe_remote_media_height("https://cdn.test/a.mp4", video("Eporner")) == 1080
    assert "-cookies" not in captured["cmd"]


@pytest.mark.parametrize("returncode,stdout,expected", [
    (1, "", None),
    (0, "not json", None),
    (0, '{"streams":[]}', None),
    (0, '{"streams":[{"height":720}]}', 720),
])
def test_probe_remote_media_height_tolerates_bad_metadata(monkeypatch, returncode, stdout, expected):
    monkeypatch.setattr(
        download.subprocess, "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, returncode, stdout, ""),
    )
    assert download._probe_remote_media_height("https://cdn.test/a.mp4", video()) == expected


def test_resolved_stream_below_floor_only_rejects_known_low_height(monkeypatch):
    monkeypatch.setattr(download, "_probe_remote_media_height", lambda url, video: 240)
    assert "240p" in (download._resolved_stream_below_floor("https://cdn.test/a.mp4", video(), 720) or "")
    assert download._resolved_stream_below_floor("https://cdn.test/a.mp4", video(), 0) is None

    monkeypatch.setattr(download, "_probe_remote_media_height", lambda url, video: 1080)
    assert download._resolved_stream_below_floor("https://cdn.test/a.mp4", video(), 720) is None

    monkeypatch.setattr(download, "_probe_remote_media_height", lambda url, video: None)
    assert download._resolved_stream_below_floor("https://cdn.test/a.mp4", video(), 720) is None


def test_ffmpeg_fallback_rejects_known_subfloor_resolved_stream(monkeypatch, tmp_path):
    monkeypatch.setattr(download, "FFMPEG", "/usr/bin/ffmpeg")
    monkeypatch.setattr(download, "_probe_remote_media_height", lambda url, video: 240)

    def unexpected(*args, **kwargs):
        raise AssertionError("sub-floor ffmpeg transfer must not start")

    monkeypatch.setattr(download, "_run_download_process", unexpected)

    returncode, dest, tail = download._run_ffmpeg_fallback(
        DownloadJob(video("PornHub", "ph1")),
        ["https://cdn.test/low.mp4"],
        tmp_path,
        lambda: None,
        min_height=720,
    )

    assert returncode != 0
    assert dest is None
    assert "240p" in " ".join(tail)
    assert "720p" in " ".join(tail)


def test_ffmpeg_fallback_allows_unknown_or_eligible_resolved_stream(monkeypatch, tmp_path):
    monkeypatch.setattr(download, "FFMPEG", "/usr/bin/ffmpeg")
    calls = []

    def fake_process(job, cmd, progress_cb, *, expected_dest=None):
        calls.append(cmd)
        return 0, expected_dest, deque()

    monkeypatch.setattr(download, "_run_download_process", fake_process)

    for probed in (None, 1080):
        calls.clear()
        monkeypatch.setattr(download, "_probe_remote_media_height", lambda url, video, _h=probed: _h)
        returncode, _, _ = download._run_ffmpeg_fallback(
            DownloadJob(video("Eporner", "e1")),
            ["https://cdn.test/high.mp4"],
            tmp_path,
            lambda: None,
            min_height=720,
        )
        assert returncode == 0
        assert len(calls) == 1


def test_browser_resolved_subfloor_stream_fails_before_transfer(monkeypatch, tmp_path):
    job = DownloadJob(video("RedTube", "rt1"))
    commands = []
    monkeypatch.setattr(download, "resolve_ytdlp_command", lambda: (["yt-dlp"], None))
    monkeypatch.setattr(
        download, "_run_download_process",
        lambda job, cmd, progress_cb, **kwargs: (commands.append(list(cmd)) or (1, None, deque(["ERROR: Requested format is not available"], maxlen=12))),
    )
    monkeypatch.setattr(download, "_resolve_browser_media_urls", lambda v, **kwargs: (["https://cdn.test/preview.mp4"], None))
    monkeypatch.setattr(download, "_probe_remote_media_height", lambda url, video: 240)
    monkeypatch.setattr(download, "_resolve_direct_download_urls", lambda *args, **kwargs: ([], "no urls"))
    monkeypatch.setattr(download, "ytdlp_version_warning", lambda: None)

    download.run_download(job, tmp_path / "tmp", tmp_path / "out", lambda: None, lambda j: None)

    assert job.status == "failed"
    assert "240p" in job.eta and "720p" in job.eta
    assert len(commands) == 2  # only the two constrained yt-dlp attempts
    assert all("height>=720" in cmd[cmd.index("-f") + 1] for cmd in commands)
    assert all("--add-header" not in cmd for cmd in commands)  # no low-quality direct transfer


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
        stdout = StringIO("[#abcd 2MiB/8MiB(25%) CN:4 DL:2MiB ETA:3s]\n")
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


@pytest.mark.parametrize("percent", ["100", "100.0"])
def test_completed_transfer_clears_stale_speed_and_eta(monkeypatch, percent):
    class Process:
        stdout = StringIO(
            "[download] 25.0% of 8MiB at 2MiB/s ETA 00:03\n"
            f"[download] {percent}% of 8MiB in 00:04\n"
        )
        returncode = 0

        def wait(self):
            return 0

    monkeypatch.setattr(download.subprocess, "Popen", lambda *a, **kw: Process())
    job = DownloadJob(video())
    updates = []
    download._run_download_process(job, ["yt-dlp"],
                                   lambda: updates.append((job.progress, job.speed, job.eta)))

    assert updates[-1] == (100.0, "", "")


@pytest.mark.parametrize("fail_on_update", [1, 2])
def test_callback_failure_terminates_download_process(fail_on_update):
    job = DownloadJob(video())
    processes = []
    updates = 0

    def progress():
        nonlocal updates
        updates += 1
        processes.append(job.proc)
        if updates == fail_on_update:
            raise RuntimeError("progress callback failed")

    command = [sys.executable, "-u", "-c",
               "import time; print('[download] 25% of 8MiB at 2MiB/s ETA 00:03'); time.sleep(30)"]
    try:
        with pytest.raises(RuntimeError, match="progress callback failed"):
            download._run_download_process(job, command, progress)
        assert processes[0].poll() is not None
        assert processes[0].stdout.closed
        assert job.proc is None
    finally:
        if processes:
            download.terminate_process_group(processes[0])
            processes[0].stdout.close()


@pytest.mark.parametrize("identifier", [".", ".."])
def test_job_temp_directory_stays_below_staging_root(tmp_path, identifier):
    destination = download._job_temp_dir(tmp_path, video(identifier=identifier))

    assert destination.resolve().is_relative_to(tmp_path.resolve())
    assert destination.resolve() != tmp_path.resolve()


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


def _browser_with_media(monkeypatch, definitions):
    from selenium import webdriver

    class Browser:
        page_source = ""

        def set_page_load_timeout(self, seconds): pass
        def set_script_timeout(self, seconds): pass
        def get(self, url): pass
        def execute_async_script(self, script): return definitions
        def quit(self): pass

    monkeypatch.setattr(webdriver, "Chrome", lambda **kwargs: Browser())


@pytest.mark.parametrize(
    ("definitions", "min_height", "expected", "error_fragment"),
    [
        (
            [
                {"quality": "1080", "videoUrl": "https://cdn.test/high.mp4"},
                {"quality": "720", "videoUrl": "https://cdn.test/mid.mp4"},
                {"quality": "480", "videoUrl": "https://cdn.test/low.mp4"},
            ],
            720,
            ["https://cdn.test/high.mp4"],
            None,
        ),
        ([{"quality": "480", "videoUrl": "https://cdn.test/low.mp4"}], 720, [], "720p"),
        (
            [
                {"videoUrl": "https://cdn.test/unknown.mp4"},
                {"quality": "480", "videoUrl": "https://cdn.test/low.mp4"},
            ],
            720,
            ["https://cdn.test/unknown.mp4"],
            None,
        ),
        (
            [
                {"quality": "1080", "videoUrl": "https://cdn.test/high.mp4"},
                {"quality": "240", "videoUrl": "https://cdn.test/low.mp4"},
            ],
            0,
            ["https://cdn.test/high.mp4"],
            None,
        ),
    ],
)
def test_browser_fallback_enforces_configured_quality_floor(
    monkeypatch, definitions, min_height, expected, error_fragment,
):
    _browser_with_media(monkeypatch, definitions)

    urls, error = download._resolve_pornhub_browser_urls(video("PornHub"), min_height=min_height)

    assert urls == expected
    if error_fragment:
        assert error_fragment in (error or "")
    else:
        assert error is None


def test_browser_fallback_reports_best_available_height_when_below_floor(monkeypatch):
    _browser_with_media(monkeypatch, [{"quality": "240", "videoUrl": "https://cdn.test/low.mp4"}])

    urls, error = download._resolve_pornhub_browser_urls(video("PornHub"), min_height=720)

    assert urls == []
    assert "240p" in (error or "")
    assert "720p" in (error or "")


def test_runtime_urls_reject_non_http_and_blurred_media():
    definitions = [
        {"videoUrl": "javascript:alert(1)"},
        {"videoUrl": "https://cdn.test/blurred/preview.mp4", "quality": "1080"},
        {"videoUrl": "https://cdn.test/full.mp4", "quality": "720"},
        {"videoUrl": "https://cdn.test/full.mp4", "quality": "720"},
        {"videoUrl": "https://cdn.test/video/get_media?key=abc"},
    ]
    assert download._media_urls_from_definitions(definitions) == [(720, "https://cdn.test/full.mp4")]


def test_age_wall_page_detection_is_specific() -> None:
    from fetcher_core import age_gate

    assert age_gate.is_age_wall_page('<video class="age-wall-video" src="promo.mp4"></video>')
    assert age_gate.is_age_wall_page('<div id="ageWall">You must be 18</div>')
    assert not age_gate.is_age_wall_page('<video class="video-player" src="clip.mp4"></video>')
    assert "age-verification wall" in age_gate.age_wall_message("RedTube")


def test_age_wall_message_names_the_restricted_region() -> None:
    from fetcher_core import age_gate

    page = (
        "<p>your elected officials in Missouri are requiring us to verify your age before "
        "allowing you access to our website.</p>"
        "<p>we have made the difficult decision to completely disable access to our website in Missouri.</p>"
    )

    assert age_gate.age_wall_region(page) == "Missouri"
    assert age_gate.age_wall_region(
        "elected officials in Washington, D.C. are requiring us to verify your age"
    ) == "Washington, D.C."
    message = age_gate.age_wall_message("RedTube", age_gate.age_wall_region(page))
    assert "Missouri" in message
    assert "age-verification wall" in message


def _age_wall_browser(monkeypatch, *, page_source: str, current_url: str, media=None):
    from selenium import webdriver

    class Browser:
        def __init__(self):
            self.page_source = page_source
            self.current_url = current_url

        def set_page_load_timeout(self, seconds): pass
        def set_script_timeout(self, seconds): pass
        def get(self, url): pass
        def execute_async_script(self, script): return media or []
        def quit(self): pass

    browser = Browser()
    monkeypatch.setattr(webdriver, "Chrome", lambda **kwargs: browser)
    return browser


def test_browser_fallback_reports_age_wall_preview_instead_of_downloading_it(monkeypatch):
    _age_wall_browser(
        monkeypatch,
        page_source='<video class="age-wall-video" src="https://cdn.test/promo.mp4"></video>',
        current_url="https://www.redtube.com/",
        media=[{"quality": "240", "videoUrl": "https://cdn.test/promo.mp4"}],
    )
    item = Video("190684621", "HotwifeXXX", 829, 0, "https://www.redtube.com/190684621", "RedTube")

    urls, error = download._resolve_pornhub_browser_urls(item, min_height=720)

    assert urls == []
    assert "age-verification wall" in (error or "")
    assert "preview" in (error or "")
    assert "240p" not in (error or "")


def test_browser_fallback_reports_homepage_redirect_as_age_wall(monkeypatch):
    _age_wall_browser(
        monkeypatch,
        page_source="<html>Redtube homepage</html>",
        current_url="https://www.redtube.com/",
        media=[{"quality": "1080", "videoUrl": "https://cdn.test/unrelated.mp4"}],
    )
    item = Video("190684621", "HotwifeXXX", 829, 0, "https://www.redtube.com/190684621", "RedTube")

    urls, error = download._resolve_pornhub_browser_urls(item, min_height=720)

    assert urls == []
    assert "age-verification wall" in (error or "")


def test_browser_fallback_does_not_flag_preserved_video_page(monkeypatch):
    _age_wall_browser(
        monkeypatch,
        page_source="<html>real video page</html>",
        current_url="https://www.redtube.com/190684621",
        media=[{"quality": "1080", "videoUrl": "https://cdn.test/full.mp4"}],
    )
    item = Video("190684621", "HotwifeXXX", 829, 0, "https://www.redtube.com/190684621", "RedTube")

    urls, error = download._resolve_pornhub_browser_urls(item, min_height=720)

    assert urls == ["https://cdn.test/full.mp4"]
    assert error is None


def test_runtime_stream_is_tried_before_repeating_failed_site_extraction(monkeypatch, tmp_path):
    job = DownloadJob(video("PornHub"))
    urls = []
    commands = []
    monkeypatch.setattr(download, "resolve_ytdlp_command", lambda: (["yt-dlp"], None))
    monkeypatch.setattr(download, "_resolve_browser_media_urls",
                        lambda v, **kwargs: (["https://cdn.test/full.mp4"], None))
    monkeypatch.setattr(download, "_probe_remote_media_height", lambda url, video: 1080)

    def run(job, cmd, progress, **kwargs):
        commands.append(list(cmd))
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
    direct = commands[-1]
    assert direct[direct.index("--add-header") + 1] == f"Cookie: {download.PORNHUB_COOKIE_HEADER}"
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


def test_impersonation_args_follow_curl_cffi_availability(monkeypatch) -> None:
    monkeypatch.setattr(download, "_CURL_CFFI_AVAILABLE", True)
    assert download._impersonation_args() == ["--impersonate", "chrome"]
    monkeypatch.setattr(download, "_CURL_CFFI_AVAILABLE", False)
    assert download._impersonation_args() == []


def test_build_ytdlp_download_cmd_adds_impersonation_only_when_available(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(download.shutil, "which", lambda name: None)
    monkeypatch.setattr(download, "_CURL_CFFI_AVAILABLE", True)
    cmd = download._build_ytdlp_download_cmd(
        ["yt-dlp"], DownloadJob(video()), tmp_path, "%(id)s.%(ext)s", download.YTDLP_FORMAT, 1,
    )
    assert cmd[cmd.index("--impersonate") + 1] == "chrome"

    monkeypatch.setattr(download, "_CURL_CFFI_AVAILABLE", False)
    cmd = download._build_ytdlp_download_cmd(
        ["yt-dlp"], DownloadJob(video()), tmp_path, "%(id)s.%(ext)s", download.YTDLP_FORMAT, 1,
    )
    assert "--impersonate" not in cmd


def test_twitter_cookie_args_writes_netscape_file_from_stored_json(monkeypatch, tmp_path) -> None:
    json_path = tmp_path / "twitter_cookies.json"
    net_path = tmp_path / "twitter_cookies.net.txt"
    monkeypatch.setattr(download, "TWITTER_COOKIES_JSON", json_path)
    monkeypatch.setattr(download, "TWITTER_COOKIES_NETSCAPE", net_path)
    json_path.write_text(json.dumps({"auth_token": "sekret", "ct0": "csrf", "twid": "u1"}), encoding="utf-8")

    assert download._twitter_cookie_args() == ["--cookies", str(net_path)]

    text = net_path.read_text(encoding="utf-8")
    assert text.startswith("# Netscape HTTP Cookie File")
    assert ".x.com\tTRUE\t/\tTRUE\t2147483647\tauth_token\tsekret" in text
    assert ".x.com\tTRUE\t/\tTRUE\t2147483647\tct0\tcsrf" in text
    assert ".x.com\tTRUE\t/\tTRUE\t2147483647\ttwid\tu1" in text


@pytest.mark.parametrize("payload", [None, "{}", '{"ct0": "only"}', '{"auth_token": ""}', "not json"])
def test_twitter_cookie_args_falls_back_to_chrome(monkeypatch, tmp_path, payload) -> None:
    json_path = tmp_path / "twitter_cookies.json"
    monkeypatch.setattr(download, "TWITTER_COOKIES_JSON", json_path)
    monkeypatch.setattr(download, "TWITTER_COOKIES_NETSCAPE", tmp_path / "twitter_cookies.net.txt")
    if payload is not None:
        json_path.write_text(payload, encoding="utf-8")

    assert download._twitter_cookie_args() == ["--cookies-from-browser", "chrome"]
    assert not (tmp_path / "twitter_cookies.net.txt").exists()


def test_resolve_stream_urls_uses_cookie_file_and_impersonation(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(download, "TWITTER_COOKIES_JSON", tmp_path / "twitter_cookies.json")
    monkeypatch.setattr(download, "TWITTER_COOKIES_NETSCAPE", tmp_path / "twitter_cookies.net.txt")
    (tmp_path / "twitter_cookies.json").write_text(json.dumps({"auth_token": "sekret"}), encoding="utf-8")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "https://media.test/x.m3u8\n", "")

    monkeypatch.setattr(download, "resolve_ytdlp_command", lambda: (["yt-dlp"], None))
    monkeypatch.setattr(download.subprocess, "run", fake_run)

    urls, error = download.resolve_stream_urls(video("Twitter", "tw9"))

    assert urls == ["https://media.test/x.m3u8"]
    assert error is None
    cmd = captured["cmd"]
    assert cmd[cmd.index("-f") + 1] == download.YTDLP_FLEX_FORMAT
    assert cmd[cmd.index("--cookies") + 1] == str(tmp_path / "twitter_cookies.net.txt")
    if download._CURL_CFFI_AVAILABLE:
        assert cmd[cmd.index("--impersonate") + 1] == "chrome"


def test_resolve_stream_urls_ignores_the_download_quality_floor(monkeypatch) -> None:
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(cmd, 0, "https://media.test/480.mp4\n", "")

    monkeypatch.setattr(download, "resolve_ytdlp_command", lambda: (["yt-dlp"], None))
    monkeypatch.setattr(download.subprocess, "run", fake_run)

    urls, error = download.resolve_stream_urls(video("Eporner", "e480"))

    assert urls == ["https://media.test/480.mp4"]
    assert error is None
    selector = captured["cmd"][captured["cmd"].index("-f") + 1]
    assert selector == download.YTDLP_FLEX_FORMAT
    assert "height>=" not in selector


def test_resolve_direct_download_urls_adds_impersonation(monkeypatch) -> None:
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(cmd, 0, "https://media.test/a.mp4\n", "")

    monkeypatch.setattr(download.subprocess, "run", fake_run)
    urls, error = download._resolve_direct_download_urls(video("Eporner", "e1"), ["yt-dlp"], force_generic=False)

    assert urls == ["https://media.test/a.mp4"]
    assert error is None
    if download._CURL_CFFI_AVAILABLE:
        assert captured["cmd"][captured["cmd"].index("--impersonate") + 1] == "chrome"


@pytest.mark.parametrize(("min_height", "expected_fragment"), [
    (720, "height>=720"),
    (1080, "height>=1080"),
    (0, None),
])
def test_direct_download_urls_respect_quality_floor(monkeypatch, min_height, expected_fragment):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(cmd, 0, "https://media.test/a.mp4\n", "")

    monkeypatch.setattr(download.subprocess, "run", fake_run)

    urls, error = download._resolve_direct_download_urls(
        video("Eporner", "e1"), ["yt-dlp"], force_generic=False, min_height=min_height,
    )

    assert urls == ["https://media.test/a.mp4"]
    assert error is None
    selector = captured["cmd"][captured["cmd"].index("-f") + 1]
    if expected_fragment is None:
        assert selector == download.YTDLP_FLEX_FORMAT
    else:
        assert expected_fragment in selector


def test_best_download_failure_prefers_age_wall_explanation() -> None:
    from fetcher_core import age_gate

    wall = age_gate.age_wall_message("RedTube")
    failures = ["ERROR: [RedTube] sample: Unable to extract video URL", wall]

    assert download._best_download_failure(failures, "ERROR: later generic failure") == wall


@pytest.mark.parametrize("site", ["PornHub", "RedTube", "xHamster", "SomeArbitraryTube"])
@pytest.mark.parametrize("message", [
    "ERROR: No video formats found!",
    "ERROR: Unable to extract video URL",
    "ERROR: Unsupported URL",
    "ERROR: [RedTube] sample: Requested format is not available. Use --list-formats for a list of available formats",
    "phantomjs is required",
    "this video is DRM protected",
    "failed to load HLS manifest.m3u8",
])
def test_needs_browser_fallback_applies_to_any_site(site, message) -> None:
    assert download._needs_browser_fallback(video(site), message)


def test_needs_browser_fallback_ignores_unrelated_errors() -> None:
    assert not download._needs_browser_fallback(video("PornHub"), "HTTP Error 404: Not Found")


def test_browser_media_resolver_alias_delegates_for_backward_compat(monkeypatch) -> None:
    sentinel = (["https://x.test/a.mp4"], None)
    monkeypatch.setattr(download, "_resolve_browser_media_urls", lambda v, **kwargs: sentinel)
    assert download._resolve_pornhub_browser_urls(object()) == sentinel


def test_browser_media_script_scans_sources_and_stream_urls() -> None:
    script = download._BROWSER_MEDIA_SCRIPT
    assert "document.querySelectorAll('video, video source, source')" in script
    assert r"\.m3u8" in script
    assert r"\.mp4" in script
    assert ".slice(0, 20)" in script  # bounded scan


def test_pornhub_media_urls_from_html_survives_truncated_player_data() -> None:
    assert download._pornhub_media_urls_from_html("var flashvars_42 = {") == []
    assert download._pornhub_media_urls_from_html("var flashvars_42 = ") == []


def test_run_download_o_flag_last_token_does_not_crash_progress_dir(monkeypatch) -> None:
    class Process:
        stdout = StringIO("[download] 50% of 4MiB at 1MiB/s ETA 00:02\n")
        returncode = 0
        def wait(self): return 0

    monkeypatch.setattr(download.subprocess, "Popen", lambda *a, **kw: Process())
    job = DownloadJob(video())
    returncode, dest, tail = download._run_download_process(job, ["yt-dlp", "-o"], lambda: None)
    assert returncode == 0
    assert job.progress == 50.0
