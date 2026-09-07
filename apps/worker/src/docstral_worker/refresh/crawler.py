from docstral_worker.crawl import crawl
from docstral_worker.fetch import FetchError
from docstral_worker.refresh.config import RefreshConfig
from docstral_worker.refresh.models import DiscoveryResult, DownloadedPage, PageResult
from docstral_worker.sitemap import fetch_sitemap
from docstral_worker.urls import admit, canonicalize


async def discover(config: RefreshConfig) -> DiscoveryResult:
    return DiscoveryResult(
        urls=await fetch_sitemap(config.request_delay),
        concurrency=config.concurrency,
        max_pages=config.max_pages,
        request_delay=config.request_delay,
    )


async def download(url: str, config: RefreshConfig) -> DownloadedPage | PageResult:
    target = canonicalize(url, url)
    selection = admit(target)
    if not selection.admitted:
        return PageResult(url=target.url, status="excluded", reason=selection.reason)
    result = await crawl((target.url,), delay=config.request_delay, max_pages=1)
    if not result.pages:
        raise FetchError(target.url, "crawler returned no page result")
    page = result.pages[0]
    if page.status == "downloaded":
        return DownloadedPage(url=page.url, html=page.body, links=page.links)
    if page.status == "failed":
        if page.reason and "robots_disallowed" in page.reason:
            return PageResult(
                url=page.url, status="excluded", reason="robots_disallowed"
            )
        raise FetchError(
            page.url,
            page.reason or "download failed",
            status_code=page.status_code,
            transient=page.transient,
        )
    return PageResult(
        url=page.url,
        status=page.status,
        reason=page.reason,
        redirect_url=page.redirect_url,
    )
