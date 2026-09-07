# Docstral

Accurate, grounded Q&A over Mistral's public documentation, exposed through an MCP server.

## Start locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), start Docker
with at least 4 GB available, and use a Mistral API key with Workflows,
embeddings and answer-model access.

```sh
cp .env.example .env
# Set MISTRAL_API_KEY in .env
make local
```

The first launch starts Vespa, applies the schema, starts a local worker and
runs the same `docstral-refresh` workflow as production. Once pages are indexed,
MCP starts at `http://127.0.0.1:8000/mcp`. Later launches reuse the corpus.
The workflow history lives in Mistral; local execution still requires internet.

Run `make refresh` to update the corpus explicitly. Ctrl+C stops the launched
worker and MCP while preserving Vespa data. No schedule is created automatically.
A partial refresh is printed with its failed URLs; MCP requires at least one
confirmed indexed page. See the [worker guide](apps/worker/README.md) for settings and recovery.

`DOCSTRAL_ANSWER_MODEL` selects the answer model (default: `ministral-8b-2512`);
restart MCP after changing it. Embeddings and the corpus are unchanged.

With [Vibe](https://docs.mistral.ai/vibe/code/cli) installed, register the local
MCP server once:

```sh
make vibe
```

Vibe reports when the same server is already configured and leaves conflicting
configurations unchanged. The command uses `DOCSTRAL_MCP_PORT` from `.env`
(default: 8000). Its non-secret header selects Vibe's static mode without OAuth.
Run `make vibe local` to register the server and then start the local environment.
Restart Vibe and use `/mcp docstral` to check its tools.

## Development

Run `make check` for lint, format, typing and unit tests.
Install local Git hooks once with `uv run pre-commit install`.

- `apps/mcp`: MCP transport, authentication and Python Q&A in `qa/`.
- `apps/worker`: workflows, crawling and incremental indexing.
- `common`: shared Vespa schema and index constructors.

The [system prompt](apps/mcp/src/docstral_mcp/qa/prompt.md) is bundled with MCP.
It loads at startup; restart after editing it locally, or rebuild the deployed image.

See [worker operations](apps/worker/README.md),
[OAuth and Docker](deployment/README.md#google-oauth-invited-users),
[GKE deployment](deployment/README.md) and
[evaluations](evals/README.md).
