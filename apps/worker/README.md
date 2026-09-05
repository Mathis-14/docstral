# Updating the documentation indexed in Vespa

The **corpus** is the documentation Docstral searches to answer questions.
Vespa stores it in a collection named `docs`, as text chunks with citation
metadata and embeddings.

`publish` rebuilds this collection from the current HTML snapshot already on
the worker's disk. It removes the old indexed chunks, then reuses the existing
ingestion pipeline to extract Markdown, split it into chunks, generate new
embeddings through Mistral, and index them in Vespa. Pages absent from the
snapshot therefore disappear from the index.

The MCP is stopped during the rebuild so it cannot answer from a half-written
index. This is a documentation update, not a deployment of application code.
It does not crawl, copy snapshots or embeddings from the Mac, create a cluster,
or apply a Vespa migration. Local development still uses `make ingest`.

## Prerequisites

- A migrated, reachable Vespa application with the shared `docs` schema.
- One MCP Deployment, with zero or one requested replica and no autoscaler.
- A worker running inside Kubernetes, as UID/GID 1000, with a writable
  persistent volume mounted at `/app/data`.
- Its ServiceAccount can get/patch the named `deployments/scale`, get that
  Deployment, and list its pods in the configured namespace.
- `MISTRAL_API_KEY` injected into the worker, never passed as an argument.

Set `VESPA_ENDPOINT`, `POD_NAMESPACE`, and `MCP_DEPLOYMENT` in the worker's
environment. `DOCSTRAL_DATA_DIR` defaults to `/app/data`. The CLI also accepts
`--vespa-endpoint`, `--namespace`, `--mcp-deployment`, and `--data-dir`.
There is no fallback to local Kubernetes credentials or localhost Vespa.

The data volume must contain an autonomous snapshot:

```text
/app/data/snapshots/
├── current                  # contains the successful snapshot directory name
└── <UTC timestamp>/
    ├── manifest.json
    └── raw/*.html
```

## Manual update and recovery

Inside the configured worker container:

```sh
docstral-worker publish
```

The command acquires the exclusive publication lock, verifies inventory and
raw SHA-256 hashes, checks Vespa and the MCP Deployment, then scales MCP to zero
and waits up to 120 seconds for its pods to disappear. Only then does it clear
`docs` and run the existing splitter, embedder, and indexer. Other Vespa
collections and the Vespa volume are untouched.

- Exit `0`: complete indexing; MCP requested back at one replica.
- Exit `1`, page errors: indexing completed with at least one valid page;
  MCP resumed, failed pages and totals logged explicitly.
- Exit `1`, failed dependency or no indexed page: publication failed. If the
  rebuild had started, MCP stays stopped with `.publication-pending` on the
  volume. Repair the dependency or snapshot and run `publish` again.
- Exit `2`: invalid CLI configuration.

There is no automatic data rollback or unconditional MCP restart. Do not
manually scale MCP up during a publication. The restart requests one replica;
it does not certify HTTP readiness or answer quality.

After a completed publication and restart request, retention keeps the latest
two complete snapshots and latest failed snapshot, additionally protecting
`current` and the published snapshot. Unknown directories and symlinks are
left alone; Mac snapshots are not cleaned. Failed publication skips retention.

## Deployment maintenance

These operator commands are not MCP tools:

```sh
docstral-worker maintenance on --timeout 120
docstral-worker maintenance off
```

`on` waits for an active publication to finish, then persists `.maintenance`
on the volume. Subsequent publications fail explicitly until `off`; the flag
survives replacement of the worker pod. Both commands refuse an incomplete
index: repair publication first. Never delete the lock file to bypass a run.
Maintenance only blocks new publications; it does not stop MCP.

## Mistral Workflows worker

With the cluster prerequisites above and `DEPLOYMENT_NAME` set:

```sh
docstral-worker workflows
```

This registers `docstral-refresh` and polls Mistral for executions over an
outbound connection. No public worker endpoint is needed. Start a manual
execution with input `{}` in [AI Studio](https://console.mistral.ai/).
The activity crawls fresh documentation and calls the existing publication
pipeline under one lock. An incomplete crawl never republishes an old snapshot.
Crawl failures leave the old index available. A failed rebuild leaves MCP
stopped until repair with `publish`; there is no automatic rollback.
The CLI enforces the SDK's strict trace redaction before importing Workflows.

The output contains `indexed`, `failed`, and `duration_seconds` (indexing time).
Page-local failures remain explicit partial results; dependency failures fail
the execution. The activity has one attempt: it does not automatically repeat
the full rebuild. Cancellation waits for a running synchronous crawl to finish
before releasing its lock, and never starts publication afterwards.

Starting this process does **not** create or activate a schedule. Mistral
[schedules](https://docs.mistral.ai/studio/workflows/building-workflows/scheduling)
are configured separately; hourly execution uses interval `PT1H` and overlap
policy `SKIP`, with `pause_on_failure=True`. A failed execution pauses future
runs until manual recovery and resumption; page-local partial results do not.
Validate one manual execution on the deployed worker before enabling the schedule.
Monitor executions and schedule state in AI Studio; email alerts are not configured.

## Verification

```sh
uv run pytest apps/worker/tests/test_publish.py apps/worker/tests/test_maintenance.py \
  apps/worker/tests/test_kubernetes.py apps/worker/tests/test_retention.py \
  apps/worker/tests/test_refresh.py apps/worker/tests/test_workflows.py
```

Tests run with simulated external services, not against a live cluster.
