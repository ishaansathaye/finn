# Architecture

## Finn service

The long-polling Telegram entrypoint routes commands through `CommandService` and
periodically invokes stock listening/alerts and event monitoring/management.
SQLModel/SQLAlchemy persist accounts, holdings, provider checkpoints, research,
recommendations, approvals and execution/reconciliation records in SQLite.

Stock research combines a rotating discovery universe with held positions,
quotes, price history, news and SEC evidence. Optional Gemini analysis enriches
deterministic scoring. Portfolio strategy constrains sizing using available
data; research and hedge evaluators are not an automatic execution permission.
Recommendations and proactive alerts are filtered for timing, materiality,
ownership, core policies, risk and repeated alerts.

The primary stock account is explicitly selected. Account context participates
in analysis caching, and SELL ownership is checked again after asynchronous
research. Ambiguous/missing/stale primary data must not select another account.
Separate account records may remain in storage for observation and history.

Prediction research uses event catalogs, tradable contracts, quotes, event
context and position history. Staging and confirmation are separate from order
submission. Live quote, payout, stake and account checks apply at execution.
Management can cut, add or change supported positions according to configuration
and approval policy. This is experimental strategy logic, not loss prevention.

## Integrations

- Robinhood private adapters: account/event data and event-contract execution.
- Robinhood MCP: read-only options research/readiness integration in Finn.
- SnapTrade: optional portfolio connectivity.
- Yahoo Finance, Google News and SEC: public-source market research, subject to
  coverage, availability, rate limits, corrections and service terms.
- Optional commercial APIs: keys alone do not guarantee plan entitlement.
- Gemini: quota-bounded synthesis, not authoritative market or account data.
- Telegram: authenticated command ingress and proactive notifications.

## Separate autonomous trader

The agentic trader is an independently deployed service and not shipped in this
repository. It may use an LLM and Robinhood MCP for its own authorized account.
Finn reads its snapshot/journal representation and must not trade that account.
Observation timestamps differ from heartbeat timestamps; a running service does
not prove its holdings were just fetched from the broker.

## Known operational limits

Some poller work is sequential. Slow network research can delay later checks.
Provider limits, AI allowance exhaustion and notification filters are different
failure/quiet states and should be diagnosed separately. Unit/integration tests
mock broker execution; no public CI job should use real broker credentials.
This codebase is actively evolving, not a fully audited production trading stack.
