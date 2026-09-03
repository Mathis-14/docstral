import argparse
from collections.abc import Sequence
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path

import structlog
from pydantic import BaseModel, ConfigDict, Field

from docstral_ingestion import IngestionError
from docstral_ingestion.crawl import MAX_PAGES, crawl
from docstral_ingestion.extract import ExtractionError, extract_snapshot
from docstral_ingestion.fetch import FetchConfig, HttpFetcher
from docstral_ingestion.sitemap import fetch_sitemap
from docstral_ingestion.snapshot import (
    current_snapshot,
    write_snapshot,
)

DEFAULT_SNAPSHOTS = Path("data/snapshots")
DEFAULT_EXTRACTED = Path("data/extracted")


class CrawlConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    out: Path = DEFAULT_SNAPSHOTS
    delay: float = Field(default=0.25, ge=0.0, allow_inf_nan=False)
    max_pages: int = Field(default=MAX_PAGES, ge=1, le=MAX_PAGES)


class ExtractConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshots: Path = DEFAULT_SNAPSHOTS
    out: Path = DEFAULT_EXTRACTED


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Docstral ingestion command line interface."""
    args = _parser().parse_args(argv)
    _configure_logging()
    if args.command == "crawl":
        return _run_crawl(
            CrawlConfig(out=args.out, delay=args.delay, max_pages=args.max_pages)
        )
    if args.command == "extract":
        return _run_extract(ExtractConfig(snapshots=args.snapshots, out=args.out))
    raise AssertionError("argparse accepted an unknown command")


def _run_crawl(config: CrawlConfig) -> int:
    logger = structlog.get_logger(__name__)
    try:
        cache = current_snapshot(config.out)
        with HttpFetcher(FetchConfig(delay_seconds=config.delay)) as fetcher:
            sitemap = fetch_sitemap(fetcher)
            crawled_at = datetime.now(UTC)
            result = crawl(
                fetcher,
                sitemap,
                cache,
                max_pages=config.max_pages,
            )
        destination = write_snapshot(config.out, crawled_at, sitemap, result)
    except IngestionError as exc:
        logger.error(
            "crawl_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return 1
    logger.info(
        "crawl_finished",
        snapshot=str(destination),
        complete=result.complete,
        stored=result.counts.stored,
        failed=result.counts.failed,
    )
    return 0 if result.complete else 1


def _run_extract(config: ExtractConfig) -> int:
    logger = structlog.get_logger(__name__)
    try:
        snapshot = current_snapshot(config.snapshots)
        if snapshot is None:
            raise ExtractionError(
                f"No current snapshot under {str(config.snapshots)!r}"
            )
        destination = config.out / snapshot.directory.name
        result = extract_snapshot(snapshot, destination)
    except IngestionError as exc:
        logger.error(
            "extract_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return 1
    logger.info(
        "extract_finished",
        snapshot=snapshot.directory.name,
        destination=str(destination),
        converted=result.converted,
        failed=result.failed,
        duration_seconds=round(result.duration_seconds, 3),
    )
    return 1 if result.failed else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="docstral-ingestion")
    commands = parser.add_subparsers(dest="command", required=True)
    crawl_parser = commands.add_parser(
        "crawl",
        help="crawl documentation into a raw snapshot",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    crawl_parser.add_argument(
        "--out", type=Path, default=DEFAULT_SNAPSHOTS, help="snapshot root"
    )
    crawl_parser.add_argument(
        "--delay",
        type=_non_negative_float,
        default=0.25,
        help="minimum delay between requests in seconds",
    )
    crawl_parser.add_argument(
        "--max-pages",
        type=_page_limit,
        default=MAX_PAGES,
        help="maximum number of pages to fetch",
    )
    extract_parser = commands.add_parser(
        "extract",
        help="convert the current raw snapshot to Markdown",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    extract_parser.add_argument(
        "--snapshots", type=Path, default=DEFAULT_SNAPSHOTS, help="snapshot root"
    )
    extract_parser.add_argument(
        "--out", type=Path, default=DEFAULT_EXTRACTED, help="extraction root"
    )
    return parser


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if not isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a finite number at least 0")
    return parsed


def _page_limit(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= MAX_PAGES:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_PAGES}")
    return parsed


def _configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ]
    )
