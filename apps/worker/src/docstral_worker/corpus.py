"""Read and mutate individual documentation sources through the Vespa SDK."""

from typing import Protocol, Self

from docstral_vespa import COLLECTION_NAME, index_for_client
from mistralai.search.toolkit.document import Document, compute_id
from mistralai.search.toolkit.plugins.vespa import VespaClient
from mistralai.search.toolkit.search.errors import DocumentNotFoundError
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from docstral_worker import IngestionError
from docstral_worker.urls import canonicalize, is_docs_url


class SourceIdentity(BaseModel):
    """An indexed canonical source, including routes no longer admitted by crawl."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    document_id: str

    @model_validator(mode="after")
    def _validate_identity(self) -> Self:
        if (
            not is_docs_url(self.source_id)
            or canonicalize(self.source_id, self.source_id).url != self.source_id
        ):
            raise ValueError("source_id must be a canonical Docstral documentation URL")
        if self.document_id != compute_id(self.source_id):
            raise ValueError("document_id must match the toolkit ID of source_id")
        return self


class Corpus(Protocol):
    """The source operations used by incremental ingestion."""

    async def list_sources(self) -> tuple[SourceIdentity, ...]: ...
    async def index_document(self, document: Document) -> None: ...
    async def delete_document(self, document_id: str) -> None: ...


class VespaCorpus:
    """Use a caller-owned client and the shared chunk index contract."""

    def __init__(self, client: VespaClient) -> None:
        self._client = client
        self._index = index_for_client(client)

    async def list_sources(self) -> tuple[SourceIdentity, ...]:
        """Read every inventory page, rejecting invalid source identities."""
        sources: set[SourceIdentity] = set()
        continuation: str | None = None
        seen_continuations: set[str] = set()
        while True:
            page = await self._client.visit_by_selection(
                COLLECTION_NAME,
                COLLECTION_NAME,
                cluster=self._index.schema.content_cluster,
                field_set=f"{COLLECTION_NAME}:source_id,document_id",
                continuation=continuation,
            )
            for document in page.documents:
                try:
                    sources.add(SourceIdentity.model_validate(document.fields))
                except ValidationError as exc:
                    raise IngestionError(
                        "Invalid Vespa source inventory; check source_id and "
                        "document_id in the docs collection"
                    ) from exc
            continuation = page.continuation
            if continuation is None:
                return tuple(sorted(sources, key=lambda source: source.source_id))
            if not continuation or continuation in seen_continuations:
                raise IngestionError(
                    "Invalid Vespa inventory continuation; cannot verify all sources"
                )
            seen_continuations.add(continuation)

    async def index_document(self, document: Document) -> None:
        """Replace only this source through the toolkit's indexing implementation."""
        await self._index.index_document(document)

    async def delete_document(self, document_id: str) -> None:
        """Ensure the source is absent, including after a previously completed delete."""
        try:
            await self._index.delete_document(document_id)
        except DocumentNotFoundError:
            return
