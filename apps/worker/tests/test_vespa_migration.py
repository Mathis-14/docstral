from docstral_worker.vespa_app import app
from mistralai.search.toolkit.plugins.vespa.app.schemas.app import (
    FieldDefinition,
    IndexingMode,
    SearchMode,
)


def test_migration_defines_the_docs_index() -> None:
    definition = app.app_definition

    assert definition.name == "docstral"
    assert definition.query_profiles == []
    assert len(definition.schemas) == 1
    schema = definition.schemas[0]
    assert schema.document_type == "docs"
    assert schema.mode is SearchMode.INDEX
    assert schema.indexing_mode is IndexingMode.DOCUMENT_PER_CHUNK
    assert schema.embedding_model.name == "mistral-embed"
    assert schema.embedding_model.dimensions == 1024

    fields = {field.name: field for field in schema.fields}
    assert isinstance(fields["title"], FieldDefinition.TextField)
    assert isinstance(fields["content_hash"], FieldDefinition.StringField)
    assert fields["content_hash"].fast_search is False
    assert {"url", "anchor", "heading_path", "document_title"}.isdisjoint(fields)
    assert schema.default_ranking_weights == {
        "bm25_content": 0.0,
        "bm25_title": 0.0,
        "match_content": 0.0,
        "match_title": 0.0,
        "content_embedding_closeness": 1.0,
        "content_embedding_cosine_similarity_score": 1.0,
    }
