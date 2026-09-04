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
- Run local ingestion through `make ingest`, which rebuilds Vespa before feeding
  the complete current snapshot.
- Pass each full Markdown page to the toolkit's standard splitter and keep
  citations at canonical-page URL granularity.
- Keep `mistral-embed` and the Vespa schema explicitly aligned at 1024
  dimensions.
