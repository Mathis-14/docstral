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

- `apps/worker`: crawling, extraction and indexing.
- `apps/backend`: retrieval and grounded Q&A, imported by MCP.
- `apps/mcp`: FastMCP server.
- `packages/vespa`: shared schema and index constructor.

## Local corpus

Start Docker. `make ingest` replaces the existing local index.

```sh
uv run docstral-worker crawl
uv run docstral-worker extract
make ingest
```

## MCP

Vespa must be running with an indexed corpus.
`DOCSTRAL_ANSWER_MODEL` selects the answer model (default: `ministral-8b-2512`);
restart MCP after changing it. Embeddings and the corpus are unchanged.

```sh
make mcp
```

In another terminal, connect Vibe to the local Streamable HTTP endpoint:

```sh
vibe mcp add docstral \
  --url http://127.0.0.1:8000/mcp \
  --transport streamable-http \
  --header X-Docstral-Client=vibe
```

The non-secret header selects Vibe's static mode. This command does not enable OAuth.

### Google OAuth (invited users)

Create a **Web application** OAuth client in
[Google Auth Platform](https://console.cloud.google.com/auth/clients), with
`http://localhost:8000/auth/callback` as the authorized redirect URI.

Fill the OAuth settings from [.env.example](.env.example) in `.env`.
Only verified addresses in `DOCSTRAL_ALLOWED_EMAILS` can use the tool;
Google's test-user list is not the access control for these identity-only scopes.

```sh
# Server terminal (stop any existing MCP first)
uv run --env-file .env docstral-mcp --auth google
# Another terminal
vibe mcp add docstral-google --url http://localhost:8000/mcp --transport streamable-http
```

In Vibe, use `/mcp login docstral-google` if needed, then ask `ask_docs` a question
and request all sources. Test outside the repository to avoid local-file context.

Keep `FASTMCP_HOME` (`data/oauth`) and the secret signing key across restarts;
Docker storage must be writable by UID 1000. This remains local, with one MCP
instance. For remote access, see [GKE HTTPS setup](deployment/https.md).
Invitations do not cap API spending. See [FastMCP OAuth](https://gofastmcp.com/integrations/google).

## Docker images

Build from the repository root (AMD64 is the GKE deployment target):

```sh
docker build --platform linux/amd64 \
  -f deployment/docker/mcp.Dockerfile -t docstral-mcp:local .
docker build --platform linux/amd64 \
  -f deployment/docker/worker.Dockerfile -t docstral-worker:local .
docker run --rm --platform linux/amd64 docstral-worker:local --help
```

With Docker Desktop and an indexed Vespa listening on the host's port 8080:

```sh
uv run --env-file .env docker run --rm --platform linux/amd64 \
  --publish 127.0.0.1:8000:8000 --env MISTRAL_API_KEY \
  docstral-mcp:local --vespa-endpoint http://host.docker.internal:8080
```

This serves `/mcp` without authentication. Images run as UID/GID 1000 and contain
no corpus or secrets. Mount worker data at `/app/data`; run local ingestion on
the host. See [cluster publication](apps/worker/README.md) and
[deployment](deployment/README.md). Cluster provisioning is separate.

## Evaluation

The [evaluation guide](evals/README.md) covers the reviewed development datasets,
retrieval and Ragas metrics, and baseline reproduction commands.
[Results](evals/RESULTS.md) compare the measured alternatives, timings,
and limitations. Evaluations are local only, outside CI and production;
experimental pipelines are not enabled in the application.
