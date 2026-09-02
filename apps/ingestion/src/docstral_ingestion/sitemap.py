from hashlib import sha256
from urllib.parse import urlsplit
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict

from docstral_ingestion import IngestionError, _safe_url
from docstral_ingestion.fetch import REDIRECT_STATUSES, Fetcher
from docstral_ingestion.urls import DOCS_HOST

SITEMAP_URL = "https://docs.mistral.ai/sitemap.xml"


class SitemapError(IngestionError):
    """Base error for sitemap loading and parsing."""

    def __init__(self, url: str, detail: str) -> None:
        self.url = _safe_url(url)
        super().__init__(f"Sitemap {self.url!r}: {detail}")


class SitemapFetchError(SitemapError):
    """Raised when the sitemap response cannot be parsed as a snapshot."""


class SitemapParseError(SitemapError):
    """Raised when sitemap XML is invalid."""


class SitemapIndexError(SitemapError):
    """Raised when a sitemap index is returned instead of a URL set."""


class SitemapSnapshot(BaseModel):
    """Group sitemap entries by the documentation host's ``/fr`` path.

    ``english_urls`` contains entries not under ``/fr`` on that host; admission
    filters the rest.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    sha256: str
    english_urls: tuple[str, ...]
    french_urls: tuple[str, ...]

    @property
    def total_count(self) -> int:
        return len(self.english_urls) + len(self.french_urls)


def fetch_sitemap(fetcher: Fetcher, url: str = SITEMAP_URL) -> SitemapSnapshot:
    """Fetch and parse the documentation sitemap."""
    result = fetcher.fetch(url, etag=None)
    if result.status_code in REDIRECT_STATUSES:
        raise SitemapFetchError(
            url,
            f"external redirect was not fetched: HTTP {result.status_code} "
            f"to {_safe_url(result.final_url)!r}",
        )
    if result.status_code != 200:
        raise SitemapFetchError(url, f"unexpected HTTP {result.status_code}")
    return parse_sitemap(result.body, result.final_url)


def parse_sitemap(payload: bytes, source_url: str) -> SitemapSnapshot:
    """Parse a sitemap URL set while preserving source order and duplicates."""
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise SitemapParseError(source_url, "invalid XML") from exc

    root_name = _local_name(root.tag)
    if root_name == "sitemapindex":
        raise SitemapIndexError(source_url, "sitemap indexes are not supported")
    if root_name != "urlset":
        raise SitemapParseError(source_url, f"expected urlset, got {root_name}")

    english: list[str] = []
    french: list[str] = []
    for index, element in enumerate(root, start=1):
        if _local_name(element.tag) != "url":
            raise SitemapParseError(
                source_url, f"unexpected {_local_name(element.tag)}"
            )
        location = _location(element, source_url, index)
        target = french if _is_french(location, source_url, index) else english
        target.append(location)

    return SitemapSnapshot(
        url=source_url,
        sha256=sha256(payload).hexdigest(),
        english_urls=tuple(english),
        french_urls=tuple(french),
    )


def _location(element: ElementTree.Element, source_url: str, index: int) -> str:
    locations = [child for child in element if _local_name(child.tag) == "loc"]
    if len(locations) != 1 or not locations[0].text or not locations[0].text.strip():
        raise SitemapParseError(source_url, f"entry {index} must contain one loc")
    return locations[0].text.strip()


def _is_french(location: str, source_url: str, index: int) -> bool:
    try:
        parts = urlsplit(location)
    except ValueError as exc:
        raise SitemapParseError(
            source_url, f"entry {index} has an invalid loc"
        ) from exc
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise SitemapParseError(source_url, f"entry {index} loc is not absolute")
    return parts.hostname == DOCS_HOST and (
        parts.path == "/fr" or parts.path.startswith("/fr/")
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1]
