"""Print sanitized research only; requires explicit AGENTIC_ACCOUNT_NUMBER."""
from __future__ import annotations

import argparse
import json

from app.adapters.options_research_config import load_account_number
from app.adapters.robinhood_options_mcp import OptionsMCPClient
from app.reports.options_readiness import build_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", help="Optional symbol for bounded chain/quote research")
    parser.add_argument("--max-instruments", type=int, default=5)
    parser.add_argument("--max-age-seconds", type=int, default=300)
    args = parser.parse_args()
    try:
        account = load_account_number()
        with OptionsMCPClient(account) as client:
            report = build_report(
                client, args.symbol, max_instruments=args.max_instruments,
                max_age_seconds=args.max_age_seconds,
            )
    except Exception:
        print(json.dumps({
            "execution_allowed": False, "execution_status": "blocked",
            "status": "unavailable", "user_action_required": True,
            "message": "Check explicit AGENTIC_ACCOUNT_NUMBER, inputs and existing MCP credentials.",
        }))
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if (
        report.get("catalog_complete") is True and report.get("account_found") is True
        and all(report["checks"].get(name) in ("available", "empty") for name in (
            "discovery", "get_accounts", "get_option_positions", "get_option_level_upgrade_info",
        ))
        and (args.symbol is None or (
            report.get("research", {}).get("status") in ("available", "empty")
            and all(report["checks"].get(name) != "unavailable" for name in (
                "get_option_chains", "get_option_instruments", "get_option_quotes",
            ))
        ))
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
