"""The fetch contract and its synchronous HTTP adapter stay together here.
Retries, redirects, and cadence share the same client and request state.
Robots parsing and URL admission remain in their dedicated modules."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import monotonic, sleep
from types import TracebackType
from typing import Protocol, Self
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel, ConfigDict, Field

from docstral_ingestion import IngestionError, _safe_url
from docstral_ingestion.robots import (
    ROBOTS_URL,
    RobotsPolicy,
    RobotsResponse,
    RobotsUnavailableError,
    load_robots,
)
from docstral_ingestion.urls import DOCS_HOST, is_docs_url

USER_AGENT = "Docstral/0.1 (+https://github.com/Mathis-14/docstral)"
MAX_ATTEMPTS = 3
MAX_REDIRECTS = 5
MAX_RETRY_AFTER_SECONDS = 30.0
BACKOFF_SECONDS: tuple[float, ...] = tuple(
    0.5 * 2**attempt for attempt in range(MAX_ATTEMPTS - 1)
)
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class FetchError(IngestionError):
    def __init__(self, url: str, detail: str) -> None:
        self.url = _safe_url(url)
        super().__init__(f"Fetch {self.url!r}: {detail}")


class FetchHttpStatusError(FetchError):
    def __init__(self, url: str, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(url, f"terminal HTTP {status_code}")


class RedirectLimitError(FetchError):
    pass


class RetryAfterTooLongError(FetchError):
    pass


class FetchConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    delay_seconds: float = Field(default=0.25, ge=0.0)


class FetchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    requested_url: str
    final_url: str
    status_code: int = Field(ge=100, le=599)
    etag: str | None
    content_type: str | None
    body: bytes


class Fetcher(Protocol):
    def fetch(self, url: str, etag: str | None) -> FetchResult: ...


class HttpFetcher:
    """Synchronous, robots-aware HTTP implementation of Fetcher."""

    def __init__(
        self,
        config: FetchConfig | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = sleep,
        clock: Callable[[], float] = monotonic,
        wall_clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        delay_seconds = (config or FetchConfig()).delay_seconds
        self._sleep = sleeper
        self._clock = clock
        self._wall_clock = wall_clock
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
            follow_redirects=False,
            transport=transport,
        )
        self._robots: RobotsPolicy | None = None
        self._robots_failure: IngestionError | None = None
        self._request_interval = delay_seconds
        self._configured_delay = delay_seconds
        self._last_request_at: float | None = None

    def __enter__(self) -> Self:
        self._client.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def fetch(self, url: str, etag: str | None) -> FetchResult:
        """Fetch one URL.

        The returned ETag belongs to ``final_url``. A 3xx ``status_code`` means
        that ``final_url`` was not fetched and must be handled by admission.
        """
        if not is_docs_url(url):
            raise FetchError(url, f"expected an HTTPS URL on {DOCS_HOST}")
        robots = self._ensure_robots()

        headers = {"If-None-Match": etag} if etag is not None else {}
        response, final_url = self._follow_redirects(
            url, headers, robots=robots, follow_external=False
        )
        final_url_was_fetched = is_docs_url(final_url)
        return FetchResult(
            requested_url=url,
            final_url=final_url,
            status_code=response.status_code,
            etag=response.headers.get("etag") if final_url_was_fetched else None,
            content_type=_content_type(response) if final_url_was_fetched else None,
            body=response.content if final_url_was_fetched else b"",
        )

    def _ensure_robots(self) -> RobotsPolicy:
        if self._robots is not None:
            return self._robots
        if self._robots_failure is not None:
            raise self._robots_failure
        try:
            policy = load_robots(self._fetch_robots, self._configured_delay)
        except IngestionError as exc:
            self._robots_failure = exc
            raise
        self._robots = policy
        self._request_interval = policy.request_interval
        return policy

    def _fetch_robots(self) -> RobotsResponse:
        try:
            response, final_url = self._follow_redirects(
                ROBOTS_URL, {}, robots=None, follow_external=True
            )
        except RetryAfterTooLongError:
            raise
        except FetchHttpStatusError as exc:
            return RobotsResponse(
                url=exc.url,
                status_code=exc.status_code,
                content_type=None,
                body=b"",
            )
        except FetchError as exc:
            raise RobotsUnavailableError(ROBOTS_URL, str(exc)) from exc
        return RobotsResponse(
            url=final_url,
            status_code=response.status_code,
            content_type=_content_type(response),
            body=response.content,
        )

    def _follow_redirects(
        self,
        url: str,
        headers: Mapping[str, str],
        *,
        robots: RobotsPolicy | None,
        follow_external: bool,
    ) -> tuple[httpx.Response, str]:
        current_url = url
        request_headers = headers
        redirect_count = 0
        while True:
            if robots is not None:
                robots.check(current_url)
            response = self._request(current_url, request_headers)
            if response.status_code not in REDIRECT_STATUSES:
                return response, current_url

            location = response.headers.get("location")
            if location is None:
                raise FetchHttpStatusError(current_url, response.status_code)
            redirect_count += 1
            if redirect_count > MAX_REDIRECTS:
                raise RedirectLimitError(url, f"more than {MAX_REDIRECTS} redirects")
            next_url = urljoin(current_url, location)
            if not follow_external and not is_docs_url(next_url):
                return response, next_url
            current_url = next_url
            request_headers = {}

    def _request(self, url: str, headers: Mapping[str, str]) -> httpx.Response:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._wait_for_request_slot()
            try:
                response = self._client.get(url, headers=headers)
            except httpx.InvalidURL as exc:
                raise FetchError(url, "invalid URL") from exc
            except (
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.RemoteProtocolError,
            ) as exc:
                if attempt == MAX_ATTEMPTS:
                    raise FetchError(
                        url, f"{type(exc).__name__} exhausted retries"
                    ) from exc
                self._sleep(BACKOFF_SECONDS[attempt - 1])
                continue
            except httpx.HTTPError as exc:
                raise FetchError(url, f"{type(exc).__name__} is not retryable") from exc

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == MAX_ATTEMPTS:
                    raise FetchHttpStatusError(url, response.status_code)
                self._sleep(self._retry_delay(response, attempt))
                continue
            if (
                200 <= response.status_code < 300
                or response.status_code == 304
                or response.status_code in REDIRECT_STATUSES
            ):
                return response
            raise FetchHttpStatusError(url, response.status_code)
        raise AssertionError("retry loop must return or raise")

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        fallback = BACKOFF_SECONDS[attempt - 1]
        value = response.headers.get("retry-after")
        if value is None:
            return fallback
        seconds = _parse_retry_after(value, self._wall_clock())
        if seconds is None:
            return fallback
        if seconds > MAX_RETRY_AFTER_SECONDS:
            raise RetryAfterTooLongError(
                str(response.request.url),
                f"Retry-After requests {seconds:.3f}s, "
                f"limit is {MAX_RETRY_AFTER_SECONDS:g}s",
            )
        return seconds

    def _wait_for_request_slot(self) -> None:
        now = self._clock()
        if self._last_request_at is not None:
            remaining = self._request_interval - (now - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = self._clock()


def _parse_retry_after(value: str, now: datetime) -> float | None:
    stripped = value.strip()
    if stripped.isascii() and stripped.isdigit():
        return float(stripped)
    try:
        retry_at = parsedate_to_datetime(stripped)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None or now.tzinfo is None:
        return None
    seconds = (retry_at - now).total_seconds()
    return seconds if seconds >= 0 else None


def _content_type(response: httpx.Response) -> str | None:
    value = response.headers.get("content-type")
    if value is None:
        return None
    media_type = value.partition(";")[0].strip().lower()
    return media_type or None
