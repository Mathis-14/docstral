import json
from hashlib import sha256
from pathlib import Path
from typing import Literal, Self

import pytest
from docstral_backend import RetrievalRequest, RetrievalResponse, RetrievedChunk

from evals.run_retrieval import (
    EvaluationRunError,
    NegativeRetrievalResult,
    PositiveRetrievalResult,
    RunConfig,
    RunManifest,
    _parse_config,
    execute,
)
from evals.tests.helpers import DOCS, make_chunk, negative_payload, positive_payload


class _FakeRetriever:
    def __init__(
        self,
        responses: dict[str, tuple[RetrievedChunk, ...]],
        *,
        failing_query: str | None = None,
    ) -> None:
        self.responses = responses
        self.failing_query = failing_query
        self.calls: list[RetrievalRequest] = []
        self.events: list[str | float] = []
        self.endpoints: list[str] = []

    def build(self, *, vespa_endpoint: str) -> Self:
        self.endpoints.append(vespa_endpoint)
        return self

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResponse:
        self.calls.append(request)
        self.events.append(request.query)
        if request.query == self.failing_query:
            raise RuntimeError("Vespa unavailable")
        return RetrievalResponse(
            query=request.query,
            chunks=self.responses.get(request.query, ()),
        )


@pytest.mark.parametrize("delay", [0.0, 1.0])
async def test_runner_queries_sequentially_and_writes_complete_artifacts(
    tmp_path: Path,
    delay: float,
) -> None:
    config, digest = _config_with_dataset(tmp_path, query_delay_seconds=delay)
    relevant = make_chunk(
        rank=1,
        chunk_id="relevant",
        source_id=f"{DOCS}/guide",
        content="Exact evidence",
        content_hash=digest,
        score=0.8,
    )
    duplicate = make_chunk(
        rank=2,
        chunk_id="duplicate",
        source_id=f"{DOCS}/guide",
        content="Other passage",
        content_hash=digest,
        score=0.7,
    )
    retriever = _FakeRetriever(
        {
            "First question": (relevant, duplicate),
            "Second question": (relevant,),
            "Unknown question": (duplicate,),
        }
    )

    async def record_sleep(delay_seconds: float) -> None:
        retriever.events.append(delay_seconds)

    run = await execute(config, retriever.build, record_sleep)

    assert retriever.endpoints == ["http://localhost:8080"]
    assert [(call.query, call.top_k) for call in retriever.calls] == [
        ("First question", 10),
        ("Second question", 10),
        ("Unknown question", 10),
    ]
    assert retriever.events == (
        ["First question", 1.0, "Second question", 1.0, "Unknown question"]
        if delay
        else ["First question", "Second question", "Unknown question"]
    )
    assert [chunk.id for chunk in run.positives[0].chunks] == [
        "relevant",
        "duplicate",
    ]
    assert run.positives[0].metrics[0].evidence_recall == 1.0
    assert len(run.negatives) == 1
    assert set(path.name for path in config.output_dir.iterdir()) == {
        "run.json",
        "positive_results.jsonl",
        "negative_results.jsonl",
        "summary.json",
    }

    manifest = RunManifest.model_validate_json(
        (config.output_dir / "run.json").read_text(encoding="utf-8")
    )
    assert manifest.top_k == 10
    assert manifest.cutoffs == (1, 3, 5, 10)
    assert manifest.query_delay_seconds == delay
    assert manifest.positive_count == 2
    assert manifest.negative_count == 1
    assert manifest.search_toolkit_version == "0.0.13"
    assert manifest.embedding_model == "mistral-embed"
    positive_rows = tuple(
        PositiveRetrievalResult.model_validate_json(line)
        for line in (config.output_dir / "positive_results.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    negative_rows = tuple(
        NegativeRetrievalResult.model_validate_json(line)
        for line in (config.output_dir / "negative_results.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert [row.question.id for row in positive_rows] == [
        "candidate-001",
        "candidate-002",
    ]
    assert positive_rows == run.positives
    assert negative_rows == run.negatives
    assert negative_rows[0].question.id == "negative-candidate-001"
    assert '"metrics":' not in (config.output_dir / "negative_results.jsonl").read_text(
        encoding="utf-8"
    )
    assert "MISTRAL_API_KEY" not in "".join(
        path.read_text(encoding="utf-8") for path in config.output_dir.iterdir()
    )


@pytest.mark.parametrize("mutation", ["edit", "remove"])
async def test_runner_keeps_loaded_dataset_hashes_when_files_change(
    tmp_path: Path, mutation: Literal["edit", "remove"]
) -> None:
    config, _ = _config_with_dataset(tmp_path, query_delay_seconds=1.0)
    original_files = {
        path: path.read_bytes()
        for path in (config.positive_dataset, config.negative_dataset)
    }
    retriever = _FakeRetriever({})
    changed = False

    async def change_datasets(delay_seconds: float) -> None:
        nonlocal changed
        if not changed:
            for path, content in original_files.items():
                if mutation == "edit":
                    path.write_bytes(content.replace(b"question", b"changed question"))
                else:
                    path.unlink()
            changed = True

    run = await execute(config, retriever.build, change_datasets)

    assert changed
    assert [call.query for call in retriever.calls] == [
        "First question",
        "Second question",
        "Unknown question",
    ]
    assert [result.question.query for result in run.positives] == [
        "First question",
        "Second question",
    ]
    assert run.negatives[0].question.query == "Unknown question"
    manifest = RunManifest.model_validate_json(
        (config.output_dir / "run.json").read_text(encoding="utf-8")
    )
    assert (
        manifest.positive_dataset_sha256
        == sha256(original_files[config.positive_dataset]).hexdigest()
    )
    assert (
        manifest.negative_dataset_sha256
        == sha256(original_files[config.negative_dataset]).hexdigest()
    )


async def test_runner_propagates_external_failure_without_output(
    tmp_path: Path,
) -> None:
    config, _ = _config_with_dataset(tmp_path)
    retriever = _FakeRetriever({}, failing_query="Unknown question")

    with pytest.raises(RuntimeError, match="Vespa unavailable") as exc_info:
        await execute(config, retriever.build)

    assert [call.query for call in retriever.calls] == [
        "First question",
        "Second question",
        "Unknown question",
    ]
    assert exc_info.value.__notes__ == [
        "Retrieval failed for evaluation question 'negative-candidate-001'"
    ]
    assert not config.output_dir.exists()


async def test_runner_refuses_existing_output_before_building_retriever(
    tmp_path: Path,
) -> None:
    config, _ = _config_with_dataset(tmp_path)
    config.output_dir.mkdir(parents=True)
    retriever = _FakeRetriever({})

    with pytest.raises(EvaluationRunError, match="already exists"):
        await execute(config, retriever.build)

    assert retriever.endpoints == []


def test_run_config_normalizes_endpoint_and_rejects_credentials(
    tmp_path: Path,
) -> None:
    config = RunConfig(
        vespa_endpoint="http://localhost:8080/",
        corpus_dir=tmp_path,
        output_dir=tmp_path / "output",
    )
    assert config.vespa_endpoint == "http://localhost:8080"

    with pytest.raises(ValueError, match="HTTP"):
        RunConfig(
            vespa_endpoint="http://user:secret@localhost:8080",
            corpus_dir=tmp_path,
            output_dir=tmp_path / "output",
        )


@pytest.mark.parametrize("explicit_datasets", [False, True])
def test_cli_parses_paths_and_dataset_defaults(explicit_datasets: bool) -> None:
    args = [
        "--vespa-endpoint",
        "http://localhost:8080/",
        "--corpus-dir",
        "corpus/pages",
        "--output-dir",
        "results",
    ]
    if explicit_datasets:
        args += [
            "--positive-dataset",
            "positive.jsonl",
            "--negative-dataset",
            "negative.jsonl",
            "--query-delay-seconds",
            "1.5",
        ]

    config = _parse_config(args)

    assert config.vespa_endpoint == "http://localhost:8080"
    assert config.corpus_dir == Path("corpus/pages")
    assert config.output_dir == Path("results")
    assert config.query_delay_seconds == (1.5 if explicit_datasets else 0.0)
    dataset_dir = Path(__file__).parents[1] / "datasets"
    assert config.positive_dataset == (
        Path("positive.jsonl")
        if explicit_datasets
        else dataset_dir / "retrieval_dev_v1.jsonl"
    )
    assert config.negative_dataset == (
        Path("negative.jsonl")
        if explicit_datasets
        else dataset_dir / "retrieval_negatives_v1.jsonl"
    )


@pytest.mark.parametrize("invalid_delay", [-1.0, float("inf"), float("nan")])
def test_run_config_rejects_invalid_query_delay(
    tmp_path: Path,
    invalid_delay: float,
) -> None:
    with pytest.raises(ValueError, match=r"finite|greater than or equal to 0"):
        RunConfig(
            vespa_endpoint="http://localhost:8080",
            corpus_dir=tmp_path,
            output_dir=tmp_path / "output",
            query_delay_seconds=invalid_delay,
        )


def _config_with_dataset(
    root: Path, *, query_delay_seconds: float = 0.0
) -> tuple[RunConfig, str]:
    content = "# Guide\n\nExact evidence"
    digest = sha256(content.encode()).hexdigest()
    corpus_dir = root / "corpus" / "snapshot" / "pages"
    corpus_dir.mkdir(parents=True)
    (corpus_dir / "guide.md").write_text(content, encoding="utf-8")

    positive_path = root / "positive.jsonl"
    positives = [
        positive_payload(
            question_id="candidate-001",
            query="First question",
            content_hash=digest,
            excerpt="Exact evidence",
        ),
        positive_payload(
            question_id="candidate-002",
            query="Second question",
            content_hash=digest,
            excerpt="Exact evidence",
        ),
    ]
    positive_path.write_text(
        "".join(f"{json.dumps(payload)}\n" for payload in positives),
        encoding="utf-8",
    )
    negative_path = root / "negative.jsonl"
    negative_path.write_text(f"{json.dumps(negative_payload())}\n", encoding="utf-8")
    return (
        RunConfig(
            vespa_endpoint="http://localhost:8080",
            corpus_dir=corpus_dir,
            output_dir=root / "artifacts" / "run-1",
            positive_dataset=positive_path,
            negative_dataset=negative_path,
            query_delay_seconds=query_delay_seconds,
        ),
        digest,
    )
