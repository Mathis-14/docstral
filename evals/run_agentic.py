import argparse
import asyncio
import json
import os
import subprocess
from collections import Counter
from contextlib import AsyncExitStack
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from time import monotonic
from typing import Literal
from urllib.parse import urlsplit

import httpx
from docstral_backend import (
    AnsweringError,
    AnswerResponse,
    DocumentationAnswerer,
    RetrievedChunk,
)
from docstral_vespa import index_for_client
from mistralai.client import Mistral
from mistralai.client.errors import MistralError
from mistralai.search.toolkit.clients.mistral import MistralClientConfig
from mistralai.search.toolkit.embedding import MistralEmbedder
from mistralai.search.toolkit.llm.mistral import MistralChat, TruncatedLLMResponseError
from mistralai.search.toolkit.plugins.vespa import VespaClient, VespaClientConfig
from mistralai.search.toolkit.retrieval.retrievers import VectorRetriever
from mistralai.search.toolkit.search import NavigableIndex
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evals.agentic import (
    LIMITS,
    MODEL,
    Arm,
    ExperimentError,
    LimitExceeded,
    Research,
    collect_evidence,
)
from evals.qa_dataset import (
    DatasetFreeze,
    QACase,
    load_qa_dataset,
    validate_observed_chunk,
)
from evals.qa_runtime import HTTPExchange, TraceTransport, append_record, read_records
from evals.retrieval_dataset import PositiveQuestion
from evals.retrieval_metrics import evaluate_question

PANEL = tuple(f"candidate-{n:03}" for n in (2, 20, 37, 7, 29, 47, 17, 33, 45)) + tuple(
    f"negative-candidate-{n:03}" for n in (3, 5, 9)
)


class RunError(Exception):
    pass


class RunConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)
    vespa_endpoint: str = Field(pattern=r"^http://127\.0\.0\.1:[0-9]+/?$")
    vespa_container: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]+$")
    corpus_dir: Path
    freeze: Path
    output_dir: Path
    question_ids: tuple[str, ...] = PANEL
    question: str | None = Field(default=None, min_length=1, max_length=2000)


class Manifest(BaseModel):
    schema_version: Literal[3] = 3
    config: RunConfig
    freeze: DatasetFreeze
    container_id: str
    index_sha256: str
    versions: dict[str, str]
    code_sha256: dict[str, str]
    status: Literal["incomplete", "completed", "partial", "error"] = "incomplete"
    error: str | None = None


def docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=10
    )
    if result.returncode:
        raise RuntimeError("Cannot inspect local Docker; check its daemon and socket")
    return result.stdout.strip()


def local_container(config: RunConfig) -> str:
    context = docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
    if (
        not context.startswith("unix://")
        or os.environ.get("DOCKER_HOST", context) != context
    ):
        raise ValueError("Docker must use the inspected local Unix socket")
    port = urlsplit(config.vespa_endpoint).port
    bindings = docker("port", config.vespa_container, "8080/tcp").splitlines()
    if not {f"127.0.0.1:{port}", f"0.0.0.0:{port}"}.intersection(bindings):
        raise ValueError("Endpoint does not match the selected container's Vespa port")
    identity = docker(
        "inspect",
        "--format",
        "{{if .State.Running}}{{.Id}}{{end}}",
        config.vespa_container,
    )
    if not identity:
        raise ValueError("Selected Vespa container is not running")
    return identity


async def audit_index(client: VespaClient, corpus: Path) -> str:
    chunks: dict[str, str] = {}
    sources: set[str] = set()
    continuation = None
    async with asyncio.timeout(60):
        while True:
            page = await client.visit_by_selection(
                "docs",
                "true",
                continuation=continuation,
                field_set="docs:id,source_id,locator,start_offset,end_offset,content,title,content_hash",
            )
            for document in page.documents:
                chunk = RetrievedChunk.model_validate(
                    {**document.fields, "rank": 1, "score": 0.0}
                )
                validate_observed_chunk(chunk, corpus)
                if chunk.id in chunks or len(chunks) >= 785:
                    raise ValueError(
                        "Unexpected or duplicate chunk in the frozen index"
                    )
                chunks[chunk.id] = chunk.model_dump_json()
                sources.add(chunk.source_id)
            continuation = page.continuation
            if continuation is None:
                break
    if len(chunks) != 785 or len(sources) != 331:
        raise ValueError("Expected the frozen index: 785 chunks from 331 sources")
    return sha256(json.dumps(chunks, sort_keys=True).encode()).hexdigest()


class BoundedTransport(TraceTransport):
    def __init__(self, destination: Path) -> None:
        super().__init__(httpx.AsyncHTTPTransport(), destination)
        self.run_attempts = 0
        self.case_attempts = 0
        self.case_limit = LIMITS.agentic_attempts

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.mistral.ai" or request.url.scheme != "https":
            raise RuntimeError("Only the official Mistral API is allowed")
        if (
            self.run_attempts >= LIMITS.run_attempts
            or self.case_attempts >= self.case_limit
        ):
            raise LimitExceeded("HTTP attempt limit reached")
        if len(request.content) > LIMITS.context_bytes:
            raise LimitExceeded("Serialized request exceeds context byte limit")
        self.case_attempts += 1
        self.run_attempts += 1
        async with asyncio.timeout(LIMITS.mistral_seconds):
            return await super().handle_async_request(request)


def failure(error: BaseException) -> tuple[str, bool]:
    seen: set[int] = set()
    cause: BaseException | None = error
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, (LimitExceeded, TimeoutError)):
            return f"limit: {str(cause) or 'deadline exceeded'}", False
        if isinstance(cause, RunError):
            return str(cause), True
        if isinstance(cause, MistralError):
            return f"Mistral HTTP {cause.status_code}; inspect http.jsonl", True
        if isinstance(cause, (ExperimentError, AnsweringError)):
            return f"invalid output: {cause}", False
        if isinstance(
            cause, (ValidationError, TruncatedLLMResponseError, json.JSONDecodeError)
        ):
            return f"invalid output: {type(cause).__name__}; inspect http.jsonl", False
        cause = cause.__cause__ or cause.__context__
    return f"{type(error).__name__}; check local dependencies and traces", True


class Outcome(BaseModel):
    question_id: str
    query: str
    arm: Arm
    result: AnswerResponse | str
    chunks: tuple[RetrievedChunk, ...]
    duration_seconds: float
    usage: dict[str, int]
    coverage: dict[str, float]


async def answer_case(
    question: str, research: Research, sdk: Mistral, case: QACase | None
) -> tuple[Outcome, bool]:
    trace = research.trace
    started, fatal = monotonic(), False
    result: AnswerResponse | str
    try:
        async with asyncio.timeout(LIMITS.question_seconds):
            initial = await research.execute("search", {"query": question})
            if research.arm == "agentic":
                await collect_evidence(initial, question, research, sdk)
            trace.step = f"{research.arm}/answer"
            result = await DocumentationAnswerer(
                research,
                MistralChat(client=sdk),
                top_k=LIMITS.chunks if research.arm == "agentic" else 5,
                model=MODEL,
            ).answer(question)
    except Exception as exc:
        result, fatal = failure(exc)
    duration = monotonic() - started
    usage: Counter[str] = Counter()
    for exchange in read_records(trace.destination, HTTPExchange):
        if exchange.question_id != trace.question_id or not exchange.step.startswith(
            research.arm + "/"
        ):
            continue
        usage["attempts"] += 1
        if isinstance(exchange.response, dict) and isinstance(
            counters := exchange.response.get("usage"), dict
        ):
            usage.update({k: v for k, v in counters.items() if type(v) is int})
    coverage: dict[str, float] = {}
    chunks = tuple(research.chunks.values())
    if case is not None and isinstance(case.question, PositiveQuestion):
        metric = evaluate_question(
            case.question, chunks, cutoffs=(max(1, len(chunks)),)
        ).metrics[0]
        coverage = {
            "recall": metric.evidence_recall,
            "all_required": metric.all_required,
        }
    return Outcome(
        question_id=trace.question_id,
        query=question,
        arm=research.arm,
        result=result,
        chunks=chunks,
        duration_seconds=duration,
        usage=dict(usage),
        coverage=coverage,
    ), fatal


async def run(config: RunConfig) -> int:
    dataset = Path("evals/datasets/qa_dev_v1.jsonl")
    loaded = load_qa_dataset(dataset, config.corpus_dir, freeze_path=config.freeze)
    cases = {c.question.id: c for c in loaded.cases}
    if (
        not config.question_ids
        or len(set(config.question_ids)) != len(config.question_ids)
        or set(config.question_ids) - cases.keys()
    ):
        raise ValueError("Select distinct, known question IDs")
    output = config.output_dir.resolve()
    if not output.is_relative_to(Path("data").resolve()) or output.exists():
        raise ValueError(
            "Use a new output directory inside this worktree's ignored data/"
        )
    container_id = local_container(config)
    credentials = MistralClientConfig()
    if credentials.api_key is None or credentials.api_url != "https://api.mistral.ai":
        raise ValueError("Set MISTRAL_API_KEY and use https://api.mistral.ai")
    async with AsyncExitStack() as stack:
        vespa = VespaClient(
            VespaClientConfig(
                endpoint=config.vespa_endpoint, timeout=LIMITS.vespa_seconds
            )
        )
        stack.push_async_callback(vespa.aclose)
        index = index_for_client(vespa)
        if not isinstance(index, NavigableIndex):
            raise RunError("Installed Vespa index does not support navigation")
        audit = await audit_index(vespa, config.corpus_dir)
        manifest = Manifest(
            config=config,
            freeze=loaded.freeze,
            container_id=container_id,
            index_sha256=audit,
            versions={
                name: version(name)
                for name in (
                    "mistralai",
                    "mistralai-search-toolkit",
                    "mistralai-search-toolkit-plugins-vespa",
                )
            },
            code_sha256={
                str(p): sha256(p.read_bytes()).hexdigest()
                for p in [
                    *Path("evals").glob("*.py"),
                    *Path("apps/backend/src").rglob("*.py"),
                    *Path("packages/vespa/src").rglob("*.py"),
                    Path("uv.lock"),
                ]
            },
        )
        output.mkdir(parents=True)
        (output / "run.json").write_text(manifest.model_dump_json(indent=2) + "\n")
        trace = BoundedTransport(output / "http.jsonl")
        try:
            http = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=trace,
                    timeout=LIMITS.mistral_seconds,
                    trust_env=False,
                    follow_redirects=False,
                )
            )
            sdk = await stack.enter_async_context(
                Mistral(
                    api_key=credentials.api_key.get_secret_value(),
                    async_client=http,
                    retry_config=None,
                    timeout_ms=int(LIMITS.mistral_seconds * 1000),
                )
            )
            search = VectorRetriever(
                client=index,
                embedder=MistralEmbedder(client=sdk, model_name="mistral-embed"),
            )
            jobs = (
                [("adhoc", config.question)]
                if config.question is not None
                else [(i, cases[i].question.query) for i in config.question_ids]
            )
            failed = False
            for position, (question_id, query) in enumerate(jobs):
                arms: tuple[Arm, ...] = (
                    ("baseline", "agentic")
                    if position % 2 == 0
                    else ("agentic", "baseline")
                )
                if config.question is not None:
                    arms = ("agentic",)
                else:
                    append_record(output / "questions.jsonl", cases[question_id])
                for arm in arms:
                    trace.question_id, trace.case_attempts = question_id, 0
                    trace.case_limit = (
                        LIMITS.agentic_attempts
                        if arm == "agentic"
                        else LIMITS.baseline_attempts
                    )
                    research = Research(search, index, config.corpus_dir, trace, arm)
                    outcome, fatal = await answer_case(
                        query, research, sdk, cases.get(question_id)
                    )
                    append_record(output / "answers.jsonl", outcome)
                    failed |= isinstance(outcome.result, str)
                    print(f"{question_id} {arm}: {outcome.result}", flush=True)
                    if fatal or trace.run_attempts >= LIMITS.run_attempts:
                        raise RunError(
                            f"Stopped at {question_id}/{arm}; inspect answers.jsonl and http.jsonl"
                        )
            if (
                await audit_index(vespa, config.corpus_dir) != audit
                or local_container(config) != container_id
            ):
                raise RunError("Local corpus or container changed during the run")
            manifest.status = "partial" if failed else "completed"
            return int(failed)
        except BaseException as exc:
            manifest.status, manifest.error = "error", failure(exc)[0]
            if isinstance(exc, Exception):
                raise RuntimeError(manifest.error) from None
            raise
        finally:
            (output / "run.json").write_text(manifest.model_dump_json(indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare local baseline and Agentic Search"
    )
    for name in (
        "vespa-endpoint",
        "vespa-container",
        "corpus-dir",
        "freeze",
        "output-dir",
    ):
        parser.add_argument(f"--{name}", required=True)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--question", help="Run one question with agentic retrieval")
    selection.add_argument("--question-ids", nargs="+", default=PANEL)
    raise SystemExit(
        asyncio.run(run(RunConfig.model_validate(vars(parser.parse_args()))))
    )


if __name__ == "__main__":
    main()
