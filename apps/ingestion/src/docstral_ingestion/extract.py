"""Convert the stored pages of a snapshot to Markdown."""

from hashlib import sha256
from pathlib import Path
from time import monotonic

import structlog
from bs4 import BeautifulSoup, Tag
from mistralai.search.toolkit.common.text import sanitize_text
from mistralai.search.toolkit.ingestion.extractors.html_converter import (
    DEFAULT_IGNORE_CLASSES,
    MarkdownifyConverter,
)
from pydantic import BaseModel, ConfigDict, Field

from docstral_ingestion import IngestionError, _safe_url
from docstral_ingestion.crawl import SHA256_PATTERN, PageDecision
from docstral_ingestion.snapshot import CurrentSnapshot, page_slug

_TITLE_SUFFIX = " | Mistral Docs"
_CODE_LANGUAGES = frozenset({"curl", "python"})


class ExtractionError(IngestionError):
    """Raised when a documentation page cannot be extracted."""


class Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    level: int = Field(ge=1, le=6)
    heading: str
    anchor: str | None


class ExtractedPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    title: str
    markdown: str
    sections: tuple[Section, ...]
    content_hash: str = Field(pattern=SHA256_PATTERN)
    chars: int = Field(ge=0)


class ExtractResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    converted: int = Field(ge=0)
    failed: int = Field(ge=0)
    duration_seconds: float = Field(ge=0.0)


class DocsHtmlConverter:
    """Select Docstral's content subtree before toolkit conversion."""

    def __init__(self) -> None:
        self._converter = MarkdownifyConverter(
            ignore_classes=[*DEFAULT_IGNORE_CLASSES, "^hidden$"]
        )

    def convert(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        articles = _outer_articles(soup)
        if not articles:
            raise ExtractionError("no main article.prose")
        for article in articles:
            for heading in article.select(":is(h1,h2,h3,h4,h5,h6):has(> button)"):
                heading.decompose()
            _label_code_blocks(article)
        return "\n\n".join(
            self._converter.convert(str(article)) for article in articles
        )


_CONVERTER = DocsHtmlConverter()


def outline(html: str) -> tuple[str, tuple[Section, ...]]:
    """Read the page title and anchored heading outline from rendered HTML."""
    soup = BeautifulSoup(html, "html.parser")
    if soup.title is None:
        raise ExtractionError("no title")
    title = soup.title.get_text(" ", strip=True).removesuffix(_TITLE_SUFFIX).strip()
    sections = tuple(
        Section(
            level=int(heading.name[1]),
            heading=heading.get_text(" ", strip=True),
            anchor=_heading_anchor(heading),
        )
        for article in _outer_articles(soup)
        for heading in article.select("h1, h2, h3, h4, h5, h6")
    )
    return title, sections


def extract_page(url: str, html: bytes) -> ExtractedPage:
    """Extract one UTF-8 HTML page to auditable Markdown."""
    try:
        decoded = html.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ExtractionError(f"Invalid UTF-8 HTML for {_safe_url(url)!r}") from exc

    try:
        title, sections = outline(decoded)
        markdown = sanitize_text(_CONVERTER.convert(decoded))
        if not markdown:
            raise ExtractionError(f"Empty Markdown for {_safe_url(url)!r}")
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"Cannot convert {_safe_url(url)!r}: {exc}") from exc

    return ExtractedPage(
        url=url,
        title=title,
        markdown=markdown,
        sections=sections,
        content_hash=sha256(markdown.encode()).hexdigest(),
        chars=len(markdown),
    )


def extract_snapshot(snapshot: CurrentSnapshot, destination: Path) -> ExtractResult:
    """Convert every stored page in a raw snapshot and write its Markdown."""
    if destination.exists():
        raise ExtractionError(f"Extraction output {str(destination)!r} already exists")
    logger = structlog.get_logger(__name__)
    started_at = monotonic()
    converted = 0
    failed = 0
    stored = [
        page for page in snapshot.manifest.pages if page.decision is PageDecision.STORED
    ]
    try:
        pages_directory = destination / "pages"
        pages_directory.mkdir(parents=True)
        for entry in stored:
            page_started_at = monotonic()
            try:
                cached = snapshot.get(entry.canonical_url)
                if cached is None:
                    raise ExtractionError(
                        f"Stored page {entry.canonical_url!r} missing from snapshot"
                    )
                if sha256(cached.body).hexdigest() != cached.raw_sha256:
                    raise ExtractionError(
                        f"Raw HTML for {entry.canonical_url!r} does not match its "
                        "recorded SHA-256"
                    )
                page = extract_page(entry.canonical_url, cached.body)
                slug = page_slug(entry.canonical_url)
                (pages_directory / f"{slug}.md").write_text(
                    page.markdown, encoding="utf-8"
                )
            except IngestionError as exc:
                failed += 1
                logger.info(
                    "extraction_page",
                    url=entry.canonical_url,
                    decision="failed",
                    duration_ms=round((monotonic() - page_started_at) * 1_000, 3),
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                continue
            converted += 1
            logger.info(
                "extraction_page",
                url=entry.canonical_url,
                decision="converted",
                duration_ms=round((monotonic() - page_started_at) * 1_000, 3),
                chars=page.chars,
            )
    except OSError as exc:
        raise ExtractionError(
            f"Cannot write extraction output {str(destination)!r}: {exc}"
        ) from exc
    return ExtractResult(
        converted=converted,
        failed=failed,
        duration_seconds=monotonic() - started_at,
    )


def _outer_articles(soup: BeautifulSoup) -> tuple[Tag, ...]:
    return tuple(
        article
        for article in soup.select("main article.prose")
        if article.find_parent("article", class_="prose") is None
    )


def _label_code_blocks(article: Tag) -> None:
    for code in article.select("pre code"):
        for parent in code.parents:
            language = parent.get("data-language")
            if isinstance(language, str) and language.casefold() in _CODE_LANGUAGES:
                code["class"] = f"language-{language.casefold()}"
                break
            if parent is article:
                break


def _heading_anchor(heading: Tag) -> str | None:
    anchor = heading.get("id")
    if isinstance(anchor, str):
        return anchor
    parent = heading.parent
    if isinstance(parent, Tag) and parent.name == "div":
        parent_anchor = parent.get("id")
        return parent_anchor if isinstance(parent_anchor, str) else None
    return None
