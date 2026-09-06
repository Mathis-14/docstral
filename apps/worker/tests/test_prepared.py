import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest
from docstral_worker import IngestionError
from docstral_worker.corpus import SourceIdentity
from docstral_worker.ingest import DocsChunkMetadata
from docstral_worker.prepared import (
    PreparationManifest,
    PreparationStats,
    PreparedStore,
    SnapshotRef,
    Stage,
    StageRef,
)
from mistralai.search.toolkit.document import (
    Document,
    DocumentChunk,
    DocumentFileMetadata,
    SerializedDocument,
    compute_char_locator,
    compute_id,
    serialize_document,
)
from pydantic import ValidationError

SNAPSHOT = SnapshotRef(name="20260906T120000Z", manifest_sha256="a" * 64)
PROCESSING_HASH = "b" * 64
STATS = PreparationStats(total=2, failed=0, changed=2, duration_seconds=1.5)
REMOVED = SourceIdentity(
    source_id="https://docs.mistral.ai/removed",
    document_id=compute_id("https://docs.mistral.ai/removed"),
)


@pytest.fixture
def store(tmp_path: Path) -> PreparedStore:
    data_dir = tmp_path / "data"
    (data_dir / "snapshots" / SNAPSHOT.name).mkdir(parents=True)
    return PreparedStore(data_dir)


def _document(*, long: bool = False) -> Document:
    url = "https://docs.mistral.ai/long" if long else "https://docs.mistral.ai/short"
    text = "# Documentation\n\n" + ("Grounded café answers.\n" * (1500 if long else 1))
    chunks = []
    for start in range(0, len(text), 5000):
        end = min(start + 5000, len(text))
        chunks.append(
            DocumentChunk(
                source_id=url,
                locator=compute_char_locator(start, end),
                start_offset=start,
                end_offset=end,
                parent_ref=compute_id(url),
                content=text[start:end],
                metadata=DocsChunkMetadata(
                    title="Documentation",
                    content_hash=sha256(text.encode()).hexdigest(),
                    embedder_name="mistral-embed",
                ),
                embedding=[1.0, *([0.0] * 1023)],
            )
        )
    return Document(
        source_id=url,
        content=text,
        chunks=chunks,
        metadata=DocumentFileMetadata(filename="documentation.html", filepath=url),
    )


def _write(
    store: PreparedStore, *, stage: Stage = "embedded", document: Document | None = None
) -> StageRef:
    return store.write(
        snapshot=SNAPSHOT,
        stage=stage,
        documents=[_document() if document is None else document],
        processing_hash=PROCESSING_HASH,
        stats=PreparationStats(
            total=1, failed=0, changed=0 if stage == "extracted" else 1
        ),
        state_revision=0,
    )


def _stage_path(store: PreparedStore, ref: StageRef) -> Path:
    return store.data_dir / "snapshots" / ref.snapshot.name / "prepared" / ref.stage


def _rewrite(
    store: PreparedStore,
    ref: StageRef,
    *,
    updates: dict[str, object] | None = None,
    documents: bytes | None = None,
) -> StageRef:
    manifest, _ = store.read(ref)
    # Mutate the JSON boundary intentionally to model a corrupt on-disk artifact.
    payload: dict[str, object] = manifest.model_dump(mode="json")
    directory = _stage_path(store, ref)
    if documents is not None:
        (directory / "documents.jsonl").write_bytes(documents)
        payload["documents_sha256"] = sha256(documents).hexdigest()
    payload.update(updates or {})
    manifest_bytes = json.dumps(payload).encode()
    (directory / "manifest.json").write_bytes(manifest_bytes)
    return ref.model_copy(
        update={"manifest_sha256": sha256(manifest_bytes).hexdigest()}
    )


def test_prepared_roundtrip_preserves_documents_metadata_and_vectors(
    store: PreparedStore,
) -> None:
    documents = [_document(), _document(long=True)]
    ref = store.write(
        snapshot=SNAPSHOT,
        stage="embedded",
        documents=documents,
        processing_hash=PROCESSING_HASH,
        stats=STATS,
        state_revision=3,
        removed=(REMOVED,),
    )

    manifest, restored = PreparedStore(store.data_dir).read(ref)

    assert restored == documents
    assert manifest.stats == STATS
    assert manifest.state_revision == 3
    assert manifest.removed == (REMOVED,)
    assert manifest.processing_hash == PROCESSING_HASH
    assert manifest.document_count == 2
    assert len(restored[1].chunks) > 1
    for document in restored:
        assert isinstance(document.metadata, DocumentFileMetadata)
        for chunk in document.chunks:
            assert isinstance(chunk.metadata, DocsChunkMetadata)
            assert len(chunk.embedding or []) == 1024
    raw_documents = (_stage_path(store, ref) / "documents.jsonl").read_text()
    assert "docstral.chunk-metadata.v1" in raw_documents
    assert "docstral_worker.ingest:DocsChunkMetadata" not in raw_documents


def test_prepared_can_be_read_in_a_new_process(store: PreparedStore) -> None:
    ref = _write(store)
    script = """
import sys
from pathlib import Path
from docstral_worker.prepared import PreparedStore, StageRef
from docstral_worker.ingest import DocsChunkMetadata

manifest, documents = PreparedStore(Path(sys.argv[1])).read(
    StageRef.model_validate_json(sys.argv[2])
)
assert manifest.document_count == 1
assert isinstance(documents[0].chunks[0].metadata, DocsChunkMetadata)
assert documents[0].chunks[0].metadata.title == "Documentation"
assert len(documents[0].chunks[0].embedding) == 1024
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(store.data_dir), ref.model_dump_json()],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("stage", ["extracted", "compared", "split", "embedded"])
def test_empty_artifacts_are_valid(store: PreparedStore, stage: Stage) -> None:
    ref = store.write(
        snapshot=SNAPSHOT,
        stage=stage,
        documents=[],
        processing_hash=PROCESSING_HASH,
        stats=PreparationStats(total=1, failed=1),
        state_revision=None if stage == "extracted" else 0,
    )
    manifest, documents = store.read(ref)
    assert manifest.document_count == 0
    assert documents == []
    assert (_stage_path(store, ref) / "documents.jsonl").read_bytes() == b""


def test_existing_artifact_is_never_overwritten(store: PreparedStore) -> None:
    ref = _write(store)
    original = store.read(ref)
    with pytest.raises(IngestionError, match="already exists"):
        _write(store)
    assert store.read(ref) == original


@pytest.mark.parametrize("kind", ["directory", "file"])
def test_incomplete_destination_is_refused(store: PreparedStore, kind: str) -> None:
    prepared = store.data_dir / "snapshots" / SNAPSHOT.name / "prepared"
    prepared.mkdir()
    if kind == "directory":
        (prepared / "embedded").mkdir()
    else:
        (prepared / "embedded").write_text("incomplete")
    with pytest.raises(IngestionError, match="already exists"):
        _write(store)


@pytest.mark.parametrize("filename", ["manifest.json", "documents.jsonl"])
def test_corrupt_bytes_fail_their_hash_check(
    store: PreparedStore, filename: str
) -> None:
    ref = _write(store)
    (_stage_path(store, ref) / filename).write_bytes(b"corrupt")
    with pytest.raises(IngestionError, match="hash does not match"):
        store.read(ref)


@pytest.mark.parametrize(
    "updates",
    [
        {"format_version": 2},
        {"unexpected": "field"},
        {"processing_hash": "invalid"},
        {"document_count": -1},
        {"document_count": 2},
        {"state_revision": -1},
        {"stats": {"total": 2, "failed": 0, "changed": 1, "unchanged": 0}},
        {"removed": [REMOVED.model_dump(), REMOVED.model_dump()]},
    ],
)
def test_invalid_manifest_fails_even_with_matching_hash(
    store: PreparedStore, updates: dict[str, object]
) -> None:
    ref = _rewrite(store, _write(store), updates=updates)
    with pytest.raises(IngestionError, match="Cannot read prepared stage"):
        store.read(ref)


@pytest.mark.parametrize(
    "updates",
    [
        {"stage": "split"},
        {
            "snapshot": {
                "name": "20260905T120000Z",
                "manifest_sha256": SNAPSHOT.manifest_sha256,
            }
        },
        {
            "snapshot": {
                "name": SNAPSHOT.name,
                "manifest_sha256": "c" * 64,
            }
        },
    ],
)
def test_manifest_must_match_its_reference(
    store: PreparedStore, updates: dict[str, object]
) -> None:
    ref = _rewrite(store, _write(store), updates=updates)
    with pytest.raises(IngestionError, match="does not match stage reference"):
        store.read(ref)


@pytest.mark.parametrize("result", ["changed", "unchanged", "removed"])
def test_extraction_cannot_claim_comparison_results(
    store: PreparedStore, result: str
) -> None:
    stats = PreparationStats(total=1, failed=0)
    removed: tuple[SourceIdentity, ...] = ()
    if result == "removed":
        removed = (REMOVED,)
    else:
        stats = stats.model_copy(update={result: 1})

    with pytest.raises(IngestionError, match="Cannot write prepared stage"):
        store.write(
            snapshot=SNAPSHOT,
            stage="extracted",
            documents=[_document()],
            processing_hash=PROCESSING_HASH,
            stats=stats,
            removed=removed,
        )

    assert not (store.data_dir / "snapshots" / SNAPSHOT.name / "prepared").exists()


@pytest.mark.parametrize("stage", ["compared", "split", "embedded"])
def test_compared_artifacts_require_the_state_revision_they_compared(
    store: PreparedStore, stage: Stage
) -> None:
    ref = _rewrite(store, _write(store, stage=stage), updates={"state_revision": None})

    with pytest.raises(IngestionError, match="Cannot read prepared stage"):
        store.read(ref)


@pytest.mark.parametrize("operation", ["read", "write"])
@pytest.mark.parametrize(
    "updates",
    [
        {"content": "Not the article passage"},
        {"id": "another-chunk-id"},
        {"embedding": [1.0] * 1023},
        {"embedding": [float("inf")] * 1024},
    ],
)
def test_artifact_boundary_rejects_inconsistent_chunks_and_vectors(
    store: PreparedStore, operation: str, updates: dict[str, object]
) -> None:
    document = _document()
    invalid = document.model_copy(
        update={"chunks": [document.chunks[0].model_copy(update=updates)]}
    )
    if operation == "read":
        payload = serialize_document(invalid, store.registry).model_dump_json().encode()
        ref = _rewrite(store, _write(store), documents=payload + b"\n")
        with pytest.raises(IngestionError):
            store.read(ref)
    else:
        with pytest.raises(IngestionError):
            _write(store, document=invalid)
        assert not (store.data_dir / "snapshots" / SNAPSHOT.name / "prepared").exists()


@pytest.mark.parametrize("payload", [b"{", b"\n", b"{}\n", b"\xff"])
def test_malformed_document_payload_fails_with_matching_hash(
    store: PreparedStore, payload: bytes
) -> None:
    ref = _rewrite(store, _write(store), documents=payload)
    with pytest.raises(IngestionError, match="Cannot read prepared stage"):
        store.read(ref)


@pytest.mark.parametrize(
    "updates",
    [
        {"type_id": "unknown.document.v1"},
        {"type_id": "mistral.document-chunk.v1"},
        {"nested_type_ids": {"chunks.8.metadata": "docstral.chunk-metadata.v1"}},
        {"payload": {"source_id": "https://docs.mistral.ai/short"}},
    ],
)
def test_document_types_and_fields_are_validated(
    store: PreparedStore, updates: dict[str, object]
) -> None:
    ref = _write(store)
    serialized = SerializedDocument.model_validate_json(
        (_stage_path(store, ref) / "documents.jsonl").read_bytes()
    )
    malformed = serialized.model_copy(update=updates)
    ref = _rewrite(store, ref, documents=malformed.model_dump_json().encode() + b"\n")
    with pytest.raises(IngestionError, match="Cannot read prepared stage"):
        store.read(ref)


@pytest.mark.parametrize(
    "component",
    ["data", "snapshots", "snapshot", "prepared", "stage", "manifest", "documents"],
)
def test_reads_refuse_symlinks_at_every_artifact_level(
    store: PreparedStore, component: str
) -> None:
    ref = _write(store)
    stage = _stage_path(store, ref)
    paths = {
        "data": store.data_dir,
        "snapshots": store.data_dir / "snapshots",
        "snapshot": stage.parent.parent,
        "prepared": stage.parent,
        "stage": stage,
        "manifest": stage / "manifest.json",
        "documents": stage / "documents.jsonl",
    }
    path = paths[component]
    target = path.with_name(path.name + "-real")
    path.rename(target)
    path.symlink_to(target, target_is_directory=target.is_dir())
    with pytest.raises(IngestionError):
        store.read(ref)
    assert target.exists()


@pytest.mark.parametrize("component", ["data", "snapshots", "snapshot", "prepared"])
def test_writes_refuse_symbolic_link_directories(
    store: PreparedStore, component: str
) -> None:
    snapshot = store.data_dir / "snapshots" / SNAPSHOT.name
    prepared = snapshot / "prepared"
    prepared.mkdir()
    paths = {
        "data": store.data_dir,
        "snapshots": store.data_dir / "snapshots",
        "snapshot": snapshot,
        "prepared": prepared,
    }
    path = paths[component]
    target = path.with_name(path.name + "-real")
    path.rename(target)
    path.symlink_to(target, target_is_directory=True)
    with pytest.raises(IngestionError, match="symbolic links"):
        _write(store)
    assert not list(target.rglob("embedded"))


@pytest.mark.parametrize("operation", ["read", "write"])
def test_store_refuses_a_symbolic_link_in_data_directory_parents(
    store: PreparedStore, operation: str
) -> None:
    ref = _write(store)
    alias = store.data_dir.parent / "linked-parent"
    alias.symlink_to(store.data_dir.parent, target_is_directory=True)
    linked_store = PreparedStore(alias / store.data_dir.name)
    with pytest.raises(IngestionError, match="symbolic links"):
        if operation == "read":
            linked_store.read(ref)
        else:
            _write(linked_store, stage="extracted")
    assert not (_stage_path(store, ref).parent / "extracted").exists()


@pytest.mark.parametrize("filename", ["manifest.json", "documents.jsonl"])
@pytest.mark.parametrize("kind", ["directory", "fifo", "missing"])
def test_reads_require_existing_regular_files(
    store: PreparedStore, filename: str, kind: str
) -> None:
    ref = _write(store)
    path = _stage_path(store, ref) / filename
    path.unlink()
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    with pytest.raises(IngestionError):
        store.read(ref)


def test_write_validation_does_not_create_an_artifact(store: PreparedStore) -> None:
    with pytest.raises(IngestionError, match="Cannot write prepared stage"):
        store.write(
            snapshot=SNAPSHOT,
            stage="extracted",
            documents=[],
            processing_hash="invalid",
            stats=PreparationStats(total=0, failed=0),
        )
    assert not (store.data_dir / "snapshots" / SNAPSHOT.name / "prepared").exists()


@pytest.mark.parametrize("name", ["../outside", "20260906T120000Z-failed", "latest"])
def test_snapshot_reference_rejects_non_snapshot_names(name: str) -> None:
    with pytest.raises(ValidationError):
        SnapshotRef(name=name, manifest_sha256="a" * 64)


@pytest.mark.parametrize("duration", [-1.0, float("inf"), float("nan")])
def test_stats_reject_invalid_duration(duration: float) -> None:
    with pytest.raises(ValidationError):
        PreparationStats(total=0, failed=0, duration_seconds=duration)


def test_manifest_contract_is_frozen(store: PreparedStore) -> None:
    manifest, _ = store.read(_write(store))
    with pytest.raises(ValidationError, match="frozen"):
        manifest.document_count = 4  # type: ignore[misc]  # Exercise runtime validation.
    assert isinstance(manifest, PreparationManifest)
