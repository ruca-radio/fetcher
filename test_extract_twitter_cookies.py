import io
import stat
import sys
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

    @unittest.skipIf(sys.platform == "win32", "POSIX permission bits are not reliable on Windows")
    def test_successful_extract_writes_private_cookie_file_without_secret_preview(self) -> None:
        auth_value = "super-secret-auth-token-value"

        with tempfile.TemporaryDirectory() as tmp:
            temp_cookie_path = Path(tmp) / "cookies.txt"
            output_path = Path(tmp) / "state" / "twitter_cookies.json"

            class FakeNamedTemporaryFile:
                name = str(temp_cookie_path)

                def __enter__(self) -> "FakeNamedTemporaryFile":
                    return self

                def __exit__(self, *_args: object) -> None:
                    return None

            def fake_run(*_args: object, **_kwargs: object) -> CompletedProcess[str]:
                temp_cookie_path.write_text(
                    "\n".join(
                        [
                            "# Netscape HTTP Cookie File",
                            f".x.com\tTRUE\t/\tTRUE\t2147483647\tauth_token\t{auth_value}",
                            ".x.com\tTRUE\t/\tTRUE\t2147483647\tct0\tcsrf-token-value",
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return CompletedProcess(args=[], returncode=0, stdout="", stderr="")

            stdout = io.StringIO()
            with (
                patch.object(extractor, "resolve_ytdlp_command", return_value=(["yt-dlp"], None)),
                patch.object(extractor.tempfile, "NamedTemporaryFile", return_value=FakeNamedTemporaryFile()),
                patch.object(extractor.subprocess, "run", side_effect=fake_run),
                patch.object(extractor, "OUTPUT", output_path),
                redirect_stdout(stdout),
            ):
                result = extractor.extract()

            self.assertEqual(result, 0)
            self.assertEqual(stat.S_IMODE(output_path.stat().st_mode), 0o600)
            self.assertNotIn(auth_value, stdout.getvalue())
            self.assertIn("auth_token ★", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
