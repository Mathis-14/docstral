import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx
import httpx2
import pytest
from docstral_worker.crawl import CrawlCounts, CrawlEntry, CrawlResult
from docstral_worker.snapshot import CurrentSnapshot, current_snapshot, write_snapshot

DOCS = "https://docs.mistral.ai"


def html(title: str, body: str) -> bytes:
    return (
        f"<html><head><title>{title} | Mistral Docs</title></head>"
        f'<body><main><article class="prose"><h1>{title}</h1>'
        f"<p>{body}</p></article></main></body></html>"
    ).encode()


def snapshot(root: Path, *pages: tuple[str, bytes]) -> CurrentSnapshot:
    result = CrawlResult(
        pages=tuple(
            CrawlEntry(url=f"{DOCS}{path}", status="downloaded", body=body)
            for path, body in pages
        ),
        counts=CrawlCounts(stored=len(pages)),
        complete=True,
        duration_seconds=0,
    )
    write_snapshot(root, datetime(2026, 9, 3, 12, tzinfo=UTC), result)
    saved = current_snapshot(root)
    assert saved is not None
    return saved


class Services:
    def __init__(self) -> None:
        self.pages = {"/a": html("A", "Evidence A"), "/b": html("B", "Evidence B")}
        self.redirects: dict[str, str] = {}
        self.documents: dict[str, dict[str, object]] = {}
        self.requests: list[httpx.Request] = []
        self.fail: str | None = None
        self.seeds = ["/a"]
        self.robots_status = 200
        self.responses: dict[str, httpx.Response | httpx2.HTTPError] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "docs.mistral.ai":
            if path in self.responses:
                response = self.responses[path]
                if isinstance(response, httpx2.HTTPError):
                    raise response
                return response
            if path == "/robots.txt":
                return httpx.Response(
                    self.robots_status, text="User-agent: *\nAllow: /\n"
                )
            if path == "/sitemap.xml":
                entries = "".join(
                    f"<url><loc>https://docs.mistral.ai{p}</loc></url>"
                    for p in self.seeds
                )
                return httpx.Response(200, text=f"<urlset>{entries}</urlset>")
            if path in self.redirects:
                return httpx.Response(301, headers={"location": self.redirects[path]})
            if path == self.fail:
                return httpx.Response(403)
            return httpx.Response(
                200 if path in self.pages else 404,
                content=self.pages.get(path, b""),
                headers={"content-type": "text/html"},
            )
        if request.url.host == "mistral.test":
            inputs = json.loads(request.content)["input"]
            return httpx.Response(
                200,
                json={
                    "id": "embedding",
                    "object": "list",
                    "model": "mistral-embed",
                    "data": [
                        {"object": "embedding", "index": i, "embedding": [1.0] * 1024}
                        for i in range(len(inputs))
                    ],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                },
            )
        assert request.url.host == "vespa.test"
        if request.method == "POST":
            if self.fail in ("index", "cleanup") and "/docs/docs/" in path:
                return httpx.Response(400, json={"message": "private-api-credential"})
            self.documents[path] = json.loads(request.content)["fields"]
        elif request.method == "DELETE":
            if self.fail == "cleanup" and any(
                previous.method == "POST" and "/docs/docs/" in previous.url.path
                for previous in self.requests
            ):
                return httpx.Response(503, json={"message": "private-api-credential"})
            selection = request.url.params.get("selection")
            if selection:
                document_id = selection.split('"')[1]
                self.documents = {
                    key: fields
                    for key, fields in self.documents.items()
                    if not (
                        key.startswith(path) and fields["document_id"] == document_id
                    )
                }
            else:
                self.documents.pop(path, None)
        elif not path.endswith("/"):
            if path not in self.documents:
                return httpx.Response(404)
            return httpx.Response(
                200, json={"id": path, "fields": self.documents[path]}
            )
        records = [
            {
                "id": key,
                "fields": {
                    name: fields[name]
                    for name in request.url.params.get(
                        "fieldSet", "all:source_id,document_id"
                    )
                    .split(":", 1)[1]
                    .split(",")
                },
            }
            for key, fields in self.documents.items()
            if key.startswith(path)
        ]
        return httpx.Response(
            200, json={"documents": records, "documentCount": len(records)}
        )

    def calls(self) -> Counter[str]:
        return Counter(request.url.host for request in self.requests)


@pytest.fixture
def services(monkeypatch: pytest.MonkeyPatch) -> Services:
    boundary = Services()
    sync_client, async_client = httpx.Client, httpx.AsyncClient

    def sync_factory(**kwargs: object) -> httpx.Client:
        return sync_client(transport=httpx.MockTransport(boundary.handle))

    def async_factory(**kwargs: object) -> httpx.AsyncClient:
        return async_client(
            base_url=str(kwargs.get("base_url", "")),
            transport=httpx.MockTransport(boundary.handle),
        )

    async def send(
        transport: httpx2.AsyncHTTPTransport, request: httpx2.Request
    ) -> httpx2.Response:
        response = boundary.handle(
            httpx.Request(
                request.method,
                str(request.url),
                headers=dict(request.headers),
                content=request.content,
            )
        )
        return httpx2.Response(
            response.status_code,
            headers=dict(response.headers),
            content=response.content,
            request=request,
        )

    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", send)
    monkeypatch.setattr(httpx, "Client", sync_factory)
    monkeypatch.setattr(httpx, "AsyncClient", async_factory)
    for name, value in {
        "VESPA_ENDPOINT": "http://vespa.test",
        "MISTRAL_API_KEY": "test-key",
        "MISTRAL_API_URL": "https://mistral.test",
        "DOCSTRAL_CRAWL_DELAY": "0",
    }.items():
        monkeypatch.setenv(name, value)
    return boundary
