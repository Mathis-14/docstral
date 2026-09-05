"""Coordinate corpus publication through the worker's persistent volume."""

import asyncio
import fcntl
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic

from docstral_worker import IngestionError


class PublicationState:
    """One lock and durable markers, shared by operator and scheduled runs."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.maintenance = directory / ".maintenance"
        self.pending = directory / ".publication-pending"
        self.published = directory / ".published-snapshot"

    @asynccontextmanager
    async def lock(
        self, *, timeout: float = 0, allow_maintenance: bool = False
    ) -> AsyncIterator[None]:
        """Reject overlap; a deployment may wait for an active publication."""
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise IngestionError(
                "Publication requires an existing worker data directory"
            )
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
                            "Another publication holds the worker lock"
                        ) from exc
                    await asyncio.sleep(min(0.1, max(0, deadline - monotonic())))
            try:
                if any(
                    path.is_symlink()
                    for path in (self.maintenance, self.pending, self.published)
                ):
                    raise IngestionError(
                        "Publication refuses symbolic-link state markers"
                    )
                if not allow_maintenance and self.maintenance.exists():
                    raise IngestionError(
                        "Worker is in maintenance; publication refused"
                    )
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def mark(self, path: Path, value: str = "") -> None:
        """Write a marker without following a substituted symbolic link."""
        descriptor = os.open(
            path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as marker:
            marker.write(value + "\n")

    async def set_maintenance(self, enabled: bool, *, timeout: float) -> None:
        """Persist deployment maintenance, never dismiss an incomplete index."""
        async with self.lock(timeout=timeout, allow_maintenance=True):
            if self.pending.exists():
                raise IngestionError(
                    "Index publication is incomplete; repair it before maintenance"
                )
            if enabled:
                self.mark(self.maintenance)
            else:
                self.maintenance.unlink(missing_ok=True)
