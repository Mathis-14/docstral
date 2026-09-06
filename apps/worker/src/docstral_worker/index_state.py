"""Persist the worker's verified and pending article index state."""

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from docstral_worker import IngestionError
from docstral_worker.corpus import SourceIdentity

_STATE_FILE = "index-state.json"


class IndexStateError(IngestionError):
    """The index registry cannot be safely read or persisted."""


class IndexedPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str
    index_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    pending: bool = False


class IndexState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format_version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    pages: dict[str, IndexedPage] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_identities(self) -> Self:
        for url, page in self.pages.items():
            SourceIdentity(source_id=url, document_id=page.document_id)
        return self


class IndexStateStore:
    """Read and replace state while the caller holds the worker volume lock."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def record(
        self, state: IndexState, url: str, page: IndexedPage | None
    ) -> IndexState:
        """Persist one confirmed or pending article change and advance the revision."""
        pages = dict(state.pages)
        if page is None:
            del pages[url]
        else:
            pages[url] = page
        updated = IndexState(revision=state.revision + 1, pages=pages)
        self.write(updated)
        return updated

    def read(self) -> IndexState | None:
        try:
            with self._directory() as directory_fd:
                try:
                    descriptor = os.open(
                        _STATE_FILE,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=directory_fd,
                    )
                except FileNotFoundError:
                    return None
                with os.fdopen(descriptor, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise IndexStateError("Index state must be a regular file")
                    return IndexState.model_validate_json(stream.read())
        except ValidationError as exc:
            raise IndexStateError(
                "Cannot read index-state.json: invalid JSON or incompatible state; "
                "check its version, page identities and hashes"
            ) from exc
        except OSError as exc:
            raise IndexStateError(
                f"Cannot read index state in {str(self.directory)!r}: {exc.strerror}"
            ) from exc

    def write(self, state: IndexState) -> None:
        try:
            # Frozen models still contain mutable dictionaries; validate at the boundary.
            checked = IndexState.model_validate_json(state.model_dump_json())
            content = (checked.model_dump_json(indent=2) + "\n").encode("utf-8")
            with self._directory() as directory_fd:
                self._check_destination(directory_fd)
                temporary = f".index-state-{uuid4().hex}.tmp"
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(
                        temporary,
                        _STATE_FILE,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                    )
                    os.fsync(directory_fd)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
        except ValidationError as exc:
            raise IndexStateError("Cannot write invalid index state") from exc
        except OSError as exc:
            raise IndexStateError(
                f"Cannot write index state in {str(self.directory)!r}: {exc.strerror}"
            ) from exc

    @staticmethod
    def _check_destination(directory_fd: int) -> None:
        try:
            existing = os.stat(_STATE_FILE, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(existing.st_mode):
            raise IndexStateError("Index state must be a regular file, not a symlink")

    @contextmanager
    def _directory(self) -> Iterator[int]:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory = self.directory.absolute()
        descriptor = os.open(directory.anchor, flags)
        try:
            for part in directory.parts[1:]:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)
