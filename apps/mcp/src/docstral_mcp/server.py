from functools import partial
from typing import Annotated, Protocol

from fastmcp import FastMCP
from fastmcp.server.middleware import AuthMiddleware
from fastmcp.tools import ToolResult
from pydantic import Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from docstral_mcp.auth import build_google_provider, is_invited
from docstral_mcp.config import GoogleAuthConfig
from docstral_mcp.qa import AnswerResponse


class _Answerer(Protocol):
    async def answer(self, question: str) -> AnswerResponse: ...


def _to_tool_result(response: AnswerResponse) -> ToolResult:
    content = response.answer
    if response.citations:
        sources = "\n".join(
            f"- [{citation.title}]({citation.url})" for citation in response.citations
        )
        content = f"{content}\n\nSources:\n{sources}"
    return ToolResult(
        content=content,
        structured_content=response.model_copy(update={"answer": content}).model_dump(
            mode="json"
        ),
    )


def create_server(
    answerer: _Answerer, *, oauth: GoogleAuthConfig | None = None
) -> FastMCP:
    server = FastMCP(
        "Docstral",
        auth=build_google_provider(oauth) if oauth is not None else None,
        middleware=[AuthMiddleware(auth=partial(is_invited, oauth))]
        if oauth is not None
        else [],
        instructions=(
            "Use ask_docs to answer questions about Mistral's public documentation. "
            "Present its answer and citations without adding factual content."
        ),
    )

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    @server.tool(
        name="ask_docs",
        title="Ask Mistral documentation",
        description=(
            "Answer questions using Docstral's indexed Mistral documentation. "
            "Treat the tool result as final: present its answer and every citation "
            "without adding, correcting, or supplementing factual content."
        ),
        output_schema=AnswerResponse.model_json_schema(),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    async def ask_docs(
        question: Annotated[
            str,
            Field(
                min_length=1,
                pattern=r"\S",
                description="Question about Mistral's public documentation.",
            ),
        ],
    ) -> ToolResult:
        response = await answerer.answer(question)
        return _to_tool_result(response)

    return server
