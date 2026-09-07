import os
import shutil
import subprocess
import sys
from pathlib import Path

import docstral_mcp.serve as serve
import pytest
from docstral_mcp.config import ServerConfig
from fastmcp import FastMCP
from pydantic import AnyHttpUrl


@pytest.mark.parametrize("model", [None, "mistral-small-2603"])
def test_answer_model_setting(
    model: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOCSTRAL_ANSWER_MODEL", raising=False)
    if model is not None:
        monkeypatch.setenv("DOCSTRAL_ANSWER_MODEL", model)

    config = ServerConfig(
        host="127.0.0.1",
        port=8000,
        top_k=5,
        vespa_endpoint=AnyHttpUrl("http://localhost:8080"),
    )

    assert config.answer_model == (model or "ministral-8b-2512")


@pytest.mark.parametrize("model", ["", "   "])
def test_command_rejects_blank_model(
    model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOCSTRAL_ANSWER_MODEL", model)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)

    with pytest.raises(SystemExit) as caught:
        serve.main([])

    assert caught.value.code == 2


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
        "uvicorn_config": {"access_log": True},
    }


@pytest.mark.parametrize(
    ("content", "message"),
    [
        pytest.param(None, "Cannot read the bundled Q&A prompt", id="missing"),
        pytest.param(b"\xff", "Cannot read the bundled Q&A prompt", id="invalid-utf8"),
        pytest.param(b" \n", "The bundled Q&A prompt is empty", id="empty"),
    ],
)
def test_startup_rejects_broken_prompt_bundle(
    tmp_path: Path, content: bytes | None, message: str
) -> None:
    package = tmp_path / "docstral_mcp"
    shutil.copytree(
        Path(__file__).parents[1] / "src/docstral_mcp",
        package,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    prompt = package / "qa/prompt.md"
    if content is None:
        prompt.unlink()
    else:
        prompt.write_bytes(content)
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "from docstral_mcp.serve import main; main([])",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path), "MISTRAL_API_KEY": "test-key"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert message in result.stderr
    assert "reinstall or rebuild docstral-mcp" in result.stderr
