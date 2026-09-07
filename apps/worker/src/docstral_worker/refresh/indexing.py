import math
from typing import Protocol

from mistralai.search.toolkit.document import Document
from mistralai.search.toolkit.ingestion import File
from mistralai.search.toolkit.ingestion.processor import DocumentProcessor

from docstral_worker import IngestionError
from docstral_worker.extract import ExtractionError
from docstral_worker.ingest import (
    DocsExtractor,
    PipelineConfig,
    build_splitter,
    document_fingerprint,
    processing_fingerprint,
)
from docstral_worker.refresh.corpus import Corpus
from docstral_worker.refresh.models import DownloadedPage, PageResult, PageState


class DocumentEmbedder(DocumentProcessor, Protocol):
    @property
    def model_name(self) -> str: ...


class PageIndexer:
    def __init__(
        self,
        corpus: Corpus,
        embedder: DocumentEmbedder,
        config: PipelineConfig | None = None,
    ) -> None:
        self._corpus = corpus
        self._embedder = embedder
        self._config = config or PipelineConfig()
        self._splitter = build_splitter(self._config)
        self._processing_hash = processing_fingerprint(
            self._config, embedder.model_name
        )

    async def sync(self, page: DownloadedPage) -> PageResult:
        try:
            document = await DocsExtractor().extract(
                File(path=page.url, name="page.html", source_id=page.url, raw=page.html)
            )
        except ExtractionError:
            return PageResult(
                url=page.url,
                status="extraction_failed",
                links=page.links,
                reason="Cannot extract documentation content",
            )
        document = document.model_copy(
            update={
                "metadata": document.metadata.model_copy(
                    update={"pipeline_version": self._config.version}
                )
            }
        )
        fingerprint = document_fingerprint(document, self._processing_hash)
        previous = await self._corpus.read_page(page.url)
        if previous is not None and previous.index_hash == fingerprint:
            return PageResult(url=page.url, status="unchanged", links=page.links)
        chunks = await self._splitter.process(document)
        embedded = await self._embedder.process(chunks)
        validate_embeddings(chunks, embedded)
        unconfirmed = PageState(source_id=page.url, document_id=document.id)
        await self._corpus.write_page(unconfirmed)
        await self._corpus.index_document(embedded)
        await self._corpus.write_page(
            unconfirmed.model_copy(update={"index_hash": fingerprint})
        )
        return PageResult(url=page.url, status="indexed", links=page.links)


def validate_embeddings(chunks: Document, embedded: Document) -> None:
    if [chunk.id for chunk in embedded.chunks] != [chunk.id for chunk in chunks.chunks]:
        raise IngestionError("Embedding output does not cover every input chunk")
    if not embedded.chunks or any(
        chunk.embedding is None
        or len(chunk.embedding) != 1024
        or not all(math.isfinite(value) for value in chunk.embedding)
        for chunk in embedded.chunks
    ):
        raise IngestionError("Every chunk requires 1024 finite embedding values")
