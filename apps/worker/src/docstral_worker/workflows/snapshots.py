from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

import structlog

from docstral_worker import IngestionError
from docstral_worker.config import CrawlConfig
from docstral_worker.crawler.crawl import CrawlResult, crawl
from docstral_worker.crawler.sitemap import fetch_sitemap
from docstral_worker.extract import ExtractionError, extract_page
from docstral_worker.models import DownloadedPage, ExtractResult, IngestResult
from docstral_worker.snapshot import (
    CurrentSnapshot,
    SnapshotReadError,
    page_slug,
    write_snapshot,
)

if TYPE_CHECKING:
    from docstral_worker.indexing import PageIndexer


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


def extract_snapshot(snapshot: CurrentSnapshot, destination: Path) -> ExtractResult:
    if destination.exists():
        raise ExtractionError(f"Extraction output {str(destination)!r} already exists")
    logger = structlog.get_logger(__name__)
    started_at = monotonic()
    converted = 0
    failed = 0
    try:
        pages_directory = destination / "pages"
        pages_directory.mkdir(parents=True)
        for entry in snapshot.manifest.pages:
            page_started_at = monotonic()
            try:
                page = extract_page(entry.url, snapshot.get(entry.url))
                slug = page_slug(entry.url)
                (pages_directory / f"{slug}.md").write_text(
                    page.markdown, encoding="utf-8"
                )
            except IngestionError as exc:
                failed += 1
                logger.info(
                    "extraction_page",
                    url=entry.url,
                    decision="failed",
                    duration_ms=round((monotonic() - page_started_at) * 1_000, 3),
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                continue
            converted += 1
            logger.info(
                "extraction_page",
                url=entry.url,
                decision="converted",
                duration_ms=round((monotonic() - page_started_at) * 1_000, 3),
                chars=page.chars,
            )
    except OSError as exc:
        raise ExtractionError(
            f"Cannot write extraction output {str(destination)!r}: {exc}"
        ) from exc
    return ExtractResult(
        converted=converted,
        failed=failed,
        duration_seconds=monotonic() - started_at,
    )


async def ingest_snapshot(
    snapshot: CurrentSnapshot, indexer: PageIndexer
) -> IngestResult:
    started_at = monotonic()
    indexed = failed = 0
    logger = structlog.get_logger(__name__)
    for entry in snapshot.manifest.pages:
        try:
            html = snapshot.get(entry.url)
        except SnapshotReadError as error:
            failed += 1
            logger.error("ingestion_page", url=entry.url, error_message=str(error))
            continue
        result = await indexer.sync(DownloadedPage(url=entry.url, html=html, links=()))
        failed += result.status == "extraction_failed"
        indexed += result.status in ("indexed", "unchanged")
        logger.info("ingestion_page", url=entry.url, decision=result.status)
    return IngestResult(
        indexed=indexed, failed=failed, duration_seconds=monotonic() - started_at
    )
