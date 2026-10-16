#!/usr/bin/env bash
set -euo pipefail

python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

echo "Installed Finn dependencies."
echo "Next steps:"
echo "  1. PYTHONPATH=. python scripts/print_health.py"
echo "  2. uvicorn app.main:app --reload"
echo "  3. PYTHONPATH=. python scripts/run_telegram_polling.py"
