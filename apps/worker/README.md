# Updating the documentation indexed in Vespa

The worker refreshes the `docs` corpus through one native Mistral workflow,
`docstral-refresh`, with six sequential activities visible in Studio:

```text
crawl → extract → compare_hashes → split → embed → index_delta
```

MCP stays running throughout ingestion. Only new, changed or pending articles
are split, embedded and indexed. An unchanged hourly run makes no embedding
request and no Vespa write. Kubernetes and image rollout belong to deployment;
the worker has no Kubernetes controls or `publish` command.

## Running the workflow

The worker needs a migrated, reachable Vespa application, a writable persistent
volume and these environment variables:

- `VESPA_ENDPOINT`: internal Vespa endpoint.
- `MISTRAL_API_KEY`: injected credential, never a command argument.
- `DEPLOYMENT_NAME`: Mistral Workflows deployment identifier.
- `DOCSTRAL_DATA_DIR`: worker data directory, default `/app/data`.

```sh
docstral-worker workflows
```

The process registers the workflow and polls over an outbound connection. Start
an execution with input `{}` in [AI Studio](https://console.mistral.ai/). Paths,
endpoints and credentials come from the worker environment. The CLI enforces
strict SDK trace redaction before importing Workflows.

Startup never creates or enables a schedule. Configure hourly execution
separately with interval `PT1H`, overlap `SKIP` and `pause_on_failure=True`.
Verify a manual execution before enabling it. Operational failures pause future
runs until recovery and manual resumption; partial extraction results do not.
Monitor both execution results and schedule state in Studio.

## What each activity does

| Activity | Behavior |
| --- | --- |
| `crawl` | Fetch a fresh, complete snapshot using the existing conditional requests and raw hashes. An incomplete crawl fails without changing the index. |
| `extract` | Convert every stored page to Markdown locally. Record page-local conversion failures and preserve their previously indexed articles. |
| `compare_hashes` | Compare Markdown, title and processing fingerprints against confirmed indexed state. Identify removals from the complete crawl inventory. |
| `split` | Split changed articles with the existing Toolkit settings: 800 / 800 / 0. |
| `embed` | Embed only those chunks with `mistral-embed`, 1024 dimensions and the existing six-retry policy. |
| `index_delta` | Validate prepared documents, replace changed articles and delete disappeared articles through the Toolkit. Record each confirmed mutation. |

The indexing fingerprint covers the Markdown hash, title, splitter settings,
Docstral pipeline version, Toolkit version and embedding model. HTML navigation
changes that leave the extracted article unchanged do not trigger reindexing.
The Markdown-only `content_hash` used by citations and evaluations is preserved.
Increment `PipelineConfig.version` when extraction semantics change independently
of the other fingerprinted settings.

The Toolkit replaces one article's chunks at a time. That article can briefly
be absent or partial while being replaced; the remaining corpus stays available.
This is not an atomic switch of the entire corpus. No activity clears `docs`,
scales a process or applies a Vespa migration.

## State, failures and retries

`index-state.json` on the worker volume records each canonical URL, Toolkit
document ID, last confirmed fingerprint and pending flag. If absent, comparison
performs a one-time read-only inventory of existing Vespa sources; their unknown
fingerprints require initial reindexing. Each mutation is marked pending before
its API call, and confirmed only after it succeeds. Pending articles are retried
even when their previous fingerprint matches.

Extraction failures yield an explicit `partial` result, without a percentage
threshold, including when all conversions fail. They never imply deletion.
Removals always follow the complete, non-empty crawl inventory. Other preparation,
persistence or dependency failures stop the execution explicitly. Preparation
errors leave the served index untouched; a failed write may leave the affected
article partial and pending.

After fixing a failure, start a new `docstral-refresh` execution with `{}`. It
performs a fresh crawl and reconciles pending state. There is no repair command
or reuse of embeddings from a previous execution.

Activities exchange typed snapshot and artifact references. Documents remain in
`snapshots/<snapshot>/prepared/<stage>/documents.jsonl`, with versioned, hashed
manifests finalized atomically. Serialization uses the Toolkit's public registry
with `DocsChunkMetadata` registered explicitly. Incomplete, corrupt, incompatible
or symbolic-link artifacts fail validation. These outputs support process changes
between activities of the same execution; they are not a cross-run cache.

Each activity holds the volume lock and rejects maintenance or stale snapshot
and index-state references. Activities have one attempt each, 20-second
heartbeats, a one-minute heartbeat timeout and a 55-minute SDK timeout. They
share a cooperative 50-minute deadline including time between activities.
Cancellation waits for a synchronous crawl to finish before releasing its lock.

The final result contains article counts `indexed`, `failed`, `changed`,
`unchanged`, `deleted`, and `status` (`complete` or `partial`). `failed` counts
extraction failures. `duration_seconds` measures preparation and feeding,
excluding crawl, scheduling and lock acquisition. Logs expose stage counters,
extraction failure rate and confirmed page mutations. Successful completion
retains two complete snapshots and one failed snapshot, additionally protecting
`current`; artifacts follow their snapshot. Unknown paths and symlink targets
are preserved.

## Deployment maintenance and transition

```sh
docstral-worker maintenance on --timeout 120
docstral-worker maintenance off
```

Maintenance waits for the active activity to release its lock, then persists a
flag that blocks later activities. It does not stop MCP. Image deployment owns
runtime rollout and starts MCP after migration independently of ingestion; see
[deployment instructions](../../deployment/README.md).

Before replacing the old single-activity workflow, suspend its schedule and
finish or terminate old executions. If `.publication-pending` exists, complete
that interrupted publication with the previous release first: the new worker
refuses it explicitly. Never delete a lock or marker to bypass recovery. Deploy
the new worker, run `{}` manually, verify all six activities and a subsequent
unchanged run, then resume the hourly schedule.

Local `crawl`, `extract` and `make ingest` retain their existing behavior.
`make ingest` rebuilds local Vespa; it is not the cluster refresh entry point.

## Verification

```sh
uv run pytest apps/worker/tests deployment/tests
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run pre-commit run --all-files
```

Unit tests replace external services at their HTTP boundaries; they do not
operate a live cluster.
