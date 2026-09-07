import asyncio
from time import monotonic
from typing import Literal
from urllib.parse import urljoin
from uuid import uuid4

from crawlee import ConcurrencySettings
from crawlee.configuration import Configuration
from crawlee.crawlers import BasicCrawlingContext, HttpCrawler, HttpCrawlingContext
from crawlee.crawlers._beautifulsoup._beautifulsoup_parser import BeautifulSoupParser
from crawlee.events import LocalEventManager
from crawlee.storage_clients import MemoryStorageClient
from crawlee.storages import RequestQueue
from pydantic import BaseModel, ConfigDict, Field

from docstral_worker import IngestionError
from docstral_worker.fetch import (
    REDIRECT_STATUSES,
    TIMEOUT,
    FetchError,
    FetchHttpStatusError,
    get,
    http_client,
    is_transient,
    request,
)
from docstral_worker.robots import check_robots, load_robots, request_delay
from docstral_worker.urls import (
    UrlCanonicalizationError,
    admit,
    canonicalize,
    is_docs_url,
)

MAX_PAGES = 2_000
SHA256_PATTERN = r"^[0-9a-f]{64}$"


class CrawlEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    status: Literal["downloaded", "redirected", "gone", "excluded", "failed"]
    body: bytes = Field(default=b"", exclude=True, repr=False)
    links: tuple[str, ...] = ()
    redirect_url: str | None = None
    reason: str | None = None
    status_code: int | None = None
    transient: bool = False


class CrawlCounts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stored: int = Field(default=0, ge=0)
    rejected: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)


class CrawlResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pages: tuple[CrawlEntry, ...]
    counts: CrawlCounts
    complete: bool
    duration_seconds: float = Field(ge=0)


def admitted_links(links: list[str], base: str) -> tuple[str, ...]:
    urls: dict[str, None] = {}
    for link in links:
        try:
            target = canonicalize(link, base)
            if admit(target).admitted:
                urls[target.url] = None
        except UrlCanonicalizationError:
            continue
    return tuple(urls)


async def crawl(
    urls: tuple[str, ...],
    *,
    delay: float = 0.25,
    retries: int = 0,
    follow_links: bool = False,
    max_pages: int = MAX_PAGES,
) -> CrawlResult:
    if not urls or not 1 <= max_pages <= MAX_PAGES:
        raise IngestionError("Crawl requires seeds and a valid page limit")
    started = monotonic()
    pages: dict[str, CrawlEntry] = {}
    parser = BeautifulSoupParser("html.parser")
    async with http_client() as client:
        robots = await load_robots(client)
        interval = request_delay(robots, delay)
        storage = MemoryStorageClient()
        configuration = Configuration()
        # Crawlee caches named queues globally, even across memory clients.
        queue = await RequestQueue.open(
            name=uuid4().hex, storage_client=storage, configuration=configuration
        )
        crawler = HttpCrawler(
            http_client=client,
            configuration=configuration,
            request_manager=queue,
            event_manager=LocalEventManager(),
            storage_client=storage,
            concurrency_settings=ConcurrencySettings(
                max_concurrency=1, desired_concurrency=1
            ),
            request_handler_timeout=TIMEOUT,
            max_request_retries=retries,
            max_requests_per_crawl=max_pages,
            use_session_pool=False,
            retry_on_blocked=False,
            configure_logging=False,
            ignore_http_error_status_codes=range(400, 600),
        )

        @crawler.pre_navigation_hook
        async def prepare(context: BasicCrawlingContext) -> None:
            try:
                check_robots(robots, context.request.url)
            except FetchError:
                context.request.no_retry = True
                raise
            await asyncio.sleep(interval)

        @crawler.router.default_handler
        async def handle(context: HttpCrawlingContext) -> None:
            url = context.request.url
            response = context.http_response
            current = url
            redirects = {url}
            try:
                while response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError(url, "redirect has no Location header")
                    destination = urljoin(current, location)
                    if not is_docs_url(destination):
                        pages[url] = CrawlEntry(
                            url=url, status="excluded", reason="outside_host"
                        )
                        return
                    target = canonicalize(destination, current)
                    selection = admit(target)
                    if not selection.admitted:
                        pages[url] = CrawlEntry(
                            url=url, status="excluded", reason=selection.reason
                        )
                        return
                    if target.url != url:
                        pages[url] = CrawlEntry(
                            url=url, status="redirected", redirect_url=target.url
                        )
                        if follow_links:
                            await context.add_requests([request(target.url)])
                        return
                    if destination in redirects or len(redirects) >= 20:
                        raise FetchError(url, "redirect cycle or limit reached")
                    redirects.add(destination)
                    check_robots(robots, destination)
                    await asyncio.sleep(interval)
                    response = await get(client, destination)
                    current = destination
                status = response.status_code
                if status in (404, 410):
                    pages[url] = CrawlEntry(url=url, status="gone")
                    return
                if status != 200:
                    raise FetchHttpStatusError(url, status)
                content_type = (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if content_type not in ("text/html", "application/xhtml+xml"):
                    pages[url] = CrawlEntry(
                        url=url, status="excluded", reason="non_html"
                    )
                    return
                soup = await parser.parse(response)
                links = admitted_links(
                    list(parser.find_links(soup, "a[href]", "href")), current
                )
                pages[url] = CrawlEntry(
                    url=url,
                    status="downloaded",
                    body=await response.read(),
                    links=links,
                )
                if follow_links:
                    await context.add_requests([request(link) for link in links])
            except Exception as error:
                context.request.no_retry = not is_transient(error)
                raise

        @crawler.failed_request_handler
        async def failed(context: BasicCrawlingContext, error: Exception) -> None:
            pages[context.request.url] = CrawlEntry(
                url=context.request.url,
                status="failed",
                reason=str(error),
                status_code=getattr(error, "status_code", None),
                transient=is_transient(error),
            )

        try:
            await crawler.run([request(url) for url in urls])
            finished = await queue.is_finished()
        finally:
            await queue.drop()
    for page in tuple(pages.values()):
        destination = page.redirect_url
        seen = {page.url}
        while destination in pages and destination is not None:
            if destination in seen:
                pages[page.url] = CrawlEntry(
                    url=page.url, status="failed", reason="redirect cycle"
                )
                break
            seen.add(destination)
            destination = pages[destination].redirect_url
    counts = CrawlCounts(
        stored=sum(p.status == "downloaded" for p in pages.values()),
        rejected=sum(
            p.status in ("redirected", "excluded", "gone") for p in pages.values()
        ),
        failed=sum(p.status == "failed" for p in pages.values()),
    )
    return CrawlResult(
        pages=tuple(pages.values()),
        counts=counts,
        complete=finished and not counts.failed,
        duration_seconds=monotonic() - started,
    )
