import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from docstral_worker import IngestionError
from docstral_worker.cli import main
from docstral_worker.maintenance import PublicationState
from docstral_worker.workflows import RefreshDocumentation, refresh_documentation
from mistralai import workflows
from temporalio.api.failure.v1 import Failure
from temporalio.converter import DataConverter
from temporalio.testing import ActivityEnvironment
from test_ingest import _FakeEmbedder
from test_kubernetes import _Apps, _Core
from test_refresh import crawl_transport as crawl_transport


def test_workflow_loads_in_mistrals_deterministic_sandbox() -> None:
    spec = workflows.get_workflow_definition(RefreshDocumentation)
    assert spec.name == "docstral-refresh"
    assert spec.enforce_determinism
    assert not spec.input_schema.get("properties")
    assert spec.schedules == []  # Starting a worker must not schedule paid ingestion.
    # SDK registration metadata, exposed dynamically by its activity decorator.
    parameters: object = vars(refresh_documentation)["__wf_activity_params__"]
    assert isinstance(parameters, dict)
    assert parameters["retry_policy_max_attempts"] == 1
    # MCP's beartype import hook does not run in the separate worker process.
    script = """
import asyncio
from docstral_worker.workflows import RefreshDocumentation
from mistralai.workflows.core.sandbox import get_sandbox_restrictions
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner
from temporalio.workflow import _Definition

async def check():
    SandboxedWorkflowRunner(restrictions=get_sandbox_restrictions()).prepare_workflow(
        _Definition.must_from_class(RefreshDocumentation)
    )
asyncio.run(check())
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "LOG_LEVEL": "ERROR"},
    )
    assert completed.returncode == 0, completed.stderr


@pytest.fixture
def cluster_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "DOCSTRAL_DATA_DIR": str(tmp_path),
        "VESPA_ENDPOINT": "http://vespa.test",
        "POD_NAMESPACE": "docstral",
        "MCP_DEPLOYMENT": "mcp",
        "DEPLOYMENT_NAME": "docstral-test",
    }.items():
        monkeypatch.setenv(name, value)


@pytest.mark.usefixtures("cluster_env")
@pytest.mark.parametrize("feed_fails", [False, True])
async def test_native_activity_runs_real_refresh_and_sanitizes_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crawl_transport: list[httpx.Request],
    feed_fails: bool,
) -> None:
    from kubernetes.aio import client, config  # type: ignore[import-untyped]

    apps = _Apps()
    monkeypatch.setattr(config, "load_incluster_config", lambda: None)
    monkeypatch.setattr(client, "AppsV1Api", lambda api: apps)
    monkeypatch.setattr(client, "CoreV1Api", lambda api: _Core())

    def embedder_factory(*, model_name: str, max_retry: int) -> _FakeEmbedder:
        assert (model_name, max_retry) == ("mistral-embed", 6)
        return _FakeEmbedder()

    monkeypatch.setattr("docstral_worker.publish.MistralEmbedder", embedder_factory)
    feeds: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "vespa.test"
        if request.method == "POST":
            feeds.append(request)
            if feed_fails:
                raise RuntimeError("private-api-credential")
        return httpx.Response(200, json={"documents": [], "documentCount": 0})

    original = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return original(
            base_url=str(kwargs["base_url"]), transport=httpx.MockTransport(handler)
        )

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    environment = ActivityEnvironment()
    beats: list[tuple[object, ...]] = []
    environment.on_heartbeat = lambda *details: beats.append(details)
    if feed_fails:
        with pytest.raises(
            IngestionError, match="Documentation refresh failed"
        ) as error:
            await environment.run(refresh_documentation)
        failure = Failure()
        await DataConverter.default.encode_failure(error.value, failure)
        assert "private-api-credential" not in str(failure)
        assert len(feeds) == 1  # No second destructive cycle on activity failure.
        assert len(apps.patches) == 1
    else:
        result = await environment.run(refresh_documentation)
        assert (result.indexed, result.failed) == (1, 0)
        assert len(apps.patches) == 2
    assert beats and all(details == () for details in beats)
    assert crawl_transport
    assert PublicationState(tmp_path).pending.exists() == feed_fails


@pytest.mark.usefixtures("cluster_env")
def test_cli_redacts_sensitive_http_traces() -> None:
    # SDK settings load at import time; exercise the real CLI in a fresh process.
    script = """
from docstral_worker.cli import main

try:
    main(["workflows"])
except SystemExit as error:
    assert error.code == 2  # Invalid endpoint stops before any remote connection.
else:
    raise AssertionError("Expected configuration rejection")

import httpx
from mistralai.workflows.core.config import config
from mistralai.workflows.core.tracing._otel_config import _apply_span_redaction
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

assert config.common.otel_redaction == "strict"
exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(
    _apply_span_redaction(exporter, config.common.otel_redaction)
))
def fail(request):
    raise RuntimeError("private-api-credential")

try:
    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        # MockTransport bypasses global network instrumentation.
        HTTPXClientInstrumentor.instrument_client(client, tracer_provider=provider)
        try:
            client.get("https://vespa.test/")
        except RuntimeError:
            pass
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert "private-api-credential" not in spans[0].to_json()
finally:
    provider.shutdown()
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "OTEL_REDACTION": "none",
            "VESPA_ENDPOINT": "invalid-endpoint",
            "MISTRAL_API_KEY": "test-key",
            "LOG_LEVEL": "ERROR",
        },
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.usefixtures("cluster_env")
def test_cli_only_starts_native_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[type[object]]] = []

    async def run_worker(definitions: list[type[object]]) -> None:
        calls.append(definitions)

    monkeypatch.setattr(workflows, "run_worker", run_worker)
    assert main(["workflows"]) == 0
    assert calls == [[RefreshDocumentation]]


@pytest.mark.usefixtures("cluster_env")
def test_worker_requires_deployment_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("DEPLOYMENT_NAME")
    assert main(["workflows"]) == 1
    assert "DEPLOYMENT_NAME" in capsys.readouterr().out


@pytest.mark.usefixtures("cluster_env")
def test_worker_configuration_errors_hide_input(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("VESPA_ENDPOINT", "credential-do-not-print")
    with pytest.raises(SystemExit) as error:
        main(["workflows"])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert "vespa_endpoint" in output.err
    assert "credential-do-not-print" not in output.err + output.out
