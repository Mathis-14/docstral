import json
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from evals.qa_dataset import (
    DatasetFreeze,
    QACase,
    corpus_fingerprint,
    load_qa_dataset,
    validate_observed_chunk,
)
from evals.retrieval_dataset import CorpusValidationError
from evals.tests.helpers import make_chunk, negative_payload, positive_payload


def frozen_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    corpus = tmp_path / "snapshot" / "pages"
    corpus.mkdir(parents=True)
    (corpus / "guide.md").write_text("# Guide\n\nEvidence\n")
    content_hash = sha256((corpus / "guide.md").read_bytes()).hexdigest()
    cases = [
        {
            "question": positive_payload(content_hash=content_hash),
            "reference": "Expected answer",
        },
        {"question": negative_payload(), "reference": None},
    ]
    dataset = tmp_path / "qa.jsonl"
    dataset.write_text("".join(json.dumps(row) + "\n" for row in cases))
    freeze = DatasetFreeze(
        dataset_sha256=sha256(dataset.read_bytes()).hexdigest(),
        snapshot="snapshot",
        corpus_sha256=corpus_fingerprint(corpus),
        positive_count=1,
        negative_count=1,
        frozen_at="2026-09-05T00:00:00Z",
        review_url="https://example.test/review",
        ancestors={},
        changes=(),
        provenance="test fixture",
    )
    freeze_path = tmp_path / "local" / "approved.freeze.json"
    freeze_path.parent.mkdir()
    freeze_path.write_text(freeze.model_dump_json())
    return dataset, corpus, freeze_path


def test_frozen_dataset_validates_gold_and_refuses_dataset_or_corpus_drift(
    tmp_path: Path,
) -> None:
    path, corpus, freeze_path = frozen_fixture(tmp_path)
    assert not path.with_suffix(".freeze.json").exists()
    loaded = load_qa_dataset(path, corpus, freeze_path=freeze_path)
    assert len(loaded.cases) == 2 and loaded.cases[0].reference == "Expected answer"
    original = path.read_bytes()
    path.write_bytes(original + b"\n")
    with pytest.raises(ValueError, match="dataset differs"):
        load_qa_dataset(path, corpus, freeze_path=freeze_path)
    path.write_bytes(original)
    (corpus / "guide.md").write_text("Changed")
    with pytest.raises(ValueError, match="corpus differs"):
        load_qa_dataset(path, corpus, freeze_path=freeze_path)


def test_missing_explicit_freeze_does_not_fall_back_to_adjacent_file(
    tmp_path: Path,
) -> None:
    path, corpus, freeze_path = frozen_fixture(tmp_path)
    path.with_suffix(".freeze.json").write_bytes(freeze_path.read_bytes())
    with pytest.raises(FileNotFoundError):
        load_qa_dataset(path, corpus, freeze_path=tmp_path / "missing.freeze.json")


@pytest.mark.parametrize(
    "question,reference",
    [(positive_payload(), None), (negative_payload(), "Invented answer")],
)
def test_reference_required_only_for_positive(
    question: dict[str, object], reference: str | None
) -> None:
    with pytest.raises(ValidationError, match="positive questions"):
        QACase.model_validate({"question": question, "reference": reference})


@pytest.mark.parametrize("defect", ["duplicate", "bad_gold", "bad_reference_evidence"])
def test_invalid_annotations_fail_even_with_matching_file_hash(
    tmp_path: Path, defect: str
) -> None:
    path, corpus, freeze_path = frozen_fixture(tmp_path)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if defect == "duplicate":
        rows.insert(1, rows[0])
    elif defect == "bad_gold":
        rows[0]["question"]["evidence_groups"][0]["alternatives"][0]["excerpt"] = (
            "Absent"
        )
    else:
        evidence = dict(rows[0]["question"]["evidence_groups"][0]["alternatives"][0])
        evidence["excerpt"] = "Absent"
        rows[0]["additional_qa_evidence"] = [evidence]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    freeze = DatasetFreeze.model_validate_json(freeze_path.read_bytes())
    freeze_path.write_text(
        freeze.model_copy(
            update={"dataset_sha256": sha256(path.read_bytes()).hexdigest()}
        ).model_dump_json()
    )
    with pytest.raises((ValueError, CorpusValidationError)):
        load_qa_dataset(path, corpus, freeze_path=freeze_path)


def test_stale_or_foreign_indexed_chunk_is_rejected(tmp_path: Path) -> None:
    _, corpus, _ = frozen_fixture(tmp_path)
    digest = sha256((corpus / "guide.md").read_bytes()).hexdigest()
    chunk = make_chunk(
        rank=1,
        source_id="https://docs.mistral.ai/guide",
        content="Evidence",
        content_hash=digest,
    )
    validate_observed_chunk(chunk, corpus)
    with pytest.raises(ValueError, match="Indexed hash"):
        validate_observed_chunk(
            chunk.model_copy(update={"content_hash": "a" * 64}), corpus
        )
    with pytest.raises(ValueError, match="Indexed content"):
        validate_observed_chunk(chunk.model_copy(update={"content": "Absent"}), corpus)


def test_committed_qa_set_preserves_v1_except_approved_corrections() -> None:
    root = Path("evals/datasets")
    cases = [
        QACase.model_validate_json(line)
        for line in (root / "qa_dev_v1.jsonl").read_bytes().splitlines()
    ]
    originals = [
        json.loads(line)
        for name in ("retrieval_dev_v1.jsonl", "retrieval_negatives_v1.jsonl")
        for line in (root / name).read_text().splitlines()
    ]
    assert len(cases) == 72
    assert sum(c.reference is not None for c in cases) == 62
    for case, original in zip(cases, originals, strict=True):
        current = case.question.model_dump(mode="json", exclude_unset=True)
        if case.question.id in {"candidate-005", "candidate-006"}:
            assert "Mistral Small" in current["query"]
            current["query"] = original["query"]
        if case.question.id == "negative-candidate-001":
            assert "marketplace" in current["reason"]
            current["reason"] = original["reason"]
        assert current == original
