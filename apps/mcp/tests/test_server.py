from docstral_backend import AnswerResponse, Citation
from docstral_mcp import create_server
from fastmcp import Client
from mcp.types import TextContent


class _FakeAnswerer:
    def __init__(
        self,
        response: AnswerResponse,
        *,
        error: RuntimeError | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.questions: list[str] = []

    async def answer(self, question: str) -> AnswerResponse:
        self.questions.append(question)
        if self.error is not None:
            raise self.error
        return self.response


async def test_server_exposes_one_read_only_grounded_answer_tool() -> None:
    response = AnswerResponse(
        answer="Use an API key.",
        abstained=False,
        citations=(
            Citation.model_validate(
                {
                    "title": "API keys",
                    "url": "https://docs.mistral.ai/getting-started/quickstart",
                }
            ),
        ),
    )
    answerer = _FakeAnswerer(response)

    async with Client(create_server(answerer)) as client:
        tools = await client.list_tools()
        result = await client.call_tool(
            "ask_docs",
            {"question": "How do I authenticate?"},
        )

    assert [tool.name for tool in tools] == ["ask_docs"]
    tool = tools[0]
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.open_world_hint is False
    assert tool.description == (
        "Answer questions using Docstral's indexed Mistral documentation. "
        "Treat the tool result as final: present its answer and every citation "
        "without adding, correcting, or supplementing factual content."
    )
    assert tool.input_schema["properties"]["question"]["minLength"] == 1
    assert tool.input_schema["properties"]["question"]["pattern"] == r"\S"
    assert tool.output_schema is not None
    assert tool.output_schema["required"] == ["answer", "abstained", "citations"]
    assert result.is_error is False
    assert result.structured_content == response.model_copy(
        update={
            "answer": (
                "Use an API key.\n\n"
                "Sources:\n"
                "- [API keys](https://docs.mistral.ai/getting-started/quickstart)"
            )
        }
    ).model_dump(mode="json")
    assert result.content == [
        TextContent(
            type="text",
            text=(
                "Use an API key.\n\n"
                "Sources:\n"
                "- [API keys](https://docs.mistral.ai/getting-started/quickstart)"
            ),
        )
    ]
    assert answerer.questions == ["How do I authenticate?"]


async def test_server_rejects_blank_question_before_answering() -> None:
    response = AnswerResponse(
        answer=(
            "I couldn't find enough information in the Mistral documentation to "
            "answer this question."
        ),
        abstained=True,
        citations=(),
    )
    answerer = _FakeAnswerer(response)

    async with Client(create_server(answerer)) as client:
        result = await client.call_tool(
            "ask_docs",
            {"question": "   "},
            raise_on_error=False,
        )

    assert result.is_error is True
    assert answerer.questions == []


async def test_server_preserves_explicit_abstention() -> None:
    response = AnswerResponse(
        answer=(
            "I couldn't find enough information in the Mistral documentation to "
            "answer this question."
        ),
        abstained=True,
        citations=(),
    )

    async with Client(create_server(_FakeAnswerer(response))) as client:
        result = await client.call_tool("ask_docs", {"question": "Grow potatoes?"})

    assert result.is_error is False
    assert result.structured_content == response.model_dump(mode="json")
    assert result.content == [TextContent(type="text", text=response.answer)]


async def test_server_reports_answering_failure_as_tool_error() -> None:
    response = AnswerResponse(
        answer=(
            "I couldn't find enough information in the Mistral documentation to "
            "answer this question."
        ),
        abstained=True,
        citations=(),
    )
    answerer = _FakeAnswerer(response, error=RuntimeError("Vespa unavailable"))

    async with Client(create_server(answerer)) as client:
        result = await client.call_tool(
            "ask_docs",
            {"question": "Question?"},
            raise_on_error=False,
        )

    assert result.is_error is True
    assert result.structured_content is None
    assert answerer.questions == ["Question?"]
