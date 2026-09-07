import argparse
import logging
from collections.abc import Sequence

from pydantic import ValidationError

from docstral_mcp.config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_TOP_K,
    DEFAULT_VESPA_ENDPOINT,
    GoogleAuthConfig,
    ServerConfig,
)
from docstral_mcp.qa import build_documentation_answerer
from docstral_mcp.server import create_server


def main(argv: Sequence[str] | None = None) -> int:
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
        model=config.answer_model,
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
