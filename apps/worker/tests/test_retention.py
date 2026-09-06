from datetime import timedelta
from pathlib import Path

from docstral_worker.retention import prune_snapshots
from docstral_worker.snapshot import write_snapshot
from test_snapshot import CRAWLED_AT, failed, result, sitemap, stored


def test_retention_preserves_current_and_unrecognised_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshots"
    complete = [
        write_snapshot(
            root,
            CRAWLED_AT + timedelta(seconds=i),
            sitemap(),
            result(stored("/a", b"a")),
        )
        for i in range(5)
    ]
    failures = [
        write_snapshot(
            root,
            CRAWLED_AT + timedelta(seconds=10 + i),
            sitemap(),
            result(failed("/a"), complete=False),
        )
        for i in range(3)
    ]
    outside = tmp_path / "personal"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    symlink = root / "20260901T000000Z"
    symlink.symlink_to(outside, target_is_directory=True)
    unknown = root / "20260902T000000Z"
    unknown.mkdir()
    (unknown / "keep").write_text("keep")
    # Even a symlink inside a recognised, expired snapshot cannot remove its target.
    (complete[1] / "external").symlink_to(outside, target_is_directory=True)
    # Current is protected even when it is outside the two newest snapshots.
    (root / "current").write_text(complete[0].name)
    pointer = (root / "current").read_bytes()

    prune_snapshots(root)

    assert [path.exists() for path in complete] == [True, False, False, True, True]
    assert [path.exists() for path in failures] == [False, False, True]
    assert (root / "current").read_bytes() == pointer
    assert (outside / "keep").read_text() == "keep"
    assert (unknown / "keep").read_text() == "keep"
    assert symlink.is_symlink()
