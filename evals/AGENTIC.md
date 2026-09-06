# Local Agentic Search

- Compares dense K=5 with iterative search using the same answerer and `ministral-8b-2512`.
- Read-only `search`, `open` and phrase `grep`; excludes consulted chunks and builds citations from their metadata.
- Toolkit/Vespa `0.0.13` already supports navigation and exclusions; no upgrade or production changes.
- The local loop fixes model and budgets; Vibe would own the loop. [Official guide](https://docs.mistral.ai/studio/search/agentic-search).

## Run

- Requires the [eval environment](README.md), Docker and the saved corpus/freeze below.
- Target: `docstral-vespa`, `127.0.0.1:8080`, 785 chunks / 331 sources. Keep ingestion stopped.
- Each run needs a new output directory under ignored `data/`.

```sh
local_args=(
  --vespa-endpoint http://127.0.0.1:8080 --vespa-container docstral-vespa
  --corpus-dir ../docstral/data/extracted/20260903T120924Z/pages
  --freeze ../docstral/data/evals/datasets/qa_dev_v1.freeze.json
)

# One question
uv run --locked --no-sync --env-file .env python -m evals.run_agentic \
  "${local_args[@]}" --question 'Where are cached prompt tokens reported?' \
  --output-dir data/evals/agentic/question-01

# Twelve paired questions: simple, precise, multi-passage and unanswerable
uv run --locked --no-sync --env-file .env python -m evals.run_agentic \
  "${local_args[@]}" --output-dir data/evals/agentic/comparison-02
```

- Subset: `--question-ids candidate-007 candidate-033`.
- Per question: 6 turns, 6 tools, 3 searches, 20 chunks, 120 s; 96 KiB per request/tool result.
- Output: 256 controller / 1,024 answer tokens. Mistral/Vespa timeouts: 30/10 s.
- HTTP attempts, including retries: 10 agentic / 5 baseline / 200 per run. No silent fallback.

## Evidence and result

- `answers.jsonl`: answers/errors, chunks, latency, usage and coverage. `tools.jsonl` / `http.jsonl`: traces without HTTP headers. `run.json`: configuration and status.
- Historical 12-question trial: baseline **9 answers, 1 abstention, 2 errors**; agentic **2 answers, 10 errors**. Keep the current pipeline; this sample proves no general advantage.
- Details: `data/evals/agentic/comparison-01/report.md`. Coverage uses all consulted chunks, so context budgets differ.
- After cleanup, one real pair passed (`smoke-runner-01`); the full trial and follow-up tools were not rerun.
- Four pure tests: `uv run --locked --no-sync pytest evals/tests/test_agentic.py`. Other checks: [README](README.md).
