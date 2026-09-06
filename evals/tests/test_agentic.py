import pytest

from evals.agentic import LIMITS, ExperimentError, LimitExceeded, merge_evidence
from evals.tests.helpers import make_chunk


def test_evidence_preserves_first_consultation_and_keeps_distinct_chunks() -> None:
    first = make_chunk(rank=1, source_id="https://docs.mistral.ai/guide", content="A")
    second = make_chunk(rank=8, source_id=first.source_id, content="B")
    consulted = {first.id: first}
    repeated = first.model_copy(update={"rank": 7, "score": 0.2})

    merged = merge_evidence(consulted, (second, repeated))

    assert list(merged.values()) == [first, second.model_copy(update={"rank": 2})]
    assert consulted == {first.id: first}
    assert merge_evidence(merged, (second, repeated)) == merged


@pytest.mark.parametrize("field", ["content", "source_id"])
def test_evidence_rejects_changed_chunks(field: str) -> None:
    chunk = make_chunk(rank=1, source_id="https://docs.mistral.ai/guide", content="A")
    consulted = {chunk.id: chunk}

    with pytest.raises(ExperimentError, match="Chunk changed"):
        merge_evidence(consulted, (chunk.model_copy(update={field: "changed"}),))

    assert consulted == {chunk.id: chunk}


def test_evidence_limit_counts_unique_chunks() -> None:
    chunks = [
        make_chunk(rank=i + 1, source_id="https://docs.mistral.ai/guide", content="A")
        for i in range(LIMITS.chunks + 1)
    ]
    consulted = {chunk.id: chunk for chunk in chunks[:-1]}
    assert merge_evidence(consulted, chunks[:-1]) == consulted

    with pytest.raises(LimitExceeded, match="chunk limit"):
        merge_evidence(consulted, (chunks[-1],))

    assert len(consulted) == LIMITS.chunks
