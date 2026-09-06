# Evaluation

This work is committed for observability but is local only. You can reproduce
the baseline on your own machine with the dependencies and saved artifacts
described below. Evaluation runs and their tests are not part of CI or production.

This directory evaluates the **existing dense-retrieval and Q&A baseline**.
[RESULTS.md](RESULTS.md) records the measurements, experimental alternatives,
latencies, and limitations. Those experiments inform a different pipeline;
they do not implement one. The maintained runners do not add reranking, neighbor
expansion, or a chunks-only MCP tool.

## Dataset and protocol

Snapshot `20260903T120924Z`: 332 stored pages, 331 convertible/indexed pages,
785 chunks. Search Toolkit and Vespa plugin `0.0.13`; Markdown splitting
800/800/0; chunk content embedded with `mistral-embed`, 1024 dimensions.
Vespa receives query text and embedding, with lexical ranking weights at zero:
hybrid candidate selection, dense ranking.

| Input | Purpose |
| --- | --- |
| [retrieval_dev_v1.jsonl](datasets/retrieval_dev_v1.jsonl) | 62 English positives, 73 required evidence groups; 11 multi-group questions. |
| [retrieval_negatives_v1.jsonl](datasets/retrieval_negatives_v1.jsonl) | 10 questions not answerable from the corpus; inspected separately. |
| [qa_dev_v1.jsonl](datasets/qa_dev_v1.jsonl) | 62 positives with reviewed reference answers, plus 10 negatives. |

Questions were drafted before retrieval, then evidence was reviewed. The set
includes 15 precise/natural pairs, vague/noisy formulations, and two builder
cases. Two retrieval golds were corrected after the first run. Q&A V1 retains
the retrieval golds but changes questions 005/006 from Magistral to Mistral Small
and clarifies negative-001's justification. Additional Q&A reference evidence
does not expand retrieval gold. The original retrieval files remain unchanged.

This is a **reviewed, frozen development set, not an unseen holdout**: repeated
experiments informed variant selection. Freezing prevents silent changes, not
development-set overfitting. References and golds never enter generation or
reranking inputs.

Evidence is checked against local Markdown. Each required group has alternative
excerpts: any alternative satisfies the group; all groups are required for
complete coverage. Matching requires source URL, content hash, and an exact
excerpt inside a retrieved chunk. Cut to K chunks **before** scoring; preserve
duplicate sources and original ranks. An unannotated equivalent passage can
therefore be useful without matching the gold.

## Metrics

| Metric | Meaning |
| --- | --- |
| Evidence recall, macro | Mean fraction of required groups covered per positive question. |
| Evidence recall, micro | Total covered groups / total required groups. |
| All required | Fraction of positives with every group covered. |
| MRR | Mean reciprocal rank of the first matching chunk; zero if absent. |
| Source hit | Fraction with a gold page retrieved, even without the exact passage. |
| Duplicate-source rate | Mean `1 - unique sources / returned chunks`; zero for empty results. |
| Ragas Faithfulness | Answer claims supported by the retrieved context supplied to the judge. |
| Ragas FactualCorrectness F1 / recall | Claim agreement with the reviewed reference; high atomicity and coverage. |

[retrieval_metrics.py](retrieval_metrics.py) measures positives at K=1/3/5/10;
the Q&A runner has five chunks, so reports K=1/3/5. Ragas means include **scored
positive answers only**. Errors, skipped/undefined scores, and pending work stay
visible; no infrastructure error becomes a zero. Positive and negative
abstentions are counted separately. An invalid structured answer is not an
abstention, and a valid answer is not necessarily correct.

F1 is reference-relative, not a percentage of correct answers: supported extra
details can lower it. The maintained runner supplies raw chunk contents to
Faithfulness; later local experiments supplied the generator's exact JSON
evidence, including titles/labels. Their scores are not directly interchangeable.

**Not established:** Precision@K, MAP, and nDCG lack exhaustive/graded relevance
labels here. Semantic citation support, answer relevance, a validated abstention
threshold, and unseen-set performance are not established. Citation membership
only proves that a cited chunk was supplied. Manual inspection and targeted SDK
checks complement, rather than replace, the automated metrics.

## Run the maintained baseline

From the repository root: installed workspace, matching local Markdown corpus,
populated Vespa at `localhost:8080`, and `MISTRAL_API_KEY` in `.env`. These commands
make **paid API calls** and write local artifacts; neither ingests nor resets
Vespa. Each independent run needs a new output directory.
The frozen corpus is not shipped in Git: exact reproduction requires its saved
artifacts, not a new crawl of potentially changed documentation.

### Retrieval only

```sh
uv run --locked --all-packages --env-file .env python -m evals.run_retrieval \
  --vespa-endpoint http://localhost:8080 \
  --corpus-dir data/extracted/20260903T120924Z/pages \
  --output-dir data/evals/retrieval-dev-v1/my-run \
  --query-delay-seconds 1
```

Sequential embeddings for 72 questions, read-only Vespa queries, no chat or
judge. Failures abort publication. Complete output: `run.json`,
`positive_results.jsonl`, `negative_results.jsonl`, `summary.json`.

### Q&A and Ragas

The approved `qa_dev_v1.freeze.json` stays local under `data/`, alongside the
corpus artifacts. Supply its path explicitly with `--freeze`; the runner checks
the dataset/corpus hashes and counts before any API call. A clone alone does
not contain this private review artifact, and the runner never regenerates it.

```sh
uv run --locked --all-packages --group eval --env-file .env \
  python -m evals.run_qa --output-dir data/evals/qa/my-run \
  --freeze data/evals/datasets/qa_dev_v1.freeze.json
```

Runs the real backend at K=5: `mistral-embed` → `mistral-small-2603` answers →
native Ragas `0.4.3` with `mistral-medium-3-5` judging. The eval dependency group
is separate from the production runtime. No judge runs on negative answers.

Open **`summary.json`** for aggregate metrics and outcome counts. `answers.jsonl`
contains verbatim answers, references, server-built citations, and actual chunks;
`scores.jsonl` contains per-question metric values and statuses. `run.json`,
`questions.jsonl`, `attempts.jsonl`, and `http.jsonl` record inputs, configuration,
progress, and HTTP exchanges without headers. Observed chunks are checked against
the corpus, not every stored Vespa vector.

The runner does not generate Markdown reports or pricing summaries. Existing
reports remain local archives; presentation scripts are not maintained commands.

After service-level retries, HTTP 429 leaves its question/metric pending and the
pass continues. Add `--resume` to the **same command** when capacity is available.
Completed outcomes are retained; an interrupted question or metric restarts as
a whole, including intermediate calls. There is no automatic loop between passes.
Resume rejects changed code, configuration, inputs, or saved results. Invalid
structured outputs and undefined metrics are recorded, not automatically retried.
Other infrastructure/configuration failures stop explicitly. A partial pass
exits nonzero with saved outcomes and a JSON summary; completed means processed,
not correct.

## Maintained code versus local experiments

| Versioned | Local only, under ignored `data/` |
| --- | --- |
| Reviewed dataset JSONL inputs | Approved freeze, corpus, snapshots, embeddings, and model weights |
| Dataset validation, metrics, baseline runners, and tests | Spike runners, report generators, Docker experiments, and alternative pipelines |
| This guide and the curated [results](RESULTS.md) | Generated reports, answers, scores, and HTTP/usage traces |

Hybrid, enrichment, neighbor, rewriting, and reranker comparisons are archived
local experiments, **not supported modes of these commands**. Their recorded
protocols and artifacts explain the results; this PR does not promise a
maintained replay command for every spike. Nothing in `data/` is copied into the
package or imported by production.

The `eval` dependency group is only needed for local Q&A evaluations and their
checks. Default pytest and mypy commands cover application and shared-package
code, not `evals`. Run evaluation checks explicitly on your machine:

```sh
uv run --locked --all-packages --group eval pytest evals/tests
uv run --locked --all-packages --group eval mypy evals
```

Tests create their own temporary freezes and need no private corpus artifacts.
The unit suite replaces external services and makes no paid calls. Live quality
measurements are the explicit runs above, not unit-test assertions about model
quality.
