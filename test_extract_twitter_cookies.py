import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import extract_twitter_cookies as extractor


class ExtractTwitterCookiesTests(unittest.TestCase):
    def test_help_exits_without_extracting_cookies(self) -> None:
        self.assertTrue(hasattr(extractor, "main"), "expected a CLI main() entry point")

        with patch.object(extractor, "extract") as extract:
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    extractor.main(["--help"])

        self.assertEqual(raised.exception.code, 0)
        extract.assert_not_called()

    def test_empty_cookie_dump_is_removed_after_failed_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cookie_path = Path(tmp) / "cookies.txt"
            mktemp_called = False

            class FakeNamedTemporaryFile:
                name = str(cookie_path)

                def __enter__(self) -> "FakeNamedTemporaryFile":
                    return self

                def __exit__(self, *_args: object) -> None:
                    return None

            def fake_mktemp(*_args: object, **_kwargs: object) -> str:
                nonlocal mktemp_called
                mktemp_called = True
                return str(cookie_path)

            def fake_run(*_args: object, **_kwargs: object) -> CompletedProcess[str]:
                cookie_path.write_text("", encoding="utf-8")
                return CompletedProcess(args=[], returncode=0, stdout="", stderr="no cookies")

            with (
                patch.object(extractor, "resolve_ytdlp_command", return_value=(["yt-dlp"], None)),
                patch.object(extractor.tempfile, "NamedTemporaryFile", return_value=FakeNamedTemporaryFile()),
                patch.object(extractor.tempfile, "mktemp", side_effect=fake_mktemp),
                patch.object(extractor.subprocess, "run", side_effect=fake_run),
                redirect_stdout(io.StringIO()),
            ):
                extractor.extract()

            self.assertFalse(mktemp_called, "mktemp should not be used for cookie extraction")
            self.assertFalse(cookie_path.exists(), "empty temporary cookie dump should be removed")


if __name__ == "__main__":
    unittest.main()
