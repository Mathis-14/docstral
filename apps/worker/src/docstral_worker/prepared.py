"""Persist complete, verifiable document artifacts between ingestion stages."""

import os
import stat
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, Self

from mistralai.search.toolkit.document import (
    Document,
    DocumentTypeRegistry,
    SerializedDocument,
    deserialize_document,
    serialize_document,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from docstral_worker import IngestionError
from docstral_worker.corpus import SourceIdentity
from docstral_worker.crawl import SHA256_PATTERN
from docstral_worker.ingest import DocsChunkMetadata, validate_documents
from docstral_worker.snapshot import SnapshotRef as SnapshotRef

Stage = Literal["extracted", "compared", "split", "embedded"]


class PreparationStats(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    total: int = Field(ge=0)
    failed: int = Field(ge=0)
    changed: int = Field(default=0, ge=0)
    unchanged: int = Field(default=0, ge=0)
    duration_seconds: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)


class StageRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot: SnapshotRef
    stage: Stage
    manifest_sha256: str = Field(pattern=SHA256_PATTERN)


class PreparationManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format_version: Literal[1] = 1
    snapshot: SnapshotRef
    stage: Stage
    processing_hash: str = Field(pattern=SHA256_PATTERN)
    documents_sha256: str = Field(pattern=SHA256_PATTERN)
    document_count: int = Field(ge=0)
    state_revision: int | None = Field(default=None, ge=0)
    stats: PreparationStats
    removed: tuple[SourceIdentity, ...] = ()

    @model_validator(mode="after")
    def validate_stage(self) -> Self:
        stats = self.stats
        expected_count = stats.total - stats.failed
        if self.stage == "extracted":
            if self.removed or stats.changed or stats.unchanged:
                raise ValueError("extraction cannot contain comparison results")
        else:
            if self.state_revision is None:
                raise ValueError("compared artifacts require an index-state revision")
            if stats.changed + stats.unchanged + stats.failed != stats.total:
                raise ValueError("article counts do not match the snapshot total")
            expected_count = stats.changed
        if self.document_count != expected_count:
            raise ValueError("document count does not match the stage totals")
        if len({source.source_id for source in self.removed}) != len(self.removed):
            raise ValueError("removed articles must be distinct")
        return self


class PreparedStore:
    """Read or finalize one stage; callers own locking and ingestion policy."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.registry = DocumentTypeRegistry()
        self.registry.register("docstral.chunk-metadata.v1", DocsChunkMetadata)

    def write(
        self,
        *,
        snapshot: SnapshotRef,
        stage: Stage,
        documents: Sequence[Document],
        processing_hash: str,
        stats: PreparationStats,
        state_revision: int | None = None,
        removed: tuple[SourceIdentity, ...] = (),
    ) -> StageRef:
        """Finalize an immutable stage directory, refusing any existing output."""
        try:
            validate_documents(documents, embedded=stage == "embedded")
            payload = b"".join(
                serialize_document(document, self.registry).model_dump_json().encode()
                + b"\n"
                for document in documents
            )
            manifest = PreparationManifest(
                snapshot=snapshot,
                stage=stage,
                processing_hash=processing_hash,
                documents_sha256=sha256(payload).hexdigest(),
                document_count=len(documents),
                state_revision=state_revision,
                stats=stats,
                removed=removed,
            )
            manifest_bytes = (manifest.model_dump_json() + "\n").encode()
            prepared = self._prepared_directory(snapshot, create=True)
            destination = prepared / stage
            if destination.exists() or destination.is_symlink():
                raise IngestionError(
                    f"Prepared artifact {str(destination)!r} already exists"
                )
            with TemporaryDirectory(prefix=f".{stage}-", dir=prepared) as temporary:
                directory = Path(temporary)
                _write_file(directory / "documents.jsonl", payload)
                _write_file(directory / "manifest.json", manifest_bytes)
                _sync_directory(directory)
                directory.rename(destination)
                _sync_directory(prepared)
            return StageRef(
                snapshot=snapshot,
                stage=stage,
                manifest_sha256=sha256(manifest_bytes).hexdigest(),
            )
        except (OSError, ValueError, LookupError, TypeError) as exc:
            raise IngestionError(
                f"Cannot write prepared stage {stage!r} for snapshot "
                f"{snapshot.name!r}: {type(exc).__name__}"
            ) from exc

    def read(self, ref: StageRef) -> tuple[PreparationManifest, list[Document]]:
        """Verify both hashes and the envelope before restoring concrete types."""
        try:
            directory = self._prepared_directory(ref.snapshot) / ref.stage
            _check_directory(directory)
            manifest_bytes = _read_file(directory / "manifest.json")
            if sha256(manifest_bytes).hexdigest() != ref.manifest_sha256:
                raise IngestionError("Prepared manifest hash does not match reference")
            manifest = PreparationManifest.model_validate_json(manifest_bytes)
            if manifest.snapshot != ref.snapshot or manifest.stage != ref.stage:
                raise IngestionError("Prepared manifest does not match stage reference")
            payload = _read_file(directory / "documents.jsonl")
            if sha256(payload).hexdigest() != manifest.documents_sha256:
                raise IngestionError("Prepared document hash does not match manifest")
            documents = [
                deserialize_document(
                    SerializedDocument.model_validate_json(line), self.registry
                )
                for line in payload.splitlines()
            ]
            if len(documents) != manifest.document_count:
                raise IngestionError("Prepared document count does not match manifest")
            validate_documents(documents, embedded=ref.stage == "embedded")
            return manifest, documents
        except (OSError, ValueError, LookupError, TypeError) as exc:
            raise IngestionError(
                f"Cannot read prepared stage {ref.stage!r} for snapshot "
                f"{ref.snapshot.name!r}: {type(exc).__name__}"
            ) from exc

    def _prepared_directory(
        self, snapshot: SnapshotRef, *, create: bool = False
    ) -> Path:
        directory = self.data_dir
        for parent in reversed(directory.parents):
            _check_directory(parent)
        _check_directory(directory)
        for component in ("snapshots", snapshot.name):
            directory /= component
            _check_directory(directory)
        prepared = directory / "prepared"
        if create and not prepared.exists() and not prepared.is_symlink():
            prepared.mkdir(mode=0o700)
            _sync_directory(directory)
        _check_directory(prepared)
        return prepared


def _check_directory(path: Path) -> None:
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise IngestionError(
            f"Prepared artifacts require a directory without symbolic links: {str(path)!r}"
        )


def _read_file(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise IngestionError(
                f"Prepared artifact file is not regular: {str(path)!r}"
            )
        return stream.read()


def _write_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
