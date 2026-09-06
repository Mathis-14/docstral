"""Run the six ingestion stages while keeping corpus writes article-scoped."""

import asyncio
from pathlib import Path
from time import monotonic
from typing import Literal, Protocol

import structlog
from mistralai.search.toolkit.document import Document
from mistralai.search.toolkit.ingestion.processor import DocumentProcessor
from pydantic import Field

from docstral_worker import IngestionError
from docstral_worker.corpus import Corpus, SourceIdentity
from docstral_worker.crawl import PageDecision
from docstral_worker.crawl_run import refresh_snapshot
from docstral_worker.index_state import IndexedPage, IndexState, IndexStateStore
from docstral_worker.ingest import (
    IngestResult,
    PipelineConfig,
    build_splitter,
    document_fingerprint,
    extract_documents,
    processing_fingerprint,
)
from docstral_worker.maintenance import WorkerState
from docstral_worker.prepared import (
    PreparationManifest,
    PreparationStats,
    PreparedStore,
    Stage,
    StageRef,
)
from docstral_worker.retention import prune_snapshots
from docstral_worker.snapshot import CurrentSnapshot, SnapshotRef, read_current_snapshot


class DocumentEmbedder(DocumentProcessor, Protocol):
    @property
    def model_name(self) -> str: ...


class RefreshResult(IngestResult):
    """Article counts; duration excludes crawl, scheduling and lock acquisition."""

    changed: int = Field(ge=0)
    unchanged: int = Field(ge=0)
    deleted: int = Field(ge=0)
    status: Literal["complete", "partial"]


class IncrementalIngestion:
    """Each stage holds the volume lock; only index_delta writes to Vespa."""

    def __init__(
        self,
        data_dir: Path,
        *,
        corpus: Corpus,
        embedder: DocumentEmbedder,
        config: PipelineConfig | None = None,
    ) -> None:
        self._root = data_dir / "snapshots"
        self._corpus = corpus
        self._embedder = embedder
        self._config = config or PipelineConfig()
        self._splitter = build_splitter(self._config)
        self._coordination = WorkerState(data_dir)
        self._state = IndexStateStore(data_dir)
        self._artifacts = PreparedStore(data_dir)
        self._processing_hash = processing_fingerprint(
            self._config, embedder.model_name
        )

    async def crawl(self) -> SnapshotRef:
        async with self._coordination.lock():
            await refresh_snapshot(self._root)
            reference, _ = read_current_snapshot(self._root)
            return reference

    async def extract(self, reference: SnapshotRef) -> StageRef:
        async with self._coordination.lock():
            started = monotonic()
            _, snapshot = read_current_snapshot(self._root, reference)
            state = self._state.read()
            documents, failed = await extract_documents(snapshot, self._config)
            return self._save(
                reference,
                "extracted",
                documents,
                PreparationStats(total=snapshot.manifest.counts.stored, failed=failed),
                started,
                state_revision=state.revision if state is not None else None,
            )

    async def compare_hashes(self, reference: StageRef) -> StageRef:
        async with self._coordination.lock():
            started = monotonic()
            manifest, documents, state, snapshot = self._load(reference, "extracted")
            if state is None:
                state = IndexState(
                    pages={
                        source.source_id: IndexedPage(document_id=source.document_id)
                        for source in await self._corpus.list_sources()
                    }
                )
                self._state.write(state)
            changed = [
                doc
                for doc in documents
                if (page := state.pages.get(doc.source_id)) is None
                or page.pending
                or page.index_hash != document_fingerprint(doc, self._processing_hash)
            ]
            present = _stored_urls(snapshot)
            removed = tuple(
                SourceIdentity(source_id=url, document_id=page.document_id)
                for url, page in sorted(state.pages.items())
                if url not in present
            )
            stats = manifest.stats.model_copy(
                update={
                    "changed": len(changed),
                    "unchanged": len(documents) - len(changed),
                }
            )
            return self._save(
                reference.snapshot,
                "compared",
                changed,
                stats,
                started,
                state_revision=state.revision,
                removed=removed,
            )

    async def split(self, reference: StageRef) -> StageRef:
        return await self._transform(reference, "compared", "split", self._splitter)

    async def embed(self, reference: StageRef) -> StageRef:
        return await self._transform(reference, "split", "embedded", self._embedder)

    async def _transform(
        self,
        reference: StageRef,
        expected: Stage,
        output: Stage,
        processor: DocumentProcessor,
    ) -> StageRef:
        async with self._coordination.lock():
            started = monotonic()
            manifest, documents, _, _ = self._load(reference, expected)
            processed: list[Document] = []
            for document in documents:
                result = await processor.process(document)
                if output == "embedded" and [chunk.id for chunk in result.chunks] != [
                    chunk.id for chunk in document.chunks
                ]:
                    raise IngestionError(
                        "Embedding output does not cover every input chunk"
                    )
                processed.append(result)
                await asyncio.sleep(0)
            return self._save(
                reference.snapshot,
                output,
                processed,
                manifest.stats,
                started,
                state_revision=manifest.state_revision,
                removed=manifest.removed,
            )

    async def index_delta(self, reference: StageRef) -> RefreshResult:
        async with self._coordination.lock():
            started = monotonic()
            manifest, documents, state, _ = self._load(reference, "embedded")
            if state is None:
                raise IngestionError("Index state is missing after comparison")
            logger = structlog.get_logger(__name__).bind(
                snapshot=reference.snapshot.name, stage="index_delta"
            )
            indexed = deleted = 0
            for document in documents:
                previous = state.pages.get(document.source_id)
                state = self._state.record(
                    state,
                    document.source_id,
                    IndexedPage(
                        document_id=document.id,
                        index_hash=previous.index_hash
                        if previous is not None
                        else None,
                        pending=True,
                    ),
                )
                await self._corpus.index_document(document)
                state = self._state.record(
                    state,
                    document.source_id,
                    IndexedPage(
                        document_id=document.id,
                        index_hash=document_fingerprint(
                            document, self._processing_hash
                        ),
                    ),
                )
                indexed += 1
                logger.info(
                    "refresh_page_indexed",
                    url=document.source_id,
                    decision="indexed",
                    indexed=indexed,
                    deleted=deleted,
                )
            for source in manifest.removed:
                page = state.pages[source.source_id].model_copy(
                    update={"pending": True}
                )
                state = self._state.record(state, source.source_id, page)
                await self._corpus.delete_document(source.document_id)
                state = self._state.record(state, source.source_id, None)
                deleted += 1
                logger.info(
                    "refresh_page_indexed",
                    url=source.source_id,
                    decision="deleted",
                    indexed=indexed,
                    deleted=deleted,
                )
            result = RefreshResult(
                indexed=indexed,
                failed=manifest.stats.failed,
                duration_seconds=manifest.stats.duration_seconds
                + monotonic()
                - started,
                changed=manifest.stats.changed,
                unchanged=manifest.stats.unchanged,
                deleted=deleted,
                status="partial" if manifest.stats.failed else "complete",
            )
            prune_snapshots(self._root)
            logger.info("refresh_finished", **result.model_dump())
            return result

    def _load(
        self,
        reference: StageRef,
        expected: Stage,
    ) -> tuple[PreparationManifest, list[Document], IndexState | None, CurrentSnapshot]:
        if reference.stage != expected:
            raise IngestionError(f"Expected {expected} artifact")
        _, snapshot = read_current_snapshot(self._root, reference.snapshot)
        manifest, documents = self._artifacts.read(reference)
        state = self._state.read()
        if manifest.processing_hash != self._processing_hash:
            raise IngestionError(
                "Prepared artifact uses incompatible processing settings"
            )
        if manifest.state_revision != (state.revision if state is not None else None):
            raise IngestionError("Prepared artifact is obsolete: indexed state changed")
        present = _stored_urls(snapshot)
        if manifest.stats.total != len(present) or any(
            doc.source_id not in present for doc in documents
        ):
            raise IngestionError(
                "Prepared articles do not match the snapshot inventory"
            )
        if expected != "extracted" and state is not None:
            expected_removed = {
                url: page.document_id
                for url, page in state.pages.items()
                if url not in present
            }
            if {
                source.source_id: source.document_id for source in manifest.removed
            } != expected_removed:
                raise IngestionError(
                    "Prepared removals do not match the snapshot inventory"
                )
        return manifest, documents, state, snapshot

    def _save(
        self,
        snapshot: SnapshotRef,
        stage: Stage,
        documents: list[Document],
        stats: PreparationStats,
        started: float,
        *,
        state_revision: int | None,
        removed: tuple[SourceIdentity, ...] = (),
    ) -> StageRef:
        stats = stats.model_copy(
            update={"duration_seconds": stats.duration_seconds + monotonic() - started}
        )
        reference = self._artifacts.write(
            snapshot=snapshot,
            stage=stage,
            documents=documents,
            processing_hash=self._processing_hash,
            stats=stats,
            state_revision=state_revision,
            removed=removed,
        )
        structlog.get_logger(__name__).info(
            "refresh_stage_finished",
            snapshot=snapshot.name,
            stage=stage,
            documents=len(documents),
            removed=len(removed),
            extraction_failure_rate=stats.failed / stats.total if stats.total else 0,
            **stats.model_dump(),
        )
        return reference


def _stored_urls(snapshot: CurrentSnapshot) -> set[str]:
    return {
        page.canonical_url
        for page in snapshot.manifest.pages
        if page.decision is PageDecision.STORED
    }
