"""Replace the cluster corpus while keeping incomplete indexes offline."""

from hashlib import sha256
from pathlib import Path
from typing import Protocol

import structlog
from mistralai.search.toolkit.embedding import MODEL_1024_EMBEDDING, MistralEmbedder
from mistralai.search.toolkit.ingestion.pipelines import Pipeline
from mistralai.search.toolkit.plugins.vespa import VespaClient, VespaClientConfig
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

from docstral_worker import IngestionError
from docstral_worker.crawl import PageDecision
from docstral_worker.ingest import IngestResult, build_pipeline, ingest_snapshot
from docstral_worker.kubernetes import in_cluster_mcp
from docstral_worker.maintenance import PublicationState
from docstral_worker.retention import prune_snapshots
from docstral_worker.snapshot import (
    CURRENT_FILE,
    MANIFEST_FILE,
    CurrentSnapshot,
    current_snapshot,
    page_slug,
)


class PublishConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    data_dir: Path
    vespa_endpoint: AnyHttpUrl
    namespace: str = Field(pattern=r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$", max_length=63)
    mcp_deployment: str = Field(
        pattern=r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$", max_length=63
    )


class CorpusAdmin(Protocol):
    async def check(self) -> None: ...
    async def clear(self) -> None: ...


class McpControl(Protocol):
    async def check(self) -> None: ...
    async def stop(self) -> None: ...
    async def start(self) -> None: ...


class VespaCorpus:
    """Use the toolkit's public transport, scoped to the shared collection."""

    def __init__(self, client: VespaClient, *, cluster: str) -> None:
        self.client = client
        self.cluster = cluster

    async def check(self) -> None:
        from docstral_vespa import COLLECTION_NAME

        await self.client.visit_by_selection(
            COLLECTION_NAME,
            COLLECTION_NAME,
            cluster=self.cluster,
            field_set="[id]",
            extra_params={"wantedDocumentCount": "1"},
        )

    async def clear(self) -> None:
        from docstral_vespa import COLLECTION_NAME

        await self.client.delete_by_selection(
            COLLECTION_NAME, COLLECTION_NAME, cluster=self.cluster
        )


async def publish(config: PublishConfig) -> IngestResult:
    """Wire cluster-only dependencies and close their clients after publication."""
    from docstral_vespa import index_for_client

    embedder = MistralEmbedder(model_name=MODEL_1024_EMBEDDING, max_retry=6)
    async with in_cluster_mcp(config.namespace, config.mcp_deployment) as mcp:
        client = VespaClient(
            VespaClientConfig(
                endpoint=str(config.vespa_endpoint).rstrip("/"), timeout=30
            )
        )
        try:
            index = index_for_client(client)
            return await publish_current(
                PublicationState(config.data_dir),
                build_pipeline(index=index, embedder=embedder),
                VespaCorpus(client, cluster=index.schema.content_cluster),
                mcp,
            )
        finally:
            await client.aclose()


async def publish_current(
    state: PublicationState, pipeline: Pipeline, corpus: CorpusAdmin, mcp: McpControl
) -> IngestResult:
    """Publish a verified current snapshot under the exclusive worker lock."""
    async with state.lock():
        root = state.directory / "snapshots"
        if root.is_symlink() or (root / CURRENT_FILE).is_symlink():
            raise IngestionError(
                "Publication refuses a symbolic-link snapshot root or pointer"
            )
        snapshot = current_snapshot(root)
        if snapshot is None:
            raise IngestionError(f"No current snapshot under {str(root)!r}")
        _check_snapshot(snapshot)
        logger = structlog.get_logger(__name__).bind(snapshot=snapshot.directory.name)
        await corpus.check()
        await mcp.check()
        logger.info("publication_stopping_mcp")
        await mcp.stop()
        state.mark(state.pending, snapshot.directory.name)
        logger.info("publication_replacing_corpus")
        await corpus.clear()
        result = await ingest_snapshot(snapshot, pipeline)
        if result.indexed == 0:
            raise IngestionError("No page was indexed; MCP remains stopped")
        state.mark(state.published, snapshot.directory.name)
        state.pending.unlink()
        logger.info("publication_starting_mcp")
        await mcp.start()
        prune_snapshots(root, published=snapshot.directory.name)
        return result


def _check_snapshot(snapshot: CurrentSnapshot) -> None:
    """Reject incomplete inventory or corrupt bytes before touching the index."""
    if snapshot.manifest.counts.failed or not snapshot.manifest.counts.stored:
        raise IngestionError("Publication requires a complete, non-empty snapshot")
    if any(
        path.is_symlink()
        for path in (
            snapshot.directory,
            snapshot.directory / "raw",
            snapshot.directory / MANIFEST_FILE,
        )
    ):
        raise IngestionError("Publication refuses a symbolic-link snapshot")
    for entry in snapshot.manifest.pages:
        if entry.decision is not PageDecision.STORED:
            continue
        if (
            snapshot.directory / "raw" / f"{page_slug(entry.canonical_url)}.html"
        ).is_symlink():
            raise IngestionError("Publication refuses a symbolic-link raw page")
        cached = snapshot.get(entry.canonical_url)
        if cached is None or sha256(cached.body).hexdigest() != cached.raw_sha256:
            raise IngestionError(
                f"Snapshot integrity check failed for {entry.canonical_url!r}"
            )
