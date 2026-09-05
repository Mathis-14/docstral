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

The MCP endpoint remains `http://127.0.0.1:8000/mcp`, without authentication.
Both images run as UID/GID 1000. Mount worker data at `/app/data`, writable by
that user; images contain neither snapshots nor secrets. Run `make ingest` on
the host: these images do not manage Vespa.

For the cluster-only replacement command, see the
[worker publication guide](apps/worker/README.md). Cluster provisioning remains
a separate step; local `make ingest` is unchanged.

## Retrieval evaluation

The [evaluation report](evals/README.md) documents the reviewed dataset, retained
and deferred metrics, dense/hybrid results, and the local reproduction command.
It measures retrieved evidence, not generated-answer quality.
