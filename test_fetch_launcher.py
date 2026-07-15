import subprocess
import unittest
from pathlib import Path


class FetchLauncherTests(unittest.TestCase):
    def test_fetch_help_uses_repo_virtualenv_when_present(self) -> None:
        repo = Path(__file__).resolve().parent
        result = subprocess.run(
            [str(repo / "fetch"), "--help"],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Fetcher v1.0 GUI, CLI, and TUI", result.stdout)

    def test_fetch_gui_launcher_is_executable(self) -> None:
        repo = Path(__file__).resolve().parent
        launcher = repo / "fetch-gui"

        self.assertTrue(launcher.is_file())
        self.assertTrue(launcher.stat().st_mode & 0o111)


if __name__ == "__main__":
    unittest.main()
