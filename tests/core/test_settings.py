import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from fetcher_core.models import BASE_DIR, AppSettings, DownloadPolicy, SearchFilters
from fetcher_core.settings import SettingsStore, parse_sftp_storage, prepare_runtime_settings


def test_runtime_directory_probe_preserves_existing_files(tmp_path):
    from fetcher_core.settings import resolve_runtime_dirs

    output = tmp_path / "output"
    temporary = tmp_path / "temporary"
    for directory in (output, temporary):
        directory.mkdir()
        (directory / ".fetcher-write-test").write_text("keep me")

    resolved_tmp, resolved_output, warnings = resolve_runtime_dirs(AppSettings(
        plex_dir=str(output), tmp_dir=str(temporary),
    ))

    assert (resolved_tmp, resolved_output, warnings) == (temporary, output, [])
    for directory in (output, temporary):
        assert (directory / ".fetcher-write-test").read_text() == "keep me"
        assert [path.name for path in directory.iterdir()] == [".fetcher-write-test"]


@pytest.mark.parametrize("contents", [None, "{broken", '{"parallel_downloads": "bad"}',
                                     '{"plex_dir": "sftp:invalid"}'])
def test_settings_fallback_does_not_share_mutable_filters(tmp_path, contents):
    path = tmp_path / "settings.json"
    defaults = AppSettings(search_filters=SearchFilters(pages=4))
    store = SettingsStore(path, defaults=defaults)

    if contents is not None:
        path.write_text(contents)
    loaded = store.load()
    loaded.settings.search_filters.pages = 9
    assert defaults.search_filters.pages == 4
    assert store.load().settings.search_filters.pages == 4


class SettingsStoreTests(unittest.TestCase):
    def test_new_policy_defaults_are_present(self) -> None:
        settings = AppSettings()

        self.assertTrue(settings.search_filters.exclude_censored)
        self.assertEqual(settings.min_download_height, 720)
        self.assertEqual(settings.eporner_preview_ratio, 0.6)
        self.assertEqual(settings.eporner_preview_min_seconds, 90)
        self.assertEqual(settings.download_policy(), DownloadPolicy(720, 0.6, 90))

    def test_loads_legacy_json_and_preserves_unknown_keys_on_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({
                "parallel_downloads": 6,
                "search_filters": {"pages": 4, "min_quality": "1080"},
                "future_key": {"enabled": True},
            }))
            store = SettingsStore(path, defaults=AppSettings())

            loaded = store.load()
            store.save(store.validate(loaded.settings))

            self.assertEqual(loaded.settings.parallel_downloads, 6)
            self.assertEqual(loaded.settings.search_filters.pages, 4)
            self.assertEqual(loaded.unknown, {"future_key": {"enabled": True}})
            self.assertEqual(json.loads(path.read_text())["future_key"], {"enabled": True})

    def test_download_policy_settings_round_trip_through_save_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            store = SettingsStore(path, defaults=AppSettings())
            settings = AppSettings(
                min_download_height=1080,
                eporner_preview_ratio=0.75,
                eporner_preview_min_seconds=120,
                search_filters=SearchFilters(exclude_censored=False),
            )

            store.save(settings)
            loaded = SettingsStore(path, defaults=AppSettings()).load().settings

            self.assertEqual(loaded.min_download_height, 1080)
            self.assertEqual(loaded.eporner_preview_ratio, 0.75)
            self.assertEqual(loaded.eporner_preview_min_seconds, 120)
            self.assertFalse(loaded.search_filters.exclude_censored)
            self.assertEqual(loaded.download_policy(), DownloadPolicy(1080, 0.75, 120))

    def test_download_policy_settings_are_clamped_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({
                "min_download_height": 9999,
                "eporner_preview_ratio": 9.0,
                "eporner_preview_min_seconds": -10,
            }))

            loaded = SettingsStore(path, defaults=AppSettings()).load().settings

            self.assertEqual(loaded.min_download_height, 4320)
            self.assertEqual(loaded.eporner_preview_ratio, 1.0)
            self.assertEqual(loaded.eporner_preview_min_seconds, 0)

    def test_download_policy_settings_garbage_falls_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({
                "min_download_height": "bad",
                "eporner_preview_ratio": "bad",
                "eporner_preview_min_seconds": "bad",
            }))

            loaded = SettingsStore(path, defaults=AppSettings()).load().settings

            self.assertEqual(loaded.min_download_height, 720)
            self.assertEqual(loaded.eporner_preview_ratio, 0.6)
            self.assertEqual(loaded.eporner_preview_min_seconds, 90)


    def test_corrupt_json_returns_defaults_and_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text("{broken")

            loaded = SettingsStore(path, defaults=AppSettings(parallel_downloads=7)).load()

            self.assertEqual(loaded.settings.parallel_downloads, 7)
            self.assertTrue(loaded.errors)
            self.assertIn("settings", loaded.errors[0].lower())

    def test_load_validation_failure_returns_defaults_and_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({
                "plex_dir": "sftp:host=missing-remote-path",
                "future_key": "preserved",
            }))
            defaults = AppSettings(parallel_downloads=7)

            loaded = SettingsStore(path, defaults=defaults).load()

            self.assertEqual(loaded.settings, defaults)
            self.assertEqual(loaded.unknown, {"future_key": "preserved"})
            self.assertTrue(loaded.errors)
            self.assertIn("sftp", loaded.errors[0].lower())

    def test_validate_clamps_ranges_and_expands_user_paths(self) -> None:
        settings = AppSettings(
            plex_dir="~/Fetcher Output",
            tmp_dir="~/Fetcher Temp",
            parallel_downloads=99,
            concurrent_fragments=0,
            search_filters=SearchFilters(pages=25),
        )

        validated = SettingsStore(Path("unused.json"), defaults=AppSettings()).validate(settings)

        self.assertEqual(validated.parallel_downloads, 16)
        self.assertEqual(validated.concurrent_fragments, 1)
        self.assertEqual(validated.search_filters.pages, 20)
        self.assertEqual(validated.plex_dir, str(Path("~/Fetcher Output").expanduser()))
        self.assertEqual(validated.tmp_dir, str(Path("~/Fetcher Temp").expanduser()))

    def test_save_uses_atomic_sibling_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "settings.json"
            store = SettingsStore(path, defaults=AppSettings())

            with patch("fetcher_core.settings.os.replace", wraps=os.replace) as replace:
                store.save(AppSettings(parallel_downloads=5))

            replace.assert_called_once()
            source, destination = replace.call_args.args
            self.assertEqual(Path(source).parent, path.parent)
            self.assertEqual(Path(destination), path)
            self.assertEqual(json.loads(path.read_text())["parallel_downloads"], 5)
            self.assertFalse(any(path.parent.glob("*.tmp")))

    def test_save_all_persists_settings_and_gui_preferences_with_one_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            store = SettingsStore(path, defaults=AppSettings())

            with patch("fetcher_core.settings.os.replace", wraps=os.replace) as replace:
                store.save_all(
                    AppSettings(parallel_downloads=5),
                    {"staging_dir": str(Path(tmp) / "staging"), "theme": "Dark"},
                )

            replace.assert_called_once()
            payload = json.loads(path.read_text())
            self.assertEqual(payload["parallel_downloads"], 5)
            self.assertEqual(payload["gui"]["theme"], "Dark")

    def test_save_fsyncs_parent_directory_after_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            store = SettingsStore(path, defaults=AppSettings())
            real_open = os.open

            def open_file(name, flags, *args, **kwargs):
                if Path(name) == path.parent and flags == os.O_RDONLY | os.O_DIRECTORY:
                    return 987
                return real_open(name, flags, *args, **kwargs)

            with (
                patch("fetcher_core.settings.os.open", side_effect=open_file) as open_dir,
                patch("fetcher_core.settings.os.fsync") as fsync,
                patch("fetcher_core.settings.os.close") as close,
            ):
                store.save(AppSettings())

            open_dir.assert_any_call(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            fsync.assert_any_call(987)
            close.assert_called_once_with(987)

    def test_save_stores_home_and_repository_paths_portably_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"future_key": "preserved"}))
            store = SettingsStore(path, defaults=AppSettings())
            store.load()
            settings = AppSettings(
                plex_dir=str(Path.home() / "Videos" / "Fetcher"),
                tmp_dir=str(BASE_DIR / "runtime" / "tmp"),
            )

            store.save(store.validate(settings))

            payload = json.loads(path.read_text())
            self.assertEqual(payload["plex_dir"], "~/Videos/Fetcher")
            self.assertEqual(payload["tmp_dir"], "./runtime/tmp")
            self.assertEqual(payload["future_key"], "preserved")
            loaded = SettingsStore(path, defaults=AppSettings()).load()
            self.assertEqual(loaded.settings.plex_dir, str(Path.home() / "Videos" / "Fetcher"))
            self.assertEqual(loaded.settings.tmp_dir, str(BASE_DIR / "runtime" / "tmp"))
            self.assertEqual(loaded.unknown, {"future_key": "preserved"})

    def test_parse_sftp_storage_accepts_portable_specification(self) -> None:
        self.assertEqual(
            parse_sftp_storage("sftp:user=alice@host=media.example:2222/library/videos"),
            {
                "user": "alice",
                "host": "media.example",
                "port": "2222",
                "path": "/library/videos",
            },
        )

    def test_parse_sftp_storage_rejects_malformed_or_control_characters(self) -> None:
        for value in (
            "sftp:",
            "sftp:host=media.example",
            "sftp:host=/library",
            "sftp:host=media.example:bad/library",
            "sftp:host=media.example/library\nput secret",
            "sftp:user=bad\x00name@host=media.example/library",
        ):
            with self.subTest(value=value):
                self.assertIsNone(parse_sftp_storage(value))

    def test_validate_rejects_control_characters_in_local_paths(self) -> None:
        store = SettingsStore(Path("unused.json"), defaults=AppSettings())

        for settings in (
            AppSettings(plex_dir="/tmp/output\nmalicious"),
            AppSettings(tmp_dir="/tmp/cache\x00hidden"),
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                store.validate(settings)

    def test_prepare_runtime_settings_preserves_sftp_and_uses_local_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "completed"
            settings = AppSettings(
                plex_dir="sftp:user=alice@host=media.example/library",
                tmp_dir=str(root / "temp"),
            )

            with patch("fetcher_core.settings.DEFAULT_LOCAL_COMPLETED_DIR", staging):
                validated, tmp_dir, output_dir, warnings = prepare_runtime_settings(settings)

            self.assertEqual(validated.plex_dir, settings.plex_dir)
            self.assertEqual(tmp_dir, root / "temp")
            self.assertEqual(output_dir, staging)
            self.assertTrue(any("sftp" in warning.lower() for warning in warnings))

    def test_load_gui_preferences_expands_path_and_clamps_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({
                "parallel_downloads": 6,
                "future_key": "preserved",
                "gui": {
                    "staging_dir": "./gui-staging",
                    "cache_size_mib": 999_999,
                    "cache_age_days": 0,
                    "theme": "Unexpected",
                },
            }))
            store = SettingsStore(path, defaults=AppSettings())

            preferences = store.load_gui_preferences()

            self.assertEqual(preferences, {
                "staging_dir": str(BASE_DIR / "gui-staging"),
                "cache_size_mib": 102_400,
                "cache_age_days": 1,
                "theme": "System",
            })
            self.assertEqual(store.load().settings.parallel_downloads, 6)
            self.assertEqual(store.load().unknown["future_key"], "preserved")

    def test_save_gui_preferences_is_atomic_portable_and_preserves_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({
                "parallel_downloads": 9,
                "future_key": {"enabled": True},
                "gui": {"future_gui_key": "preserved"},
            }))
            store = SettingsStore(path, defaults=AppSettings())

            with patch("fetcher_core.settings.os.replace", wraps=os.replace) as replace_file:
                saved = store.save_gui_preferences({
                    "staging_dir": str(Path.home() / "Fetcher Staging"),
                    "cache_size_mib": 2048,
                    "cache_age_days": 30,
                    "theme": "Dark",
                })

            replace_file.assert_called_once()
            payload = json.loads(path.read_text())
            self.assertEqual(payload["parallel_downloads"], 9)
            self.assertEqual(payload["future_key"], {"enabled": True})
            self.assertEqual(payload["gui"]["staging_dir"], "~/Fetcher Staging")
            self.assertEqual(payload["gui"]["cache_size_mib"], 2048)
            self.assertEqual(payload["gui"]["future_gui_key"], "preserved")
            self.assertEqual(saved["staging_dir"], str(Path.home() / "Fetcher Staging"))
            self.assertEqual(SettingsStore(path, defaults=AppSettings()).load_gui_preferences(), saved)

    def test_save_runtime_path_failure_does_not_mutate_settings_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "settings.json"
            original = json.dumps({"parallel_downloads": 4, "future_key": "safe"})
            path.write_text(original)

            def failing_probe(candidate: Path) -> None:
                if candidate == root / "unwritable-output":
                    raise OSError("permission denied")

            store = SettingsStore(path, defaults=AppSettings(), path_probe=failing_probe)
            store.load()

            with self.assertRaisesRegex(ValueError, "Download directory.*permission denied"):
                store.save(AppSettings(
                    plex_dir=str(root / "unwritable-output"),
                    tmp_dir=str(root / "temp"),
                ))

            self.assertEqual(path.read_text(), original)

    def test_validate_runtime_paths_probes_temp_local_plex_and_gui_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            probed: list[Path] = []
            store = SettingsStore(
                root / "settings.json",
                defaults=AppSettings(),
                path_probe=probed.append,
            )
            settings = AppSettings(
                plex_dir=str(root / "plex"),
                tmp_dir=str(root / "temp"),
            )

            validated, preferences, warnings = store.validate_runtime_paths(settings, {
                "staging_dir": str(root / "staging"),
                "cache_size_mib": 512,
                "cache_age_days": 14,
                "theme": "System",
            })

            self.assertEqual(validated.plex_dir, str(root / "plex"))
            self.assertEqual(preferences["staging_dir"], str(root / "staging"))
            self.assertEqual(probed, [root / "temp", root / "plex", root / "staging"])
            self.assertEqual(warnings, [])

    def test_validate_runtime_paths_does_not_probe_sftp_remote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            probed: list[Path] = []
            store = SettingsStore(
                root / "settings.json",
                defaults=AppSettings(),
                path_probe=probed.append,
            )
            sftp = "sftp:user=alice@host=media.example/library"

            validated, _, warnings = store.validate_runtime_paths(AppSettings(
                plex_dir=sftp,
                tmp_dir=str(root / "temp"),
            ))

            self.assertEqual(validated.plex_dir, sftp)
            self.assertEqual(probed, [root / "temp"])
            self.assertTrue(any("reachability" in warning.lower() for warning in warnings))


if __name__ == "__main__":
    unittest.main()
