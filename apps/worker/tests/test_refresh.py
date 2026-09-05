import asyncio
from collections.abc import Callable
from pathlib import Path
from threading import Event

import httpx
import pytest
from docstral_worker import IngestionError
from docstral_worker.cli import main
from docstral_worker.ingest import build_pipeline
from docstral_worker.maintenance import PublicationState
from docstral_worker.publish import publish_current
from docstral_worker.snapshot import current_snapshot
from test_ingest import _FakeEmbedder, _html, _MemoryIndex
from test_publish import _Corpus, _Mcp, _snapshot


@pytest.fixture
def crawl_transport(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/sitemap.xml":
            return httpx.Response(
                200,
                text='<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                "<url><loc>https://docs.mistral.ai/new</loc></url></urlset>",
            )
        return httpx.Response(
            200,
            content=_html("New", "Updated evidence"),
            headers={"content-type": "text/html"},
        )

    _http_client(monkeypatch, handler)
    return requests


def _http_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    original = httpx.Client

    def factory(**kwargs: object) -> httpx.Client:
        return original(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "Client", factory)


async def test_refresh_crawls_and_replaces_old_corpus(
    tmp_path: Path,
    crawl_transport: list[httpx.Request],
) -> None:
    _snapshot(tmp_path, "/old")
    index = _MemoryIndex()
    events: list[str] = []
    state = PublicationState(tmp_path)
    outcome = await publish_current(
        state,
        build_pipeline(index=index, embedder=_FakeEmbedder()),
        _Corpus(index, events),
        _Mcp(events),
        refresh=True,
    )
    assert (outcome.indexed, outcome.failed) == (1, 0)
    assert [doc.source_id for doc in index.documents] == ["https://docs.mistral.ai/new"]
    snapshot = current_snapshot(tmp_path / "snapshots")
    assert snapshot is not None
    assert state.published.read_text().strip() == snapshot.directory.name
    assert events == ["check corpus", "check mcp", "stop", "clear", "start"]
    assert {request.url.path for request in crawl_transport} == {
        "/robots.txt",
        "/sitemap.xml",
        "/new",
    }


@pytest.mark.parametrize("blocked", ["maintenance", "overlap"])
async def test_blocked_refresh_does_not_fetch(
    tmp_path: Path,
    crawl_transport: list[httpx.Request],
    blocked: str,
) -> None:
    state = PublicationState(tmp_path)
    index = _MemoryIndex()
    events: list[str] = []

    async def refresh() -> None:
        with pytest.raises(IngestionError):
            await publish_current(
                state,
                build_pipeline(index=index, embedder=_FakeEmbedder()),
                _Corpus(index, events),
                _Mcp(events),
                refresh=True,
            )

    if blocked == "maintenance":
        await state.set_maintenance(True, timeout=0)
        await refresh()
    else:
        async with state.lock():
            await refresh()
    assert not crawl_transport
    assert not events


@pytest.mark.parametrize("failure", ["sitemap", "page"])
async def test_failed_crawl_never_republishes_previous_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    previous = _snapshot(tmp_path, "/old")

    def handler(request: httpx.Request) -> httpx.Response:
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
    events: list[str] = []
    index = _MemoryIndex()
    with pytest.raises(IngestionError):
        await publish_current(
            PublicationState(tmp_path),
            build_pipeline(index=index, embedder=_FakeEmbedder()),
            _Corpus(index, events),
            _Mcp(events),
            refresh=True,
        )
    assert not events
    current = current_snapshot(tmp_path / "snapshots")
    assert current is not None and current.directory == previous
    assert len(list((tmp_path / "snapshots").glob("*-failed"))) == (
        1 if failure == "page" else 0
    )


async def test_repeated_cancellation_holds_lock_until_crawl_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, finish = Event(), Event()

    def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        assert finish.wait(5)
        return httpx.Response(404)

    _http_client(monkeypatch, handler)
    state = PublicationState(tmp_path)
    index = _MemoryIndex()
    events: list[str] = []
    task = asyncio.create_task(
        publish_current(
            state,
            build_pipeline(index=index, embedder=_FakeEmbedder()),
            _Corpus(index, events),
            _Mcp(events),
            refresh=True,
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 5)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            with pytest.raises(IngestionError, match="holds the worker lock"):
                async with state.lock():
                    pytest.fail("Cancellation released an active crawl's lock")
    finally:
        finish.set()
        with pytest.raises((asyncio.CancelledError, IngestionError)):
            await task
    assert not events
    async with state.lock():
        assert not state.pending.exists()


def test_crawl_cli_still_uses_shared_snapshot_path(
    tmp_path: Path,
    crawl_transport: list[httpx.Request],
) -> None:
    assert main(["crawl", "--out", str(tmp_path), "--delay", "0"]) == 0
    assert current_snapshot(tmp_path) is not None
    assert crawl_transport
