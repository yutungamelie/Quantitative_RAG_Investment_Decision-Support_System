# Latency experiments

[Project overview](../README.md) · [繁體中文](../README.zh-TW.md) · [Recorded results](evaluation.md)

Goal: identify expensive stages and evaluate one change at a time before adopting it in the Streamlit application. The existing local smoke results support a concise-response prototype, but neither comparison has an eligible matched pair yet.

## 1. Use the application's environment

Run from the repository root, with dependencies installed and your Gemini settings configured as described in the [README](../README.md#run-locally). The PowerShell examples below use the project's virtual-environment interpreter directly.

The latency script makes real embedding, generation, and price requests. It uses a separate temporary Chroma index and does not modify the application source or existing vector database. Every command requires a new output filename to preserve earlier records.

## 2. Establish a stage profile

```powershell
.\.venv\Scripts\python.exe stock_ai_latency_lab.py profile --repo . --questions 2 --rounds 1 --output benchmark_runs/profile_01.json
```

This schedules 2 generated answers. Inspect row-level `success`, `checks_passed`, `candidate_count`, `stage_s`, and `end_to_end_s`, followed by `summary.baseline`. Generation, retrieval, and market-data time include provider/network waiting; setup, imports, and index construction outside the request are not part of its end-to-end metric.

## 3. Smoke-test each change separately

All three experiments can be run independently. Each command below schedules **8 generated answers**: 2 questions × 2 rounds × 2 variants, plus embedding and market-data calls. Three commands schedule 24 generated answers in total; service retries may add provider requests.

```powershell
.\.venv\Scripts\python.exe stock_ai_latency_lab.py compare --repo . --experiment concise --questions 2 --rounds 2 --output benchmark_runs/concise_01.json
.\.venv\Scripts\python.exe stock_ai_latency_lab.py compare --repo . --experiment market_cache --questions 2 --rounds 2 --ttl 300 --output benchmark_runs/cache_01.json
.\.venv\Scripts\python.exe stock_ai_latency_lab.py compare --repo . --experiment reuse --questions 2 --rounds 2 --output benchmark_runs/reuse_01.json
```

| Variant | What changes inside the benchmark process | What to inspect |
| --- | --- | --- |
| `concise` | Adds a concise-expression request to the LLM input after preparation, retaining the requested sections, numbers, and risk wording. | Generation time, output length, and whether facts/numbers survive shortening. |
| `market_cache` | Caches price-history results per ticker/period with a default 300-second TTL; cached data is copied before reuse. | Cache hits, cold/warm requests, freshness, failures, and market-data time. |
| `reuse` | Reuses retrievers keyed by k, their embedding/Chroma objects, and the LLM client. | First-use versus warm requests, chain setup, retrieval time, and output consistency. |

The cache is a price-data cache, not an answer cache. Reuse does not eliminate the remote query embedding or answer-generation calls. None of these modes currently enables an optimization in `app.py`.

## 4. Check whether a comparison is valid

For a pair to enter `summary.matched_comparison`, the script requires both calls to succeed, all five benchmark candidates to be present, identical observed price-data fingerprints, and identical base prompt inputs. The intentional concise instruction is excluded from the base-input comparison.

Check these fields before reading a percentage:

| Field | Interpretation |
| --- | --- |
| `eligible_pairs` | Number of pairs satisfying comparison conditions. Zero means no matched latency result. |
| `excluded_pairs` | Why pairs were rejected; retain these counts in the report. |
| `median_reduction_percent` | Populated only when eligible matched pairs exist; still needs adequate sampling and quality review. |
| `warm_matched_comparison` | Available for caching/reuse; restricts the comparison to warmed variants. |
| `baseline_checks_passed` / `variant_checks_passed` | Basic text checks among matched pairs. Failed checks are retained rather than filtered out to improve the apparent speed. |

The uploaded smoke comparisons both had **zero eligible pairs**, primarily because market data changed. Running more trials against changing data does not by itself solve that problem.

## 5. Stabilize inputs before a larger experiment

The current harness detects changing data but **does not offer a frozen-price or replay flag**. A controlled next experiment requires an additional fixture/replay implementation or carefully verified identical live observations.

- For a generation-focused experiment, prepare the numerical values and retrieved passages once, then give both prompts the same prepared inputs. Report this as generation-stage timing rather than a full live-data response benchmark.
- For full-pipeline caching/reuse tests, serve identical saved OHLCV inputs to both variants through a controlled data source, while preserving cold/warm behavior. Report that the experiment uses replayed data; local replay alone does not measure the network savings of a live provider cache.
- Fix the question set, cash, stock pool, corpus, model, temperature, and package versions. Continue checking fingerprints instead of assuming equality.
- Record timestamps, failure counts, model/token metadata when available, and the exact boundary of each timer.

Once valid pairs and basic behavior are confirmed, a command such as `--questions 10 --rounds 3` schedules 60 generated answers for one comparison. It uses the existing ten questions and is not an independent held-out set. Increase sample diversity and repeat sessions before making a stable tail-latency claim.

## 6. Review quality and adopt verified changes

The harness checks at least two expected keywords and a disclaimer. It also reports simple structured allocation checks. It does **not** automatically verify numbers in prose or calculate claim-level citation coverage.

Review generated stock identifiers, scores, amounts, share counts, and cash against the structured output. Check that each substantive explanation is supported by the supplied numerical data or reference passage. Record failures and missing evidence in both variants.

Only after these checks should an optimization be integrated into the application, followed by measurement through the actual app path. A result statement should identify the change, paired sample size, timing boundary, cold/warm conditions, failures, and verified quality criteria.

## Offline checks

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_latency_harness -v
```

These checks exercise timers, cache expiry, resource reuse, input pairing, and failure reporting using fixtures. Passing them is not evidence of live API latency or answer quality.
