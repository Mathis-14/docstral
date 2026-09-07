# Evaluation

[run.py](run.py) calls the same `build_documentation_answerer` as the MCP server:
production retrieval, bundled prompt, model configuration and citation validation.
It saves answers beside reviewed references for manual inspection, with no judge
or automatic quality score. It evaluates the Python Q&A path, not MCP transport
or authentication.

From the repository root, with a populated Vespa index and `MISTRAL_API_KEY` in
`.env`:

```sh
uv run --locked --all-packages --env-file .env python -m evals.run \
  --vespa-endpoint http://localhost:8080 \
  --output data/evals/current-pipeline.jsonl
```

This command makes API calls but does not ingest or reset the corpus. Match the
server's `--vespa-endpoint`, `--top-k` and `DOCSTRAL_ANSWER_MODEL` to inspect the
same configuration; defaults come directly from MCP configuration. Each run
requires a new output file. Errors stop the run and preserve completed answers.

Each JSONL row contains the question, reference, response (answer, citations and
abstention), answer model, top K, endpoint and elapsed seconds. Only the question
is sent to the answerer; references and evidence annotations never enter generation.
Results stay local under ignored `data/`. Runs are opt-in, outside CI and production.

## Datasets and history

| File | Content |
| --- | --- |
| [qa_dev_v1.jsonl](datasets/qa_dev_v1.jsonl) | Default: 62 questions with reviewed answers and 10 historical negatives. |
| [retrieval_dev_v1.jsonl](datasets/retrieval_dev_v1.jsonl) | Archived evidence annotations for 62 positive questions. |
| [retrieval_negatives_v1.jsonl](datasets/retrieval_negatives_v1.jsonl) | Archived evidence notes for 10 negative questions. |

Use `--dataset` for another JSONL file with the same Q&A case shape. These are
reviewed development datasets, not an unseen holdout; references and negative
labels may be stale against the current index. The original measurements,
protocols and extraction spike are preserved in [RESULTS.md](RESULTS.md).
