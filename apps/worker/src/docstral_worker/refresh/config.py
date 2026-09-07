import os

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field


class RefreshConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    vespa_endpoint: AnyHttpUrl
    concurrency: int = Field(default=2, ge=1, le=8)
    max_pages: int = Field(default=1000, ge=1, le=1000)
    request_delay: float = Field(default=0.25, ge=0, allow_inf_nan=False)


def refresh_config() -> RefreshConfig:
    return RefreshConfig.model_validate(
        {
            "vespa_endpoint": os.environ.get("VESPA_ENDPOINT"),
            "concurrency": os.environ.get("DOCSTRAL_REFRESH_CONCURRENCY", "2"),
            "max_pages": os.environ.get("DOCSTRAL_REFRESH_MAX_PAGES", "1000"),
            "request_delay": os.environ.get("DOCSTRAL_CRAWL_DELAY", "0.25"),
        }
    )
