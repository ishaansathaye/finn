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

An open contract is not a new purchase: automatic exits compare the held
side's independently estimated payout to its executable sale value, not the
entry edge or the last trade. The initial independent feed covers matched
NFL, NBA and MLB full-game winners only. Two distinct, recent play updates at least 15
seconds apart must favor selling by six cents after a two-cent cost allowance.
The first two minutes after acquisition, missing or wide books, stale plays
and ambiguous matchups cannot force a sale. These margins are policy buffers,
not a measured prediction advantage or an exact broker fee schedule.
Even during the opening two minutes, exceptional deterioration confirmed by
two post-entry plays can request approval, never force an exit.

Only small positions within the configured event exposure cap can be sold
automatically. Larger exits need a Telegram approval. Without independent
evidence, a bid down at least 15 cents and 25 percent from cost on distinct
book updates can prompt an approval-only defensive exit. A flip also needs a
separate opportunity on the other side, approval, a complete closing fill
and a fresh entry check. A partial fill is not a full exit. An ambiguous
broker acknowledgement blocks duplicate exits until reconciled.

The first confirmed exit candidate is journaled per position. Bet statistics
compare that hypothetical exit with holding when a broker settlement or
unambiguous final game winner is known; unresolved outcomes stay unresolved.
Exit P/L remains in financial risk metrics, not in the hit rate of the
original forecast.

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
