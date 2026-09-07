from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta

import httpx
import structlog
from mistralai import workflows
from mistralai.search.toolkit.clients.mistral import build_mistral_client
from mistralai.search.toolkit.embedding import MODEL_1024_EMBEDDING, MistralEmbedder
from mistralai.search.toolkit.plugins.vespa import VespaClient, VespaClientConfig
from mistralai.workflows.exceptions import WorkflowError

from docstral_worker.refresh.config import refresh_config
from docstral_worker.refresh.corpus import VespaCorpus
from docstral_worker.refresh.crawler import discover, download
from docstral_worker.refresh.indexing import PageIndexer
from docstral_worker.refresh.models import DiscoveryResult, PageResult


def retryable(error: BaseException) -> bool:
    if isinstance(error, BaseExceptionGroup):
        return all(retryable(cause) for cause in error.exceptions)
    if isinstance(
        error, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
    ):
        return True
    status = getattr(error, "status_code", None)
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
    if isinstance(status, int):
        return status in (408, 429) or 500 <= status < 600
    cause = error.__cause__ or error.__context__
    return cause is not None and retryable(cause)


async def heartbeat() -> None:
    while True:
        workflows.activity_heartbeat()
        await asyncio.sleep(20)


@asynccontextmanager
async def activity_scope(stage: str, url: str | None = None) -> AsyncIterator[None]:
    try:
        async with asyncio.TaskGroup() as tasks:
            task = tasks.create_task(heartbeat())
            try:
                yield
            finally:
                task.cancel()
    except Exception as error:
        while isinstance(error, ExceptionGroup) and len(error.exceptions) == 1:
            error = error.exceptions[0]
        transient = retryable(error)
        structlog.get_logger(__name__).error(
            "refresh_activity_failed",
            stage=stage,
            url=url,
            error_type=type(error).__name__,
            retryable=transient,
        )
        raise WorkflowError(
            f"{stage} failed for {url or 'the corpus'} ({type(error).__name__}); "
            "check worker configuration and dependency availability",
            non_retryable=not transient,
        ) from None


@asynccontextmanager
async def corpus_client() -> AsyncIterator[VespaCorpus]:
    config = refresh_config()
    client = VespaClient(
        VespaClientConfig(
            endpoint=str(config.vespa_endpoint).rstrip("/"),
            timeout=30,
        )
    )
    try:
        yield VespaCorpus(client)
    finally:
        await client.aclose()


@workflows.activity(
    name="discover_urls",
    start_to_close_timeout=timedelta(minutes=5),
    heartbeat_timeout=timedelta(minutes=1),
    retry_policy_max_attempts=3,
    retry_policy_backoff_coefficient=2,
)
async def discover_urls() -> DiscoveryResult:
    async with activity_scope("discover_urls"):
        return await asyncio.to_thread(discover, refresh_config())


@workflows.activity(
    name="sync_page",
    start_to_close_timeout=timedelta(minutes=5),
    heartbeat_timeout=timedelta(minutes=1),
    retry_policy_max_attempts=3,
    retry_policy_backoff_coefficient=2,
)
async def sync_page(url: str) -> PageResult:
    async with activity_scope("sync_page", url):
        page = await asyncio.to_thread(download, url, refresh_config())
        if isinstance(page, PageResult):
            return page
        async with AsyncExitStack() as resources:
            corpus = await resources.enter_async_context(corpus_client())
            mistral = resources.enter_context(build_mistral_client())
            await resources.enter_async_context(mistral)
            indexer = PageIndexer(
                corpus,
                MistralEmbedder(
                    client=mistral,
                    model_name=MODEL_1024_EMBEDDING,
                    max_retry=3,
                ),
            )
            return await indexer.sync(page)


@workflows.activity(
    name="plan_deletions",
    start_to_close_timeout=timedelta(minutes=5),
    heartbeat_timeout=timedelta(minutes=1),
    retry_policy_max_attempts=3,
    retry_policy_backoff_coefficient=2,
)
async def plan_deletions(present_urls: tuple[str, ...]) -> tuple[str, ...]:
    async with activity_scope("plan_deletions"), corpus_client() as corpus:
        present = set(present_urls)
        return tuple(
            source.source_id
            for source in await corpus.list_sources()
            if source.source_id not in present
        )


@workflows.activity(
    name="delete_page",
    start_to_close_timeout=timedelta(minutes=5),
    heartbeat_timeout=timedelta(minutes=1),
    retry_policy_max_attempts=3,
    retry_policy_backoff_coefficient=2,
)
async def delete_page(url: str) -> None:
    async with activity_scope("delete_page", url), corpus_client() as corpus:
        await corpus.delete_page(url)
