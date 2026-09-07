from datetime import UTC, datetime
from pathlib import Path

import structlog
from pydantic import BaseModel, ConfigDict, Field

from docstral_worker.crawl import MAX_PAGES, CrawlResult, crawl
from docstral_worker.sitemap import fetch_sitemap
from docstral_worker.snapshot import write_snapshot


class CrawlConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    out: Path = Path("data/snapshots")
    delay: float = Field(default=0.25, ge=0, allow_inf_nan=False)
    max_pages: int = Field(default=MAX_PAGES, ge=1, le=MAX_PAGES)


async def crawl_snapshot(config: CrawlConfig) -> CrawlResult:
    urls = await fetch_sitemap(config.delay)
    result = await crawl(
        urls,
        delay=config.delay,
        retries=2,
        follow_links=True,
        max_pages=config.max_pages,
    )
    destination = write_snapshot(config.out, datetime.now(UTC), result)
    structlog.get_logger(__name__).info(
        "crawl_finished",
        snapshot=str(destination) if destination else None,
        complete=result.complete,
        limit_reached=not result.complete and result.counts.failed == 0,
        **result.counts.model_dump(),
        errors={
            page.url: page.reason for page in result.pages if page.status == "failed"
        },
    )
    return result
