from collections.abc import Callable

import httpx
import pytest
from docstral_worker.fetch import FetchConfig, HttpFetcher
from docstral_worker.robots import (
    ROBOTS_URL,
    RobotsDeniedError,
    RobotsPolicy,
    RobotsResponse,
    RobotsUnavailableError,
    load_robots,
)

DOCS = "https://docs.mistral.ai"


def make_response(
    *,
    status_code: int = 200,
    content_type: str | None = "text/plain",
    body: bytes = b"User-agent: *\nAllow: /\n",
) -> RobotsResponse:
    return RobotsResponse(
        url=ROBOTS_URL,
        status_code=status_code,
        content_type=content_type,
        body=body,
    )


def load(response: RobotsResponse) -> RobotsPolicy:
    return load_robots(lambda: response, configured_delay=0.0)


def make_fetcher(
    handler: Callable[[httpx.Request], httpx.Response],
) -> HttpFetcher:
    return HttpFetcher(
        FetchConfig(delay_seconds=0.0),
        transport=httpx.MockTransport(handler),
    )


def test_429_is_unavailable() -> None:
    with pytest.raises(RobotsUnavailableError, match="HTTP 429"):
        load(make_response(status_code=429))


def test_non_utf8_body_is_unavailable() -> None:
    with pytest.raises(RobotsUnavailableError, match="not valid UTF-8"):
        load(make_response(body=b"\xff"))


def test_non_plain_text_response_is_unavailable() -> None:
    with pytest.raises(RobotsUnavailableError, match="expected text/plain"):
        load(make_response(content_type="text/html"))


def test_wildcard_rule_matches_query() -> None:
    policy = load(make_response(body=b"User-agent: *\nDisallow: /*?\n"))

    policy.check(f"{DOCS}/studio")
    with pytest.raises(RobotsDeniedError):
        policy.check(f"{DOCS}/studio?source=test")


def test_more_specific_allow_wins() -> None:
    policy = load(
        make_response(
            body=(b"User-agent: *\nDisallow: /private\nAllow: /private/public\n")
        )
    )

    policy.check(f"{DOCS}/private/public/page")
    with pytest.raises(RobotsDeniedError):
        policy.check(f"{DOCS}/private/secret")


def test_robots_redirect_is_followed() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(301, headers={"location": "/robots-v2.txt"})
        if request.url.path == "/robots-v2.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200)

    with make_fetcher(handler) as fetcher:
        fetcher.fetch(f"{DOCS}/studio", etag=None)

    assert paths == ["/robots.txt", "/robots-v2.txt", "/studio"]
