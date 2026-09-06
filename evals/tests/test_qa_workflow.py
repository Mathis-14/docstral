"""Exercise the command's whole workflow; only Mistral and Vespa are replaced."""

import json
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
from instructor.core.exceptions import InstructorRetryException
from mistralai.search.toolkit.llm.mistral import MistralLLMException
from mistralai.search.toolkit.plugins.vespa.client import VespaClient
from pydantic import JsonValue
from vespa.io import VespaQueryResponse

from evals.qa_dataset import DatasetFreeze
from evals.qa_metrics import QASummary
from evals.qa_runtime import (
    METRICS,
    CaseResult,
    HTTPExchange,
    MetricScore,
    read_records,
)
from evals.run_qa import QAConfig, QARunManifest, RunAttempt, run
from evals.tests.test_qa_dataset import frozen_fixture


class Services:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.requests: list[dict[str, object]] = []
        self.invalid_answer = "missing_labels"
        self.fail_judge = False
        self.fail_generation: str | None = None
        self.rate_limit_generation: str | None = None
        self.rate_limit_embedding: str | None = None
        self.rate_limit_judge = False
        self.rate_limit_judge_at: int | None = None
        self.undefined = False

    def http(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        model = body["model"]
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if model == "mistral-embed":
            if self.rate_limit_embedding in body["input"]:
                return httpx.Response(429, json={"message": "Rate limited"})
            return httpx.Response(
                200,
                json={
                    "id": "embed",
                    "object": "list",
                    "model": model,
                    "data": [
                        {"object": "embedding", "index": 0, "embedding": [0.1] * 1024}
                    ],
                    "usage": {"prompt_tokens": 10, "total_tokens": 10},
                },
            )
        if model == "mistral-small-2603":
            query = json.loads(body["messages"][1]["content"])["question"]
            if self.fail_generation == query:
                return httpx.Response(401, json={"message": "Unauthorized"})
            if self.rate_limit_generation == query:
                return httpx.Response(429, json={"message": "Rate limited"})
            payload: dict[str, object] = {"answer": "Evidence", "evidence_ids": ["E1"]}
            if query == "First question":
                if self.invalid_answer == "missing_labels":
                    payload["evidence_ids"] = []
                elif self.invalid_answer == "unknown_label":
                    payload["evidence_ids"] = ["E99"]
            elif query == "Unknown question":
                payload = {"answer": "", "evidence_ids": []}
        else:
            assert model == "mistral-medium-3-5"
            schema = body["messages"][0]["content"]
            judge_calls = sum(r["model"] == model for r in self.requests)
            if (self.rate_limit_judge and '"claims"' in schema) or (
                judge_calls == self.rate_limit_judge_at
            ):
                return httpx.Response(429, json={"message": "Rate limited"})
            if self.fail_judge and '"claims"' in schema:
                raise httpx.ConnectError("offline outage", request=request)
            if '"verdict"' in schema:
                payload = {
                    "statements": [
                        {"statement": "Evidence", "reason": "Supported", "verdict": 1}
                    ]
                }
            elif '"claims"' in schema:
                payload = {"claims": ["Evidence"]}
            else:
                payload = {"statements": [] if self.undefined else ["Evidence"]}
        content = json.dumps(payload)
        if (
            model == "mistral-small-2603"
            and query == "First question"
            and self.invalid_answer == "malformed_json"
        ):
            content = '{"answer":'
        return httpx.Response(
            200,
            json={
                "id": "unit",
                "object": "chat.completion",
                "created": 1,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ],
                "usage": usage,
            },
        )


def workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[QAConfig, Services]:
    dataset, corpus, freeze_path = frozen_fixture(tmp_path)
    rows = [json.loads(line) for line in dataset.read_text().splitlines()]
    rows[0]["question"]["query"] = "First question"
    rows[0]["reference"] = "GOLD_ONLY"
    second = json.loads(json.dumps(rows[0]))
    second["question"].update(id="candidate-002", query="Second question")
    rows.insert(1, second)
    dataset.write_text("".join(json.dumps(row) + "\n" for row in rows))
    freeze = DatasetFreeze.model_validate_json(freeze_path.read_bytes())
    freeze_path.write_text(
        freeze.model_copy(
            update={
                "dataset_sha256": sha256(dataset.read_bytes()).hexdigest(),
                "positive_count": 2,
            }
        ).model_dump_json()
    )
    services = Services()

    async def query(
        self: VespaClient, body: dict[str, JsonValue]
    ) -> VespaQueryResponse:
        assert isinstance(body["query"], str)
        services.queries.append(body["query"])
        embedding = body["input.query(embedding)"]
        assert (
            body["hits"] == 5 and isinstance(embedding, list) and len(embedding) == 1024
        )
        fields = {
            "id": "chunk-1",
            "source_id": "https://docs.mistral.ai/guide",
            "locator": "char:9-17",
            "start_offset": 9,
            "end_offset": 17,
            "chunk_type": "content",
            "content": "Evidence",
            "metadata": json.dumps(
                {
                    "title": "Guide",
                    "content_hash": sha256(
                        (corpus / "guide.md").read_bytes()
                    ).hexdigest(),
                }
            ),
        }
        return VespaQueryResponse(
            json={
                "root": {
                    "children": [{"id": "chunk-1", "relevance": 1, "fields": fields}]
                }
            },
            status_code=200,
            url="http://localhost:8080/search/",
        )

    monkeypatch.setenv("MISTRAL_API_KEY", "test-secret")
    monkeypatch.setenv("RAGAS_DO_NOT_TRACK", "true")
    monkeypatch.setattr(
        httpx, "AsyncHTTPTransport", lambda: httpx.MockTransport(services.http)
    )
    monkeypatch.setattr(VespaClient, "query", query)
    config = QAConfig(
        output_dir=tmp_path / "run",
        freeze_path=freeze_path,
        dataset=dataset,
        corpus_dir=corpus,
        http_delay_seconds=0,
    )
    return config, services


def load_manifest(config: QAConfig) -> QARunManifest:
    return QARunManifest.model_validate_json(
        (config.output_dir / "run.json").read_bytes()
    )


@pytest.mark.parametrize(
    "invalid", ["missing_labels", "unknown_label", "malformed_json"]
)
async def test_full_workflow_records_invalid_output_then_judges_valid_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid: str,
) -> None:
    config, services = workflow(tmp_path, monkeypatch)
    services.invalid_answer = invalid
    await run(config)
    results = read_records(config.output_dir / "answers.jsonl", CaseResult)
    assert [r.status for r in results] == ["generation_error", "answered", "abstained"]
    assert results[0].chunks and results[0].generation_error
    assert services.queries == ["First question", "Second question", "Unknown question"]
    scores = read_records(config.output_dir / "scores.jsonl", MetricScore)
    assert len(scores) == 9
    assert sum(s.status == "ok" for s in scores) == 3
    summary = QASummary.model_validate_json(
        (config.output_dir / "summary.json").read_bytes()
    )
    assert summary.positive_generation_errors == 1
    assert summary.positive_count == 2 and summary.negative_abstentions == 1
    assert (
        summary.retrieval is not None
        and summary.retrieval.overall[0].question_count == 2
    )
    assert load_manifest(config).status == "completed"
    assert summary.processed_questions == 3
    assert summary.metrics["faithfulness"].mean == 1.0
    assert {path.name for path in config.output_dir.iterdir()} == {
        "answers.jsonl",
        "scores.jsonl",
        "summary.json",
        "run.json",
        "questions.jsonl",
        "attempts.jsonl",
        "http.jsonl",
    }
    for body in services.requests:
        if body["model"] != "mistral-medium-3-5":
            assert "GOLD_ONLY" not in json.dumps(body)
    assert "test-secret" not in (config.output_dir / "http.jsonl").read_text()
    with pytest.raises(ValueError, match="already completed"):
        await run(config, resume=True)


async def test_undefined_metric_does_not_block_other_scores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, services = workflow(tmp_path, monkeypatch)
    services.undefined = True
    await run(config)
    scores = read_records(config.output_dir / "scores.jsonl", MetricScore)
    assert len(scores) == 9 and sum(s.status == "undefined" for s in scores) == 1
    assert sum(s.status == "ok" for s in scores) == 2
    assert load_manifest(config).status == "completed"
    summary = QASummary.model_validate_json(
        (config.output_dir / "summary.json").read_bytes()
    )
    assert summary.metrics["faithfulness"].undefined == 1


async def test_api_error_stops_without_becoming_a_generation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, services = workflow(tmp_path, monkeypatch)
    services.fail_generation = "First question"
    with pytest.raises(MistralLLMException) as caught:
        await run(config)
    assert getattr(caught.value.__cause__, "status_code", None) == 401
    assert read_records(config.output_dir / "answers.jsonl", CaseResult) == ()
    assert services.queries == ["First question"]
    attempt = read_records(config.output_dir / "attempts.jsonl", RunAttempt)[-1]
    assert (
        attempt.status == "incomplete" and attempt.error_type == "MistralLLMException"
    )
    summary = QASummary.model_validate_json(
        (config.output_dir / "summary.json").read_bytes()
    )
    assert summary.processed_questions == 0
    assert all(metric.missing == 3 for metric in summary.metrics.values())


async def test_resume_after_judge_outage_keeps_all_generation_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, services = workflow(tmp_path, monkeypatch)
    services.fail_judge = True
    services.undefined = True
    with pytest.raises(InstructorRetryException):
        await run(config)
    before = (config.output_dir / "answers.jsonl").read_bytes()
    scored_before = (config.output_dir / "scores.jsonl").read_bytes()
    services.fail_judge = False
    await run(config, resume=True)
    assert (config.output_dir / "answers.jsonl").read_bytes() == before
    assert (config.output_dir / "scores.jsonl").read_bytes().startswith(scored_before)
    scores = read_records(config.output_dir / "scores.jsonl", MetricScore)
    assert sum(s.status == "undefined" for s in scores) == 1
    assert services.queries == ["First question", "Second question", "Unknown question"]
    assert len(read_records(config.output_dir / "scores.jsonl", MetricScore)) == 9
    assert load_manifest(config).status == "completed"


async def test_resume_generation_does_not_retry_an_invalid_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, services = workflow(tmp_path, monkeypatch)
    services.fail_generation = "Second question"
    with pytest.raises(MistralLLMException):
        await run(config)
    saved = (config.output_dir / "answers.jsonl").read_bytes()
    assert len(read_records(config.output_dir / "answers.jsonl", CaseResult)) == 1
    services.fail_generation = None
    await run(config, resume=True)
    assert (config.output_dir / "answers.jsonl").read_bytes().startswith(saved)
    assert services.queries == [
        "First question",
        "Second question",
        "Second question",
        "Unknown question",
    ]


async def test_resume_rejects_changed_artifacts_before_any_service_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, services = workflow(tmp_path, monkeypatch)
    services.fail_generation = "Second question"
    with pytest.raises(MistralLLMException):
        await run(config)
    path = config.output_dir / "answers.jsonl"
    path.write_bytes(path.read_bytes() + b"\n")
    previous_queries = services.queries.copy()
    with pytest.raises(ValueError, match="Saved answers or scores changed"):
        await run(config, resume=True)
    assert services.queries == previous_queries


async def test_resume_rejects_changed_local_freeze_before_any_service_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, services = workflow(tmp_path, monkeypatch)
    services.fail_generation = "Second question"
    with pytest.raises(MistralLLMException):
        await run(config)
    config.freeze_path.write_bytes(config.freeze_path.read_bytes() + b"\n")
    previous_requests = len(services.requests)
    with pytest.raises(ValueError, match="Resume configuration"):
        await run(config, resume=True)
    assert len(services.requests) == previous_requests


@pytest.mark.parametrize("stage", ["embedding", "generation"])
async def test_rate_limited_question_leaves_hole_and_resumes_only_missing_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    config, services = workflow(tmp_path, monkeypatch)
    services.invalid_answer = "valid"
    if stage == "embedding":
        services.rate_limit_embedding = "First question"
    else:
        services.rate_limit_generation = "First question"
    with pytest.raises(RuntimeError, match="HTTP 429 left 1 questions"):
        await run(config)
    answers_path, scores_path = (
        config.output_dir / "answers.jsonl",
        config.output_dir / "scores.jsonl",
    )
    before_answers, before_scores = answers_path.read_bytes(), scores_path.read_bytes()
    assert [r.question.id for r in read_records(answers_path, CaseResult)] == [
        "candidate-002",
        "negative-candidate-001",
    ]
    assert len(read_records(scores_path, MetricScore)) == 6
    summary = QASummary.model_validate_json(
        (config.output_dir / "summary.json").read_bytes()
    )
    assert all(metric.missing == 1 for metric in summary.metrics.values())
    assert summary.positive_generation_errors == 0
    assert load_manifest(config).status == "incomplete"
    requests_before = len(services.requests)
    services.rate_limit_embedding = services.rate_limit_generation = None
    await run(config, resume=True)
    assert answers_path.read_bytes().startswith(before_answers)
    assert scores_path.read_bytes().startswith(before_scores)
    answers = read_records(answers_path, CaseResult)
    assert [r.question.id for r in answers] == [
        "candidate-002",
        "negative-candidate-001",
        "candidate-001",
    ]
    assert len(read_records(scores_path, MetricScore)) == 9
    new_requests = services.requests[requests_before:]
    assert [r["model"] for r in new_requests].count("mistral-embed") == 1
    assert [r["model"] for r in new_requests].count("mistral-small-2603") == 1
    assert load_manifest(config).status == "completed"


async def test_judge_429_continues_other_metrics_and_questions_then_resumes_holes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, services = workflow(tmp_path, monkeypatch)
    services.invalid_answer = "valid"
    services.rate_limit_judge = True
    with pytest.raises(RuntimeError, match="HTTP 429 left 0 questions and 4 metrics"):
        await run(config)
    saved_answers = (config.output_dir / "answers.jsonl").read_bytes()
    scores_path = config.output_dir / "scores.jsonl"
    saved_scores = scores_path.read_bytes()
    scores = read_records(scores_path, MetricScore)
    assert len(scores) == 5
    assert [(s.question_id, s.metric) for s in scores if s.status == "ok"] == [
        ("candidate-001", "faithfulness"),
        ("candidate-002", "faithfulness"),
    ]
    assert sum(s.status == "skipped" for s in scores) == 3
    before_http = read_records(config.output_dir / "http.jsonl", HTTPExchange)
    assert any(exchange.status_code == 429 for exchange in before_http)
    # A repeated throttled pass must neither replay finished work nor mark it done.
    with pytest.raises(RuntimeError, match="HTTP 429"):
        await run(config, resume=True)
    assert (config.output_dir / "answers.jsonl").read_bytes() == saved_answers
    assert scores_path.read_bytes() == saved_scores
    services.rate_limit_judge = False
    await run(config, resume=True)
    assert (config.output_dir / "answers.jsonl").read_bytes() == saved_answers
    assert scores_path.read_bytes().startswith(saved_scores)
    assert len(read_records(scores_path, MetricScore)) == 9
    later_http = read_records(config.output_dir / "http.jsonl", HTTPExchange)[
        len(before_http) :
    ]
    assert all(exchange.step in METRICS[1:] for exchange in later_http)
    assert services.queries == ["First question", "Second question", "Unknown question"]
    assert load_manifest(config).status == "completed"


async def test_resume_restarts_interrupted_metric_but_not_completed_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, services = workflow(tmp_path, monkeypatch)
    # Faithfulness decomposes the answer, then the NLI call is rate limited.
    services.rate_limit_judge_at = 2
    with pytest.raises(RuntimeError, match="0 questions and 1 metrics"):
        await run(config)
    scores_path = config.output_dir / "scores.jsonl"
    before_scores = scores_path.read_bytes()
    assert len(read_records(scores_path, MetricScore)) == 8
    before_http = read_records(config.output_dir / "http.jsonl", HTTPExchange)
    await run(config, resume=True)
    after_http = read_records(config.output_dir / "http.jsonl", HTTPExchange)
    new_http = after_http[len(before_http) :]
    assert len(new_http) == 2
    assert all(
        exchange.question_id == "candidate-002"
        and exchange.step == "faithfulness"
        and exchange.status_code == 200
        for exchange in new_http
    )
    assert scores_path.read_bytes().startswith(before_scores)
    assert len(read_records(scores_path, MetricScore)) == 9
    assert load_manifest(config).status == "completed"
