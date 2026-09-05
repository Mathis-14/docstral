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
- `apps/backend` owns read-only retrieval and grounded Q&A.
- `apps/mcp` exposes the backend through a thin FastMCP transport adapter.
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

## MCP

Vespa must be running with an indexed corpus.

```sh
make mcp
```

The FastMCP Streamable HTTP endpoint is `http://127.0.0.1:8000/mcp`.

Connect it to Vibe:

```sh
vibe mcp add docstral \
  --url http://127.0.0.1:8000/mcp \
  --transport streamable-http \
  --header X-Docstral-Client=vibe
```

The non-secret header selects Vibe's static connection mode; this local baseline
does not enforce authentication.

## Retrieval evaluation

The [evaluation report](evals/README.md) documents the reviewed dataset, retained
and deferred metrics, dense/hybrid results, and the local reproduction command.
It measures retrieved evidence, not generated-answer quality.
