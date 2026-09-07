import json
import shutil
import socket
import subprocess
from collections.abc import Iterator

import httpx
import pytest
from docstral_worker import IngestionError
from docstral_worker.refresh.models import RefreshResult
from local import LocalConfig, launch
from mistralai.search.toolkit.document import compute_id

SOCKET_BIND = socket.socket.bind


class Process:
    def __init__(self, worker: bool) -> None:
        self.returncode: int | None = None if worker else 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0


class LocalServices:
    def __init__(self) -> None:
        self.confirmed = False
        self.active = False
        self.partial = False
        self.commands: list[list[str]] = []
        self.environments: list[dict[str, str]] = []
        self.requests: list[httpx.Request] = []
        self.processes: list[Process] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.startswith("/document/"):
            fields = {
                "source_id": "https://docs.mistral.ai/a",
                "document_id": compute_id("https://docs.mistral.ai/a"),
                "index_hash": "a" * 64,
            }
            documents = [{"id": "page", "fields": fields}] if self.confirmed else []
            return httpx.Response(
                200, json={"documents": documents, "documentCount": len(documents)}
            )
        if path.startswith("/v1/workflows/deployments/"):
            return httpx.Response(
                200,
                json={
                    "id": "deployment",
                    "name": self.environments[-1]["DEPLOYMENT_NAME"],
                    "is_active": True,
                    "created_at": "2026-09-01T00:00:00Z",
                    "updated_at": "2026-09-01T00:00:00Z",
                    "workers": [
                        {
                            "name": self.environments[-1]["WORKER_NAME"],
                            "is_active": True,
                            "created_at": "2026-09-01T00:00:00Z",
                            "updated_at": "2026-09-01T00:00:00Z",
                        }
                    ],
                },
            )
        execution: dict[str, object] = {
            "workflow_name": "docstral-refresh",
            "execution_id": "local-run",
            "root_execution_id": "local-run",
            "status": "RUNNING",
            "start_time": "2026-09-01T00:00:00Z",
            "end_time": None,
            "result": None,
        }
        if path == "/v1/workflows/runs":
            return httpx.Response(
                200,
                json={
                    "executions": [execution] if self.active else [],
                    "next_page_token": None,
                },
            )
        if path.endswith("/execute"):
            self.active = True
            return httpx.Response(200, json=execution)
        if path == "/v1/workflows/executions/local-run":
            self.active = False
            self.confirmed = True
            result = RefreshResult(
                indexed=1,
                unchanged=0,
                changed=1,
                deleted=0,
                failed=int(self.partial),
                failed_urls=("https://docs.mistral.ai/b",) if self.partial else (),
                discovered=2 if self.partial else 1,
                deletions_skipped=self.partial,
                duration_seconds=1,
                status="partial" if self.partial else "complete",
            )
            return httpx.Response(
                200,
                json={
                    **execution,
                    "status": "COMPLETED",
                    "result": result.model_dump(mode="json"),
                },
            )
        raise AssertionError(f"Unexpected external call: {request.method} {path}")


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> Iterator[LocalServices]:
    boundary = LocalServices()
    sync_client, async_client = httpx.Client, httpx.AsyncClient

    def sync_factory(**kwargs: object) -> httpx.Client:
        return sync_client(transport=httpx.MockTransport(boundary.handle))

    def async_factory(**kwargs: object) -> httpx.AsyncClient:
        return async_client(
            base_url=str(kwargs.get("base_url", "")),
            transport=httpx.MockTransport(boundary.handle),
        )

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        boundary.commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    def popen(command: list[str], *, cwd: object, env: dict[str, str]) -> Process:
        boundary.commands.append(command)
        boundary.environments.append(env)
        process = Process(worker=command[0] == "docstral-worker")
        boundary.processes.append(process)
        return process

    def bind(sock: socket.socket, address: object) -> None:
        pass

    monkeypatch.setattr(shutil, "which", lambda command: command)
    monkeypatch.setattr(socket.socket, "bind", bind)
    monkeypatch.setattr(httpx, "Client", sync_factory)
    monkeypatch.setattr(httpx, "AsyncClient", async_factory)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)
    yield boundary
    assert all(process.poll() is not None for process in boundary.processes)


def config() -> LocalConfig:
    return LocalConfig.model_validate({"api_key": "test-key", "mcp_port": 18437})


def test_first_start_initializes_then_serves_from_local_vespa(
    boundary: LocalServices,
) -> None:
    assert launch(config(), refresh=False) == 0
    assert boundary.commands[-1][0] == "docstral-mcp"
    execution = next(
        request
        for request in boundary.requests
        if request.url.path.endswith("/execute")
    )
    assert json.loads(execution.content)["input"] == {}
    assert json.loads(execution.content)["deployment_name"] == config().deployment
    assert all(
        env["VESPA_ENDPOINT"] == "http://localhost:8080"
        for env in boundary.environments
    )
    assert all("down" not in command for command in boundary.commands)


def test_second_start_reuses_index_without_crawling(boundary: LocalServices) -> None:
    boundary.confirmed = True
    assert launch(config(), refresh=False) == 0
    assert all(
        not request.url.path.endswith("/execute") for request in boundary.requests
    )


def test_restart_accepts_port_after_closed_connection(
    boundary: LocalServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket.socket, "bind", SOCKET_BIND)
    with socket.socket() as listener, socket.socket() as client:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        address = listener.getsockname()
        listener.listen(1)
        client.settimeout(2)
        client.connect(address)
        connection, _ = listener.accept()
        with connection:
            connection.shutdown(socket.SHUT_WR)
        assert client.recv(1) == b""
    boundary.confirmed = True
    settings = config().model_copy(update={"mcp_port": address[1]})
    assert launch(settings, refresh=False) == 0
    assert boundary.commands[-1][0] == "docstral-mcp"


def test_occupied_port_stops_before_starting_services(
    boundary: LocalServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket.socket, "bind", SOCKET_BIND)
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        settings = config().model_copy(update={"mcp_port": listener.getsockname()[1]})
        with pytest.raises(IngestionError, match="Cannot bind MCP port"):
            launch(settings, refresh=False)
    assert not boundary.processes
    assert not boundary.requests


def test_explicit_refresh_updates_existing_corpus_and_exits(
    boundary: LocalServices,
) -> None:
    boundary.confirmed = True
    assert launch(config(), refresh=True) == 0
    assert (
        sum(request.url.path.endswith("/execute") for request in boundary.requests) == 1
    )
    assert all(command[0] != "docstral-mcp" for command in boundary.commands)


def test_active_local_execution_is_resumed_without_duplicate(
    boundary: LocalServices,
) -> None:
    boundary.active = True
    assert launch(config(), refresh=True) == 0
    assert all(
        not request.url.path.endswith("/execute") for request in boundary.requests
    )


def test_partial_refresh_is_reported_before_serving(
    boundary: LocalServices, capsys: pytest.CaptureFixture[str]
) -> None:
    boundary.partial = True
    assert launch(config(), refresh=False) == 0
    assert '"status": "partial"' in capsys.readouterr().out


def test_production_environment_cannot_route_the_local_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEPLOYMENT_NAME", "production")
    monkeypatch.setenv("VESPA_ENDPOINT", "https://production.example")
    env = config().environment("worker")
    assert env["DEPLOYMENT_NAME"].startswith("docstral-local-")
    assert env["VESPA_ENDPOINT"] == "http://localhost:8080"
    assert (
        LocalConfig.model_validate(
            {"api_key": "test-key", "query_port": 8082}
        ).deployment
        != config().deployment
    )


def test_failed_workflow_does_not_start_mcp(
    boundary: LocalServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = boundary.handle

    def failed(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.url.path == "/v1/workflows/executions/local-run":
            return httpx.Response(
                200, json={**response.json(), "status": "FAILED", "result": None}
            )
        return response

    monkeypatch.setattr(boundary, "handle", failed)
    with pytest.raises(IngestionError, match="FAILED"):
        launch(config(), refresh=False)
    assert all(command[0] != "docstral-mcp" for command in boundary.commands)


def test_completed_run_without_confirmed_pages_cannot_start_mcp(
    boundary: LocalServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = boundary.handle

    def empty(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.url.path.startswith("/document/"):
            return httpx.Response(200, json={"documents": [], "documentCount": 0})
        return response

    monkeypatch.setattr(boundary, "handle", empty)
    with pytest.raises(IngestionError, match="No confirmed"):
        launch(config(), refresh=False)
    assert all(command[0] != "docstral-mcp" for command in boundary.commands)
