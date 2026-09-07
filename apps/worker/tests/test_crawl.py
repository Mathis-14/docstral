import httpx
import pytest
from docstral_worker.crawl import crawl
from worker_fixtures import DOCS, Services
from worker_fixtures import services as services


async def test_sitemap_seeds_and_links_share_one_canonical_queue(
    services: Services,
) -> None:
    services.pages["/a"] += b"""<a href="/en/b/?query=x#anchor">B</a>
        <a href="/b">Duplicate</a><a href="/fr/a">French</a>
        <a href="https://example.com/a">External</a><a href="/asset.png">Asset</a>"""
    result = await crawl((DOCS + "/a", DOCS + "/b"), delay=0, follow_links=True)
    assert result.complete
    assert {page.url for page in result.pages} == {DOCS + "/a", DOCS + "/b"}
    assert result.counts.stored == 2
    assert sum(request.url.path == "/b" for request in services.requests) == 1


async def test_page_limit_marks_capture_incomplete(services: Services) -> None:
    result = await crawl(
        (DOCS + "/a", DOCS + "/b"), delay=0, follow_links=True, max_pages=1
    )
    assert not result.complete
    assert result.counts.stored == 1


async def test_aliases_fetch_the_destination_once(services: Services) -> None:
    services.redirects = {"/alias": "/a"}
    result = await crawl((DOCS + "/alias", DOCS + "/a"), delay=0, follow_links=True)
    assert result.complete
    assert result.counts.stored == 1
    assert sum(request.url.path == "/a" for request in services.requests) == 1


async def test_redirect_cycle_is_not_a_complete_capture(services: Services) -> None:
    services.redirects = {"/a": "/b", "/b": "/a"}
    result = await crawl((DOCS + "/a",), delay=0, follow_links=True)
    assert not result.complete
    assert any(page.reason == "redirect cycle" for page in result.pages)


@pytest.mark.parametrize(
    "content_type", ["text/html; charset=utf-8", "application/xhtml+xml"]
)
async def test_html_and_xhtml_return_original_bytes_and_links(
    services: Services, content_type: str
) -> None:
    body = b'<html><a href="/b">B</a></html>'
    services.responses["/a"] = httpx.Response(
        200, content=body, headers={"content-type": content_type}
    )
    result = await crawl((DOCS + "/a",), delay=0)
    assert result.pages[0].body == body
    assert result.pages[0].links == (DOCS + "/b",)


@pytest.mark.parametrize(
    ("status", "expected"),
    [(404, "gone"), (410, "gone"), (403, "failed"), (503, "failed")],
)
async def test_http_results_keep_disappearance_distinct_from_failure(
    services: Services, status: int, expected: str
) -> None:
    services.responses["/a"] = httpx.Response(status)
    result = await crawl((DOCS + "/a",), delay=0)
    assert result.pages[0].status == expected
    assert result.complete == (expected == "gone")


async def test_non_html_is_excluded(services: Services) -> None:
    services.responses["/a"] = httpx.Response(
        200, headers={"content-type": "application/json"}, json={}
    )
    result = await crawl((DOCS + "/a",), delay=0)
    assert result.pages[0].reason == "non_html"
