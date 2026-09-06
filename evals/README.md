# Local evaluations

- Retrieval and Q&A benchmarks; separate [Agentic Search prototype](AGENTIC.md).
- Frozen development set: 62 positive questions, 10 negatives, 331 pages, 785 chunks.
- Metrics: exact-passage coverage, MRR, citations, abstentions and Ragas scores. See [results](RESULTS.md).
- Local only, outside CI and production. Model calls are paid; tests make no API calls.

## Run

- Requires the saved corpus/freeze, populated local Vespa and `MISTRAL_API_KEY` in `.env`.
- Install dependencies: `uv sync --locked --all-packages --group eval`.
- Use a new output directory per run. Neither command ingests or resets Vespa.

```sh
# Retrieval only
uv run --locked --no-sync --env-file .env python -m evals.run_retrieval \
  --vespa-endpoint http://localhost:8080 \
  --corpus-dir data/extracted/20260903T120924Z/pages \
  --output-dir data/evals/retrieval/my-run --query-delay-seconds 1

# Q&A: mistral-small-2603 answers, mistral-medium-3-5 Ragas judge
uv run --locked --no-sync --env-file .env python -m evals.run_qa \
  --freeze data/evals/datasets/qa_dev_v1.freeze.json \
  --output-dir data/evals/qa/my-run
```

- Read `summary.json`; inspect answers, scores and HTTP traces under ignored `data/`.
- Q&A: use `--resume` after a 429. Partial runs exit nonzero and retain results.
- References never enter generation. This reused development set cannot establish general quality.
- Checks: `uv run --locked --no-sync` followed by `pytest evals/tests`, `mypy evals`, `ruff check evals` or `ruff format --check evals`.
