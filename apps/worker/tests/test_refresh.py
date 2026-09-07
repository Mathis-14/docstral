from pathlib import Path

from docstral_worker.cli import main
from docstral_worker.snapshot import current_snapshot
from worker_fixtures import Services
from worker_fixtures import services as services


def test_explicit_capture_cli_writes_an_offline_snapshot(
    tmp_path: Path, services: Services
) -> None:
    assert main(["crawl", "--out", str(tmp_path), "--delay", "0"]) == 0
    snapshot = current_snapshot(tmp_path)
    assert snapshot is not None
    assert snapshot.get("https://docs.mistral.ai/a") == services.pages["/a"]
