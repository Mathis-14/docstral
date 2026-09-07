from hashlib import sha256
from typing import override

from bs4 import BeautifulSoup, Tag
from mistralai.search.toolkit.common.text import sanitize_text
from mistralai.search.toolkit.context import IngestContext
from mistralai.search.toolkit.document import (
    Document,
    DocumentChunk,
    DocumentChunkMetadata,
    compute_char_locator,
    compute_id,
)
from mistralai.search.toolkit.ingestion import File
from mistralai.search.toolkit.ingestion.extractors.base import DocumentExtractor
from mistralai.search.toolkit.ingestion.extractors.html_converter import (
    DEFAULT_IGNORE_CLASSES,
    MarkdownifyConverter,
)
from pydantic import BaseModel, ConfigDict, Field

from docstral_worker import IngestionError, _safe_url
from docstral_worker.crawler.crawl import SHA256_PATTERN

_TITLE_SUFFIX = " | Mistral Docs"
_CODE_LANGUAGES = frozenset({"curl", "python"})


class ExtractionError(IngestionError):
    pass


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


class DocsHtmlConverter:
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


_DEFAULT_CONTEXT = IngestContext()


class DocsChunkMetadata(DocumentChunkMetadata):
    model_config = ConfigDict(frozen=True)

    title: str
    content_hash: str


class DocsExtractor(DocumentExtractor):
    @override
    async def extract(
        self, file: File, context: IngestContext = _DEFAULT_CONTEXT
    ) -> Document:
        page = extract_page(file.source_id, file.raw)
        return Document(
            source_id=page.url,
            content=page.markdown,
            chunks=[
                DocumentChunk(
                    source_id=page.url,
                    locator=compute_char_locator(0, page.chars),
                    start_offset=0,
                    end_offset=page.chars,
                    parent_ref=compute_id(page.url),
                    content=page.markdown,
                    metadata=DocsChunkMetadata(
                        title=page.title,
                        content_hash=page.content_hash,
                    ),
                )
            ],
        )
