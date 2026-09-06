"""Six native activities refresh documentation while MCP remains available."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from mistralai import workflows
from pydantic import AnyHttpUrl, AwareDatetime, BaseModel, ConfigDict

from docstral_worker import IngestionError

if TYPE_CHECKING:
    from logging import LogRecord

    from docstral_worker.incremental import IncrementalIngestion

with workflows.workflow.unsafe.imports_passed_through():
    # Only immutable result/reference types enter the deterministic workflow.
    from docstral_worker.incremental import RefreshResult
    from docstral_worker.prepared import SnapshotRef, StageRef


class WorkflowConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    data_dir: Path
    vespa_endpoint: AnyHttpUrl


class RefreshContext(BaseModel):
    """One deadline survives every activity boundary and workflow replay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    deadline: AwareDatetime


def _worker_config() -> WorkflowConfig:
    # Paths and credentials belong to the worker, never to workflow inputs.
    return WorkflowConfig.model_validate(
        {
            "data_dir": os.environ.get("DOCSTRAL_DATA_DIR", "/app/data"),
            "vespa_endpoint": os.environ.get("VESPA_ENDPOINT"),
        }
    )


async def _heartbeat() -> None:
    while True:
        workflows.activity_heartbeat()
        await asyncio.sleep(20)


def _redact_vespa_exceptions(record: LogRecord) -> bool:
    """Keep SDK cleanup events without exporting exception bodies or tracebacks."""
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None
    # structlog's payload and the LogRecord feed separate OTel body/attribute paths.
    if isinstance(record.msg, dict):
        for field in ("exc_info", "exception", "stack_info", "stack"):
            record.msg.pop(field, None)
    return True


def _failure_details(error: BaseException) -> list[dict[str, str | int]]:
    """Locate failures without copying exception text, tracebacks or local values."""
    if isinstance(error, BaseExceptionGroup):
        return [
            cause for nested in error.exceptions for cause in _failure_details(nested)
        ]
    details: dict[str, str | int] = {"error_type": type(error).__name__}
    traceback = error.__traceback__
    while traceback is not None:
        module = traceback.tb_frame.f_globals.get("__name__")
        if isinstance(module, str) and module.startswith("docstral_worker."):
            details.update(error_module=module, error_line=traceback.tb_lineno)
        traceback = traceback.tb_next
    return [details]


@asynccontextmanager
async def _activity_worker(
    stage: str,
    context: RefreshContext,
    reference: SnapshotRef | StageRef | None = None,
) -> AsyncIterator[IncrementalIngestion]:
    import structlog
    from mistralai.search.toolkit.clients.mistral import build_mistral_client
    from mistralai.search.toolkit.embedding import MODEL_1024_EMBEDDING, MistralEmbedder
    from mistralai.search.toolkit.plugins.vespa import VespaClient, VespaClientConfig

    from docstral_worker.corpus import VespaCorpus
    from docstral_worker.incremental import IncrementalIngestion

    logger = structlog.get_logger(__name__).bind(stage=stage)
    if reference is not None:
        snapshot = reference.snapshot if isinstance(reference, StageRef) else reference
        logger = logger.bind(snapshot=snapshot.name)
        if isinstance(reference, StageRef):
            logger = logger.bind(artifact=reference.stage)
    try:
        remaining = (context.deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise TimeoutError
        async with asyncio.timeout(remaining), asyncio.TaskGroup() as tasks:
            async with AsyncExitStack() as resources:
                heartbeat = tasks.create_task(_heartbeat())
                resources.callback(heartbeat.cancel)
                config = _worker_config()
                client = VespaClient(
                    VespaClientConfig(
                        endpoint=str(config.vespa_endpoint).rstrip("/"), timeout=30
                    )
                )
                resources.push_async_callback(client.aclose)
                # The SDK owns separate sync and async HTTP transports.
                mistral = resources.enter_context(build_mistral_client())
                await resources.enter_async_context(mistral)
                yield IncrementalIngestion(
                    config.data_dir,
                    corpus=VespaCorpus(client),
                    embedder=MistralEmbedder(
                        client=mistral, model_name=MODEL_1024_EMBEDDING, max_retry=6
                    ),
                )
    except TimeoutError:
        logger.error("refresh_activity_failed", error_code="deadline_exceeded")
        raise IngestionError(
            f"Documentation activity {stage!r} exceeded the shared 50-minute "
            "deadline; start a new docstral-refresh execution to retry."
        ) from None
    except Exception as error:
        # Dependency exceptions can contain credentials; omit them from history.
        logger.error(
            "refresh_activity_failed",
            error_code="activity_failed",
            causes=_failure_details(error),
        )
        raise IngestionError(
            f"Documentation activity {stage!r} failed; check worker logs, "
            "configuration and dependencies, then start a new docstral-refresh "
            "execution to retry."
        ) from None


@workflows.activity(
    name="crawl",
    start_to_close_timeout=timedelta(minutes=55),
    heartbeat_timeout=timedelta(minutes=1),
    retry_policy_max_attempts=1,
)
async def crawl(context: RefreshContext) -> SnapshotRef:
    async with _activity_worker("crawl", context) as worker:
        return await worker.crawl()


@workflows.activity(
    name="extract",
    start_to_close_timeout=timedelta(minutes=55),
    heartbeat_timeout=timedelta(minutes=1),
    retry_policy_max_attempts=1,
)
async def extract(context: RefreshContext, snapshot: SnapshotRef) -> StageRef:
    async with _activity_worker("extract", context, snapshot) as worker:
        return await worker.extract(snapshot)


@workflows.activity(
    name="compare_hashes",
    start_to_close_timeout=timedelta(minutes=55),
    heartbeat_timeout=timedelta(minutes=1),
    retry_policy_max_attempts=1,
)
async def compare_hashes(context: RefreshContext, extracted: StageRef) -> StageRef:
    async with _activity_worker("compare_hashes", context, extracted) as worker:
        return await worker.compare_hashes(extracted)


@workflows.activity(
    name="split",
    start_to_close_timeout=timedelta(minutes=55),
    heartbeat_timeout=timedelta(minutes=1),
    retry_policy_max_attempts=1,
)
async def split(context: RefreshContext, compared: StageRef) -> StageRef:
    async with _activity_worker("split", context, compared) as worker:
        return await worker.split(compared)


@workflows.activity(
    name="embed",
    start_to_close_timeout=timedelta(minutes=55),
    heartbeat_timeout=timedelta(minutes=1),
    retry_policy_max_attempts=1,
)
async def embed(context: RefreshContext, chunks: StageRef) -> StageRef:
    async with _activity_worker("embed", context, chunks) as worker:
        return await worker.embed(chunks)


@workflows.activity(
    name="index_delta",
    start_to_close_timeout=timedelta(minutes=55),
    heartbeat_timeout=timedelta(minutes=1),
    retry_policy_max_attempts=1,
)
async def index_delta(context: RefreshContext, embedded: StageRef) -> RefreshResult:
    async with _activity_worker("index_delta", context, embedded) as worker:
        return await worker.index_delta(embedded)


@workflows.workflow.define(name="docstral-refresh", enforce_determinism=True)
class RefreshDocumentation:
    @workflows.workflow.entrypoint
    async def run(self) -> RefreshResult:
        context = RefreshContext(
            deadline=workflows.workflow.now() + timedelta(minutes=50)
        )
        snapshot = await crawl(context)
        extracted = await extract(context, snapshot)
        compared = await compare_hashes(context, extracted)
        chunks = await split(context, compared)
        embedded = await embed(context, chunks)
        return await index_delta(context, embedded)


async def run_worker() -> None:
    """Register and poll; starting a worker never creates or activates a schedule."""
    import logging

    from mistralai.workflows.core.logging import setup_logging

    config = workflows.config.common
    setup_logging(
        log_level=config.log_level,
        log_format=config.log_format,
        app_version=config.app_version,
        inject_otel_trace=config.otel_enabled and config.otel_inject_logs,
    )
    # Filter at the emitting SDK logger so every handler receives the safe record.
    logging.getLogger(
        "mistralai.search.toolkit.plugins.vespa.search.document_per_chunk_index"
    ).addFilter(_redact_vespa_exceptions)
    _worker_config()
    if not os.environ.get("DEPLOYMENT_NAME", "").strip():
        raise IngestionError("DEPLOYMENT_NAME is required for the Workflows worker")
    await workflows.run_worker([RefreshDocumentation])
