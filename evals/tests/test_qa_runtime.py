import json
from importlib.util import find_spec
from pathlib import Path

import httpx
import pytest
from docstral_backend import (
    DocumentationAnswerer,
    RetrievalRequest,
    RetrievalResponse,
)
from mistralai.client import Mistral
from mistralai.client.errors import SDKError
from mistralai.search.toolkit.llm.mistral import MistralChat

from evals.qa_runtime import (
    CaseResult,
    HTTPExchange,
    RecordingRetriever,
    TraceTransport,
    is_rate_limit,
    record_score,
    score_case,
)
from evals.retrieval_dataset import NegativeQuestion, PositiveQuestion
from evals.tests.helpers import make_chunk, negative_payload, positive_payload


class _VespaBoundary:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[RetrievalRequest] = []
        self.fail = fail

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResponse:
        self.calls.append(request)
        if self.fail:
            raise RuntimeError("Vespa unavailable")
        return RetrievalResponse(
            query=request.query,
            chunks=(
                make_chunk(
                    rank=1,
                    source_id="https://docs.mistral.ai/guide",
                    content="Evidence",
                ),
            ),
        )


def _completion(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "test-completion",
            "object": "chat.completion",
            "created": 1,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(payload)},
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        },
    )


async def test_capture_runs_real_answerer_without_gold_and_preserves_labels(
    tmp_path: Path,
) -> None:
    trace = TraceTransport(
        httpx.MockTransport(
            lambda _: _completion(
                {"answer": "Supported answer", "evidence_ids": ["E1"]}
            )
        ),
        tmp_path / "http.jsonl",
    )
    trace.question_id, trace.step = "candidate-001", "generation"
    boundary = _VespaBoundary()
    recorder = RecordingRetriever(boundary)
    question = PositiveQuestion.model_validate(positive_payload(claim="GOLD_ONLY"))
    async with httpx.AsyncClient(transport=trace) as http:
        async with Mistral(api_key="test-secret", async_client=http) as sdk:
            response = await DocumentationAnswerer(
                recorder, MistralChat(client=sdk), top_k=5
            ).answer(question.query)
    assert boundary.calls == [RetrievalRequest(query=question.query, top_k=5)]
    assert response.answer == "Supported answer"
    assert str(response.citations[0].url) == recorder.chunks[0].source_id
    raw = (tmp_path / "http.jsonl").read_text()
    assert "test-secret" not in raw and "GOLD_ONLY" not in raw
    assert "E1" in raw and "Evidence" in raw
    exchange = HTTPExchange.model_validate_json(raw)
    assert exchange.status_code == 200
    assert exchange.step == "generation"


async def test_recorder_propagates_retrieval_failure() -> None:
    recorder = RecordingRetriever(_VespaBoundary(fail=True))
    with pytest.raises(RuntimeError, match="Vespa unavailable"):
        await recorder.retrieve(RetrievalRequest(query="Question", top_k=5))
    assert recorder.chunks == ()


async def test_transport_records_failed_attempt_without_hiding_error(
    tmp_path: Path,
) -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unavailable", request=request)

    trace = TraceTransport(httpx.MockTransport(unavailable), tmp_path / "http.jsonl")
    async with httpx.AsyncClient(transport=trace) as http:
        with pytest.raises(httpx.ConnectError):
            await http.post(
                "https://api.mistral.ai/v1/chat/completions", json={"model": "test"}
            )
    exchange = HTTPExchange.model_validate_json((tmp_path / "http.jsonl").read_text())
    assert exchange.status_code is None
    assert exchange.response == {"transport_error": "ConnectError"}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_score_is_explicitly_undefined(value: float) -> None:
    score = record_score("candidate-001", "faithfulness", value)
    assert score.status == "undefined" and score.value is None
    assert score.reason
    assert "NaN" not in score.model_dump_json()


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
def test_only_http_429_is_deferred_through_sdk_wrappers(status: int) -> None:
    from openai import APIStatusError

    response = httpx.Response(
        status, request=httpx.Request("POST", "https://api.mistral.ai/v1/test")
    )
    for error in (
        SDKError("SDK error", response),
        APIStatusError("SDK error", response=response, body=None),
    ):
        wrapper = RuntimeError("Provider failed")
        wrapper.__cause__ = error
        assert is_rate_limit(wrapper) == (status == 429)
    assert not is_rate_limit(ValueError("Invalid JSON containing 429"))


async def test_native_ragas_receives_context_and_reference_separately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if find_spec("ragas") is None:
        pytest.skip("Install the uv eval group")
    monkeypatch.setenv("RAGAS_DO_NOT_TRACK", "true")
    import openai

    def judge(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        schema = body["messages"][0]["content"]
        assert body["model"] == "mistral-medium-3-5"
        assert body["temperature"] == 0 and body["top_p"] == 1
        assert body["max_tokens"] == 4096
        if '"verdict"' in schema:
            return _completion(
                {
                    "statements": [
                        {"statement": "Evidence", "reason": "Supported", "verdict": 1}
                    ]
                }
            )
        if '"claims"' in schema:
            return _completion({"claims": ["Evidence"]})
        return _completion({"statements": ["Evidence"]})

    question = PositiveQuestion.model_validate(positive_payload(claim="GOLD_ONLY"))
    boundary = _VespaBoundary()
    chunks = (
        await boundary.retrieve(RetrievalRequest(query=question.query, top_k=5))
    ).chunks
    from docstral_backend import AnswerResponse, Citation

    case = CaseResult(
        question=question,
        reference="GOLD_ONLY",
        chunks=chunks,
        response=AnswerResponse(
            answer="Evidence",
            abstained=False,
            citations=(
                Citation.model_validate({"title": "Guide", "url": chunks[0].source_id}),
            ),
        ),
        duration_seconds=0.0,
    )
    trace = TraceTransport(httpx.MockTransport(judge), tmp_path / "http.jsonl")
    async with httpx.AsyncClient(transport=trace) as http:
        async with openai.AsyncOpenAI(
            api_key="test-secret",
            base_url="https://api.mistral.ai/v1",
            http_client=http,
            max_retries=0,
        ) as client:
            scores = await score_case(case, client, trace)
            assert all(score.status == "ok" and score.value == 1.0 for score in scores)
            negative = NegativeQuestion.model_validate(negative_payload())
            skipped = await score_case(
                case.model_copy(update={"question": negative, "reference": None}),
                client,
                trace,
            )
            assert all(
                score.status == "skipped" and score.value is None for score in skipped
            )
            from docstral_backend.answering import _ABSTENTION_MESSAGE

            abstained = await score_case(
                case.model_copy(
                    update={
                        "response": AnswerResponse(
                            answer=_ABSTENTION_MESSAGE, abstained=True, citations=()
                        )
                    }
                ),
                client,
                trace,
            )
            assert all(
                score.status == "skipped" and score.value is None for score in abstained
            )
    exchanges = [
        HTTPExchange.model_validate_json(line)
        for line in trace.destination.read_text().splitlines()
    ]
    assert len(exchanges) == 10
    assert all("GOLD_ONLY" not in json.dumps(x.request) for x in exchanges[:2])
    for metric in ("factual_correctness_f1", "factual_correctness_recall"):
        requests = [x.request for x in exchanges if x.step == metric]
        assert len(requests) == 4
        assert any("GOLD_ONLY" in json.dumps(request) for request in requests)
