# Docstral

**Mistral documentation, one question away.**

Docstral is an MCP server for grounded Q&A over Mistral documentation. Use its `ask_docs` tool from Vibe or another compatible MCP client to get English answers with source citations.

https://github.com/user-attachments/assets/20f86b44-8bfb-4ec7-b533-59fe5a7d9802

## Use with an MCP client

Connect your client using:

- **URL:** `https://docstral-mcp.dev/mcp`
- **Transport:** Streamable HTTP
- **Authentication:** Google OAuth, with an authorized Google account

With [Vibe](https://github.com/mistralai/mistral-vibe) installed, add Docstral:

```sh
vibe mcp add docstral --url https://docstral-mcp.dev/mcp --transport streamable-http
```

Open or restart Vibe, complete the Google sign-in when prompted, then ask anything about Mistral documentation.

## Run locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), have `make` available, and start Docker with at least **4 GB of memory**.

```sh
git clone https://github.com/Mathis-14/docstral.git
cd docstral
cp .env.example .env
```

Set `MISTRAL_API_KEY` in `.env`, then start:

```sh
make local
```

Your key needs access to **Mistral Workflows**, **mistral-embed** and the answer model (**ministral-8b-2512** by default). Local execution requires internet and uses your Mistral API account for embeddings and generation.

The first launch installs dependencies, starts Vespa and ingests the documentation. **Allow several minutes.** Once pages are indexed, MCP starts at `http://127.0.0.1:8000/mcp`. Later launches reuse the indexed corpus.

Register the local server in Vibe from another terminal:

```sh
vibe mcp add docstral-local --url http://127.0.0.1:8000/mcp \
  --transport streamable-http --header X-Docstral-Client=vibe
```

Open or restart Vibe, then use `/mcp docstral-local` to check that `ask_docs` is available.

The local server uses **Streamable HTTP without authentication**. If you change `DOCSTRAL_MCP_PORT` in `.env`, update your client URL accordingly.

### Everyday commands

| Command | Purpose |
| --- | --- |
| `make local` or `make` | Start locally; initialize the corpus if needed. |
| `make ingestion` | Update the local corpus incrementally, then exit. |
| `make check` | Run Ruff, format checks, strict mypy and unit tests. |

`Ctrl+C` stops the worker and MCP; Vespa data is preserved. No schedule is created automatically.

See [.env.example](.env.example) for configuration and the [worker guide](apps/worker/README.md) for advanced commands and recovery.

## How it works

The worker discovers documentation pages, extracts their content and indexes chunks in Vespa. The MCP server retrieves relevant chunks, generates an answer and builds citations from their metadata.

Local and production ingestion use the same **`docstral-refresh`** workflow. Unchanged pages require no new embeddings or writes. Refreshes update pages progressively while MCP remains available.

| Component | Role |
| --- | --- |
| FastMCP | `ask_docs` tool, MCP transport and Google OAuth. |
| Mistral Search Toolkit + Vespa | Chunking, indexing and retrieval. |
| Mistral models | Embeddings and answer generation. |
| Crawlee | Page downloads, HTML parsing and link discovery. |
| Mistral Workflows | Incremental ingestion with per-page progress and retries. |
| Python 3.13 + uv | Runtime and locked dependencies. |

`apps/mcp/` owns retrieval and answering, `apps/worker/` owns ingestion, and `packages/vespa/` contains the shared index definition.

## Results

On the September 2026 retrieval development set, dense retrieval at five chunks covered every required evidence group for **54/62 questions**, with **89.52% macro evidence recall**.

These measure retrieval on a development set, not answer accuracy or performance on an unseen holdout. See [results and limitations](evals/RESULTS.md) for the experiments and the [evaluation guide](evals/README.md) to evaluate the current pipeline.

## Deployment

GKE runs separate MCP and worker images with persistent Vespa storage. Only MCP is publicly exposed, through HTTPS and Google OAuth with a configured email or domain allowlist.

Deployment does not ingest documentation or enable a schedule automatically.

- [GKE deployment](deployment/README.md): configuration, images, rollout and recovery.
- [Public HTTPS](deployment/https.md): DNS, certificates and readiness.
- [Worker operations](apps/worker/README.md): ingestion, scheduling and failures.

Project conventions live in [AGENTS.md](AGENTS.md).
