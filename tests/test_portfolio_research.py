import json
from pathlib import Path
import runpy

import numpy as np
import pandas as pd
import pytest

from app.services.portfolio_research import compare_strategies, frame_from_csv
evaluate_input = runpy.run_path(str(Path(__file__).parents[1] / "scripts/evaluate_stock_hedges.py"))["evaluate_input"]


def prices():
    rng = np.random.default_rng(7)
    values = 100 * np.exp(np.cumsum(rng.normal(.0002, .01, (250, 4)), axis=0))
    return pd.DataFrame(values, index=pd.bdate_range("2025-01-01", periods=250, tz="UTC"),
                        columns=["SPY", "XOM", "MSFT", "LLY"])


def test_fixed_comparison_is_reproducible_and_not_promoted():
    report = compare_strategies(prices(), train_size=126, min_train_size=126)
    json.dumps(report, allow_nan=False)
    assert len(report["methods"]) == 4
    assert report["automatic_promotion"] is False
    assert report["execution_enabled"] is False
    assert len(report["input_prices"]["data"]) == len(prices())
    assert "scipy" in report["dependencies"]
    assert report["methods"]["equal_weight"]["metrics"] == report["benchmarks"]["equal_weight"]["metrics"]
    again = compare_strategies(prices(), train_size=126, min_train_size=126, include_daily=False)
    assert again["data_fingerprint"] == report["data_fingerprint"]
    for method, result in again["methods"].items():
        assert "daily" not in result
        assert result["metrics"] == report["methods"][method]["metrics"]
        assert result["metrics"]["annualized_volatility"] >= 0
        assert result["metrics"]["total_cost"] > 0


def test_csv_preserves_gaps_and_rejects_bad_data(tmp_path):
    frame = prices()
    frame.iloc[10, 1] = np.nan
    path = tmp_path / "prices.csv"
    frame.to_csv(path)
    loaded = frame_from_csv(path)
    assert loaded.isna().any().any()
    with pytest.raises(ValueError):
        compare_strategies(loaded)


def test_benchmark_not_inferred_from_first_stock():
    result = compare_strategies(prices().drop(columns="SPY"))
    assert "regime_minimum_variance" not in result["methods"]
    assert result["benchmarks"]["spy_cash"]["metrics"]["total_return"] == 0


def test_hedge_json_boundary_keeps_account_private_and_orders_disabled():
    data = {"account_id": "secret", "holding": {"account_id": "secret", "symbol": "XYZ", "shares": 100},
            "underlying_quote": {"symbol": "XYZ", "bid": 99, "ask": 101, "as_of": "2026-10-07T12:00:00Z"},
            "put": {"symbol": "XYZ", "option_type": "put", "strike": 95, "expiry": "2026-11-07T12:00:00Z",
                    "bid": 2, "ask": 3, "as_of": "2026-10-07T12:00:00Z"},
            "now": "2026-10-07T12:00:00Z", "cash_available": 1000, "premium_budget": 400}
    result = evaluate_input(data)
    assert "secret" not in json.dumps(result, allow_nan=False)
    assert result["execution_allowed"] is False
    assert result["strategies"]["protective_put"]["status"] == "evaluated"
    assert isinstance(data["holding"], dict)
