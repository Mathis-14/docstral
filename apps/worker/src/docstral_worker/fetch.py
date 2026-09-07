from collections.abc import Awaitable, Callable
from datetime import timedelta
from urllib.parse import urljoin

import httpx2
from crawlee import Request
from crawlee.http_clients import HttpClient, HttpResponse, HttpxHttpClient

from docstral_worker import IngestionError, _safe_url
from docstral_worker.urls import DOCS_HOST, is_docs_url

USER_AGENT = "Docstral/0.1 (+https://github.com/Mathis-14/docstral)"
TIMEOUT = timedelta(seconds=30)
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class FetchError(IngestionError):
    def __init__(
        self,
        url: str,
        detail: str,
        *,
        status_code: int | None = None,
        transient: bool = False,
    ) -> None:
        self.url = _safe_url(url)
        self.status_code = status_code
        self.transient = transient
        super().__init__(f"Fetch {self.url!r}: {detail}")


class FetchHttpStatusError(FetchError):
    def __init__(self, url: str, status_code: int) -> None:
        super().__init__(
            url,
            f"HTTP {status_code}",
            status_code=status_code,
            transient=status_code in (408, 429) or status_code >= 500,
        )


def http_client() -> HttpxHttpClient:
    return HttpxHttpClient(header_generator=None, follow_redirects=False)


def request(url: str) -> Request:
    if not is_docs_url(url):
        raise FetchError(url, f"expected an HTTPS URL on {DOCS_HOST}")
    return Request.from_url(url, unique_key=url, headers={"User-Agent": USER_AGENT})


async def get(
    client: HttpClient,
    url: str,
    *,
    follow_redirects: bool = False,
    before_request: Callable[[str], Awaitable[None]] | None = None,
) -> HttpResponse:
    visited: set[str] = set()
    while True:
        request(url)
        if url in visited or len(visited) >= 20:
            raise FetchError(url, "redirect cycle or limit reached")
        visited.add(url)
        if before_request is not None:
            await before_request(url)
        try:
            response = await client.send_request(
                url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
            )
        except (TimeoutError, httpx2.HTTPError) as error:
            raise FetchError(
                url, type(error).__name__, transient=is_transient(error)
            ) from error
        if not follow_redirects or response.status_code not in REDIRECT_STATUSES:
            return response
        location = response.headers.get("location")
        if not location:
            raise FetchError(url, "redirect has no Location header")
        url = urljoin(url, location)


def is_transient(error: BaseException) -> bool:
    if isinstance(error, FetchError):
        return error.transient
    if isinstance(
        error,
        (
            TimeoutError,
            httpx2.TimeoutException,
            httpx2.NetworkError,
            httpx2.RemoteProtocolError,
        ),
    ):
        return True
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return status in (408, 429) or status >= 500
    cause = error.__cause__ or error.__context__
    return cause is not None and is_transient(cause)
