# Security and privacy

Finn can hold financial credentials and submit real-money prediction orders.
Run it under a dedicated OS user on a trusted machine. Keep a recovery copy of
private state separately from source control, with restricted permissions.

Never commit `.env`, `.secrets`, browser cookies, MCP OAuth credentials,
broker sessions, database files or journals, logs, portfolio exports, account
identifiers, personal finance plans, or proprietary third-party data. Git ignore
rules do not protect force-added files or secrets embedded in source. Review
the staged diff, run the public-tree check and a secret scanner before pushing.
If a secret is ever published, revoke/rotate it; deleting the file is insufficient.

Telegram commands must be restricted to the configured chat. Use a private
one-person chat, not a shared group, for real-money control. Webhooks additionally
require `TELEGRAM_WEBHOOK_SECRET`. Prefer long polling without inbound ports.
Do not let arbitrary messages change the background notification destination.

Treat news, filings, web pages, research tools, and LLM output as untrusted input.
An LLM should propose actions, not override account authorization, ownership,
quote freshness, limits or execution checks. Read-only observers must not share
write authority with an autonomous trader. Always reconcile order state after
timeouts or failures to prevent accidental duplicate orders.

There is no guarantee that secret scanning catches every private value, or that
passing unit tests proves a live strategy safe or profitable. This is an
experimental personal project, not a certified brokerage integration.

Use GitHub's private vulnerability reporting if available. Do not put credentials,
account details or sensitive traces in a public issue.
