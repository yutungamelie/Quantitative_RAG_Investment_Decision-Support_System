# Architecture and implementation scope

[Project overview](../README.md) · [繁體中文](../README.zh-TW.md) · [Evaluation](evaluation.md) · [Latency experiments](latency-experiments.md)

The application combines deterministic numerical processing with retrieval-augmented text generation. This document describes the current code, including boundaries that matter when extending it.

## Request flow

| Stage | Source | Current behavior |
| --- | --- | --- |
| User input | [app.py](../app.py) | Accept cash, a selected pool, and a question. Both sidebar submissions and follow-up questions call `generate_final_decision`. |
| Market data | [data/stock_prices.py](../data/stock_prices.py) | Resolve configured Yahoo symbols and request six months of daily OHLCV data with `auto_adjust=False`. Flatten returned columns and normalize field names. |
| Indicators and scoring | [quant/](../quant/) | Compute moving averages, RSI, return, and volatility; derive bounded rule-based scores. |
| Candidate ranking | [decision/candidate_selector.py](../decision/candidate_selector.py) | Fetch and score instruments sequentially; sort by total score and return up to 5. Failed instruments are skipped with a console message. |
| Allocation | [decision/position_sizing.py](../decision/position_sizing.py) | Compute a cash budget, capped score-proportional weights, suggested amounts, and whole-share estimates. |
| Retrieval | [rag/retriever.py](../rag/retriever.py) | Query persistent Chroma storage using Gemini embeddings and similarity search. The final-decision route requests 5 chunks. |
| Synthesis | [decision/final_decision_chain.py](../decision/final_decision_chain.py) | Combine the question, cash, scores, allocation, and retrieved passages in a LangChain prompt; invoke Gemini and return text plus structured inputs. |
| Review and export | [app.py](../app.py), [database/](../database/), [exports/](../exports/) | Display tables and references, maintain in-session messages, and download a JSON record. |

## Quantitative scoring

The current score is a hand-designed heuristic, not a trained forecasting model:

```text
total_score = round(0.4 * trend_score + 0.3 * momentum_score + 0.3 * risk_score)
```

- Trend uses the close relative to MA20/MA60 and the relationship between the moving averages.
- Momentum uses RSI14 and 20-day return thresholds.
- Risk uses a rolling 20-day standard deviation of daily returns. A higher risk subscore denotes lower volatility under the configured rule.
- Labels are `可配置` for scores ≥75, `觀望` for 60–74, and `避免` below 60.

These rules are implemented in [quant/scoring.py](../quant/scoring.py). Their thresholds have not been validated against portfolio returns. The volatility shown is not annualized, and the price series used for scoring is the unadjusted close rather than a total-return series.

## Allocation semantics

For valid candidates whose label is not `避免`:

```text
investable_cash = floor(total_cash * 0.8)
raw_weight = candidate_score / sum(valid_candidate_scores)
weight = min(raw_weight, 0.45 for ETF or 0.30 for Stock)
suggested_amount = floor(investable_cash * weight)
estimated_shares = floor(suggested_amount / close)
```

The 45%/30% caps apply to the **investable 80% budget**, not directly to total cash. Capped weights are not redistributed, so more than 20% of total cash may remain unused. The displayed weight is relative to that investable budget. Share estimates exclude fees and execution effects.

`觀望` candidates can still receive an allocation because the current filter excludes only `避免`. The rules do not model existing holdings, transaction costs, or order execution. These are implementation details to address before introducing stronger portfolio-management claims.

## Retrieval and prompt construction

The corpus contains four manually maintained Markdown documents in [rag_docs/](../rag_docs/). [rag/build_vector_db.py](../rag/build_vector_db.py) uses `RecursiveCharacterTextSplitter` with a chunk size of 500 and overlap of 100, then `models/gemini-embedding-001` and a persistent Chroma collection named `investment_rules`. The recorded fresh-index evaluation produced 8 chunks.

For the final decision, the retrieval query contains the user's question, cash budget, formatted candidate data, and a request for allocation/risk-control rules. This differs from the standalone retrieval evaluation, which queries with each annotated question alone. Its filename-level retrieval metrics therefore do not automatically measure the final-decision prompt's grounding.

The final prompt requests six Traditional Chinese sections covering the conclusion, ranking, reasons, selected instrument observations, allocation, and risk reminders. Gemini runs at temperature 0.2. Reference passages and filenames are returned separately for inspection. Their presence does not demonstrate that every sentence in the generated answer is supported.

## State, failures, and runtime

- **Session state:** conversation messages are kept in memory by Streamlit. Prior messages are displayed but are not included in the next model prompt. Each follow-up recomputes the decision from that question and the current controls.
- **LLM failure:** an exception during generation returns `success=False` and preserves the prepared quantitative data and references. Failures during earlier preparation can instead reach the UI's outer error handler.
- **Market failure:** ranking can return a partial list if individual price requests fail. An empty stock-pool selection currently falls back to all configured instruments.
- **Resource lifecycle:** retriever/embedding/Chroma objects and the LLM client are constructed again by the current request path. Reuse is available as a benchmark experiment, not an adopted application optimization.
- **Data persistence:** JSON export is available; authentication, a server database, background jobs, and a separate inference API are not implemented.
- **Index refresh:** the builder inserts documents into persistent storage without a versioned replacement/deduplication protocol. The benchmark tools use a fresh temporary index to avoid counting old collection contents.

## Development priorities

| Priority | Work | Evidence needed before describing it as complete |
| --- | --- | --- |
| 1 | Controlled latency comparison | Identical price and base-prompt inputs, eligible-pair counts, failures, cold/warm state, and P50/P95. |
| 2 | Answer quality | Reviewed score/amount/share consistency, source-supported claims, and behavior when evidence is missing. |
| 3 | Input and data handling | Explicit behavior for empty pools, partial price data, stale dates, and invalid cash/price values. |
| 4 | Application integration | Reused resources or concise prompting benchmarked through the actual app path, with old/new outputs compared. |
| 5 | Reproducible deployment | A clean dependency environment, documented model configuration, and a deployment smoke test. |
| 6 | Broader market support | Market-specific symbols, currency/calendar assumptions, corpus updates, and held-out evaluation questions. |

Automated trading, an autonomous agent, and investment-performance backtesting are not part of the current implementation.
