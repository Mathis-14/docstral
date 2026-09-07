from typing import Protocol

from docstral_vespa import search_index
from mistralai.search.toolkit.embedding import (
    MODEL_1024_EMBEDDING,
    MistralEmbedder,
)
from mistralai.search.toolkit.retrieval.retrievers import VectorRetriever
from mistralai.search.toolkit.search import SearchResult
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
)

from docstral_mcp.qa.models import RetrievalRequest, RetrievalResponse, RetrievedChunk


class RetrievalError(Exception):
    pass


class _IndexedChunkMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    title: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)


class _SearchRetriever(Protocol):
    async def retrieve(
        self,
        query: str,
        top_k: int,
        include_metadata: bool,
        include_content: bool,
    ) -> list[SearchResult]: ...


class DocumentationRetriever:
    def __init__(self, retriever: _SearchRetriever) -> None:
        self._retriever = retriever

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResponse:
        results = await self._retriever.retrieve(
            query=request.query,
            top_k=request.top_k,
            include_metadata=True,
            include_content=True,
        )
        chunks = tuple(
            _to_retrieved_chunk(result, rank=rank)
            for rank, result in enumerate(results, start=1)
        )
        return RetrievalResponse(query=request.query, chunks=chunks)


def build_documentation_retriever(*, vespa_endpoint: str) -> DocumentationRetriever:
    index = search_index(vespa_endpoint)
    embedder = MistralEmbedder(model_name=MODEL_1024_EMBEDDING)
    return DocumentationRetriever(VectorRetriever(client=index, embedder=embedder))


def _to_retrieved_chunk(result: SearchResult, *, rank: int) -> RetrievedChunk:
    try:
        metadata = _IndexedChunkMetadata.model_validate(result.chunk.metadata)
        return RetrievedChunk.model_validate(
            {
                "rank": rank,
                "id": result.chunk.id,
                "source_id": result.chunk.source_id,
                "title": metadata.title,
                "content_hash": metadata.content_hash,
                "locator": result.chunk.locator,
                "start_offset": result.chunk.start_offset,
                "end_offset": result.chunk.end_offset,
                "content": result.chunk.content,
                "score": result.score,
            }
        )
    except ValidationError as exc:
        raise RetrievalError(
            f"Retrieved chunk {result.chunk.id!r} does not satisfy the "
            "Docstral index contract"
        ) from exc
