# Retrieval evaluation

Dense ranking retrieved all required evidence for **54/62 questions at K=5**.
Two hybrid configurations performed worse at K=5; the weaker hybrid improved
coverage at K=10. This report evaluates retrieval, not generated answers.

## Method

Snapshot `20260903T120924Z` contains **331 indexed documentation pages and 785
chunks**, extracted from 332 stored pages. The standard Markdown splitter used an
800-token target/maximum and no overlap. Chunk content alone was embedded with
`mistral-embed` (1024 dimensions), using Search Toolkit and Vespa plugin `0.0.13`.

The reviewed English development set contains
[62 answerable questions](datasets/retrieval_dev_v1.jsonl) and
[10 unanswerable questions](datasets/retrieval_negatives_v1.jsonl), including
15 precise/natural pairs, vague and noisy formulations, and two builder cases.
The positives require
73 evidence groups; 11 questions require more than one group. Questions were
drafted before observing retrieval, then evidence was annotated and reviewed.
Two gold annotations were corrected after the initial run; question wording
was unchanged. This is a development set, not an unseen holdout.

Each group describes one required fact with alternative supporting excerpts. A
match requires the source URL, content hash, and exact excerpt within a retrieved
chunk. Any alternative satisfies its group; complete coverage requires every
group. Evaluation uses the actual top-K chunks, preserving duplicate sources and
original ranks. Negative questions are inspected separately, without evaluation
metrics.

Dense/hybrid comparisons used the same index and finalized gold annotations,
with one query embedding shared between configurations. The baseline sends text
and embedding to Vespa but sets lexical ranking weights to zero: **hybrid
candidate selection, dense ranking**. No reranker, query rewriting, or semantic
cache was used.

## Retained metrics

| Metric                | What it measures                                                        |
| --------------------- | ----------------------------------------------------------------------- |
| Recall, macro         | Mean fraction of required evidence groups covered per question.         |
| Recall, micro         | Total covered groups / total required groups.                           |
| All required          | Questions with every evidence group covered.                            |
| MRR                   | Mean reciprocal rank of the first matching chunk; zero when absent.     |
| Source hit            | Questions with a gold page retrieved, regardless of passage coverage.   |
| Duplicate-source rate | Mean of `1 - unique sources / returned chunks`; zero for empty results. |

All rates use positive questions only, at **K=1/3/5/10**. Recall, all-required,
and MRR measure evidence coverage and rank; source hit and duplication are
diagnostics. Implementations: [retrieval_metrics.py](retrieval_metrics.py).

**Deferred / not measured:** Precision@K, MAP, and nDCG are not reported because
the gold is not an exhaustive set of relevance judgments; no graded labels were
assigned. Answer correctness, faithfulness, completeness, relevance, citation
quality, and abstention (including Ragas-style metrics) were not measured by
these retrieval runs. There was no chat model or LLM judge.

## Results

### Dense baseline

| K   | Macro recall | Micro recall | All required | MRR    | Source hit | Duplicate-source rate |
| --- | ------------ | ------------ | ------------ | ------ | ---------- | --------------------- |
| 1   | 62.10%       | 60.27%       | 37/62        | 0.6452 | 72.58%     | 0.00%                 |
| 3   | 81.45%       | 79.45%       | 49/62        | 0.7366 | 90.32%     | 23.66%                |
| 5   | 89.52%       | 87.67%       | 54/62        | 0.7535 | 93.55%     | 25.81%                |
| 10  | 92.74%       | 91.78%       | 56/62        | 0.7581 | 96.77%     | 29.84%                |

Runs `20260904T183545Z` and `20260904T184621Z` produced identical metrics and
top-5 ordering for all 62 positives; top-10 ordering matched for 59/62.
Eight questions lack complete evidence at K=5, falling to six at K=10. Noisy
questions were weakest: only 2/5 had complete evidence at K=5.

The initial run (`20260904T174009Z`) had **86.29% recall@5 and 52/62** complete
questions. Corrections to the gold for `candidate-038` and `candidate-046`
produced the finalized annotations used above; this was not a retrieval gain.

### Hybrid comparison at K=5

| Configuration          | Macro recall | All required | MRR        |
| ---------------------- | ------------ | ------------ | ---------- |
| Dense                  | **89.52%**   | **54/62**    | **0.7535** |
| Strong lexical hybrid  | 78.23%       | 47/62        | 0.6304     |
| Weak calibrated hybrid | 86.29%       | 52/62        | 0.6973     |

The strong hybrid (`20260904T194338Z`) used BM25 content/title weights of 50/100
and cosine weight 80, versus 0/0 and 1 for dense. The weak hybrid
(`20260904T200225Z`) used 0.0105476094/0.0635251557 and 1. Closeness stayed at 1
and field-match weights at 0. Weak weights were calibrated without gold labels:
each BM25 field's non-zero p95 contributed 10% of the median dense contribution
among dense top-10 candidates.

At K=5, strong and weak hybrids gained complete coverage on four questions each,
but lost eleven and six respectively. At K=10, the weak hybrid improved macro
recall to **95.16%** and completeness to **58/62**, versus **92.74% and 56/62**
for dense, while MRR remained lower (**0.7077 vs 0.7581**).

## Interpretation and limits

Dense ranking remains the K=5 reference. These results concern two configurations,
not hybrid search in general; the strong configuration also changed cosine
weight. Neither reranking nor chunk enrichment was evaluated in these runs.

Exact matching can miss unannotated equivalent evidence. Excerpts must fit in one
800/800/0 chunk, so this protocol is tied to the current splitter. Paired questions
share an intent, and subgroup sizes are too small for strong generalizations.
Positive and negative scores overlap; no abstention threshold was established.

## Reproduction

From the repository root, with the workspace installed, the matching Markdown
corpus and indexed Vespa available, and `MISTRAL_API_KEY` in `.env`:

```sh
uv run --locked --all-packages --env-file .env python -m evals.run_retrieval \
  --vespa-endpoint http://localhost:8080 \
  --corpus-dir data/extracted/20260903T120924Z/pages \
  --output-dir data/evals/retrieval-dev-v1/my-run \
  --query-delay-seconds 1
```

This embeds 72 questions sequentially, reads Vespa without rebuilding it, and
uses no chat model. The output directory must be new; infrastructure errors
abort publication. Complete runs save `run.json`, `positive_results.jsonl`,
`negative_results.jsonl`, and `summary.json` under ignored `data/evals/`.
These contain configuration, dataset hashes, ordered chunks, and metrics.

The command runs the dense baseline. The hybrid comparisons were separate local
spikes, not modes of this runner. Saved runs allow metrics to be recalculated;
their manifests record a dirty worktree and do not attest the live Vespa contents.
