import os
import subprocess
import sys
from datetime import UTC, datetime

import pytest
from docstral_worker.refresh.models import RefreshResult
from docstral_worker.refresh.workflow import RefreshDocumentation
from mistralai import workflows
from temporalio.testing import ActivityEnvironment
from worker_fixtures import Services
from worker_fixtures import services as services


async def refresh() -> RefreshResult:
    return RefreshResult.model_validate(
        await ActivityEnvironment().run(RefreshDocumentation().run)
    )


@pytest.fixture
def cluster_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VESPA_ENDPOINT", "http://vespa.test")
    monkeypatch.setenv("DEPLOYMENT_NAME", "docstral-test")


@pytest.fixture(autouse=True)
def workflow_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflows.workflow, "now", lambda: datetime.now(UTC))


async def test_redirect_aliases_schedule_one_destination(services: Services) -> None:
    services.seeds = ["/a", "/alias", "/en/a/", "/b"]
    services.redirects = {"/a": "/b", "/alias": "/b"}
    result = await refresh()
    assert result.indexed == 1
    assert sum(request.url.path == "/b" for request in services.requests) == 1


async def test_only_missing_pages_are_deleted(services: Services) -> None:
    services.seeds = ["/a", "/b"]
    await refresh()
    services.seeds = ["/a"]
    result = await refresh()
    assert (result.unchanged, result.deleted) == (1, 1)
    assert {fields["source_id"] for fields in services.documents.values()} == {
        "https://docs.mistral.ai/a"
    }


async def test_internal_links_are_discovered_on_unchanged_pages(
    services: Services,
) -> None:
    services.pages["/a"] += b'<a href="/b">B</a>'
    await refresh()
    result = await refresh()
    assert (result.unchanged, result.discovered) == (2, 2)


async def test_redirect_cycles_fail_without_deletions(services: Services) -> None:
    from mistralai.workflows.exceptions import WorkflowError

    services.redirects = {"/a": "/b", "/b": "/a"}
    with pytest.raises(WorkflowError, match="Redirect cycle"):
        await refresh()
    assert all(request.method != "DELETE" for request in services.requests)


async def test_incomplete_discovery_prevents_deletions(services: Services) -> None:
    services.seeds = ["/a", "/b"]
    await refresh()
    previous = services.documents.copy()
    services.seeds = ["/a"]
    services.fail = "/a"
    result = await refresh()
    assert result.deletions_skipped
    assert result.failed_urls == ("https://docs.mistral.ai/a",)
    assert services.documents == previous


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


def test_workflow_loads_in_mistrals_deterministic_sandbox() -> None:
    script = """
import asyncio
from docstral_worker.refresh.workflow import RefreshDocumentation
from mistralai.workflows.core.sandbox import get_sandbox_restrictions
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner
from temporalio.workflow import _Definition
async def check():
    SandboxedWorkflowRunner(restrictions=get_sandbox_restrictions()).prepare_workflow(
        _Definition.must_from_class(RefreshDocumentation)
    )
asyncio.run(check())
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
