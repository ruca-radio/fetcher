import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fetcher_core.download
import fetchtui
from fetcher_core.jobs import DownloadManager
from rich.console import Console

from fetchtui import AppSettings, FileEntry, Video


class FetcherRuntimeTests(unittest.TestCase):
    def test_file_preview_text_treats_bracketed_video_ids_as_plain_text(self) -> None:
        entry = FileEntry(
            path=Path("/tmp/I DIDN'T DO MY HOMEWORK [xhTX7Bq-1].mp4"),
            size=90197982,
            modified_ts=1782658984.5883265,
        )

        preview = fetchtui._file_preview_text(entry)
        console = Console()
        for span in preview.spans:
            if isinstance(span.style, str):
                console.get_style(span.style)

        self.assertIn("[xhTX7Bq-1]", preview.plain)

    def test_pornhub_direct_url_resolution_falls_back_to_browser_on_phantomjs_error(self) -> None:
        video = Video(
            id="sample",
            title="Sample",
            duration_s=0,
            views=0,
            url="https://www.pornhub.com/view_video.php?viewkey=sample",
            site="PornHub",
        )
        proc = subprocess.CompletedProcess(
            ["yt-dlp"],
            1,
            stdout="",
            stderr="ERROR: [PornHub] sample: PhantomJS not found, Please download it from https://phantomjs.org/download.html",
        )

        with (
            patch.object(fetcher_core.download.subprocess, "run", return_value=proc),
            patch.object(
                fetcher_core.download,
                "_resolve_pornhub_browser_urls",
                return_value=(["https://media.example/video.m3u8"], None),
            ) as browser_resolver,
        ):
            urls, error = fetcher_core.download._resolve_direct_download_urls(video, ["yt-dlp"], force_generic=False)

        self.assertEqual(urls, ["https://media.example/video.m3u8"])
        self.assertIsNone(error)
        browser_resolver.assert_called_once_with(video)

    def test_download_manager_marks_worker_exceptions_failed(self) -> None:
        def explode(job, progress_cb, done_cb):
            raise RuntimeError("boom")

        manager = DownloadManager(explode, max_workers=1)
        self.addCleanup(manager.close, wait=False, cancel_futures=True)
        manager.submit(
            Video(
                id="sample",
                title="Sample",
                duration_s=0,
                views=0,
                url="https://example.invalid/video",
                site="PornHub",
            )
        )
        manager.wait()

        snapshot = manager.get("sample")
        self.assertEqual(snapshot.status, "failed")
        self.assertIn("boom", snapshot.message)
        self.assertFalse(snapshot.worker_started)

    def test_tui_download_manager_adapter_preserves_run_download_callbacks(self) -> None:
        settings = AppSettings(naming_template="%(id)s.%(ext)s", organize_by_site=True, concurrent_fragments=7)
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as out:
            manager = fetchtui.create_download_manager(settings, Path(tmp), Path(out))
            self.addCleanup(manager.close, wait=False, cancel_futures=True)

            def fake_run(job, tmp_dir, output_dir, progress_cb, done_cb, template, organize, fragments, policy):
                self.assertEqual(tmp_dir, Path(tmp))
                self.assertEqual(output_dir, Path(out))
                self.assertEqual((template, organize, fragments), ("%(id)s.%(ext)s", True, 7))
                self.assertIsNotNone(policy)
                job.status = "done"
                progress_cb()
                done_cb(job)

            with patch.object(fetcher_core.download, "run_download", side_effect=fake_run):
                manager.submit(Video("adapter", "Adapter", 0, 0, "https://example.test/a", "Test"))
                manager.wait()

        self.assertEqual(manager.get("adapter").status, "done")


if __name__ == "__main__":
    unittest.main()
