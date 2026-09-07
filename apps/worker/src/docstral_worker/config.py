import os
from pathlib import Path

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

MAX_PAGES = 2_000

DEFAULT_SNAPSHOTS = Path("data/snapshots")

DEFAULT_EXTRACTED = Path("data/extracted")

DEFAULT_VESPA_ENDPOINT = "http://localhost:8080"


class RefreshConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    vespa_endpoint: AnyHttpUrl
    concurrency: int = Field(default=2, ge=1, le=8)
    max_pages: int = Field(default=1000, ge=1, le=1000)
    request_delay: float = Field(default=0.25, ge=0, allow_inf_nan=False)


class CrawlConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    out: Path = Path("data/snapshots")
    delay: float = Field(default=0.25, ge=0, allow_inf_nan=False)
    max_pages: int = Field(default=MAX_PAGES, ge=1, le=MAX_PAGES)


class ExtractConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshots: Path = DEFAULT_SNAPSHOTS
    out: Path = DEFAULT_EXTRACTED


class IngestConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshots: Path = DEFAULT_SNAPSHOTS
    vespa_endpoint: str = DEFAULT_VESPA_ENDPOINT


class PipelineConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Bump when extraction semantics change independently of these settings.
    version: str = Field(default="1.0.0", min_length=1)
    chunk_size: int = Field(default=800, gt=0)
    chunk_max_size: int = Field(default=800, gt=0)
    chunk_overlap: int = Field(default=0, ge=0)


def refresh_config() -> RefreshConfig:
    return RefreshConfig.model_validate(
        {
            "vespa_endpoint": os.environ.get("VESPA_ENDPOINT"),
            "concurrency": os.environ.get("DOCSTRAL_REFRESH_CONCURRENCY", "2"),
            "max_pages": os.environ.get("DOCSTRAL_REFRESH_MAX_PAGES", "1000"),
            "request_delay": os.environ.get("DOCSTRAL_CRAWL_DELAY", "0.25"),
        }
    )
