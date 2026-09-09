# Evaluation and recorded results

[Project overview](../README.md) · [繁體中文](../README.zh-TW.md) · [Architecture](architecture.md) · [Latency experiments](latency-experiments.md)

This report summarizes saved **local experiments from September 7–8, 2026**. The update that added this report did not run new live API experiments. Public results are stored in [observed-results.json](benchmarks/observed-results.json), which retains aggregate metrics, selected per-run measurements, and provenance without publishing full generated investment answers or machine-specific paths.

## Evidence and provenance

| Record | Role |
| --- | --- |
| `stock_ai_live_metrics.json` | Initial 10-query retrieval and answer evaluation, September 7. |
| `latency_profile.json` | Two-request stage profile, September 8. |
| `latency_smoke_concise.json` | Baseline versus concise prompting, 2 questions × 2 rounds per variant. |
| `latency_smoke_cache.json` | Baseline versus market-data caching, 2 questions × 2 rounds per variant. |

The separately uploaded `latency_profile(1).json` is byte-identical to `latency_profile.json` and is counted once. The recorded source fingerprints for the three latency logs match 23 source files and 4 corpus files from [commit 2658791](https://github.com/yutungamelie/Quantitative_RAG_Investment_Decision-Support_System/tree/2658791aef7a6737cc6bc3ed278e9eb1780c1817) after accounting for Windows CRLF line endings and existing UTF-8 BOMs. The initial live report does not include an equivalent source manifest, so its exact source version is not independently pinned by that log.

Recorded environment: Windows 11, Python 3.13.5, `gemini-3.5-flash`, and `models/gemini-embedding-001`. Recorded package versions include langchain-google-genai 4.2.5, langchain-chroma 1.1.0, ChromaDB 1.5.9, pandas 3.0.3, and yfinance 1.4.1. These are historical conditions, not claims about the latest package versions.

## Retrieval

The original case set in [tests/eval_cases.py](../tests/eval_cases.py) has **10 questions**, 18 expected-source labels, and 39 expected-keyword labels. The fresh evaluation index contains **4 rule documents and 8 chunks**, with two chunks per document.

| k | Source Hit Rate@k | Mean source Recall@k |
| --- | --- | --- |
| 1 | 100% | 60% |
| 3 | 100% | 85% |
| 5 | 100% | 95% |

For each question, a hit means at least one expected filename appears among the first k retrieved chunks. Source recall is the number of distinct expected filenames recovered divided by the number of expected filenames; the reported mean averages this value across questions. Duplicated chunks from one file do not count as additional recovered sources. The @1 and @3 values use prefixes of the same top-5 result list.

This is a small, filename-labeled evaluation over authored rules. It does not establish performance on unseen questions, passage-level relevance, or financial-answer accuracy. The full decision route uses an enriched retrieval query containing candidate scores and cash, whereas this retrieval test uses each question alone.

## Answer checks and initial latency

- **10 attempts, 10 successful generated answers, 10 strict passes.** A strict pass requires generation success, at least 2 expected keywords, and one accepted disclaimer phrase. It does not require every expected keyword.
- **20.8643 s median / 26.5876 s nearest-rank P95** across the 10 successful complete calls.
- Retrieval-only median: **2.0173 s** across 10 retrieval calls.
- Reference-list presence: **10/10** successful answers had references. **Claim-level citation coverage is unmeasured.**

The initial run was sequential, without warm-up. Full-call latency includes price fetching, scoring, allocation, retrieval, generation, and formatting, while index construction is reported separately. It does not measure first-token latency or browser rendering. These checks do not validate facts, recommendation suitability, investment returns, or numeric consistency in generated prose.

## Stage profile

The profile contains two successful baseline requests, q01 and q02. Mean total request time was **21.3220 s**.

| Stage | Mean seconds | Share of mean total |
| --- | --- | --- |
| Market-data requests | 2.4685 | 11.6% |
| Scoring and ranking | 0.0188 | 0.1% |
| Retrieval | 1.2365 | 5.8% |
| Chain setup | 0.4413 | 2.1% |
| LLM generation | 17.1568 | **80.5%** |
| Other | 0.0002 | <0.1% |

LLM generation was the largest measured stage in this two-request profile. Retrieval includes remote query embedding and object construction; the generation timer includes the provider call and its waiting time, not just model computation.

## Latency smoke experiments

Both comparisons used q01/q02 for two rounds, alternated baseline/variant order, and kept generation settings unchanged except for the selected experimental change. Percentiles below use successful calls only; failures are included explicitly in the denominators.

| Experiment / variant | Successful / attempted | Successful-call median | Nearest-rank P95 | Keyword/disclaimer passes |
| --- | --- | --- | --- | --- |
| Concise experiment: baseline | 4/4 | 22.5524 s | 24.0731 s | 4/4 |
| Concise experiment: concise | 4/4 | **15.1637 s** | 16.3364 s | 4/4 |
| Cache experiment: baseline | 2/4 | 26.0874 s | 30.7200 s | 2/4 |
| Cache experiment: market cache | 3/4 | 21.5520 s | 23.0983 s | 3/4 |

The concise variant averaged 728.25 output characters versus 2,257.25 in its baseline; these are character counts, not token counts. Its four successful answers also passed the recorded structured budget/share checks. Numerical consistency in the generated prose remains unreviewed.

**Neither experiment establishes a matched latency reduction:**

- Concise: **0 eligible pairs**; all 4 excluded for changed market-data fingerprints.
- Market cache: **0 eligible pairs**; 2 excluded for generation failure, and 2 for changed market-data fingerprints. Its attempted variant calls include 1 cold request and 3 warm requests; its aggregate latency mixes successful cold and warm calls.
- Resource reuse: **no recorded result yet**.

No improvement percentage is reported. Successful-call medians with different underlying inputs and different failure counts are not a controlled before/after comparison. With 2–4 successful samples, nearest-rank P95 equals the largest observation and is not a stable estimate of a service's tail latency.

The experiment logs have empty top-level `errors` arrays even when `success=False` appears in individual rows: generation exceptions can be handled inside the app. Evaluate row-level outcomes rather than treating `status=completed` or an empty `errors` array as proof that every request succeeded.

## Reproduction and next measurements

- [stock_ai_metrics.py](../stock_ai_metrics.py) performs the static inventory or the live retrieval/answer evaluation using a fresh temporary index.
- [stock_ai_latency_lab.py](../stock_ai_latency_lab.py) instruments the existing final-decision path and compares `market_cache`, `reuse`, or `concise` within its own process.
- [tests/test_latency_harness.py](../tests/test_latency_harness.py) exercises measurement behavior offline; it does not measure live model quality or speed.

The existing `python -m tests.run_all_tests` command runs the retrieval check only. The final-answer runner is separate, and these original console runners are not a comprehensive CI pass/fail gate. The measurement scripts provide more explicit counts and failure reporting.

Before making a stronger achievement claim, collect identical-input matched comparisons, more questions/repetitions, failures and cold/warm timing, and manual reviews of numeric fidelity and supported claims. See the [step-by-step latency experiment guide](latency-experiments.md).
