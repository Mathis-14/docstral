"""Bound cluster snapshot storage without touching unknown paths or symlinks."""

import re
import shutil
from pathlib import Path

import structlog
from pydantic import ValidationError

from docstral_worker import IngestionError
from docstral_worker.snapshot import MANIFEST_FILE, SnapshotManifest, current_snapshot

_SNAPSHOT_NAME = re.compile(r"\d{8}T\d{6}Z(?:-failed)?")


def prune_snapshots(root: Path) -> None:
    """Keep two complete snapshots, one failed run and current."""
    if root.is_symlink():
        raise IngestionError("Snapshot retention refuses a symbolic-link root")
    current = current_snapshot(root)
    protected = {current.directory.name if current else None}
    complete: list[Path] = []
    failed: list[Path] = []
    for directory in root.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            continue
        if not _SNAPSHOT_NAME.fullmatch(directory.name):
            continue
        manifest_path = directory / MANIFEST_FILE
        if manifest_path.is_symlink():
            continue
        try:
            manifest = SnapshotManifest.model_validate_json(manifest_path.read_bytes())
        except (OSError, ValidationError):
            # An unrecognised directory is not ours to remove.
            continue
        is_failed = directory.name.endswith("-failed")
        if is_failed != (manifest.counts.failed > 0):
            continue
        (failed if is_failed else complete).append(directory)
    for directories, keep in ((complete, 2), (failed, 1)):
        for directory in sorted(directories, reverse=True)[keep:]:
            if directory.name not in protected:
                shutil.rmtree(directory)
                structlog.get_logger(__name__).info(
                    "snapshot_removed", snapshot=directory.name
                )
