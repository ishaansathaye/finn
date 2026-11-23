# Recommendation resilience and optional language analysis

## Problem

A live service can be quiet for different reasons: unavailable evidence, an
exhausted model allowance, no eligible ideas, portfolio risk vetoes, notification
timing/cooldown rules, or failed delivery. These must not be collapsed into either
"the bot is broken" or "everything is healthy." Message count is not a strategy
quality metric, and no model or framework guarantees daily profits.

## Implemented boundaries

- Deterministic collection, scoring, evidence gates, portfolio sizing and risk
  checks continue without an LLM. Existing BUY thresholds and account protections
  were not lowered to generate notifications.
- Successful language analysis may be reused for at most one hour. Identity,
  ownership, model, cash and substantive packet evidence participate in the key.
  Market-value noise is excluded, but all candidate prices must remain within
  0.5% of the analyzed prices and have a recognized non-stale quote source.
  Current sizing and portfolio checks still run. Cache reuse is not permission
  to execute at an old price.
- Empty input or an unconfigured model consumes no AI allowance. Failed requests
  still count; quota is not reset to hide failures. The daily allowance remains
  a UTC-day counter, with market-hour spacing and a separate off-hours reserve.
- Each analysis pass records its actual mode, packet count, budget day/timezone,
  usage and known next eligibility. This differs from the last successful model
  response's status.
- Alert scans record outcomes and categorized filter reasons. Once per weekday
  in the 16:00-17:00 Eastern close window, a no-alert day can produce one concise
  informational status through the existing Telegram delivery path. No investment
  recommendation or trade authorization is synthesized for this status.
- Status delivery is deduplicated by day and only marked sent by the existing
  delivery owner; an unsent status can be retried within its window. It does not
  replace the latest actionable recommendation batch.

## Validation approach

Regression tests cover account/model/evidence changes, cache expiry, price drift,
unknown/stale sources, malformed cached decisions, allowance exhaustion, UTC and
DST boundaries, notification filtering and status retry/deduplication. Broker,
model and Telegram calls are mocked. CI scans Git history for secrets and runs
tests on a clean installation, without personal deployment configuration.

## Limits and follow-up

This is a bounded reliability change, not proof of better predictive accuracy or
lower measured spend. Detailed usage/cost measurements should precede changing
model providers or introducing Hermes. See [Hermes evaluation](hermes.md).
The close-window scheduler uses weekdays, not a full holiday/early-close exchange
calendar. A stopped process cannot send its own quiet-day status; external
monitoring remains a separate deployment concern. Some work in the main poller
is sequential, so slow providers can delay later checks.

## Core holding pullbacks

Configured core symbols get a bounded share of detailed research slots (up to
one quarter). The remaining slots continue to use ranking and sector coverage.
A fresh current-session price at least 3% below the prior close or 7% below the
highest close in the previous 20 sessions prompts a thesis review. Price is
not evidence that a rebound will follow. Normal evidence, sizing, recent-buy
and portfolio-risk checks still apply. A regular-session informational watch
has no trade size and is deduplicated per symbol and trading date. Actionable
buys can still alert after a watch.

`STOCK_GEMINI_MODEL` overrides the analyst for stocks only. The daily request
cap and market-window interval are configurable. Off-hours calls are limited
to at most two per UTC budget day. The deterministic research path remains
available during quota exhaustion or provider outages.
If the stock-specific model is unavailable, the next eligible batch uses the
existing analyst for up to two hours before retrying the primary. This does
not create a second request in the same scan or bypass a quota response.

## Long-horizon sales and independent research

The hold policy applies before sizing in both the stock report and single-name
research. Confirmed and inferred core positions default to hold when a proposed
sale is based solely on price, portfolio weight or slower positive growth.
A recent issuer insolvency filing can justify an exit; a high-confidence,
dated sales and earnings contraction can justify a trim. A protected sell is
alerted once per material filing and symbol. These are conservative heuristics,
not a backtested claim of excess return. A held ETF is not evaluated as an
operating company.

An optional atomic market-only JSON export (`STOCK_RESEARCH_FEED_PATH`) lets
an independent paper evaluator compare sectors. No holdings, account details,
balances, trade sizes or broker instructions are exported. Stale quotes are
excluded. Paper results are hypothetical, not broker fills.
