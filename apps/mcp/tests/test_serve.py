import docstral_mcp.serve as serve
import pytest
from fastmcp import FastMCP


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["--host", ""], id="empty-host"),
        pytest.param(["--host", "   "], id="blank-host"),
        pytest.param(["--port", "0"], id="invalid-port"),
        pytest.param(["--top-k", "0"], id="invalid-top-k"),
        pytest.param(
            ["--vespa-endpoint", "ftp://localhost:8080"],
            id="invalid-vespa-endpoint",
        ),
        pytest.param(
            ["--vespa-endpoint", "http://localhost:invalid"],
            id="invalid-vespa-port",
        ),
        pytest.param(
            ["--vespa-endpoint", "http://localhost:99999"],
            id="out-of-range-vespa-port",
        ),
        pytest.param(
            ["--vespa-endpoint", "http://:8080"],
            id="missing-vespa-host",
        ),
    ],
)
def test_command_rejects_invalid_configuration(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        serve.main(arguments)

    assert caught.value.code == 2


def test_command_runs_fastmcp_with_http_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_options: dict[str, object] = {}

    def run(_server: FastMCP, **options: object) -> None:
        run_options.update(options)

    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    monkeypatch.setattr(FastMCP, "run", run)

    result = serve.main(
        [
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
            "--top-k",
            "7",
            "--vespa-endpoint",
            "http://vespa.test:8080",
        ]
    )

    assert result == 0
    assert run_options == {
        "host": "0.0.0.0",
        "json_response": True,
        "path": "/mcp",
        "port": 9000,
        "stateless_http": True,
        "transport": "http",
    }
