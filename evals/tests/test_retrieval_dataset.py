import json
from collections import Counter
from hashlib import sha256
from pathlib import Path

import pytest
from mistralai.search.toolkit.ingestion.text_splitters import (
    MarkdownTokenTextSplitter,
    MarkdownTokenTextSplitterConfig,
)

from evals.retrieval_dataset import (
    CorpusValidationError,
    DatasetError,
    NegativeQuestion,
    PositiveQuestion,
    RetrievalDataset,
    load_dataset,
    validate_corpus,
)
from evals.tests.helpers import DOCS, negative_payload, positive_payload

EVALS_ROOT = Path(__file__).parents[1]
POSITIVE_DATASET = EVALS_ROOT / "datasets/retrieval_dev_v1.jsonl"
NEGATIVE_DATASET = EVALS_ROOT / "datasets/retrieval_negatives_v1.jsonl"


def test_versioned_dataset_is_the_reviewed_frozen_pool() -> None:
    dataset = load_dataset(POSITIVE_DATASET, NEGATIVE_DATASET).dataset

    assert [question.id for question in dataset.positives] == [
        f"candidate-{number:03d}" for number in range(1, 63)
    ]
    assert [question.id for question in dataset.negatives] == [
        f"negative-candidate-{number:03d}" for number in range(1, 11)
    ]
    assert all(question.language == "en" for question in dataset.positives)
    assert all(question.language == "en" for question in dataset.negatives)
    pair_counts = Counter(
        question.pair_id
        for question in dataset.positives
        if question.pair_id is not None
    )
    assert len(pair_counts) == 15
    assert set(pair_counts.values()) == {2}
    builders = {
        question.id: question
        for question in dataset.positives
        if question.id in {"candidate-061", "candidate-062"}
    }
    assert set(builders) == {"candidate-061", "candidate-062"}
    assert all("builder" in question.tags for question in builders.values())
    assert all(len(question.evidence_groups) == 2 for question in builders.values())


def test_loader_fingerprints_original_bytes_before_newline_normalization(
    tmp_path: Path,
) -> None:
    positive_path, negative_path = _write_datasets(tmp_path, [positive_payload()])
    positive_bytes = positive_path.read_bytes().replace(b"\n", b"\r\n")
    positive_path.write_bytes(positive_bytes)
    negative_bytes = negative_path.read_bytes()

    loaded = load_dataset(positive_path, negative_path)

    assert loaded.dataset.positives[0].query == "What is the evidence?"
    assert loaded.positive_sha256 == sha256(positive_bytes).hexdigest()
    assert loaded.negative_sha256 == sha256(negative_bytes).hexdigest()


def test_loader_reports_the_invalid_jsonl_line(tmp_path: Path) -> None:
    positive_path, negative_path = _write_datasets(tmp_path, [positive_payload()])
    positive_path.write_text(
        f"{positive_path.read_text(encoding='utf-8')}{{\n", encoding="utf-8"
    )

    with pytest.raises(DatasetError, match=r"retrieval_dev\.jsonl':2"):
        load_dataset(positive_path, negative_path)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        pytest.param("query", "  ", "text must not be blank", id="blank-query"),
        pytest.param(
            "query", "\u001c", "text must not be blank", id="unicode-blank-query"
        ),
        pytest.param("language", "fr", "Input should be 'en'", id="language"),
        pytest.param("id", "question-1", "string_pattern_mismatch", id="id"),
    ],
)
def test_loader_rejects_invalid_question_fields(
    tmp_path: Path, field: str, value: object, match: str
) -> None:
    payload = positive_payload()
    payload[field] = value
    positive_path, negative_path = _write_datasets(tmp_path, [payload])

    with pytest.raises(DatasetError, match=match):
        load_dataset(positive_path, negative_path)


def test_loader_rejects_extra_fields(tmp_path: Path) -> None:
    payload = positive_payload()
    payload["unexpected"] = True
    positive_path, negative_path = _write_datasets(tmp_path, [payload])

    with pytest.raises(DatasetError, match="extra_forbidden"):
        load_dataset(positive_path, negative_path)


def test_loader_rejects_missing_required_field(tmp_path: Path) -> None:
    payload = positive_payload()
    del payload["evidence_groups"]
    positive_path, negative_path = _write_datasets(tmp_path, [payload])

    with pytest.raises(DatasetError, match="Field required"):
        load_dataset(positive_path, negative_path)


@pytest.mark.parametrize(
    ("field", "match"),
    [
        pytest.param("claim", "text must not be blank", id="claim"),
        pytest.param("excerpt", "text must not be blank", id="excerpt"),
        pytest.param("tag", "tags must not be blank", id="tag"),
        pytest.param("reason", "text must not be blank", id="negative-reason"),
    ],
)
@pytest.mark.parametrize("blank", [" ", "\u2003", "\u001c"])
def test_loader_rejects_blank_nested_text(
    tmp_path: Path, field: str, match: str, blank: str
) -> None:
    positive = positive_payload(
        claim=blank if field == "claim" else "The evidence answers the question.",
        excerpt=blank if field == "excerpt" else "Evidence",
        tags=[blank] if field == "tag" else ["test"],
    )
    negative = negative_payload()
    if field == "reason":
        negative["reason"] = blank
    positive_path, negative_path = _write_datasets(tmp_path, [positive], [negative])

    with pytest.raises(DatasetError, match=match):
        load_dataset(positive_path, negative_path)


@pytest.mark.parametrize(
    ("source_id", "content_hash", "match"),
    [
        pytest.param(
            "http://docs.mistral.ai/guide",
            "a" * 64,
            "canonical Mistral documentation URL",
            id="http-url",
        ),
        pytest.param(
            f"{DOCS}/Guide",
            "a" * 64,
            "canonical Mistral documentation URL",
            id="mixed-case-url",
        ),
        pytest.param(
            DOCS,
            "a" * 64,
            "canonical Mistral documentation URL",
            id="root-without-slash",
        ),
        pytest.param(
            f"{DOCS}/guide/",
            "a" * 64,
            "canonical Mistral documentation URL",
            id="trailing-slash",
        ),
        pytest.param(
            f"{DOCS}/guide",
            "not-a-hash",
            "string_pattern_mismatch",
            id="content-hash",
        ),
    ],
)
def test_loader_rejects_invalid_gold_identity(
    tmp_path: Path, source_id: str, content_hash: str, match: str
) -> None:
    positive_path, negative_path = _write_datasets(
        tmp_path,
        [positive_payload(source_id=source_id, content_hash=content_hash)],
    )

    with pytest.raises(DatasetError, match=match):
        load_dataset(positive_path, negative_path)


def test_positive_question_accepts_canonical_root_source() -> None:
    question = PositiveQuestion.model_validate(positive_payload(source_id=f"{DOCS}/"))

    assert question.evidence_groups[0].alternatives[0].source_id == f"{DOCS}/"


def test_dataset_rejects_duplicate_ids(tmp_path: Path) -> None:
    payload = positive_payload()
    positive_path, negative_path = _write_datasets(tmp_path, [payload, payload])

    with pytest.raises(DatasetError, match="question IDs must be distinct"):
        load_dataset(positive_path, negative_path)


def test_dataset_rejects_duplicate_queries_across_sets(tmp_path: Path) -> None:
    positive = positive_payload(query="Same question")
    negative = negative_payload(query="Same question")
    positive_path, negative_path = _write_datasets(tmp_path, [positive], [negative])

    with pytest.raises(DatasetError, match="question queries must be distinct"):
        load_dataset(positive_path, negative_path)


def test_dataset_rejects_duplicate_evidence_alternatives(tmp_path: Path) -> None:
    alternative = {
        "source_id": f"{DOCS}/guide",
        "content_hash": "a" * 64,
        "excerpt": "Evidence",
    }
    payload = positive_payload()
    payload["evidence_groups"] = [
        {
            "claim": "The evidence answers the question.",
            "alternatives": [alternative, alternative],
        }
    ]
    positive_path, negative_path = _write_datasets(tmp_path, [payload])

    with pytest.raises(DatasetError, match="alternatives must be distinct"):
        load_dataset(positive_path, negative_path)


@pytest.mark.parametrize(
    ("variant", "match"),
    [
        pytest.param("one", "exactly two questions", id="cardinality"),
        pytest.param("styles", "precise and natural_contextual", id="styles"),
        pytest.param("evidence", "identical evidence", id="evidence"),
    ],
)
def test_dataset_rejects_inconsistent_pairs(
    tmp_path: Path, variant: str, match: str
) -> None:
    first = positive_payload(pair_id="intent-001")
    positives = [first]
    if variant != "one":
        second = positive_payload(
            question_id="candidate-002",
            query="Natural version",
            pair_id="intent-001",
            style="precise" if variant == "styles" else "natural_contextual",
            excerpt="Different evidence" if variant == "evidence" else "Evidence",
        )
        positives.append(second)
    positive_path, negative_path = _write_datasets(tmp_path, positives)

    with pytest.raises(DatasetError, match=match):
        load_dataset(positive_path, negative_path)


def test_corpus_validation_accepts_exact_gold_in_one_real_chunk(
    tmp_path: Path,
) -> None:
    content = "# Guide\n\nExact evidence for the answer."
    dataset = _dataset_for_content(content, "Exact evidence")
    corpus_dir = _write_corpus(tmp_path, content)

    validate_corpus(dataset, corpus_dir)


def test_corpus_validation_rejects_missing_source(tmp_path: Path) -> None:
    content = "# Guide\n\nExact evidence."
    dataset = _dataset_for_content(content, "Exact evidence")
    corpus_dir = tmp_path / "pages"
    corpus_dir.mkdir()

    with pytest.raises(CorpusValidationError, match="Cannot read gold source"):
        validate_corpus(dataset, corpus_dir)


def test_corpus_validation_rejects_hash_mismatch(tmp_path: Path) -> None:
    content = "# Guide\n\nExact evidence."
    dataset = _dataset_for_content(content, "Exact evidence", content_hash="b" * 64)
    corpus_dir = _write_corpus(tmp_path, content)

    with pytest.raises(CorpusValidationError, match="content hash mismatch"):
        validate_corpus(dataset, corpus_dir)


@pytest.mark.parametrize(
    ("content", "excerpt", "occurrences"),
    [
        pytest.param("# Guide\n\nOther text.", "Missing", 0, id="missing"),
        pytest.param("# Guide\n\nRepeat. Repeat.", "Repeat", 2, id="duplicate"),
    ],
)
def test_corpus_validation_requires_one_exact_excerpt(
    tmp_path: Path, content: str, excerpt: str, occurrences: int
) -> None:
    dataset = _dataset_for_content(content, excerpt)
    corpus_dir = _write_corpus(tmp_path, content)

    with pytest.raises(CorpusValidationError, match=f"found {occurrences} occurrences"):
        validate_corpus(dataset, corpus_dir)


def test_corpus_validation_rejects_excerpt_across_real_chunk_boundary(
    tmp_path: Path,
) -> None:
    content = "# Guide\n\n" + " ".join(
        f"unique_token_{number:04d}" for number in range(2_000)
    )
    splitter = MarkdownTokenTextSplitter(
        MarkdownTokenTextSplitterConfig(
            chunk_size=800, chunk_max_size=800, chunk_overlap=0
        )
    )
    fragments = splitter.split_text(content)
    assert len(fragments) > 1
    boundary = fragments[1].end_offset
    excerpt = content[boundary - 20 : boundary + 20]
    assert content.count(excerpt) == 1
    dataset = _dataset_for_content(content, excerpt)
    corpus_dir = _write_corpus(tmp_path, content)

    with pytest.raises(CorpusValidationError, match=r"crosses.*chunk boundaries"):
        validate_corpus(dataset, corpus_dir)


def _write_datasets(
    root: Path,
    positives: list[dict[str, object]],
    negatives: list[dict[str, object]] | None = None,
) -> tuple[Path, Path]:
    positive_path = root / "retrieval_dev.jsonl"
    negative_path = root / "retrieval_negatives.jsonl"
    positive_path.write_text(
        "".join(f"{json.dumps(payload)}\n" for payload in positives),
        encoding="utf-8",
    )
    negative_payloads = negatives if negatives is not None else [negative_payload()]
    negative_path.write_text(
        "".join(f"{json.dumps(payload)}\n" for payload in negative_payloads),
        encoding="utf-8",
    )
    return positive_path, negative_path


def _dataset_for_content(
    content: str, excerpt: str, *, content_hash: str | None = None
) -> RetrievalDataset:
    digest = content_hash or sha256(content.encode()).hexdigest()
    positive = PositiveQuestion.model_validate(
        positive_payload(content_hash=digest, excerpt=excerpt)
    )
    negative = NegativeQuestion.model_validate(negative_payload())
    return RetrievalDataset(positives=(positive,), negatives=(negative,))


def _write_corpus(root: Path, content: str) -> Path:
    corpus_dir = root / "pages"
    corpus_dir.mkdir()
    (corpus_dir / "guide.md").write_text(content, encoding="utf-8")
    return corpus_dir
