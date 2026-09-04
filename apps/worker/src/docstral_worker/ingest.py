"""Index the current documentation snapshot in Vespa."""

from hashlib import sha256
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
from docstral_worker.extract import extract_page
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
    splitter = MarkdownTokenTextSplitter(
        MarkdownTokenTextSplitterConfig(
            chunk_size=800,
            chunk_max_size=800,
            chunk_overlap=0,
        )
    )
    return Pipeline(
        loader=None,
        extractor=DocsExtractor(),
        text_splitter=splitter,
        embedder=embedder,
        stores=index,
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
            cached = snapshot.get(entry.canonical_url)
            if cached is None:
                raise IngestionError(
                    f"Stored page {entry.canonical_url!r} missing from snapshot"
                )
            if sha256(cached.body).hexdigest() != cached.raw_sha256:
                raise IngestionError(
                    f"Raw HTML for {entry.canonical_url!r} does not match its "
                    "recorded SHA-256"
                )
            document = await pipeline.run_file(
                File(
                    path=entry.canonical_url,
                    name=f"{page_slug(entry.canonical_url)}.html",
                    raw=cached.body,
                    source_id=entry.canonical_url,
                )
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
