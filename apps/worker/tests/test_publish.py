from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from docstral_worker import IngestionError
from docstral_worker.cli import main
from docstral_worker.ingest import build_pipeline
from docstral_worker.maintenance import PublicationState
from docstral_worker.publish import VespaCorpus, publish_current
from docstral_worker.snapshot import SnapshotManifest, write_snapshot
from mistralai.search.toolkit.embedding.errors import EmbedderException
from mistralai.search.toolkit.plugins.vespa import VespaClient, VespaClientConfig
from structlog.testing import capture_logs
from test_ingest import _FailingEmbedder, _FakeEmbedder, _html, _MemoryIndex
from test_kubernetes import _Apps, _Core
from test_snapshot import CRAWLED_AT, failed, result, sitemap, stored


class _Corpus:
    def __init__(self, index: _MemoryIndex, events: list[str]) -> None:
        self.index = index
        self.events = events
        self.error: str | None = None

    async def check(self) -> None:
        self.events.append("check corpus")
        if self.error == "check":
            raise RuntimeError("Vespa unavailable")

    async def clear(self) -> None:
        self.events.append("clear")
        self.index.documents.clear()
        if self.error == "clear":
            raise RuntimeError("Vespa delete interrupted")


class _Mcp:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.running = True
        self.stop_fails = False

    async def check(self) -> None:
        self.events.append("check mcp")

    async def stop(self) -> None:
        self.events.append("stop")
        if self.stop_fails:
            raise RuntimeError("pods still running")
        self.running = False

    async def start(self) -> None:
        self.events.append("start")
        self.running = True


def _snapshot(root: Path, *paths: str, offset: int = 0) -> Path:
    return write_snapshot(
        root / "snapshots",
        CRAWLED_AT + timedelta(seconds=offset),
        sitemap(),
        result(*(stored(path, _html(path, "Evidence")) for path in paths)),
    )


async def test_republication_removes_absent_pages(tmp_path: Path) -> None:
    _snapshot(tmp_path, "/a", "/b")
    index = _MemoryIndex()
    events: list[str] = []
    corpus, mcp = _Corpus(index, events), _Mcp(events)
    pipeline = build_pipeline(index=index, embedder=_FakeEmbedder())
    state = PublicationState(tmp_path)

    first = await publish_current(state, pipeline, corpus, mcp)
    assert first.indexed == 2
    _snapshot(tmp_path, "/a", offset=1)
    second = await publish_current(state, pipeline, corpus, mcp)

    assert second.indexed == 1
    assert [doc.source_id for doc in index.documents] == ["https://docs.mistral.ai/a"]
    assert events == ["check corpus", "check mcp", "stop", "clear", "start"] * 2
    assert not state.pending.exists()
    assert state.published.read_text().strip() == "20260903T120001Z"


@pytest.mark.parametrize("problem", ["missing", "empty", "failed", "corrupt"])
async def test_preflight_preserves_index_and_mcp(tmp_path: Path, problem: str) -> None:
    if problem == "empty":
        _snapshot(tmp_path)
    elif problem in {"failed", "corrupt"}:
        directory = _snapshot(tmp_path, "/a")
        if problem == "corrupt":
            (directory / "raw/a.html").write_bytes(b"damaged")
        else:
            (directory / "manifest.json").write_text(
                # A forged successful name must not hide a failed crawl.
                SnapshotManifest(
                    crawled_at=CRAWLED_AT,
                    sitemap_url=sitemap().url,
                    sitemap_sha256=sitemap().sha256,
                    pages=(failed("/a"),),
                    counts=result(failed("/a"), complete=False).counts,
                ).model_dump_json()
            )
    index = _MemoryIndex()
    events: list[str] = []
    mcp = _Mcp(events)
    with pytest.raises(IngestionError):
        await publish_current(
            PublicationState(tmp_path),
            build_pipeline(index=index, embedder=_FakeEmbedder()),
            _Corpus(index, events),
            mcp,
        )
    assert events == []
    assert mcp.running


@pytest.mark.parametrize("failure", ["check", "stop", "clear", "embed"])
async def test_dependency_failure_never_restarts_mcp(
    tmp_path: Path, failure: str
) -> None:
    _snapshot(tmp_path, "/a")
    index = _MemoryIndex()
    events: list[str] = []
    corpus, mcp = _Corpus(index, events), _Mcp(events)
    corpus.error = failure
    mcp.stop_fails = failure == "stop"
    embedder = _FailingEmbedder() if failure == "embed" else _FakeEmbedder()
    state = PublicationState(tmp_path)
    with pytest.raises((RuntimeError, EmbedderException)):
        await publish_current(
            state, build_pipeline(index=index, embedder=embedder), corpus, mcp
        )
    assert "start" not in events
    assert state.pending.exists() == (failure in {"clear", "embed"})
    if failure in {"check", "stop"}:
        assert "clear" not in events
    else:
        assert not mcp.running
        # A successful retry clears the durable failure marker and reopens MCP.
        corpus.error = None
        await publish_current(
            state, build_pipeline(index=index, embedder=_FakeEmbedder()), corpus, mcp
        )
        assert not state.pending.exists()
        assert mcp.running


async def test_page_failure_is_partial_but_zero_indexed_stays_offline(
    tmp_path: Path,
) -> None:
    write_snapshot(
        tmp_path / "snapshots",
        CRAWLED_AT,
        sitemap(),
        result(stored("/a", _html("A", "Evidence")), stored("/empty", b"<main/>")),
    )
    index = _MemoryIndex()
    events: list[str] = []
    corpus, mcp = _Corpus(index, events), _Mcp(events)
    state = PublicationState(tmp_path)
    pipeline = build_pipeline(index=index, embedder=_FakeEmbedder())
    with capture_logs() as logs:
        outcome = await publish_current(state, pipeline, corpus, mcp)
    assert (outcome.indexed, outcome.failed) == (1, 1)
    assert mcp.running
    assert any(log.get("decision") == "failed" for log in logs)
    write_snapshot(
        tmp_path / "snapshots",
        CRAWLED_AT + timedelta(seconds=1),
        sitemap(),
        result(stored("/empty", b"<main/>")),
    )
    with pytest.raises(IngestionError, match="No page was indexed"):
        await publish_current(state, pipeline, corpus, mcp)
    assert state.pending.exists()
    assert not mcp.running


async def test_vespa_admin_uses_scoped_public_paginated_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.params["selection"] == "docs"
        assert request.url.path.rstrip("/") == "/document/v1/docs/docs/docid"
        if request.method == "GET":
            return httpx.Response(200, json={"documents": []})
        assert request.url.params["cluster"] == "content"
        if "continuation" not in request.url.params:
            return httpx.Response(
                200, json={"documentCount": 1, "continuation": "next"}
            )
        return httpx.Response(200, json={"documentCount": 1})

    original = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return original(
            base_url=str(kwargs["base_url"]), transport=httpx.MockTransport(handler)
        )

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    client = VespaClient(VespaClientConfig(endpoint="http://vespa.test"))
    try:
        corpus = VespaCorpus(client, cluster="content")
        await corpus.check()
        await corpus.clear()
    finally:
        await client.aclose()
    assert [request.method for request in requests] == ["GET", "DELETE", "DELETE"]
    assert requests[0].url.params["cluster"] == "content"
    assert requests[-1].url.params["continuation"] == "next"


def test_publish_requires_explicit_cluster_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("VESPA_ENDPOINT", "POD_NAMESPACE", "MCP_DEPLOYMENT"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as error:
        main(["publish"])
    assert error.value.code == 2


@pytest.mark.parametrize(
    "target", ["root", "current", "snapshot", "manifest", "raw", "page"]
)
async def test_publication_refuses_symlinks_before_mutation(
    tmp_path: Path, target: str
) -> None:
    directory = _snapshot(tmp_path, "/a")
    paths = {
        "root": tmp_path / "snapshots",
        "current": tmp_path / "snapshots/current",
        "snapshot": directory,
        "manifest": directory / "manifest.json",
        "raw": directory / "raw",
        "page": directory / "raw/a.html",
    }
    path = paths[target]
    outside = tmp_path / "outside"
    path.rename(outside)
    path.symlink_to(outside, target_is_directory=outside.is_dir())
    index = _MemoryIndex()
    events: list[str] = []
    with pytest.raises(IngestionError, match="symbolic-link"):
        await publish_current(
            PublicationState(tmp_path),
            build_pipeline(index=index, embedder=_FakeEmbedder()),
            _Corpus(index, events),
            _Mcp(events),
        )
    assert events == []
    assert outside.exists()


@pytest.mark.parametrize("outcome", ["complete", "partial", "feed_failure"])
def test_publish_cli_runs_real_pipeline_and_vespa_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
) -> None:
    # Replace only external SDK boundaries; CLI, publication and toolkit stay real.
    from kubernetes.aio import client, config  # type: ignore[import-untyped]

    pages = [stored("/a", _html("A", "Evidence"))]
    if outcome == "partial":
        pages.append(stored("/empty", b"<main/>"))
    write_snapshot(tmp_path / "snapshots", CRAWLED_AT, sitemap(), result(*pages))
    apps = _Apps()
    monkeypatch.setattr(config, "load_incluster_config", lambda: None)
    monkeypatch.setattr(client, "AppsV1Api", lambda api: apps)
    monkeypatch.setattr(client, "CoreV1Api", lambda api: _Core())

    def embedder_factory(*, model_name: str, max_retry: int) -> _FakeEmbedder:
        assert (model_name, max_retry) == ("mistral-embed", 6)
        return _FakeEmbedder()

    monkeypatch.setattr("docstral_worker.publish.MistralEmbedder", embedder_factory)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "vespa.test"
        if request.method == "DELETE":
            assert request.url.params["cluster"] == "content"
        if request.method == "POST" and outcome == "feed_failure":
            return httpx.Response(500, text="private-upstream-details")
        return httpx.Response(200, json={"documents": [], "documentCount": 0})

    clients: list[httpx.AsyncClient] = []
    original = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        http = original(
            base_url=str(kwargs["base_url"]), transport=httpx.MockTransport(handler)
        )
        clients.append(http)
        return http

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    code = main(
        [
            "publish",
            "--data-dir",
            str(tmp_path),
            "--vespa-endpoint",
            "http://vespa.test",
            "--namespace",
            "docstral",
            "--mcp-deployment",
            "mcp",
        ]
    )
    assert code == (0 if outcome == "complete" else 1)
    assert len(apps.patches) == (1 if outcome == "feed_failure" else 2)
    assert (tmp_path / ".publication-pending").exists() == (outcome == "feed_failure")
    assert len(clients) == 1 and clients[0].is_closed
    assert any(request.method == "POST" for request in requests)
    assert requests[0].method == "GET"
    assert requests[0].url.params["cluster"] == "content"
    assert requests[1].method == "DELETE"
    assert requests[1].url.params["selection"] == "docs"
    output = capsys.readouterr().out
    assert "private-upstream-details" not in output
    expected_log = "publish_failed" if outcome == "feed_failure" else '"indexed": 1'
    assert expected_log in output
