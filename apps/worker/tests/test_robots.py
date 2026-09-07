import httpx
import pytest
from docstral_worker.crawl import crawl
from docstral_worker.fetch import FetchError, http_client
from docstral_worker.robots import (
    RobotsDeniedError,
    check_robots,
    load_robots,
    request_delay,
)
from worker_fixtures import DOCS, Services
from worker_fixtures import services as services


async def test_robots_permissions_use_wildcards_and_longest_match(
    services: Services,
) -> None:
    services.responses["/robots.txt"] = httpx.Response(
        200,
        text="User-agent: *\nDisallow: /private*\nAllow: /private/public\nCrawl-delay: 2\n",
    )
    async with http_client() as client:
        policy = await load_robots(client)
    check_robots(policy, DOCS + "/private/public")
    with pytest.raises(RobotsDeniedError):
        check_robots(policy, DOCS + "/private/page")
    assert request_delay(policy, 0.25) == 2
    assert request_delay(policy, 3) == 3


@pytest.mark.parametrize("status", [429, 503])
async def test_unavailable_robots_stops_before_downloading_pages(
    services: Services, status: int
) -> None:
    services.robots_status = status
    with pytest.raises(FetchError, match=f"HTTP {status}"):
        await crawl((DOCS + "/a",), delay=0)
    assert [request.url.path for request in services.requests] == ["/robots.txt"]


async def test_missing_robots_allows_crawl(services: Services) -> None:
    services.robots_status = 404
    assert (await crawl((DOCS + "/a",), delay=0)).complete


async def test_robots_redirect_keeps_destination_permissions(
    services: Services,
) -> None:
    services.responses["/robots.txt"] = httpx.Response(
        301, headers={"location": "/redirected/robots.txt"}
    )
    services.responses["/redirected/robots.txt"] = httpx.Response(
        200, text="User-agent: *\nDisallow: /a\n"
    )
    result = await crawl((DOCS + "/a",), delay=0)
    assert not result.complete
    assert [request.url.path for request in services.requests] == [
        "/robots.txt",
        "/redirected/robots.txt",
    ]


async def test_fractional_crawl_delay_is_preserved(services: Services) -> None:
    services.responses["/robots.txt"] = httpx.Response(
        200, text="User-agent: *\nCrawl-delay: 0.75\n"
    )
    async with http_client() as client:
        policy = await load_robots(client)
    assert request_delay(policy, 0.25) == 0.75
    assert request_delay(policy, 1.5) == 1.5


async def test_disallowed_page_is_never_downloaded(services: Services) -> None:
    services.responses["/robots.txt"] = httpx.Response(
        200, text="User-agent: *\nDisallow: /a\n"
    )
    result = await crawl((DOCS + "/a",), delay=0)
    assert not result.complete
    assert [request.url.path for request in services.requests] == ["/robots.txt"]
