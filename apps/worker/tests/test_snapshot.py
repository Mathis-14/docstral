import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from docstral_worker.crawl import CrawlCounts, CrawlEntry, CrawlResult
from docstral_worker.snapshot import SnapshotReadError, current_snapshot, write_snapshot
from worker_fixtures import DOCS, snapshot


def test_complete_snapshot_can_be_read_without_network(tmp_path: Path) -> None:
    saved = snapshot(tmp_path, ("/a", b"<html>A</html>"))
    assert saved.manifest.version == 2
    assert saved.get(DOCS + "/a") == b"<html>A</html>"
    assert sorted(path.name for path in saved.directory.iterdir()) == [
        "manifest.json",
        "raw",
    ]


def test_absent_current_does_not_create_files(tmp_path: Path) -> None:
    assert current_snapshot(tmp_path / "absent") is None
    assert list(tmp_path.iterdir()) == []


def test_failed_capture_preserves_current_without_archiving_failure(
    tmp_path: Path,
) -> None:
    previous = snapshot(tmp_path, ("/a", b"A"))
    before = set(tmp_path.iterdir())
    result = CrawlResult(
        pages=(CrawlEntry(url=DOCS + "/b", status="failed", reason="HTTP 503"),),
        counts=CrawlCounts(failed=1),
        complete=False,
        duration_seconds=0,
    )
    assert write_snapshot(tmp_path, datetime.now(UTC), result) is None
    assert set(tmp_path.iterdir()) == before
    current = current_snapshot(tmp_path)
    assert current is not None
    assert current.directory == previous.directory


@pytest.mark.parametrize("damage", ["corrupt", "missing", "directory", "symlink"])
def test_unusable_html_fails_when_the_page_is_read(tmp_path: Path, damage: str) -> None:
    saved = snapshot(tmp_path, ("/a", b"Original"))
    page = saved.directory / saved.manifest.pages[0].path
    page.unlink()
    if damage == "corrupt":
        page.write_bytes(b"Changed")
    elif damage == "directory":
        page.mkdir()
    elif damage == "symlink":
        outside = tmp_path / "outside"
        outside.write_bytes(b"Original")
        page.symlink_to(outside)
    with pytest.raises(SnapshotReadError):
        saved.get(DOCS + "/a")


def test_legacy_format_requires_new_capture_and_preserves_old_files(
    tmp_path: Path,
) -> None:
    saved = snapshot(tmp_path, ("/a", b"A"))
    manifest = saved.directory / "manifest.json"
    manifest.write_text('{"version": 1, "pages": []}')
    with pytest.raises(SnapshotReadError, match="crawl again"):
        current_snapshot(tmp_path)
    assert manifest.read_text() == '{"version": 1, "pages": []}'


def test_manifest_cannot_read_a_path_outside_snapshot(tmp_path: Path) -> None:
    saved = snapshot(tmp_path, ("/a", b"A"))
    manifest = saved.directory / "manifest.json"
    payload = json.loads(manifest.read_text())
    payload["pages"][0]["path"] = "../../outside.html"
    manifest.write_text(json.dumps(payload))
    loaded = current_snapshot(tmp_path)
    assert loaded is not None
    with pytest.raises(SnapshotReadError, match="Invalid snapshot path"):
        loaded.get(DOCS + "/a")


def test_current_must_name_a_local_directory(tmp_path: Path) -> None:
    (tmp_path / "current").write_text("../outside")
    with pytest.raises(SnapshotReadError):
        current_snapshot(tmp_path)


def test_distinct_urls_do_not_overwrite_raw_files(tmp_path: Path) -> None:
    saved = snapshot(tmp_path, ("/", b"Root"), ("/index", b"Index"))
    assert saved.get(DOCS + "/") == b"Root"
    assert saved.get(DOCS + "/index") == b"Index"


def test_manifest_rejects_noncanonical_page_identity(tmp_path: Path) -> None:
    saved = snapshot(tmp_path, ("/a", b"A"))
    manifest = saved.directory / "manifest.json"
    payload = json.loads(manifest.read_text())
    payload["pages"][0]["url"] = "https://docs.mistral.ai/en/a/"
    manifest.write_text(json.dumps(payload))
    with pytest.raises(SnapshotReadError, match="canonical"):
        current_snapshot(tmp_path)
