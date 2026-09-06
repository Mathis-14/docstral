"""Load a frozen Q&A development set and check its snapshot-backed evidence."""

from hashlib import sha256
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from docstral_backend import RetrievedChunk
from pydantic import BaseModel, ConfigDict, Field, model_validator

from evals.retrieval_dataset import (
    EvidenceAlternative,
    NegativeQuestion,
    NonBlankText,
    PositiveQuestion,
    RetrievalDataset,
    validate_corpus,
)


class QACase(BaseModel):
    """One reviewed question; reference evidence is not retrieval gold."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question: PositiveQuestion | NegativeQuestion
    reference: NonBlankText | None
    additional_qa_evidence: tuple[EvidenceAlternative, ...] = ()

    @model_validator(mode="after")
    def reference_matches_question(self) -> Self:
        if isinstance(self.question, PositiveQuestion) != (self.reference is not None):
            raise ValueError("Exactly the positive questions must have a reference")
        if isinstance(self.question, NegativeQuestion) and self.additional_qa_evidence:
            raise ValueError(
                "Negative questions must not carry positive reference evidence"
            )
        return self


class DatasetFreeze(BaseModel):
    """Fingerprints and provenance of an approved set, not an unseen holdout."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["qa_dev_v1"] = "qa_dev_v1"
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot: str
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    positive_count: int = Field(ge=1)
    negative_count: int = Field(ge=1)
    frozen_at: str
    review_url: str
    ancestors: dict[str, str]
    changes: tuple[str, ...]
    provenance: str


class LoadedQA(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cases: tuple[QACase, ...]
    freeze: DatasetFreeze


def corpus_fingerprint(corpus_dir: Path) -> str:
    """Hash the ordered inventory and bytes of every Markdown page."""
    pages = sorted(corpus_dir.glob("*.md"))
    if not pages:
        raise ValueError(f"No Markdown pages in {corpus_dir}")
    inventory = "".join(
        f"{page.name}:{sha256(page.read_bytes()).hexdigest()}\n" for page in pages
    )
    return sha256(inventory.encode()).hexdigest()


def source_markdown(source_id: str, corpus_dir: Path) -> str:
    slug = urlsplit(source_id).path.strip("/").replace("/", "__") or "index"
    return (corpus_dir / f"{slug}.md").read_text(encoding="utf-8")


def validate_observed_chunk(chunk: RetrievedChunk, corpus_dir: Path) -> None:
    """Reject a stale or foreign hit before it reaches the answer model."""
    # Reuse the annotation boundary's URL/hash validation, not worker imports.
    EvidenceAlternative(
        source_id=chunk.source_id,
        content_hash=chunk.content_hash,
        excerpt=chunk.content,
    )
    markdown = source_markdown(chunk.source_id, corpus_dir)
    if sha256(markdown.encode()).hexdigest() != chunk.content_hash:
        raise ValueError(f"Indexed hash differs from snapshot: {chunk.source_id}")
    if chunk.content not in markdown:
        raise ValueError(f"Indexed content absent from snapshot: {chunk.id}")


def load_qa_dataset(path: Path, corpus_dir: Path, *, freeze_path: Path) -> LoadedQA:
    """Validate all inputs before any API call; never fill missing references."""
    raw = path.read_bytes()
    freeze = DatasetFreeze.model_validate_json(freeze_path.read_bytes())
    if sha256(raw).hexdigest() != freeze.dataset_sha256:
        raise ValueError("Q&A dataset differs from its freeze")
    if corpus_dir.parent.name != freeze.snapshot:
        raise ValueError("Snapshot identifier differs from the Q&A freeze")
    if corpus_fingerprint(corpus_dir) != freeze.corpus_sha256:
        raise ValueError("Markdown corpus differs from the Q&A freeze")
    cases = tuple(QACase.model_validate_json(line) for line in raw.splitlines())
    dataset = RetrievalDataset(
        positives=tuple(
            c.question for c in cases if isinstance(c.question, PositiveQuestion)
        ),
        negatives=tuple(
            c.question for c in cases if isinstance(c.question, NegativeQuestion)
        ),
    )
    if (len(dataset.positives), len(dataset.negatives)) != (
        freeze.positive_count,
        freeze.negative_count,
    ):
        raise ValueError("Q&A question counts differ from the freeze")
    validate_corpus(dataset, corpus_dir)
    for case in cases:
        for evidence in case.additional_qa_evidence:
            markdown = source_markdown(evidence.source_id, corpus_dir)
            if sha256(markdown.encode()).hexdigest() != evidence.content_hash:
                raise ValueError(
                    f"Reference evidence hash mismatch: {case.question.id}"
                )
            if markdown.count(evidence.excerpt) != 1:
                raise ValueError(
                    f"Reference evidence must occur once: {case.question.id}"
                )
    return LoadedQA(cases=cases, freeze=freeze)
