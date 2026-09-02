# Docstral

Accurate, grounded Q&A over Mistral's public documentation, exposed through an MCP server.

Status: bootstrap

## Setup

```sh
cp .env.example .env
```

Set `MISTRAL_API_KEY` in `.env`.

```sh
uv sync
uv run pre-commit run --all-files
```
