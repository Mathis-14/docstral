from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from typing import override

import pytest
from docstral_worker import IngestionError
from docstral_worker.corpus import SourceIdentity
from docstral_worker.incremental import (
    DocumentEmbedder,
    IncrementalIngestion,
    RefreshResult,
)
from docstral_worker.index_state import IndexState, IndexStateStore
from docstral_worker.ingest import DocsChunkMetadata, PipelineConfig, build_pipeline
from docstral_worker.maintenance import WorkerState
from docstral_worker.prepared import PreparedStore, SnapshotRef, StageRef
from docstral_worker.snapshot import (
    SnapshotReadError,
    read_current_snapshot,
    write_snapshot,
)
from mistralai.search.toolkit.context import RetrievalContext
from mistralai.search.toolkit.document import Document, compute_id
from mistralai.search.toolkit.embedding import Embedder, EmbeddingResult
from mistralai.search.toolkit.embedding.errors import EmbedderException
from mistralai.search.toolkit.ingestion import File
from structlog.testing import capture_logs
from test_ingest import _FailingEmbedder, _FakeEmbedder, _html, _MemoryIndex
from test_snapshot import CRAWLED_AT, DOCS, result, sitemap, stored

_RETRIEVAL_CONTEXT = RetrievalContext()


class _Corpus:
    def __init__(self, *legacy_paths: str) -> None:
        self.documents: dict[str, Document | None] = {
            DOCS + path: None for path in legacy_paths
        }
        self.mutations: list[tuple[str, str]] = []
        self.inventory_calls = 0
        self.fail_inventory = False
        self.fail_index: str | None = None
        self.fail_delete: str | None = None

    async def list_sources(self) -> tuple[SourceIdentity, ...]:
        self.inventory_calls += 1
        if self.fail_inventory:
            raise RuntimeError("Vespa inventory unavailable")
        return tuple(
            SourceIdentity(source_id=url, document_id=compute_id(url))
            for url in sorted(self.documents)
        )

    async def index_document(self, document: Document) -> None:
        self.mutations.append(("index", document.source_id))
        # The external boundary reproduces the SDK's delete-before-insert behavior.
        self.documents.pop(document.source_id, None)
        if document.source_id == self.fail_index:
            raise RuntimeError("Vespa write interrupted")
        self.documents[document.source_id] = document

    async def delete_document(self, document_id: str) -> None:
        self.mutations.append(("delete", document_id))
        self.documents = {
            url: document
            for url, document in self.documents.items()
            if compute_id(url) != document_id
        }
        if document_id == self.fail_delete:
            raise RuntimeError("Vespa delete acknowledgement lost")


class _InvalidEmbedder(Embedder):
    def __init__(self, problem: str) -> None:
        super().__init__("invalid-embedding-service")
        self.problem = problem

    @override
    async def embed(
        self,
        texts: list[str],
        context: RetrievalContext = _RETRIEVAL_CONTEXT,
    ) -> EmbeddingResult:
        if self.problem == "missing":
            return EmbeddingResult(embeddings=[], total_tokens=0)
        vector = [1.0] if self.problem == "dimension" else [float("nan")] * 1024
        return EmbeddingResult(
            embeddings=[vector.copy() for _ in texts], total_tokens=0
        )


def _snapshot(root: Path, pages: dict[str, bytes], *, offset: int = 0) -> SnapshotRef:
    directory = write_snapshot(
        root / "snapshots",
        CRAWLED_AT + timedelta(seconds=offset),
        sitemap(),
        result(*(stored(path, body) for path, body in pages.items())),
    )
    return SnapshotRef(
        name=directory.name,
        manifest_sha256=sha256((directory / "manifest.json").read_bytes()).hexdigest(),
    )


async def _prepare(
    root: Path,
    snapshot: SnapshotRef,
    corpus: _Corpus,
    embedder: DocumentEmbedder,
    config: PipelineConfig | None = None,
) -> StageRef:
    def worker() -> IncrementalIngestion:
        return IncrementalIngestion(
            root, corpus=corpus, embedder=embedder, config=config
        )

    reference = await worker().extract(snapshot)
    for stage in (
        IncrementalIngestion.compare_hashes,
        IncrementalIngestion.split,
        IncrementalIngestion.embed,
    ):
        # A fresh worker and a serialized reference cross every activity boundary.
        reference = StageRef.model_validate_json(reference.model_dump_json())
        reference = await stage(worker(), reference)
    return reference


async def _run(
    root: Path,
    pages: dict[str, bytes],
    corpus: _Corpus,
    embedder: DocumentEmbedder,
    config: PipelineConfig | None = None,
    *,
    offset: int = 0,
) -> RefreshResult:
    snapshot = _snapshot(root, pages, offset=offset)
    reference = await _prepare(root, snapshot, corpus, embedder, config)
    return await IncrementalIngestion(
        root, corpus=corpus, embedder=embedder, config=config
    ).index_delta(StageRef.model_validate_json(reference.model_dump_json()))


def _state(root: Path) -> IndexState:
    state = IndexStateStore(root).read()
    assert state is not None
    return state


async def test_first_run_restores_every_stage_and_skips_identical_next_run(
    tmp_path: Path,
) -> None:
    pages = {"/a": _html("A", "word " * 2_000), "/b": _html("B", "Evidence")}
    corpus, embedder = _Corpus(), _FakeEmbedder()
    snapshot = _snapshot(tmp_path, pages)
    embedded = await _prepare(tmp_path, snapshot, corpus, embedder)

    assert corpus.mutations == []
    assert _state(tmp_path).pages == {}
    manifest, documents = PreparedStore(tmp_path).read(embedded)
    assert manifest.stage == "embedded"
    assert len(documents[0].chunks) > 1
    assert all(
        isinstance(chunk.metadata, DocsChunkMetadata)
        and len(chunk.embedding or []) == 1024
        for document in documents
        for chunk in document.chunks
    )
    first = await IncrementalIngestion(
        tmp_path, corpus=corpus, embedder=embedder
    ).index_delta(embedded)
    assert (first.indexed, first.changed, first.unchanged, first.failed) == (2, 2, 0, 0)
    assert first.status == "complete"
    state = _state(tmp_path)
    assert all(page.index_hash and not page.pending for page in state.pages.values())
    inputs, mutations = list(embedder.inputs), list(corpus.mutations)

    second = await _run(tmp_path, pages, corpus, embedder, offset=1)

    assert (second.indexed, second.changed, second.unchanged, second.deleted) == (
        0,
        0,
        2,
        0,
    )
    assert second.status == "complete"
    assert embedder.inputs == inputs
    assert corpus.mutations == mutations
    assert corpus.inventory_calls == 1
    assert _state(tmp_path) == state


@pytest.mark.parametrize("change", ["content", "title", "chrome"])
async def test_only_changed_article_content_or_title_is_reindexed(
    tmp_path: Path, change: str
) -> None:
    original = _html("A", "Original evidence")
    pages = {"/a": original, "/b": _html("B", "Stable evidence")}
    corpus, embedder = _Corpus(), _FakeEmbedder()
    await _run(tmp_path, pages, corpus, embedder)
    previous = corpus.documents[DOCS + "/a"]
    assert previous is not None
    embedder.inputs.clear()
    corpus.mutations.clear()
    if change == "content":
        pages["/a"] = _html("A", "Updated evidence")
    elif change == "title":
        pages["/a"] = original.replace(b"<title>A |", b"<title>Renamed |")
    else:
        pages["/a"] = original.replace(
            b"</head>", b"<script>newBuild()</script></head>"
        )

    outcome = await _run(tmp_path, pages, corpus, embedder, offset=1)

    changed = int(change != "chrome")
    assert (outcome.indexed, outcome.changed, outcome.unchanged) == (
        changed,
        changed,
        2 - changed,
    )
    assert corpus.mutations == ([("index", DOCS + "/a")] if changed else [])
    assert bool(embedder.inputs) == bool(changed)
    current = corpus.documents[DOCS + "/a"]
    assert current is not None
    if change == "title":
        assert current.content == previous.content
        assert current.chunks[0].metadata["title"] == "Renamed"


@pytest.mark.parametrize(
    ("config", "model"),
    [
        (PipelineConfig(version="2.0.0"), None),
        (PipelineConfig(chunk_size=400, chunk_max_size=400), None),
        (PipelineConfig(), "new-external-model"),
    ],
    ids=["pipeline-version", "splitter-settings", "embedding-model"],
)
async def test_processing_changes_invalidate_index_hash(
    tmp_path: Path, config: PipelineConfig, model: str | None
) -> None:
    pages = {"/a": _html("A", "Evidence")}
    corpus, embedder = _Corpus(), _FakeEmbedder()
    await _run(tmp_path, pages, corpus, embedder)
    before = _state(tmp_path).pages[DOCS + "/a"].index_hash
    replacement = _FakeEmbedder()
    if model is not None:
        replacement.model_name = model

    outcome = await _run(tmp_path, pages, corpus, replacement, config, offset=1)

    assert outcome.indexed == outcome.changed == 1
    assert replacement.inputs
    assert _state(tmp_path).pages[DOCS + "/a"].index_hash != before


async def test_initial_inventory_is_reconciled_without_trusting_legacy_marker(
    tmp_path: Path,
) -> None:
    corpus, embedder = _Corpus("/a", "/gone"), _FakeEmbedder()
    (tmp_path / ".published-snapshot").write_text("unknown-old-snapshot\n")

    outcome = await _run(
        tmp_path,
        {"/a": _html("A", "Evidence"), "/new": _html("New", "New")},
        corpus,
        embedder,
    )

    assert (outcome.indexed, outcome.deleted, outcome.unchanged) == (2, 1, 0)
    assert set(corpus.documents) == {DOCS + "/a", DOCS + "/new"}
    assert ("delete", compute_id(DOCS + "/gone")) in corpus.mutations
    assert set(_state(tmp_path).pages) == set(corpus.documents)


@pytest.mark.parametrize("failed_count", [1, 2, 3])
async def test_extraction_failures_keep_old_pages_without_a_percentage_threshold(
    tmp_path: Path, failed_count: int
) -> None:
    paths = ["/a", "/b", "/c"]
    corpus, embedder = _Corpus(), _FakeEmbedder()
    await _run(tmp_path, {path: _html(path, "Old") for path in paths}, corpus, embedder)
    previous = dict(corpus.documents)
    previous_state = _state(tmp_path)
    corpus.mutations.clear()
    pages = {
        path: b"invalid HTML" if index < failed_count else _html(path, "New")
        for index, path in enumerate(paths)
    }

    outcome = await _run(tmp_path, pages, corpus, embedder, offset=1)

    assert outcome.status == "partial"
    assert (outcome.failed, outcome.indexed, outcome.deleted) == (
        failed_count,
        3 - failed_count,
        0,
    )
    for path in paths[:failed_count]:
        assert corpus.documents[DOCS + path] == previous[DOCS + path]
        assert _state(tmp_path).pages[DOCS + path] == previous_state.pages[DOCS + path]
    assert all(operation == "index" for operation, _ in corpus.mutations)


async def test_failed_new_article_is_retried_and_does_not_delete_existing_failure(
    tmp_path: Path,
) -> None:
    corpus, embedder = _Corpus("/old-failed", "/removed"), _FakeEmbedder()
    first = await _run(
        tmp_path, {"/old-failed": b"invalid", "/new": b"invalid"}, corpus, embedder
    )
    assert (first.failed, first.indexed, first.deleted) == (2, 0, 1)
    assert set(corpus.documents) == {DOCS + "/old-failed"}
    assert _state(tmp_path).pages[DOCS + "/old-failed"].index_hash is None
    assert DOCS + "/new" not in _state(tmp_path).pages

    second = await _run(
        tmp_path,
        {"/old-failed": b"invalid", "/new": _html("New", "Fixed")},
        corpus,
        embedder,
        offset=1,
    )

    assert (second.failed, second.indexed, second.deleted) == (1, 1, 0)
    assert set(corpus.documents) == {DOCS + "/old-failed", DOCS + "/new"}


async def test_delete_only_run_has_no_embeddings_or_index_writes(
    tmp_path: Path,
) -> None:
    pages = {"/a": _html("A", "Stable"), "/b": _html("B", "Removed")}
    corpus, embedder = _Corpus(), _FakeEmbedder()
    await _run(tmp_path, pages, corpus, embedder)
    corpus.mutations.clear()
    embedder.inputs.clear()

    outcome = await _run(tmp_path, {"/a": pages["/a"]}, corpus, embedder, offset=1)

    assert (outcome.indexed, outcome.changed, outcome.unchanged, outcome.deleted) == (
        0,
        0,
        1,
        1,
    )
    assert embedder.inputs == []
    assert corpus.mutations == [("delete", compute_id(DOCS + "/b"))]


async def test_embedding_failure_preserves_index_and_success_registry(
    tmp_path: Path,
) -> None:
    pages = {"/a": _html("A", "Old")}
    corpus = _Corpus()
    await _run(tmp_path, pages, corpus, _FakeEmbedder())
    original, state = dict(corpus.documents), _state(tmp_path)
    corpus.mutations.clear()
    failing = _FailingEmbedder()

    with pytest.raises(EmbedderException, match="embedding unavailable"):
        await _prepare(tmp_path, _snapshot(tmp_path, pages, offset=1), corpus, failing)

    assert failing.calls == 1
    assert corpus.mutations == []
    assert corpus.documents == original
    assert _state(tmp_path) == state


@pytest.mark.parametrize("problem", ["missing", "dimension", "nan"])
async def test_invalid_embedding_output_is_rejected_before_indexing(
    tmp_path: Path, problem: str
) -> None:
    corpus = _Corpus("/old")

    with pytest.raises(IngestionError, match=r"Embedding output|Every prepared chunk"):
        await _prepare(
            tmp_path,
            _snapshot(tmp_path, {"/a": _html("A", "Evidence")}),
            corpus,
            _InvalidEmbedder(problem),
        )

    assert corpus.mutations == []
    assert set(corpus.documents) == {DOCS + "/old"}


async def test_failed_inventory_cannot_initialize_registry_or_mutate_index(
    tmp_path: Path,
) -> None:
    corpus, embedder = _Corpus("/old"), _FakeEmbedder()
    corpus.fail_inventory = True
    worker = IncrementalIngestion(tmp_path, corpus=corpus, embedder=embedder)
    extracted = await worker.extract(
        _snapshot(tmp_path, {"/a": _html("A", "Evidence")})
    )

    with pytest.raises(RuntimeError, match="inventory unavailable"):
        await worker.compare_hashes(extracted)

    assert IndexStateStore(tmp_path).read() is None
    assert corpus.mutations == []


@pytest.mark.parametrize("recovery", ["old_content", "extraction_failure", "deleted"])
async def test_pending_recovery_uses_fresh_snapshot_even_if_old_hash_returns(
    tmp_path: Path, recovery: str
) -> None:
    stable = _html("Stable", "Always available")
    old = _html("A", "Old content")
    corpus, embedder = _Corpus(), _FakeEmbedder()
    await _run(tmp_path, {"/a": old, "/stable": stable}, corpus, embedder)
    old_hash = _state(tmp_path).pages[DOCS + "/a"].index_hash
    corpus.fail_index = DOCS + "/a"
    with pytest.raises(RuntimeError, match="write interrupted"):
        await _run(
            tmp_path,
            {"/a": _html("A", "Attempted update"), "/stable": stable},
            corpus,
            embedder,
            offset=1,
        )
    pending = _state(tmp_path).pages[DOCS + "/a"]
    assert pending.pending and pending.index_hash == old_hash
    assert DOCS + "/a" not in corpus.documents
    assert DOCS + "/stable" in corpus.documents
    corpus.fail_index = None
    corpus.mutations.clear()
    pages = {"/stable": stable}
    if recovery != "deleted":
        pages["/a"] = old if recovery == "old_content" else b"invalid"

    outcome = await _run(tmp_path, pages, corpus, embedder, offset=2)

    if recovery == "old_content":
        assert outcome.indexed == 1 and outcome.unchanged == 1
        assert corpus.mutations == [("index", DOCS + "/a")]
        repaired = _state(tmp_path).pages[DOCS + "/a"]
        assert not repaired.pending and repaired.index_hash == old_hash
    elif recovery == "extraction_failure":
        assert outcome.failed == 1 and outcome.status == "partial"
        assert corpus.mutations == []
        assert _state(tmp_path).pages[DOCS + "/a"].pending
    else:
        assert outcome.deleted == 1 and outcome.indexed == 0
        assert corpus.mutations == [("delete", compute_id(DOCS + "/a"))]
        assert DOCS + "/a" not in _state(tmp_path).pages


async def test_retry_skips_pages_written_before_a_later_page_failed(
    tmp_path: Path,
) -> None:
    pages = {"/a": _html("A", "Evidence"), "/b": _html("B", "Evidence")}
    corpus, embedder = _Corpus(), _FakeEmbedder()
    corpus.fail_index = DOCS + "/b"
    with pytest.raises(RuntimeError, match="write interrupted"):
        await _run(tmp_path, pages, corpus, embedder)
    assert not _state(tmp_path).pages[DOCS + "/a"].pending
    assert _state(tmp_path).pages[DOCS + "/b"].pending
    corpus.fail_index = None
    corpus.mutations.clear()

    outcome = await _run(tmp_path, pages, corpus, embedder, offset=1)

    assert (outcome.indexed, outcome.unchanged) == (1, 1)
    assert corpus.mutations == [("index", DOCS + "/b")]


async def test_interrupted_delete_is_retried_when_article_is_already_absent(
    tmp_path: Path,
) -> None:
    stable = _html("Stable", "Evidence")
    corpus, embedder = _Corpus("/gone"), _FakeEmbedder()
    corpus.fail_delete = compute_id(DOCS + "/gone")
    with pytest.raises(RuntimeError, match="acknowledgement lost"):
        await _run(tmp_path, {"/stable": stable}, corpus, embedder)
    assert _state(tmp_path).pages[DOCS + "/gone"].pending
    assert DOCS + "/gone" not in corpus.documents
    corpus.fail_delete = None

    outcome = await _run(tmp_path, {"/stable": stable}, corpus, embedder, offset=1)

    assert (outcome.indexed, outcome.deleted, outcome.unchanged) == (0, 1, 1)
    assert DOCS + "/gone" not in _state(tmp_path).pages


@pytest.mark.parametrize("blocked", ["lock", "maintenance", "legacy"])
async def test_indexing_respects_lock_maintenance_and_legacy_pending(
    tmp_path: Path, blocked: str
) -> None:
    corpus, embedder = _Corpus(), _FakeEmbedder()
    reference = await _prepare(
        tmp_path, _snapshot(tmp_path, {"/a": _html("A", "Evidence")}), corpus, embedder
    )
    worker = IncrementalIngestion(tmp_path, corpus=corpus, embedder=embedder)
    coordination = WorkerState(tmp_path)
    if blocked == "lock":
        async with coordination.lock():
            with pytest.raises(IngestionError, match="holds the worker lock"):
                await worker.index_delta(reference)
    else:
        if blocked == "maintenance":
            await coordination.set_maintenance(True, timeout=0)
        else:
            (tmp_path / ".publication-pending").write_text("legacy publication\n")
        with pytest.raises(IngestionError, match=r"maintenance|Legacy publication"):
            await worker.index_delta(reference)

    assert corpus.mutations == []


@pytest.mark.parametrize(
    "change", ["current", "snapshot_manifest", "state", "settings"]
)
async def test_obsolete_preparation_is_rejected_before_index_mutation(
    tmp_path: Path, change: str
) -> None:
    pages = {"/a": _html("A", "Evidence")}
    corpus, embedder = _Corpus(), _FakeEmbedder()
    snapshot = _snapshot(tmp_path, pages)
    reference = await _prepare(tmp_path, snapshot, corpus, embedder)
    config = None
    if change == "current":
        _snapshot(tmp_path, pages, offset=1)
    elif change == "snapshot_manifest":
        path = tmp_path / "snapshots" / snapshot.name / "manifest.json"
        path.write_bytes(path.read_bytes() + b"\n")
    elif change == "state":
        state = _state(tmp_path)
        IndexStateStore(tmp_path).write(
            IndexState(revision=state.revision + 1, pages=state.pages)
        )
    else:
        config = PipelineConfig(version="changed-during-run")

    with pytest.raises(IngestionError, match=r"obsolete|incompatible"):
        await IncrementalIngestion(
            tmp_path, corpus=corpus, embedder=embedder, config=config
        ).index_delta(reference)

    assert corpus.mutations == []


@pytest.mark.parametrize("filename", ["manifest.json", "documents.jsonl"])
async def test_corrupt_artifact_is_rejected_before_index_mutation(
    tmp_path: Path, filename: str
) -> None:
    corpus, embedder = _Corpus(), _FakeEmbedder()
    reference = await _prepare(
        tmp_path, _snapshot(tmp_path, {"/a": _html("A", "Evidence")}), corpus, embedder
    )
    path = (
        tmp_path
        / "snapshots"
        / reference.snapshot.name
        / "prepared"
        / "embedded"
        / filename
    )
    path.write_bytes(path.read_bytes() + b"\n")

    with pytest.raises(IngestionError, match="hash does not match"):
        await IncrementalIngestion(
            tmp_path, corpus=corpus, embedder=embedder
        ).index_delta(reference)

    assert corpus.mutations == []


@pytest.mark.parametrize(
    "problem", ["corrupt_raw", "missing_raw", "raw_symlink", "current_symlink"]
)
async def test_invalid_snapshot_is_rejected_before_preparation(
    tmp_path: Path, problem: str
) -> None:
    corpus, embedder = _Corpus(), _FakeEmbedder()
    snapshot = _snapshot(tmp_path, {"/a": _html("A", "Evidence")})
    raw = tmp_path / "snapshots" / snapshot.name / "raw" / "a.html"
    if problem == "corrupt_raw":
        raw.write_bytes(b"changed after crawl")
    elif problem == "missing_raw":
        raw.unlink()
    else:
        path = (
            tmp_path / "snapshots" / "current" if problem == "current_symlink" else raw
        )
        target = path.with_suffix(".backup")
        path.rename(target)
        path.symlink_to(target)

    with pytest.raises(SnapshotReadError):
        await IncrementalIngestion(tmp_path, corpus=corpus, embedder=embedder).extract(
            snapshot
        )

    assert corpus.mutations == []
    assert embedder.inputs == []


async def test_stage_order_and_existing_output_are_explicit_errors(
    tmp_path: Path,
) -> None:
    corpus, embedder = _Corpus(), _FakeEmbedder()
    worker = IncrementalIngestion(tmp_path, corpus=corpus, embedder=embedder)
    snapshot = _snapshot(tmp_path, {"/a": _html("A", "Evidence")})
    extracted = await worker.extract(snapshot)

    with pytest.raises(IngestionError, match="Expected embedded artifact"):
        await worker.index_delta(extracted)
    with pytest.raises(IngestionError, match="already exists"):
        await worker.extract(snapshot)

    assert corpus.mutations == []


@pytest.mark.parametrize("body", ["Short evidence", "word " * 2_000])
async def test_staged_document_exactly_matches_existing_pipeline(
    tmp_path: Path, body: str
) -> None:
    html = _html("Guide", body)
    legacy_index = _MemoryIndex()
    legacy = await build_pipeline(
        index=legacy_index, embedder=_FakeEmbedder()
    ).run_file(
        File(
            path=DOCS + "/guide", name="guide.html", raw=html, source_id=DOCS + "/guide"
        )
    )
    corpus = _Corpus()

    outcome = await _run(tmp_path, {"/guide": html}, corpus, _FakeEmbedder())

    actual = corpus.documents[DOCS + "/guide"]
    assert actual is not None
    assert outcome.indexed == 1
    assert actual.model_dump(mode="json") == legacy.model_dump(mode="json")


async def test_partial_run_reports_extraction_failure_rate_and_article_counts(
    tmp_path: Path,
) -> None:
    corpus = _Corpus()
    pages = {"/a": b"invalid", "/b": b"invalid", "/c": _html("C", "Evidence")}

    with capture_logs() as logs:
        outcome = await _run(tmp_path, pages, corpus, _FakeEmbedder())

    extracted = next(
        log
        for log in logs
        if log.get("event") == "refresh_stage_finished"
        and log.get("stage") == "extracted"
    )
    assert extracted["extraction_failure_rate"] == pytest.approx(2 / 3)
    assert extracted["total"] == 3 and extracted["failed"] == 2
    finished = next(log for log in logs if log.get("event") == "refresh_finished")
    assert finished["indexed"] == outcome.indexed == 1
    assert finished["failed"] == outcome.failed == 2
    assert finished["status"] == outcome.status == "partial"


async def test_retention_removes_prepared_artifacts_with_their_old_snapshot(
    tmp_path: Path,
) -> None:
    corpus, embedder = _Corpus(), _FakeEmbedder()
    pages = {"/a": _html("A", "Evidence")}
    snapshots: list[Path] = []
    for offset in range(3):
        await _run(tmp_path, pages, corpus, embedder, offset=offset)
        _, snapshot = read_current_snapshot(tmp_path / "snapshots")
        snapshots.append(snapshot.directory)
        assert (snapshot.directory / "prepared" / "embedded").is_dir()

    assert not snapshots[0].exists()
    for directory in snapshots[1:]:
        assert (directory / "prepared" / "embedded").is_dir()
    assert _state(tmp_path).pages[DOCS + "/a"].index_hash is not None
    assert corpus.mutations == [("index", DOCS + "/a")]


async def test_successful_vespa_write_with_failed_local_commit_is_reconciled(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    unavailable = tmp_path / "temporarily-unavailable"

    class RelocatingCorpus(_Corpus):
        lose_local_path: bool = False

        async def index_document(self, document: Document) -> None:
            await super().index_document(document)
            if self.lose_local_path:
                data_dir.rename(unavailable)

    corpus, embedder = RelocatingCorpus(), _FakeEmbedder()
    await _run(data_dir, {"/a": _html("A", "Old content")}, corpus, embedder)
    old_hash = _state(data_dir).pages[DOCS + "/a"].index_hash
    pages = {"/a": _html("A", "New content")}
    corpus.lose_local_path = True
    try:
        with pytest.raises(IngestionError, match="Cannot write index state"):
            await _run(data_dir, pages, corpus, embedder, offset=1)
    finally:
        corpus.lose_local_path = False
        if unavailable.exists():
            unavailable.rename(data_dir)

    accepted = corpus.documents[DOCS + "/a"]
    assert accepted is not None and "New content" in accepted.content
    pending = _state(data_dir).pages[DOCS + "/a"]
    assert pending.pending and pending.index_hash == old_hash
    corpus.mutations.clear()

    outcome = await _run(data_dir, pages, corpus, embedder, offset=2)

    assert (outcome.indexed, outcome.changed, outcome.unchanged) == (1, 1, 0)
    assert corpus.mutations == [("index", DOCS + "/a")]
    assert corpus.documents[DOCS + "/a"] == accepted
    repaired = _state(data_dir).pages[DOCS + "/a"]
    assert not repaired.pending and repaired.index_hash != old_hash
