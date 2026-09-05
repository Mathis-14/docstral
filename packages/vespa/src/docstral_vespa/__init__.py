"""Vespa application and search-index access for Docstral."""

from pathlib import Path

from mistralai.search.toolkit.plugins.vespa import (
    VespaApp,
    VespaClient,
    VespaClientConfig,
    VespaSearchIndex,
    create_search_index,
)

COLLECTION_NAME = "docs"

app = VespaApp(Path(__file__).parent)


def search_index(endpoint: str) -> VespaSearchIndex:
    """Return the Docstral Vespa index at the configured endpoint."""
    return app.get_search_index(
        VespaClientConfig(endpoint=endpoint), collection_name=COLLECTION_NAME
    )


def index_for_client(client: VespaClient) -> VespaSearchIndex:
    """Use a caller-owned client for publication and close it with the run."""
    for schema in app.app_definition.schemas:
        if schema.document_type == COLLECTION_NAME:
            return create_search_index(client, schema)
    raise ValueError(f"Missing Vespa schema {COLLECTION_NAME!r}")
