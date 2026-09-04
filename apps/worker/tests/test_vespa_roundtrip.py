import os
from hashlib import sha256

import pytest
from docstral_vespa import search_index
from docstral_worker.ingest import DocsChunkMetadata
from mistralai.search.toolkit.document import (
    Document,
    DocumentChunk,
    compute_char_locator,
    compute_id,
)
from mistralai.search.toolkit.errors import SearchToolkitException
from mistralai.search.toolkit.search import VectorSearchQuery


@pytest.mark.integration
async def test_vespa_roundtrip_preserves_chunk_metadata() -> None:
    port = os.environ.get("VESPA_QUERY_PORT", "8080")
    endpoint = os.environ.get("VESPA_ENDPOINT", f"http://localhost:{port}")
    index = search_index(endpoint)
    source_id = "https://docs.mistral.ai/docstral-integration-test"
    content = "# Integration\n\nDocstral Vespa round-trip sentinel."
    content_hash = sha256(content.encode()).hexdigest()
    vector = [1.0, *([0.0] * 1023)]
    document = Document(
        source_id=source_id,
        content=content,
        chunks=[
            DocumentChunk(
                source_id=source_id,
                locator=compute_char_locator(0, len(content)),
                start_offset=0,
                end_offset=len(content),
                parent_ref=compute_id(source_id),
                content=content,
                metadata=DocsChunkMetadata(
                    title="Integration",
                    content_hash=content_hash,
                ),
                embedding=vector,
            )
        ],
    )
    indexed = False
    try:
        try:
            await index.index_document(document)
        except SearchToolkitException as exc:
            pytest.fail(f"Vespa is unavailable or not migrated at {endpoint}: {exc}")
        indexed = True
        results = await index.search(
            VectorSearchQuery(
                query="Docstral Vespa round-trip sentinel",
                embedding=vector,
                top_k=1,
            )
        )

        assert len(results) == 1
        chunk = results[0].chunk
        assert chunk.source_id == source_id
        assert chunk.metadata["title"] == "Integration"
        assert chunk.metadata["content_hash"] == content_hash
    finally:
        if indexed:
            await index.delete_document(document.id)
