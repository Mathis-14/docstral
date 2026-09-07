import httpx
import httpx2
import pytest
from docstral_worker.crawl import crawl
from docstral_worker.fetch import USER_AGENT, FetchError, get, http_client
from worker_fixtures import DOCS, Services
from worker_fixtures import services as services


async def test_downloads_fresh_html_without_etag_cache(services: Services) -> None:
    for _ in range(2):
        result = await crawl((DOCS + "/a",), delay=0)
        assert result.pages[0].body == services.pages["/a"]
    requests = [request for request in services.requests if request.url.path == "/a"]
    assert len(requests) == 2
    assert all(request.headers["user-agent"] == USER_AGENT for request in requests)
    assert all("if-none-match" not in request.headers for request in requests)


@pytest.mark.parametrize(("retries", "attempts"), [(0, 1), (2, 3)])
async def test_transient_retries_belong_to_capture_or_native_activity(
    services: Services, retries: int, attempts: int
) -> None:
    services.responses["/a"] = httpx.Response(503, headers={"retry-after": "31"})
    result = await crawl((DOCS + "/a",), delay=0, retries=retries)
    assert result.pages[0].transient
    assert sum(request.url.path == "/a" for request in services.requests) == attempts


async def test_permanent_http_failure_is_not_retried(services: Services) -> None:
    services.responses["/a"] = httpx.Response(403)
    result = await crawl((DOCS + "/a",), delay=0, retries=2)
    assert not result.pages[0].transient
    assert sum(request.url.path == "/a" for request in services.requests) == 1


async def test_timeout_remains_retryable(services: Services) -> None:
    services.responses["/a"] = httpx2.ReadTimeout("Slow page")
    result = await crawl((DOCS + "/a",), delay=0, retries=2)
    assert result.pages[0].transient
    assert sum(request.url.path == "/a" for request in services.requests) == 3


@pytest.mark.parametrize(
    "target",
    ["https://example.com/a", "https://user:sensitive-value@docs.mistral.ai/a"],
)
async def test_out_of_scope_redirect_is_never_downloaded(
    services: Services, target: str
) -> None:
    services.redirects["/a"] = target
    result = await crawl((DOCS + "/a",), delay=0, follow_links=True)
    assert result.pages[0].reason == "outside_host"
    assert [request.url.path for request in services.requests] == ["/robots.txt", "/a"]
    assert "sensitive-value" not in result.model_dump_json()


async def test_credentials_are_rejected_before_request(services: Services) -> None:
    async with http_client() as client:
        with pytest.raises(FetchError) as error:
            await get(client, "https://user:sensitive-value@docs.mistral.ai/a")
    assert not services.requests
    assert "sensitive-value" not in str(error.value)
