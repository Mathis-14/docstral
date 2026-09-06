import json
import os
from pathlib import Path

import pytest
from docstral_worker.index_state import (
    IndexedPage,
    IndexState,
    IndexStateError,
    IndexStateStore,
)
from mistralai.search.toolkit.document import compute_id
from pydantic import ValidationError

URL = "https://docs.mistral.ai/guide"


def test_missing_state_is_distinct_from_empty_initialized_state(tmp_path: Path) -> None:
    store = IndexStateStore(tmp_path)

    assert store.read() is None
    assert not (tmp_path / "index-state.json").exists()
    store.write(IndexState())

    assert IndexStateStore(tmp_path).read() == IndexState()


def test_state_survives_reload_and_atomic_replacement(tmp_path: Path) -> None:
    store = IndexStateStore(tmp_path)
    first = IndexState(pages={URL: IndexedPage(document_id=compute_id(URL))})
    store.write(first)
    path = tmp_path / "index-state.json"

    with path.open("rb") as previous:
        updated = IndexState(
            revision=1,
            pages={
                URL: IndexedPage(
                    document_id=compute_id(URL), index_hash="a" * 64, pending=True
                )
            },
        )
        store.write(updated)
        assert IndexState.model_validate_json(previous.read()) == first

    assert IndexStateStore(tmp_path).read() == updated
    assert sorted(item.name for item in tmp_path.iterdir()) == ["index-state.json"]
    assert path.stat().st_mode & 0o777 == 0o600


def test_index_state_accepts_canonical_excluded_urls() -> None:
    url = "https://docs.mistral.ai/api/endpoint/chat"
    state = IndexState(pages={url: IndexedPage(document_id=compute_id(url))})

    assert url in state.pages


def test_index_state_rejects_noncanonical_urls() -> None:
    # SourceIdentity owns the full URL matrix; this checks the registry uses it.
    url = "https://docs.mistral.ai/en/guide"
    with pytest.raises(ValidationError, match="canonical"):
        IndexState(pages={url: IndexedPage(document_id=compute_id(url))})


def test_index_state_rejects_a_document_id_from_another_page() -> None:
    with pytest.raises(ValidationError, match="document_id must match"):
        IndexState(pages={URL: IndexedPage(document_id=compute_id(URL + "/other"))})


@pytest.mark.parametrize("index_hash", ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64])
def test_indexed_page_rejects_invalid_hash(index_hash: str) -> None:
    with pytest.raises(ValidationError):
        IndexedPage(document_id=compute_id(URL), index_hash=index_hash)


@pytest.mark.parametrize(
    "content",
    [
        b"not-json",
        b"\xff",
        b"[]",
        b'{"format_version": 2}',
        b'{"revision": -1}',
        b'{"unexpected": true}',
        b'{"pages": {"https://docs.mistral.ai/guide": {"document_id": "wrong"}}}',
    ],
)
def test_read_rejects_corrupt_or_incompatible_state(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "index-state.json"
    path.write_bytes(content)

    with pytest.raises(IndexStateError, match="invalid JSON or incompatible state"):
        IndexStateStore(tmp_path).read()

    assert path.read_bytes() == content


def test_read_error_does_not_expose_untrusted_state_values(tmp_path: Path) -> None:
    secret_url = (
        "https://user:private-password@docs.mistral.ai/guide?token=private-token"
    )
    (tmp_path / "index-state.json").write_text(
        json.dumps({"pages": {secret_url: {"document_id": "private-id"}}})
    )

    with pytest.raises(IndexStateError) as error:
        IndexStateStore(tmp_path).read()

    assert "private" not in str(error.value)


@pytest.mark.parametrize("operation", ["read", "write"])
@pytest.mark.parametrize(
    "kind", ["file_symlink", "dangling_symlink", "directory", "fifo"]
)
def test_store_rejects_nonregular_state_paths(
    tmp_path: Path, operation: str, kind: str
) -> None:
    path = tmp_path / "index-state.json"
    target = tmp_path / "keep"
    target.write_text("untouched")
    if kind == "file_symlink":
        path.symlink_to(target)
    elif kind == "dangling_symlink":
        path.symlink_to(tmp_path / "absent")
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    store = IndexStateStore(tmp_path)

    with pytest.raises(IndexStateError):
        if operation == "read":
            store.read()
        else:
            store.write(IndexState())

    assert target.read_text() == "untouched"
    assert kind != "dangling_symlink" or not (tmp_path / "absent").exists()


@pytest.mark.parametrize("operation", ["read", "write"])
@pytest.mark.parametrize("parent", [False, True])
def test_store_rejects_symlink_directory_components(
    tmp_path: Path, operation: str, parent: bool
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    if parent:
        (real / "data").mkdir()
    store = IndexStateStore(link / "data" if parent else link)

    with pytest.raises(IndexStateError):
        if operation == "read":
            store.read()
        else:
            store.write(IndexState())

    assert not tuple(real.rglob("index-state.json"))


@pytest.mark.parametrize("operation", ["read", "write"])
def test_missing_directory_is_an_error(tmp_path: Path, operation: str) -> None:
    store = IndexStateStore(tmp_path / "missing")

    with pytest.raises(IndexStateError):
        if operation == "read":
            store.read()
        else:
            store.write(IndexState())

    assert not store.directory.exists()


def test_write_revalidates_mutated_pages_without_replacing_state(
    tmp_path: Path,
) -> None:
    store = IndexStateStore(tmp_path)
    original = IndexState()
    store.write(original)
    malformed = IndexState()
    malformed.pages[URL] = IndexedPage(document_id="wrong")

    with pytest.raises(IndexStateError, match="Cannot write invalid index state"):
        store.write(malformed)

    assert store.read() == original
