from datetime import datetime
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from docstral_worker import IngestionError
from docstral_worker.crawl import SHA256_PATTERN, CrawlResult
from docstral_worker.urls import admit, canonicalize


class SnapshotReadError(IngestionError):
    pass


class SnapshotPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    path: str
    sha256: str = Field(pattern=SHA256_PATTERN)

    @field_validator("url")
    @classmethod
    def canonical_documentation_url(cls, value: str) -> str:
        target = canonicalize(value, value)
        if target.url != value or not admit(target).admitted:
            raise ValueError("Snapshot URL must be canonical and in scope")
        return value


class SnapshotManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[2]
    crawled_at: datetime
    pages: tuple[SnapshotPage, ...]


class CurrentSnapshot:
    def __init__(self, directory: Path, manifest: SnapshotManifest) -> None:
        self.directory = directory
        self.manifest = manifest
        self._pages = {page.url: page for page in manifest.pages}
        if len(self._pages) != len(manifest.pages):
            raise SnapshotReadError("Snapshot contains duplicate page URLs")

    def get(self, url: str) -> bytes:
        page = self._pages.get(url)
        if page is None:
            raise SnapshotReadError(f"Page {url!r} is missing from the snapshot")
        try:
            body = _read(self.directory, page.path)
            if sha256(body).hexdigest() != page.sha256:
                raise SnapshotReadError(f"HTML hash mismatch for {url!r}; crawl again")
            return body
        except OSError as error:
            raise SnapshotReadError(f"Cannot read HTML for {url!r}: {error}") from error


def _read(root: Path, relative: str) -> bytes:
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise SnapshotReadError(f"Invalid snapshot path: {relative!r}")
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise SnapshotReadError(f"Snapshot path is a symbolic link: {current}")
    if not path.is_file():
        raise SnapshotReadError(f"Snapshot file is missing or not regular: {path}")
    return path.read_bytes()


def current_snapshot(root: Path) -> CurrentSnapshot | None:
    if not (root / "current").exists() and not (root / "current").is_symlink():
        return None
    try:
        name = _read(root, "current").decode().strip()
        if not name or Path(name).name != name or name in (".", ".."):
            raise SnapshotReadError("Invalid current snapshot directory")
        payload = _read(root, f"{name}/manifest.json")
        manifest = SnapshotManifest.model_validate_json(payload)
        return CurrentSnapshot(root / name, manifest)
    except (IngestionError, OSError, UnicodeError, ValidationError) as error:
        raise SnapshotReadError(
            f"Cannot read snapshot; run docstral-worker crawl again: {error}"
        ) from error


def page_slug(url: str) -> str:
    return sha256(url.encode()).hexdigest()


def write_snapshot(
    root: Path, crawled_at: datetime, result: CrawlResult
) -> Path | None:
    if not result.complete:
        return None
    name = crawled_at.strftime("%Y%m%dT%H%M%S%fZ")
    destination = root / name
    if destination.exists():
        raise IngestionError(f"Snapshot already exists: {destination}")
    try:
        root.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".snapshot-", dir=root) as temporary:
            staging = Path(temporary)
            (staging / "raw").mkdir()
            pages: list[SnapshotPage] = []
            for page in result.pages:
                if page.status != "downloaded":
                    continue
                relative = f"raw/{page_slug(page.url)}.html"
                (staging / relative).write_bytes(page.body)
                pages.append(
                    SnapshotPage(
                        url=page.url,
                        path=relative,
                        sha256=sha256(page.body).hexdigest(),
                    )
                )
            manifest = SnapshotManifest(
                version=2, crawled_at=crawled_at, pages=tuple(pages)
            )
            (staging / "manifest.json").write_text(manifest.model_dump_json(indent=2))
            staging.rename(destination)
        with NamedTemporaryFile(
            mode="w", prefix=".current-", dir=root, delete=False
        ) as pointer:
            pointer.write(f"{name}\n")
        try:
            Path(pointer.name).replace(root / "current")
        finally:
            Path(pointer.name).unlink(missing_ok=True)
    except OSError as error:
        raise IngestionError(f"Cannot write snapshot {destination}: {error}") from error
    return destination
