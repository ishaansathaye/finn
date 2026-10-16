# Operations and development

## Server as source of truth

Use a full Git checkout on the server where Finn runs. Keep the virtualenv,
environment, auth sessions, database, logs and backups private and outside Git.
Private local plans may coexist in `docs/`; only explicitly allowlisted public
documents are tracked. Before committing, inspect `git status --ignored` and
`git diff --cached`. Never use `git add -f` on private files.

```sh
git config core.hooksPath .githooks
python3 scripts/check_public_tree.py
gitleaks git --redact --no-banner .
```

Install Gitleaks separately from its official release and verify its checksum.
The pre-commit hook checks staged file boundaries and secrets. Run full-history
scanning before a public push. These are defense-in-depth, not a substitute for
review of hardcoded personal details and proprietary content.

## Documentation-only workstation

On a fresh workstation directory:

```sh
git clone --filter=blob:none --sparse https://github.com/ishaansathaye/finn.git
cd finn
git sparse-checkout set docs
```

Cone-mode sparse checkout includes top-level metadata such as README and license,
but excludes application directories from the working tree. Existing private
documents must be backed up and kept untracked when initializing Git in a current
directory. Sparse checkout is not a secret boundary: public Git history remains
public. Do not copy private plans into tracked documents.

## Tests and deployment

```sh
.venv/bin/pytest -q
.venv/bin/ruff check app tests scripts
PYTHONPATH=. .venv/bin/python scripts/print_health.py
```

Tests must use temporary databases and stubbed execution, not a real trading
account. Run the poller under a dedicated systemd service with its WorkingDirectory
set to the checkout and its ExecStart set to the virtualenv Python plus
`scripts/run_telegram_polling.py`. Supply `PYTHONPATH=.` and private environment
configuration. Do not launch a second poller for an already-running bot.

For container deployments, run the site's restart/redeploy procedure from the
host, then inspect systemd status and logs inside the container. Hostnames,
service secrets and personal infrastructure configuration are intentionally not
published. Keep a known-good commit and private database backup before changes.

## Diagnosing no recommendations

1. Check process uptime and errors; active systemd state alone is insufficient.
2. Check listener checkpoints and scan counts.
3. Check the chosen account, data freshness and provider failures.
4. Check analysis mode, AI usage/timeout state and deterministic output.
5. Check timing, materiality, core policy and repeat-alert filters.
6. Check pending/sent batch records and Telegram delivery responses.

Do not weaken risk/ownership checks or manufacture BUYs to increase alert count.
An honest no-action day is valid; a broken or stalled pipeline is not.
