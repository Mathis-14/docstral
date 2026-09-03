"""Persist autonomous raw snapshots and validate their manifest.

Naming, raw-file integrity, atomic promotion, and report rendering stay together
because they enforce one on-disk snapshot contract.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from docstral_ingestion import IngestionError
from docstral_ingestion.crawl import (
    CachedPage,
    CrawlCounts,
    CrawlEntry,
    CrawlResult,
    PageCache,
    PageDecision,
)
from docstral_ingestion.sitemap import SitemapSnapshot

MANIFEST_FILE = "manifest.json"
REPORT_FILE = "report.md"
CURRENT_FILE = "current"
_SUCCESSFUL_SNAPSHOT_NAME = re.compile(r"\d{8}T\d{6}Z")


class SnapshotError(IngestionError):
    """Base error for snapshot persistence."""


class SnapshotReadError(SnapshotError):
    def __init__(self, path: Path, detail: str) -> None:
        self.path = path
        super().__init__(f"Cannot read snapshot {str(path)!r}: {detail}")


class SnapshotWriteError(SnapshotError):
    def __init__(self, path: Path, detail: str) -> None:
        self.path = path
        super().__init__(f"Cannot write snapshot {str(path)!r}: {detail}")


class SnapshotCollisionError(SnapshotWriteError):
    """Raised when a snapshot or raw-page path would collide."""


class SnapshotManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    crawled_at: datetime
    sitemap_url: str
    sitemap_sha256: str
    counts: CrawlCounts
    pages: tuple[CrawlEntry, ...]

    @model_validator(mode="after")
    def validate_invariants(self) -> SnapshotManifest:
        urls = [page.canonical_url for page in self.pages]
        if urls != sorted(urls) or len(urls) != len(set(urls)):
            raise ValueError("pages must have distinct canonical URLs in sorted order")
        _validate_counts(self.pages, self.counts)
        _validate_page_states(self.pages)
        return self


class _CurrentSnapshot:
    def __init__(self, directory: Path, manifest: SnapshotManifest) -> None:
        self._directory = directory
        self._pages = {
            page.canonical_url: page
            for page in manifest.pages
            if page.decision is PageDecision.STORED
        }

    def get(self, canonical_url: str) -> CachedPage | None:
        page = self._pages.get(canonical_url)
        if page is None:
            return None
        if page.raw_sha256 is None:
            raise SnapshotReadError(
                self._directory / MANIFEST_FILE,
                f"invalid raw metadata for {canonical_url!r}",
            )
        path = self._directory / _raw_path(canonical_url)
        try:
            body = path.read_bytes()
        except OSError as exc:
            raise SnapshotReadError(path, str(exc)) from exc
        return CachedPage(
            etag=page.etag,
            raw_sha256=page.raw_sha256,
            body=body,
        )


def load_current_snapshot(out: Path) -> PageCache | None:
    """Load the snapshot named by ``current`` without creating directories."""
    pointer = out / CURRENT_FILE
    if not pointer.exists():
        if pointer.is_symlink():
            raise SnapshotReadError(pointer, "broken symbolic link")
        return None
    try:
        name = pointer.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise SnapshotReadError(pointer, str(exc)) from exc
    if _SUCCESSFUL_SNAPSHOT_NAME.fullmatch(name) is None:
        raise SnapshotReadError(pointer, "must name one successful snapshot directory")

    directory = out / name
    manifest_path = directory / MANIFEST_FILE
    try:
        manifest = SnapshotManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SnapshotReadError(manifest_path, str(exc)) from exc
    return _CurrentSnapshot(directory, manifest)


def write_snapshot(
    out: Path,
    crawled_at: datetime,
    sitemap: SitemapSnapshot,
    result: CrawlResult,
) -> Path:
    """Write and promote a complete or failed autonomous snapshot."""
    destination = out / _snapshot_name(crawled_at, result.complete)
    if destination.exists():
        raise SnapshotCollisionError(destination, "destination already exists")
    try:
        out.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".snapshot-", dir=out) as temporary:
            temporary_path = Path(temporary)
            _write_raw_pages(temporary_path, result.pages)
            manifest = SnapshotManifest(
                crawled_at=crawled_at,
                sitemap_url=sitemap.url,
                sitemap_sha256=sitemap.sha256,
                counts=result.counts,
                pages=result.pages,
            )
            (temporary_path / MANIFEST_FILE).write_text(
                f"{manifest.model_dump_json(indent=2)}\n", encoding="utf-8"
            )
            (temporary_path / REPORT_FILE).write_text(
                _render_report(manifest, result), encoding="utf-8"
            )
            temporary_path.rename(destination)
        if result.complete:
            _replace_current(out, destination.name)
    except SnapshotError:
        raise
    except (OSError, ValueError) as exc:
        raise SnapshotWriteError(destination, str(exc)) from exc
    return destination


def _write_raw_pages(directory: Path, pages: tuple[CrawlEntry, ...]) -> None:
    raw_directory = directory / "raw"
    raw_directory.mkdir()
    paths: dict[str, str] = {}
    for page in sorted(pages, key=lambda item: item.canonical_url):
        if page.decision is not PageDecision.STORED:
            continue
        if page.body is None or page.raw_sha256 is None:
            raise SnapshotWriteError(
                directory, f"missing raw body for {page.canonical_url!r}"
            )
        if sha256(page.body).hexdigest() != page.raw_sha256:
            raise SnapshotWriteError(
                directory, f"raw SHA-256 mismatch for {page.canonical_url!r}"
            )
        raw_path = _raw_path(page.canonical_url)
        previous_url = paths.setdefault(raw_path, page.canonical_url)
        if previous_url != page.canonical_url:
            raise SnapshotCollisionError(
                directory / raw_path,
                f"slug collision between {previous_url!r} and {page.canonical_url!r}",
            )
        (directory / raw_path).write_bytes(page.body)


def _raw_path(canonical_url: str) -> str:
    path = urlsplit(canonical_url).path.strip("/")
    slug = path.replace("/", "__") if path else "index"
    return f"raw/{slug}.html"


def _snapshot_name(crawled_at: datetime, complete: bool) -> str:
    if crawled_at.tzinfo is None:
        raise ValueError("crawled_at must be timezone-aware")
    name = crawled_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return name if complete else f"{name}-failed"


def _replace_current(out: Path, snapshot_name: str) -> None:
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".current-", dir=out, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(f"{snapshot_name}\n")
        os.replace(temporary_path, out / CURRENT_FILE)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise SnapshotWriteError(out / CURRENT_FILE, str(exc)) from exc


def _validate_counts(pages: tuple[CrawlEntry, ...], counts: CrawlCounts) -> None:
    decisions = Counter(page.decision for page in pages)
    if (
        decisions[PageDecision.STORED] != counts.stored
        or decisions[PageDecision.REJECTED] != counts.rejected
        or decisions[PageDecision.FAILED] != counts.failed
    ):
        raise ValueError("page decisions do not match counts")
    rejections = Counter(page.reason for page in pages if page.reason is not None)
    if dict(rejections) != counts.rejections:
        raise ValueError("page rejection reasons do not match counts")


def _validate_page_states(pages: tuple[CrawlEntry, ...]) -> None:
    raw_paths: set[str] = set()
    for page in pages:
        if page.decision is PageDecision.STORED:
            if page.raw_sha256 is None:
                raise ValueError(
                    f"stored page {page.canonical_url!r} lacks raw metadata"
                )
            raw_path = _raw_path(page.canonical_url)
            if raw_path in raw_paths:
                raise ValueError(f"duplicate raw path {raw_path!r}")
            raw_paths.add(raw_path)
        elif page.decision is PageDecision.REJECTED and page.reason is None:
            raise ValueError(f"rejected page {page.canonical_url!r} lacks a reason")
        elif page.decision is PageDecision.FAILED and (
            page.error_type is None or page.error_message is None
        ):
            raise ValueError(f"failed page {page.canonical_url!r} lacks error context")


def _render_report(manifest: SnapshotManifest, result: CrawlResult) -> str:
    status = "complete" if result.complete else "failed"
    counts = manifest.counts
    lines = [
        "# Crawl report",
        "",
        f"- Status: {status}",
        f"- Crawled at: {manifest.crawled_at.isoformat()}",
        f"- Sitemap URLs: {counts.sitemap_english + counts.sitemap_french}",
        f"- Pages admitted: {counts.admitted}",
        f"- Pages stored: {counts.stored}",
        f"- Pages discovered by links: {counts.discovered_by_link}",
        f"- HTTP 200: {counts.status_200}",
        f"- HTTP 304: {counts.status_304}",
        f"- Redirects: {counts.redirects}",
        f"- External links: {counts.external_links}",
        f"- Malformed links: {counts.malformed_links}",
        f"- Failures: {counts.failed}",
        f"- Duration: {result.duration_seconds:.3f} s",
        "",
        "## Rejections",
        "",
        "| Reason | Count |",
        "| --- | ---: |",
    ]
    lines.extend(
        f"| {reason.value} | {count} |"
        for reason, count in sorted(
            counts.rejections.items(), key=lambda item: item[0].value
        )
    )
    lines.extend(_failure_report(manifest.pages))
    return "\n".join(lines) + "\n"


def _failure_report(pages: tuple[CrawlEntry, ...]) -> list[str]:
    lines = ["", "## Failures", ""]
    failures = [page for page in pages if page.decision is PageDecision.FAILED]
    if not failures:
        lines.append("None.")
    else:
        lines.extend(["| URL | Error | Message |", "| --- | --- | --- |"])
        lines.extend(
            f"| {_cell(page.canonical_url)} | {_cell(page.error_type)} | "
            f"{_cell(page.error_message)} |"
            for page in failures
        )
    return lines


def _cell(value: str | None) -> str:
    return (value or "").replace("|", "\\|").replace("\n", " ")
