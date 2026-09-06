"""Exclude concurrent ingestion and deployment maintenance on the worker volume."""

import asyncio
import fcntl
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic

from docstral_worker import IngestionError


class WorkerState:
    """One lock and durable markers, shared by operator and scheduled runs."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.maintenance = directory / ".maintenance"

    @asynccontextmanager
    async def lock(
        self, *, timeout: float = 0, allow_maintenance: bool = False
    ) -> AsyncIterator[None]:
        """Reject overlap; maintenance may wait for the active ingestion stage."""
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise IngestionError("Ingestion requires an existing worker data directory")
        # Keep the existing filename so old and new deployment tools share the lock.
        descriptor = os.open(
            self.directory / ".publication.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "r+") as lock_file:
            deadline = monotonic() + timeout
            while True:
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if monotonic() >= deadline:
                        raise IngestionError(
                            "Another ingestion stage holds the worker lock"
                        ) from exc
                    await asyncio.sleep(min(0.1, max(0, deadline - monotonic())))
            try:
                if any(
                    path.is_symlink()
                    for path in (
                        self.maintenance,
                        self.directory / ".publication-pending",
                    )
                ):
                    raise IngestionError(
                        "Ingestion refuses symbolic-link state markers"
                    )
                if (self.directory / ".publication-pending").exists():
                    raise IngestionError(
                        "Legacy publication is incomplete; finish it with the previous "
                        "release before switching to incremental ingestion"
                    )
                if not allow_maintenance and self.maintenance.exists():
                    raise IngestionError("Worker is in maintenance; ingestion refused")
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    async def set_maintenance(self, enabled: bool, *, timeout: float) -> None:
        """Persist maintenance without stopping any application process."""
        async with self.lock(timeout=timeout, allow_maintenance=True):
            if enabled:
                descriptor = os.open(
                    self.maintenance,
                    os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW,
                    0o600,
                )
                os.close(descriptor)
            else:
                self.maintenance.unlink(missing_ok=True)
