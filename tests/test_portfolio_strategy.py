from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.services.portfolio_strategy import (
    PortfolioLimits, align_histories, market_regime, optimize_incremental,
    research_weights, shrink_covariance,
)


def panel(n=252):
    rng = np.random.default_rng(7)
    market = rng.normal(.0004, .01, n)
    returns = np.column_stack([market, market + rng.normal(0, .001, n), rng.normal(.0003, .007, n)])
    return pd.DataFrame(100*np.exp(np.cumsum(returns, axis=0)),
                        index=pd.bdate_range("2025-10-01", periods=n, tz="UTC"), columns=["A", "B", "C"])


def test_covariance_is_psd_and_regularizes_collinearity():
    prices = panel()
    prices["B"] = prices["A"]
    cov = shrink_covariance(prices)
    assert np.allclose(cov, cov.T)
    assert np.linalg.eigvalsh(cov).min() > 0
    for method in ("minimum_variance", "inverse_volatility", "equal_weight"):
        weights = research_weights(prices, method)
        assert sum(weights.values()) == pytest.approx(1)
        assert min(weights.values()) >= 0
        assert max(weights.values()) <= .350001


def test_optimizer_respects_cash_original_size_and_sector_constraints():
    result = optimize_incremental(panel(), {"A": 20000}, {"B": 9000, "C": 9000},
                                  {"A": "Tech", "B": "Tech", "C": "Energy"}, 100000, 8000)
    assert result["status"] == "optimized"
    assert sum(result["allocations"].values()) <= 5000
    assert result["sector_weights_after"]["Tech"] <= .35 + 1e-6
    assert result["risk_after"]["annualized_volatility_pct"] <= result["risk_before"]["annualized_volatility_pct"] + 1.001
    assert result["allocations"]["C"] > result["allocations"]["B"]
    assert "A" not in result["allocations"]  # Existing holdings never become sell orders.


def test_overweight_holdings_are_not_sold_or_added_to():
    result = optimize_incremental(panel(), {"A": 40000}, {"A": 1000, "B": 1000, "C": 1000},
                                  {"A": "Tech", "B": "Tech", "C": "Energy"}, 100000, 5000)
    assert result["allocations"]["A"] == 0
    assert result["allocations"]["B"] == 0
    assert 0 <= result["allocations"]["C"] <= 1000


@pytest.mark.parametrize("cash", [0, 5])
def test_insufficient_cash_is_valid_no_trade(cash):
    result = optimize_incremental(panel(), {}, {"A": 100}, {"A": "Tech"}, 10000, cash)
    assert result["status"] == "cash_only"
    assert result["allocations"]["A"] == 0


def test_missing_material_holdings_blocks_optimization():
    result = optimize_incremental(panel(), {"MISSING": 50000}, {"A": 100}, {}, 100000, 1000)
    assert result["reason"] == "incomplete_holdings_risk_coverage"
    assert result["allocations"]["A"] == 0


def test_solver_failure_is_not_used(monkeypatch):
    monkeypatch.setattr("app.services.portfolio_strategy.minimize", lambda *a, **k: SimpleNamespace(success=False, x=np.ones(3)))
    result = optimize_incremental(panel(), {}, {"A": 100}, {}, 10000, 1000)
    assert result["reason"] == "optimizer_failed_constraints"
    assert result["allocations"]["A"] == 0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1])
def test_invalid_portfolio_inputs_fail_closed(value):
    result = optimize_incremental(panel(), {}, {"A": value}, {}, 10000, 1000)
    assert result["status"] == "blocked"


def test_history_alignment_excludes_future_ambiguous_and_stale():
    prices = panel(200)
    histories = {s: [{"datetime": t.isoformat(), "close": float(v)} for t, v in prices[s].items()] for s in prices}
    as_of = (prices.index[-1]+pd.Timedelta(days=1)).to_pydatetime()
    histories["A"].append({"datetime": (prices.index[-1]+pd.Timedelta(days=5)).isoformat(), "close": 1e6})
    histories["B"].append({"datetime": prices.index[-1].isoformat(), "close": 999})
    aligned, excluded = align_histories(histories, as_of)
    assert set(aligned.columns) == {"A", "C"}
    assert aligned["A"].iloc[-1] != 1e6
    assert excluded["B"] == "ambiguous_dates"
    _, excluded = align_histories(histories, (prices.index[-1]+pd.Timedelta(days=10)).to_pydatetime())
    assert excluded["C"] == "stale_history"
    with pytest.raises(ValueError):
        align_histories(histories, datetime(2026, 1, 1))


def test_market_stress_retains_cash():
    prices = pd.Series(np.r_[np.linspace(100, 130, 200), np.linspace(130, 85, 22)])
    result = market_regime(prices)
    assert result["deployment_scale"] == .25
    assert result["status"] == "stress"
    assert market_regime(None)["status"] == "unknown"


def test_constant_single_asset_is_bounded():
    prices = pd.DataFrame({"A": np.ones(200)*100}, index=pd.bdate_range("2025-01-01", periods=200))
    assert research_weights(prices) == {"A": 1.0}
    result = optimize_incremental(prices, {}, {"A": 100}, {"A": "Tech"}, 10000, 1000)
    assert 0 <= result["allocations"]["A"] <= 100
    assert result["risk_after"]["annualized_volatility_pct"] >= 0


def test_limits_are_validated():
    with pytest.raises(ValueError):
        PortfolioLimits(shrinkage=float("nan"))
    with pytest.raises(ValueError):
        PortfolioLimits(min_observations=2)
