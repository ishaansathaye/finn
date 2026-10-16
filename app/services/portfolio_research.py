"""Fixed-model out-of-sample comparisons; never select or enable a trading strategy."""
from datetime import UTC, datetime
import json
from importlib.metadata import version

import pandas as pd

from app.services.portfolio_strategy import VERSION, panel_fingerprint, research_weights
from app.services.strategy_evaluation import evaluate_walk_forward


METHODS = ("minimum_variance", "inverse_volatility", "equal_weight", "regime_minimum_variance")


def compare_strategies(prices, *, train_size=126, test_size=21, min_train_size=126,
                       transaction_cost_bps=5., slippage_bps=5., include_daily=True):
    results = {}
    benchmarks = None
    windows = None
    limitations = []
    config = None
    for method in METHODS:
        if method == "regime_minimum_variance" and "SPY" not in prices:
            continue
        evaluation = evaluate_walk_forward(
            prices, lambda train, current, m=method: research_weights(train, m),
            train_size=train_size, test_size=test_size, min_train_size=min_train_size,
            execution_lag=1, transaction_cost_bps=transaction_cost_bps, slippage_bps=slippage_bps,
        )
        result = {"metrics": evaluation["strategy"]["metrics"]}
        if include_daily:
            result["daily"] = json.loads(evaluation["strategy"]["daily"].reset_index().to_json(orient="records", date_format="iso"))
            result["weights"] = json.loads(evaluation["strategy"]["weights"].reset_index().to_json(orient="records", date_format="iso"))
        results[method] = result
        benchmarks = {key: {"metrics": value["metrics"]} for key, value in evaluation["benchmarks"].items()}
        windows = json.loads(evaluation["windows"].to_json(orient="records", date_format="iso"))
        config = evaluation["config"]
        limitations = evaluation["limitations"]
    output = {"version": VERSION, "as_of": datetime.now(UTC).isoformat(), "kind": "fixed_model_walk_forward",
              "data_fingerprint": panel_fingerprint(prices), "symbols": list(prices.columns),
              "start": prices.index[0].isoformat(), "end": prices.index[-1].isoformat(),
              "observations": len(prices), "methods": results, "benchmarks": benchmarks,
              "windows": windows, "config": config, "limitations": limitations,
              "automatic_promotion": False, "execution_enabled": False}
    output["dependencies"] = {name: version(name) for name in ("numpy", "pandas", "scipy")}
    if include_daily:
        # Preserve the actual input, since providers can retrospectively adjust history.
        output["input_prices"] = json.loads(prices.reset_index().to_json(orient="split", date_format="iso", double_precision=15))
    json.dumps(output, allow_nan=False)
    return output


def frame_from_csv(path):
    frame = pd.read_csv(path, index_col=0)
    frame.index = pd.to_datetime(frame.index, utc=True).normalize()
    return frame
