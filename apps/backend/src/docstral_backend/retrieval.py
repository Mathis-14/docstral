"""Retrieve ordered documentation chunks from the shared Vespa index."""

from typing import Protocol, Self

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
    field_validator,
    model_validator,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class RetrievalError(Exception):
    """A retrieved chunk does not satisfy Docstral's index contract."""


class RetrievalRequest(BaseModel):
    """One documentation retrieval request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str
    top_k: int = Field(ge=1)

    @field_validator("query")
    @classmethod
    def _query_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value


class RetrievedChunk(BaseModel):
    """A ranked documentation chunk safe to expose outside the toolkit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rank: int = Field(ge=1)
    id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content_hash: str = Field(pattern=_SHA256_PATTERN)
    locator: str = Field(min_length=1)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)
    content: str = Field(min_length=1)
    score: float = Field(allow_inf_nan=False)

    @field_validator("title")
    @classmethod
    def _title_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value

    @model_validator(mode="after")
    def _offsets_must_be_ordered(self) -> Self:
        if self.start_offset > self.end_offset:
            raise ValueError("start_offset must not exceed end_offset")
        return self


class RetrievalResponse(BaseModel):
    """Ordered chunks returned for the original query."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str
    chunks: tuple[RetrievedChunk, ...]


class _IndexedChunkMetadata(BaseModel):
    """Required Docstral fields within toolkit-owned chunk metadata."""

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
    """Project toolkit results onto Docstral's stable retrieval boundary."""

    def __init__(self, retriever: _SearchRetriever) -> None:
        self._retriever = retriever

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResponse:
        """Retrieve chunks without changing their ranking or source multiplicity."""
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
    """Build the baseline retriever against the shared Docstral Vespa index."""
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
