from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from docstral_worker.cli import main
from docstral_worker.snapshot import current_snapshot
from test_ingest import _html


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


def test_crawl_cli_still_uses_shared_snapshot_path(
    tmp_path: Path,
    crawl_transport: list[httpx.Request],
) -> None:
    assert main(["crawl", "--out", str(tmp_path), "--delay", "0"]) == 0
    assert current_snapshot(tmp_path) is not None
    assert crawl_transport
