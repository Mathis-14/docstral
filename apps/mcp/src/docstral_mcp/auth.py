from fastmcp.server.auth import AuthContext
from fastmcp.server.auth.providers.google import GoogleProvider

from docstral_mcp.config import GoogleAuthConfig


def build_google_provider(config: GoogleAuthConfig) -> GoogleProvider:
    return GoogleProvider(
        client_id=config.google_client_id,
        client_secret=config.google_client_secret.get_secret_value(),
        base_url=config.oauth_base_url,
        required_scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
        jwt_signing_key=config.oauth_signing_key.get_secret_value(),
    )


def is_invited(config: GoogleAuthConfig, context: AuthContext) -> bool:
    if context.token is None:
        return False
    email = context.token.claims.get("email")
    verified = context.token.claims.get("email_verified")
    return (
        (verified is True or verified == "true")
        and isinstance(email, str)
        and email.lower() in config.allowed_emails
    )
