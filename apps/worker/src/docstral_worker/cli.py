from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections.abc import Sequence
from math import isfinite
from pathlib import Path
from urllib.parse import urlsplit

import structlog
from mistralai.search.toolkit.clients.mistral import build_mistral_client
from mistralai.search.toolkit.embedding import (
    MODEL_1024_EMBEDDING,
    MistralEmbedder,
)
from mistralai.search.toolkit.errors import SearchToolkitException
from pydantic import BaseModel, ConfigDict, ValidationError

from docstral_worker import IngestionError
from docstral_worker.crawl import MAX_PAGES
from docstral_worker.crawl_run import CrawlConfig, crawl_snapshot
from docstral_worker.extract import ExtractionError, extract_snapshot
from docstral_worker.ingest import IngestResult, ingest_snapshot
from docstral_worker.snapshot import current_snapshot

DEFAULT_SNAPSHOTS = Path("data/snapshots")
DEFAULT_EXTRACTED = Path("data/extracted")
DEFAULT_VESPA_ENDPOINT = "http://localhost:8080"


class ExtractConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshots: Path = DEFAULT_SNAPSHOTS
    out: Path = DEFAULT_EXTRACTED


class IngestConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshots: Path = DEFAULT_SNAPSHOTS
    vespa_endpoint: str = DEFAULT_VESPA_ENDPOINT


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    _configure_logging()
    if args.command == "workflows":
        os.environ.setdefault("LOG_FORMAT", "json")
        # SDK settings load on import; its default trace filter can expose errors.
        os.environ["OTEL_REDACTION"] = "strict"
        from docstral_worker.refresh.worker import run_worker

        try:
            asyncio.run(run_worker())
        except ValidationError as exc:
            parser.error(str(exc.errors(include_input=False, include_url=False)))
        except IngestionError as exc:
            structlog.get_logger(__name__).error(
                "workflows_failed", error_message=str(exc)
            )
            return 1
        return 0
    if args.command == "crawl":
        return _run_crawl(
            CrawlConfig(out=args.out, delay=args.delay, max_pages=args.max_pages)
        )
    if args.command == "extract":
        return _run_extract(ExtractConfig(snapshots=args.snapshots, out=args.out))
    if args.command == "ingest":
        return _run_ingest(
            IngestConfig(
                snapshots=args.snapshots,
                vespa_endpoint=args.vespa_endpoint,
            )
        )
    raise AssertionError("argparse accepted an unknown command")


def _run_crawl(config: CrawlConfig) -> int:
    logger = structlog.get_logger(__name__)
    try:
        result = asyncio.run(crawl_snapshot(config))
    except IngestionError as exc:
        logger.error(
            "crawl_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return 1
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


def _run_ingest(config: IngestConfig) -> int:
    logger = structlog.get_logger(__name__)
    try:
        snapshot = current_snapshot(config.snapshots)
        if snapshot is None:
            raise IngestionError(f"No current snapshot under {str(config.snapshots)!r}")
        from mistralai.search.toolkit.plugins.vespa import (
            VespaClient,
            VespaClientConfig,
        )

        from docstral_worker.refresh.corpus import VespaCorpus
        from docstral_worker.refresh.indexing import PageIndexer

        async def ingest() -> IngestResult:
            client = VespaClient(
                VespaClientConfig(endpoint=config.vespa_endpoint, timeout=30)
            )
            try:
                with build_mistral_client() as mistral:
                    async with mistral:
                        return await ingest_snapshot(
                            snapshot,
                            PageIndexer(
                                VespaCorpus(client),
                                MistralEmbedder(
                                    client=mistral,
                                    model_name=MODEL_1024_EMBEDDING,
                                    max_retry=3,
                                ),
                            ),
                        )
            except RuntimeError as error:
                raise IngestionError(str(error)) from error
            finally:
                await client.aclose()

        result = asyncio.run(ingest())
    except (IngestionError, SearchToolkitException) as exc:
        logger.error(
            "ingest_failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return 1
    logger.info(
        "ingest_finished",
        snapshot=snapshot.directory.name,
        indexed=result.indexed,
        failed=result.failed,
        duration_seconds=round(result.duration_seconds, 3),
    )
    return 1 if result.failed else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="docstral-worker")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("workflows", help="run the Mistral Workflows ingestion worker")
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
    ingest_parser = commands.add_parser(
        "ingest",
        help="index the current raw snapshot in Vespa",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ingest_parser.add_argument(
        "--snapshots", type=Path, default=DEFAULT_SNAPSHOTS, help="snapshot root"
    )
    ingest_parser.add_argument(
        "--vespa-endpoint",
        type=_http_endpoint,
        default=DEFAULT_VESPA_ENDPOINT,
        help="Vespa query and document endpoint",
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


def _http_endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an absolute HTTP(S) URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError("must be an absolute HTTP(S) URL")
    return value


def _configure_logging() -> None:
    logging.getLogger("HttpCrawler").setLevel(logging.CRITICAL)
    logging.getLogger("crawlee").setLevel(logging.WARNING)
    structlog.reset_defaults()
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ]
    )
