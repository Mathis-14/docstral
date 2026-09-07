import argparse
import asyncio
import os
import shutil
import socket
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

from docstral_vespa import PAGE_COLLECTION_NAME, index_for_client
from docstral_worker import IngestionError
from docstral_worker.refresh.models import PageState, RefreshResult
from mistralai.client import Mistral
from mistralai.client.errors import SDKError
from mistralai.search.toolkit.plugins.vespa import VespaClient, VespaClientConfig
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

WORKFLOW = "docstral-refresh"
ACTIVE = ("RUNNING", "RETRYING_AFTER_ERROR")
ROOT = Path(__file__).resolve().parents[1]


class LocalConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    api_key: SecretStr = Field(min_length=1)
    container: str = Field(
        default="docstral-vespa", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$"
    )
    query_port: int = Field(default=8080, ge=1, le=65535)
    config_port: int = Field(default=19071, ge=1, le=65535)
    mcp_port: int = Field(default=8000, ge=1, le=65535)

    @property
    def endpoint(self) -> str:
        return f"http://localhost:{self.query_port}"

    @property
    def deployment(self) -> str:
        identity = f"{socket.gethostname()}:{os.getuid()}:{self.container}:{self.query_port}:{self.config_port}"
        return "docstral-local-" + sha256(identity.encode()).hexdigest()[:16]

    def environment(self, worker_name: str) -> dict[str, str]:
        return {
            **os.environ,
            "MISTRAL_API_KEY": self.api_key.get_secret_value(),
            "VESPA_ENDPOINT": self.endpoint,
            "DEPLOYMENT_NAME": self.deployment,
            "WORKER_NAME": worker_name,
            "OTEL_REDACTION": "strict",
        }


def active_run(client: Mistral, deployment: str) -> str | None:
    runs: set[str] = set()
    token: str | None = None
    seen: set[str] = set()
    while True:
        page = client.workflows.runs.list_runs(
            workflow_identifier=WORKFLOW,
            deployment_name=deployment,
            page_size=100,
            next_page_token=token,
            timeout_ms=10000,
        )
        if page is None:
            raise IngestionError(
                "Workflow inventory unavailable; check access in Mistral Studio"
            )
        runs.update(
            run.execution_id for run in page.result.executions if run.status in ACTIVE
        )
        cursor = page.result.next_page_token
        if not isinstance(cursor, str) or not cursor:
            break
        if cursor in seen:
            raise IngestionError("Workflow inventory repeated a cursor")
        seen.add(cursor)
        token = cursor
    if len(runs) > 1:
        raise IngestionError(
            "Multiple local refreshes are active; stop the extra runs in Mistral Studio"
        )
    return next(iter(runs), None)


def require_worker(worker: subprocess.Popen[bytes]) -> None:
    if worker.poll() is not None:
        raise IngestionError("Local worker stopped; inspect its preceding error")


def wait_registered(
    client: Mistral, config: LocalConfig, worker: subprocess.Popen[bytes], name: str
) -> None:
    deadline = monotonic() + 120
    while monotonic() < deadline:
        require_worker(worker)
        try:
            deployment = client.workflows.deployments.get_deployment(
                name=config.deployment, workflow_name=WORKFLOW, timeout_ms=10000
            )
        except SDKError as error:
            if error.status_code != 404:
                raise
        else:
            if any(item.name == name and item.is_active for item in deployment.workers):
                return
        sleep(2)
    raise IngestionError(
        "Local worker registration timed out; check Workflows access and worker logs"
    )


def wait_refresh(
    client: Mistral, execution_id: str, worker: subprocess.Popen[bytes]
) -> RefreshResult:
    print(f"Waiting for {WORKFLOW}: {execution_id}", flush=True)
    deadline = monotonic() + 50 * 60
    while monotonic() < deadline:
        require_worker(worker)
        run = client.workflows.executions.get_workflow_execution(
            execution_id=execution_id, timeout_ms=10000
        )
        if run.status == "COMPLETED":
            result = RefreshResult.model_validate(run.result)
            print(result.model_dump_json(indent=2), flush=True)
            return result
        if run.status not in ACTIVE:
            raise IngestionError(
                f"Refresh {execution_id} ended with {run.status}; inspect this run in Mistral Studio"
            )
        sleep(5)
    raise IngestionError(
        f"Refresh {execution_id} is still active; rerun the command to reconnect"
    )


async def confirmed_pages(endpoint: str) -> int:
    client = VespaClient(VespaClientConfig(endpoint=endpoint, timeout=30))
    confirmed = 0
    continuation: str | None = None
    seen: set[str] = set()
    try:
        while True:
            page = await client.visit_by_selection(
                PAGE_COLLECTION_NAME,
                PAGE_COLLECTION_NAME,
                cluster=index_for_client(client).schema.content_cluster,
                field_set="pages:source_id,document_id,index_hash",
                continuation=continuation,
                extra_params={"wantedDocumentCount": "1000"},
            )
            confirmed += sum(
                bool(PageState.model_validate(record.fields).index_hash)
                for record in page.documents
            )
            continuation = page.continuation
            if continuation is None:
                return confirmed
            if not continuation or continuation in seen:
                raise IngestionError("Vespa page inventory is incomplete")
            seen.add(continuation)
    finally:
        await client.aclose()


def migrate(config: LocalConfig, environment: dict[str, str]) -> None:
    subprocess.run(
        [
            "mistral-vespa",
            "migrate",
            "--app-dir",
            str(ROOT / "packages/vespa/src/docstral_vespa"),
            "--config-server",
            f"http://localhost:{config.config_port}",
            "--query-port",
            str(config.query_port),
        ],
        check=True,
        cwd=ROOT,
        env=environment,
    )


def stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def launch(config: LocalConfig, *, refresh: bool) -> int:
    for command in ("docker", "mistral-vespa", "docstral-worker", "docstral-mcp"):
        if shutil.which(command) is None:
            raise IngestionError(
                f"Missing command {command}; install Docker and run uv sync --all-packages"
            )
    environment = config.environment("local-" + uuid4().hex[:12])
    subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    if not refresh:
        with socket.socket() as port:
            try:
                port.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                port.bind(("127.0.0.1", config.mcp_port))
            except OSError as error:
                raise IngestionError(
                    f"Cannot bind MCP port {config.mcp_port} ({type(error).__name__}); check permissions or set DOCSTRAL_MCP_PORT"
                ) from error
    subprocess.run(
        [
            "mistral-vespa",
            "local",
            "up",
            "--query-port",
            str(config.query_port),
            "--config-port",
            str(config.config_port),
            "--name",
            config.container,
        ],
        check=True,
        cwd=ROOT,
        env=environment,
    )
    with Mistral(api_key=config.api_key.get_secret_value()) as client:
        execution_id = active_run(client, config.deployment)
        # A resumed run finishes against its existing schema before any migration.
        if execution_id is None:
            migrate(config, environment)
        worker = subprocess.Popen(
            ["docstral-worker", "workflows"], cwd=ROOT, env=environment
        )
        try:
            print(f"Local Workflows deployment: {config.deployment}", flush=True)
            wait_registered(client, config, worker, environment["WORKER_NAME"])
            if execution_id is not None:
                wait_refresh(client, execution_id, worker)
                migrate(config, environment)
            elif refresh or asyncio.run(confirmed_pages(config.endpoint)) == 0:
                # Recheck after registration so another local command's run is reused.
                execution_id = active_run(client, config.deployment)
                if execution_id is None:
                    execution_id = client.workflows.execute_workflow(
                        workflow_identifier=WORKFLOW,
                        input={},
                        deployment_name=config.deployment,
                        timeout_ms=10000,
                    ).execution_id
                wait_refresh(client, execution_id, worker)
            count = asyncio.run(confirmed_pages(config.endpoint))
            if count == 0:
                raise IngestionError(
                    "No confirmed documentation page is indexed; inspect the refresh errors, then run make refresh"
                )
            print(f"{count} confirmed pages available in local Vespa.", flush=True)
            if refresh:
                return 0
            mcp = subprocess.Popen(
                [
                    "docstral-mcp",
                    "--vespa-endpoint",
                    config.endpoint,
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(config.mcp_port),
                ],
                cwd=ROOT,
                env=environment,
            )
            try:
                while mcp.poll() is None:
                    require_worker(worker)
                    sleep(1)
                return mcp.returncode
            finally:
                stop(mcp)
        finally:
            stop(worker)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Start Docstral against local Vespa and native Mistral Workflows"
    )
    parser.add_argument(
        "--refresh", action="store_true", help="refresh the corpus and exit"
    )
    args = parser.parse_args()
    try:
        config = LocalConfig.model_validate(
            {
                "api_key": os.environ.get("MISTRAL_API_KEY", "").strip(),
                "container": os.environ.get("VESPA_CONTAINER", "docstral-vespa"),
                "query_port": os.environ.get("VESPA_QUERY_PORT", "8080"),
                "config_port": os.environ.get("VESPA_CONFIG_PORT", "19071"),
                "mcp_port": os.environ.get("DOCSTRAL_MCP_PORT", "8000"),
            }
        )
        return launch(config, refresh=args.refresh)
    except KeyboardInterrupt:
        return 130
    except ValidationError as error:
        print(
            f"Invalid local configuration: {error.errors(include_input=False, include_url=False)}; set MISTRAL_API_KEY in .env",
            file=sys.stderr,
        )
    except subprocess.CalledProcessError as error:
        print(
            f"Local command failed ({error.cmd[0]}); inspect the preceding logs. If Vespa failed to start, run docker logs {config.container} and check Docker memory.",
            file=sys.stderr,
        )
    except IngestionError as error:
        print(str(error), file=sys.stderr)
    except Exception as error:
        print(
            f"Local startup failed ({type(error).__name__}); check Docker, Mistral Workflows access and the preceding logs.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
