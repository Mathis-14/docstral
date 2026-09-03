from enum import StrEnum
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict

from docstral_worker import IngestionError, _safe_url

DOCS_HOST = "docs.mistral.ai"

ASSET_SUFFIXES = tuple(
    f".{suffix}"
    for suffix in (
        "avif bmp css csv gif gz ico jpeg jpg js json map md mdx mjs mov mp3 "
        "mp4 ogg otf pdf png svg tar tgz tsv txt wav webm webmanifest webp "
        "woff woff2 xml yaml yml zip"
    ).split()
)
EXCLUDED_ROUTES = (
    ("/api", False),
    ("/api/endpoint", True),
    ("/resources/cookbooks", True),
    ("/resources/deprecated", True),
    ("/vibe/chat-legacy", True),
)


class UrlCanonicalizationError(IngestionError):
    """Raised when a URL cannot be parsed safely."""

    def __init__(self, url: str, base: str) -> None:
        self.url = _safe_url(url)
        self.base = _safe_url(base)
        super().__init__(f"Cannot canonicalize {self.url!r} against {self.base!r}")


class RejectionReason(StrEnum):
    OUTSIDE_HOST = "outside_host"
    FRENCH = "french"
    ASSET = "asset"
    EXCLUDED_ROUTE = "excluded_route"
    NON_HTML = "non_html"
    GONE = "gone"
    DUPLICATE = "duplicate"
    ROBOTS_DISALLOWED = "robots_disallowed"


class CanonicalUrl(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    anchor: str | None = None


class AdmissionDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    reason: RejectionReason | None = None

    @property
    def admitted(self) -> bool:
        return self.reason is None


def canonicalize(url: str, base: str) -> CanonicalUrl:
    """Resolve a URL, removing its query and userinfo and returning its fragment."""
    try:
        parts = urlsplit(urljoin(base, url))
        scheme = parts.scheme.lower()
        netloc = _normalized_netloc(parts, scheme)
    except ValueError as exc:
        raise UrlCanonicalizationError(url, base) from exc

    path = parts.path or ("/" if netloc else "")
    if parts.hostname and parts.hostname.lower() == DOCS_HOST:
        scheme = "https"
        path = path.lower()
        if path == "/en":
            path = "/"
        elif path.startswith("/en/"):
            path = path[3:]
    path = path.rstrip("/") or "/"

    canonical = urlunsplit((scheme, netloc, path, "", ""))
    return CanonicalUrl(url=canonical, anchor=parts.fragment or None)


def admit(url: CanonicalUrl) -> AdmissionDecision:
    """Apply the version 1 URL admission rules without retaining state."""
    if not is_docs_url(url.url):
        return _rejected(url, RejectionReason.OUTSIDE_HOST)
    parts = urlsplit(url.url)
    path = parts.path or "/"
    if path == "/fr" or path.startswith("/fr/"):
        return _rejected(url, RejectionReason.FRENCH)
    if path.lower().endswith(ASSET_SUFFIXES):
        return _rejected(url, RejectionReason.ASSET)
    if any(
        path == route or (with_children and path.startswith(f"{route}/"))
        for route, with_children in EXCLUDED_ROUTES
    ):
        return _rejected(url, RejectionReason.EXCLUDED_ROUTE)
    return AdmissionDecision(url=url.url)


def is_docs_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (
            parts.scheme == "https"
            and parts.hostname == DOCS_HOST
            and parts.port in (None, 443)
            and parts.username is None
            and parts.password is None
        )
    except ValueError:
        return False


def _normalized_netloc(parts: SplitResult, scheme: str) -> str:
    host = parts.hostname
    if host is None:
        return parts.netloc.lower()
    normalized_host = f"[{host}]" if ":" in host else host.lower()
    port = parts.port
    is_default_port = (scheme, port) in {("http", 80), ("https", 443)}
    return (
        normalized_host
        if port is None or is_default_port
        else f"{normalized_host}:{port}"
    )


def _rejected(url: CanonicalUrl, reason: RejectionReason) -> AdmissionDecision:
    return AdmissionDecision(url=url.url, reason=reason)
