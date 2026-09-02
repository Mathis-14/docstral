# Ingestion

## Commands

- Install: `uv sync --all-packages`
- Lint: `uv run ruff check apps/ingestion`
- Format check: `uv run ruff format --check apps/ingestion`
- Typecheck: `uv run mypy`
- Test: `uv run pytest apps/ingestion/tests`

## Rules

- Keep URL admission, sitemap parsing, robots policy, and HTTP transport separate.
- Keep crawling sequential and make every rejection or failure explicit.
- Fetch through the one-method `Fetcher` protocol outside the HTTP adapter.
- Respect `robots.txt`, request cadence, retry bounds, and conditional requests.
- Use frozen Pydantic models for data crossing module boundaries.
- Keep network tests local with `httpx.MockTransport`.
