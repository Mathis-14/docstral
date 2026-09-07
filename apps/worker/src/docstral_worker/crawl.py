from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from time import monotonic
from typing import Protocol

import structlog
from bs4 import BeautifulSoup, ParserRejectedMarkup
from pydantic import BaseModel, ConfigDict, Field

from docstral_worker import IngestionError, _safe_url
from docstral_worker.fetch import (
    REDIRECT_STATUSES,
    Fetcher,
    FetchHttpStatusError,
    FetchResult,
    RetryAfterTooLongError,
)
from docstral_worker.robots import RobotsDeniedError, RobotsUnavailableError
from docstral_worker.sitemap import SitemapSnapshot
from docstral_worker.urls import (
    CanonicalUrl,
    RejectionReason,
    UrlCanonicalizationError,
    admit,
    canonicalize,
    is_docs_url,
)

LOGGER = structlog.get_logger(__name__)
MAX_PAGES = 2_000
SHA256_PATTERN = r"^[0-9a-f]{64}$"


class DiscoveryVia(StrEnum):
    SITEMAP = "sitemap"
    LINK = "link"


class PageDecision(StrEnum):
    STORED = "stored"
    REJECTED = "rejected"
    FAILED = "failed"


class CachedPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    etag: str | None
    raw_sha256: str = Field(pattern=SHA256_PATTERN)
    body: bytes


class PageCache(Protocol):
    def get(self, canonical_url: str) -> CachedPage | None: ...


class CrawlEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    canonical_url: str
    requested_url: str
    final_url: str | None = None
    discovered_via: DiscoveryVia
    decision: PageDecision
    reason: RejectionReason | None = None
    status_code: int | None = Field(default=None, ge=100)
    etag: str | None = None
    raw_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    error_type: str | None = None
    error_message: str | None = None
    body: bytes | None = Field(default=None, exclude=True, repr=False)


class CrawlCounts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sitemap_english: int = Field(ge=0)
    sitemap_french: int = Field(ge=0)
    discovered_by_link: int = Field(ge=0)
    admitted: int = Field(ge=0)
    stored: int = Field(ge=0)
    rejected: int = Field(ge=0)
    failed: int = Field(ge=0)
    status_200: int = Field(ge=0)
    status_304: int = Field(ge=0)
    redirects: int = Field(ge=0)
    external_links: int = Field(ge=0)
    malformed_links: int = Field(ge=0)
    rejections: dict[RejectionReason, int]


class CrawlResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pages: tuple[CrawlEntry, ...]
    counts: CrawlCounts
    complete: bool
    duration_seconds: float = Field(ge=0.0)


class CrawlLimitError(IngestionError):
    def __init__(self, url: str, max_pages: int) -> None:
        self.url = _safe_url(url)
        super().__init__(f"Crawl page limit {max_pages} reached before {self.url!r}")


class CacheIntegrityError(IngestionError):
    def __init__(self, url: str, detail: str) -> None:
        self.url = _safe_url(url)
        super().__init__(f"Cached page {self.url!r}: {detail}")


@dataclass(frozen=True)
class _Candidate:
    url: str
    discovered_via: DiscoveryVia


@dataclass
class _State:
    cache: PageCache | None
    queue: deque[_Candidate] = field(default_factory=deque)
    seen: set[str] = field(default_factory=set)
    pages: dict[str, CrawlEntry] = field(default_factory=dict)
    external_links: int = 0
    malformed_links: int = 0
    discovered_by_link: int = 0
    admitted: int = 0
    status_200: int = 0
    status_304: int = 0
    redirects: int = 0

    def discover(self, url: CanonicalUrl, via: DiscoveryVia) -> None:
        if url.url in self.seen:
            return
        self.seen.add(url.url)
        if not is_docs_url(url.url) and via is DiscoveryVia.LINK:
            self.external_links += 1
            return
        if via is DiscoveryVia.LINK:
            self.discovered_by_link += 1
        reason = admit(url).reason
        candidate = _Candidate(url.url, via)
        if reason is None:
            self.admitted += 1
            self.queue.append(candidate)
            return
        self.record(_rejected(candidate, reason))

    def record(self, entry: CrawlEntry, duration_seconds: float = 0.0) -> None:
        if entry.canonical_url in self.pages:
            raise IngestionError(
                f"Duplicate canonical entry {_safe_url(entry.canonical_url)!r}"
            )
        self.pages[entry.canonical_url] = entry
        self.seen.add(entry.canonical_url)
        LOGGER.info(
            "crawl_page",
            canonical_url=entry.canonical_url,
            status_code=entry.status_code,
            duration_ms=round(duration_seconds * 1_000, 3),
            decision=entry.decision.value,
            reason=entry.reason.value if entry.reason is not None else None,
        )

    def count_response(self, result: FetchResult) -> None:
        if result.final_url != result.requested_url:
            self.redirects += 1
        if result.status_code == 200:
            self.status_200 += 1
        elif result.status_code == 304:
            self.status_304 += 1


def crawl(
    fetcher: Fetcher,
    sitemap: SitemapSnapshot,
    cache: PageCache | None = None,
    *,
    max_pages: int = MAX_PAGES,
) -> CrawlResult:
    if not 1 <= max_pages <= MAX_PAGES:
        raise ValueError(f"max_pages must be between 1 and {MAX_PAGES}")
    started_at = monotonic()
    state = _State(cache=cache)
    for url in sitemap.french_urls:
        state.discover(canonicalize(url, sitemap.url), DiscoveryVia.SITEMAP)
    for url in sitemap.english_urls:
        state.discover(canonicalize(url, sitemap.url), DiscoveryVia.SITEMAP)
    if not state.queue:
        raise IngestionError("Sitemap admitted no URL")

    fetched = 0
    while state.queue:
        candidate = state.queue.popleft()
        if candidate.url in state.pages:
            continue
        if fetched >= max_pages:
            state.queue.appendleft(candidate)
            _drain(state, lambda url: CrawlLimitError(url, max_pages))
            break
        fetched += 1
        if _visit(state, fetcher, candidate):
            _drain(
                state,
                lambda url: IngestionError(f"Crawl stopped before {_safe_url(url)!r}"),
            )
            break

    return _result(state, sitemap, monotonic() - started_at)


def _drain(state: _State, error: Callable[[str], Exception]) -> None:
    while state.queue:
        candidate = state.queue.popleft()
        if candidate.url not in state.pages:
            state.record(_failed(candidate, error(candidate.url)))


def _visit(state: _State, fetcher: Fetcher, candidate: _Candidate) -> bool:
    started_at = monotonic()
    try:
        cached = state.cache.get(candidate.url) if state.cache is not None else None
    except IngestionError as exc:
        state.record(_failed(candidate, exc), monotonic() - started_at)
        return False
    result: FetchResult | None = None
    try:
        result = fetcher.fetch(candidate.url, cached.etag if cached else None)
        _consume(state, candidate, result, cached, monotonic() - started_at)
    except (RetryAfterTooLongError, RobotsUnavailableError) as exc:
        state.record(_failed(candidate, exc, result), monotonic() - started_at)
        return True
    except RobotsDeniedError as exc:
        state.record(
            _rejected(
                candidate,
                RejectionReason.ROBOTS_DISALLOWED,
                final_url=exc.url,
            ),
            monotonic() - started_at,
        )
    except FetchHttpStatusError as exc:
        if exc.url != candidate.url:
            state.redirects += 1
        if exc.status_code in {404, 410}:
            state.record(
                _rejected(
                    candidate,
                    RejectionReason.GONE,
                    final_url=exc.url,
                    status=exc.status_code,
                ),
                monotonic() - started_at,
            )
        else:
            state.record(_failed(candidate, exc), monotonic() - started_at)
    except IngestionError as exc:
        state.record(_failed(candidate, exc, result), monotonic() - started_at)
    return False


def _consume(
    state: _State,
    candidate: _Candidate,
    result: FetchResult,
    cached: CachedPage | None,
    duration_seconds: float,
) -> None:
    state.count_response(result)
    final = canonicalize(result.final_url, candidate.url)
    reason = _rejection_reason(state, candidate, result, final)
    if reason is not None:
        state.record(
            _rejected(
                candidate,
                reason,
                final_url=_safe_url(result.final_url),
                status=result.status_code,
                etag=result.etag,
            ),
            duration_seconds,
        )
        return
    body, etag = _response_body(final.url, result, cached)
    links, malformed = extract_links(body, final.url)
    state.malformed_links += malformed
    state.record(_stored(candidate, result, final.url, body, etag), duration_seconds)
    for link in links:
        state.discover(link, DiscoveryVia.LINK)


def _rejection_reason(
    state: _State,
    candidate: _Candidate,
    result: FetchResult,
    final: CanonicalUrl,
) -> RejectionReason | None:
    if result.status_code in REDIRECT_STATUSES:
        if not is_docs_url(result.final_url):
            return RejectionReason.OUTSIDE_HOST
        reason = admit(final).reason
        if reason is None:
            raise IngestionError(
                f"Unfetched redirect target {_safe_url(final.url)!r} was admitted"
            )
        return reason
    reason = admit(final).reason
    if reason is not None:
        return reason
    if final.url != candidate.url and final.url in state.pages:
        return RejectionReason.DUPLICATE
    if result.status_code == 200 and result.content_type != "text/html":
        return RejectionReason.NON_HTML
    return None


def extract_links(body: bytes, base: str) -> tuple[tuple[CanonicalUrl, ...], int]:
    try:
        soup = BeautifulSoup(body, "html.parser")
    except ParserRejectedMarkup as exc:
        raise IngestionError(f"Unparseable HTML in {_safe_url(base)!r}") from exc
    links: list[CanonicalUrl] = []
    malformed = 0
    for element in soup.select("a[href]"):
        href = element.get("href")
        if not isinstance(href, str):
            malformed += 1
            continue
        try:
            links.append(canonicalize(href, base))
        except UrlCanonicalizationError:
            malformed += 1
    return tuple(links), malformed


def _response_body(
    url: str,
    result: FetchResult,
    cached: CachedPage | None,
) -> tuple[bytes, str | None]:
    if result.status_code == 200:
        return result.body, result.etag
    if result.status_code != 304:
        raise IngestionError(
            f"Unexpected HTTP {result.status_code} for {_safe_url(url)!r}"
        )
    if cached is None:
        raise CacheIntegrityError(url, "HTTP 304 without a cached page")
    if sha256(cached.body).hexdigest() != cached.raw_sha256:
        raise CacheIntegrityError(
            url, "raw SHA-256 does not match its recorded SHA-256"
        )
    return cached.body, result.etag or cached.etag


def _rejected(
    candidate: _Candidate,
    reason: RejectionReason,
    *,
    final_url: str | None = None,
    status: int | None = None,
    etag: str | None = None,
) -> CrawlEntry:
    return CrawlEntry(
        canonical_url=candidate.url,
        requested_url=candidate.url,
        final_url=final_url,
        discovered_via=candidate.discovered_via,
        decision=PageDecision.REJECTED,
        reason=reason,
        status_code=status,
        etag=etag,
    )


def _stored(
    candidate: _Candidate,
    result: FetchResult,
    canonical_url: str,
    body: bytes,
    etag: str | None,
) -> CrawlEntry:
    return CrawlEntry(
        canonical_url=canonical_url,
        requested_url=candidate.url,
        final_url=_safe_url(result.final_url),
        discovered_via=candidate.discovered_via,
        decision=PageDecision.STORED,
        status_code=result.status_code,
        etag=etag,
        raw_sha256=sha256(body).hexdigest(),
        body=body,
    )


def _failed(
    candidate: _Candidate,
    error: Exception,
    result: FetchResult | None = None,
) -> CrawlEntry:
    final_url = _safe_url(result.final_url) if result is not None else None
    status_code = result.status_code if result is not None else None
    if isinstance(error, FetchHttpStatusError):
        final_url = error.url
        status_code = error.status_code
    return CrawlEntry(
        canonical_url=candidate.url,
        requested_url=candidate.url,
        final_url=final_url,
        discovered_via=candidate.discovered_via,
        decision=PageDecision.FAILED,
        status_code=status_code,
        error_type=type(error).__name__,
        error_message=str(error),
    )


def _result(
    state: _State, sitemap: SitemapSnapshot, duration_seconds: float
) -> CrawlResult:
    pages = tuple(sorted(state.pages.values(), key=lambda page: page.canonical_url))
    stored = sum(page.decision is PageDecision.STORED for page in pages)
    rejected = sum(page.decision is PageDecision.REJECTED for page in pages)
    failed = sum(page.decision is PageDecision.FAILED for page in pages)
    rejections: Counter[RejectionReason] = Counter(
        page.reason for page in pages if page.reason is not None
    )
    return CrawlResult(
        pages=pages,
        counts=CrawlCounts(
            sitemap_english=len(sitemap.english_urls),
            sitemap_french=len(sitemap.french_urls),
            discovered_by_link=state.discovered_by_link,
            admitted=state.admitted,
            stored=stored,
            rejected=rejected,
            failed=failed,
            status_200=state.status_200,
            status_304=state.status_304,
            redirects=state.redirects,
            external_links=state.external_links,
            malformed_links=state.malformed_links,
            rejections=dict(sorted(rejections.items(), key=lambda item: item[0].value)),
        ),
        complete=failed == 0,
        duration_seconds=duration_seconds,
    )
