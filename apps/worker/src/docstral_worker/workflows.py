"""Native Mistral orchestration; corpus operations remain in the worker."""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import TYPE_CHECKING

from mistralai import workflows

from docstral_worker import IngestionError

if TYPE_CHECKING:
    from docstral_worker.publish import PublishConfig

with workflows.workflow.unsafe.imports_passed_through():
    # Only this immutable result type is used by the deterministic workflow.
    from docstral_worker.ingest import IngestResult


def _publish_config() -> PublishConfig:
    from docstral_worker.publish import PublishConfig

    # Cluster configuration belongs to the worker, never to workflow inputs.
    return PublishConfig.model_validate(
        {
            "data_dir": os.environ.get("DOCSTRAL_DATA_DIR", "/app/data"),
            "vespa_endpoint": os.environ.get("VESPA_ENDPOINT"),
            "namespace": os.environ.get("POD_NAMESPACE"),
            "mcp_deployment": os.environ.get("MCP_DEPLOYMENT"),
        }
    )


async def _heartbeat() -> None:
    while True:
        workflows.activity_heartbeat()
        await asyncio.sleep(20)


@workflows.activity(
    name="refresh_documentation",
    start_to_close_timeout=timedelta(minutes=55),
    heartbeat_timeout=timedelta(minutes=1),
    retry_policy_max_attempts=1,
)
async def refresh_documentation() -> IngestResult:
    """One full cycle, without automatically retrying a destructive rebuild."""
    from docstral_worker.publish import publish

    try:
        async with asyncio.TaskGroup() as tasks:
            heartbeat = tasks.create_task(_heartbeat())
            try:
                async with asyncio.timeout(50 * 60):
                    return await publish(_publish_config(), refresh=True)
            finally:
                heartbeat.cancel()
    except Exception:
        # API exceptions can contain credentials. Do not upload them to workflow history.
        raise IngestionError(
            "Documentation refresh failed; check worker configuration and dependency "
            "availability. If publication started, repair with publish before resuming."
        ) from None


@workflows.workflow.define(name="docstral-refresh", enforce_determinism=True)
class RefreshDocumentation:
    @workflows.workflow.entrypoint
    async def run(self) -> IngestResult:
        return await refresh_documentation()


async def run_worker() -> None:
    """Register and poll for executions; starting a worker does not create a schedule."""
    _publish_config()
    if not os.environ.get("DEPLOYMENT_NAME", "").strip():
        raise IngestionError("DEPLOYMENT_NAME is required for the Workflows worker")
    await workflows.run_worker([RefreshDocumentation])
