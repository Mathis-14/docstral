import pytest
from docstral_backend import (
    DocumentationRetriever,
    RetrievalError,
    RetrievalRequest,
)
from mistralai.search.toolkit.document import ChunkType
from mistralai.search.toolkit.search import SearchResult, SearchResultChunk
from pydantic import ValidationError

DOCS = "https://docs.mistral.ai"


class _FakeRetriever:
    def __init__(self, results: list[SearchResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, int, bool, bool]] = []

    async def retrieve(
        self,
        query: str,
        top_k: int,
        include_metadata: bool,
        include_content: bool,
    ) -> list[SearchResult]:
        self.calls.append((query, top_k, include_metadata, include_content))
        return self.results


class _FailingRetriever:
    def __init__(self, error: RuntimeError) -> None:
        self.error = error

    async def retrieve(
        self,
        query: str,
        top_k: int,
        include_metadata: bool,
        include_content: bool,
    ) -> list[SearchResult]:
        raise self.error


async def test_retrieve_projects_ordered_chunks() -> None:
    first = _result(
        chunk_id="chunk-b",
        source_id=f"{DOCS}/second",
        title="Second",
        score=0.4,
        start_offset=10,
        end_offset=17,
    )
    second = _result(
        chunk_id="chunk-a",
        source_id=f"{DOCS}/first",
        title="First",
        score=0.9,
        start_offset=20,
        end_offset=27,
    )
    raw_retriever = _FakeRetriever([first, second])
    retriever = DocumentationRetriever(raw_retriever)

    response = await retriever.retrieve(
        RetrievalRequest(query="How does retrieval work?", top_k=7)
    )

    assert raw_retriever.calls == [("How does retrieval work?", 7, True, True)]
    assert response.query == "How does retrieval work?"
    assert [chunk.id for chunk in response.chunks] == ["chunk-b", "chunk-a"]
    assert [chunk.rank for chunk in response.chunks] == [1, 2]
    assert response.chunks[0].source_id == f"{DOCS}/second"
    assert response.chunks[0].title == "Second"
    assert response.chunks[0].content_hash == "a" * 64
    assert response.chunks[0].locator == "char:10-17"
    assert response.chunks[0].start_offset == 10
    assert response.chunks[0].end_offset == 17
    assert response.chunks[0].content == "Content"
    assert response.chunks[0].score == 0.4


async def test_retrieve_preserves_duplicate_sources() -> None:
    source_id = f"{DOCS}/shared"
    raw_retriever = _FakeRetriever(
        [
            _result(
                chunk_id="chunk-1",
                source_id=source_id,
                title="Shared",
                score=0.8,
                start_offset=0,
                end_offset=7,
            ),
            _result(
                chunk_id="chunk-2",
                source_id=source_id,
                title="Shared",
                score=0.7,
                start_offset=10,
                end_offset=17,
            ),
        ]
    )

    response = await DocumentationRetriever(raw_retriever).retrieve(
        RetrievalRequest(query="shared", top_k=2)
    )

    assert [chunk.source_id for chunk in response.chunks] == [source_id, source_id]
    assert [chunk.id for chunk in response.chunks] == ["chunk-1", "chunk-2"]


@pytest.mark.parametrize("query", ["", " ", "\n\t"])
def test_request_rejects_blank_query(query: str) -> None:
    with pytest.raises(ValidationError, match="query must not be blank"):
        RetrievalRequest(query=query, top_k=1)


@pytest.mark.parametrize("top_k", [0, -1])
def test_request_rejects_invalid_top_k(top_k: int) -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        RetrievalRequest(query="valid", top_k=top_k)


@pytest.mark.parametrize(
    ("title", "content_hash"),
    [
        pytest.param(None, "a" * 64, id="missing-title"),
        pytest.param("   ", "a" * 64, id="blank-title"),
        pytest.param("Invalid", "not-a-hash", id="invalid-content-hash"),
    ],
)
async def test_retrieve_rejects_invalid_chunk_metadata(
    title: str | None, content_hash: str
) -> None:
    raw_retriever = _FakeRetriever(
        [
            _result(
                chunk_id="invalid-metadata",
                source_id=f"{DOCS}/invalid",
                title=title,
                score=0.5,
                start_offset=0,
                end_offset=7,
                content_hash=content_hash,
            )
        ]
    )

    with pytest.raises(RetrievalError, match="invalid-metadata") as caught:
        await DocumentationRetriever(raw_retriever).retrieve(
            RetrievalRequest(query="valid", top_k=1)
        )

    assert isinstance(caught.value.__cause__, ValidationError)


async def test_retrieve_rejects_missing_chunk_offsets() -> None:
    raw_retriever = _FakeRetriever(
        [
            _result(
                chunk_id="missing-offsets",
                source_id=f"{DOCS}/invalid",
                title="Invalid",
                score=0.5,
                start_offset=None,
                end_offset=None,
            )
        ]
    )

    with pytest.raises(RetrievalError, match="missing-offsets") as caught:
        await DocumentationRetriever(raw_retriever).retrieve(
            RetrievalRequest(query="valid", top_k=1)
        )

    assert isinstance(caught.value.__cause__, ValidationError)


async def test_retrieve_rejects_reversed_chunk_offsets() -> None:
    raw_retriever = _FakeRetriever(
        [
            _result(
                chunk_id="reversed-offsets",
                source_id=f"{DOCS}/invalid",
                title="Invalid",
                score=0.5,
                start_offset=10,
                end_offset=3,
            )
        ]
    )

    with pytest.raises(RetrievalError, match="reversed-offsets") as caught:
        await DocumentationRetriever(raw_retriever).retrieve(
            RetrievalRequest(query="valid", top_k=1)
        )

    assert isinstance(caught.value.__cause__, ValidationError)


async def test_retrieve_propagates_external_failure() -> None:
    failure = RuntimeError("Vespa unavailable")
    retriever = DocumentationRetriever(_FailingRetriever(failure))

    with pytest.raises(RuntimeError, match="Vespa unavailable") as caught:
        await retriever.retrieve(RetrievalRequest(query="valid", top_k=1))

    assert caught.value is failure


def _result(
    *,
    chunk_id: str,
    source_id: str,
    title: str | None,
    score: float,
    start_offset: int | None,
    end_offset: int | None,
    content_hash: str = "a" * 64,
) -> SearchResult:
    metadata = {"content_hash": content_hash}
    if title is not None:
        metadata["title"] = title
    return SearchResult(
        chunk=SearchResultChunk(
            id=chunk_id,
            source_id=source_id,
            locator=f"char:{start_offset}-{end_offset}",
            start_offset=start_offset,
            end_offset=end_offset,
            chunk_type=ChunkType.CONTENT,
            content="Content",
            metadata=metadata,
        ),
        score=score,
    )
