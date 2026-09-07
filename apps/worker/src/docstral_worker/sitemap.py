import asyncio
from urllib.parse import urlsplit
from xml.etree import ElementTree

from docstral_worker.fetch import FetchError, FetchHttpStatusError, get, http_client
from docstral_worker.robots import check_robots, load_robots, request_delay
from docstral_worker.urls import DOCS_HOST, admit, canonicalize

SITEMAP_URL = f"https://{DOCS_HOST}/sitemap.xml"


class SitemapParseError(FetchError):
    pass


async def fetch_sitemap(delay: float = 0.25) -> tuple[str, ...]:
    async with http_client() as client:
        robots = await load_robots(client)

        async def prepare(url: str) -> None:
            check_robots(robots, url)
            await asyncio.sleep(request_delay(robots, delay))

        response = await get(
            client, SITEMAP_URL, follow_redirects=True, before_request=prepare
        )
        if response.status_code != 200:
            raise FetchHttpStatusError(SITEMAP_URL, response.status_code)
        return parse_sitemap(await response.read(), SITEMAP_URL)


def parse_sitemap(payload: bytes, source_url: str = SITEMAP_URL) -> tuple[str, ...]:
    try:
        root = ElementTree.fromstring(payload)
        if root.tag.rsplit("}", 1)[-1] != "urlset":
            raise ValueError("expected a urlset sitemap")
        urls: dict[str, None] = {}
        for entry in root:
            locations = entry.findall("{*}loc")
            if (
                entry.tag.rsplit("}", 1)[-1] != "url"
                or len(locations) != 1
                or not locations[0].text
            ):
                raise ValueError("each sitemap entry must contain one loc")
            location = locations[0].text.strip()
            if (
                urlsplit(location).scheme not in ("http", "https")
                or not urlsplit(location).netloc
            ):
                raise ValueError("sitemap loc must be an absolute HTTP(S) URL")
            url = canonicalize(location, source_url)
            if admit(url).admitted:
                urls[url.url] = None
        if not urls:
            raise ValueError("no in-scope documentation URLs")
        return tuple(urls)
    except (ElementTree.ParseError, ValueError) as error:
        raise SitemapParseError(source_url, str(error)) from error
