from __future__ import annotations

from pathlib import Path

from fetcher_core.paths import resolve_runtime_paths


def test_source_checkout_keeps_repository_local_runtime_paths(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'fetcher-v1'\n")

    paths = resolve_runtime_paths(tmp_path, environ={}, home=tmp_path / "home")

    assert paths.state_dir == tmp_path / ".fetcher"
    assert paths.cache_dir == tmp_path / ".fetcher"
    assert paths.tmp_dir == tmp_path / "tmp"
    assert paths.preview_cache_dir == tmp_path / ".fetcher" / "preview-cache"


def test_installed_package_uses_xdg_roots_without_creating_directories(tmp_path: Path) -> None:
    package_root = tmp_path / "site-packages"
    package_root.mkdir()
    package_root.chmod(0o555)
    data_home = tmp_path / "xdg-data"
    cache_home = tmp_path / "xdg-cache"

    paths = resolve_runtime_paths(
        package_root,
        environ={"XDG_DATA_HOME": str(data_home), "XDG_CACHE_HOME": str(cache_home)},
        home=tmp_path / "home",
    )

    assert paths.state_dir == data_home / "fetcher"
    assert paths.cache_dir == cache_home / "fetcher"
    assert paths.tmp_dir == cache_home / "fetcher" / "tmp"
    assert paths.preview_cache_dir == cache_home / "fetcher" / "preview-cache"
    assert paths.mutable_root == data_home / "fetcher"
    assert list(tmp_path.iterdir()) == [package_root]


def test_installed_package_falls_back_to_user_local_xdg_defaults(tmp_path: Path) -> None:
    package_root = tmp_path / "site-packages"
    home = tmp_path / "home"

    paths = resolve_runtime_paths(package_root, environ={}, home=home)

    assert paths.state_dir == home / ".local" / "share" / "fetcher"
    assert paths.cache_dir == home / ".cache" / "fetcher"


def test_model_defaults_are_derived_from_central_runtime_paths() -> None:
    from fetcher_core import models

    assert models.STATE_DIR == models.RUNTIME_PATHS.state_dir
    assert models.CACHE_DIR == models.RUNTIME_PATHS.cache_dir
    assert models.DEFAULT_TMP_DIR == models.RUNTIME_PATHS.tmp_dir
    assert models.PREVIEW_CACHE_DIR == models.RUNTIME_PATHS.preview_cache_dir
    assert models.MUTABLE_ROOT == models.RUNTIME_PATHS.mutable_root
