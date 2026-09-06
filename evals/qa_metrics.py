"""Aggregate saved Q&A outcomes and native Ragas scores; no I/O or model calls."""

from collections import Counter
from statistics import mean

from pydantic import BaseModel, ConfigDict

from evals.qa_runtime import METRICS, CaseResult, MetricScore
from evals.retrieval_dataset import PositiveQuestion
from evals.retrieval_metrics import RetrievalSummary, evaluate_question, summarize


class MetricAggregate(BaseModel):
    model_config = ConfigDict(frozen=True)

    mean: float | None
    scored: int
    skipped: int
    undefined: int
    missing: int


class QASummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    processed_questions: int
    valid_responses: int
    positive_count: int
    negative_count: int
    positive_abstentions: int
    negative_abstentions: int
    positive_generation_errors: int
    negative_generation_errors: int
    metrics: dict[str, MetricAggregate]
    retrieval: RetrievalSummary | None


def summarize_answers(
    answers: tuple[CaseResult, ...],
    scores: tuple[MetricScore, ...],
    *,
    total_questions: int | None = None,
) -> QASummary:
    expected = len(answers) if total_questions is None else total_questions
    positives = tuple(a for a in answers if isinstance(a.question, PositiveQuestion))
    negatives = tuple(
        a for a in answers if not isinstance(a.question, PositiveQuestion)
    )
    metrics: dict[str, MetricAggregate] = {}
    for name in METRICS:
        selected = [s for s in scores if s.metric == name]
        values = [s.value for s in selected if s.status == "ok" and s.value is not None]
        metrics[name] = MetricAggregate(
            mean=mean(values) if values else None,
            scored=len(values),
            skipped=sum(s.status == "skipped" for s in selected),
            undefined=sum(s.status == "undefined" for s in selected),
            missing=expected - len(selected),
        )
    retrieval = tuple(
        evaluate_question(a.question, a.chunks, cutoffs=(1, 3, 5))
        for a in positives
        if isinstance(a.question, PositiveQuestion)
    )
    # Partial runs need not contain both pair members.
    pair_counts = Counter(item.pair_id for item in retrieval if item.pair_id)
    retrieval = tuple(
        item.model_copy(update={"pair_id": None})
        if item.pair_id and pair_counts[item.pair_id] != 2
        else item
        for item in retrieval
    )
    return QASummary(
        processed_questions=len(answers),
        valid_responses=sum(a.response is not None for a in answers),
        positive_count=len(positives),
        negative_count=len(negatives),
        positive_abstentions=sum(a.status == "abstained" for a in positives),
        negative_abstentions=sum(a.status == "abstained" for a in negatives),
        positive_generation_errors=sum(a.response is None for a in positives),
        negative_generation_errors=sum(a.response is None for a in negatives),
        metrics=metrics,
        retrieval=summarize(retrieval) if retrieval else None,
    )
