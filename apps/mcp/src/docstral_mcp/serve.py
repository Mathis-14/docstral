"""Run the Docstral MCP server over Streamable HTTP."""

import argparse
import logging
from collections.abc import Sequence

from docstral_backend import build_documentation_answerer
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, ValidationError

from docstral_mcp.auth import GoogleAuthConfig
from docstral_mcp.server import create_server

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_TOP_K = 5
DEFAULT_VESPA_ENDPOINT = "http://localhost:8080"


class ServerConfig(BaseModel):
    """Validated runtime configuration for the local MCP server."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1, pattern=r"\S")
    port: int = Field(ge=1, le=65535)
    top_k: int = Field(ge=1)
    vespa_endpoint: AnyHttpUrl


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Docstral MCP server."""
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        config = ServerConfig(
            host=args.host,
            port=args.port,
            top_k=args.top_k,
            vespa_endpoint=args.vespa_endpoint,
        )
        oauth = GoogleAuthConfig() if args.auth == "google" else None
    except ValidationError as exc:
        parser.error(str(exc))
    if oauth is not None:
        # Google's tokeninfo URL contains the access token.
        logging.getLogger("httpx2").setLevel(logging.WARNING)
    answerer = build_documentation_answerer(
        vespa_endpoint=str(config.vespa_endpoint).rstrip("/"),
        top_k=config.top_k,
    )
    try:
        create_server(answerer, oauth=oauth).run(
            transport="http",
            host=config.host,
            port=config.port,
            path="/mcp",
            json_response=True,
            stateless_http=True,
            uvicorn_config={"access_log": oauth is None},
        )
    except KeyboardInterrupt:
        pass
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docstral-mcp",
        description="Serve grounded documentation Q&A through MCP.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="server bind host")
    parser.add_argument(
        "--auth", choices=("none", "google"), default="none", help="authentication mode"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="server bind port",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help="documentation chunks retrieved per question",
    )
    parser.add_argument(
        "--vespa-endpoint",
        default=DEFAULT_VESPA_ENDPOINT,
        help="Vespa query endpoint",
    )
    return parser
