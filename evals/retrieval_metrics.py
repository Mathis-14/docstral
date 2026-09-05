"""Measure whether retrieved chunks contain the annotated evidence.

evaluate_question scores one ranked result list; summarize averages the scores
across questions, styles, and precise/natural pairs. No I/O or model calls.
"""

from collections import defaultdict
from collections.abc import Sequence

from docstral_backend import RetrievedChunk
from pydantic import BaseModel, ConfigDict, Field

from evals.retrieval_dataset import EvidenceGroup, PositiveQuestion, QuestionStyle

DEFAULT_CUTOFFS = (1, 3, 5, 10)


class MetricsAtK(BaseModel):
    """Metrics for one question after cutting the chunk ranking at K."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    k: int = Field(ge=1)
    covered_groups: int = Field(ge=0)
    total_groups: int = Field(ge=1)
    evidence_recall: float = Field(ge=0.0, le=1.0)
    all_required: float = Field(ge=0.0, le=1.0)
    reciprocal_rank: float = Field(ge=0.0, le=1.0)
    source_hit: float = Field(ge=0.0, le=1.0)
    duplicate_source_rate: float = Field(ge=0.0, le=1.0)


class QuestionEvaluation(BaseModel):
    """All configured cutoffs for one positive question."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question_id: str
    style: QuestionStyle
    pair_id: str | None
    metrics: tuple[MetricsAtK, ...] = Field(min_length=1)


class AggregateAtK(BaseModel):
    """Macro and micro aggregates for a group of questions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    k: int = Field(ge=1)
    question_count: int = Field(ge=1)
    macro_evidence_recall: float = Field(ge=0.0, le=1.0)
    micro_evidence_recall: float = Field(ge=0.0, le=1.0)
    all_required_rate: float = Field(ge=0.0, le=1.0)
    mrr: float = Field(ge=0.0, le=1.0)
    source_hit_rate: float = Field(ge=0.0, le=1.0)
    duplicate_source_rate: float = Field(ge=0.0, le=1.0)


class StyleSummary(BaseModel):
    """Aggregated metrics for one query style."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    style: QuestionStyle
    metrics: tuple[AggregateAtK, ...]


class PairAtK(BaseModel):
    """Side-by-side precise and natural metrics for one intent at K."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    k: int = Field(ge=1)
    precise_evidence_recall: float = Field(ge=0.0, le=1.0)
    natural_evidence_recall: float = Field(ge=0.0, le=1.0)
    precise_reciprocal_rank: float = Field(ge=0.0, le=1.0)
    natural_reciprocal_rank: float = Field(ge=0.0, le=1.0)
    precise_all_required: float = Field(ge=0.0, le=1.0)
    natural_all_required: float = Field(ge=0.0, le=1.0)


class PairComparison(BaseModel):
    """The two frozen formulations of one shared intent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pair_id: str
    precise_id: str
    natural_id: str
    metrics: tuple[PairAtK, ...]


class RetrievalSummary(BaseModel):
    """Overall, per-style, and paired retrieval aggregates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overall: tuple[AggregateAtK, ...]
    by_style: tuple[StyleSummary, ...]
    pairs: tuple[PairComparison, ...]


def evaluate_question(
    question: PositiveQuestion,
    chunks: Sequence[RetrievedChunk],
    cutoffs: Sequence[int] = DEFAULT_CUTOFFS,
) -> QuestionEvaluation:
    """Evaluate exact annotated evidence without changing the chunk ranking."""
    normalized_cutoffs = _validate_cutoffs(cutoffs)
    total_groups = len(question.evidence_groups)
    gold_sources = {
        alternative.source_id
        for group in question.evidence_groups
        for alternative in group.alternatives
    }
    metrics: list[MetricsAtK] = []
    for k in normalized_cutoffs:
        top_chunks = tuple(chunks[:k])
        first_ranks = [
            _first_evidence_rank(group, top_chunks)
            for group in question.evidence_groups
        ]
        matched_ranks = [rank for rank in first_ranks if rank > 0]
        covered_groups = len(matched_ranks)
        unique_sources = {chunk.source_id for chunk in top_chunks}
        duplicate_rate = (
            1.0 - len(unique_sources) / len(top_chunks) if top_chunks else 0.0
        )
        metrics.append(
            MetricsAtK(
                k=k,
                covered_groups=covered_groups,
                total_groups=total_groups,
                evidence_recall=covered_groups / total_groups,
                all_required=float(covered_groups == total_groups),
                reciprocal_rank=(1.0 / min(matched_ranks) if matched_ranks else 0.0),
                source_hit=float(
                    any(chunk.source_id in gold_sources for chunk in top_chunks)
                ),
                duplicate_source_rate=duplicate_rate,
            )
        )
    return QuestionEvaluation(
        question_id=question.id,
        style=question.style,
        pair_id=question.pair_id,
        metrics=tuple(metrics),
    )


def summarize(evaluations: Sequence[QuestionEvaluation]) -> RetrievalSummary:
    """Aggregate question metrics without treating duplicate chunks as new gold."""
    if not evaluations:
        raise ValueError("at least one question evaluation is required")

    by_style: dict[QuestionStyle, list[QuestionEvaluation]] = defaultdict(list)
    by_pair: dict[str, list[QuestionEvaluation]] = defaultdict(list)
    for evaluation in evaluations:
        by_style[evaluation.style].append(evaluation)
        if evaluation.pair_id is not None:
            by_pair[evaluation.pair_id].append(evaluation)

    style_summaries = tuple(
        StyleSummary(style=style, metrics=_aggregate(group))
        for style, group in sorted(by_style.items())
    )
    pair_comparisons = tuple(
        _compare_pair(pair_id, pair) for pair_id, pair in sorted(by_pair.items())
    )
    return RetrievalSummary(
        overall=_aggregate(evaluations),
        by_style=style_summaries,
        pairs=pair_comparisons,
    )


def _validate_cutoffs(cutoffs: Sequence[int]) -> tuple[int, ...]:
    normalized = tuple(cutoffs)
    if not normalized:
        raise ValueError("at least one cutoff is required")
    if any(k < 1 for k in normalized):
        raise ValueError("cutoffs must be positive")
    if tuple(sorted(set(normalized))) != normalized:
        raise ValueError("cutoffs must be distinct and sorted")
    return normalized


def _first_evidence_rank(group: EvidenceGroup, chunks: Sequence[RetrievedChunk]) -> int:
    """Return the best original rank containing an accepted passage, or zero."""
    ranks = (
        chunk.rank
        for chunk in chunks
        if any(
            chunk.source_id == alternative.source_id
            and chunk.content_hash == alternative.content_hash
            and alternative.excerpt in chunk.content
            for alternative in group.alternatives
        )
    )
    return min(ranks, default=0)


def _aggregate(
    evaluations: Sequence[QuestionEvaluation],
) -> tuple[AggregateAtK, ...]:
    if not evaluations:
        raise ValueError("at least one question evaluation is required")
    cutoffs = tuple(metric.k for metric in evaluations[0].metrics)
    if any(
        tuple(metric.k for metric in evaluation.metrics) != cutoffs
        for evaluation in evaluations[1:]
    ):
        raise ValueError("question evaluations must use identical cutoffs")

    aggregates: list[AggregateAtK] = []
    count = len(evaluations)
    for position, k in enumerate(cutoffs):
        metrics = [evaluation.metrics[position] for evaluation in evaluations]
        total_groups = sum(metric.total_groups for metric in metrics)
        aggregates.append(
            AggregateAtK(
                k=k,
                question_count=count,
                macro_evidence_recall=sum(metric.evidence_recall for metric in metrics)
                / count,
                micro_evidence_recall=sum(metric.covered_groups for metric in metrics)
                / total_groups,
                all_required_rate=sum(metric.all_required for metric in metrics)
                / count,
                mrr=sum(metric.reciprocal_rank for metric in metrics) / count,
                source_hit_rate=sum(metric.source_hit for metric in metrics) / count,
                duplicate_source_rate=sum(
                    metric.duplicate_source_rate for metric in metrics
                )
                / count,
            )
        )
    return tuple(aggregates)


def _compare_pair(pair_id: str, pair: Sequence[QuestionEvaluation]) -> PairComparison:
    if len(pair) != 2:
        raise ValueError(f"pair {pair_id!r} must contain exactly two evaluations")
    by_style = {evaluation.style: evaluation for evaluation in pair}
    try:
        precise = by_style["precise"]
        natural = by_style["natural_contextual"]
    except KeyError as exc:
        raise ValueError(
            f"pair {pair_id!r} must contain precise and natural_contextual styles"
        ) from exc
    if tuple(metric.k for metric in precise.metrics) != tuple(
        metric.k for metric in natural.metrics
    ):
        raise ValueError(f"pair {pair_id!r} must use identical cutoffs")

    metrics = tuple(
        PairAtK(
            k=precise_metric.k,
            precise_evidence_recall=precise_metric.evidence_recall,
            natural_evidence_recall=natural_metric.evidence_recall,
            precise_reciprocal_rank=precise_metric.reciprocal_rank,
            natural_reciprocal_rank=natural_metric.reciprocal_rank,
            precise_all_required=precise_metric.all_required,
            natural_all_required=natural_metric.all_required,
        )
        for precise_metric, natural_metric in zip(
            precise.metrics, natural.metrics, strict=True
        )
    )
    return PairComparison(
        pair_id=pair_id,
        precise_id=precise.question_id,
        natural_id=natural.question_id,
        metrics=metrics,
    )
