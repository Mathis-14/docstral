# Refreshing documentation

```text
docstral-refresh {}
  |
  +-- discover_urls()                 sitemap seeds
  |
  +-- deduplicated URL queue          bounded concurrency
  |     sync_page(URL) x N
  |       download + discover links
  |       extract + compare page fingerprint
  |       if changed: split + embed + replace + confirm
  |       return status + canonical URL + links
  |             |
  |             +-- links / redirect target --> queue
  |
  +-- reliable inventory --> plan_deletions(present URLs)
  |                           delete_page(URL) x M
  +-- summary
```

Start the worker against a migrated Vespa instance:

```sh
uv run --env-file .env docstral-worker workflows
```

Set `MISTRAL_API_KEY`, `VESPA_ENDPOINT` and `DEPLOYMENT_NAME` in the environment.
Optional: `DOCSTRAL_REFRESH_CONCURRENCY` (default 2, maximum 8),
`DOCSTRAL_REFRESH_MAX_PAGES` (default and maximum 1000) and
`DOCSTRAL_CRAWL_DELAY` (default 0.25 seconds).

Each page is a native Mistral activity with its own result and up to three
attempts for temporary failures. Successful pages remain recorded in the same
execution. The workflow times out after 50 minutes; each activity after five.
Permanent errors are explicit. Exhausted page failures produce a partial result
and prevent deletions; extraction failures preserve the old indexed page.
A partial result is a completed workflow, so `pause_on_failure` does not pause
its schedule: inspect the returned `status`, `failed_urls` and `deletions_skipped`.

The worker keeps only the pages currently being processed in temporary memory.
Mistral records progress; Vespa's `pages` collection records confirmed page
fingerprints, separately from searchable `docs` chunks. The fingerprint covers
Markdown, title and processing settings. Before replacing chunks, the worker
clears confirmation; it restores confirmation only after every chunk is written.
A retry repairs an unconfirmed page even if its content reverts to an old value.
On the first refresh after migration, existing pages have no confirmation and
are indexed once. Subsequent unchanged pages require no embeddings or writes.

Crawlee 1.10.0 handles HTTP requests, HTML parsing and the standalone capture
queue. Docstral keeps URL identity, exclusions and response classification.
The sitemap uses the same HTTP client and a short strict XML parser; a failed
fetch or malformed sitemap never becomes an empty inventory.

Each activity reloads robots and fetches fresh HTML. Crawlee retries are disabled
inside activities; the three native attempts handle temporary download failures.
Robots permissions and `Crawl-delay` remain; `Request-rate`, custom HTTP backoff,
ETag caching and the old `Retry-After > 30s` failure rule have been removed.
A redirect to another page returns its destination before downloading it.
HTML and XHTML both return links, including links from unchanged pages.
MCP stays available while pages are updated; page replacement is not transactional.

Only one refresh may run against a corpus, and none may start during deployment.
Worker startup never creates or activates a schedule. Before changing the activity
graph, pause scheduling and let old executions finish with the old worker.
Verify a fresh refresh and an unchanged run before resuming hourly scheduling.
See [deployment](../../deployment/README.md) for migration and rollout.

## Local startup

From the repository root, set `MISTRAL_API_KEY` in `.env` and run `make local`.
The launcher uses a stable `docstral-local-…` deployment derived from this machine
and Vespa container/ports. It explicitly routes `{}` to `docstral-refresh` there
and overrides `VESPA_ENDPOINT` with localhost, even if `.env` contains production
values. The activity graph and refresh defaults are identical to production.

On an empty corpus it waits for the first refresh before starting MCP. A corpus
with confirmed pages is reused. `make refresh` requests an update and exits;
an already active local execution is resumed instead of duplicated. A resumed
execution finishes before migrations. Run only one startup/update command at a
time. Ctrl+C stops the processes started by that command, keeping Vespa data;
a later command can reconnect to an unfinished native execution. Changed workflow
graphs still require finishing old executions with compatible worker code.

For separate local instances, override `VESPA_CONTAINER`, `VESPA_QUERY_PORT` and
`VESPA_CONFIG_PORT` on the make command. `DOCSTRAL_MCP_PORT` selects the MCP port.
The key must permit native Workflows and embeddings. No schedule or local
pending marker is created. `make mcp` remains available for an already running,
indexed Vespa instance.

## Explicit offline snapshots

```sh
make crawl       # fresh HTML capture; no embeddings
make extract     # convert current capture to Markdown without network
make ingest      # rebuild local Vespa from current capture
```

These tools are separate from normal startup. Capture uses the shared Crawlee
adapter with at most three attempts per page and a memory queue. The summary
contains stored, ignored and failed counts plus actionable errors. A complete
capture writes raw HTML and a version-2 manifest containing date, canonical URL,
relative path and HTML hash, then atomically replaces `current`. Incomplete
captures leave `current` unchanged and are not archived.

Readers validate the manifest, local paths and one HTML hash per page read.
Older snapshot formats fail with an instruction to crawl again; old directories
are left untouched. Extraction remains offline, and explicit snapshot ingestion
uses the same page indexer as the native workflow. Markdown citation hashes and
indexing fingerprints are unchanged.

Checks: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`,
`uv run pytest`, `uv run pre-commit run --all-files`.
