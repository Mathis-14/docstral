# Worker

## Commands

- Install: `uv sync --all-packages`
- Lint: `uv run ruff check apps/worker`
- Format check: `uv run ruff format --check apps/worker`
- Typecheck: `uv run mypy`
- Test: `uv run pytest apps/worker/tests`
- Crawl: `uv run docstral-worker crawl`
- Extract: `uv run docstral-worker extract`
- Ingest: `make ingest`
- Cluster refresh and maintenance: see [README.md](README.md).
- Mistral Workflows polling worker: `docstral-worker workflows` (cluster configuration required).

## Rules

- Keep URL admission, sitemap parsing, robots policy, and HTTP transport separate.
- Keep crawling sequential and make every rejection or failure explicit.
- Fetch through the one-method `Fetcher` protocol outside the HTTP adapter.
- Respect `robots.txt`, request cadence, retry bounds, and conditional requests.
- Use frozen Pydantic models for data crossing module boundaries.
- Keep network tests local with `httpx.MockTransport`.
- Snapshots live under `data/snapshots/<UTC timestamp>/`; `current` names the
  latest complete run and is the only snapshot consumers read.
- Extractions live under `data/extracted/<snapshot>/pages/<slug>.md`; an existing
  destination is refused, never overwritten.
- Only `crawl` fetches public documentation. `extract` is offline; `ingest`
  reads the `current` raw snapshot and calls only Mistral embeddings and Vespa.
- Keep indexing sequential. Continue after a page-local `IngestionError`, but
  stop the run when the splitter, embedding API, or Vespa fails.
- The incremental engine extracts before comparing article fingerprints. Keep
  `content_hash` as the Markdown hash; the indexing fingerprint also covers title
  and processing settings. Only confirmed writes establish indexed fingerprints.
  A pending article must be indexed again even when its previous hash matches.
- Incremental removals use the complete crawl inventory, never the subset of
  successfully extracted documents. Extraction failures are partial results
  without a percentage threshold; other stage failures remain explicit errors.
- Prepared artifacts use the Toolkit's public document serialization with an
  explicit `DocsChunkMetadata` registration. Keep immutable, versioned stage
  outputs on the snapshot volume and pass typed references between stages.
  They are not a cache reused by a new ingestion execution.
- Run local ingestion through `make ingest`, which rebuilds Vespa before feeding
  the complete current snapshot.
- Pass each full Markdown page to the toolkit's standard splitter and keep
  citations at canonical-page URL granularity.
- Keep `mistral-embed` and the Vespa schema explicitly aligned at 1024
  dimensions.
- Cluster refresh runs six native activities in order: crawl, extract,
  compare_hashes, split, embed, index_delta. Keep the public `docstral-refresh`
  input `{}`. All I/O belongs in activities; the workflow remains deterministic.
- Ingestion never controls Kubernetes, clears the collection or stops MCP.
  Use Toolkit replacement per article; a changed article can briefly be absent
  or partial during its update. Image deployment owns runtime rollout.
- Each activity holds the worker volume lock and verifies maintenance, current
  snapshot identity and index-state revision before proceeding. Maintenance
  persists between activities and worker replacements. An old
  `.publication-pending` requires recovery with the previous release.
- Keep one activity attempt, dependency retries, 20-second heartbeats and a
  one-minute heartbeat timeout. Share one cooperative 50-minute deadline across
  activities, with the SDK's 55-minute timeout on each. Await synchronous crawl
  cancellation before releasing its lock.
- SDK trace redaction does not sanitize logs. Filter Vespa cleanup exceptions
  before console and OTel handlers receive them; preserve the event and workflow
  correlation. Test both log bodies and attributes when feed and cleanup fail.
- Retention runs after completed incremental indexing. Keep two complete
  snapshots and one failed snapshot, additionally protecting current,
  unrecognised directories and symlink targets.
- Never create a schedule on worker startup. Workflow inputs cannot select
  cluster endpoints, paths or credentials. Recovery starts a fresh execution.
