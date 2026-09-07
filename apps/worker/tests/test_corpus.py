from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
import pytest
from docstral_worker import IngestionError
from docstral_worker.refresh.corpus import VespaCorpus
from mistralai.search.toolkit.document import compute_id
from mistralai.search.toolkit.plugins.vespa import VespaClient, VespaClientConfig


@asynccontextmanager
async def corpus_client(
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


def record(path: str) -> dict[str, object]:
    url = f"https://docs.mistral.ai{path}"
    return {"id": path, "fields": {"source_id": url, "document_id": compute_id(url)}}


async def test_inventory_deduplicates_all_pages_and_keeps_unconfirmed_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/pages/pages/" in request.url.path:
            documents = [record("/b"), record("/unconfirmed")]
        elif "continuation" not in request.url.params:
            return httpx.Response(
                200, json={"documents": [record("/b")], "continuation": "next"}
            )
        else:
            documents = [record("/fr/old"), record("/b")]
        return httpx.Response(200, json={"documents": documents})

    async with corpus_client(monkeypatch, handler) as corpus:
        sources = await corpus.list_sources()
    assert [source.source_id for source in sources] == [
        "https://docs.mistral.ai/b",
        "https://docs.mistral.ai/fr/old",
        "https://docs.mistral.ai/unconfirmed",
    ]


async def test_inventory_rejects_a_page_with_the_wrong_document_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        fields = {"source_id": "https://docs.mistral.ai/a", "document_id": "wrong-id"}
        return httpx.Response(
            200, json={"documents": [{"id": "chunk", "fields": fields}]}
        )

    async with corpus_client(monkeypatch, handler) as corpus:
        with pytest.raises(IngestionError, match="Invalid Vespa source inventory"):
            await corpus.list_sources()


async def test_inventory_refuses_repeating_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"documents": [], "continuation": "same"})

    async with corpus_client(monkeypatch, handler) as corpus:
        with pytest.raises(IngestionError, match="inventory continuation"):
            await corpus.list_sources()
