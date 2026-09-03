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

`apps/worker` is currently the only application. It runs locally and owns all
corpus mutations: crawling, extraction, snapshots, and later indexing. Backend
and MCP applications will be added only when they contain runnable code.

## Crawl

```sh
uv run docstral-worker crawl
```

## Extract

```sh
uv run docstral-worker extract
```
