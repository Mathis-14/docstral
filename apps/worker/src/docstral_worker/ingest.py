from __future__ import annotations

import json
from hashlib import sha256
from importlib.metadata import version
from time import monotonic
from typing import TYPE_CHECKING, override

import structlog
from mistralai.search.toolkit.context import IngestContext
from mistralai.search.toolkit.document import (
    Document,
    DocumentChunk,
    DocumentChunkMetadata,
    compute_char_locator,
    compute_id,
)
from mistralai.search.toolkit.ingestion import File
from mistralai.search.toolkit.ingestion.extractors.base import DocumentExtractor
from mistralai.search.toolkit.ingestion.text_splitters import (
    MarkdownTokenTextSplitter,
    MarkdownTokenTextSplitterConfig,
)
from pydantic import BaseModel, ConfigDict, Field

from docstral_worker import IngestionError
from docstral_worker.extract import extract_page
from docstral_worker.refresh.models import DownloadedPage
from docstral_worker.snapshot import CurrentSnapshot, SnapshotReadError

if TYPE_CHECKING:
    from docstral_worker.refresh.indexing import PageIndexer

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


async def ingest_snapshot(
    snapshot: CurrentSnapshot, indexer: PageIndexer
) -> IngestResult:
    started_at = monotonic()
    indexed = failed = 0
    logger = structlog.get_logger(__name__)
    for entry in snapshot.manifest.pages:
        try:
            html = snapshot.get(entry.url)
        except SnapshotReadError as error:
            failed += 1
            logger.error("ingestion_page", url=entry.url, error_message=str(error))
            continue
        result = await indexer.sync(DownloadedPage(url=entry.url, html=html, links=()))
        failed += result.status == "extraction_failed"
        indexed += result.status in ("indexed", "unchanged")
        logger.info("ingestion_page", url=entry.url, decision=result.status)
    return IngestResult(
        indexed=indexed, failed=failed, duration_seconds=monotonic() - started_at
    )
