from hashlib import sha256

import pytest
from docstral_worker import IngestionError
from docstral_worker.crawl import (
    MAX_PAGES,
    CachedPage,
    CrawlResult,
    DiscoveryVia,
    PageDecision,
    crawl,
)
from docstral_worker.fetch import (
    FetchError,
    FetchHttpStatusError,
    FetchResult,
    RetryAfterTooLongError,
)
from docstral_worker.robots import RobotsDeniedError, RobotsUnavailableError
from docstral_worker.sitemap import SitemapSnapshot
from docstral_worker.urls import RejectionReason

DOCS = "https://docs.mistral.ai"


class FakeFetcher:
    def __init__(self, outcomes: dict[str, FetchResult | Exception]) -> None:
        self.outcomes = outcomes
        self.requests: list[tuple[str, str | None]] = []

    def fetch(self, url: str, etag: str | None) -> FetchResult:
        self.requests.append((url, etag))
        outcome = self.outcomes[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeCache:
    def __init__(self, pages: dict[str, CachedPage]) -> None:
        self.pages = pages

    def get(self, canonical_url: str) -> CachedPage | None:
        return self.pages.get(canonical_url)


def fetched(
    path: str,
    body: bytes,
    *,
    status_code: int = 200,
    content_type: str | None = "text/html",
    final_url: str | None = None,
    etag: str | None = None,
) -> FetchResult:
    url = f"{DOCS}{path}"
    return FetchResult(
        requested_url=url,
        final_url=final_url or url,
        status_code=status_code,
        etag=etag,
        content_type=content_type,
        body=body,
    )


def sitemap(*paths: str, french: tuple[str, ...] = ()) -> SitemapSnapshot:
    return SitemapSnapshot(
        url=f"{DOCS}/sitemap.xml",
        sha256="0" * 64,
        english_urls=tuple(f"{DOCS}{path}" for path in paths),
        french_urls=tuple(f"{DOCS}{path}" for path in french),
    )


def by_url(result: CrawlResult) -> dict[str, PageDecision]:
    return {page.canonical_url: page.decision for page in result.pages}


def test_discovers_linked_pages_once_and_stops_on_empty_frontier() -> None:
    bodies = {
        "/seed": b"""<!doctype html><html><body>
            <a href="/linked">Linked page</a>
            <a href="/linked?source=nav#example">Linked page again</a>
            <a href="/fr/guide">French page</a>
            <a href="https://github.com/mistralai">GitHub</a>
            </body></html>""",
        "/linked": b"""<!doctype html><html><body>
            <a href="/cycle-a">Cycle A</a>
            <a href="/api">Excluded API</a>
            </body></html>""",
        "/cycle-a": b'<a href="/cycle-b">Cycle B</a>',
        "/cycle-b": b'<a href="/seed">Back to seed</a>',
    }
    fetcher = FakeFetcher(
        {f"{DOCS}{path}": fetched(path, body) for path, body in bodies.items()}
    )

    result = crawl(fetcher, sitemap("/seed", french=("/fr/start",)))

    assert [url for url, _ in fetcher.requests] == [f"{DOCS}{path}" for path in bodies]
    assert result.complete
    assert result.counts.admitted == 4
    assert result.counts.stored == 4
    assert result.counts.discovered_by_link == 5
    assert result.counts.external_links == 1
    assert result.counts.rejections == {
        RejectionReason.EXCLUDED_ROUTE: 1,
        RejectionReason.FRENCH: 2,
    }
    pages = {page.canonical_url: page for page in result.pages}
    assert pages[f"{DOCS}/linked"].discovered_via is DiscoveryVia.LINK
    assert pages[f"{DOCS}/cycle-b"].raw_sha256 == sha256(bodies["/cycle-b"]).hexdigest()


def test_sitemap_external_url_is_recorded_without_counting_an_external_link() -> None:
    inventory = sitemap("/seed").model_copy(
        update={"english_urls": (f"{DOCS}/seed", "https://example.com/docs")}
    )
    fetcher = FakeFetcher({f"{DOCS}/seed": fetched("/seed", b"<main>Seed</main>")})

    result = crawl(fetcher, inventory)

    assert result.counts.external_links == 0
    assert result.counts.rejections == {RejectionReason.OUTSIDE_HOST: 1}
    assert by_url(result)["https://example.com/docs"] is PageDecision.REJECTED


def test_alias_redirecting_to_a_stored_page_is_recorded_as_duplicate() -> None:
    fetcher = FakeFetcher(
        {
            f"{DOCS}/a": fetched("/a", b"<main>A</main>"),
            f"{DOCS}/alias": fetched(
                "/alias",
                b"<main>A</main>",
                final_url=f"{DOCS}/a",
            ),
        }
    )

    result = crawl(fetcher, sitemap("/a", "/alias"))

    assert result.counts.rejections == {RejectionReason.DUPLICATE: 1}
    assert result.counts.redirects == 1
    duplicate = next(
        page for page in result.pages if page.reason is RejectionReason.DUPLICATE
    )
    assert (duplicate.canonical_url, duplicate.final_url, duplicate.status_code) == (
        f"{DOCS}/alias",
        f"{DOCS}/a",
        200,
    )


def test_reuses_verified_304_body_and_discovers_its_links() -> None:
    body = b'<a href="/linked">Linked</a>'
    cached = CachedPage(
        etag='"old"',
        raw_sha256=sha256(body).hexdigest(),
        body=body,
    )
    fetcher = FakeFetcher(
        {
            f"{DOCS}/seed": fetched("/seed", b"", status_code=304, content_type=None),
            f"{DOCS}/linked": fetched("/linked", b"<main>Linked</main>"),
        }
    )

    result = crawl(
        fetcher,
        sitemap("/seed"),
        FakeCache({f"{DOCS}/seed": cached}),
    )

    assert fetcher.requests[0] == (f"{DOCS}/seed", '"old"')
    assert [url for url, _ in fetcher.requests] == [
        f"{DOCS}/seed",
        f"{DOCS}/linked",
    ]
    assert result.counts.status_304 == 1
    seed = next(page for page in result.pages if page.canonical_url.endswith("/seed"))
    assert seed.body == body
    assert seed.etag == '"old"'


def test_corrupt_304_cache_is_recorded_and_crawl_continues() -> None:
    cached = CachedPage(
        etag='"old"',
        raw_sha256="0" * 64,
        body=b"<main>Corrupt</main>",
    )
    fetcher = FakeFetcher(
        {
            f"{DOCS}/seed": fetched("/seed", b"", status_code=304, content_type=None),
            f"{DOCS}/later": fetched("/later", b"<main>Later</main>"),
        }
    )

    result = crawl(
        fetcher,
        sitemap("/seed", "/later"),
        FakeCache({f"{DOCS}/seed": cached}),
    )

    assert not result.complete
    assert [url for url, _ in fetcher.requests] == [
        f"{DOCS}/seed",
        f"{DOCS}/later",
    ]
    failed = next(page for page in result.pages if page.decision is PageDecision.FAILED)
    assert failed.error_type == "CacheIntegrityError"
    assert failed.status_code == 304
    assert result.counts.stored == 1


def test_two_candidates_landing_on_the_same_dead_url_are_both_recorded() -> None:
    fetcher = FakeFetcher(
        {
            f"{DOCS}/x": FetchHttpStatusError(f"{DOCS}/dead", 404),
            f"{DOCS}/y": FetchHttpStatusError(f"{DOCS}/dead", 404),
        }
    )

    result = crawl(fetcher, sitemap("/x", "/y"))

    assert result.complete
    assert result.counts.redirects == 2
    gone = [page for page in result.pages if page.reason is RejectionReason.GONE]
    assert [(page.canonical_url, page.final_url) for page in gone] == [
        (f"{DOCS}/x", f"{DOCS}/dead"),
        (f"{DOCS}/y", f"{DOCS}/dead"),
    ]


def test_classifies_page_outcomes_and_continues_after_failures() -> None:
    fetcher = FakeFetcher(
        {
            f"{DOCS}/gone": FetchHttpStatusError(f"{DOCS}/missing/", 404),
            f"{DOCS}/download": fetched(
                "/download", b"pdf", content_type="application/pdf"
            ),
            f"{DOCS}/redirect": fetched(
                "/redirect",
                b"",
                status_code=302,
                content_type=None,
                final_url="https://example.com/docs",
            ),
            f"{DOCS}/broken": FetchError(f"{DOCS}/broken", "network exhausted"),
            f"{DOCS}/old": fetched(
                "/old",
                b"<main>Moved</main>",
                final_url=f"{DOCS}/new/",
            ),
        }
    )

    result = crawl(
        fetcher,
        sitemap("/gone", "/download", "/redirect", "/broken", "/old", "/new"),
    )

    assert not result.complete
    assert len(fetcher.requests) == 5
    assert result.counts.admitted == 6
    assert result.counts.stored == 1
    assert result.counts.failed == 1
    assert result.counts.redirects == 3
    assert result.counts.rejections == {
        RejectionReason.GONE: 1,
        RejectionReason.NON_HTML: 1,
        RejectionReason.OUTSIDE_HOST: 1,
    }
    assert by_url(result)[f"{DOCS}/broken"] is PageDecision.FAILED
    gone = next(page for page in result.pages if page.reason is RejectionReason.GONE)
    assert (gone.canonical_url, gone.requested_url, gone.final_url) == (
        f"{DOCS}/gone",
        f"{DOCS}/gone",
        f"{DOCS}/missing/",
    )
    moved = next(page for page in result.pages if page.canonical_url.endswith("/new"))
    assert (moved.requested_url, moved.final_url) == (
        f"{DOCS}/old",
        f"{DOCS}/new/",
    )


def test_odd_http_status_is_recorded_as_a_failure() -> None:
    fetcher = FakeFetcher({f"{DOCS}/odd": FetchHttpStatusError(f"{DOCS}/odd", 999)})

    result = crawl(fetcher, sitemap("/odd"))

    assert not result.complete
    failed = result.pages[0]
    assert failed.decision is PageDecision.FAILED
    assert failed.status_code == 999


def test_malformed_link_does_not_fail_its_page() -> None:
    fetcher = FakeFetcher(
        {
            f"{DOCS}/seed": fetched(
                "/seed", b'<a href="http://<host>:<port>/v1">Broken</a>'
            )
        }
    )

    result = crawl(fetcher, sitemap("/seed"))

    assert result.complete
    assert result.counts.stored == 1
    assert result.counts.malformed_links == 1


def test_robots_denial_is_an_explained_rejection() -> None:
    fetcher = FakeFetcher(
        {
            f"{DOCS}/private": RobotsDeniedError(
                f"{DOCS}/private", "disallowed by robots.txt"
            )
        }
    )

    result = crawl(fetcher, sitemap("/private"))

    assert result.complete
    assert result.counts.rejections == {RejectionReason.ROBOTS_DISALLOWED: 1}
    assert result.pages[0].decision is PageDecision.REJECTED


@pytest.mark.parametrize(
    "inventory",
    (sitemap(), sitemap(french=("/fr/x",))),
)
def test_sitemap_without_an_admitted_url_fails_preflight(
    inventory: SitemapSnapshot,
) -> None:
    fetcher = FakeFetcher({})

    with pytest.raises(IngestionError, match="Sitemap admitted no URL"):
        crawl(fetcher, inventory)

    assert fetcher.requests == []


def test_page_limit_and_fatal_retry_stop_the_frontier() -> None:
    pages: dict[str, FetchResult | Exception] = {
        f"{DOCS}/one": fetched("/one", b"<main>One</main>"),
        f"{DOCS}/two": fetched("/two", b"<main>Two</main>"),
        f"{DOCS}/three": fetched("/three", b"<main>Three</main>"),
    }
    limited = FakeFetcher(pages)

    with pytest.raises(ValueError, match=f"between 1 and {MAX_PAGES}"):
        crawl(limited, sitemap("/one"), max_pages=MAX_PAGES + 1)

    limited_result = crawl(limited, sitemap("/one", "/two", "/three"), max_pages=2)

    assert not limited_result.complete
    assert len(limited.requests) == 2
    assert limited_result.counts.failed == 1
    limited_failure = next(
        page for page in limited_result.pages if page.decision is PageDecision.FAILED
    )
    assert limited_failure.error_type == "CrawlLimitError"

    fatal_errors = (
        RetryAfterTooLongError(f"{DOCS}/two", "limit is 30s"),
        RobotsUnavailableError(f"{DOCS}/robots.txt", "terminal HTTP 503"),
    )
    for error in fatal_errors:
        fatal = FakeFetcher(
            {
                f"{DOCS}/one": pages[f"{DOCS}/one"],
                f"{DOCS}/two": error,
                f"{DOCS}/three": pages[f"{DOCS}/three"],
            }
        )
        fatal_result = crawl(fatal, sitemap("/one", "/two", "/three"))

        assert not fatal_result.complete
        assert [url for url, _ in fatal.requests] == [
            f"{DOCS}/one",
            f"{DOCS}/two",
        ]
        assert fatal_result.counts.failed == 2
        stopped = next(
            page for page in fatal_result.pages if page.canonical_url.endswith("/three")
        )
        assert stopped.error_message is not None
        assert stopped.error_message.startswith("Crawl stopped before")
