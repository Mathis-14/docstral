from typing import Literal, Self

from mistralai.search.toolkit.document import compute_id
from pydantic import BaseModel, ConfigDict, Field, model_validator

from docstral_worker.urls import canonicalize, is_docs_url


class SourceIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    document_id: str

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if (
            not is_docs_url(self.source_id)
            or canonicalize(self.source_id, self.source_id).url != self.source_id
        ):
            raise ValueError("source_id must be a canonical Docstral documentation URL")
        if self.document_id != compute_id(self.source_id):
            raise ValueError("document_id must match the toolkit ID of source_id")
        return self


class PageState(SourceIdentity):
    # An empty value keeps the URL in the inventory while a write is unconfirmed.
    index_hash: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")


class DiscoveryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    urls: tuple[str, ...]
    concurrency: int = Field(ge=1, le=8)
    max_pages: int = Field(ge=1, le=1000)
    request_delay: float = Field(ge=0)


class PageResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    status: Literal[
        "indexed", "unchanged", "redirected", "gone", "excluded", "extraction_failed"
    ]
    links: tuple[str, ...] = ()
    redirect_url: str | None = None
    reason: str | None = None


class DownloadedPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    html: bytes
    links: tuple[str, ...]


class RefreshResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    indexed: int = Field(ge=0)
    unchanged: int = Field(ge=0)
    changed: int = Field(ge=0)
    deleted: int = Field(ge=0)
    failed: int = Field(ge=0)
    failed_urls: tuple[str, ...]
    discovered: int = Field(ge=0)
    deletions_skipped: bool
    duration_seconds: float = Field(ge=0)
    status: Literal["complete", "partial"]
