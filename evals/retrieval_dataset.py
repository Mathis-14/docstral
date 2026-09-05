"""Check the evaluation questions and their manually annotated evidence.

load_dataset checks the question files; validate_corpus checks their gold passages
against the local Markdown. Neither function calls a model or Vespa.
"""

from collections import defaultdict
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from mistralai.search.toolkit.ingestion.text_splitters import (
    MarkdownTokenTextSplitter,
    MarkdownTokenTextSplitterConfig,
)
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

QuestionStyle = Literal[
    "precise",
    "natural_contextual",
    "vague_but_answerable",
    "telegraphic",
    "noisy_realistic",
]

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_POSITIVE_ID_PATTERN = r"^candidate-[0-9]{3}$"
_NEGATIVE_ID_PATTERN = r"^negative-candidate-[0-9]{3}$"
_PAIR_ID_PATTERN = r"^intent-[0-9]{3}$"


class DatasetError(Exception):
    """A retrieval evaluation dataset is invalid or unreadable."""


class CorpusValidationError(DatasetError):
    """The local Markdown corpus does not satisfy the dataset gold."""


def _non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("text must not be blank")
    return value


NonBlankText = Annotated[str, AfterValidator(_non_blank)]


class EvidenceAlternative(BaseModel):
    """One exact passage that can satisfy an evidence group."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    content_hash: str = Field(pattern=_SHA256_PATTERN)
    excerpt: NonBlankText

    @field_validator("source_id")
    @classmethod
    def _source_id_must_be_canonical(cls, value: str) -> str:
        parts = urlsplit(value)
        try:
            port = parts.port
        except ValueError as exc:
            raise ValueError("source_id has an invalid port") from exc
        path = parts.path
        segments = path.strip("/").split("/") if path else []
        if (
            parts.scheme != "https"
            or parts.hostname != "docs.mistral.ai"
            or parts.netloc != "docs.mistral.ai"
            or port is not None
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or not path
            or (path != "/" and path.endswith("/"))
            or path != path.lower()
            or path == "/en"
            or path.startswith("/en/")
            or any(segment in {".", ".."} for segment in segments)
        ):
            raise ValueError("source_id must be a canonical Mistral documentation URL")
        return value


class EvidenceGroup(BaseModel):
    """A required claim with one or more equivalent evidence alternatives."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: NonBlankText
    alternatives: tuple[EvidenceAlternative, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _alternatives_must_be_distinct(self) -> "EvidenceGroup":
        if len(self.alternatives) != len(set(self.alternatives)):
            raise ValueError("evidence alternatives must be distinct")
        return self


class PositiveQuestion(BaseModel):
    """One answerable retrieval question and its required evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=_POSITIVE_ID_PATTERN)
    query: NonBlankText
    language: Literal["en"]
    style: QuestionStyle
    tags: tuple[str, ...] = Field(min_length=1)
    pair_id: str | None = Field(default=None, pattern=_PAIR_ID_PATTERN)
    evidence_groups: tuple[EvidenceGroup, ...] = Field(min_length=1)

    @field_validator("tags")
    @classmethod
    def _tags_must_be_non_blank_and_distinct(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if any(not tag.strip() for tag in value):
            raise ValueError("tags must not be blank")
        if len(value) != len(set(value)):
            raise ValueError("tags must be distinct")
        return value


class NegativeQuestion(BaseModel):
    """One question whose answer is absent from the indexed corpus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=_NEGATIVE_ID_PATTERN)
    query: NonBlankText
    language: Literal["en"]
    style: QuestionStyle
    reason: NonBlankText


class RetrievalDataset(BaseModel):
    """The complete positive and negative retrieval development set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    positives: tuple[PositiveQuestion, ...] = Field(min_length=1)
    negatives: tuple[NegativeQuestion, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _questions_must_be_unique(self) -> "RetrievalDataset":
        questions = self.positives + self.negatives
        ids = [question.id for question in questions]
        queries = [question.query for question in questions]
        if len(ids) != len(set(ids)):
            raise ValueError("question IDs must be distinct")
        if len(queries) != len(set(queries)):
            raise ValueError("question queries must be distinct")
        return self

    @model_validator(mode="after")
    def _pairs_must_compare_the_same_evidence(self) -> "RetrievalDataset":
        """A pair changes only the wording: one precise and one natural question."""
        pairs: dict[str, list[PositiveQuestion]] = defaultdict(list)
        for question in self.positives:
            if question.pair_id is not None:
                pairs[question.pair_id].append(question)
        for pair_id, pair in pairs.items():
            if len(pair) != 2:
                raise ValueError(f"pair {pair_id!r} must contain exactly two questions")
            if {question.style for question in pair} != {
                "precise",
                "natural_contextual",
            }:
                raise ValueError(
                    f"pair {pair_id!r} must contain precise and natural_contextual styles"
                )
            if pair[0].evidence_groups != pair[1].evidence_groups:
                raise ValueError(f"pair {pair_id!r} must share identical evidence")
        return self


class LoadedDataset(BaseModel):
    """Validated questions and fingerprints of the exact JSONL bytes loaded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset: RetrievalDataset
    positive_sha256: str = Field(pattern=_SHA256_PATTERN)
    negative_sha256: str = Field(pattern=_SHA256_PATTERN)


def load_dataset(positive_path: Path, negative_path: Path) -> LoadedDataset:
    """Load and fingerprint both JSONL files from a single read of each."""
    positives, positive_sha256 = _load_jsonl(positive_path, PositiveQuestion)
    negatives, negative_sha256 = _load_jsonl(negative_path, NegativeQuestion)
    try:
        dataset = RetrievalDataset(positives=positives, negatives=negatives)
    except ValidationError as exc:
        raise DatasetError(f"Retrieval dataset is inconsistent: {exc}") from exc
    return LoadedDataset(
        dataset=dataset,
        positive_sha256=positive_sha256,
        negative_sha256=negative_sha256,
    )


def validate_corpus(dataset: RetrievalDataset, corpus_dir: Path) -> None:
    """Verify every gold passage against the exact local corpus and splitter."""
    if not corpus_dir.is_dir():
        raise CorpusValidationError(
            f"Markdown corpus directory {str(corpus_dir)!r} does not exist"
        )

    splitter = MarkdownTokenTextSplitter(
        MarkdownTokenTextSplitterConfig(
            chunk_size=800,
            chunk_max_size=800,
            chunk_overlap=0,
        )
    )
    for question in dataset.positives:
        for group_number, group in enumerate(question.evidence_groups, start=1):
            for alternative in group.alternatives:
                path = corpus_dir / _markdown_filename(alternative.source_id)
                try:
                    markdown = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    raise CorpusValidationError(
                        f"Cannot read gold source {alternative.source_id!r} "
                        f"at {str(path)!r}: {exc}"
                    ) from exc
                actual_hash = sha256(markdown.encode()).hexdigest()
                context = (
                    f"question {question.id!r}, evidence group {group_number}, "
                    f"source {alternative.source_id!r}"
                )
                if actual_hash != alternative.content_hash:
                    raise CorpusValidationError(
                        f"Gold content hash mismatch for {context}: expected "
                        f"{alternative.content_hash}, found {actual_hash}"
                    )
                occurrences = markdown.count(alternative.excerpt)
                if occurrences != 1:
                    raise CorpusValidationError(
                        f"Gold excerpt for {context} must occur exactly once; "
                        f"found {occurrences} occurrences"
                    )
                chunks = splitter.split_text(markdown)
                if not any(alternative.excerpt in chunk.content for chunk in chunks):
                    raise CorpusValidationError(
                        f"Gold excerpt for {context} crosses the configured chunk boundaries"
                    )


def _load_jsonl[Model: BaseModel](
    path: Path, model: type[Model]
) -> tuple[tuple[Model, ...], str]:
    """Read once, returning the questions and the hash of the exact file bytes."""
    try:
        content = path.read_bytes()
        lines = content.decode("utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise DatasetError(f"Cannot read dataset {str(path)!r}: {exc}") from exc
    if not lines:
        raise DatasetError(f"Dataset {str(path)!r} must not be empty")

    values: list[Model] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise DatasetError(
                f"Dataset {str(path)!r}:{line_number} contains a blank line"
            )
        try:
            values.append(model.model_validate_json(line))
        except ValidationError as exc:
            raise DatasetError(
                f"Invalid dataset entry at {str(path)!r}:{line_number}: {exc}"
            ) from exc
    return tuple(values), sha256(content).hexdigest()


def _markdown_filename(source_id: str) -> str:
    path = urlsplit(source_id).path.strip("/")
    slug = path.replace("/", "__") if path else "index"
    return f"{slug}.md"
