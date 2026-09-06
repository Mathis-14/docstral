# Evaluation results

**The exploratory alternatives are not enabled in the baseline.** Listwise
reranking gives the strongest measured retrieval improvement: complete evidence
for **61/62 rather than 52/62** questions at five chunks. It does **not** establish
better overall answers or safe abstention. This is an evaluation PR: the findings
support a separate pipeline decision, not an implemented reranker or MCP redesign.

[README.md](README.md) defines the datasets, metrics, maintained commands, and
versioned/local boundary. All measurements below use the known development set;
none establish unseen-set performance.

## 1. Retrieval baseline and hybrid controls

Finalized retrieval V1: 62 positives, 73 evidence groups, 10 separate negatives.
K counts chunks, not unique pages. No answer generation or judge in these runs.

| Dense K | Macro recall | Micro recall | All required | MRR | Source hit | Duplicate sources |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 62.10% | 60.27% | 37/62 | 0.6452 | 72.58% | 0.00% |
| 3 | 81.45% | 79.45% | 49/62 | 0.7366 | 90.32% | 23.66% |
| 5 | 89.52% | 87.67% | 54/62 | 0.7535 | 93.55% | 25.81% |
| 10 | 92.74% | 91.78% | 56/62 | 0.7581 | 96.77% | 29.84% |

The first run measured 86.29% macro recall and 52/62 complete at K=5.
Corrections to golds 038/046 explain the increase to 54/62: **not a retrieval
improvement**. The two finalized repeats had identical metrics and top-5 ordering
for 62/62 positives; top-10 ordering matched for 59/62.

| K=5 configuration | Macro recall | All required | MRR |
| --- | ---: | ---: | ---: |
| Dense control | 89.52% | 54/62 | 0.7535 |
| Strong lexical hybrid | 78.23% | 47/62 | 0.6304 |
| Weak calibrated hybrid | 86.29% | 52/62 | 0.6973 |

Shared query vectors, index, and golds. Strong weights: BM25 content/title
50/100, cosine 80. Weak: 0.0105476094/0.0635251557, cosine 1. Dense: 0/0,
cosine 1. Closeness stayed 1 and field-match weights 0. Weak weights used
non-gold calibration: each field's nonzero p95 contributed 10% of the median
dense contribution among dense top-10 candidates.

Both hybrids gained complete evidence on four questions at K=5 but lost eleven
and six respectively. Weak hybrid improved K=10 to **95.16%, 58/62**, with lower
MRR (0.7077 vs 0.7581). These two configurations did not justify changing K=5;
they do not disprove hybrid search generally. Strong also changed cosine weight.

## 2. Answer baseline and prompt clarification

Frozen Q&A V1 changes questions 005/006 from Magistral to Mistral Small, retaining
the same golds. Both lose a matching group in dense top-5; this explains the
later **52/62**, versus historical retrieval V1's 54/62. Do not mix these cohorts.

The initial full attempt stopped before judging and is excluded. The completed
runs below used Small 2603 answers and native Ragas with Medium 3.5 judging.
The prompt clarification made product/API/client identity explicit, allowed
technical URLs, specified the abstention shape, and requested complete snippets.
This is the existing baseline prompt fix, separate from exploratory pipelines.

| Q&A run | Scored positives | Faithfulness | Factual F1 | Factual recall | Negative abstentions | Generation errors, all 72 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Initial full | 60/62 | 0.8888 | 0.5588 | 0.5097 | 3/10 | 4 |
| Clarified prompt | 60/62 | 0.8897 | 0.5038 | 0.7552 | 0/10 | 4 |

More reference claims appeared, but F1 and abstention did not improve. These
are scored-only run means, not paired causal estimates. Historical Faithfulness
received **raw chunk contents**; later exploration/reranking used the exact JSON
evidence shown to generation, including titles and labels. Keep those protocols
separate. Neither judge scores nor valid output schemas certify executable code.

## 3. Local alternatives

Same frozen Q&A set, 62 positives and 10 negatives. The eight-arm campaign
produced 576 generation outcomes; Ragas covered a common 12-positive panel for
all eight arms, then all 62 positives for A/B/G/H. Its 888 metric records contain
867 scores and 21 explicit skips, with none pending. References were withheld
from generation; the full-judging shortlist was selected using development data.

| Arm / experiment | Context | Macro evidence recall | All required | MRR | Observation |
| --- | --- | ---: | ---: | ---: | --- |
| A: dense control | 5 | 86.29% | 52/62 | 0.7341 | Reference |
| B: conservative prompt | Same 5 | 86.29% | 52/62 | 0.7341 | No clear overall answer gain |
| C: source URL visible | Same 5 | 86.29% | 52/62 | 0.7341 | No clear panel gain |
| D: dense top-10 | 10 | 89.52% | 54/62 | 0.739 | More context, not a fixed-budget comparison |
| E: top-5 + immediate neighbors | At most 10 | 91.94% | 56/62 | 0.742 | Adjacent chunks from the same page; +60% characters vs A |
| G: exact cosine, content only | 5 | 86.29% | 52/62 | 0.7341 | Same ordered top-5 as Vespa on 72/72 |
| H: title + content embeddings | 5 | 79.03% | 47/62 | 0.677 | Lost evidence; no full-run answer gain |
| J: LLM rewriting | 5 | 79.51% | 47/61 | 0.655 | One positive rejected before retrieval; not a 62-case mean |

| Full Ragas arm | Scored positives | Faithfulness | Factual F1 | Factual recall |
| --- | ---: | ---: | ---: | ---: |
| A: dense | 60/62 | 0.898 | 0.479 | 0.720 |
| B: conservative prompt | 61/62 | 0.882 | 0.458 | 0.741 |
| G: exact cosine | 60/62 | 0.895 | 0.476 | 0.748 |
| H: title embeddings | 62/62 | 0.838 | 0.479 | 0.698 |

These are scored-only means; C/D/E/J have **panel judging only**. Simple title
enrichment and rewriting were not retained. Product/section enrichment was not
tested by the title-only variant. Dense top-50 already contains all 73 gold
groups, so the prepared lexical rescue (27/72 triggers, 126 extra candidates)
could not add gold coverage; its reranked quality was not measured.

Neighbors were checked on six targeted cases, then with two additional
generations per arm (36 new outcomes). Neighbor context fixed OpenAI `base_url`
on 029 in 3/3 runs; dense top-10 did not. Conversely, top-10 supplied backoff on
033 in 3/3 while neighbors did not. Three of the four aggregate neighbor gains
already had semantically equivalent, unannotated evidence in the control.
**No universal neighbor winner:** coverage grows mechanically when seeds are
retained and context is added.

The Alibaba cross-encoder `Alibaba-NLP/gte-reranker-modernbert-base` was tested
through TEI on emulated AMD64/ARM64, 2 CPU and 4 GiB. Initial preparation hit an
OOM; later inference worked but only 26/41 pairs completed in a 45-minute budget.
Two targeted rankings improved, but there is no full quality evaluation and
the runtime is not representative of native/GPU serving. F/I remained unmeasured.
Hosted API options were researched, not integrated. Pointwise Ministral was
stopped in favor of one-call listwise ranking; partial results are not a benchmark.

## 4. Full listwise reranking A/B

**72 questions, new answers in both arms, identical archived candidates.** A uses
dense top-5. B sends all 50 candidate contents in **one**
`ministral-8b-2512` call and receives five ordered IDs; no generated summary,
new embeddings, neighbors, or enrichment. Temperature 0, maximum 256 output
tokens. Both then use the same `mistral-small-2603` answerer (maximum 1024).
Ragas uses `mistral-medium-3-5`, high atomicity/coverage, with the exact generator
evidence for Faithfulness. This experiment used the public Toolkit chat API,
not its per-chunk `LLMReRanker`.

| Retrieval @5, 62 positives | A: dense | B: listwise |
| --- | ---: | ---: |
| Macro evidence recall | 86.29% | **99.19%** |
| Micro evidence recall | 84.93% (62/73) | **98.63% (72/73)** |
| All required | 52/62 | **61/62** |
| MRR | 0.7341 | **0.9301** |
| Source hit | 93.55% | 100.00% |
| Duplicate-source rate | 25.48% | 17.10% |

Nine recall gains, zero losses, 53 ties. Question 045 still misses one payment
group. Excluding the four earlier spike questions: 51/58 → 57/58 complete;
this subset is still development data. B's top-3 already covers the same gold
as top-5, but answers with only three chunks were **not** tested.

| Ragas, same 60 valid positive pairs | A | B | B − A |
| --- | ---: | ---: | ---: |
| Faithfulness | 0.8881 | 0.8711 | −0.0170 |
| FactualCorrectness F1 | 0.5220 | 0.4972 | −0.0248 |
| FactualCorrectness recall | 0.7498 | 0.7573 | +0.0075 |

All 372 positive metric records are resolved: 366 scores, six skips for A's two
invalid positive outputs. B answers all 62 positives; A answers 60. Across the
10 negatives, neither arm produces a valid explicit abstention; A has one
invalid output, B two. Strict agent review found seven unsupported negative
answers per arm (six for A if the borderline authentication detail in 006 is
accepted). **Better evidence ranking is not demonstrated overall Q&A
improvement or safer abstention.** One run per arm gives no stability interval;
there is no isolation of candidate-pool depth versus reranking.

| Observed unit | Count | Mean | P95 |
| --- | ---: | ---: | ---: |
| B listwise ranking | 72 | 2.241 s | 3.322 s |
| A answer generation | 72 | 1.351 s | 4.004 s |
| B answer generation | 72 | 1.227 s | 2.088 s |
| Ragas judging, both arms | 1,220 HTTP calls | — | — |
| Complete campaign | 1,436 HTTP calls | 15 min 20 s wall span | — |

Unit timings include shared-gate waiting/parsing and exclude retrieval and
embedding. They are not production latency guarantees. All physical calls
returned 200; there were no 429s or pending retries.

## 5. What the audits establish

- 45 offline SDK tests checked selected snippets with installed Mistral/OpenAI
  SDKs and Vibe configuration; only HTTP was replaced. They exposed `.content`
  versus `.parsed`, wrong client methods, and transcription `create` versus
  `complete`. This did not execute every generated snippet or a live MCP handshake.
- Question 061's `.parsed` example exists in raw page data but is missing from
  extracted Markdown: a concrete extraction loss, not fixed by these experiments.
- Reranking fixes concrete evidence/API cases, but 030 still mixes OpenAI code
  with contradictory prose. Faithfulness can score it 1.0; 034's corrected code
  can score F1/recall zero against a short reference. Judge results need inspection.
- Final-run audits verified 3,600 candidate contents, 360 selected chunks, 139
  valid citation mappings, and 122 exact judge contexts. The full-run prototype had
  eight targeted tests plus lint/type checks; these are recorded checks, not
  a fresh full-repository validation.
- No MMR, semantic cache, whole-corpus generation, or precise product/section
  enrichment comparison was completed. None is a validated pipeline improvement.

## 6. Run ledger: time and completion

Paths below are relative to ignored `data/evals/`; local reports and JSON traces
are the primary records. `NR` means **not recorded**, never zero. Times are
rounded; `wall` is elapsed time, `active` sums recorded passes, and `HTTP` sums
physical request durations. A window includes pauses/checks. These quantities
must not be added together. Dates in directory names are UTC run identifiers.

### Retrieval and initial preparation

| Run directory | Outcome | Time |
| --- | --- | --- |
| `retrieval-dev-v1/20260904T174009Z` | 72 questions; initial golds | 29.4 s wall |
| `retrieval-dev-v1/20260904T183545Z` | 72; finalized golds | 92.5 s wall |
| `retrieval-dev-v1/20260904T184621Z` | 72; finalized repeat | 92.3 s wall |
| `retrieval-dev-v1/premerge-20260905T125058Z` | 72; end-to-end verification | 91.9 s wall |
| `retrieval-hybrid-spike/20260904T194338Z` | Dense/strong hybrid; 72 shared embeddings | 92.2 s wall |
| `retrieval-balanced-hybrid-spike/20260904T200225Z` | Dense/weak hybrid and calibration | 94.6 s wall |
| `reranker-smoke/20260904T203606Z` | Download verified; inference not ready | NR |

### Q&A wiring and completed baselines

| Run directory | Outcome | Time |
| --- | --- | --- |
| `ragas-spike/20260905T142930Z` | 5 questions; 12 scores + 3 skips | 82.2 s recorded |
| `qa-control/20260905T164307Z` | Rejudge 4 archived answers; 12 scores | 114.4 s active |
| `qa/20260905T164613Z` | Incomplete: 67 saved answers, no judging | 201.6 s active / 271.3 s window |
| `qa-smoke/20260905T175209Z/run` | 3 cases; 1 valid answer, 2 errors | 56.1 s active / 37.7 min window |
| `qa/20260905T193000Z-full` | 72 outcomes; 180 scores + 36 skips | 25.9 min wall |
| `qa/20260905T204128Z-grounding` | 72 outcomes; 180 scores + 36 skips | 40.5 min active / 59.6 min window |

The incomplete attempt was not a scored baseline. Grounding spans four passes
and 173 HTTP 429s. The 36 skips in each full baseline are ten negatives and two
invalid positive answers, each with three metrics, not 36 missing positive
judgments.

### Exploration, neighbors, and reranking

| Run directory | Outcome | Time |
| --- | --- | --- |
| `exploration/20260906T090752Z` | 8 arms; 576 outcomes; 867 scores + 21 skips | 85.6 min campaign window |
| `reranker-spike/20260906T105501Z` | Partial: 2/3 questions, 26/41 pairs | 45 min inference wall + 8.3 min startup |
| `neighbors-top10-spike/20260906T105654Z` | 6 cases × 3 archived arms; 18 new metric records | 123.1 s HTTP; wall NR |
| `sdk-snippets-spike/20260906T105753Z` | 45 offline tests | 0.87 s pytest only; wall NR |
| `reranker-api-options/20260906T112900Z` | Provider research only, no inference | NR |
| `neighbors-repeat-spike/20260906T112740Z` | 36 new outcomes; 102 scores + 6 skips | 765.1 s HTTP; wall NR |
| `ministral-rerank-spike/20260906T125621Z` | Stopped: 3 complete rankings, fourth partial; no answers | 339.9 s HTTP; wall NR |
| `ministral-listwise-spike/20260906T131100Z` | 4 rankings + 4 answers; 8 HTTP 200 | 10.9 s execution window |
| `ministral-listwise-eval/20260906T133700Z` | 72 rankings, 144 answer outcomes; 366 scores + 6 skips | 920.4 s wall |

For retrieval, inspect `run.json` and `summary.json`. Archived Q&A runs contain
`report.md`, `usage.json`, `attempts.jsonl`, and `http.jsonl`. Exploration uses `FINDINGS.md`
and `COMPLETION.json`; subsequent spikes use `REPORT.md`, except
stopped pointwise (`STOPPED.md`). The full listwise directory also contains
`PROTOCOL.md`, `VALIDATION.md`, `QUALITY_REVIEW.md`, `NEGATIVE_REVIEW.md`, and
both arms' `answers.md`. These generated artifacts remain local; this curated
summary is versioned. No experimental pipeline is promoted by this PR.
