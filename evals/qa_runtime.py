"""Shared observation and native Ragas adapters for local Q&A evaluations."""

import asyncio
import math
import os
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Literal, Protocol, Self

import httpx
from docstral_backend import (
    AnswerResponse,
    RetrievalRequest,
    RetrievalResponse,
    RetrievedChunk,
)
from mistralai.client.errors import MistralError
from openai import APIStatusError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    model_validator,
)

from evals.qa_dataset import validate_observed_chunk
from evals.retrieval_dataset import NegativeQuestion, PositiveQuestion

if TYPE_CHECKING:
    from openai import AsyncOpenAI

JUDGE_MODEL = "mistral-medium-3-5"
METRICS = ("faithfulness", "factual_correctness_f1", "factual_correctness_recall")
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class CaseResult(BaseModel):
    """A processed question, including rejected model output; never a silent skip."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question: PositiveQuestion | NegativeQuestion
    reference: str | None
    chunks: tuple[RetrievedChunk, ...]
    response: AnswerResponse | None = None
    generation_error: str | None = Field(default=None, min_length=1)
    duration_seconds: float

    @model_validator(mode="after")
    def response_or_error(self) -> Self:
        if (self.response is None) == (self.generation_error is None):
            raise ValueError("Exactly one response or generation error is required")
        return self

    @property
    def status(self) -> str:
        if self.response is None:
            return "generation_error"
        return "abstained" if self.response.abstained else "answered"


class MetricScore(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    question_id: str
    metric: str
    status: Literal["ok", "skipped", "undefined"]
    value: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    reason: str | None = None

    @model_validator(mode="after")
    def consistent_status(self) -> "MetricScore":
        if (self.status == "ok") != (self.value is not None):
            raise ValueError("Only an ok score has a numeric value")
        if self.status != "ok" and not self.reason:
            raise ValueError("Skipped/undefined scores need an explanation")
        return self


class HTTPExchange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    question_id: str
    step: str
    path: str
    request: JsonValue
    response: JsonValue
    status_code: int | None
    duration_seconds: float


def append_record(path: Path, record: BaseModel) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(record.model_dump_json() + "\n")


def read_records[Model: BaseModel](path: Path, model: type[Model]) -> tuple[Model, ...]:
    """Missing files represent work not started; malformed records fail loudly."""
    if not path.exists():
        return ()
    return tuple(
        model.model_validate_json(line) for line in path.read_bytes().splitlines()
    )


def is_rate_limit(error: BaseException) -> bool:
    """Inspect SDK status codes through toolkit/Instructor wrappers, not messages."""
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, (MistralError, APIStatusError)):
            return cause.status_code == 429
        cause = cause.__cause__
    return False


class TraceTransport(httpx.AsyncBaseTransport):
    """Observe actual HTTP attempts, including retries; never persist headers."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        destination: Path,
        *,
        delay_seconds: float = 0.0,
    ) -> None:
        self.inner = inner
        self.destination = destination
        self.question_id = ""
        self.step = ""
        self.delay_seconds = delay_seconds

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(self.delay_seconds)
        started = monotonic()
        response: httpx.Response | None = None
        response_data: JsonValue = None
        try:
            response = await self.inner.handle_async_request(request)
            body = await response.aread()
            try:
                response_data = _JSON.validate_json(body)
            except ValueError:
                response_data = {"non_json_response": response.text}
            return response
        except httpx.TransportError as exc:
            response_data = {"transport_error": type(exc).__name__}
            raise
        finally:
            append_record(
                self.destination,
                HTTPExchange(
                    question_id=self.question_id,
                    step=self.step,
                    path=request.url.path,
                    request=_JSON.validate_json(request.content),
                    response=response_data,
                    status_code=response.status_code if response is not None else None,
                    duration_seconds=monotonic() - started,
                ),
            )

    async def aclose(self) -> None:
        await self.inner.aclose()


class _Retriever(Protocol):
    async def retrieve(self, request: RetrievalRequest) -> RetrievalResponse: ...


class RecordingRetriever:
    """Observe the answerer's one retrieval, without another search or sorting."""

    def __init__(
        self, retriever: _Retriever, *, corpus_dir: Path | None = None
    ) -> None:
        self.retriever = retriever
        self.corpus_dir = corpus_dir
        self.chunks: tuple[RetrievedChunk, ...] = ()

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResponse:
        self.chunks = ()
        response = await self.retriever.retrieve(request)
        if self.corpus_dir is not None:
            for chunk in response.chunks:
                validate_observed_chunk(chunk, self.corpus_dir)
        self.chunks = response.chunks
        return response


def record_score(question_id: str, metric: str, value: float) -> MetricScore:
    if not math.isfinite(value):
        return MetricScore(
            question_id=question_id,
            metric=metric,
            status="undefined",
            reason="Ragas returned a non-finite score; inspect the judge traces.",
        )
    return MetricScore(question_id=question_id, metric=metric, status="ok", value=value)


async def score_case(
    case: CaseResult,
    client: "AsyncOpenAI",
    trace: TraceTransport,
    *,
    metric_names: tuple[str, ...] = METRICS,
) -> tuple[MetricScore, ...]:
    if not metric_names or not set(metric_names) <= set(METRICS):
        raise ValueError("Unknown or empty Ragas metric selection")
    # Import only in the eval path, after disabling optional Ragas telemetry.
    os.environ["RAGAS_DO_NOT_TRACK"] = "true"
    from ragas.llms import llm_factory
    from ragas.metrics.collections import FactualCorrectness, Faithfulness

    trace.question_id = case.question.id
    if case.response is None or case.reference is None or case.response.abstained:
        reason = case.generation_error or (
            "negative question" if case.reference is None else "answer abstained"
        )
        return tuple(
            MetricScore(
                question_id=case.question.id,
                metric=name,
                status="skipped",
                reason=reason,
            )
            for name in metric_names
        )

    llm = llm_factory(
        JUDGE_MODEL,
        client=client,
        provider="openai",
        adapter="instructor",
        temperature=0.0,
        top_p=1.0,
        max_tokens=4096,
        max_retries=3,
    )
    scores: list[MetricScore] = []
    for name in metric_names:
        trace.step = name
        if name == "faithfulness":
            result = await Faithfulness(llm=llm).ascore(
                user_input=case.question.query,
                response=case.response.answer,
                retrieved_contexts=[chunk.content for chunk in case.chunks],
            )
        else:
            mode: Literal["f1", "recall"] = "f1" if name.endswith("_f1") else "recall"
            result = await FactualCorrectness(
                llm=llm, mode=mode, atomicity="high", coverage="high"
            ).ascore(response=case.response.answer, reference=case.reference)
        scores.append(record_score(case.question.id, name, float(result)))
    return tuple(scores)
