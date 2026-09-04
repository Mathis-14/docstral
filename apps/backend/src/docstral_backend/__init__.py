"""Read-only documentation retrieval for Docstral."""

from docstral_backend.retrieval import (
    DocumentationRetriever,
    RetrievalError,
    RetrievalRequest,
    RetrievalResponse,
    RetrievedChunk,
    build_documentation_retriever,
)

__all__ = [
    "DocumentationRetriever",
    "RetrievalError",
    "RetrievalRequest",
    "RetrievalResponse",
    "RetrievedChunk",
    "build_documentation_retriever",
]
