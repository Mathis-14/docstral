import asyncio
from pathlib import Path

import pytest
from docstral_worker import IngestionError
from docstral_worker.cli import main
from docstral_worker.maintenance import WorkerState


async def test_ingestion_stages_cannot_overlap_and_lock_is_released(
    tmp_path: Path,
) -> None:
    state = WorkerState(tmp_path)
    async with state.lock():
        with pytest.raises(IngestionError, match="holds the worker lock"):
            async with WorkerState(tmp_path).lock():
                pytest.fail("concurrent ingestion entered")
    async with WorkerState(tmp_path).lock():
        pass


async def test_maintenance_waits_persists_and_blocks_new_runs(tmp_path: Path) -> None:
    state = WorkerState(tmp_path)
    async with state.lock():
        task = asyncio.create_task(state.set_maintenance(True, timeout=1))
        await asyncio.sleep(0)
        assert not task.done()
    await task
    with pytest.raises(IngestionError, match="maintenance"):
        async with WorkerState(tmp_path).lock():
            pytest.fail("ingestion entered maintenance")
    await WorkerState(tmp_path).set_maintenance(False, timeout=0)
    async with state.lock():
        pass


async def test_legacy_publication_requires_recovery_with_the_previous_release(
    tmp_path: Path,
) -> None:
    state = WorkerState(tmp_path)
    pending = tmp_path / ".publication-pending"
    pending.write_text("20260903T120000Z\n")
    for enabled in (True, False):
        with pytest.raises(IngestionError, match="finish it with the previous release"):
            await state.set_maintenance(enabled, timeout=0)
    assert not state.maintenance.exists()
    with pytest.raises(IngestionError, match="finish it with the previous release"):
        async with state.lock():
            pytest.fail("incremental ingestion bypassed an incomplete legacy rebuild")
    assert pending.read_text() == "20260903T120000Z\n"


async def test_lock_refuses_symlink_without_modifying_target(tmp_path: Path) -> None:
    target = tmp_path / "personal"
    target.write_text("keep")
    (tmp_path / ".publication.lock").symlink_to(target)
    with pytest.raises(OSError):
        async with WorkerState(tmp_path).lock():
            pytest.fail("followed lock symlink")
    assert target.read_text() == "keep"


@pytest.mark.parametrize("name", [".maintenance", ".publication-pending"])
async def test_dangling_state_symlink_fails_closed(tmp_path: Path, name: str) -> None:
    (tmp_path / name).symlink_to(tmp_path / "missing")
    with pytest.raises(IngestionError, match="symbolic-link state"):
        async with WorkerState(tmp_path).lock():
            pytest.fail("ignored a substituted marker")


def test_maintenance_cli_reports_state_and_lock_errors(tmp_path: Path) -> None:
    args = ["--data-dir", str(tmp_path), "--timeout", "0"]
    assert main(["maintenance", "on", *args]) == 0
    assert (tmp_path / ".maintenance").exists()
    assert main(["maintenance", "off", *args]) == 0
    assert not (tmp_path / ".maintenance").exists()
    (tmp_path / ".publication-pending").touch()
    assert main(["maintenance", "off", *args]) == 1
    assert (tmp_path / ".publication-pending").exists()
