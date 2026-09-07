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

The existing HTTP adapter is reused per activity, including robots checks and
bounded HTTP retries. It currently reloads robots for each page. Replacing that
adapter with a crawler library and adding conditional HTTP caching remain
separate work. Each refresh downloads current HTML; retries have no frozen copy.
A redirect to another page is returned to the queue before that page is fetched.
MCP stays available while pages are updated; page replacement is not transactional.

Only one refresh may run against a corpus, and none may start during deployment.
Worker startup never creates or activates a schedule. Before changing the activity
graph, pause scheduling and let old executions finish with the old worker.
Verify a fresh refresh and an unchanged run before resuming hourly scheduling.
See [deployment](../../deployment/README.md) for migration and rollout.

Local `docstral-worker crawl`, `docstral-worker extract` and `make ingest` remain
available separately. They use local snapshots; `make ingest` rebuilds local Vespa.

Checks: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`,
`uv run pytest`, `uv run pre-commit run --all-files`.
