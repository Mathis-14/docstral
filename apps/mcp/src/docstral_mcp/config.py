from typing import Annotated

from pydantic import AnyHttpUrl, EmailStr, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_TOP_K = 5
DEFAULT_VESPA_ENDPOINT = "http://localhost:8080"
DEFAULT_ANSWER_MODEL = "ministral-8b-2512"
MAX_ANSWER_TOKENS = 1024


class ServerConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DOCSTRAL_", frozen=True, extra="forbid"
    )

    host: str = Field(min_length=1, pattern=r"\S")
    port: int = Field(ge=1, le=65535)
    top_k: int = Field(ge=1)
    vespa_endpoint: AnyHttpUrl
    answer_model: str = Field(default=DEFAULT_ANSWER_MODEL, min_length=1, pattern=r"\S")


class GoogleAuthConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DOCSTRAL_",
        frozen=True,
        extra="forbid",
        hide_input_in_errors=True,
        enable_decoding=False,
    )

    google_client_id: str = Field(min_length=1, pattern=r"\S")
    google_client_secret: SecretStr = Field(min_length=1)
    oauth_base_url: AnyHttpUrl
    allowed_emails: frozenset[EmailStr] = Field(min_length=1)
    allowed_domains: frozenset[
        Annotated[
            str,
            Field(
                max_length=253,
                pattern=r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
            ),
        ]
    ] = frozenset()
    oauth_signing_key: SecretStr = Field(min_length=32)

    @field_validator("allowed_emails", "allowed_domains", mode="before")
    @classmethod
    def parse_invites(cls, value: object) -> object:
        if isinstance(value, str):
            if "*" in value:
                raise ValueError(
                    "invites must be exact email addresses or domains, not wildcards"
                )
            return (
                [item.strip().lower() for item in value.split(",")]
                if value.strip()
                else []
            )
        return value

    @field_validator("google_client_secret", "oauth_signing_key")
    @classmethod
    def reject_blank_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("oauth_base_url")
    @classmethod
    def validate_public_origin(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.scheme != "https" and value.host not in (
            "localhost",
            "127.0.0.1",
            "[::1]",
        ):
            raise ValueError("must use HTTPS except on loopback")
        if (
            value.username is not None
            or value.password is not None
            or value.query is not None
            or value.fragment is not None
            or value.path not in (None, "/")
        ):
            raise ValueError("must be an origin without credentials, path or query")
        return value
