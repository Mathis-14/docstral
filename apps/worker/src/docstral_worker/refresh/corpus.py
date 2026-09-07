import json
from typing import Protocol

from docstral_vespa import COLLECTION_NAME, PAGE_COLLECTION_NAME, index_for_client
from mistralai.search.toolkit.document import Document, compute_id
from mistralai.search.toolkit.plugins.vespa import VespaClient
from mistralai.search.toolkit.search.errors import DocumentNotFoundError
from pydantic import ValidationError

from docstral_worker import IngestionError
from docstral_worker.refresh.models import PageState, SourceIdentity


class Corpus(Protocol):
    async def list_sources(self) -> tuple[SourceIdentity, ...]: ...
    async def read_page(self, url: str) -> PageState | None: ...
    async def write_page(self, page: PageState) -> None: ...
    async def delete_page(self, url: str) -> None: ...
    async def index_document(self, document: Document) -> None: ...
    async def delete_document(self, document_id: str) -> None: ...


class VespaCorpus:
    def __init__(self, client: VespaClient) -> None:
        self._client = client
        self._index = index_for_client(client)

    async def list_sources(self) -> tuple[SourceIdentity, ...]:
        chunks = await self._list_sources(COLLECTION_NAME)
        pages = await self._list_sources(PAGE_COLLECTION_NAME)
        return tuple(
            sorted(set(chunks) | set(pages), key=lambda source: source.source_id)
        )

    async def _list_sources(self, collection: str) -> tuple[SourceIdentity, ...]:
        sources: set[SourceIdentity] = set()
        continuation: str | None = None
        seen_continuations: set[str] = set()
        while True:
            page = await self._client.visit_by_selection(
                collection,
                collection,
                cluster=self._index.schema.content_cluster,
                field_set=f"{collection}:source_id,document_id",
                continuation=continuation,
                extra_params={"wantedDocumentCount": "1000"},
            )
            for document in page.documents:
                try:
                    sources.add(SourceIdentity.model_validate(document.fields))
                except ValidationError as exc:
                    raise IngestionError(
                        "Invalid Vespa source inventory; check source_id and "
                        f"document_id in the {collection} collection"
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
        await self._index.index_document(document)

    async def delete_document(self, document_id: str) -> None:
        try:
            await self._index.delete_document(document_id)
        except DocumentNotFoundError:
            return

    async def read_page(self, url: str) -> PageState | None:
        record = await self._client.get_document(PAGE_COLLECTION_NAME, compute_id(url))
        if record is None:
            return None
        page = PageState.model_validate(record.fields)
        if page.source_id != url:
            raise IngestionError("Vespa page confirmation does not match its URL")
        return page

    async def write_page(self, page: PageState) -> None:
        await self._client.feed_document(
            PAGE_COLLECTION_NAME,
            page.document_id,
            json.dumps({"fields": page.model_dump(mode="json")}),
        )

    async def delete_page(self, url: str) -> None:
        document_id = compute_id(url)
        await self.write_page(PageState(source_id=url, document_id=document_id))
        await self.delete_document(document_id)
        await self._client.delete_document(PAGE_COLLECTION_NAME, document_id)
