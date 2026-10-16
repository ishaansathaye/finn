"""Evaluate an explicit JSON scenario, without orders, account inference or quote fetching."""
import argparse
from datetime import datetime
import json
from pathlib import Path

from app.services.hedge_strategies import Holding, UnderlyingQuote, OptionQuote, HedgeFees, evaluate_hedges


def evaluate_input(data):
    payload = dict(data)
    payload["now"] = datetime.fromisoformat(payload["now"].replace("Z", "+00:00"))
    payload["holding"] = Holding(**payload["holding"])
    quote = dict(payload["underlying_quote"])
    quote["as_of"] = datetime.fromisoformat(quote["as_of"].replace("Z", "+00:00"))
    payload["underlying_quote"] = UnderlyingQuote(**quote)
    for name in ("put", "call"):
        if payload.get(name) is not None:
            value = dict(payload[name])
            for field in ("as_of", "expiry"):
                value[field] = datetime.fromisoformat(value[field].replace("Z", "+00:00"))
            payload[name] = OptionQuote(**value)
    if "fees" in payload:
        payload["fees"] = HedgeFees(**payload["fees"])
    return evaluate_hedges(**payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Verified account-scoped positions and timestamped quotes in JSON")
    parser.add_argument("--output", help="Optional JSON artifact")
    args = parser.parse_args()
    report = evaluate_input(json.loads(Path(args.input).read_text()))
    text = json.dumps(report, indent=2, allow_nan=False)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        path.write_text(text)
        path.chmod(0o600)
    print(text)


if __name__ == "__main__":
    main()
