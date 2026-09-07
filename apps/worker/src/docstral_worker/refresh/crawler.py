from docstral_worker.crawl import extract_links
from docstral_worker.fetch import (
    REDIRECT_STATUSES,
    FetchConfig,
    FetchHttpStatusError,
    HttpFetcher,
)
from docstral_worker.refresh.config import RefreshConfig
from docstral_worker.refresh.models import DiscoveryResult, DownloadedPage, PageResult
from docstral_worker.robots import RobotsDeniedError
from docstral_worker.sitemap import SITEMAP_URL, SitemapParseError, fetch_sitemap
from docstral_worker.urls import admit, canonicalize


def discover(config: RefreshConfig) -> DiscoveryResult:
    with HttpFetcher(FetchConfig(delay_seconds=config.request_delay)) as fetcher:
        sitemap = fetch_sitemap(fetcher)
    urls = tuple(
        dict.fromkeys(
            target.url
            for url in sitemap.english_urls
            if admit(target := canonicalize(url, SITEMAP_URL)).admitted
        )
    )
    if not urls:
        raise SitemapParseError(SITEMAP_URL, "no in-scope documentation URLs")
    return DiscoveryResult(
        urls=urls,
        concurrency=config.concurrency,
        max_pages=config.max_pages,
        request_delay=config.request_delay,
    )


def download(url: str, config: RefreshConfig) -> DownloadedPage | PageResult:
    target = canonicalize(url, url)
    decision = admit(target)
    if not decision.admitted:
        return PageResult(url=target.url, status="excluded", reason=decision.reason)
    try:
        with HttpFetcher(
            FetchConfig(delay_seconds=config.request_delay), stop_at_new_page=True
        ) as fetcher:
            fetched = fetcher.fetch(target.url, etag=None)
    except RobotsDeniedError:
        return PageResult(url=target.url, status="excluded", reason="robots_disallowed")
    except FetchHttpStatusError as error:
        if error.status_code in (404, 410):
            return PageResult(url=target.url, status="gone")
        raise
    if fetched.status_code in REDIRECT_STATUSES:
        destination = canonicalize(fetched.final_url, target.url)
        decision = admit(destination)
        if not decision.admitted:
            return PageResult(url=target.url, status="excluded", reason=decision.reason)
        return PageResult(
            url=target.url, status="redirected", redirect_url=destination.url
        )
    if fetched.status_code != 200:
        raise FetchHttpStatusError(target.url, fetched.status_code)
    if fetched.content_type not in ("text/html", "application/xhtml+xml"):
        return PageResult(url=target.url, status="excluded", reason="non_html")
    links, _ = extract_links(fetched.body, fetched.final_url)
    return DownloadedPage(
        url=target.url,
        html=fetched.body,
        links=tuple(dict.fromkeys(link.url for link in links if admit(link).admitted)),
    )
