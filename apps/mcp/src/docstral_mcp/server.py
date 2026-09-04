"""Expose Docstral's grounded answering boundary through MCP."""

from typing import Annotated, Protocol

from docstral_backend import AnswerResponse
from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from pydantic import Field


class _Answerer(Protocol):
    async def answer(self, question: str) -> AnswerResponse: ...


def create_server(answerer: _Answerer) -> FastMCP:
    """Create the read-only Docstral MCP server."""
    server = FastMCP(
        "Docstral",
        instructions=(
            "Use ask_docs to answer questions about Mistral's public documentation. "
            "Present its answer and citations without adding factual content."
        ),
    )

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
        """Answer from indexed documentation or abstain when evidence is insufficient."""
        response = await answerer.answer(question)
        content = response.answer
        if response.citations:
            sources = "\n".join(
                f"- [{citation.title}]({citation.url})"
                for citation in response.citations
            )
            content = f"{content}\n\nSources:\n{sources}"
        return ToolResult(
            content=content,
            structured_content=response.model_copy(
                update={"answer": content}
            ).model_dump(mode="json"),
        )

    return server
