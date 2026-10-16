"""Offline strategy comparisons. Fetches public prices only; never contacts a broker."""
import argparse
from datetime import UTC, datetime
import json
from pathlib import Path

import pandas as pd
import yfinance as yf

from app.services.portfolio_research import compare_strategies, frame_from_csv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", help="CSV with date index and positive aligned adjusted-close symbol columns")
    parser.add_argument("--symbols", default="SPY,MSFT,XOM,LLY,CAT,JPM,PLD,DUK")
    parser.add_argument("--period", choices=["2y", "5y", "10y"], default="5y")
    parser.add_argument("--train", type=int, default=252)
    parser.add_argument("--test", type=int, default=21)
    parser.add_argument("--cost-bps", type=float, default=5)
    parser.add_argument("--slippage-bps", type=float, default=5)
    parser.add_argument("--output", required=True, help="Destination JSON artifact")
    args = parser.parse_args()
    if args.csv:
        prices = frame_from_csv(args.csv)
        source = "operator_supplied_csv; adjustment convention must be verified"
    else:
        symbols = list(dict.fromkeys(s.strip().upper() for s in args.symbols.split(",") if s.strip()))
        if not 2 <= len(symbols) <= 30 or any(not s.replace("-", "").isalnum() for s in symbols):
            parser.error("supply 2-30 ticker symbols")
        raw = yf.download(symbols, period=args.period, auto_adjust=True, progress=False, threads=False, timeout=15)
        if raw is None or raw.empty:
            raise RuntimeError("price provider returned no history")
        prices = raw["Close"].copy()
        if set(prices.columns) != set(symbols):
            raise RuntimeError("provider did not return all requested symbols")
        prices.index = pd.to_datetime(prices.index, utc=True).normalize()
        prices = prices.loc[prices.index < pd.Timestamp.now(tz="UTC").normalize(), symbols]
        # Intentionally reject gaps in the evaluator instead of silently filling/drop-selecting.
        source = "Yahoo/yfinance auto_adjust=True; retrospectively adjusted and survivor-biased"
    report = compare_strategies(prices, train_size=args.train, min_train_size=args.train,
                                test_size=args.test, transaction_cost_bps=args.cost_bps, slippage_bps=args.slippage_bps)
    report["data_source"] = source
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    path.write_text(json.dumps(report, indent=2, allow_nan=False))
    path.chmod(0o600)
    print(json.dumps({"artifact": str(path), "as_of": datetime.now(UTC).isoformat(),
                      "observations": report["observations"], "windows": len(report["windows"]),
                      "methods": {key: value["metrics"] for key, value in report["methods"].items()},
                      "benchmarks": report["benchmarks"], "automatic_promotion": False}, indent=2))


if __name__ == "__main__":
    main()
