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
