# Worker

## Commands

- Install: `uv sync --all-packages`
- Check: `uv run ruff check apps/worker`, `uv run mypy`, `uv run pytest apps/worker/tests`
- Native worker: `docstral-worker workflows`; see [README.md](README.md).
- Local snapshot tools: `docstral-worker crawl`, `docstral-worker extract`, `make ingest`.

## Refresh

- Keep `docstral-refresh` input `{}`. The deterministic workflow owns the URL
  queue, deduplication and bounded activity concurrency. All I/O is in activities.
- Run `discover_urls`, then native `sync_page` calls. Read the sitemap once;
  each page returns its links, including unchanged pages. Redirects to another
  normalized identity return their target before fetching or indexing it.
- Download, extract, compare, split, embed and replace within one page activity.
  Pass lightweight results between activities; never persist HTML, Markdown,
  embeddings, snapshots or a local registry for refresh.
- Use existing admission, robots, extraction and Toolkit components. The HTTP
  adapter's state lives only for the current activity, never between activities.
- Fingerprints cover Markdown, title and processing settings at page level.
  Preserve the Markdown-only citation hash. Only confirmed writes allow skipping
  a page. Store confirmation in Vespa, outside the `docs` chunks collection.
- Prepare embeddings before mutation. Invalidate confirmation before replacing
  chunks, confirm after success. An interrupted mutation must be repaired even
  if live content returns to its old fingerprint.
- Extraction failure preserves existing content and returns a partial result.
  Failed exploration prevents all deletions. Use the complete present inventory,
  not just successfully indexed pages, when planning deletions.
- Retry transient activities at most three times with exponential backoff;
  permanent configuration or content failures must not be treated as outages.
  Keep 20-second heartbeats, a one-minute heartbeat timeout, five-minute activity
  timeouts and a 50-minute workflow timeout.
- Do not stop MCP, clear collections, migrate Vespa or control Kubernetes from
  ingestion. A changed page may briefly be absent or partial during replacement.
- No maintenance module, local lock or worker volume is required by refresh.
  Drain old workflow histories before deploying a changed activity graph.
  Run only one refresh per corpus, and none during migration. Worker startup
  never creates or activates a schedule.
- SDK trace redaction does not sanitize logs. Filter Vespa cleanup exceptions
  before console and OTel handlers receive them; retain workflow correlation.
- No module, class or function docstrings. Use explicit names and short tests
  of business behavior; replace only external services at their boundary.

## Standalone local tools

- Keep local `crawl` sequential, using `Fetcher`, URL admission and robots rules.
- Snapshots live under `data/snapshots/<UTC timestamp>/`; only complete runs
  update `current`. `extract` reads snapshots offline and refuses overwrite.
- `make ingest` rebuilds local Vespa before ingesting the current snapshot.
  These commands are separate from the native refresh workflow.
- Keep the full-page Toolkit splitter at 800 / 800 / 0, embeddings at 1024
  dimensions and citations at canonical-page granularity.
