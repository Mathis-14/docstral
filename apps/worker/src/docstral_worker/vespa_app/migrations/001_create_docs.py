"""Create the chunk-level documentation index."""

from typing import override

from mistralai.search.toolkit.embedding import MistralEmbeddingPreset
from mistralai.search.toolkit.plugins.vespa.app.schemas.app import (
    FieldDefinition,
    IndexingMode,
    SearchMode,
)
from mistralai.search.toolkit.plugins.vespa.migration import (
    VespaMigration,
    create_schema,
    set_app_name,
    set_default_ranking_weights,
)


class CreateDocs(VespaMigration):
    """Define Docstral's first Vespa schema."""

    @override
    def migrate(self) -> None:
        set_app_name("docstral")
        create_schema(
            name="docs",
            mode=SearchMode.INDEX,
            indexing_mode=IndexingMode.DOCUMENT_PER_CHUNK,
            embedding_model=MistralEmbeddingPreset.MISTRAL_EMBED_DIM_1024,
            fields=[
                FieldDefinition.TextField(name="title"),
                FieldDefinition.StringField(name="content_hash"),
            ],
        )
        set_default_ranking_weights(
            "docs",
            {
                "bm25_content": 0.0,
                "bm25_title": 0.0,
                "match_content": 0.0,
                "match_title": 0.0,
                "content_embedding_closeness": 1.0,
                "content_embedding_cosine_similarity_score": 1.0,
            },
        )
