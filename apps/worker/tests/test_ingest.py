import json
import subprocess
import sys
from pathlib import Path

import pytest
from docstral_worker.cli import main
from docstral_worker.extract import extract_page
from docstral_worker.ingest import ingest_snapshot
from docstral_worker.refresh.activities import corpus_client
from docstral_worker.refresh.indexing import PageIndexer
from docstral_worker.refresh.models import DownloadedPage
from mistralai.search.toolkit.clients.mistral import build_mistral_client
from mistralai.search.toolkit.embedding import MODEL_1024_EMBEDDING, MistralEmbedder
from mistralai.search.toolkit.errors import SearchToolkitException
from worker_fixtures import DOCS, Services, html, snapshot
from worker_fixtures import services as services


@pytest.mark.parametrize("body", ["Short evidence", "word " * 2000])
async def test_page_indexing_preserves_chunk_content_and_citation_metadata(
    services: Services, body: str
) -> None:
    raw = html("Guide", body)
    expected = extract_page(DOCS + "/guide", raw)
    with build_mistral_client() as mistral:
        async with mistral, corpus_client() as corpus:
            indexer = PageIndexer(
                corpus, MistralEmbedder(client=mistral, model_name=MODEL_1024_EMBEDDING)
            )
            result = await indexer.sync(
                DownloadedPage(url=DOCS + "/guide", html=raw, links=())
            )
    assert result.status == "indexed"
    chunks = [
        fields for path, fields in services.documents.items() if "/docs/docs/" in path
    ]
    assert len(chunks) > 1 if len(body) > 1000 else len(chunks) == 1
    for chunk in chunks:
        assert chunk["source_id"] == DOCS + "/guide"
        metadata = json.loads(str(chunk["metadata"]))
        assert metadata["title"] == "Guide"
        assert metadata["content_hash"] == expected.content_hash
        assert str(chunk["content"]) in expected.markdown
    embedding_inputs = [
        text
        for request in services.requests
        if request.url.host == "mistral.test"
        for text in json.loads(request.content)["input"]
    ]
    assert embedding_inputs == [chunk["content"] for chunk in chunks]


async def test_offline_ingestion_continues_after_page_failure(
    services: Services, tmp_path: Path
) -> None:
    saved = snapshot(tmp_path, ("/good", html("Good", "Kept")), ("/invalid", b"\xff"))
    with build_mistral_client() as mistral:
        async with mistral, corpus_client() as corpus:
            result = await ingest_snapshot(
                saved,
                PageIndexer(
                    corpus,
                    MistralEmbedder(client=mistral, model_name=MODEL_1024_EMBEDDING),
                ),
            )
    assert (result.indexed, result.failed) == (1, 1)
    assert {fields["source_id"] for fields in services.documents.values()} == {
        DOCS + "/good"
    }


async def test_offline_ingestion_stops_on_index_service_failure(
    services: Services, tmp_path: Path
) -> None:
    saved = snapshot(
        tmp_path, ("/first", html("First", "Body")), ("/second", html("Second", "Body"))
    )
    services.fail = "index"
    with build_mistral_client() as mistral:
        async with mistral, corpus_client() as corpus:
            with pytest.raises(SearchToolkitException):
                await ingest_snapshot(
                    saved,
                    PageIndexer(
                        corpus,
                        MistralEmbedder(
                            client=mistral, model_name=MODEL_1024_EMBEDDING
                        ),
                    ),
                )
    assert all(
        fields["source_id"] != DOCS + "/second"
        for fields in services.documents.values()
    )


def test_ingest_command_fails_without_current_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["ingest", "--snapshots", str(tmp_path)])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "No current snapshot" in output
    assert "MISTRAL_API_KEY" not in output
    assert "Traceback" not in output


def test_cli_does_not_eagerly_import_vespa() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import docstral_worker.cli; "
            "assert 'docstral_vespa' not in sys.modules",
        ],
        check=True,
    )


def test_make_ingest_rebuilds_local_vespa() -> None:
    result = subprocess.run(
        ["make", "--dry-run", "ingest"],
        check=True,
        capture_output=True,
        text=True,
    )

    commands = result.stdout
    steps = (
        "mistral-vespa local down",
        "mistral-vespa local up",
        "mistral-vespa migrate",
        "docstral-worker ingest",
    )
    positions = [commands.index(step) for step in steps]
    assert positions == sorted(positions)
    assert "--app-dir packages/vespa/src/docstral_vespa" in commands


@pytest.mark.parametrize("endpoint", ["localhost:8080", "ftp://localhost:8080"])
def test_ingest_command_rejects_invalid_endpoint(endpoint: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["ingest", "--vespa-endpoint", endpoint])

    assert exc_info.value.code == 2
