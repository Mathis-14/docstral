"""Google authentication and invitation checks for the MCP boundary."""

from fastmcp.server.auth import AuthContext
from fastmcp.server.auth.providers.google import GoogleProvider
from pydantic import AnyHttpUrl, EmailStr, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class GoogleAuthConfig(BaseSettings):
    """OAuth settings supplied through the process environment, never a dotenv read."""

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
    oauth_signing_key: SecretStr = Field(min_length=32)

    @field_validator("allowed_emails", mode="before")
    @classmethod
    def parse_invites(cls, value: object) -> object:
        if isinstance(value, str):
            if "*" in value:
                raise ValueError("invites must be exact email addresses, not wildcards")
            return [email.strip().lower() for email in value.split(",")]
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

    def is_invited(self, context: AuthContext) -> bool:
        """Require a verified Google email and an exact invitation match."""
        if context.token is None:
            return False
        email = context.token.claims.get("email")
        verified = context.token.claims.get("email_verified")
        return (
            (verified is True or verified == "true")
            and isinstance(email, str)
            and email.lower() in self.allowed_emails
        )


def build_google_provider(config: GoogleAuthConfig) -> GoogleProvider:
    """Use FastMCP's OAuth proxy and its encrypted, persistent file store."""
    return GoogleProvider(
        client_id=config.google_client_id,
        client_secret=config.google_client_secret.get_secret_value(),
        base_url=config.oauth_base_url,
        required_scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
        jwt_signing_key=config.oauth_signing_key.get_secret_value(),
    )
