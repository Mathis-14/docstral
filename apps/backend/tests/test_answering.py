import json

import pytest
from docstral_backend import (
    AnsweringError,
    DocumentationAnswerer,
    RetrievalRequest,
    RetrievalResponse,
    RetrievedChunk,
)
from docstral_backend.answering import _AnswerDraft
from mistralai.search.toolkit.llm.chat import ChatMessage, ChatParseResult
from pydantic import ValidationError

DOCS = "https://docs.mistral.ai"
EXPECTED_ABSTENTION = (
    "I couldn't find enough information in the Mistral documentation to answer "
    "this question."
)


class _FakeRetriever:
    def __init__(
        self,
        chunks: tuple[RetrievedChunk, ...] = (),
        *,
        error: RuntimeError | None = None,
    ) -> None:
        self.chunks = chunks
        self.error = error
        self.calls: list[RetrievalRequest] = []

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResponse:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return RetrievalResponse(query=request.query, chunks=self.chunks)


class _FakeChat:
    def __init__(
        self,
        draft: _AnswerDraft | None = None,
        *,
        error: RuntimeError | None = None,
    ) -> None:
        self.draft = draft
        self.error = error
        self.calls = 0
        self.response_format: type[_AnswerDraft] | None = None
        self.model: str | None = None
        self.messages: list[ChatMessage] | None = None
        self.temperature: float | None = None
        self.max_tokens: int | None = None

    async def parse_chat(
        self,
        *,
        model: str,
        messages: list[ChatMessage],
        response_format: type[_AnswerDraft],
        temperature: float,
        max_tokens: int,
    ) -> ChatParseResult[_AnswerDraft]:
        self.calls += 1
        self.response_format = response_format
        self.model = model
        self.messages = messages
        self.temperature = temperature
        self.max_tokens = max_tokens
        if self.error is not None:
            raise self.error
        if self.draft is None:
            raise AssertionError("test chat has no configured draft")
        return ChatParseResult(parsed=self.draft, total_tokens=10)


async def test_answer_uses_structured_evidence_and_builds_page_citations() -> None:
    shared_url = f"{DOCS}/shared"
    chunks = (
        _chunk("chunk-1", shared_url, "Shared", "First"),
        _chunk("chunk-2", shared_url, "Shared", "Second"),
        _chunk("chunk-3", f"{DOCS}/other", "Other", "Third"),
    )
    retriever = _FakeRetriever(chunks)
    chat = _FakeChat(
        _AnswerDraft(answer="Grounded answer.", evidence_ids=("E3", "E1", "E2"))
    )

    response = await DocumentationAnswerer(retriever, chat, top_k=7).answer(
        "How does it work?"
    )

    assert retriever.calls == [RetrievalRequest(query="How does it work?", top_k=7)]
    assert response.answer == "Grounded answer."
    assert response.abstained is False
    assert [(citation.title, str(citation.url)) for citation in response.citations] == [
        ("Other", f"{DOCS}/other"),
        ("Shared", shared_url),
    ]
    assert chat.response_format is _AnswerDraft
    assert chat.model == "ministral-8b-2512"
    assert chat.temperature == 0.0
    assert chat.max_tokens == 1024
    assert chat.messages is not None
    system_message, user_message = chat.messages
    assert system_message.role == "system"
    assert isinstance(system_message.content, str)
    assert "untrusted data" in system_message.content
    assert user_message.role == "user"
    assert isinstance(user_message.content, str)
    assert json.loads(user_message.content) == {
        "question": "How does it work?",
        "evidence": [
            {"id": "E1", "title": "Shared", "content": "First"},
            {"id": "E2", "title": "Shared", "content": "Second"},
            {"id": "E3", "title": "Other", "content": "Third"},
        ],
    }


async def test_answer_uses_selected_model() -> None:
    chunk = _chunk("chunk-1", f"{DOCS}/page", "Page", "Content")
    chat = _FakeChat(_AnswerDraft(answer="Grounded answer.", evidence_ids=("E1",)))

    await DocumentationAnswerer(
        _FakeRetriever((chunk,)), chat, top_k=1, model="mistral-small-2603"
    ).answer("Question?")

    assert chat.model == "mistral-small-2603"


async def test_answer_abstains_without_calling_chat_when_no_chunks() -> None:
    chat = _FakeChat()

    response = await DocumentationAnswerer(_FakeRetriever(), chat, top_k=5).answer(
        "Unknown?"
    )

    assert response.answer == EXPECTED_ABSTENTION
    assert response.abstained is True
    assert response.citations == ()
    assert chat.calls == 0


async def test_answer_uses_fixed_abstention_for_model_abstention() -> None:
    chunk = _chunk("chunk-1", f"{DOCS}/page", "Page", "Content")
    chat = _FakeChat(_AnswerDraft(answer="", evidence_ids=()))

    response = await DocumentationAnswerer(
        _FakeRetriever((chunk,)), chat, top_k=1
    ).answer("Unknown?")

    assert response.answer == EXPECTED_ABSTENTION
    assert response.abstained is True
    assert response.citations == ()


async def test_answer_rejects_unknown_evidence() -> None:
    chunk = _chunk("chunk-1", f"{DOCS}/page", "Page", "Content")
    chat = _FakeChat(_AnswerDraft(answer="Unsupported.", evidence_ids=("E2",)))

    with pytest.raises(AnsweringError, match="unknown evidence 'E2'"):
        await DocumentationAnswerer(_FakeRetriever((chunk,)), chat, top_k=1).answer(
            "Question?"
        )


async def test_answer_propagates_retrieval_failure() -> None:
    failure = RuntimeError("Vespa unavailable")
    chat = _FakeChat()

    with pytest.raises(RuntimeError, match="Vespa unavailable") as caught:
        await DocumentationAnswerer(
            _FakeRetriever(error=failure), chat, top_k=1
        ).answer("Question?")

    assert caught.value is failure
    assert chat.calls == 0


async def test_answer_propagates_chat_failure() -> None:
    failure = RuntimeError("Mistral unavailable")
    chunk = _chunk("chunk-1", f"{DOCS}/page", "Page", "Content")

    with pytest.raises(RuntimeError, match="Mistral unavailable") as caught:
        await DocumentationAnswerer(
            _FakeRetriever((chunk,)), _FakeChat(error=failure), top_k=1
        ).answer("Question?")

    assert caught.value is failure


@pytest.mark.parametrize("question", ["", " ", "\n\t"])
async def test_answer_rejects_blank_question(question: str) -> None:
    retriever = _FakeRetriever()

    with pytest.raises(ValidationError, match="query must not be blank"):
        await DocumentationAnswerer(retriever, _FakeChat(), top_k=1).answer(question)

    assert retriever.calls == []


@pytest.mark.parametrize(
    ("answer", "evidence_ids"),
    [
        pytest.param("", ("E1",), id="abstention-with-evidence"),
        pytest.param("answer", (), id="answer-without-evidence"),
        pytest.param(" ", (), id="blank-answer"),
    ],
)
def test_answer_draft_rejects_inconsistent_state(
    answer: str, evidence_ids: tuple[str, ...]
) -> None:
    with pytest.raises(ValidationError):
        _AnswerDraft(answer=answer, evidence_ids=evidence_ids)


def test_answerer_rejects_invalid_top_k() -> None:
    with pytest.raises(ValueError, match="top_k must be at least 1"):
        DocumentationAnswerer(_FakeRetriever(), _FakeChat(), top_k=0)


def _chunk(chunk_id: str, source_id: str, title: str, content: str) -> RetrievedChunk:
    return RetrievedChunk(
        rank=1,
        id=chunk_id,
        source_id=source_id,
        title=title,
        content_hash="a" * 64,
        locator="char:0-7",
        start_offset=0,
        end_offset=7,
        content=content,
        score=0.5,
    )
