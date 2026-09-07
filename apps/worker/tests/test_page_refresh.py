import json
from collections import Counter

import httpx
import pytest
from docstral_worker.refresh.activities import sync_page
from docstral_worker.refresh.models import PageResult
from temporalio.testing import ActivityEnvironment
from test_ingest import _html


class Services:
    def __init__(self) -> None:
        self.pages = {"/a": _html("A", "Evidence A"), "/b": _html("B", "Evidence B")}
        self.redirects: dict[str, str] = {}
        self.documents: dict[str, dict[str, object]] = {}
        self.requests: list[httpx.Request] = []
        self.fail: str | None = None
        self.seeds = ["/a"]
        self.robots_status = 200

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "docs.mistral.ai":
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
                "fields": {name: fields[name] for name in ("source_id", "document_id")},
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


async def sync(path: str = "/a") -> PageResult:
    return await ActivityEnvironment().run(sync_page, f"https://docs.mistral.ai{path}")


async def test_unchanged_page_skips_embedding_and_indexing(services: Services) -> None:
    assert (await sync()).status == "indexed"
    services.requests.clear()
    assert (await sync()).status == "unchanged"
    assert services.calls()["mistral.test"] == 0
    assert all(request.method == "GET" for request in services.requests)


@pytest.mark.parametrize("status_code", [429, 503])
async def test_temporary_robots_failure_allows_native_page_retry(
    services: Services, status_code: int
) -> None:
    from mistralai.workflows.exceptions import WorkflowError

    services.robots_status = status_code
    with pytest.raises(WorkflowError) as error:
        await sync()
    assert not error.value.non_retryable
    assert all(request.url.path == "/robots.txt" for request in services.requests)


async def test_changed_page_replaces_chunks_and_confirms_hash(
    services: Services,
) -> None:
    await sync()
    services.pages["/a"] = _html("A updated", "New evidence")
    assert (await sync()).status == "indexed"
    chunks = [
        fields["content"]
        for path, fields in services.documents.items()
        if "/docs/docs/" in path
    ]
    assert chunks == ["# A updated\n\nNew evidence"]
    assert (await sync()).status == "unchanged"


async def test_interrupted_indexing_does_not_confirm_page(services: Services) -> None:
    from mistralai.workflows.exceptions import WorkflowError

    await sync()
    services.pages["/a"] = _html("Changed", "Changed evidence")
    services.fail = "index"
    with pytest.raises(WorkflowError) as error:
        await sync()
    assert "private-api-credential" not in str(error.value)
    assert error.value.non_retryable
    assert all(
        fields["index_hash"] == ""
        for path, fields in services.documents.items()
        if "/pages/pages/" in path
    )
    services.fail = None
    services.pages["/a"] = _html("A", "Evidence A")
    assert (await sync()).status == "indexed"


async def test_extraction_failure_preserves_indexed_page(services: Services) -> None:
    await sync()
    previous = services.documents.copy()
    services.pages["/a"] = b"<html>Missing article</html>"
    assert (await sync()).status == "extraction_failed"
    assert services.documents == previous


async def test_redirect_returns_target_without_fetching_it(services: Services) -> None:
    services.redirects["/a"] = "/b"
    result = await sync()
    assert result.redirect_url == "https://docs.mistral.ai/b"
    assert all(request.url.path != "/b" for request in services.requests)
    assert not services.documents


async def test_syntax_redirect_is_fetched_under_same_identity(
    services: Services,
) -> None:
    services.redirects["/a"] = "/en/a/"
    services.pages["/en/a/"] = services.pages["/a"]
    assert (await sync()).status == "indexed"
    assert {fields["source_id"] for fields in services.documents.values()} == {
        "https://docs.mistral.ai/a"
    }


async def test_failed_vespa_cleanup_does_not_leak_secrets_in_logs(
    services: Services,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from docstral_worker.refresh.worker import run_worker
    from mistralai import workflows
    from mistralai.workflows.exceptions import WorkflowError
    from temporalio.api.failure.v1 import Failure
    from temporalio.converter import DataConverter

    async def execute(definitions: list[type[object]]) -> None:
        services.fail = "cleanup"
        with pytest.raises(WorkflowError) as error:
            await sync()
        failure = Failure()
        await DataConverter.default.encode_failure(error.value, failure)
        assert "private-api-credential" not in str(failure)

    monkeypatch.setenv("DEPLOYMENT_NAME", "test")
    monkeypatch.setattr(workflows, "run_worker", execute)
    await run_worker()
    logs = capsys.readouterr()
    assert "private-api-credential" not in logs.out + logs.err
    assert "refresh_activity_failed" in logs.out + logs.err


async def test_lost_heartbeat_stops_page_before_index_writes(
    services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mistralai import workflows
    from mistralai.workflows.exceptions import WorkflowError

    def disconnected() -> None:
        raise httpx.ConnectError("Control plane unavailable")

    monkeypatch.setattr(workflows, "activity_heartbeat", disconnected)
    with pytest.raises(WorkflowError):
        await sync()
    assert not services.documents
