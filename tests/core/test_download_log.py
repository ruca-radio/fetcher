from __future__ import annotations

from pathlib import Path

from fetcher_core.download_log import DownloadCatalog, record_download, video_is_downloaded, without_downloaded
from fetcher_core.models import Video


def video(video_id: str = "abc123", title: str = "Shared Title", site: str = "PornHub") -> Video:
    return Video(video_id, title, 90, 10, f"https://example.test/{video_id}", site)


def test_record_download_appends_a_readable_log_and_legacy_id(tmp_path: Path) -> None:
    log_path = tmp_path / "downloads.txt"
    legacy_path = tmp_path / "downloaded_ids.txt"
    saved = tmp_path / "library" / "Shared Title [abc123].mkv"

    record_download(video(title="A\tBad\nTitle"), saved, log_path=log_path, legacy_path=legacy_path)
    record_download(video("second", "Later"), None, log_path=log_path, legacy_path=legacy_path)

    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if not line.startswith("#")]
    assert len(lines) == 2
    first = lines[0].split("\t")
    assert len(first) == 6
    assert first[1:] == ["PornHub", "abc123", "A Bad Title", "https://example.test/abc123", str(saved)]
    assert "second" in lines[1]
    assert legacy_path.read_text(encoding="utf-8").splitlines() == ["abc123", "second"]


def test_catalog_matches_legacy_ids_log_titles_and_library_files(tmp_path: Path) -> None:
    library = tmp_path / "library"
    nested = library / "PornHub"
    nested.mkdir(parents=True)
    (nested / "Clip [ph999].mkv").write_bytes(b"x")
    (library / "Plain Title.mp4").write_bytes(b"x")
    (library / "Untitled [solo].mp4").write_bytes(b"x")
    (library / "ignore.mp4.part").write_bytes(b"x")
    legacy = tmp_path / "downloaded_ids.txt"
    legacy.write_text("legacy-id\n", encoding="utf-8")
    catalog = DownloadCatalog(tmp_path / "downloads.txt", legacy, [library])
    catalog.record(video("logged", "Logged Title"), library / "Logged Title [logged].mkv")

    assert catalog.contains(video("legacy-id", "Something else"))
    assert catalog.contains(video("other", "Logged Title"))
    assert catalog.contains(video("ph999", "Different name"))
    assert catalog.contains(video("missing", "Plain Title"))
    assert catalog.contains(video("solo", "Not untitled"))
    assert not catalog.contains(video("zzz", "Untitled"))
    assert not catalog.contains(video("ignore", "ignore"))
    assert not catalog.contains(video("fresh", "Fresh Title"))


def test_result_cell_greys_downloaded_text() -> None:
    from fetchtui import result_cell

    assert result_cell("Title", False) == "Title"
    grey = result_cell("Title", True)
    assert grey.plain == "Title"
    assert grey.style == "#8a8a8a"


def test_visibility_helpers_hide_or_keep_downloaded_rows() -> None:
    kept = video("new", "New")
    old = video("abc123", "Shared Title")
    ids = {"abc123"}
    titles = {"sharedtitle"}

    assert video_is_downloaded(old, ids, titles)
    assert video_is_downloaded(video("other", "Shared Title"), ids, titles)
    assert not video_is_downloaded(kept, ids, titles)
    assert without_downloaded([kept, old], ids, titles) == [kept]
