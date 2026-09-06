import asyncio
from hashlib import sha256
from pathlib import Path
from threading import Event

import httpx
import pytest
from docstral_worker import IngestionError
from docstral_worker.incremental import IncrementalIngestion
from docstral_worker.maintenance import WorkerState
from docstral_worker.snapshot import current_snapshot
from test_incremental import _Corpus, _snapshot
from test_ingest import _FakeEmbedder, _html
from test_refresh import _http_client
from test_refresh import crawl_transport as crawl_transport


async def test_crawl_returns_the_new_snapshot_without_preparing_or_indexing(
    tmp_path: Path,
    crawl_transport: list[httpx.Request],
) -> None:
    previous = _snapshot(tmp_path, {"/old": _html("Old", "Previous evidence")})
    corpus, embedder = _Corpus("/old"), _FakeEmbedder()

    reference = await IncrementalIngestion(
        tmp_path, corpus=corpus, embedder=embedder
    ).crawl()

    current = current_snapshot(tmp_path / "snapshots")
    assert current is not None
    assert reference.name == current.directory.name
    assert reference != previous
    assert (
        reference.manifest_sha256
        == sha256((current.directory / "manifest.json").read_bytes()).hexdigest()
    )
    assert current.get("https://docs.mistral.ai/new") is not None
    assert current.manifest.counts.stored == 1
    assert {request.url.path for request in crawl_transport} == {
        "/robots.txt",
        "/sitemap.xml",
        "/new",
    }
    assert corpus.inventory_calls == 0
    assert corpus.mutations == []
    assert list(corpus.documents) == ["https://docs.mistral.ai/old"]
    assert embedder.inputs == []
    assert not (tmp_path / "index-state.json").exists()
    assert not (current.directory / "prepared").exists()
    assert not (tmp_path / ".publication-pending").exists()


@pytest.mark.parametrize("failure", ["sitemap", "page"])
async def test_failed_crawl_does_not_return_the_previous_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    previous = _snapshot(tmp_path, {"/old": _html("Old", "Previous evidence")})
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/sitemap.xml":
            return httpx.Response(
                404 if failure == "sitemap" else 200,
                text='<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                "<url><loc>https://docs.mistral.ai/broken</loc></url></urlset>",
            )
        return httpx.Response(422)

    _http_client(monkeypatch, handler)
    corpus, embedder = _Corpus("/old"), _FakeEmbedder()
    with pytest.raises(IngestionError):
        await IncrementalIngestion(tmp_path, corpus=corpus, embedder=embedder).crawl()

    current = current_snapshot(tmp_path / "snapshots")
    assert current is not None and current.directory.name == previous.name
    assert len(list((tmp_path / "snapshots").glob("*-failed"))) == (
        1 if failure == "page" else 0
    )
    assert requests
    assert corpus.inventory_calls == 0
    assert corpus.mutations == []
    assert list(corpus.documents) == ["https://docs.mistral.ai/old"]
    assert embedder.inputs == []
    assert not (tmp_path / "index-state.json").exists()
    async with WorkerState(tmp_path).lock():
        pass


@pytest.mark.parametrize("blocked", ["maintenance", "overlap", "legacy"])
async def test_blocked_crawl_does_not_fetch(
    tmp_path: Path,
    crawl_transport: list[httpx.Request],
    blocked: str,
) -> None:
    state = WorkerState(tmp_path)
    corpus, embedder = _Corpus(), _FakeEmbedder()
    ingestion = IncrementalIngestion(tmp_path, corpus=corpus, embedder=embedder)

    if blocked == "legacy":
        (tmp_path / ".publication-pending").write_text("legacy publication\n")
        with pytest.raises(IngestionError, match="previous release"):
            await ingestion.crawl()
        assert (tmp_path / ".publication-pending").read_text() == "legacy publication\n"
    elif blocked == "maintenance":
        await state.set_maintenance(True, timeout=0)
        with pytest.raises(IngestionError, match="maintenance"):
            await ingestion.crawl()
    else:
        async with state.lock():
            with pytest.raises(IngestionError, match="holds the worker lock"):
                await ingestion.crawl()

    assert not crawl_transport
    assert not (tmp_path / "snapshots").exists()
    assert corpus.inventory_calls == 0
    assert corpus.mutations == []
    assert embedder.inputs == []


@pytest.mark.parametrize("cancellations", [1, 2])
async def test_cancelled_crawl_retains_lock_until_blocking_fetch_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancellations: int,
) -> None:
    started, finish = Event(), Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/sitemap.xml":
            return httpx.Response(
                200,
                text='<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                "<url><loc>https://docs.mistral.ai/new</loc></url></urlset>",
            )
        started.set()
        assert finish.wait(5)
        return httpx.Response(
            200,
            content=_html("New", "Updated evidence"),
            headers={"content-type": "text/html"},
        )

    _http_client(monkeypatch, handler)
    corpus, embedder = _Corpus(), _FakeEmbedder()
    state = WorkerState(tmp_path)
    task = asyncio.create_task(
        IncrementalIngestion(tmp_path, corpus=corpus, embedder=embedder).crawl()
    )
    try:
        assert await asyncio.to_thread(started.wait, 5)
        for _ in range(cancellations):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            with pytest.raises(IngestionError, match="holds the worker lock"):
                async with state.lock():
                    pytest.fail("Cancellation released the active crawl's lock")
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert task.cancelled()
    async with state.lock():
        current = current_snapshot(tmp_path / "snapshots")
        assert current is not None
        assert current.get("https://docs.mistral.ai/new") is not None
        assert not (current.directory / "prepared").exists()
        assert not (tmp_path / ".publication-pending").exists()
    assert corpus.inventory_calls == 0
    assert corpus.mutations == []
    assert embedder.inputs == []
    assert not (tmp_path / "index-state.json").exists()
