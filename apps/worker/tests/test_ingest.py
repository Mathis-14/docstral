import subprocess
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import override

import pytest
from docstral_worker.cli import main
from docstral_worker.crawl import (
    CrawlCounts,
    CrawlEntry,
    CrawlResult,
    DiscoveryVia,
    PageDecision,
)
from docstral_worker.extract import extract_page
from docstral_worker.ingest import (
    DocsChunkMetadata,
    PipelineConfig,
    build_pipeline,
    build_splitter,
    extract_documents,
    ingest_snapshot,
    validate_documents,
)
from docstral_worker.sitemap import SitemapSnapshot
from docstral_worker.snapshot import (
    CurrentSnapshot,
    SnapshotReadError,
    current_snapshot,
    write_snapshot,
)
from mistralai.search.toolkit.context import IngestContext, RetrievalContext
from mistralai.search.toolkit.document import Document, compute_id
from mistralai.search.toolkit.embedding import Embedder, EmbeddingResult
from mistralai.search.toolkit.embedding.errors import EmbedderException
from mistralai.search.toolkit.ingestion import File
from mistralai.search.toolkit.ingestion.text_splitters import MarkdownTokenTextSplitter
from mistralai.search.toolkit.search import (
    SearchResult,
    VectorSearchQuery,
    VectorStoreIndex,
)
from structlog.testing import capture_logs

DOCS = "https://docs.mistral.ai"
CRAWLED_AT = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
_INGEST_CONTEXT = IngestContext()
_RETRIEVAL_CONTEXT = RetrievalContext()


class _FakeEmbedder(Embedder):
    def __init__(self) -> None:
        super().__init__("fake-1024")
        self.inputs: list[str] = []

    @override
    async def embed(
        self,
        texts: list[str],
        context: RetrievalContext = _RETRIEVAL_CONTEXT,
    ) -> EmbeddingResult:
        self.inputs.extend(texts)
        vector = [1.0, *([0.0] * 1023)]
        return EmbeddingResult(
            embeddings=[vector.copy() for _ in texts], total_tokens=0
        )


class _FailingEmbedder(Embedder):
    def __init__(self) -> None:
        super().__init__("failing")
        self.calls = 0

    @override
    async def embed(
        self,
        texts: list[str],
        context: RetrievalContext = _RETRIEVAL_CONTEXT,
    ) -> EmbeddingResult:
        self.calls += 1
        raise EmbedderException("embedding unavailable")


class _MemoryIndex(VectorStoreIndex):
    def __init__(self) -> None:
        self.documents: list[Document] = []

    @override
    async def index_document(
        self, document: Document, context: IngestContext = _INGEST_CONTEXT
    ) -> None:
        self.documents.append(document)

    @override
    async def delete_document(
        self, doc_id: str, context: IngestContext = _INGEST_CONTEXT
    ) -> None:
        self.documents = [
            document for document in self.documents if document.id != doc_id
        ]

    @override
    async def search(
        self,
        query: VectorSearchQuery,
        context: RetrievalContext = _RETRIEVAL_CONTEXT,
    ) -> list[SearchResult]:
        return []


async def test_pipeline_extracts_splits_embeds_and_indexes() -> None:
    url = f"{DOCS}/guide"
    html = _html("Guide", "Kept")
    expected = extract_page(url, html)
    embedder = _FakeEmbedder()
    index = _MemoryIndex()
    pipeline = build_pipeline(index=index, embedder=embedder)

    document = await pipeline.run_file(
        File(path=url, name="guide.html", raw=html, source_id=url)
    )

    assert isinstance(pipeline.text_splitter, MarkdownTokenTextSplitter)
    assert pipeline.text_splitter.config.chunk_size == 800
    assert pipeline.text_splitter.config.chunk_max_size == 800
    assert pipeline.text_splitter.config.chunk_overlap == 0
    assert document.content == expected.markdown
    assert index.documents == [document]
    assert embedder.inputs == [chunk.content for chunk in document.chunks]
    chunk = document.chunks[0]
    assert chunk.source_id == url
    assert chunk.parent_ref == compute_id(url)
    assert chunk.content == expected.markdown
    assert chunk.start_offset == 0
    assert chunk.end_offset == expected.chars
    assert len(chunk.embedding or []) == 1024
    assert isinstance(chunk.metadata, DocsChunkMetadata)
    assert chunk.metadata.title == "Guide"
    assert chunk.metadata.content_hash == expected.content_hash


async def test_pipeline_lets_the_toolkit_chunk_the_whole_page() -> None:
    url = f"{DOCS}/repeated"
    html = (
        b"<html><head><title>Repeated | Mistral Docs</title></head>"
        b'<body><main><article class="prose">'
        b'<h1 id="top">Repeated</h1><p>Intro</p>'
        b'<h2 id="first">Same</h2><p>First</p>'
        b'<h2 id="second">Same</h2><p>Second</p>'
        b"</article></main></body></html>"
    )
    pipeline = build_pipeline(index=_MemoryIndex(), embedder=_FakeEmbedder())

    document = await pipeline.run_file(
        File(path=url, name="repeated.html", raw=html, source_id=url)
    )

    assert len(document.chunks) == 1
    chunk = document.chunks[0]
    assert chunk.content.count("## Same") == 2
    assert chunk.start_offset == 0
    assert chunk.end_offset == len(document.content)


async def test_pipeline_splits_a_long_page_and_preserves_metadata() -> None:
    url = f"{DOCS}/long"
    html = _html("Long", "word " * 2_000)
    expected = extract_page(url, html)
    embedder = _FakeEmbedder()
    pipeline = build_pipeline(index=_MemoryIndex(), embedder=embedder)

    document = await pipeline.run_file(
        File(path=url, name="long.html", raw=html, source_id=url)
    )

    assert len(document.chunks) > 1
    assert all(
        chunk.content == document.content[chunk.start_offset : chunk.end_offset]
        for chunk in document.chunks
    )
    assert all(chunk.metadata["title"] == "Long" for chunk in document.chunks)
    assert all(
        chunk.metadata["content_hash"] == expected.content_hash
        for chunk in document.chunks
    )
    assert embedder.inputs == [chunk.content for chunk in document.chunks]


async def test_ingest_snapshot_records_page_failure_and_continues(
    tmp_path: Path,
) -> None:
    snapshot = _write_current_snapshot(
        tmp_path,
        ("/good", _html("Good", "Kept")),
        ("/invalid", b"\xff"),
    )
    index = _MemoryIndex()
    pipeline = build_pipeline(index=index, embedder=_FakeEmbedder())

    with capture_logs() as logs:
        result = await ingest_snapshot(snapshot, pipeline)

    assert result.indexed == 1
    assert result.failed == 1
    assert [document.source_id for document in index.documents] == [f"{DOCS}/good"]
    assert any(
        log.get("event") == "ingestion_page"
        and log.get("decision") == "failed"
        and log.get("url") == f"{DOCS}/invalid"
        for log in logs
    )


async def test_staged_extraction_preserves_pipeline_documents(tmp_path: Path) -> None:
    html = _html("Long", "word " * 2_000)
    snapshot = _write_current_snapshot(tmp_path, ("/long", html))
    config = PipelineConfig()
    documents, failed = await extract_documents(snapshot, config)
    assert failed == 0
    assert len(documents) == 1
    validate_documents(documents, embedded=False)
    split = await build_splitter(config).process(documents[0])
    staged = await _FakeEmbedder().process(split)
    validate_documents([staged], embedded=True)

    url = f"{DOCS}/long"
    baseline = await build_pipeline(
        index=_MemoryIndex(), embedder=_FakeEmbedder()
    ).run_file(File(path=url, name="long.html", raw=html, source_id=url))

    assert staged == baseline


@pytest.mark.parametrize("all_failed", [False, True])
async def test_staged_extraction_counts_conversion_errors(
    tmp_path: Path, all_failed: bool
) -> None:
    pages = [("/invalid", b"\xff")]
    if not all_failed:
        pages.append(("/good", _html("Good", "Evidence")))
    snapshot = _write_current_snapshot(tmp_path, *pages)

    with capture_logs() as logs:
        documents, failed = await extract_documents(
            snapshot, PipelineConfig(version="next")
        )

    assert failed == 1
    assert len(documents) == (0 if all_failed else 1)
    assert all(document.metadata.pipeline_version == "next" for document in documents)
    assert any(
        log.get("event") == "refresh_page_failed"
        and log.get("snapshot") == snapshot.directory.name
        and log.get("stage") == "extract"
        and log.get("url") == f"{DOCS}/invalid"
        and log.get("error_code") == "extraction_failed"
        for log in logs
    )


@pytest.mark.parametrize("kind", ["corrupt", "symlink"])
async def test_staged_extraction_does_not_count_snapshot_corruption_as_conversion_error(
    tmp_path: Path, kind: str
) -> None:
    snapshot = _write_current_snapshot(tmp_path, ("/guide", _html("Guide", "Body")))
    raw = snapshot.directory / "raw" / "guide.html"
    if kind == "corrupt":
        raw.write_bytes(b"Modified")
    else:
        target = tmp_path / "target.html"
        raw.rename(target)
        raw.symlink_to(target)

    with pytest.raises(SnapshotReadError):
        await extract_documents(snapshot, PipelineConfig())


async def test_ingest_snapshot_stops_on_external_failure(tmp_path: Path) -> None:
    snapshot = _write_current_snapshot(
        tmp_path,
        ("/first", _html("First", "Body")),
        ("/second", _html("Second", "Body")),
    )
    embedder = _FailingEmbedder()
    index = _MemoryIndex()
    pipeline = build_pipeline(index=index, embedder=embedder)

    with pytest.raises(EmbedderException, match="embedding unavailable"):
        await ingest_snapshot(snapshot, pipeline)

    assert embedder.calls == 1
    assert index.documents == []


def test_ingest_command_fails_without_current_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["ingest", "--snapshots", str(tmp_path)])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "No current snapshot" in output
    assert "MISTRAL_API_KEY" not in output
    assert "Traceback" not in output


def test_cli_does_not_eagerly_import_vespa() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import docstral_worker.cli; "
            "assert 'docstral_vespa' not in sys.modules",
        ],
        check=True,
    )


def test_make_ingest_rebuilds_local_vespa() -> None:
    result = subprocess.run(
        ["make", "--dry-run", "ingest"],
        check=True,
        capture_output=True,
        text=True,
    )

    commands = result.stdout
    steps = (
        "mistral-vespa local down",
        "mistral-vespa local up",
        "mistral-vespa migrate",
        "docstral-worker ingest",
    )
    positions = [commands.index(step) for step in steps]
    assert positions == sorted(positions)
    assert "--app-dir packages/vespa/src/docstral_vespa" in commands


@pytest.mark.parametrize("endpoint", ["localhost:8080", "ftp://localhost:8080"])
def test_ingest_command_rejects_invalid_endpoint(endpoint: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["ingest", "--vespa-endpoint", endpoint])

    assert exc_info.value.code == 2


def _html(title: str, body: str) -> bytes:
    return (
        f"<html><head><title>{title} | Mistral Docs</title></head>"
        f'<body><main><article class="prose"><h1>{title}</h1>'
        f"<p>{body}</p></article></main></body></html>"
    ).encode()


def _write_current_snapshot(root: Path, *pages: tuple[str, bytes]) -> CurrentSnapshot:
    entries = tuple(
        sorted(
            (_stored(path, body) for path, body in pages),
            key=lambda page: page.canonical_url,
        )
    )
    result = CrawlResult(
        pages=entries,
        counts=CrawlCounts(
            sitemap_english=len(entries),
            sitemap_french=0,
            discovered_by_link=0,
            admitted=len(entries),
            stored=len(entries),
            rejected=0,
            failed=0,
            status_200=len(entries),
            status_304=0,
            redirects=0,
            external_links=0,
            malformed_links=0,
            rejections={},
        ),
        complete=True,
        duration_seconds=0.1,
    )
    write_snapshot(
        root,
        CRAWLED_AT,
        SitemapSnapshot(
            url=f"{DOCS}/sitemap.xml",
            sha256="a" * 64,
            english_urls=tuple(entry.canonical_url for entry in entries),
            french_urls=(),
        ),
        result,
    )
    snapshot = current_snapshot(root)
    assert snapshot is not None
    return snapshot


def _stored(path: str, body: bytes) -> CrawlEntry:
    url = f"{DOCS}{path}"
    return CrawlEntry(
        canonical_url=url,
        requested_url=url,
        final_url=url,
        discovered_via=DiscoveryVia.SITEMAP,
        decision=PageDecision.STORED,
        status_code=200,
        raw_sha256=sha256(body).hexdigest(),
        body=body,
    )
