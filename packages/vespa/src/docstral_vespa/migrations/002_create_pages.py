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
)


class CreatePages(VespaMigration):
    @override
    def migrate(self) -> None:
        create_schema(
            name="pages",
            mode=SearchMode.INDEX,
            indexing_mode=IndexingMode.DOCUMENT_PER_CHUNK,
            embedding_model=MistralEmbeddingPreset.MISTRAL_EMBED_DIM_1024,
            fields=[FieldDefinition.StringField(name="index_hash")],
        )
