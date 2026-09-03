import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from docstral_worker.crawl import (
    CrawlCounts,
    CrawlEntry,
    CrawlResult,
    DiscoveryVia,
    PageDecision,
)
from docstral_worker.sitemap import SitemapSnapshot
from docstral_worker.snapshot import (
    SnapshotCollisionError,
    SnapshotManifest,
    SnapshotReadError,
    current_snapshot,
    page_slug,
    write_snapshot,
)
from docstral_worker.urls import RejectionReason

DOCS = "https://docs.mistral.ai"
CRAWLED_AT = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def stored(path: str, body: bytes) -> CrawlEntry:
    url = f"{DOCS}{path}"
    return CrawlEntry(
        canonical_url=url,
        requested_url=url,
        final_url=url,
        discovered_via=DiscoveryVia.SITEMAP,
        decision=PageDecision.STORED,
        status_code=200,
        etag='"v1"',
        raw_sha256=sha256(body).hexdigest(),
        body=body,
    )


def failed(path: str) -> CrawlEntry:
    url = f"{DOCS}{path}"
    return CrawlEntry(
        canonical_url=url,
        requested_url=url,
        discovered_via=DiscoveryVia.LINK,
        decision=PageDecision.FAILED,
        error_type="FetchError",
        error_message=f"Fetch {url!r}: network exhausted",
    )


def result(*pages: CrawlEntry, complete: bool = True) -> CrawlResult:
    stored_count = sum(page.decision is PageDecision.STORED for page in pages)
    rejected_count = sum(page.decision is PageDecision.REJECTED for page in pages)
    failed_count = sum(page.decision is PageDecision.FAILED for page in pages)
    rejections = {
        reason: sum(page.reason is reason for page in pages)
        for reason in RejectionReason
        if any(page.reason is reason for page in pages)
    }
    return CrawlResult(
        pages=tuple(sorted(pages, key=lambda page: page.canonical_url)),
        counts=CrawlCounts(
            sitemap_english=len(pages),
            sitemap_french=0,
            discovered_by_link=failed_count,
            admitted=len(pages),
            stored=stored_count,
            rejected=rejected_count,
            failed=failed_count,
            status_200=stored_count,
            status_304=0,
            redirects=0,
            external_links=0,
            malformed_links=0,
            rejections=rejections,
        ),
        complete=complete,
        duration_seconds=1.25,
    )


def sitemap() -> SitemapSnapshot:
    return SitemapSnapshot(
        url=f"{DOCS}/sitemap.xml",
        sha256="a" * 64,
        english_urls=(f"{DOCS}/",),
        french_urls=(),
    )


def test_writes_complete_autonomous_snapshot_and_loads_cache(tmp_path: Path) -> None:
    root = stored("/", b"<main>Root</main>")
    guide = stored("/guide/start", b"<main>Guide</main>")

    destination = write_snapshot(tmp_path, CRAWLED_AT, sitemap(), result(guide, root))

    assert destination.name == "20260903T120000Z"
    assert (tmp_path / "current").read_text() == f"{destination.name}\n"
    assert (destination / "raw" / "index.html").read_bytes() == root.body
    assert (destination / "raw" / "guide__start.html").read_bytes() == guide.body
    payload = json.loads((destination / "manifest.json").read_text())
    assert list(payload) == [
        "crawled_at",
        "sitemap_url",
        "sitemap_sha256",
        "counts",
        "pages",
    ]
    assert [page["canonical_url"] for page in payload["pages"]] == [
        f"{DOCS}/",
        f"{DOCS}/guide/start",
    ]
    assert "body" not in payload["pages"][0]
    assert SnapshotManifest.model_validate(payload).counts.stored == 2
    current = current_snapshot(tmp_path)
    assert current is not None
    assert current.directory == destination
    assert current.manifest.counts.stored == 2
    assert page_slug(f"{DOCS}/guide/start") == "guide__start"
    report = (destination / "report.md").read_text()
    assert "- Status: complete" in report
    assert "- Pages stored: 2" in report
    assert "- External links: 0" in report
    assert "- Malformed links: 0" in report

    cached = current.get(f"{DOCS}/guide/start")
    assert cached is not None
    assert cached.body == guide.body
    assert cached.raw_sha256 == guide.raw_sha256
    assert not list(tmp_path.glob(".snapshot-*"))
    assert not list(tmp_path.glob(".current-*"))


def test_missing_current_does_not_create_output_directory(tmp_path: Path) -> None:
    out = tmp_path / "snapshots"

    assert current_snapshot(out) is None
    assert not out.exists()


def test_current_rejects_a_path_outside_the_snapshot_directory(tmp_path: Path) -> None:
    (tmp_path / "current").write_text("..\n")

    with pytest.raises(SnapshotReadError, match="successful snapshot directory"):
        current_snapshot(tmp_path)


def test_failed_snapshot_preserves_current_and_previous_snapshot(
    tmp_path: Path,
) -> None:
    previous = write_snapshot(
        tmp_path,
        CRAWLED_AT,
        sitemap(),
        result(stored("/", b"<main>Previous</main>")),
    )
    previous_manifest = (previous / "manifest.json").read_bytes()

    destination = write_snapshot(
        tmp_path,
        CRAWLED_AT + timedelta(seconds=1),
        sitemap(),
        result(
            stored("/partial", b"<main>Partial</main>"),
            failed("/broken"),
            complete=False,
        ),
    )

    assert destination.name == "20260903T120001Z-failed"
    assert (tmp_path / "current").read_text() == f"{previous.name}\n"
    assert (previous / "manifest.json").read_bytes() == previous_manifest
    assert (destination / "raw" / "partial.html").exists()
    report = (destination / "report.md").read_text()
    assert "- Status: failed" in report
    assert "FetchError" in report
    assert f"{DOCS}/broken" in report


def test_slug_collision_fails_without_promotion(tmp_path: Path) -> None:
    crawl_result = result(
        stored("/", b"<main>Root</main>"),
        stored("/index", b"<main>Index</main>"),
    )

    with pytest.raises(SnapshotCollisionError, match="slug collision"):
        write_snapshot(tmp_path, CRAWLED_AT, sitemap(), crawl_result)

    assert not (tmp_path / "current").exists()
    assert not (tmp_path / "20260903T120000Z").exists()
    assert not list(tmp_path.glob(".snapshot-*"))
