import json
from importlib.resources import files
from typing import Protocol

from mistralai.search.toolkit.llm.chat import ChatMessage, ChatParseResult
from mistralai.search.toolkit.llm.mistral import MistralChat

from docstral_mcp.config import DEFAULT_ANSWER_MODEL, MAX_ANSWER_TOKENS
from docstral_mcp.qa.models import (
    _ABSTENTION_MESSAGE,
    AnswerResponse,
    Citation,
    RetrievalRequest,
    RetrievalResponse,
    RetrievedChunk,
    _AnswerDraft,
)
from docstral_mcp.qa.retrieval import build_documentation_retriever


class AnsweringError(Exception):
    pass


class _DocumentationRetriever(Protocol):
    async def retrieve(self, request: RetrievalRequest) -> RetrievalResponse: ...


class _Chat(Protocol):
    async def parse_chat(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        response_format: type[_AnswerDraft],
        temperature: float,
        max_tokens: int,
    ) -> ChatParseResult[_AnswerDraft]: ...


class DocumentationAnswerer:
    def __init__(
        self,
        retriever: _DocumentationRetriever,
        chat: _Chat,
        *,
        top_k: int,
        model: str = DEFAULT_ANSWER_MODEL,
    ) -> None:
        if top_k < 1:
            raise ValueError("top_k must be at least 1")
        self._retriever = retriever
        self._chat = chat
        self._top_k = top_k
        self._model = model
        self._system_prompt = _load_system_prompt()

    async def answer(self, question: str) -> AnswerResponse:
        retrieval = await self._retriever.retrieve(
            RetrievalRequest(query=question, top_k=self._top_k)
        )
        if not retrieval.chunks:
            return _abstention()

        result = await self._chat.parse_chat(
            model=self._model,
            messages=[
                ChatMessage(role="system", content=self._system_prompt),
                ChatMessage(
                    role="user",
                    content=_question_with_evidence(question, retrieval.chunks),
                ),
            ],
            response_format=_AnswerDraft,
            temperature=0.0,
            max_tokens=MAX_ANSWER_TOKENS,
        )
        draft = result.parsed
        if not draft.answer:
            return _abstention()

        return AnswerResponse(
            answer=draft.answer,
            abstained=False,
            citations=_citations(draft.evidence_ids, retrieval.chunks),
        )


def build_documentation_answerer(
    *, vespa_endpoint: str, top_k: int, model: str = DEFAULT_ANSWER_MODEL
) -> DocumentationAnswerer:
    return DocumentationAnswerer(
        build_documentation_retriever(vespa_endpoint=vespa_endpoint),
        MistralChat(),
        top_k=top_k,
        model=model,
    )


def _question_with_evidence(question: str, chunks: tuple[RetrievedChunk, ...]) -> str:
    evidence = [
        {
            "id": _evidence_id(position),
            "title": chunk.title,
            "content": chunk.content,
        }
        for position, chunk in enumerate(chunks, start=1)
    ]
    return json.dumps(
        {"question": question, "evidence": evidence},
        ensure_ascii=False,
    )


def _citations(
    evidence_ids: tuple[str, ...], chunks: tuple[RetrievedChunk, ...]
) -> tuple[Citation, ...]:
    chunks_by_evidence = {
        _evidence_id(position): chunk for position, chunk in enumerate(chunks, start=1)
    }
    citations: list[Citation] = []
    cited_urls: set[str] = set()
    for evidence_id in evidence_ids:
        chunk = chunks_by_evidence.get(evidence_id)
        if chunk is None:
            raise AnsweringError(
                f"Answer model referenced unknown evidence {evidence_id!r}"
            )
        if chunk.source_id in cited_urls:
            continue
        citations.append(
            Citation.model_validate({"title": chunk.title, "url": chunk.source_id})
        )
        cited_urls.add(chunk.source_id)
    return tuple(citations)


def _evidence_id(position: int) -> str:
    return f"E{position}"


def _abstention() -> AnswerResponse:
    return AnswerResponse(
        answer=_ABSTENTION_MESSAGE,
        abstained=True,
        citations=(),
    )


def _load_system_prompt() -> str:
    try:
        content = (
            files("docstral_mcp.qa").joinpath("prompt.md").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError) as error:
        raise AnsweringError(
            "Cannot read the bundled Q&A prompt; reinstall or rebuild docstral-mcp"
        ) from error
    if not content.strip():
        raise AnsweringError(
            "The bundled Q&A prompt is empty; reinstall or rebuild docstral-mcp"
        )
    return content
