"""Run the frozen Q&A set sequentially, then judge its saved answers with Ragas."""

import argparse
import asyncio
import subprocess
from datetime import UTC, datetime
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from time import monotonic
from typing import Literal

import httpx
from docstral_backend import (
    AnsweringError,
    DocumentationAnswerer,
    DocumentationRetriever,
)
from docstral_vespa import search_index
from mistralai.client import Mistral
from mistralai.search.toolkit.clients.mistral import MistralClientConfig
from mistralai.search.toolkit.embedding import MODEL_1024_EMBEDDING, MistralEmbedder
from mistralai.search.toolkit.llm.mistral import (
    MistralChat,
    MistralLLMException,
    TruncatedLLMResponseError,
)
from mistralai.search.toolkit.retrieval.retrievers import VectorRetriever
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evals.qa_dataset import (
    DatasetFreeze,
    QACase,
    load_qa_dataset,
    validate_observed_chunk,
)
from evals.qa_metrics import summarize_answers
from evals.qa_runtime import (
    METRICS,
    CaseResult,
    MetricScore,
    RecordingRetriever,
    TraceTransport,
    append_record,
    is_rate_limit,
    read_records,
    score_case,
)


class QAConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    output_dir: Path
    freeze_path: Path
    dataset: Path = Path("evals/datasets/qa_dev_v1.jsonl")
    corpus_dir: Path = Path("data/extracted/20260903T120924Z/pages")
    vespa_endpoint: Literal["http://localhost:8080"] = "http://localhost:8080"
    http_delay_seconds: float = Field(default=1.0, ge=0.0, allow_inf_nan=False)


class QARunManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    config: QAConfig
    freeze: DatasetFreeze
    input_fingerprints: dict[str, str]
    versions: dict[str, str]
    git_revision: str
    git_dirty: bool
    question_ids: tuple[str, ...]
    started_at: str
    status: Literal["incomplete", "completed"] = "incomplete"
    saved_sha256: dict[str, str] = Field(default_factory=dict)
    top_k: Literal[5] = 5
    embedding_model: Literal["mistral-embed"] = "mistral-embed"
    answer_model: Literal["mistral-small-2603"] = "mistral-small-2603"
    judge_model: Literal["mistral-medium-3-5"] = "mistral-medium-3-5"
    judge_temperature: float = 0.0
    judge_top_p: float = 1.0
    judge_max_tokens: int = 4096
    instructor_max_attempts: int = 3
    openai_max_retries: int = 0
    atomicity: str = "high"
    coverage: str = "high"
    metrics: tuple[str, ...] = METRICS


class RunAttempt(BaseModel):
    model_config = ConfigDict(frozen=True)

    started_at: str
    completed_at: str
    duration_seconds: float
    status: Literal["incomplete", "completed"]
    failed_question: str | None
    failed_step: str | None
    error_type: str | None


def fingerprints(config: QAConfig) -> dict[str, str]:
    paths = [
        *Path("evals").glob("*.py"),
        *Path("apps/backend/src").rglob("*.py"),
        *Path("packages/vespa/src").rglob("*.py"),
        Path("uv.lock"),
        config.dataset,
        config.freeze_path,
    ]
    return {
        str(path): sha256(path.read_bytes()).hexdigest() for path in sorted(set(paths))
    }


def saved_fingerprints(output_dir: Path) -> dict[str, str]:
    return {
        name: sha256((output_dir / name).read_bytes()).hexdigest()
        for name in ("answers.jsonl", "scores.jsonl")
        if (output_dir / name).exists()
    }


def validate_saved(
    cases: tuple[QACase, ...],
    answers: tuple[CaseResult, ...],
    scores: tuple[MetricScore, ...],
) -> None:
    """Allow holes and append order, but never duplicate or changed outcomes."""
    expected_by_id = {case.question.id: case for case in cases}
    by_id = {answer.question.id: answer for answer in answers}
    if len(by_id) != len(answers):
        raise ValueError("Duplicate saved answer")
    for answer in answers:
        expected = expected_by_id.get(answer.question.id)
        if (
            expected is None
            or expected.question != answer.question
            or expected.reference != answer.reference
        ):
            raise ValueError("Saved answers differ from the frozen question/reference")
    keys = [(s.question_id, s.metric) for s in scores]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate saved metric")
    for score in scores:
        if score.question_id not in by_id or score.metric not in METRICS:
            raise ValueError("Saved metric has an unknown question or name")
        answer = by_id[score.question_id]
        should_skip = (
            answer.response is None
            or answer.reference is None
            or answer.response.abstained
        )
        if (score.status == "skipped") != should_skip:
            raise ValueError("Saved metric skip status contradicts the saved answer")


async def generate_answers(
    cases: tuple[QACase, ...],
    saved: tuple[CaseResult, ...],
    answerer: DocumentationAnswerer,
    recorder: RecordingRetriever,
    trace: TraceTransport,
    output_dir: Path,
) -> tuple[CaseResult, ...]:
    answers = {answer.question.id: answer for answer in saved}
    for case in cases:
        if case.question.id in answers:
            continue
        trace.question_id, trace.step = case.question.id, "generation"
        started = monotonic()
        response, generation_error = None, None
        try:
            response = await answerer.answer(case.question.query)
        except Exception as exc:
            if is_rate_limit(exc):
                print(f"{case.question.id}: generation pending (HTTP 429)", flush=True)
                continue
            if isinstance(exc, AnsweringError):
                generation_error = str(exc)
            elif isinstance(exc, MistralLLMException) and isinstance(
                exc.__cause__, ValidationError
            ):
                generation_error = "; ".join(
                    error["msg"] for error in exc.__cause__.errors(include_input=False)
                )
            elif isinstance(exc, TruncatedLLMResponseError):
                generation_error = "Model returned malformed or truncated JSON."
            else:
                raise
        answer = CaseResult(
            question=case.question,
            reference=case.reference,
            chunks=recorder.chunks,
            response=response,
            generation_error=generation_error,
            duration_seconds=monotonic() - started,
        )
        append_record(output_dir / "answers.jsonl", answer)
        answers[case.question.id] = answer
        print(
            f"{case.question.id}: {answer.status}",
            flush=True,
        )
    return tuple(answers[c.question.id] for c in cases if c.question.id in answers)


async def run(config: QAConfig, *, resume: bool = False) -> None:
    from openai import AsyncOpenAI

    loaded = load_qa_dataset(
        config.dataset, config.corpus_dir, freeze_path=config.freeze_path
    )
    cases = loaded.cases
    client_config = MistralClientConfig()
    if client_config.api_key is None:
        raise ValueError("MISTRAL_API_KEY is required")
    if client_config.api_url != "https://api.mistral.ai":
        raise ValueError("This evaluation requires https://api.mistral.ai")
    manifest = QARunManifest(
        config=config,
        freeze=loaded.freeze,
        input_fingerprints=fingerprints(config),
        versions={
            name: version(name)
            for name in (
                "ragas",
                "instructor",
                "openai",
                "mistralai",
                "mistralai-search-toolkit",
                "mistralai-search-toolkit-plugins-vespa",
            )
        },
        git_revision=subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        git_dirty=bool(
            subprocess.check_output(["git", "status", "--porcelain"], text=True)
        ),
        question_ids=tuple(c.question.id for c in cases),
        started_at=datetime.now(UTC).isoformat(),
    )
    manifest_path = config.output_dir / "run.json"
    if resume:
        previous = QARunManifest.model_validate_json(manifest_path.read_bytes())
        if previous.model_dump(
            exclude={"started_at", "status", "saved_sha256"}
        ) != manifest.model_dump(exclude={"started_at", "status", "saved_sha256"}):
            raise ValueError(
                "Resume configuration, code, environment or dataset differs"
            )
        if previous.status == "completed":
            raise ValueError("This run is already completed")
        if previous.saved_sha256 != saved_fingerprints(config.output_dir):
            raise ValueError("Saved answers or scores changed after the last attempt")
        manifest = previous
    else:
        config.output_dir.mkdir(parents=True, exist_ok=False)
        manifest_path.write_text(manifest.model_dump_json(indent=2) + "\n")
        for case in cases:
            append_record(config.output_dir / "questions.jsonl", case)
    answers = read_records(config.output_dir / "answers.jsonl", CaseResult)
    scores = read_records(config.output_dir / "scores.jsonl", MetricScore)
    validate_saved(cases, answers, scores)
    for answer in answers:
        for chunk in answer.chunks:
            validate_observed_chunk(chunk, config.corpus_dir)
    done = {(s.question_id, s.metric) for s in scores}
    trace = TraceTransport(
        httpx.AsyncHTTPTransport(),
        config.output_dir / "http.jsonl",
        delay_seconds=config.http_delay_seconds,
    )
    started = monotonic()
    attempt_started_at = datetime.now(UTC).isoformat()
    error_type = None
    interruption = None
    try:
        async with httpx.AsyncClient(transport=trace, timeout=120.0) as http:
            async with Mistral(
                api_key=client_config.api_key.get_secret_value(), async_client=http
            ) as sdk:
                recorder = RecordingRetriever(
                    DocumentationRetriever(
                        VectorRetriever(
                            client=search_index(config.vespa_endpoint),
                            embedder=MistralEmbedder(
                                client=sdk, model_name=MODEL_1024_EMBEDDING
                            ),
                        )
                    ),
                    corpus_dir=config.corpus_dir,
                )
                answers = await generate_answers(
                    cases,
                    answers,
                    DocumentationAnswerer(
                        recorder,
                        MistralChat(client=sdk),
                        top_k=5,
                        model=manifest.answer_model,
                    ),
                    recorder,
                    trace,
                    config.output_dir,
                )
                async with AsyncOpenAI(
                    api_key=client_config.api_key.get_secret_value(),
                    base_url="https://api.mistral.ai/v1",
                    http_client=http,
                    max_retries=0,
                ) as judge:
                    for answer in answers:
                        for name in METRICS:
                            if (answer.question.id, name) in done:
                                continue
                            try:
                                (score,) = await score_case(
                                    answer, judge, trace, metric_names=(name,)
                                )
                            except Exception as exc:
                                if not is_rate_limit(exc):
                                    raise
                                print(
                                    f"{answer.question.id}: {name} pending (HTTP 429)",
                                    flush=True,
                                )
                                continue
                            append_record(config.output_dir / "scores.jsonl", score)
                            done.add((score.question_id, score.metric))
                            print(
                                f"{score.question_id}: {name}={score.value} ({score.status})",
                                flush=True,
                            )
        if len(answers) == len(cases) and len(done) == len(cases) * len(METRICS):
            manifest = manifest.model_copy(update={"status": "completed"})
        else:
            interruption = (
                f"HTTP 429 left {len(cases) - len(answers)} questions and "
                f"{len(answers) * len(METRICS) - len(done)} metrics on saved answers "
                "pending. Use --resume to retry missing work."
            )
    except Exception as exc:
        error_type = type(exc).__name__
        interruption = (
            f"{trace.question_id}, {trace.step}: {error_type}; inspect http.jsonl"
        )
        print(
            f"Run incomplete: {trace.question_id}, {trace.step}, {error_type}; inspect local traces.",
            flush=True,
        )
        raise
    finally:
        manifest = manifest.model_copy(
            update={"saved_sha256": saved_fingerprints(config.output_dir)}
        )
        manifest_path.write_text(manifest.model_dump_json(indent=2) + "\n")
        append_record(
            config.output_dir / "attempts.jsonl",
            RunAttempt(
                started_at=attempt_started_at,
                completed_at=datetime.now(UTC).isoformat(),
                duration_seconds=monotonic() - started,
                status=manifest.status,
                failed_question=trace.question_id if error_type else None,
                failed_step=trace.step if error_type else None,
                error_type=error_type,
            ),
        )
        summary = summarize_answers(
            read_records(config.output_dir / "answers.jsonl", CaseResult),
            read_records(config.output_dir / "scores.jsonl", MetricScore),
            total_questions=len(cases),
        )
        summary_path = config.output_dir / "summary.json"
        summary_path.write_text(summary.model_dump_json(indent=2) + "\n")
        print(
            f"{manifest.status}: {summary.processed_questions}/{len(cases)} questions; "
            f"summary: {summary_path}",
            flush=True,
        )
    if manifest.status != "completed":
        raise RuntimeError(interruption)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--freeze",
        type=Path,
        required=True,
        help="Path to the approved local freeze JSON",
    )
    parser.add_argument(
        "--dataset", type=Path, default=QAConfig.model_fields["dataset"].default
    )
    parser.add_argument(
        "--corpus-dir", type=Path, default=QAConfig.model_fields["corpus_dir"].default
    )
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    asyncio.run(
        run(
            QAConfig(
                output_dir=args.output_dir,
                freeze_path=args.freeze,
                dataset=args.dataset,
                corpus_dir=args.corpus_dir,
            ),
            resume=args.resume,
        )
    )


if __name__ == "__main__":
    main()
