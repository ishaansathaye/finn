from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from app.services.strategy_evaluation import evaluate_walk_forward


def prices(n=24):
    t = np.arange(n)
    return pd.DataFrame(
        {"A": 100 * 1.01**t, "B": 100 * 0.999**t, "SPY": 100 * 1.002**t},
        index=pd.bdate_range("2020-01-01", periods=n),
    )


def evaluate(frame, allocator=lambda train, weights: {"A": 1.0}, **kwargs):
    options = dict(
        train_size=6,
        min_train_size=3,
        test_size=4,
        execution_lag=1,
        transaction_cost_bps=0,
        slippage_bps=0,
        max_stale_sessions=None,
        as_of="2025-01-01",
    )
    options.update(kwargs)
    return evaluate_walk_forward(frame, allocator, **options)


def test_defaults_grow_training_to_126_and_use_nonoverlapping_21_session_windows():
    frame = prices(180)
    seen = []
    result = evaluate_walk_forward(
        frame,
        lambda train, weights: seen.append(len(train)) or {},
        as_of="2025-01-01",
    )
    assert seen == [63, 84, 105, 126, 126, 126]
    assert result["strategy"]["metrics"]["sample_days"] == 117
    assert result["windows"]["sample_days"].tolist() == [21, 21, 21, 21, 21, 12]
    assert result["strategy"]["daily"].index.equals(frame.index[63:])


@pytest.mark.parametrize("lag", [1, 2, 7])
def test_future_mutation_and_appended_data_do_not_change_prefix(lag):
    frame = prices(35)

    def allocator(train, weights):
        symbol = train.iloc[-1].div(train.iloc[0]).idxmax()
        return {symbol: 0.7}

    original = evaluate(frame, allocator, execution_lag=lag)
    changed = frame.copy()
    changed.iloc[20:, 0] *= 10
    changed.iloc[20:, 1] *= 0.01
    mutated = evaluate(changed, allocator, execution_lag=lag)
    shorter = evaluate(frame.iloc[:20], allocator, execution_lag=lag)
    for name in ("strategy", "equal_weight", "spy_cash", "cash"):

        def portfolio(result):
            return result[name] if name == "strategy" else result["benchmarks"][name]

        for key in ("daily", "weights"):
            prefix = portfolio(original)[key].loc[: frame.index[19]]
            assert_frame_equal(prefix, portfolio(mutated)[key].loc[: frame.index[19]])
            assert_frame_equal(prefix, portfolio(shorter)[key])


def test_training_cutoff_and_execution_close_are_distinct_and_no_return_is_stolen():
    frame = prices(8)[["A"]]
    frame["A"] = [100, 100, 100, 200, 220, 242, 242, 242]
    seen = []

    def allocate(train, weights):
        seen.append((train.copy(), dict(weights)))
        return {"A": 1}

    result = evaluate(frame, allocate, execution_lag=2)
    window = result["windows"].iloc[0]
    assert window.train_end == frame.index[2]
    assert window.execution_day == frame.index[4]
    assert window.train_end < window.execution_day
    assert seen[0][0].index[-1] == frame.index[2]
    assert seen[0][1] == {"A": 0.0}
    daily = result["strategy"]["daily"]
    assert daily.iloc[0].net_return == 0
    assert daily.iloc[1].net_return == pytest.approx(0.1)


def test_current_weights_are_decision_day_weights_not_future_execution_weights():
    frame = prices(15)[["A", "B"]]
    seen = []

    def allocate(train, weights):
        seen.append((train.index[-1], weights.copy()))
        return {"A": 0.5, "B": 0.5}

    result = evaluate(frame, allocate, execution_lag=2)
    weights = result["strategy"]["weights"]
    for day, observed in seen[1:]:
        assert observed == pytest.approx(weights.loc[day, ["A", "B"]].to_dict())
    assert seen[1][1]["A"] > 0.5


def test_turnover_includes_cash_and_fees_and_slippage_compound():
    frame = prices(9)[["A"]]
    frame[:] = 100
    targets = iter([{"A": 1}, {}, {"A": 0.5}])
    result = evaluate(
        frame,
        lambda train, weights: next(targets),
        test_size=2,
        transaction_cost_bps=100,
        slippage_bps=50,
    )
    daily = result["strategy"]["daily"]
    trades = daily[daily.turnover > 0]
    assert trades.turnover.tolist() == [1, 1, 0.5]
    assert trades.one_way_turnover.tolist() == [1, 1, 0.5]
    assert trades.traded_notional.tolist() == [1, 1, 0.5]
    assert trades.transaction_cost.iloc[0] == pytest.approx(0.01)
    assert trades.slippage_cost.iloc[0] == pytest.approx(0.005)
    assert daily.equity.iloc[-1] == pytest.approx(0.985**2 * 0.9925)
    metrics = result["strategy"]["metrics"]
    assert metrics["total_turnover"] == 2.5
    assert metrics["total_one_way_turnover"] == 2.5
    assert metrics["total_traded_notional"] == 2.5
    assert metrics["total_cost"] == pytest.approx(1 - daily.equity.iloc[-1])
    assert metrics["max_drawdown"] == pytest.approx(1 - daily.equity.iloc[-1])
    assert metrics["cost_fraction_sum"] == pytest.approx(0.0375)


def test_weights_drift_and_rebalance_turnover_uses_drift():
    frame = prices(9)[["A", "B"]]
    frame["A"] = [100, 100, 100, 100, 200, 200, 200, 200, 200]
    frame["B"] = 100
    result = evaluate(frame, lambda train, weights: {"A": 0.5, "B": 0.5})
    portfolio = result["strategy"]
    assert portfolio["weights"].loc[frame.index[4], "A"] == pytest.approx(2 / 3)
    assert portfolio["weights"].loc[frame.index[6], "A"] == pytest.approx(2 / 3)
    assert portfolio["daily"].loc[frame.index[7], "turnover"] == pytest.approx(1 / 6)
    assert portfolio["daily"].loc[frame.index[7], "traded_notional"] == pytest.approx(1 / 3)
    assert portfolio["weights"].loc[frame.index[7], "A"] == pytest.approx(0.5)


def test_benchmarks_have_identical_cost_lag_dates_and_drift_engine():
    frame = prices()
    result = evaluate(
        frame,
        lambda train, weights: {symbol: 1 / 3 for symbol in train.columns},
        execution_lag=2,
        transaction_cost_bps=20,
        slippage_bps=10,
    )
    assert_frame_equal(result["strategy"]["daily"], result["benchmarks"]["equal_weight"]["daily"])
    spy = evaluate(
        frame,
        lambda train, weights: {"SPY": 1},
        execution_lag=2,
        transaction_cost_bps=20,
        slippage_bps=10,
    )
    assert_frame_equal(spy["strategy"]["daily"], result["benchmarks"]["spy_cash"]["daily"])
    assert result["benchmarks"]["spy_cash"]["asset"] == "SPY"
    cash = result["benchmarks"]["cash"]
    assert (cash["daily"].equity == 1).all()
    assert cash["metrics"]["total_turnover"] == 0
    assert cash["metrics"]["sharpe"] is None
    assert cash["metrics"]["annualized_volatility"] == 0
    assert cash["metrics"]["total_traded_notional"] == 0


def test_missing_spy_is_explicit_cash_fallback_not_a_synthetic_spy():
    result = evaluate(prices()[["A"]])
    assert result["benchmarks"]["spy_cash"]["asset"] == "CASH"
    assert any("SPY absent" in note for note in result["limitations"])
    assert_frame_equal(
        result["benchmarks"]["cash"]["daily"], result["benchmarks"]["spy_cash"]["daily"]
    )


def test_windows_cover_each_oos_day_exactly_once_including_partial_tail():
    frame = prices(18)
    result = evaluate(frame, execution_lag=2)
    days = []
    for window in result["windows"].itertuples():
        days.extend(frame.loc[window.test_start : window.test_end].index)
        assert window.train_end < window.execution_day == window.test_start
    assert pd.DatetimeIndex(days).equals(result["strategy"]["daily"].index)
    assert len(days) == len(set(days)) == 14
    assert result["windows"].sample_days.tolist() == [4, 4, 4, 2]


def test_callback_cannot_mutate_engine_inputs_or_previous_weights():
    frame = prices()
    before = frame.copy(deep=True)

    def hostile_callback(train, weights):
        train.iloc[:] = 999
        train.index = pd.date_range("1999-01-01", periods=len(train))
        train.columns = ["OTHER_A", "OTHER_B", "OTHER_SPY"]
        weights["A"] = 999
        return {"A": 1}

    mutated = evaluate(frame, hostile_callback)
    ordinary = evaluate(frame)
    assert_frame_equal(before, frame)
    assert_frame_equal(mutated["strategy"]["daily"], ordinary["strategy"]["daily"])


def test_bankruptcy_is_absorbing_costs_are_capped_and_never_resurrects():
    calls = []
    result = evaluate(
        prices(),
        lambda train, weights: calls.append(1) or {"A": 1},
        transaction_cost_bps=12000,
        slippage_bps=8000,
    )
    portfolio = result["strategy"]
    assert len(calls) == 1
    assert (portfolio["daily"].equity == 0).all()
    assert portfolio["daily"].net_return.tolist() == [-1] + [0] * 20
    assert portfolio["daily"].transaction_cost.sum() == pytest.approx(0.6)
    assert portfolio["daily"].slippage_cost.sum() == pytest.approx(0.4)
    assert (portfolio["weights"] == 0).all().all()
    assert portfolio["metrics"]["bankrupt"] is True
    assert portfolio["metrics"]["cagr"] == -1
    assert portfolio["metrics"]["max_drawdown"] == 1


def test_metrics_use_net_returns_sample_days_and_initial_nav():
    frame = prices(6)[["A"]]
    result = evaluate(frame, transaction_cost_bps=100)
    daily = result["strategy"]["daily"]
    metrics = result["strategy"]["metrics"]
    expected = np.array([-0.01, 0.01, 0.01])
    np.testing.assert_allclose(daily.net_return, expected)
    assert metrics["sample_days"] == 3
    assert metrics["cagr"] == pytest.approx(daily.equity.iloc[-1] ** (252 / 3) - 1)
    assert metrics["sharpe"] == pytest.approx(expected.mean() / expected.std(ddof=1) * np.sqrt(252))
    assert metrics["annualized_volatility"] == pytest.approx(expected.std(ddof=1) * np.sqrt(252))
    assert metrics["max_drawdown"] == pytest.approx(0.01)
    assert metrics["annualized_turnover"] == pytest.approx(252 / 3)


@pytest.mark.parametrize("n,lag", [(0, 1), (2, 1), (3, 1), (4, 2)])
def test_insufficient_data_is_explicit(n, lag):
    with pytest.raises(ValueError, match="insufficient"):
        evaluate(prices(n), execution_lag=lag)


def test_single_execution_day_reports_undefined_sharpe_not_nan():
    result = evaluate(prices(4))
    assert result["strategy"]["metrics"]["sample_days"] == 1
    assert result["strategy"]["metrics"]["sharpe"] is None
    assert result["strategy"]["metrics"]["annualized_volatility"] is None


@pytest.mark.parametrize("value", [np.nan, np.inf, -np.inf, 0, -1])
def test_missing_nonfinite_nonpositive_prices_are_rejected_without_filling(value):
    frame = prices()
    frame.iloc[5, 1] = value
    with pytest.raises(ValueError, match="finite and positive"):
        evaluate(frame)


@pytest.mark.parametrize("kind", ["duplicate", "unsorted", "nat", "future", "intraday", "nondate"])
def test_invalid_indexes_are_rejected(kind):
    frame = prices()
    dates = list(frame.index)
    if kind == "duplicate":
        dates[2] = dates[1]
    elif kind == "unsorted":
        dates[1], dates[2] = dates[2], dates[1]
    elif kind == "nat":
        dates[2] = pd.NaT
    elif kind == "future":
        dates[-1] = pd.Timestamp("2099-01-01")
    elif kind == "intraday":
        dates[2] += pd.Timedelta(hours=1)
    else:
        dates = list(range(len(frame)))
    frame.index = dates
    with pytest.raises(ValueError):
        evaluate(frame)


@pytest.mark.parametrize("columns", [["A", "A", "SPY"], ["A", "CASH", "SPY"], ["A", "", "SPY"]])
def test_duplicate_or_invalid_symbols_are_rejected(columns):
    frame = prices()
    frame.columns = columns
    with pytest.raises(ValueError):
        evaluate(frame)


def test_stale_price_guard_is_configurable_and_never_fills():
    frame = prices()
    frame.iloc[2:8, 0] = 100
    with pytest.raises(ValueError, match="stale"):
        evaluate(frame, max_stale_sessions=5)
    result = evaluate(frame, max_stale_sessions=None)
    assert any("stale" in note.lower() for note in result["limitations"])


def test_as_of_rejects_unavailable_dates_and_accepts_timezone_aware_daily_index():
    frame = prices()
    with pytest.raises(ValueError, match="future"):
        evaluate(frame, as_of=frame.index[5])
    frame.index = frame.index.tz_localize("America/New_York")
    assert evaluate(frame)["strategy"]["daily"].index.tz == frame.index.tz


@pytest.mark.parametrize(
    "target",
    [
        {"MISSING": 0.1},
        {"CASH": 0.1},
        {"A": -0.1},
        {"A": np.nan},
        {"A": np.inf},
        {"A": 1.1},
        {"A": 0.8, "B": 0.3},
        [],
        {"A": True},
    ],
)
def test_invalid_allocator_output_fails_closed(target):
    with pytest.raises(ValueError, match="allocator"):
        evaluate(prices(), lambda train, weights: target)


@pytest.mark.parametrize(
    "options",
    [
        {"execution_lag": 0},
        {"execution_lag": -1},
        {"execution_lag": 1.5},
        {"train_size": 2},
        {"min_train_size": 0},
        {"test_size": 0},
        {"test_size": True},
        {"transaction_cost_bps": -1},
        {"slippage_bps": np.inf},
        {"max_stale_sessions": 0},
    ],
)
def test_invalid_configuration_is_rejected(options):
    with pytest.raises(ValueError):
        evaluate(prices(), **options)


def test_limitations_are_reported_and_no_parameter_selection_is_performed():
    result = evaluate(prices())
    notes = " ".join(result["limitations"]).lower()
    for word in ("survivorship", "price-only", "news", "options", "tuning", "252", "cash"):
        assert word in notes
    assert result["config"]["parameter_selection"] == "none"


def test_callback_receives_exact_rolling_slice_without_caller_metadata():
    frame = prices(18)
    frame.attrs["future_signal"] = "not training data"
    observed = []

    def allocator(train, weights):
        observed.append(train.copy(deep=True))
        assert train.attrs == {}
        assert set(weights) == set(frame.columns)
        return {}

    result = evaluate(frame, allocator)
    for train, window in zip(observed, result["windows"].itertuples(), strict=True):
        expected = frame.loc[window.train_start : window.train_end].copy()
        expected.attrs = {}
        assert_frame_equal(train, expected)
        assert window.train_sample_days == len(train)
        assert len(train) <= 6


def test_total_loss_from_numerical_underflow_remains_bankrupt():
    frame = prices(8)[["A"]]
    frame["A"] = [1e308, 1e308, 1e308, 1e308, 1e-308, 1e-308, 1e-308, 1e-308]
    portfolio = evaluate(frame)["strategy"]
    assert portfolio["daily"].equity.tolist() == [1, 0, 0, 0, 0]
    assert portfolio["daily"].net_return.tolist() == [0, -1, 0, 0, 0]
    assert portfolio["metrics"]["bankrupt"] is True


def test_unrepresentable_price_relative_fails_instead_of_emitting_nonfinite_nav():
    frame = prices(8)[["A"]]
    frame["A"] = [1e-308, 1e-308, 1e-308, 1e-308, 1e308, 1e308, 1e308, 1e308]
    with pytest.raises(ValueError, match="nonfinite price return or NAV"):
        evaluate(frame)


def test_overflowing_annualized_return_is_explicitly_undefined():
    frame = prices(5)[["A"]]
    frame["A"] = [1, 1, 1, 1, 1e100]
    result = evaluate(frame)
    assert result["strategy"]["metrics"]["cagr"] is None
    assert np.isfinite(result["strategy"]["daily"].equity).all()


def test_allocator_exception_is_not_silently_replaced_by_cash():
    def broken(train, weights):
        raise RuntimeError("allocator failed")

    with pytest.raises(RuntimeError, match="allocator failed"):
        evaluate(prices(), broken)


def test_default_as_of_rejects_future_session_without_network_or_external_clock_mock():
    frame = prices()
    frame.index = pd.bdate_range("2099-01-01", periods=len(frame))
    with pytest.raises(ValueError, match="future"):
        evaluate_walk_forward(frame, lambda train, weights: {})


@pytest.mark.parametrize("lag", [1, 2, 5])
def test_full_asset_rotation_charges_both_legs_but_cash_entry_only_one(lag):
    frame = prices(8 + lag)[["A", "B"]]
    frame[:] = 100
    targets = iter([{"A": 1}, {"B": 1}, {"B": 1}])
    result = evaluate(
        frame,
        lambda train, weights: next(targets),
        test_size=2,
        execution_lag=lag,
        transaction_cost_bps=100,
        slippage_bps=50,
    )
    portfolio = result["strategy"]
    daily = portfolio["daily"]
    assert daily.one_way_turnover.tolist() == [1, 0, 1, 0, 0, 0]
    assert daily.traded_notional.tolist() == [1, 0, 2, 0, 0, 0]
    np.testing.assert_allclose(daily.turnover, daily.one_way_turnover)
    np.testing.assert_allclose(daily.cost_fraction, [0.015, 0, 0.03, 0, 0, 0])
    assert daily.transaction_cost.iloc[0] == pytest.approx(0.01)
    assert daily.slippage_cost.iloc[0] == pytest.approx(0.005)
    assert daily.transaction_cost.iloc[2] == pytest.approx(0.985 * 2 * 0.01)
    assert daily.slippage_cost.iloc[2] == pytest.approx(0.985 * 2 * 0.005)
    assert daily.equity.iloc[-1] == pytest.approx(0.985 * 0.97)
    metrics = portfolio["metrics"]
    assert metrics["total_one_way_turnover"] == metrics["total_turnover"] == 2
    assert metrics["total_traded_notional"] == 3
    assert metrics["annualized_traded_notional"] == pytest.approx(3 * 252 / 6)
    assert metrics["annualized_one_way_turnover"] == metrics["annualized_turnover"]
    assert metrics["total_cost"] == pytest.approx(1 - 0.985 * 0.97)
    assert result["config"]["cost_basis"] == "asset_leg_traded_notional"
    assert result["config"]["traded_notional_convention"] == "l1_excluding_cash"


def test_asset_leg_fees_use_execution_day_drift_and_pre_cost_nav():
    frame = prices(9)[["A", "B"]]
    frame["A"] = [100, 100, 100, 100, 200, 200, 200, 200, 200]
    frame["B"] = 100
    result = evaluate(
        frame,
        lambda train, weights: {"A": 0.5, "B": 0.5},
        transaction_cost_bps=100,
        slippage_bps=50,
    )
    daily = result["strategy"]["daily"]
    rebalance = daily.loc[frame.index[7]]
    assert rebalance.one_way_turnover == pytest.approx(1 / 6)
    assert rebalance.traded_notional == pytest.approx(1 / 3)
    assert rebalance.transaction_cost == pytest.approx(0.985 * 1.5 / 3 * 0.01)
    assert rebalance.slippage_cost == pytest.approx(0.985 * 1.5 / 3 * 0.005)
    assert rebalance.equity == pytest.approx(0.985 * 1.5 * 0.995)
    assert (daily.loc[frame.index[4] : frame.index[6], "traded_notional"] == 0).all()


def test_two_leg_rotation_can_bankrupt_and_cost_cap_still_applies():
    frame = prices(9)[["A", "B"]]
    frame[:] = 100
    targets = iter([{"A": 1}, {"B": 1}])
    result = evaluate(
        frame,
        lambda train, weights: next(targets),
        test_size=2,
        transaction_cost_bps=4000,
        slippage_bps=2000,
    )
    daily = result["strategy"]["daily"]
    assert daily.traded_notional.tolist() == [1, 0, 2, 0, 0, 0]
    assert daily.cost_fraction.tolist() == pytest.approx([0.6, 0, 1, 0, 0, 0])
    assert daily.equity.tolist() == pytest.approx([0.4, 0.4, 0, 0, 0, 0])
    assert daily.transaction_cost.sum() == pytest.approx(2 / 3)
    assert daily.slippage_cost.sum() == pytest.approx(1 / 3)
    assert result["strategy"]["metrics"]["bankrupt"] is True
