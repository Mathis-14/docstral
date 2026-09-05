"""Build a snapshot through the same crawl path for CLI and scheduled runs."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import structlog
from pydantic import BaseModel, ConfigDict, Field

from docstral_worker import IngestionError
from docstral_worker.crawl import MAX_PAGES, CrawlResult, crawl
from docstral_worker.fetch import FetchConfig, HttpFetcher
from docstral_worker.sitemap import fetch_sitemap
from docstral_worker.snapshot import current_snapshot, write_snapshot


class CrawlConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    out: Path = Path("data/snapshots")
    delay: float = Field(default=0.25, ge=0.0, allow_inf_nan=False)
    max_pages: int = Field(default=MAX_PAGES, ge=1, le=MAX_PAGES)


def crawl_snapshot(config: CrawlConfig) -> CrawlResult:
    cache = current_snapshot(config.out)
    with HttpFetcher(FetchConfig(delay_seconds=config.delay)) as fetcher:
        sitemap = fetch_sitemap(fetcher)
        crawled_at = datetime.now(UTC)
        result = crawl(fetcher, sitemap, cache, max_pages=config.max_pages)
    destination = write_snapshot(config.out, crawled_at, sitemap, result)
    structlog.get_logger(__name__).info(
        "crawl_finished",
        snapshot=str(destination),
        complete=result.complete,
        stored=result.counts.stored,
        failed=result.counts.failed,
    )
    return result


async def refresh_snapshot(root: Path) -> None:
    """Keep blocking fetches off the event loop, without abandoning snapshot writes."""
    task = asyncio.create_task(asyncio.to_thread(crawl_snapshot, CrawlConfig(out=root)))
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        # Threads cannot be cancelled: retain the publication lock until it finishes.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                pass
        raise
    if not result.complete:
        raise IngestionError("Crawl incomplete; current corpus was not replaced")
