from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from docstral_ingestion.cli import main
from docstral_ingestion.crawl import (
    CrawlCounts,
    CrawlEntry,
    CrawlResult,
    DiscoveryVia,
    PageDecision,
)
from docstral_ingestion.extract import (
    DocsHtmlConverter,
    ExtractionError,
    extract_page,
    outline,
)
from docstral_ingestion.sitemap import SitemapSnapshot
from docstral_ingestion.snapshot import write_snapshot
from mistralai.search.toolkit.ingestion import File
from mistralai.search.toolkit.ingestion.extractors import (
    HTMLExtractor,
    HtmlToMarkdownConverter,
)

DOCS = "https://docs.mistral.ai"
CRAWLED_AT = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)

# One page carrying every docs.mistral.ai markup pattern the converter handles:
# a nested article, a light/dark code block pair under language and non-language
# tab groups, an anchor on the heading's parent div, a table, a closed accordion.
PAGE = b"""<html><head><title>Guide | Mistral Docs</title></head>
<body>
<nav><a href="/">Home</a> <a href="/studio">Studio</a></nav>
<main>
<article class="prose">
<h1>Guide</h1>
<p>Set <code>safe_prompt</code> as documented on <a href="https://github.com/mistralai">GitHub</a>.</p>
<div id="setup"><h2>Setup</h2></div>
<p>Install the\x1b client first.</p>
<div data-language="sync">
<div data-language="python" role="tabpanel">
<div data-type="code">
<div class="relative dark:hidden"><pre><code>import os

client = Mistral()</code></pre></div>
<div class="relative hidden dark:block"><pre><code>import os

client = Mistral()</code></pre></div>
<button>Copy</button>
</div>
</div>
</div>
<div data-language="unix"><div data-type="code">
<div class="relative dark:hidden"><pre><code>uv sync</code></pre></div>
</div></div>
<h3 id="pricing">Pricing</h3>
<table><thead><tr><th>Model</th><th>Price</th></tr></thead>
<tbody><tr><td>mistral-small</td><td>0.15</td></tr></tbody></table>
<h3 data-state="closed"><button aria-expanded="false">Is it free?</button></h3>
<div hidden></div>
<article class="prose"><p>Nested note.</p></article>
</article>
</main>
<footer>Footer</footer>
</body></html>
"""

EXPECTED = "\n".join(
    [
        "# Guide",
        "",
        "Set `safe_prompt` as documented on [GitHub](https://github.com/mistralai).",
        "",
        "## Setup",
        "",
        "Install the� client first.",
        "",
        "```python",
        "import os",
        "",
        "client = Mistral()",
        "```",
        "",
        "```",
        "uv sync",
        "```",
        "",
        "### Pricing",
        "",
        "| Model | Price |",
        "| --- | --- |",
        "| mistral-small | 0.15 |",
        "",
        "Nested note.",
    ]
)


def test_extracts_the_page_to_the_expected_markdown() -> None:
    page = extract_page(f"{DOCS}/guide", PAGE)

    assert page.markdown == EXPECTED
    assert page.title == "Guide"
    assert page.chars == len(EXPECTED)
    assert page.content_hash == sha256(EXPECTED.encode()).hexdigest()
    # The accordion heading is in the HTML outline but not in the Markdown.
    assert len(page.sections) == 4


def test_outline_reads_the_title_and_lifts_anchors_from_the_parent_div() -> None:
    title, sections = outline(PAGE.decode())

    assert title == "Guide"
    assert [
        (section.level, section.heading, section.anchor) for section in sections
    ] == [
        (1, "Guide", None),
        (2, "Setup", "setup"),
        (3, "Pricing", "pricing"),
        (3, "Is it free?", None),
    ]


async def test_converter_satisfies_the_toolkit_contract() -> None:
    url = f"{DOCS}/guide"
    converter = DocsHtmlConverter()

    assert isinstance(converter, HtmlToMarkdownConverter)
    document = await HTMLExtractor(converter=converter).extract(
        File(path=url, name="guide.html", raw=PAGE, source_id=url)
    )
    assert document.content == EXPECTED
    assert document.content == extract_page(url, PAGE).markdown


def test_extract_page_requires_a_documentation_article() -> None:
    html = (
        b"<html><head><title>X | Mistral Docs</title></head>"
        b"<body><main></main></body></html>"
    )

    with pytest.raises(ExtractionError, match=r"article\.prose"):
        extract_page(f"{DOCS}/x", html)


def test_extract_page_rejects_an_empty_article() -> None:
    html = (
        b"<html><head><title>X | Mistral Docs</title></head>"
        b'<body><main><article class="prose"></article></main></body></html>'
    )

    with pytest.raises(ExtractionError, match="Empty Markdown"):
        extract_page(f"{DOCS}/x", html)


def test_extract_command_writes_markdown(tmp_path: Path) -> None:
    snapshots = tmp_path / "snapshots"
    extracted = tmp_path / "extracted"
    _write_snapshot(snapshots, ("/guide", _html("Guide", "Body")))

    exit_code = main(
        ["extract", "--snapshots", str(snapshots), "--out", str(extracted)]
    )

    destination = extracted / "20260903T120000Z"
    assert exit_code == 0
    assert (destination / "pages" / "guide.md").read_text() == "# Guide\n\nBody"


def test_extract_command_fails_without_a_current_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshots = tmp_path / "snapshots"
    extracted = tmp_path / "extracted"

    exit_code = main(
        ["extract", "--snapshots", str(snapshots), "--out", str(extracted)]
    )

    assert exit_code == 1
    assert not extracted.exists()
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "ExtractionError" in output
    assert "No current snapshot" in output
    assert "Traceback" not in output


def test_extract_command_records_invalid_utf8_and_continues(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshots = tmp_path / "snapshots"
    extracted = tmp_path / "extracted"
    _write_snapshot(
        snapshots,
        ("/good", _html("Good", "Kept")),
        ("/invalid", b"\xff"),
    )

    exit_code = main(
        ["extract", "--snapshots", str(snapshots), "--out", str(extracted)]
    )

    destination = extracted / "20260903T120000Z"
    assert exit_code == 1
    assert (destination / "pages" / "good.md").read_text() == "# Good\n\nKept"
    assert not (destination / "pages" / "invalid.md").exists()
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "Invalid UTF-8 HTML" in output
    assert "Traceback" not in output


def test_extract_command_refuses_to_overwrite_output(tmp_path: Path) -> None:
    snapshots = tmp_path / "snapshots"
    extracted = tmp_path / "extracted"
    _write_snapshot(snapshots, ("/guide", _html("Guide", "Body")))
    destination = extracted / "20260903T120000Z"
    destination.mkdir(parents=True)
    marker = destination / "keep"
    marker.write_text("unchanged")

    exit_code = main(
        ["extract", "--snapshots", str(snapshots), "--out", str(extracted)]
    )

    assert exit_code == 1
    assert marker.read_text() == "unchanged"
    assert list(destination.iterdir()) == [marker]


def test_extract_records_a_corrupted_raw_page_and_continues(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshots = tmp_path / "snapshots"
    extracted = tmp_path / "extracted"
    _write_snapshot(
        snapshots,
        ("/corrupt", _html("Corrupt", "Original")),
        ("/good", _html("Good", "Kept")),
        ("/missing", _html("Missing", "Gone")),
    )
    raw = snapshots / "20260903T120000Z" / "raw" / "corrupt.html"
    raw.write_bytes(_html("Corrupt", "Changed"))
    missing = snapshots / "20260903T120000Z" / "raw" / "missing.html"
    missing.unlink()

    exit_code = main(
        ["extract", "--snapshots", str(snapshots), "--out", str(extracted)]
    )

    destination = extracted / "20260903T120000Z"
    assert exit_code == 1
    assert not (destination / "pages" / "corrupt.md").exists()
    assert (destination / "pages" / "good.md").read_text() == "# Good\n\nKept"
    assert not (destination / "pages" / "missing.md").exists()
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "SHA-256" in output
    assert "SnapshotReadError" in output


def _html(title: str, body: str) -> bytes:
    return (
        f"<html><head><title>{title} | Mistral Docs</title></head>"
        f'<body><main><article class="prose"><h1>{title}</h1>'
        f"<p>{body}</p></article></main></body></html>"
    ).encode()


def _write_snapshot(root: Path, *pages: tuple[str, bytes]) -> None:
    entries = tuple(_stored(path, body) for path, body in pages)
    result = CrawlResult(
        pages=entries,
        counts=CrawlCounts(
            sitemap_english=len(entries),
            sitemap_french=0,
            discovered_by_link=0,
            admitted=len(entries),
            stored=len(entries),
            rejected=0,
            failed=0,
            status_200=len(entries),
            status_304=0,
            redirects=0,
            external_links=0,
            malformed_links=0,
            rejections={},
        ),
        complete=True,
        duration_seconds=0.1,
    )
    sitemap = SitemapSnapshot(
        url=f"{DOCS}/sitemap.xml",
        sha256="a" * 64,
        english_urls=tuple(entry.canonical_url for entry in entries),
        french_urls=(),
    )
    write_snapshot(root, CRAWLED_AT, sitemap, result)


def _stored(path: str, body: bytes) -> CrawlEntry:
    url = f"{DOCS}{path}"
    return CrawlEntry(
        canonical_url=url,
        requested_url=url,
        final_url=url,
        discovered_via=DiscoveryVia.SITEMAP,
        decision=PageDecision.STORED,
        status_code=200,
        raw_sha256=sha256(body).hexdigest(),
        body=body,
    )
