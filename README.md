# Docstral

Accurate, grounded Q&A over Mistral's public documentation, exposed through an MCP server.

## Setup

```sh
uv sync --all-packages
uv run pre-commit install
cp .env.example .env
```

Set `MISTRAL_API_KEY` in `.env`.

## Architecture

- `apps/worker` owns all corpus mutations: crawling, extraction, snapshots,
  and indexing.
- `apps/backend` owns read-only retrieval and will later own grounded Q&A.
- `packages/vespa` is the shared Vespa application and index contract.

## Crawl

```sh
uv run docstral-worker crawl
```

## Extract

```sh
uv run docstral-worker extract
```

## Index

Docker must be running.

```sh
make ingest
```
