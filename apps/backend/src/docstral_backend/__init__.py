"""Read-only documentation Q&A for Docstral."""

from docstral_backend.answering import (
    AnsweringError,
    AnswerResponse,
    Citation,
    DocumentationAnswerer,
)
from docstral_backend.retrieval import (
    DocumentationRetriever,
    RetrievalError,
    RetrievalRequest,
    RetrievalResponse,
    RetrievedChunk,
    build_documentation_retriever,
)

__all__ = [
    "AnswerResponse",
    "AnsweringError",
    "Citation",
    "DocumentationAnswerer",
    "DocumentationRetriever",
    "RetrievalError",
    "RetrievalRequest",
    "RetrievalResponse",
    "RetrievedChunk",
    "build_documentation_retriever",
]
