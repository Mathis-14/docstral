"""Run the local retrieval experiment, not the production Q&A service.

execute loads and checks the gold, queries the backend, scores the returned chunks,
and saves a complete run. main supplies its configuration from the command line.
"""

import argparse
import asyncio
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Literal, Protocol
from urllib.parse import urlsplit

from docstral_backend import (
    RetrievalRequest,
    RetrievalResponse,
    RetrievedChunk,
    build_documentation_retriever,
)
from mistralai.search.toolkit.embedding import MODEL_1024_EMBEDDING
from pydantic import BaseModel, ConfigDict, Field, field_validator

from evals.retrieval_dataset import (
    LoadedDataset,
    NegativeQuestion,
    PositiveQuestion,
    load_dataset,
    validate_corpus,
)
from evals.retrieval_metrics import (
    DEFAULT_CUTOFFS,
    MetricsAtK,
    QuestionEvaluation,
    RetrievalSummary,
    evaluate_question,
    summarize,
)

_TOP_K = 10
_DEFAULT_POSITIVES = Path(__file__).parent / "datasets/retrieval_dev_v1.jsonl"
_DEFAULT_NEGATIVES = Path(__file__).parent / "datasets/retrieval_negatives_v1.jsonl"


class EvaluationRunError(Exception):
    """The evaluation cannot start or persist a complete run."""


class RunConfig(BaseModel):
    """Explicit local inputs and output destination for one retrieval run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    vespa_endpoint: str
    corpus_dir: Path
    output_dir: Path
    positive_dataset: Path = _DEFAULT_POSITIVES
    negative_dataset: Path = _DEFAULT_NEGATIVES
    query_delay_seconds: float = Field(
        default=0.0,
        ge=0.0,
        allow_inf_nan=False,
    )

    @field_validator("vespa_endpoint")
    @classmethod
    def _endpoint_must_be_http(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError("vespa_endpoint must be an HTTP(S) endpoint")
        return value.rstrip("/")


class PositiveRetrievalResult(BaseModel):
    """One positive question, its ordered chunks, and passage metrics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question: PositiveQuestion
    chunks: tuple[RetrievedChunk, ...]
    metrics: tuple[MetricsAtK, ...]
    duration_seconds: float = Field(ge=0.0)


class NegativeRetrievalResult(BaseModel):
    """One negative question and its unscored ordered nearest chunks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question: NegativeQuestion
    chunks: tuple[RetrievedChunk, ...]
    duration_seconds: float = Field(ge=0.0)


class EvaluationRun(BaseModel):
    """Complete in-memory results, written only after every query succeeds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    started_at: datetime
    completed_at: datetime
    duration_seconds: float = Field(ge=0.0)
    positives: tuple[PositiveRetrievalResult, ...]
    negatives: tuple[NegativeRetrievalResult, ...]
    summary: RetrievalSummary


class RunManifest(BaseModel):
    """Configuration and provenance for one completed local run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    started_at: datetime
    completed_at: datetime
    duration_seconds: float = Field(ge=0.0)
    git_revision: str
    git_dirty: bool
    positive_dataset: str
    positive_dataset_sha256: str
    negative_dataset: str
    negative_dataset_sha256: str
    corpus_dir: str
    corpus_snapshot: str
    vespa_endpoint: str
    search_toolkit_version: str
    embedding_model: str
    top_k: int
    cutoffs: tuple[int, ...]
    query_delay_seconds: float = Field(ge=0.0, allow_inf_nan=False)
    positive_count: int
    negative_count: int


class _DocumentationRetriever(Protocol):
    async def retrieve(self, request: RetrievalRequest) -> RetrievalResponse: ...


class _RetrieverFactory(Protocol):
    def __call__(self, *, vespa_endpoint: str) -> _DocumentationRetriever: ...


async def execute(
    config: RunConfig,
    retriever_factory: _RetrieverFactory = build_documentation_retriever,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> EvaluationRun:
    """Validate all local inputs, run sequential retrieval, and write atomically."""
    _prepare_output_parent(config.output_dir)
    loaded = load_dataset(config.positive_dataset, config.negative_dataset)
    validate_corpus(loaded.dataset, config.corpus_dir)
    retriever = retriever_factory(vespa_endpoint=config.vespa_endpoint)
    dataset = loaded.dataset
    started_at = datetime.now(UTC)
    started = monotonic()
    positives: list[PositiveRetrievalResult] = []
    negatives: list[NegativeRetrievalResult] = []
    evaluations: list[QuestionEvaluation] = []
    questions: tuple[PositiveQuestion | NegativeQuestion, ...] = (
        *dataset.positives,
        *dataset.negatives,
    )
    for position, question in enumerate(questions):
        if position > 0 and config.query_delay_seconds > 0:
            await sleeper(config.query_delay_seconds)
        query_started = monotonic()
        try:
            response = await retriever.retrieve(
                RetrievalRequest(query=question.query, top_k=_TOP_K)
            )
        except Exception as exc:
            exc.add_note(f"Retrieval failed for evaluation question {question.id!r}")
            raise
        if isinstance(question, PositiveQuestion):
            evaluation = evaluate_question(question, response.chunks)
            evaluations.append(evaluation)
            positives.append(
                PositiveRetrievalResult(
                    question=question,
                    chunks=response.chunks,
                    metrics=evaluation.metrics,
                    duration_seconds=monotonic() - query_started,
                )
            )
        else:
            negatives.append(
                NegativeRetrievalResult(
                    question=question,
                    chunks=response.chunks,
                    duration_seconds=monotonic() - query_started,
                )
            )

    completed_at = datetime.now(UTC)
    run = EvaluationRun(
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=monotonic() - started,
        positives=tuple(positives),
        negatives=tuple(negatives),
        summary=summarize(evaluations),
    )
    _write_outputs(config, run, loaded)
    return run


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the development command and execute one complete evaluation."""
    config = _parse_config(argv)
    run = asyncio.run(execute(config))
    _print_summary(run, config.output_dir)
    return 0


def _prepare_output_parent(output_dir: Path) -> None:
    if output_dir.exists():
        raise EvaluationRunError(
            f"Evaluation output {str(output_dir)!r} already exists"
        )
    try:
        output_dir.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise EvaluationRunError(
            f"Cannot prepare evaluation output parent {str(output_dir.parent)!r}: {exc}"
        ) from exc


def _write_outputs(
    config: RunConfig, run: EvaluationRun, loaded: LoadedDataset
) -> None:
    revision, dirty = _git_state()
    manifest = RunManifest(
        started_at=run.started_at,
        completed_at=run.completed_at,
        duration_seconds=run.duration_seconds,
        git_revision=revision,
        git_dirty=dirty,
        positive_dataset=str(config.positive_dataset),
        positive_dataset_sha256=loaded.positive_sha256,
        negative_dataset=str(config.negative_dataset),
        negative_dataset_sha256=loaded.negative_sha256,
        corpus_dir=str(config.corpus_dir),
        corpus_snapshot=config.corpus_dir.parent.name,
        vespa_endpoint=config.vespa_endpoint,
        search_toolkit_version=version("mistralai-search-toolkit"),
        embedding_model=MODEL_1024_EMBEDDING,
        top_k=_TOP_K,
        cutoffs=DEFAULT_CUTOFFS,
        query_delay_seconds=config.query_delay_seconds,
        positive_count=len(run.positives),
        negative_count=len(run.negatives),
    )
    try:
        with TemporaryDirectory(
            prefix=f".{config.output_dir.name}-", dir=config.output_dir.parent
        ) as temporary:
            destination = Path(temporary)
            _write_json(destination / "run.json", manifest)
            _write_jsonl(destination / "positive_results.jsonl", run.positives)
            _write_jsonl(destination / "negative_results.jsonl", run.negatives)
            _write_json(destination / "summary.json", run.summary)
            destination.rename(config.output_dir)
    except OSError as exc:
        raise EvaluationRunError(
            f"Cannot write complete evaluation output {str(config.output_dir)!r}: {exc}"
        ) from exc


def _write_json(path: Path, model: BaseModel) -> None:
    path.write_text(f"{model.model_dump_json(indent=2)}\n", encoding="utf-8")


def _write_jsonl(path: Path, models: Sequence[BaseModel]) -> None:
    content = "".join(f"{model.model_dump_json()}\n" for model in models)
    path.write_text(content, encoding="utf-8")


def _git_state() -> tuple[str, bool]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise EvaluationRunError(f"Cannot record Git state: {exc}") from exc
    if not revision:
        raise EvaluationRunError("Cannot record Git state: empty revision")
    return revision, bool(status)


def _parse_config(argv: Sequence[str] | None) -> RunConfig:
    parser = argparse.ArgumentParser(
        description="Evaluate Docstral retrieval against the frozen development set."
    )
    parser.add_argument("--vespa-endpoint", required=True)
    parser.add_argument("--corpus-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--positive-dataset", default=str(_DEFAULT_POSITIVES))
    parser.add_argument("--negative-dataset", default=str(_DEFAULT_NEGATIVES))
    parser.add_argument("--query-delay-seconds", type=float, default=0.0)
    return RunConfig.model_validate(vars(parser.parse_args(argv)))


def _print_summary(run: EvaluationRun, output_dir: Path) -> None:
    print(
        f"Completed {len(run.positives)} positive and "
        f"{len(run.negatives)} negative retrieval queries."
    )
    for metric in run.summary.overall:
        print(
            f"k={metric.k}: macro_recall={metric.macro_evidence_recall:.3f} "
            f"micro_recall={metric.micro_evidence_recall:.3f} "
            f"all_required={metric.all_required_rate:.3f} "
            f"mrr={metric.mrr:.3f} source_hit={metric.source_hit_rate:.3f} "
            f"duplicate_source_rate={metric.duplicate_source_rate:.3f}"
        )
    print(f"Artifacts: {output_dir}")


if __name__ == "__main__":
    raise SystemExit(main())
