import json
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
from docstral_worker.extract import extract_page
from docstral_worker.snapshot import CurrentSnapshot, page_slug

_DEFAULT_CONTEXT = IngestContext()


class DocsChunkMetadata(DocumentChunkMetadata):
    model_config = ConfigDict(frozen=True)

    title: str
    content_hash: str


class IngestResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    indexed: int = Field(ge=0)
    failed: int = Field(ge=0)
    duration_seconds: float = Field(ge=0.0)


class PipelineConfig(BaseModel):
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
    return _hash_json(
        {
            "pipeline": config.model_dump(mode="json"),
            "toolkit": version("mistralai-search-toolkit"),
            "embedding_model": model_name,
            "embedding_dimensions": 1024,
        }
    )


def document_fingerprint(document: Document, processing_hash: str) -> str:
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


async def ingest_snapshot(
    snapshot: CurrentSnapshot, pipeline: Pipeline
) -> IngestResult:
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
