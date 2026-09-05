"""Small, explicit inputs shared by the retrieval evaluation tests."""

from docstral_backend import RetrievedChunk

DOCS = "https://docs.mistral.ai"
CONTENT_HASH = "a" * 64


def positive_payload(
    *,
    question_id: str = "candidate-001",
    query: str = "What is the evidence?",
    source_id: str = f"{DOCS}/guide",
    content_hash: str = "a" * 64,
    excerpt: str = "Evidence",
    claim: str = "The evidence answers the question.",
    tags: list[str] | None = None,
    pair_id: str | None = None,
    style: str = "precise",
) -> dict[str, object]:
    return {
        "id": question_id,
        "query": query,
        "language": "en",
        "style": style,
        "tags": tags if tags is not None else ["test"],
        "pair_id": pair_id,
        "evidence_groups": [
            {
                "claim": claim,
                "alternatives": [
                    {
                        "source_id": source_id,
                        "content_hash": content_hash,
                        "excerpt": excerpt,
                    }
                ],
            }
        ],
    }


def negative_payload(
    *,
    query: str = "Unknown question",
    reason: str = "The corpus does not contain the requested fact.",
) -> dict[str, object]:
    return {
        "id": "negative-candidate-001",
        "query": query,
        "language": "en",
        "style": "natural_contextual",
        "reason": reason,
    }


def make_chunk(
    *,
    rank: int,
    source_id: str,
    content: str,
    content_hash: str = CONTENT_HASH,
    chunk_id: str | None = None,
    score: float | None = None,
) -> RetrievedChunk:
    return RetrievedChunk(
        rank=rank,
        id=chunk_id if chunk_id is not None else f"chunk-{rank}",
        source_id=source_id,
        title="Guide",
        content_hash=content_hash,
        locator=f"char:{rank}-{rank + len(content)}",
        start_offset=rank,
        end_offset=rank + len(content),
        content=content,
        score=score if score is not None else 1.0 / rank,
    )
