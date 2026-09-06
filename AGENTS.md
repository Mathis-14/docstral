# Docstral

Python monorepo for accurate, grounded Q&A over Mistral's public documentation,
exposed through an MCP server.

This file is normative and living. It records the rules that stay true across
tasks; update it when a durable convention or constraint is learned.

## Hard rules

- Preserve user work. Inspect the current diff before editing.
- Never make an unanswered product or architecture decision implicitly. Ask.
- No silent fallback. A failed dependency, fetch, parser, model, or
  configuration fails explicitly with actionable context.
- Write simple, idiomatic Python that follows established best practices.
  Readable beats clever; no speculative abstractions, no empty directories,
  no placeholder modules.
- Every answer and citation is traceable to retrieved chunks. Citations are
  built by the server from chunk metadata, never copied from model text.
- Never expose corpus mutation through the public MCP interface.
- Never commit, amend, rebase, merge, tag, push, or create branches. The user
  owns all Git mutations.
- Never add `Co-Authored-By`, `Generated with`, or any other agent attribution
  line to a commit message, PR title, or PR body.
- Never hardcode, log, or commit a secret. Configuration comes from
  environment variables.

## Toolchain

- Python 3.13, `uv` workspace, hatchling. Applications live under `apps/`,
  each with its own `pyproject.toml`, `src/` and `tests/`. Shared code is
  extracted into `packages/` only when two applications actually share it.
- Ruff for lint and format, mypy strict, pytest.
- Dependencies are pinned in `uv.lock`. Upgrading Search Toolkit or its Vespa
  plugin is a dedicated PR.
- Checks: `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy`, `uv run pytest`, `uv run pre-commit run --all-files`.
- Generated corpus, snapshots, and results never enter Git.

## Typing

- Every function is fully annotated; mypy runs in strict mode on all `src/`.
- Data crossing a boundary is a pydantic model: configuration, manifests,
  snapshots, tool inputs and outputs.
- Injected dependencies are typed with a small `Protocol`. No `Any` without a
  comment saying why.

## Testing

- pytest, with `pytest-asyncio` for the async Search Toolkit API.
- Unit tests run real Docstral logic on small fixtures. Only external services
  are replaced at the boundary: network, Vespa, Mistral API. Internal modules
  are not mocked.
- Test observable behavior, error paths, and regressions. A fixed bug ships
  with the test that would have caught it.
- Integration tests carry the `integration` marker, are excluded from the unit
  suite, and fail explicitly when their service is missing.

## Workflow

1. Read this file, the relevant code, tests, and current diff.
2. Use `python-quality-coder` for substantive Python implementation, fixes,
   refactors, and reviews. Use `solid-code implement` on the same changes, and
   `solid-code review` on the diff before a PR; use `solid-code lock` only when
   a new module boundary or shared contract is introduced. Use
   `iterate-q-light` while requirements remain open.
3. Make the smallest coherent change that satisfies the task. An abstraction
   needs a second real case, a simpler test, or a protected external boundary.
4. Run proportionate checks and read their actual output.
5. Report completed work, checks, assumptions, and unresolved decisions. Do
   not hide an incomplete check behind an unrelated successful one.

## Git and pull requests

- Conventional Commits with a scope, for commits and PR titles alike:
  `feat(crawl): discover documentation from sitemap`. One capability per PR,
  squash merged.
- PRs are prepared with `pullr` and carry Context, Implementation, and
  Checks / QA. Checks list only what actually ran.

## References

- Search Toolkit: https://docs.mistral.ai/studio/search/search-toolkit
- Search Toolkit quickstart: https://docs.mistral.ai/studio/search/search-toolkit/quickstart
- Agentic Search: https://docs.mistral.ai/studio/search/agentic-search
- Search Starter App: https://github.com/mistralai/search-starter-app
- Vibe CLI MCP servers: https://docs.mistral.ai/vibe/code/cli/mcp-servers
- Corpus sitemap: https://docs.mistral.ai/sitemap.xml

## Decision log

<!-- Durable implementation decisions only. WHY first. Append; supersede,
never rewrite history. -->

- D001 — Discover documentation from the sitemap, then follow in-scope
  internal links; keep requested, final, and canonical URLs; separate
  discovery from admission and record a reason for every URL. Why: the
  sitemap omits roughly forty public documentation pages and its counts can
  change between runs, so every run measures its inventory; a publishable
  crawl has zero unexplained rejections and zero unknown structures, and an
  unreachable `robots.txt` caused by a 5xx response disallows crawling under
  RFC 9309.
- D002 — Extract rendered HTML with one roughly 120-line standard-library
  extractor for version 1, accepting an estimated 7% information loss from
  inactive tabs and closed FAQs; keep React payload extraction as the next
  candidate. Why: HTML covered all 13 sampled pages and every rendered table
  at materially lower implementation cost; revisit payload extraction when
  evaluation failures are attributable to hidden tabs or a demo requires it.
- D003 — Canonical URL = final URL after redirects: HTTPS, docs.mistral.ai;
  no /en prefix, trailing slash, query, fragment, or userinfo. V1 excludes
  /fr, assets, /api, /api/endpoint, /resources/cookbooks, /resources/deprecated,
  /vibe/chat-legacy. Why: one page identity; API and legacy stay deferred.
- D004 — Parse robots.txt with protego under RFC 9309: 4xx allows;
  429, 5xx, or network failure aborts; wildcards and longest match apply.
  Cadence is the maximum of configured delay, Crawl-delay, and Request-rate.
  Why: the standard library parser ignores wildcards and precedence.
- D005 — Lowercase paths. Why: the site ignored path casing on 3 September 2026;
  variants are one page and both sitemap advisories become lowercase.
- D006 — Count outbound and malformed links; record decisions only for
  docs.mistral.ai URLs. Why: 295 outbound hrefs were 23% of the manifest and are
  not crawl inventory; a malformed href is a defect of the link, not of the page
  that carries it; an outbound redirect of a documentation URL stays recorded as
  `outside_host`.
- D007 — Write autonomous snapshots with their raw bytes and manifest;
  complete runs update `current`, incomplete runs end in `-failed` without
  changing `current`, and preflight failures create no directory. Why:
  consumers see only complete, independently verifiable snapshots while
  failed runs remain auditable.
- D008 — Convert HTML with the toolkit's `MarkdownifyConverter` behind a
  Docstral pre-clean (select `main article.prose`, drop `.hidden`, copy
  the `python` and `curl` `data-language` values onto `<code>`); supersedes the
  standard-library extractor of D002, rendered HTML as the source stands. Why:
  measured on the 13 spike pages on 3 September 2026, the toolkit converter
  loses no table, labels every fence the site labels, and keeps identifiers
  intact inside inline code (markdownify still escapes underscores in plain
  prose); a custom converter would be ~180 lines for the same output.
- D009 — Organize deployable processes by runtime responsibility under
  `apps/`: `worker` owns corpus mutations, `backend` will own read-only
  retrieval and Q&A, and `mcp` will remain a thin protocol adapter. Create an
  application only when it has runnable code; keep the toolkit's `VespaApp`
  inside its sole consumer, then extract stable search code to `packages/`
  only when a second application consumes it. Why: runtime names remain clear,
  ingestion does not become a catch-all, and shared code follows observed use;
  local execution remains the baseline until deployment work is undertaken.
- D010 — Index the current raw snapshot as one Vespa document per chunk: pass
  each full Markdown page to `MarkdownTokenTextSplitter` with `chunk_size=800`,
  `chunk_max_size=800`, and no overlap; attach page title and content hash, use
  the canonical URL as `source_id`, embed chunk content alone with the
  1024-dimensional `mistral-embed`, and rank densely by closeness then cosine,
  with lexical weights at zero. Continue after page-local ingestion errors, but
  stop on splitter, embedding, or Vespa failure. The local `make ingest` entry
  point rebuilds Vespa before each complete snapshot ingestion. Citations are
  page-level for this baseline. Why: the current snapshot is the complete corpus,
  while per-document upserts cannot remove a page absent from a later snapshot.
  Measured on the current 331 convertible pages, the toolkit baseline produced
  785 chunks with a 625-token median and only 13 chunks under 64 tokens; 15
  exceeded its configured maximum, with an observed maximum of 850.
- D011 — Keep the Vespa application definition, migrations, collection name,
  and index constructor in the `packages/vespa` workspace package; isolate its
  behavior-neutral extraction immediately before the backend retrieval PR.
  Why: worker ingestion and backend retrieval need the same index contract,
  while neither application should depend on the other or duplicate it.
- D012 — Start backend retrieval with one Search Toolkit `VectorRetriever`
  over the shared `docstral_vespa` index; require callers to choose `top_k`,
  preserve ranked chunks and duplicate sources, and expose a typed Docstral
  result with the fields needed for later citations. Describe the Vespa path
  as hybrid candidate selection with dense ranking: the retriever sends query
  text and embedding, while D010 keeps all lexical ranking weights at zero.
  Keep the interactive embedder on the toolkit's three-retry default rather
  than the worker's six-retry batch policy. Do not add `QueryEngine`, keyword
  storage, preprocessing, reranking, diversification, or semantic caching
  until retrieval evaluation demonstrates the specific failure it addresses.
  Why: this is the smallest read path that exercises the real index, keeps
  failures attributable, and gives the future MCP a stable citation boundary.
- D013 — Build grounded English answers with `mistral-small-2603`. Docstral
  labels retrieved chunks `E1`, `E2`, and so on, then asks the model for a
  structured answer containing the supporting labels. Docstral rejects unknown
  labels and maps valid ones back to trusted chunk metadata to build
  deduplicated page-level citations; the model never supplies citation URLs.
  Return one fixed citation-free abstention when evidence is insufficient,
  while operational and invalid-output failures remain explicit errors. Why:
  citations must remain traceable to retrieved chunks and must not depend on
  model-generated URLs.
- D014 — Expose one read-only `ask_docs` tool through standalone FastMCP,
  with typed schemas and stateless Streamable HTTP at `/mcp` (JSON responses).
  Append server-built citation links to both text and `structured.answer`,
  preserving `structured.citations`: Vibe may ignore text when structured
  output exists. This baseline runs locally; authentication and public
  deployment are deferred.
- D015 — The first retrieval evaluation retained the existing dense ranking
  at K=5. Why: both tested hybrid configurations reduced evidence recall,
  all-required coverage, and MRR at K=5; the weaker lexical configuration
  improved coverage at K=10 but still had lower MRR. The September 2026 English
  development set measured macro/micro evidence-group recall, all-required,
  RR/MRR, source hit, and duplicate-source rate at K=1/3/5/10. Matching used
  source identity, content hash, and exact annotated passages within the actual
  top-K chunks, with duplicate sources and original ranks preserved. Passage
  alternatives satisfied a group with OR semantics; all distinct groups were
  required for complete coverage. This distinguished useful evidence from a
  correct page containing the wrong passage. Negative questions remained
  qualitative diagnostics, with no validated rejection threshold. These are
  development results, not an unseen holdout or a general rejection of hybrid
  search. Reranking quality, chunk enrichment, and a consolidated evaluation of
  the Q&A path remain unmeasured. Method, run history, and limitations are in
  [evals/README.md](evals/README.md).
- D016 — Build separate MCP and worker runtime images from the workspace root,
  installing only each application's locked dependencies as non-editable
  packages. Pin Python and uv images by digest and run as UID/GID 1000. Why:
  MCP consumes the backend as a library, both runtimes share the Vespa contract,
  and neither runtime needs the repository, build tooling, or a Docker daemon.
  Worker data stays outside its image at `/app/data`; local Docker execution
  does not add authentication or change the ingestion and answering behavior.
  Dockerfiles live in `deployment/docker/`; GitHub CI/CD workflows belong in
  `.github/workflows/`.
- D017 — Publish the cluster corpus through the worker's `publish` command:
  hold one volume-backed lock, verify the complete snapshot and raw hashes,
  stop the single-replica MCP Deployment and wait for its pods to terminate,
  then clear only `docs` through the toolkit's public client and reuse the
  existing ingestion pipeline. Resume MCP only after a completed run with at
  least one indexed page; page-local failures remain explicit partial results.
  Why: absent pages must disappear without serving a partially rebuilt index
  or destroying Vespa's container or disk. A persistent pending marker blocks
  deployment maintenance after an interrupted rebuild; retrying publication
  repairs it. Maintenance shares the lock and survives worker replacement.
  After publication, retain two complete snapshots and one failed snapshot,
  also protecting `current` and the published snapshot; never follow symlinks
  during cleanup. This is a cluster-only path using in-cluster credentials:
  local `make ingest` and Mac snapshot retention remain unchanged.
- D018 — Protect the MCP with FastMCP's native Google OAuth provider when
  explicitly launched with `--auth google`; permit tool access only for verified
  Google emails in `DOCSTRAL_ALLOWED_EMAILS`. Why: the autonomous Q&A assessment
  needs authenticated remote access without changing its answering contract,
  while invitations contain initial exposure of owner-funded API calls. Keep
  local `--auth none` available; invalid Google configuration must fail rather
  than downgrade. Use native encrypted file storage under `FASTMCP_HOME` with
  a stable signing key and one replica; suppress HTTP access logs in Google
  mode to avoid recording callback codes. Authentication stays in `apps/mcp`.
  Public access, rate limits and spending quotas require a later decision and
  PR; invitations are not a cost cap.
- D019 — Run ingestion through a native Mistral Workflows polling worker with
  one `docstral-refresh` workflow whose activity crawls then publishes under the
  existing volume lock. Why: Mistral hosts scheduling and execution history while corpus
  operations stay inside the cluster, without a public mutation endpoint or a
  second scheduler. Keep configuration in the worker environment and return
  only indexing totals; failed crawls cannot publish the previous snapshot.
  Use one activity attempt, preserving dependency-level retries and D017 partial
  results, and enforce the SDK's strict trace redaction at CLI startup. Worker
  startup never creates or activates a schedule; scheduling is an explicit
  operator operation after deployment verification, hourly with overlap `SKIP`
  and `pause_on_failure=True` until manual recovery and resumption.
- D020 — Deploy one Vespa StatefulSet and single worker/MCP Deployments with
  native Kustomize, persistent disks and internal Services. Why: the first
  GKE target reproduces the local runtime without Helm, Terraform or a second
  backend process. A manual deployment selects one stable release, verifies
  both image revisions, pins their digests and holds D017 maintenance through
  migration and rollout. Initial MCP replicas stay at zero until publication;
  deployment never ingests or enables a schedule. First remote access uses an
  authenticated port-forward; public HTTPS and high availability are deferred.
- D021 — Read `DOCSTRAL_ANSWER_MODEL` at MCP startup, defaulting to the dated
  `ministral-8b-2512`; supersedes only D013's fixed model. Why: operators need
  to change generation models without rebuilding images when model access is
  restricted. Keep the override in the operator-owned `runtime` ConfigMap and
  restart MCP to apply it. Reject blank values; never switch models on failure.
  Embeddings, retrieval and citation validation are unchanged; answer quality
  with the new model still needs evaluation.
- D022 — Expose only MCP through a GKE global external HTTPS Gateway, with
  Google-managed TLS, an explicit HTTP health route and the existing OAuth
  invitations. Why: remote clients need a stable public origin without gaining
  cluster access or exposing corpus mutation. Gateway manifests follow the
  guarded release deployment; DNS, IP, certificate and TLS policy are operator
  prerequisites. Render public resource references from environment variables,
  reject an OAuth-origin mismatch before maintenance, and disable load balancer
  access logs to avoid recording authorization codes. Pod readiness does not
  prove public TLS or Q&A readiness; certificate issuance does not hold maintenance.
- D023 — Keep the incremental ingestion engine article-based: extract locally,
  compare fingerprints of Markdown, title and processing settings, then use the
  existing Toolkit components only for changed articles. Why: routine refreshes
  should skip unchanged embeddings and writes and should not control serving
  runtimes. Record fingerprints only after confirmed indexing, mark mutations
  pending beforehand, and derive removals from a complete crawl inventory.
  Extraction failures remain explicit partial results without a percentage
  threshold. Stage artifacts use public Toolkit serialization and typed,
  versioned references on the snapshot volume; a new run recrawls rather than
  reusing old artifacts. The existing Markdown-only citation hash is unchanged.
- D024 — Keep hourly ingestion independent of application deployment: replace
  D017/D019 with six native Mistral activities, `crawl`, `extract`,
  `compare_hashes`, `split`, `embed`, and `index_delta`. Why: documentation
  changes must leave MCP running and require embeddings only for changed
  articles. Remove the worker's publication command and Kubernetes controls;
  use the Toolkit's per-article replacement, accepting that the article being
  updated may briefly be absent or partial. Each activity holds the volume
  lock, checks maintenance and rejects stale snapshot/state references. Keep
  one attempt per activity, dependency retries, 20-second heartbeats and a
  shared 50-minute deadline. Input remains `{}`; startup never enables a
  schedule. Image deployment still owns runtime rollout and maintenance, and
  starts MCP after migration independently of ingestion, superseding D020's
  publication-dependent bootstrap. Refuse an interrupted legacy publication
  until it is completed with the previous release. Retention protects current
  plus the two latest complete snapshots and latest failed snapshot.
