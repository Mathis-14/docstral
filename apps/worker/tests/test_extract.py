from hashlib import sha256
from pathlib import Path

import pytest
from docstral_worker.cli import main
from docstral_worker.extract import (
    DocsHtmlConverter,
    ExtractionError,
    extract_page,
    outline,
)
from docstral_worker.snapshot import page_slug
from mistralai.search.toolkit.ingestion import File
from mistralai.search.toolkit.ingestion.extractors import (
    HTMLExtractor,
    HtmlToMarkdownConverter,
)
from worker_fixtures import html, snapshot

DOCS = "https://docs.mistral.ai"


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
        File(
            path=url, name=f"{page_slug(DOCS + '/guide')}.html", raw=PAGE, source_id=url
        )
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
    snapshot(snapshots, ("/guide", html("Guide", "Body")))

    exit_code = main(
        ["extract", "--snapshots", str(snapshots), "--out", str(extracted)]
    )

    destination = extracted / "20260903T120000000000Z"
    assert exit_code == 0
    assert (
        destination / "pages" / f"{page_slug(DOCS + '/guide')}.md"
    ).read_text() == "# Guide\n\nBody"


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
    snapshot(
        snapshots,
        ("/good", html("Good", "Kept")),
        ("/invalid", b"\xff"),
    )

    exit_code = main(
        ["extract", "--snapshots", str(snapshots), "--out", str(extracted)]
    )

    destination = extracted / "20260903T120000000000Z"
    assert exit_code == 1
    assert (
        destination / "pages" / f"{page_slug(DOCS + '/good')}.md"
    ).read_text() == "# Good\n\nKept"
    assert not (destination / "pages" / f"{page_slug(DOCS + '/invalid')}.md").exists()
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "Invalid UTF-8 HTML" in output
    assert "Traceback" not in output


def test_extract_command_refuses_to_overwrite_output(tmp_path: Path) -> None:
    snapshots = tmp_path / "snapshots"
    extracted = tmp_path / "extracted"
    snapshot(snapshots, ("/guide", html("Guide", "Body")))
    destination = extracted / "20260903T120000000000Z"
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
    snapshot(
        snapshots,
        ("/corrupt", html("Corrupt", "Original")),
        ("/good", html("Good", "Kept")),
        ("/missing", html("Missing", "Gone")),
    )
    raw = (
        snapshots
        / "20260903T120000000000Z"
        / "raw"
        / f"{page_slug(DOCS + '/corrupt')}.html"
    )
    raw.write_bytes(html("Corrupt", "Changed"))
    missing = (
        snapshots
        / "20260903T120000000000Z"
        / "raw"
        / f"{page_slug(DOCS + '/missing')}.html"
    )
    missing.unlink()

    exit_code = main(
        ["extract", "--snapshots", str(snapshots), "--out", str(extracted)]
    )

    destination = extracted / "20260903T120000000000Z"
    assert exit_code == 1
    assert not (destination / "pages" / f"{page_slug(DOCS + '/corrupt')}.md").exists()
    assert (
        destination / "pages" / f"{page_slug(DOCS + '/good')}.md"
    ).read_text() == "# Good\n\nKept"
    assert not (destination / "pages" / f"{page_slug(DOCS + '/missing')}.md").exists()
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "HTML hash mismatch" in output
    assert "SnapshotReadError" in output
