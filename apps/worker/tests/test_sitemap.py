from hashlib import sha256
from pathlib import Path

import pytest
from docstral_worker.fetch import FetchResult
from docstral_worker.sitemap import (
    SitemapFetchError,
    SitemapIndexError,
    SitemapParseError,
    fetch_sitemap,
    parse_sitemap,
)

SITEMAP_URL = "https://docs.mistral.ai/sitemap.xml"


class StubFetcher:
    def __init__(
        self,
        body: bytes,
        status_code: int = 200,
        final_url: str = SITEMAP_URL,
    ) -> None:
        self.body = body
        self.status_code = status_code
        self.final_url = final_url
        self.request: tuple[str, str | None] | None = None

    def fetch(self, url: str, etag: str | None) -> FetchResult:
        self.request = (url, etag)
        return FetchResult(
            requested_url=url,
            final_url=self.final_url,
            status_code=self.status_code,
            etag=None,
            content_type="application/xml",
            body=self.body,
        )


def test_fetch_sitemap_filters_language_and_hashes_bytes() -> None:
    fixture = Path(__file__).parent / "fixtures" / "sitemap.xml"
    payload = fixture.read_bytes()
    fetcher = StubFetcher(payload)

    result = fetch_sitemap(fetcher)

    assert fetcher.request == (SITEMAP_URL, None)
    assert len(result.english_urls) == 10
    assert len(result.french_urls) == 10
    assert result.english_urls[0] == "https://docs.mistral.ai/"
    assert result.french_urls[0] == "https://docs.mistral.ai/fr"
    assert result.total_count == 20
    assert result.sha256 == sha256(payload).hexdigest()


def test_fetch_sitemap_names_unfetched_external_redirect() -> None:
    fetcher = StubFetcher(
        b"", status_code=302, final_url="https://example.com/sitemap.xml"
    )

    with pytest.raises(SitemapFetchError, match="external redirect was not fetched"):
        fetch_sitemap(fetcher)


def test_fetch_sitemap_names_unexpected_304() -> None:
    fetcher = StubFetcher(b"", status_code=304)

    with pytest.raises(SitemapFetchError, match="unexpected HTTP 304"):
        fetch_sitemap(fetcher)


def test_sitemap_error_hides_source_query() -> None:
    source_url = f"{SITEMAP_URL}?token=secret"

    with pytest.raises(SitemapParseError) as caught:
        parse_sitemap(b"<urlset>", source_url)

    assert caught.value.url == SITEMAP_URL
    assert "secret" not in str(caught.value)


def test_sitemap_index_fails_explicitly() -> None:
    payload = b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" />'

    with pytest.raises(SitemapIndexError, match="not supported"):
        parse_sitemap(payload, SITEMAP_URL)


@pytest.mark.parametrize(
    "payload",
    [
        b"<urlset>",
        b"<urlset><url /></urlset>",
        b"<urlset><url><loc>/relative</loc></url></urlset>",
        b"<feed />",
    ],
)
def test_invalid_sitemap_fails_with_context(payload: bytes) -> None:
    with pytest.raises(SitemapParseError, match="Sitemap"):
        parse_sitemap(payload, SITEMAP_URL)
