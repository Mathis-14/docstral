from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import TYPE_CHECKING

import httpx
import pytest
from docstral_ingestion.fetch import (
    USER_AGENT,
    FetchConfig,
    Fetcher,
    FetchError,
    FetchHttpStatusError,
    HttpFetcher,
    RedirectLimitError,
    RetryAfterTooLongError,
)
from docstral_ingestion.robots import RobotsDeniedError, RobotsUnavailableError

DOCS = "https://docs.mistral.ai"
WALL_TIME = datetime(2026, 1, 1, tzinfo=UTC)

if TYPE_CHECKING:
    _conforms: Fetcher = HttpFetcher()


class FakeTime:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds

    def wall_clock(self) -> datetime:
        return WALL_TIME + timedelta(seconds=self.value)


def make_fetcher(
    handler: Callable[[httpx.Request], httpx.Response],
    timer: FakeTime | None = None,
    *,
    delay: float = 0.0,
) -> HttpFetcher:
    timer = timer or FakeTime()
    return HttpFetcher(
        FetchConfig(delay_seconds=delay),
        transport=httpx.MockTransport(handler),
        sleeper=timer.sleep,
        clock=timer.monotonic,
        wall_clock=timer.wall_clock,
    )


def without_robots(
    page_handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return page_handler(request)

    return handler


def test_fetches_page_with_metadata_and_user_agent() -> None:
    def page(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"] == USER_AGENT
        return httpx.Response(
            200,
            content=b"<main>Docs</main>",
            headers={"content-type": "text/html; charset=utf-8", "etag": '"v1"'},
        )

    with make_fetcher(without_robots(page)) as fetcher:
        result = fetcher.fetch(f"{DOCS}/studio", etag=None)

    assert result.body == b"<main>Docs</main>"
    assert result.content_type == "text/html"
    assert result.etag == '"v1"'
    assert result.status_code == 200


def test_sends_etag_and_returns_304() -> None:
    def page(request: httpx.Request) -> httpx.Response:
        assert request.headers["if-none-match"] == '"v1"'
        return httpx.Response(304, headers={"etag": '"v1"'})

    with make_fetcher(without_robots(page)) as fetcher:
        result = fetcher.fetch(f"{DOCS}/studio", etag='"v1"')

    assert result.status_code == 304
    assert result.body == b""


@pytest.mark.parametrize(
    ("retry_after", "expected_delay"),
    [
        (None, 0.5),
        ("2", 2.0),
        ("30", 30.0),
        ("invalid", 0.5),
        (format_datetime(WALL_TIME + timedelta(seconds=2), usegmt=True), 2.0),
    ],
)
def test_retries_429_with_retry_after_policy(
    retry_after: str | None, expected_delay: float
) -> None:
    timer = FakeTime()
    attempts = 0

    def page(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            headers = {"retry-after": retry_after} if retry_after is not None else {}
            return httpx.Response(429, headers=headers)
        return httpx.Response(200)

    with make_fetcher(without_robots(page), timer) as fetcher:
        fetcher.fetch(f"{DOCS}/studio", etag=None)

    assert attempts == 2
    assert timer.sleeps == [expected_delay]


def test_retry_after_over_30_seconds_is_fatal() -> None:
    timer = FakeTime()

    def page(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "31"})

    with make_fetcher(without_robots(page), timer) as fetcher:
        with pytest.raises(RetryAfterTooLongError, match="limit is 30s"):
            fetcher.fetch(f"{DOCS}/studio", etag=None)

    assert timer.sleeps == []


def test_retries_5xx_three_times() -> None:
    timer = FakeTime()
    attempts = 0

    def page(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        headers = {"retry-after": "31"} if attempts == 3 else {}
        return httpx.Response(503, headers=headers)

    with make_fetcher(without_robots(page), timer) as fetcher:
        with pytest.raises(FetchHttpStatusError, match="HTTP 503"):
            fetcher.fetch(f"{DOCS}/studio", etag=None)

    assert attempts == 3
    assert timer.sleeps == [0.5, 1.0]


@pytest.mark.parametrize(
    ("outcome", "expected_error", "message"),
    [
        (404, FetchHttpStatusError, "HTTP 404"),
        (
            httpx.UnsupportedProtocol("unsupported protocol"),
            FetchError,
            "UnsupportedProtocol",
        ),
    ],
)
def test_definitive_error_is_not_retried(
    outcome: int | httpx.HTTPError,
    expected_error: type[FetchError],
    message: str,
) -> None:
    attempts = 0

    def page(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if isinstance(outcome, int):
            return httpx.Response(outcome)
        raise outcome

    with make_fetcher(without_robots(page)) as fetcher:
        with pytest.raises(expected_error, match=message):
            fetcher.fetch(f"{DOCS}/studio", etag=None)

    assert attempts == 1


def test_retries_timeouts_three_times() -> None:
    timer = FakeTime()

    def page(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow response", request=request)

    with make_fetcher(without_robots(page), timer) as fetcher:
        with pytest.raises(FetchError, match="ReadTimeout exhausted retries"):
            fetcher.fetch(f"{DOCS}/studio", etag=None)

    assert timer.sleeps == [0.5, 1.0]


def test_follows_five_redirects_and_rejects_sixth() -> None:
    stop_at: int | None = 5

    def page(request: httpx.Request) -> httpx.Response:
        step = int(request.url.path.rsplit("/", maxsplit=1)[-1])
        if step == stop_at:
            return httpx.Response(200)
        return httpx.Response(302, headers={"location": f"/redirect/{step + 1}"})

    with make_fetcher(without_robots(page)) as fetcher:
        result = fetcher.fetch(f"{DOCS}/redirect/0", etag=None)

    assert result.final_url == f"{DOCS}/redirect/5"

    stop_at = None
    with make_fetcher(without_robots(page)) as fetcher:
        with pytest.raises(RedirectLimitError, match="more than 5"):
            fetcher.fetch(f"{DOCS}/redirect/0", etag=None)


def test_sends_etag_only_before_redirect() -> None:
    def page(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            assert request.headers["if-none-match"] == '"v1"'
            return httpx.Response(301, headers={"location": "/new"})
        assert request.url.path == "/new"
        assert "if-none-match" not in request.headers
        return httpx.Response(200, headers={"etag": '"v2"'})

    with make_fetcher(without_robots(page)) as fetcher:
        result = fetcher.fetch(f"{DOCS}/old", etag='"v1"')

    assert result.final_url == f"{DOCS}/new"
    assert result.etag == '"v2"'


def test_redirect_checks_robots_before_second_page_request() -> None:
    page_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200, text="User-agent: Docstral\nDisallow: /private\n"
            )
        page_requests.append(request.url.path)
        if request.url.path == "/a":
            return httpx.Response(302, headers={"location": "/private"})
        raise AssertionError("disallowed redirect target must not be fetched")

    with make_fetcher(handler) as fetcher:
        with pytest.raises(RobotsDeniedError, match="disallowed"):
            fetcher.fetch(f"{DOCS}/a", etag=None)

    assert page_requests == ["/a"]


def test_returns_external_redirect_without_following_it() -> None:
    requests: list[str] = []

    def page(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://example.com/docs", "etag": '"redirect"'},
        )

    with make_fetcher(without_robots(page)) as fetcher:
        result = fetcher.fetch(f"{DOCS}/studio", etag=None)

    assert requests == [f"{DOCS}/studio"]
    assert result.final_url == "https://example.com/docs"
    assert result.etag is None
    assert result.content_type is None
    assert result.body == b""


def test_rejects_credentials_without_request_or_password_in_error() -> None:
    requests: list[httpx.Request] = []
    password = "sensitive-value"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    with make_fetcher(handler) as fetcher:
        with pytest.raises(FetchError) as caught:
            fetcher.fetch(f"https://user:{password}@docs.mistral.ai/studio", etag=None)

    assert requests == []
    assert password not in str(caught.value)


def test_does_not_follow_same_host_redirect_with_credentials() -> None:
    requests: list[str] = []
    redirect_url = "https://user:sensitive-value@docs.mistral.ai/private"

    def page(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": redirect_url})

    with make_fetcher(without_robots(page)) as fetcher:
        result = fetcher.fetch(f"{DOCS}/studio", etag=None)

    assert requests == [f"{DOCS}/studio"]
    assert result.status_code == 302
    assert result.final_url == redirect_url


def test_rejects_outside_host_without_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    with make_fetcher(handler) as fetcher:
        with pytest.raises(FetchError, match="expected an HTTPS URL"):
            fetcher.fetch("https://example.com/docs", etag=None)

    assert requests == []


def test_robots_503_is_cached_and_stops_before_page_fetch() -> None:
    timer = FakeTime()
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(503)

    with make_fetcher(handler, timer) as fetcher:
        for _ in range(2):
            with pytest.raises(RobotsUnavailableError, match=r"robots\.txt"):
                fetcher.fetch(f"{DOCS}/studio", etag=None)

    assert paths == ["/robots.txt"] * 3
    assert timer.sleeps == [0.5, 1.0]


@pytest.mark.parametrize(
    ("directive", "configured_delay", "expected_delay"),
    [
        ("Crawl-delay: 2", 0.0, 2.0),
        ("Request-rate: 2/5", 0.0, 2.5),
        ("Crawl-delay: 2", 3.0, 3.0),
    ],
)
def test_robots_and_config_set_request_cadence(
    directive: str, configured_delay: float, expected_delay: float
) -> None:
    timer = FakeTime()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            rules = f"User-agent: Docstral\n{directive}\n"
            return httpx.Response(200, text=rules)
        return httpx.Response(200)

    with make_fetcher(handler, timer, delay=configured_delay) as fetcher:
        fetcher.fetch(f"{DOCS}/studio", etag=None)

    assert timer.sleeps == [expected_delay]
