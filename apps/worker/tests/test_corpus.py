import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from hashlib import sha256

import httpx
import pytest
from docstral_worker import IngestionError
from docstral_worker.corpus import SourceIdentity, VespaCorpus
from docstral_worker.ingest import DocsChunkMetadata
from mistralai.search.toolkit.document import Document, DocumentChunk, compute_id
from mistralai.search.toolkit.plugins.vespa import VespaClient, VespaClientConfig
from mistralai.search.toolkit.plugins.vespa.errors import (
    VespaClientError,
    VespaIndexException,
)
from mistralai.search.toolkit.search.errors import IndexingError
from pydantic import ValidationError


@asynccontextmanager
async def _corpus(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> AsyncIterator[VespaCorpus]:
    original = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return original(
            base_url=str(kwargs["base_url"]), transport=httpx.MockTransport(handler)
        )

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    client = VespaClient(VespaClientConfig(endpoint="http://vespa.test"))
    try:
        yield VespaCorpus(client)
    finally:
        await client.aclose()


def _identity(path: str) -> SourceIdentity:
    source_id = f"https://docs.mistral.ai{path}"
    return SourceIdentity(source_id=source_id, document_id=compute_id(source_id))


def _record(path: str, chunk: str = "chunk") -> dict[str, object]:
    return {"id": chunk, "fields": _identity(path).model_dump()}


@pytest.mark.parametrize("path", ["/", "/models", "/api", "/fr/agents", "/image.png"])
def test_source_identity_accepts_canonical_historical_routes(path: str) -> None:
    assert _identity(path).source_id == f"https://docs.mistral.ai{path}"


@pytest.mark.parametrize(
    "source_id",
    [
        "https://outside.test/models",
        "http://docs.mistral.ai/models",
        "https://docs.mistral.ai/en/models",
        "https://docs.mistral.ai/Models",
        "https://docs.mistral.ai/models/",
        "https://docs.mistral.ai/models?q=x",
        "https://docs.mistral.ai/models#section",
        "https://user:password@docs.mistral.ai/models",
        "https://docs.mistral.ai:444/models",
        "https://docs.mistral.ai:443/models",
        "/models",
        "",
    ],
)
def test_source_identity_rejects_noncanonical_urls(source_id: str) -> None:
    with pytest.raises(ValidationError, match="canonical Docstral"):
        SourceIdentity(source_id=source_id, document_id=compute_id(source_id))


async def test_inventory_reads_all_pages_and_deduplicates_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/document/v1/docs/docs/docid/"
        assert request.url.params["selection"] == "docs"
        assert request.url.params["cluster"] == "content"
        assert request.url.params["fieldSet"] == "docs:source_id,document_id"
        if "continuation" not in request.url.params:
            return httpx.Response(
                200,
                json={"documents": [_record("/b")], "continuation": "next"},
            )
        assert request.url.params["continuation"] == "next"
        return httpx.Response(
            200,
            json={"documents": [_record("/a"), _record("/b", "another-chunk")]},
        )

    async with _corpus(monkeypatch, handler) as corpus:
        assert await corpus.list_sources() == (_identity("/a"), _identity("/b"))
    assert len(requests) == 2


async def test_inventory_can_be_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"documents": []})

    async with _corpus(monkeypatch, handler) as corpus:
        assert await corpus.list_sources() == ()


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"source_id": "https://docs.mistral.ai/a"},
        {"source_id": "https://docs.mistral.ai/a", "document_id": "wrong-id"},
        {"source_id": 123, "document_id": "wrong-id"},
        {**_identity("/a").model_dump(), "unexpected": "field"},
    ],
)
async def test_inventory_rejects_invalid_identity(
    monkeypatch: pytest.MonkeyPatch, fields: dict[str, object]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"documents": [{"id": "chunk", "fields": fields}]}
        )

    async with _corpus(monkeypatch, handler) as corpus:
        with pytest.raises(IngestionError, match="Invalid Vespa source inventory"):
            await corpus.list_sources()


@pytest.mark.parametrize("continuation", ["", "repeated"])
async def test_inventory_rejects_invalid_continuation(
    monkeypatch: pytest.MonkeyPatch, continuation: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"documents": [], "continuation": continuation})

    async with _corpus(monkeypatch, handler) as corpus:
        with pytest.raises(IngestionError, match="inventory continuation"):
            await corpus.list_sources()


@pytest.mark.parametrize("failure", ["service", "network", "shape"])
async def test_inventory_dependency_failures_are_explicit(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "continuation" not in request.url.params:
            return httpx.Response(
                200,
                json={"documents": [_record("/a")], "continuation": "next"},
            )
        if failure == "network":
            raise httpx.ConnectError("unavailable", request=request)
        if failure == "service":
            return httpx.Response(503, json={"message": "unavailable"})
        return httpx.Response(200, json={"documents": [{"id": "chunk"}]})

    async with _corpus(monkeypatch, handler) as corpus:
        with pytest.raises(VespaClientError):
            await corpus.list_sources()


def _document() -> Document:
    source_id = _identity("/a").source_id
    content = "First evidence. Second evidence."
    return Document(
        source_id=source_id,
        content=content,
        chunks=[
            DocumentChunk(
                source_id=source_id,
                locator=f"char:{start}-{end}",
                start_offset=start,
                end_offset=end,
                content=content[start:end],
                metadata=DocsChunkMetadata(
                    title="Evidence", content_hash=sha256(content.encode()).hexdigest()
                ),
                embedding=[1.0] * 1024,
            )
            for start, end in ((0, 15), (16, len(content)))
        ],
    )


@pytest.mark.parametrize("feed_fails", [False, True])
async def test_index_document_uses_real_toolkit_replacement(
    monkeypatch: pytest.MonkeyPatch, feed_fails: bool
) -> None:
    document = _document()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "DELETE":
            assert (
                request.url.params["selection"] == f'docs.document_id=="{document.id}"'
            )
            assert request.url.params["cluster"] == "content"
            return httpx.Response(200, json={"documentCount": 1})
        assert request.method == "POST"
        fields = json.loads(request.content)["fields"]
        chunk = document.chunks[len([r for r in requests if r.method == "POST"]) - 1]
        assert fields["document_id"] == document.id
        assert fields["source_id"] == document.source_id
        assert fields["id"] == chunk.id
        assert fields["content"] == chunk.content
        assert fields["content_embedding"]["values"] == chunk.embedding
        assert fields["title"] == "Evidence"
        assert fields["content_hash"] == sha256(document.content.encode()).hexdigest()
        if feed_fails and chunk == document.chunks[-1]:
            return httpx.Response(503, json={"message": "unavailable"})
        return httpx.Response(200, json={})

    async with _corpus(monkeypatch, handler) as corpus:
        if feed_fails:
            with pytest.raises(IndexingError):
                await corpus.index_document(document)
        else:
            await corpus.index_document(document)
    assert [request.method for request in requests] == (
        ["DELETE", "POST", "POST", "DELETE"]
        if feed_fails
        else ["DELETE", "POST", "POST"]
    )


@pytest.mark.parametrize("count", [0, 2])
async def test_delete_document_is_scoped_and_accepts_absence(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    identity = _identity("/a")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert (
            request.url.params["selection"]
            == f'docs.document_id=="{identity.document_id}"'
        )
        assert request.url.params["cluster"] == "content"
        return httpx.Response(200, json={"documentCount": count})

    async with _corpus(monkeypatch, handler) as corpus:
        await corpus.delete_document(identity.document_id)


async def test_delete_document_does_not_hide_service_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"message": "unavailable"})

    async with _corpus(monkeypatch, handler) as corpus:
        with pytest.raises(
            VespaIndexException, match="Failed to delete document chunks"
        ):
            await corpus.delete_document(_identity("/a").document_id)
