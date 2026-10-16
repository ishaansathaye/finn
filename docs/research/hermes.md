# Hermes evaluation

Research date: October 15, 2026. Research only: Hermes was not installed or given
broker credentials. The evaluated upstream revision was
`3b2f971133e4e011022ccf33dc6dc1c9050563d8`.

## Decision

Do not replace the trading or risk pipeline with Hermes to save tokens. Hermes
is a general-purpose agent runtime, not a validated investment strategy. Finn
should collect evidence, calculate metrics, apply constraints and prepare routine
reports with deterministic code. Use optional language analysis for bounded
interpretation, not for arithmetic or broker authorization.

## Useful capabilities

- [Script-only scheduled jobs](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)
  can run without inference. Finn can achieve the same architectural separation
  with its existing scheduler; adding a second agent loop is unnecessary.
- [Memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
  and [skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
  preserve procedures and context. They are not verified facts or proof of
  improved investment returns. Background reviews may consume additional inference.
- [MCP filtering](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp)
  can limit tool exposure. Filtering is not OS isolation or a financial-risk gate.
- [Provider routing](https://hermes-agent.nousresearch.com/docs/user-guide/features/provider-routing)
  and local/custom endpoints offer deployment choices, not guaranteed savings.
  Turn caps and wall-clock budgets are not comprehensive monetary ceilings.

## Brokerage and security

The optional [Robinhood manifest](https://github.com/NousResearch/hermes-agent/blob/3b2f971133e4e011022ccf33dc6dc1c9050563d8/optional-mcps/robinhood/manifest.yaml)
permits real trading tools. Connectivity does not establish portfolio constraints,
idempotency, reconciliation or profitable forecasting. The upstream
[security policy](https://github.com/NousResearch/hermes-agent/blob/3b2f971133e4e011022ccf33dc6dc1c9050563d8/SECURITY.md)
treats whole-process OS containment as the relevant security boundary.

If a later pilot is justified, run it in an isolated unprivileged environment
over sanitized public/synthetic evidence, without broker credentials or write
tools. Disable automatic background reviews, delegated agents and premium
fallbacks initially. Compare it against deterministic-only and single bounded
model-call baselines on identical data, measuring accuracy, provenance, latency
and all-in cost. Do not adopt it based on plausible prose or simulated profits.

The root [project license](https://github.com/NousResearch/hermes-agent/blob/main/LICENSE)
is MIT. That license does not grant rights to third-party data, models or broker
services. No measured savings or trading-performance claim is made here.
