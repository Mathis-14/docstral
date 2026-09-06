import asyncio
import json
import logging
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import httpx
import pytest
from docstral_worker import IngestionError
from docstral_worker.cli import main
from docstral_worker.prepared import StageRef
from docstral_worker.workflows import (
    RefreshContext,
    RefreshDocumentation,
    compare_hashes,
    crawl,
    embed,
    extract,
    index_delta,
    split,
)
from mistralai import workflows
from mistralai.workflows.core._graph import build_graph_dynamically
from mistralai.workflows.core.logging import (
    LogFormat,
    LogLevel,
    build_json_log_formatter,
)
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.trace import TracerProvider
from temporalio.api.failure.v1 import Failure
from temporalio.converter import DataConverter
from temporalio.testing import ActivityEnvironment
from test_incremental import _snapshot
from test_ingest import _html
from test_refresh import crawl_transport as crawl_transport


def test_workflow_loads_in_mistrals_deterministic_sandbox() -> None:
    spec = workflows.get_workflow_definition(RefreshDocumentation)
    assert spec.name == "docstral-refresh"
    assert spec.enforce_determinism
    assert not spec.input_schema.get("properties")
    assert spec.schedules == []  # Starting a worker must not schedule paid ingestion.
    for activity in (crawl, extract, compare_hashes, split, embed, index_delta):
        # Registration metadata is exposed dynamically by the SDK decorator.
        parameters: object = vars(activity)["__wf_activity_params__"]
        assert isinstance(parameters, dict)
        assert parameters["retry_policy_max_attempts"] == 1
        assert parameters["start_to_close_timeout_seconds"] == 55 * 60
        assert parameters["heartbeat_timeout_seconds"] == 60
    graph = build_graph_dynamically(RefreshDocumentation)
    activities = [node for node in graph.nodes if node.type == "activity"]
    assert [node.name for node in activities] == [
        "crawl",
        "extract",
        "compare_hashes",
        "split",
        "embed",
        "index_delta",
    ]
    assert not graph.incomplete
    edges = {(edge.from_, edge.to) for edge in graph.edges}
    assert all((left.id, right.id) in edges for left, right in pairwise(activities))
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
        "DEPLOYMENT_NAME": "docstral-test",
        "MISTRAL_API_KEY": "test-key",
        "MISTRAL_API_URL": "https://mistral.test",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("POD_NAMESPACE", raising=False)
    monkeypatch.delenv("MCP_DEPLOYMENT", raising=False)


class _Services:
    """Only the HTTP boundaries are replaced; SDK and ingestion stay real."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.clients: list[httpx.AsyncClient] = []
        self.fail: str | None = None
        self.hold_embeddings = False

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "mistral.test":
            assert request.url.path == "/v1/embeddings"
            assert request.method == "POST"
            if self.hold_embeddings:
                await asyncio.sleep(10)
            if self.fail == "embed":
                return httpx.Response(400, json={"message": "private-api-credential"})
            payload: object = json.loads(request.content)
            assert isinstance(payload, dict)
            assert payload["model"] == "mistral-embed"
            inputs = payload["input"]
            assert isinstance(inputs, list)
            return httpx.Response(
                200,
                json={
                    "id": "embedding-test",
                    "object": "list",
                    "model": "mistral-embed",
                    "data": [
                        {
                            "object": "embedding",
                            "index": index,
                            "embedding": [1.0, *([0.0] * 1023)],
                        }
                        for index in range(len(inputs))
                    ],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                },
            )
        assert request.url.host == "vespa.test"
        if request.method == "DELETE":
            assert "document_id" in request.url.params["selection"]
            if self.fail == "cleanup" and any(
                sent.method == "POST" and sent.url.host == "vespa.test"
                for sent in self.requests
            ):
                return httpx.Response(503, json={"message": "private-api-credential"})
        if request.method == "POST" and self.fail in ("index_delta", "cleanup"):
            return httpx.Response(500, json={"message": "private-api-credential"})
        return httpx.Response(200, json={"documents": [], "documentCount": 0})


@pytest.fixture
def services(monkeypatch: pytest.MonkeyPatch) -> _Services:
    boundary = _Services()
    original = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        client = original(
            base_url=str(kwargs.get("base_url", "")),
            transport=httpx.MockTransport(boundary.handle),
        )
        boundary.clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    return boundary


def _context() -> RefreshContext:
    return RefreshContext(deadline=datetime.now(UTC) + timedelta(minutes=50))


async def _prepare(
    environment: ActivityEnvironment, context: RefreshContext
) -> StageRef:
    snapshot = await environment.run(crawl, context)
    extracted = await environment.run(extract, context, snapshot)
    compared = await environment.run(compare_hashes, context, extracted)
    return await environment.run(split, context, compared)


@pytest.mark.usefixtures("cluster_env")
async def test_native_activities_index_only_after_preparation_and_skip_unchanged(
    tmp_path: Path,
    crawl_transport: list[httpx.Request],
    services: _Services,
) -> None:
    environment = ActivityEnvironment()
    beats: list[tuple[object, ...]] = []
    environment.on_heartbeat = lambda *details: beats.append(details)
    context = _context()
    chunks = await _prepare(environment, context)
    assert all(request.method == "GET" for request in services.requests)
    embedded = await environment.run(embed, context, chunks)
    assert not any(
        request.method != "GET" and request.url.host == "vespa.test"
        for request in services.requests
    )
    result = await environment.run(index_delta, context, embedded)
    assert (result.indexed, result.failed, result.status) == (1, 0, "complete")
    assert (
        len([request for request in services.requests if request.method == "POST"]) == 2
    )
    assert beats and all(details == () for details in beats)
    assert crawl_transport
    assert services.clients and all(client.is_closed for client in services.clients)

    requests_before = list(services.requests)
    snapshot = _snapshot(tmp_path, {"/new": _html("New", "Updated evidence")})
    extracted = await environment.run(extract, context, snapshot)
    compared = await environment.run(compare_hashes, context, extracted)
    chunks = await environment.run(split, context, compared)
    embedded = await environment.run(embed, context, chunks)
    unchanged = await environment.run(index_delta, context, embedded)
    assert (unchanged.indexed, unchanged.unchanged, unchanged.failed) == (0, 1, 0)
    assert services.requests == requests_before
    assert all(client.is_closed for client in services.clients)


@pytest.mark.usefixtures("cluster_env")
async def test_all_extraction_failures_return_a_partial_result(
    tmp_path: Path, services: _Services
) -> None:
    snapshot = _snapshot(tmp_path, {"/broken": b"<html></html>"})
    environment, context = ActivityEnvironment(), _context()
    extracted = await environment.run(extract, context, snapshot)
    compared = await environment.run(compare_hashes, context, extracted)
    chunks = await environment.run(split, context, compared)
    embedded = await environment.run(embed, context, chunks)
    result = await environment.run(index_delta, context, embedded)
    assert (result.indexed, result.failed, result.status) == (0, 1, "partial")
    assert all(request.method == "GET" for request in services.requests)
    assert all(client.is_closed for client in services.clients)


@pytest.mark.usefixtures("cluster_env")
async def test_expired_deadline_prevents_new_activity_side_effects(
    services: _Services, crawl_transport: list[httpx.Request]
) -> None:
    context = RefreshContext(deadline=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(IngestionError, match="shared 50-minute deadline"):
        await ActivityEnvironment().run(crawl, context)
    assert not services.requests
    assert not services.clients
    assert not crawl_transport


@pytest.mark.usefixtures("cluster_env", "crawl_transport")
async def test_shared_deadline_cancels_an_active_embedding_and_closes_clients(
    services: _Services,
) -> None:
    environment = ActivityEnvironment()
    chunks = await _prepare(environment, _context())
    services.hold_embeddings = True
    context = RefreshContext(deadline=datetime.now(UTC) + timedelta(seconds=1))
    with pytest.raises(IngestionError, match="shared 50-minute deadline"):
        await environment.run(embed, context, chunks)
    assert any(request.url.host == "mistral.test" for request in services.requests)
    assert all(client.is_closed for client in services.clients)
    assert all(
        request.method == "GET"
        for request in services.requests
        if request.url.host == "vespa.test"
    )


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


@pytest.mark.usefixtures("cluster_env", "crawl_transport")
@pytest.mark.parametrize(
    ("failure_kind", "log_format"),
    [
        ("corrupt", "json"),
        ("obsolete", "json"),
        ("dependency", "json"),
        ("index_delta", "json"),
        ("cleanup", "json"),
        ("cleanup", "console"),
    ],
)
def test_cli_preserves_correlated_and_redacted_activity_diagnostics(
    tmp_path: Path,
    services: _Services,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_kind: str,
    log_format: str,
) -> None:
    indexing = failure_kind in ("index_delta", "cleanup")
    stage, artifact = ("index_delta", "embedded") if indexing else ("embed", "split")
    monkeypatch.setattr(workflows.config.common, "log_level", LogLevel.INFO)
    monkeypatch.setattr(workflows.config.common, "log_format", LogFormat(log_format))
    # The installed OpenTelemetry test exporter has an unannotated constructor.
    exporter = InMemoryLogRecordExporter()  # type: ignore[no-untyped-call]
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    traces = TracerProvider()
    expected: dict[str, str | int] = {}

    async def run_worker(definitions: list[type[object]]) -> None:
        assert definitions == [RefreshDocumentation]
        # Attach a real local collector after the CLI configures native SDK logging.
        handler = LoggingHandler(logger_provider=provider)
        handler.setFormatter(build_json_log_formatter())
        logging.getLogger().addHandler(handler)
        try:
            environment = ActivityEnvironment()
            environment.info = replace(
                environment.info,
                workflow_id="refresh-execution",
                workflow_run_id="refresh-run",
            )
            with traces.get_tracer(__name__).start_as_current_span("refresh") as span:
                expected["otel.trace_id"] = format(
                    span.get_span_context().trace_id, "032x"
                )
                context = _context()
                chunks = await _prepare(environment, context)
                expected["snapshot"] = chunks.snapshot.name
                if failure_kind == "corrupt":
                    artifact = (
                        tmp_path
                        / "snapshots"
                        / chunks.snapshot.name
                        / "prepared"
                        / "split"
                        / "documents.jsonl"
                    )
                    artifact.write_text("private-api-credential")
                elif failure_kind == "obsolete":
                    _snapshot(
                        tmp_path, {"/replacement": _html("New", "Other evidence")}
                    )
                else:
                    services.fail = failure_kind if indexing else "embed"
                with pytest.raises(
                    IngestionError, match=f"activity '{stage}' failed"
                ) as error:
                    embedded = await environment.run(embed, context, chunks)
                    await environment.run(index_delta, context, embedded)
                failure = Failure()
                await DataConverter.default.encode_failure(error.value, failure)
                assert "private-api-credential" not in str(failure)
        finally:
            logging.getLogger().removeHandler(handler)
            handler.close()

    monkeypatch.setattr(workflows, "run_worker", run_worker)
    try:
        assert main(["workflows"]) == 0
        assert all(client.is_closed for client in services.clients)
        console = capsys.readouterr()
        assert "private-api-credential" not in console.out + console.err
        records = exporter.get_finished_logs()
        assert records
        events = []
        cleanup_events = []
        for record in records:
            body = record.log_record.body
            assert isinstance(body, str)
            assert "private-api-credential" not in body
            assert "private-api-credential" not in str(record.log_record.attributes)
            event = json.loads(body)
            if event.get("event") == "refresh_activity_failed":
                events.append(event)
            if (
                event.get("event")
                == "Best-effort cleanup of partially indexed chunks failed"
            ):
                cleanup_events.append(event)
        assert len(events) == 1
        event = events[0]
        assert event["workflow.execution_id"] == "refresh-execution"
        assert event["workflow.run_id"] == "refresh-run"
        assert event["otel.trace_id"] == expected["otel.trace_id"]
        assert event["snapshot"] == expected["snapshot"]
        assert (event["stage"], event["artifact"]) == (stage, artifact)
        (cause,) = event["causes"]
        assert cause["error_type"]
        assert cause["error_line"] > 0
        assert (
            cause["error_module"]
            == {
                "corrupt": "docstral_worker.prepared",
                "obsolete": "docstral_worker.snapshot",
                "dependency": "docstral_worker.incremental",
                "index_delta": "docstral_worker.corpus",
                "cleanup": "docstral_worker.corpus",
            }[failure_kind]
        )
        if failure_kind == "cleanup":
            (cleanup,) = cleanup_events
            assert cleanup["document_id"]
            assert cleanup["level"] == "error"
            for key in ("workflow.execution_id", "workflow.run_id", "otel.trace_id"):
                assert cleanup[key] == event[key]
            assert "Best-effort cleanup" in console.out + console.err
        else:
            assert not cleanup_events
        assert [
            request.method
            for request in services.requests
            if request.url.host == "vespa.test" and request.method != "GET"
        ] == (["DELETE", "POST", "DELETE"] if indexing else [])
    finally:
        traces.shutdown()
        provider.shutdown()


@pytest.mark.usefixtures("cluster_env")
def test_worker_requires_deployment_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("DEPLOYMENT_NAME")
    assert main(["workflows"]) == 1
    output = capsys.readouterr()
    assert "DEPLOYMENT_NAME" in output.out + output.err


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
