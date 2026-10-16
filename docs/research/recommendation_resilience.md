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
