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
