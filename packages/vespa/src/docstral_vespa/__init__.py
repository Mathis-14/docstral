"""Vespa application and search-index access for Docstral."""

from pathlib import Path

from mistralai.search.toolkit.plugins.vespa import (
    VespaApp,
    VespaClientConfig,
    VespaSearchIndex,
)

COLLECTION_NAME = "docs"

app = VespaApp(Path(__file__).parent)


def search_index(endpoint: str) -> VespaSearchIndex:
    """Return the Docstral Vespa index at the configured endpoint."""
    return app.get_search_index(
        VespaClientConfig(endpoint=endpoint), collection_name=COLLECTION_NAME
    )
