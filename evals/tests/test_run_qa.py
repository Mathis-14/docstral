import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from docstral_backend import (
    AnswerResponse,
    DocumentationAnswerer,
    RetrievalRequest,
    RetrievalResponse,
)
from mistralai.client import Mistral
from mistralai.search.toolkit.llm.mistral import MistralChat

from evals.qa_dataset import QACase
from evals.qa_metrics import summarize_answers
from evals.qa_runtime import (
    METRICS,
    CaseResult,
    HTTPExchange,
    MetricScore,
    RecordingRetriever,
    TraceTransport,
    read_records,
)
from evals.retrieval_dataset import NegativeQuestion, PositiveQuestion
from evals.run_qa import generate_answers, saved_fingerprints, validate_saved
from evals.tests.helpers import make_chunk, negative_payload, positive_payload


def test_cli_requires_explicit_freeze_before_starting_run(tmp_path: Path) -> None:
    output_dir = tmp_path / "run"
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "evals.run_qa",
            "--output-dir",
            str(output_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2 and "--freeze" in result.stderr
    assert not output_dir.exists()


class VespaBoundary:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.calls: list[str] = []
        self.fail_at = fail_at

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResponse:
        self.calls.append(request.query)
        if len(self.calls) == self.fail_at:
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


def completion(_: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "unit",
            "object": "chat.completion",
            "created": 1,
            "model": "mistral-small-2603",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {"answer": "Evidence", "evidence_ids": ["E1"]}
                        ),
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )


def cases() -> tuple[QACase, ...]:
    return tuple(
        QACase(
            question=PositiveQuestion.model_validate(
                positive_payload(question_id=f"candidate-{n:03}", query=f"Question {n}")
            ),
            reference="GOLD_ONLY",
        )
        for n in (1, 2)
    )


async def test_generation_failure_retains_prefix_and_resume_queries_only_missing(
    tmp_path: Path,
) -> None:
    trace = TraceTransport(httpx.MockTransport(completion), tmp_path / "http.jsonl")
    boundary = VespaBoundary(fail_at=2)
    recorder = RecordingRetriever(boundary)
    async with httpx.AsyncClient(transport=trace) as http:
        async with Mistral(api_key="test-secret", async_client=http) as sdk:
            answerer = DocumentationAnswerer(recorder, MistralChat(client=sdk), top_k=5)
            with pytest.raises(RuntimeError, match="Vespa unavailable"):
                await generate_answers(cases(), (), answerer, recorder, trace, tmp_path)
            saved = read_records(tmp_path / "answers.jsonl", CaseResult)
            assert len(saved) == 1
            prefix = (tmp_path / "answers.jsonl").read_bytes()
            validate_saved(cases(), saved, ())
            boundary.fail_at = None
            answers = await generate_answers(
                cases(), saved, answerer, recorder, trace, tmp_path
            )
    assert len(answers) == 2
    assert boundary.calls == ["Question 1", "Question 2", "Question 2"]
    assert (tmp_path / "answers.jsonl").read_bytes().startswith(prefix)
    trace_text = (tmp_path / "http.jsonl").read_text()
    assert "GOLD_ONLY" not in trace_text and "test-secret" not in trace_text
    assert len(read_records(tmp_path / "http.jsonl", HTTPExchange)) == 2
    assert not (tmp_path / "scores.jsonl").exists()
    fingerprints = saved_fingerprints(tmp_path)
    (tmp_path / "answers.jsonl").write_bytes(prefix)
    assert saved_fingerprints(tmp_path) != fingerprints


def answer(case: QACase, *, abstained: bool = False) -> CaseResult:
    from docstral_backend import Citation
    from docstral_backend.answering import _ABSTENTION_MESSAGE

    chunks = (
        make_chunk(
            rank=1, source_id="https://docs.mistral.ai/guide", content="Evidence"
        ),
    )
    response = AnswerResponse(
        answer=_ABSTENTION_MESSAGE if abstained else "Evidence",
        abstained=abstained,
        citations=()
        if abstained
        else (
            Citation.model_validate(
                {"title": "Guide", "url": "https://docs.mistral.ai/guide"}
            ),
        ),
    )
    return CaseResult(
        question=case.question,
        reference=case.reference,
        chunks=chunks,
        response=response,
        duration_seconds=0,
    )


def test_resume_rejects_reference_drift_duplicate_scores_and_wrong_skip() -> None:
    saved = (answer(cases()[0]),)
    score = MetricScore(
        question_id=saved[0].question.id, metric=METRICS[0], status="ok", value=1.0
    )
    validate_saved(cases(), saved, (score,))
    with pytest.raises(ValueError, match="frozen question/reference"):
        validate_saved(
            cases(), (saved[0].model_copy(update={"reference": "Changed"}),), ()
        )
    with pytest.raises(ValueError, match="Duplicate"):
        validate_saved(cases(), saved, (score, score))
    with pytest.raises(ValueError, match="skip status"):
        validate_saved(
            cases(),
            saved,
            (
                MetricScore(
                    question_id=score.question_id,
                    metric=score.metric,
                    status="skipped",
                    reason="Wrong",
                ),
            ),
        )


def test_resume_accepts_holes_but_rejects_duplicate_or_unknown_answers() -> None:
    first, second = (answer(case) for case in cases())
    validate_saved(cases(), (second,), ())
    validate_saved(cases(), (second, first), ())
    with pytest.raises(ValueError, match="Duplicate saved answer"):
        validate_saved(cases(), (first, first), ())
    with pytest.raises(ValueError, match="frozen question/reference"):
        validate_saved((cases()[0],), (second,), ())


def test_summary_keeps_abstentions_missing_and_undefined_out_of_means() -> None:
    negative = QACase(
        question=NegativeQuestion.model_validate(negative_payload()), reference=None
    )
    answers = (
        answer(cases()[0]),
        answer(cases()[1], abstained=True),
        answer(negative, abstained=True),
    )
    scores = (
        MetricScore(
            question_id=answers[0].question.id,
            metric=METRICS[0],
            status="ok",
            value=0.5,
        ),
        MetricScore(
            question_id=answers[1].question.id,
            metric=METRICS[0],
            status="skipped",
            reason="abstention",
        ),
        MetricScore(
            question_id=answers[2].question.id,
            metric=METRICS[0],
            status="skipped",
            reason="negative",
        ),
        MetricScore(
            question_id=answers[0].question.id,
            metric=METRICS[1],
            status="undefined",
            reason="NaN",
        ),
    )
    summary = summarize_answers(answers, scores)
    assert (summary.positive_abstentions, summary.positive_count) == (1, 2)
    assert (summary.negative_abstentions, summary.negative_count) == (1, 1)
    metric = summary.metrics[METRICS[0]]
    assert (metric.mean, metric.scored, metric.skipped) == (0.5, 1, 2)
    assert summary.metrics[METRICS[1]].mean is None
    assert summary.metrics[METRICS[1]].undefined == 1
    assert summary.metrics[METRICS[2]].missing == 3
    assert summary.retrieval is not None
    assert [m.k for m in summary.retrieval.overall] == [1, 3, 5]


def test_partial_pair_does_not_invent_a_pair_comparison() -> None:
    case = QACase(
        question=PositiveQuestion.model_validate(
            positive_payload(pair_id="intent-001")
        ),
        reference="Expected",
    )
    saved = answer(case)
    summary = summarize_answers((saved,), (), total_questions=2)
    assert summary.retrieval is not None and not summary.retrieval.pairs
    assert summary.processed_questions == 1
    assert all(metric.missing == 2 for metric in summary.metrics.values())


def test_corrupt_saved_json_fails_instead_of_being_skipped(tmp_path: Path) -> None:
    (tmp_path / "answers.jsonl").write_text("broken\n")
    with pytest.raises(ValueError):
        read_records(tmp_path / "answers.jsonl", CaseResult)


def test_case_result_requires_response_or_explicit_error() -> None:
    saved = answer(cases()[0]).model_dump()
    with pytest.raises(ValueError, match="Exactly one"):
        CaseResult.model_validate({**saved, "response": None})
    with pytest.raises(ValueError, match="Exactly one"):
        CaseResult.model_validate({**saved, "generation_error": "invalid"})
