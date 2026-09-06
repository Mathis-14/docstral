"""Index the current documentation snapshot in Vespa."""

import asyncio
import json
import math
from collections.abc import Sequence
from hashlib import sha256
from importlib.metadata import version
from time import monotonic
from typing import override

import structlog
from mistralai.search.toolkit.context import IngestContext
from mistralai.search.toolkit.document import (
    Document,
    DocumentChunk,
    DocumentChunkMetadata,
    compute_char_locator,
    compute_id,
)
from mistralai.search.toolkit.embedding import Embedder
from mistralai.search.toolkit.ingestion import File
from mistralai.search.toolkit.ingestion.extractors.base import DocumentExtractor
from mistralai.search.toolkit.ingestion.pipelines import Pipeline
from mistralai.search.toolkit.ingestion.text_splitters import (
    MarkdownTokenTextSplitter,
    MarkdownTokenTextSplitterConfig,
)
from mistralai.search.toolkit.search import VectorStoreIndex
from pydantic import BaseModel, ConfigDict, Field

from docstral_worker import IngestionError
from docstral_worker.crawl import PageDecision
from docstral_worker.extract import ExtractionError, extract_page
from docstral_worker.snapshot import CurrentSnapshot, page_slug

_DEFAULT_CONTEXT = IngestContext()


class DocsChunkMetadata(DocumentChunkMetadata):
    """Metadata persisted with each documentation chunk."""

    model_config = ConfigDict(frozen=True)

    title: str
    content_hash: str


class IngestResult(BaseModel):
    """Summary of one snapshot ingestion run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    indexed: int = Field(ge=0)
    failed: int = Field(ge=0)
    duration_seconds: float = Field(ge=0.0)


class PipelineConfig(BaseModel):
    """Processing settings shared by local and staged ingestion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Bump when extraction semantics change independently of these settings.
    version: str = Field(default="1.0.0", min_length=1)
    chunk_size: int = Field(default=800, gt=0)
    chunk_max_size: int = Field(default=800, gt=0)
    chunk_overlap: int = Field(default=0, ge=0)


def build_splitter(config: PipelineConfig) -> MarkdownTokenTextSplitter:
    return MarkdownTokenTextSplitter(
        MarkdownTokenTextSplitterConfig(
            chunk_size=config.chunk_size,
            chunk_max_size=config.chunk_max_size,
            chunk_overlap=config.chunk_overlap,
        )
    )


def processing_fingerprint(config: PipelineConfig, model_name: str) -> str:
    """Invalidate indexed content when its processing settings change."""
    return _hash_json(
        {
            "pipeline": config.model_dump(mode="json"),
            "toolkit": version("mistralai-search-toolkit"),
            "embedding_model": model_name,
            "embedding_dimensions": 1024,
        }
    )


def document_fingerprint(document: Document, processing_hash: str) -> str:
    """Combine the citation content hash with indexed title and processing settings."""
    metadata = document.chunks[0].metadata
    if not isinstance(metadata, DocsChunkMetadata):
        raise IngestionError("Article metadata is missing")
    return _hash_json(
        {
            "content_hash": metadata.content_hash,
            "title": metadata.title,
            "processing_hash": processing_hash,
        }
    )


def _hash_json(value: dict[str, object]) -> str:
    return sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


class DocsExtractor(DocumentExtractor):
    """Adapt Docstral's audited HTML extraction to a toolkit document."""

    @override
    async def extract(
        self, file: File, context: IngestContext = _DEFAULT_CONTEXT
    ) -> Document:
        page = extract_page(file.source_id, file.raw)
        return Document(
            source_id=page.url,
            content=page.markdown,
            chunks=[
                DocumentChunk(
                    source_id=page.url,
                    locator=compute_char_locator(0, page.chars),
                    start_offset=0,
                    end_offset=page.chars,
                    parent_ref=compute_id(page.url),
                    content=page.markdown,
                    metadata=DocsChunkMetadata(
                        title=page.title,
                        content_hash=page.content_hash,
                    ),
                )
            ],
        )


def build_pipeline(*, index: VectorStoreIndex, embedder: Embedder) -> Pipeline:
    """Build the deterministic Docstral indexing pipeline."""
    config = PipelineConfig()
    return Pipeline(
        loader=None,
        extractor=DocsExtractor(),
        text_splitter=build_splitter(config),
        embedder=embedder,
        stores=index,
        pipeline_version=config.version,
    )


def _snapshot_file(snapshot: CurrentSnapshot, url: str) -> File:
    cached = snapshot.get(url)
    if cached is None:
        raise IngestionError(f"Stored page {url!r} missing from snapshot")
    return File(
        path=url,
        name=f"{page_slug(url)}.html",
        raw=cached.body,
        source_id=url,
    )


async def extract_documents(
    snapshot: CurrentSnapshot, config: PipelineConfig
) -> tuple[list[Document], int]:
    """Extract stored pages, counting conversion errors and rejecting corrupt files."""
    extractor = DocsExtractor()
    documents: list[Document] = []
    failed = 0
    for entry in snapshot.manifest.pages:
        if entry.decision is not PageDecision.STORED:
            continue
        try:
            document = await extractor.extract(
                _snapshot_file(snapshot, entry.canonical_url)
            )
        except ExtractionError:
            failed += 1
            structlog.get_logger(__name__).warning(
                "refresh_page_failed",
                snapshot=snapshot.directory.name,
                stage="extract",
                url=entry.canonical_url,
                error_code="extraction_failed",
            )
        else:
            documents.append(
                document.model_copy(
                    update={
                        "metadata": document.metadata.model_copy(
                            update={"pipeline_version": config.version}
                        )
                    }
                )
            )
        # Parsing and file reads are synchronous; let heartbeats/cancellation run.
        await asyncio.sleep(0)
    return documents, failed


def validate_documents(documents: Sequence[Document], *, embedded: bool) -> None:
    """Enforce the identity, content and vector contract at artifact boundaries."""
    # Offline CLI imports do not need to load the Vespa application.
    from docstral_worker.corpus import SourceIdentity

    seen: set[str] = set()
    for document in documents:
        SourceIdentity(source_id=document.source_id, document_id=document.id)
        if document.source_id in seen or not document.content or not document.chunks:
            raise IngestionError("Prepared articles must be distinct and non-empty")
        seen.add(document.source_id)
        content_hash = sha256(document.content.encode()).hexdigest()
        chunk_ids: set[str] = set()
        title: str | None = None
        for chunk in document.chunks:
            metadata = chunk.metadata
            if (
                not isinstance(metadata, DocsChunkMetadata)
                or metadata.content_hash != content_hash
            ):
                raise IngestionError(
                    "Prepared chunk metadata does not match its article"
                )
            if title is not None and metadata.title != title:
                raise IngestionError("Prepared chunks disagree on their article title")
            title = metadata.title
            if (
                chunk.source_id != document.source_id
                or chunk.parent_ref != document.id
                or not 0
                <= chunk.start_offset
                < chunk.end_offset
                <= len(document.content)
                or chunk.content
                != document.content[chunk.start_offset : chunk.end_offset]
                or chunk.locator
                != compute_char_locator(chunk.start_offset, chunk.end_offset)
                or chunk.id != compute_id(document.source_id, chunk.locator)
                or chunk.id in chunk_ids
            ):
                raise IngestionError("Prepared chunk identity or position is invalid")
            chunk_ids.add(chunk.id)
            if embedded and (
                chunk.embedding is None
                or len(chunk.embedding) != 1024
                or not all(math.isfinite(value) for value in chunk.embedding)
            ):
                raise IngestionError(
                    "Every prepared chunk requires 1024 finite embedding values"
                )


async def ingest_snapshot(
    snapshot: CurrentSnapshot, pipeline: Pipeline
) -> IngestResult:
    """Index every stored page, continuing only after page-local failures."""
    logger = structlog.get_logger(__name__)
    started_at = monotonic()
    indexed = 0
    failed = 0
    stored = (
        page for page in snapshot.manifest.pages if page.decision is PageDecision.STORED
    )
    for entry in stored:
        page_started_at = monotonic()
        try:
            document = await pipeline.run_file(
                _snapshot_file(snapshot, entry.canonical_url)
            )
        except IngestionError as exc:
            failed += 1
            logger.info(
                "ingestion_page",
                url=entry.canonical_url,
                decision="failed",
                duration_ms=round((monotonic() - page_started_at) * 1_000, 3),
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            continue
        indexed += 1
        logger.info(
            "ingestion_page",
            url=entry.canonical_url,
            decision="indexed",
            duration_ms=round((monotonic() - page_started_at) * 1_000, 3),
            chunks=len(document.chunks),
        )
    return IngestResult(
        indexed=indexed,
        failed=failed,
        duration_seconds=monotonic() - started_at,
    )
