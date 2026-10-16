# Finn

Finn is a self-hosted, Telegram-first investing research and prediction-market
assistant. It combines portfolio-aware stock research, proactive alerts, market
and filing data, optional LLM analysis, and a Robinhood event-contract adapter.

**Experimental software, not financial advice. No strategy here guarantees
profits. Real-money execution can lose money, including through software bugs.**

## What it does

- **Stocks and ETFs:** discover candidates across industries, collect prices,
  news and SEC filings, evaluate technical/fundamental evidence, and produce
  portfolio-aware BUY/SELL/WATCH recommendations. Stock execution in Finn remains
  manual; an available recommendation is not an executed trade.
- **Strategy research:** risk-constrained allocation, hedge research, and
  walk-forward evaluation with explicit data/cost assumptions. Backtests are not
  evidence of future profitability and research results do not automatically
  authorize live execution.
- **Prediction markets:** discover event contracts, track positions and event
  context, stage selected bets, refresh quotes, and submit/cancel/manage supported
  Robinhood event-contract orders. This adapter uses **reverse-engineered private
  Robinhood endpoints**, including the event-order surface. It is not an official
  public trading API and is not affiliated with or endorsed by Robinhood.
- **Proactive Telegram:** scan for material opportunities and open-position
  changes, with filtering and cooldowns rather than a fixed recommendation quota.
- **Agentic observer:** read snapshots from a separate autonomous trader. That
  service and its credentials are not bundled here; Finn's observer does not
  execute trades in the agentic account.
- **Options readiness/research:** read-only Robinhood MCP capability and approval
  checks. This is not a claim that Finn automatically trades options.

## Components

| Area | Location |
| --- | --- |
| Telegram commands and polling | `app/command_service.py`, `scripts/run_telegram_polling.py` |
| Market data, broker and LLM adapters | `app/adapters/` |
| Research, portfolio sync and recommendation lifecycle | `app/services/` |
| Models and SQLite persistence | `app/models/`, `app/db.py` |
| Scoring, reconciliation and reports | `app/scoring/`, `app/reconciliation/`, `app/reports/` |
| Offline evaluation tools | `scripts/evaluate_stock_*.py` |
| Regression tests | `tests/` |

See [architecture](docs/architecture.md), [operations](docs/operations.md), and
[security](SECURITY.md) before deployment.

## Get started

Python 3.11+ is required; the deployed/tested environment uses Python 3.12.

```sh
git clone https://github.com/ishaansathaye/finn.git
cd finn
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
chmod 600 .env
```

Edit `.env` locally. Create your own Telegram bot, set its token and your permitted
chat ID, and supply a real SEC contact user-agent. Provider keys are optional and
missing/unavailable providers reduce coverage. Gemini is optional enrichment,
not an unlimited or free entitlement. No personal accounts, balances, credentials,
databases, finance notes or broker sessions are included in this repository.

Run tests **before** configuring broker write credentials:

```sh
.venv/bin/pytest -q
.venv/bin/ruff check app tests scripts
PYTHONPATH=. .venv/bin/python scripts/print_health.py
PYTHONPATH=. .venv/bin/python scripts/run_telegram_polling.py
```

`print_health.py` reports configuration presence, not an end-to-end service or
broker health guarantee. Use a dedicated test bot and avoid running two polling
processes against the same Telegram token. Do not expose the optional FastAPI
webhook publicly without authentication and a hardened reverse proxy.

## Robinhood connection

There is no shared account or bundled login. If you choose to enable the private
adapter, review the code, account eligibility, broker terms and risks first:

```sh
PYTHONPATH=. .venv/bin/python scripts/robinhood_write_login.py
```

Complete your own MFA/device approval. The session is stored on the machine
running Finn, normally under `~/.finn/`, and must remain private. Linux deployment
does not require iPhone mirroring, but authentication may require approval on your
own device. Select your primary investment account explicitly after connecting;
never assume the first or largest account is the correct one. Event execution
also requires the correct prediction-account configuration.

**Adding broker credentials enables real-money capabilities, including configured
position management. This repository does not provide a complete paper-trading
broker simulator.** Endpoint changes, expired sessions, account eligibility and
quote availability can prevent execution. Failed or ambiguous submissions must
be reconciled before retrying; never assume a timeout means no order was placed.

## Telegram workflow

| Command | Purpose |
| --- | --- |
| `r` | Stock/ETF report |
| `ask <ticker or company>` | Focused stock research |
| `g` | Medium/higher-stake prediction recommendations |
| `gs` | Small-stake prediction recommendations |
| `gsl` | Event slate |
| `g x 1 2` / `gs x 1 2` | Stage selected recommendations |
| `x c` | Confirm the staged execution preview |
| `y` / `n` | Approve/reject a pending proactive position-change alert |
| `agent` | Observe the separate agentic account snapshot, if configured |
| `pf`, `sy`, `bs`, `h` | Portfolio, sync, bet statistics, help |

Automatic position management and recommendation filters have additional
configuration in `app/config.py`. Read them before granting write access.
Available cash is not itself permission to risk all of it.

## Development and public/private boundary

This repository intentionally tracks source and sanitized documentation only.
Existing deployment-specific plans and finance documents are ignored. Use
`scripts/check_public_tree.py` and Gitleaks before committing or publishing;
never force-add private files. See [operations](docs/operations.md) for a
documentation-only sparse checkout.

## License

MIT, see [LICENSE](LICENSE). Third-party libraries retain their own licenses.
Broker/data-provider rights and service terms are not granted by this license.
