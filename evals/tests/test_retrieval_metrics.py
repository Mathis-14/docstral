import pytest

from evals.retrieval_dataset import (
    EvidenceAlternative,
    EvidenceGroup,
    PositiveQuestion,
    QuestionStyle,
)
from evals.retrieval_metrics import evaluate_question, summarize
from evals.tests.helpers import CONTENT_HASH, DOCS, make_chunk


def test_metrics_cut_before_matching_and_keep_original_rank() -> None:
    question = _question()
    chunks = (
        make_chunk(rank=1, source_id=f"{DOCS}/guide", content="Wrong passage"),
        make_chunk(rank=2, source_id=f"{DOCS}/guide", content="Exact evidence"),
    )

    evaluation = evaluate_question(question, chunks, cutoffs=(1, 2))

    at_one, at_two = evaluation.metrics
    assert at_one.evidence_recall == 0.0
    assert at_one.source_hit == 1.0
    assert at_one.reciprocal_rank == 0.0
    assert at_two.evidence_recall == 1.0
    assert at_two.all_required == 1.0
    assert at_two.reciprocal_rank == 0.5


def test_groups_are_and_alternatives_are_or_without_duplicate_credit() -> None:
    question = _question(
        groups=(
            EvidenceGroup(
                claim="First claim",
                alternatives=(
                    _alternative(f"{DOCS}/first", "First evidence"),
                    _alternative(f"{DOCS}/alternate", "Alternate evidence"),
                ),
            ),
            EvidenceGroup(
                claim="Second claim",
                alternatives=(_alternative(f"{DOCS}/second", "Second evidence"),),
            ),
        )
    )
    chunks = (
        make_chunk(
            rank=1,
            source_id=f"{DOCS}/alternate",
            content="Alternate evidence",
        ),
        make_chunk(
            rank=2,
            source_id=f"{DOCS}/alternate",
            content="Alternate evidence repeated",
        ),
        make_chunk(rank=3, source_id=f"{DOCS}/second", content="Second evidence"),
    )

    evaluation = evaluate_question(question, chunks, cutoffs=(2, 3))

    at_two, at_three = evaluation.metrics
    assert at_two.covered_groups == 1
    assert at_two.evidence_recall == 0.5
    assert at_two.all_required == 0.0
    assert at_two.duplicate_source_rate == 0.5
    assert at_three.covered_groups == 2
    assert at_three.evidence_recall == 1.0
    assert at_three.all_required == 1.0
    assert at_three.duplicate_source_rate == pytest.approx(1.0 - 2 / 3)


def test_empty_results_have_zero_metrics() -> None:
    (metrics,) = evaluate_question(_question(), (), cutoffs=(5,)).metrics

    assert metrics.covered_groups == 0
    assert metrics.evidence_recall == 0.0
    assert metrics.all_required == 0.0
    assert metrics.reciprocal_rank == 0.0
    assert metrics.source_hit == 0.0
    assert metrics.duplicate_source_rate == 0.0


def test_one_chunk_covers_multiple_groups_without_compressing_rank() -> None:
    group = _question().evidence_groups[0]
    question = _question(groups=(group, group))
    chunk = make_chunk(rank=3, source_id=f"{DOCS}/guide", content="Exact evidence")

    (metrics,) = evaluate_question(question, (chunk,), cutoffs=(1,)).metrics

    assert metrics.covered_groups == 2
    assert metrics.evidence_recall == 1.0
    assert metrics.all_required == 1.0
    assert metrics.reciprocal_rank == pytest.approx(1 / 3)


@pytest.mark.parametrize(
    ("source_id", "content", "content_hash"),
    [
        pytest.param(
            f"{DOCS}/guide",
            "Exact evidence",
            "b" * 64,
            id="wrong-hash",
        ),
        pytest.param(
            f"{DOCS}/guide",
            "Other passage",
            CONTENT_HASH,
            id="wrong-passage",
        ),
        pytest.param(
            f"{DOCS}/other",
            "Exact evidence",
            CONTENT_HASH,
            id="wrong-source",
        ),
    ],
)
def test_evidence_match_requires_source_hash_and_excerpt(
    source_id: str, content: str, content_hash: str
) -> None:
    chunk = make_chunk(
        rank=1,
        source_id=source_id,
        content=content,
        content_hash=content_hash,
    )
    (metrics,) = evaluate_question(_question(), (chunk,), cutoffs=(1,)).metrics

    assert metrics.evidence_recall == 0.0
    assert metrics.reciprocal_rank == 0.0


def test_summary_aggregates_styles_and_frozen_pairs() -> None:
    precise = _question(
        question_id="candidate-001", style="precise", pair_id="intent-001"
    )
    natural = _question(
        question_id="candidate-002",
        query="Natural question",
        style="natural_contextual",
        pair_id="intent-001",
    )
    vague = _question(
        question_id="candidate-003",
        query="vague question",
        style="vague_but_answerable",
    )
    first = make_chunk(rank=1, source_id=f"{DOCS}/guide", content="Exact evidence")
    irrelevant = make_chunk(rank=1, source_id=f"{DOCS}/other", content="Other")
    second = make_chunk(rank=2, source_id=f"{DOCS}/guide", content="Exact evidence")
    evaluations = (
        evaluate_question(precise, (first,), cutoffs=(1, 2)),
        evaluate_question(natural, (irrelevant, second), cutoffs=(1, 2)),
        evaluate_question(vague, (), cutoffs=(1, 2)),
    )

    summary = summarize(evaluations)

    assert summary.overall[0].macro_evidence_recall == pytest.approx(1 / 3)
    assert summary.overall[1].macro_evidence_recall == pytest.approx(2 / 3)
    assert summary.overall[1].mrr == pytest.approx(0.5)
    assert [item.style for item in summary.by_style] == [
        "natural_contextual",
        "precise",
        "vague_but_answerable",
    ]
    assert len(summary.pairs) == 1
    pair = summary.pairs[0]
    assert pair.pair_id == "intent-001"
    assert pair.precise_id == "candidate-001"
    assert pair.natural_id == "candidate-002"
    assert pair.metrics[0].precise_evidence_recall == 1.0
    assert pair.metrics[0].natural_evidence_recall == 0.0
    assert pair.metrics[1].natural_reciprocal_rank == 0.5
    assert [
        [(m.question_count, m.macro_evidence_recall, m.mrr) for m in style.metrics]
        for style in summary.by_style
    ] == [
        [(1, 0.0, 0.0), (1, 1.0, 0.5)],
        [(1, 1.0, 1.0), (1, 1.0, 1.0)],
        [(1, 0.0, 0.0), (1, 0.0, 0.0)],
    ]


def test_summary_distinguishes_macro_and_micro_evidence_recall() -> None:
    single_group = _question()
    three_groups = _question(
        question_id="candidate-002",
        query="What are the three requirements?",
        groups=tuple(
            EvidenceGroup(
                claim=f"Required claim {number}",
                alternatives=(_alternative(f"{DOCS}/guide", f"Evidence {number}"),),
            )
            for number in range(3)
        ),
    )
    relevant = make_chunk(rank=1, source_id=f"{DOCS}/guide", content="Exact evidence")

    summary = summarize(
        (
            evaluate_question(single_group, (relevant,), cutoffs=(5,)),
            evaluate_question(three_groups, (), cutoffs=(5,)),
        )
    )

    (aggregate,) = summary.overall
    assert aggregate.macro_evidence_recall == 0.5
    assert aggregate.micro_evidence_recall == 0.25
    assert aggregate.all_required_rate == 0.5


@pytest.mark.parametrize("cutoffs", [(), (0,), (3, 1), (1, 1)])
def test_metrics_reject_invalid_cutoffs(cutoffs: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="cutoff"):
        evaluate_question(_question(), (), cutoffs=cutoffs)


def _question(
    *,
    question_id: str = "candidate-001",
    query: str = "What is the evidence?",
    style: QuestionStyle = "precise",
    pair_id: str | None = None,
    groups: tuple[EvidenceGroup, ...] | None = None,
) -> PositiveQuestion:
    if groups is None:
        groups = (
            EvidenceGroup(
                claim="The evidence answers the question.",
                alternatives=(_alternative(f"{DOCS}/guide", "Exact evidence"),),
            ),
        )
    return PositiveQuestion(
        id=question_id,
        query=query,
        language="en",
        style=style,
        tags=("test",),
        pair_id=pair_id,
        evidence_groups=groups,
    )


def _alternative(source_id: str, excerpt: str) -> EvidenceAlternative:
    return EvidenceAlternative(
        source_id=source_id, content_hash=CONTENT_HASH, excerpt=excerpt
    )
