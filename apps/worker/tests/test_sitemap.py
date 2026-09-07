import httpx
import pytest
from docstral_worker.fetch import FetchError
from docstral_worker.sitemap import SitemapParseError, fetch_sitemap, parse_sitemap
from worker_fixtures import DOCS, Services
from worker_fixtures import services as services


def test_sitemap_filters_and_deduplicates_documentation_urls() -> None:
    paths = ["/a", "/en/a/", "/b", "/fr/a", "/api", "/asset.png"]
    xml = (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<url><loc>{DOCS}{path}</loc></url>" for path in paths)
        + "</urlset>"
    )
    assert parse_sitemap(xml.encode()) == (DOCS + "/a", DOCS + "/b")


@pytest.mark.parametrize(
    "xml",
    [
        b"broken",
        b"<sitemapindex/>",
        b"<urlset/>",
        b"<urlset><url/></urlset>",
        b"<urlset><url><loc>/relative</loc></url></urlset>",
        b"<urlset><url><loc> </loc></url></urlset>",
    ],
)
def test_incomplete_sitemap_fails_explicitly(xml: bytes) -> None:
    with pytest.raises(SitemapParseError):
        parse_sitemap(xml)


async def test_sitemap_uses_shared_http_client(services: Services) -> None:
    services.seeds = ["/a", "/b"]
    assert await fetch_sitemap(delay=0) == (DOCS + "/a", DOCS + "/b")


async def test_failed_sitemap_never_becomes_empty_discovery(services: Services) -> None:
    services.responses["/sitemap.xml"] = httpx.Response(503)
    with pytest.raises(FetchError, match="HTTP 503"):
        await fetch_sitemap(delay=0)


async def test_redirected_sitemap_discovers_pages(services: Services) -> None:
    services.responses["/sitemap.xml"] = httpx.Response(
        301, headers={"location": "/redirected/sitemap.xml"}
    )
    services.responses["/redirected/sitemap.xml"] = httpx.Response(
        200, text=f"<urlset><url><loc>{DOCS}/a</loc></url></urlset>"
    )
    assert await fetch_sitemap(delay=0) == (DOCS + "/a",)
    assert [request.url.path for request in services.requests] == [
        "/robots.txt",
        "/sitemap.xml",
        "/redirected/sitemap.xml",
    ]


async def test_sitemap_redirect_cannot_bypass_robots(services: Services) -> None:
    services.responses["/robots.txt"] = httpx.Response(
        200, text="User-agent: *\nDisallow: /private\n"
    )
    services.responses["/sitemap.xml"] = httpx.Response(
        301, headers={"location": "/private/sitemap.xml"}
    )
    with pytest.raises(FetchError, match="robots_disallowed"):
        await fetch_sitemap(delay=0)
    assert [request.url.path for request in services.requests] == [
        "/robots.txt",
        "/sitemap.xml",
    ]


@pytest.mark.parametrize(
    "location", ["https://example.com/sitemap.xml", "/sitemap.xml", None]
)
async def test_invalid_sitemap_redirect_fails_before_destination_fetch(
    services: Services, location: str | None
) -> None:
    services.responses["/sitemap.xml"] = httpx.Response(
        301, headers={"location": location} if location else {}
    )
    with pytest.raises(FetchError):
        await fetch_sitemap(delay=0)
    assert [request.url.path for request in services.requests] == [
        "/robots.txt",
        "/sitemap.xml",
    ]
