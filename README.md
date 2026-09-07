# Docstral

**Mistral documentation, one question away.**

Ask questions from Vibe or another MCP client. Docstral retrieves relevant
passages and returns an English answer with sources built from retrieved chunks.

**MCP endpoint: [https://docstral-mcp.dev/mcp](https://docstral-mcp.dev/mcp)**

```sh
vibe mcp add docstral --url https://docstral-mcp.dev/mcp --transport streamable-http
```

With [Vibe](https://github.com/mistralai/mistral-vibe) installed, connect using
Google OAuth with an authorized email address. Open or restart Vibe, then ask:

> Use Docstral to explain how to get structured JSON output from Mistral.
> Include the sources.

<!-- Demo video: insert the existing recording here. -->

## Run locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), have `make`
available, and start Docker with at least **4 GB of memory**. From the cloned
repository, prepare your configuration once:

```sh
cp .env.example .env
# Fill in MISTRAL_API_KEY in .env before starting
make local
```

Your key needs access to **Mistral Workflows**, **mistral-embed** and the answer
model (**ministral-8b-2512** by default). Local execution requires internet;
embeddings and generation use your Mistral API account.

The first launch installs locked dependencies, starts Vespa, applies its schema
and runs ingestion through a local worker. Once pages are indexed, MCP starts at
**`http://127.0.0.1:8000/mcp`**. Allow several minutes. Later launches reuse the
indexed corpus. Partial ingestion results include their failed URLs.

To use the local server in Vibe, register it once from another terminal:

```sh
vibe mcp add docstral-local --url http://127.0.0.1:8000/mcp \
  --transport streamable-http --header X-Docstral-Client=vibe
```

Open or restart Vibe and use `/mcp docstral-local` to check its tools. Other MCP
clients can use the same URL with **Streamable HTTP**. Local access requires no
OAuth. If you change `DOCSTRAL_MCP_PORT` in `.env`, update the client URL too.

### Everyday commands

| Command | Purpose |
| --- | --- |
| `make local` or `make` | Start locally; initialize the corpus if needed. |
| `make ingestion` | Update the local corpus incrementally, then exit. |
| `make check` | Run Ruff, format checks, strict mypy and unit tests. |

`Ctrl+C` stops the worker and MCP started by the launcher; Vespa data is preserved.
No schedule is created automatically. See [.env.example](.env.example) for
configuration and the [worker guide](apps/worker/README.md) for advanced commands
and recovery. Install Git hooks with `uv run pre-commit install`.

## Stack

| Component | Role |
| --- | --- |
| FastMCP | Read-only `ask_docs` tool, MCP transport and Google OAuth. |
| Mistral Search Toolkit + Vespa | Chunking, indexing and retrieval with dense ranking. |
| Mistral models | Embeddings with `mistral-embed` and answer generation. |
| Crawlee | HTTP downloads, HTML parsing and link discovery. |
| Mistral Workflows | Incremental ingestion with per-page progress and retries. |
| Python 3.13 + uv | Runtime, workspace and locked dependencies. |

`apps/mcp` owns retrieval and answering; `apps/worker` owns ingestion.
`packages/vespa/` holds the shared Vespa schema and index constructors. The
[system prompt](apps/mcp/src/docstral_mcp/qa/prompt.md) is bundled with MCP;
restart locally or rebuild the image after editing it.

Local and production ingestion use the same **`docstral-refresh`** workflow.
Unchanged pages need no embeddings or writes; removals require a reliable crawl
inventory. MCP stays available during refreshes, although a page can briefly be
absent or partial while replaced. Deployments interrupt MCP during rollout.

<!-- Mistral Workflows screenshot: insert a per-page refresh execution here. -->

## Results

On the September 2026 retrieval development set, dense retrieval at five chunks
covered every required evidence group for **54/62 questions**, with **89.52% macro
evidence recall**. The saved corpus contained 331 indexed pages and 785 chunks.
These are development results, not an unseen holdout.

Both tested hybrids underperformed at K=5. On the separate Q&A formulations,
listwise reranking improved complete coverage from **52/62 to 61/62**, without
demonstrating better overall answers or reliable abstention. Those Q&A experiments
used `mistral-small-2603`, not the current default answer model. A separate
[agentic-search prototype](https://github.com/Mathis-14/docstral/blob/feat/agentic-search/evals/AGENTIC.md)
was less reliable in a small 12-question trial. These alternatives are not enabled.

See the [results and limitations](evals/RESULTS.md) and
[evaluation guide](evals/README.md).

## Deployment

GKE runs separate MCP and worker images with persistent Vespa storage. Only MCP
is exposed through a public HTTPS Gateway with Google-managed TLS and Google
OAuth; only verified email addresses in the invitation allowlist can call the tool.
Deployment does not populate the corpus or enable a schedule automatically.

- [GKE deployment](deployment/README.md): configuration, images, rollout and recovery.
- [Public HTTPS](deployment/https.md): DNS, certificates and readiness.
- [Worker operations](apps/worker/README.md): ingestion, scheduling and failures.

Project conventions live in [AGENTS.md](AGENTS.md).
