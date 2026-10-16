"""Offline, long-only, causal walk-forward evaluation of supplied daily closes.

Timing reference:
https://skfolio.org/generated/skfolio.model_selection.WalkForward.html
Bias reference:
https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/research-guide

This module neither fetches data nor places orders. Callbacks are trusted code:
they must be deterministic, offline, and must not capture future data externally.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from math import isfinite
from numbers import Integral, Real
from typing import Any

import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype, is_complex_dtype, is_numeric_dtype

Allocator = Callable[[pd.DataFrame, dict[str, float]], dict[str, float]]

_LIMITATIONS = (
    "Survivorship and universe-selection bias are not corrected; supply a point-in-time universe.",
    "Price-only evaluation cannot validate news, options, fundamentals, or publication timing.",
    "This is allocator model comparison, not historical reconstruction of news-selected trades "
    "or production incremental BUY sizing against fixed existing holdings.",
    "No parameter tuning or selection is performed. Any callback tuning must use a separate "
    "chronological validation split entirely within its training data, never OOS results.",
    "Callbacks are trusted: the engine cannot prevent external data access, captured future data, "
    "nondeterminism, or research selection bias.",
    "Daily rows are assumed to be consecutive exchange sessions with finalized closes. "
    "No exchange calendar, missing-session, quote age, or delisting validation is available.",
    "Unchanged-close stale detection is only a heuristic and may reject genuinely flat prices; "
    "disabling it leaves stale quotes undetected. No interpolation, forward-fill, or backfill occurs.",
    "Cash earns zero; Sharpe uses zero risk-free rate and sample standard deviation. Annualized "
    "volatility is the sample standard deviation of net daily returns times sqrt(252), undefined "
    "for fewer than two samples. CAGR, Sharpe, and volatility use 252 sessions, not calendar days; "
    "short samples are unreliable.",
    "Close execution is a model, not a fill guarantee. No volume, liquidity, taxes, market impact, "
    "dividends, splits, or financing model is included. Supply consistently adjusted prices; "
    "retrospective adjustments can themselves introduce point-in-time bias.",
    "One-way turnover is half L1 weight change INCLUDING residual cash. Costs instead use "
    "traded notional: the full L1 change of ASSET legs only, excluding cash, times pre-cost NAV. "
    "Bps are per dollar executed: cash-to-asset trades 1x NAV; full A-to-B rotation trades 2x NAV. "
    "Targets are assigned on post-cost NAV; this is a proportional-cost approximation, not an "
    "exact shares/cash execution ledger. Costs are capped at remaining NAV on bankruptcy; "
    "traded notional still reports the modeled target change, not actual fills.",
    "All portfolios begin with NAV 1 in cash and use the same rebalance dates, lag, and cost model. "
    "Execution occurs after that day's old-holdings return. The last OOS window may be partial; "
    "there is no forced terminal liquidation or terminal liquidation cost.",
)


def _positive_integer(name: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, Integral) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _validate_prices(
    prices: pd.DataFrame, as_of: Any, max_stale_sessions: int | None
) -> pd.DataFrame:
    if not isinstance(prices, pd.DataFrame):
        raise ValueError("prices must be a daily close DataFrame")
    index = prices.index
    if not isinstance(index, pd.DatetimeIndex):
        raise ValueError("prices require a DatetimeIndex")
    if index.hasnans or not index.is_unique or not index.is_monotonic_increasing:
        raise ValueError("price index must be increasing, unique, and free of NaT")
    if not index.equals(index.normalize()):
        raise ValueError("daily close index must contain normalized session dates")
    if not prices.columns.is_unique or not len(prices.columns):
        raise ValueError("prices require unique, nonempty symbol columns")
    if any(not isinstance(s, str) or not s.strip() or s == "CASH" for s in prices.columns):
        raise ValueError("symbols must be nonempty strings; CASH is reserved")
    cutoff = pd.Timestamp.now(tz="UTC") if as_of is None else pd.Timestamp(as_of)
    if pd.isna(cutoff):
        raise ValueError("as_of must be a valid date")
    # Session labels are dates, not timestamps of actual close publication.
    if len(index) and index[-1].date() > cutoff.date():
        raise ValueError("future price session exceeds as_of")
    if any(
        not is_numeric_dtype(dtype) or is_bool_dtype(dtype) or is_complex_dtype(dtype)
        for dtype in prices.dtypes
    ):
        raise ValueError("prices must be numeric, finite and positive")
    values = prices.to_numpy(dtype=float, copy=True, na_value=np.nan)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError("prices must be finite and positive; missing values are not filled")
    if max_stale_sessions is not None and len(values):
        runs = np.zeros(len(prices.columns), dtype=int)
        for i in range(1, len(values)):
            runs = np.where(values[i] == values[i - 1], runs + 1, 0)
            if (runs >= max_stale_sessions).any():
                symbols = prices.columns[runs >= max_stale_sessions].tolist()
                raise ValueError(f"potentially stale unchanged closes at {index[i]}: {symbols}")
    # Rebuild to detach values, axes, and arbitrary attrs from caller-owned objects.
    return pd.DataFrame(values, index=index.copy(deep=True), columns=prices.columns.copy(deep=True))


def _target_weights(target: Any, symbols: list[str]) -> np.ndarray:
    if not isinstance(target, Mapping) or any(symbol not in symbols for symbol in target):
        raise ValueError("allocator must return a symbol:weight mapping for supplied symbols")
    weights = np.zeros(len(symbols) + 1, dtype=float)
    for i, symbol in enumerate(symbols):
        value = target.get(symbol, 0.0)
        if (
            isinstance(value, bool)
            or not isinstance(value, Real)
            or not isfinite(value)
            or value < 0
        ):
            raise ValueError("allocator weights must be finite nonnegative real numbers")
        weights[i] = value
    total = float(weights[:-1].sum())
    if not isfinite(total) or total > 1.0 + 1e-12:
        raise ValueError("allocator weights must sum to at most one; no leverage or shorts")
    if total > 1.0:
        weights[:-1] /= total
    weights[-1] = max(0.0, 1.0 - total)
    return weights


def _metrics(daily: pd.DataFrame) -> dict[str, Any]:
    n = len(daily)
    returns = daily["net_return"].to_numpy()
    equity = daily["equity"].to_numpy()
    peak = np.maximum.accumulate(np.r_[1.0, equity])[1:]
    std = float(np.std(returns, ddof=1)) if n > 1 else 0.0
    volatility = std * float(np.sqrt(252)) if n > 1 else None
    sharpe = float(np.mean(returns) / std * np.sqrt(252)) if std > 1e-15 else None
    with np.errstate(over="ignore", invalid="ignore"):
        cagr = float(np.expm1(np.log(equity[-1]) * 252 / n)) if equity[-1] > 0 else -1.0
    turnover = float(daily["one_way_turnover"].sum())
    traded_notional = float(daily["traded_notional"].sum())
    return {
        "sample_days": n,
        "total_return": float(equity[-1] - 1.0),
        "cagr": cagr if isfinite(cagr) else None,
        "sharpe": sharpe if sharpe is None or isfinite(sharpe) else None,
        "annualized_volatility": (
            volatility if volatility is None or isfinite(volatility) else None
        ),
        "max_drawdown": float(np.max(1.0 - equity / peak)),
        "total_turnover": turnover,
        "annualized_turnover": turnover * 252 / n,
        "total_one_way_turnover": turnover,
        "annualized_one_way_turnover": turnover * 252 / n,
        "total_traded_notional": traded_notional,
        "annualized_traded_notional": traded_notional * 252 / n,
        "transaction_cost": float(daily["transaction_cost"].sum()),
        "slippage_cost": float(daily["slippage_cost"].sum()),
        "total_cost": float(daily["cost"].sum()),
        "cost_fraction_sum": float(daily["cost_fraction"].sum()),
        "bankrupt": bool(daily["bankrupt"].any()),
    }


def _simulate(
    prices: pd.DataFrame,
    allocator: Allocator,
    decisions: range,
    train_size: int,
    execution_lag: int,
    transaction_rate: float,
    slippage_rate: float,
) -> dict[str, Any]:
    symbols = list(prices.columns)
    values = prices.to_numpy()
    first_execution = decisions.start + execution_lag
    weights = np.zeros(len(symbols) + 1)
    weights[-1] = 1.0
    nav = 1.0
    pending: dict[int, np.ndarray] = {}
    rows = []
    weight_rows = []
    cost_rate = transaction_rate + slippage_rate

    for day in range(decisions.start, len(prices)):
        previous_nav = nav
        gross_return = 0.0
        one_way_turnover = traded_notional = 0.0
        transaction_cost = slippage_cost = cost_fraction = 0.0
        if day > decisions.start and nav > 0:
            # Mark old holdings before executing at today's close, including across folds.
            with np.errstate(over="ignore", under="ignore", invalid="ignore", divide="ignore"):
                relatives = np.r_[values[day] / values[day - 1], 1.0]
                marked = weights * relatives
                growth = float(marked.sum())
                nav *= growth
            if not np.isfinite(relatives).all() or not isfinite(nav):
                raise ValueError("nonfinite price return or NAV; rescale or check supplied prices")
            gross_return = growth - 1.0
            if nav <= 0 or growth <= 0:
                nav = 0.0
                weights[:] = 0
            else:
                weights = marked / growth

        target = pending.pop(day, None)
        if target is not None and nav > 0:
            changes = np.abs(target - weights)
            one_way_turnover = float(changes.sum() / 2.0)
            # Asset rotation executes both sell and buy legs; cash is not an execution leg.
            traded_notional = float(changes[:-1].sum())
            cost_fraction = min(1.0, traded_notional * cost_rate)
            total_cost = nav * cost_fraction
            if cost_rate > 0:
                transaction_cost = total_cost * transaction_rate / cost_rate
                slippage_cost = total_cost * slippage_rate / cost_rate
            nav *= 1.0 - cost_fraction
            weights = target.copy() if nav > 0 else np.zeros_like(weights)

        if day in decisions and nav > 0:
            start = max(0, day + 1 - train_size)
            train = prices.iloc[start : day + 1].copy(deep=True)
            train.index = train.index.copy(deep=True)
            train.columns = train.columns.copy(deep=True)
            current_weights = dict(zip(symbols, map(float, weights[:-1]), strict=True))
            target = _target_weights(allocator(train, current_weights), symbols)
            pending[day + execution_lag] = target

        if day >= first_execution:
            rows.append(
                {
                    "equity": nav,
                    "gross_return": gross_return,
                    "net_return": nav / previous_nav - 1.0 if previous_nav > 0 else 0.0,
                    "turnover": one_way_turnover,
                    "one_way_turnover": one_way_turnover,
                    "traded_notional": traded_notional,
                    "transaction_cost": transaction_cost,
                    "slippage_cost": slippage_cost,
                    "cost": transaction_cost + slippage_cost,
                    "cost_fraction": cost_fraction,
                    "bankrupt": nav == 0,
                }
            )
            weight_rows.append(weights.copy())

    index = prices.index[first_execution:].copy(deep=True)
    daily = pd.DataFrame(rows, index=index)
    return {
        "daily": daily,
        "weights": pd.DataFrame(weight_rows, index=index, columns=[*symbols, "CASH"]),
        "metrics": _metrics(daily),
    }


def evaluate_walk_forward(
    prices: pd.DataFrame,
    allocator: Allocator,
    *,
    train_size: int = 126,
    test_size: int = 21,
    min_train_size: int = 63,
    execution_lag: int = 1,
    transaction_cost_bps: float = 5.0,
    slippage_bps: float = 5.0,
    as_of: Any = None,
    max_stale_sessions: int | None = 5,
) -> dict[str, Any]:
    """Evaluate a fixed allocator without tuning or inspecting future prices.

    The callback receives a detached training-close DataFrame ending on the
    decision day and risky-asset weights marked at that day's close. Omitted
    target symbols have zero weight; residual weight is cash. Shorting/leverage
    and an explicit CASH target are rejected. Do not reuse stateful callbacks
    across evaluations or capture outside data in them.

    The first decision follows min_train_size closes; the training window grows
    to train_size then rolls. Setting min_train_size=train_size requests a full
    initial window. Every test_size sessions another decision is queued, even
    when execution_lag exceeds test_size. Its target executes at the close
    execution_lag rows later and earns price returns starting the following row.
    Old holdings remain exposed during the lag. No decision uses execution-day
    weights or prices. Only decisions that execute within the sample are made.

    Inputs must have finite positive numeric values in every cell and an aligned,
    increasing, unique, normalized DatetimeIndex. No rows/symbols are dropped or
    filled. as_of defaults to today's UTC session date; historical samples need
    not extend to as_of. max_stale_sessions rejects that many consecutive
    unchanged close-to-close intervals; None explicitly disables this heuristic.

    Results contain strategy and benchmarks (equal_weight across all supplied
    columns, spy_cash using SPY when present and otherwise explicit cash, cash).
    Each has daily accounting, post-close weights including CASH, and net metrics.
    Daily NAV/cost amounts are in initial-NAV units, not dollars. Windows give
    inclusive train/test dates and disjoint OOS sample counts. A zero NAV is
    absorbing, reported through the remaining common horizon, and stops callbacks.
    Daily one_way_turnover is half L1 change including cash; traded_notional is
    full L1 change of asset weights only, a fraction of that day's pre-cost NAV.
    Both are measured against execution-day drifted weights. Transaction costs
    and slippage charge their respective bps per asset-leg dollar executed.
    A cash-to-asset entry has traded_notional 1; a full A-to-B rotation has 2.
    Their total metrics sum these daily NAV fractions (not dollar amounts);
    annualized metrics multiply by 252 / sample_days. The original turnover,
    total_turnover, and annualized_turnover fields remain one-way aliases.
    Annualized volatility uses net daily returns and sample standard deviation.
    Undefined Sharpe, volatility with fewer than two samples, or overflowing
    annualized metrics are None, not NaN/Inf.
    """
    for name, value in (
        ("train_size", train_size),
        ("test_size", test_size),
        ("min_train_size", min_train_size),
        ("execution_lag", execution_lag),
    ):
        _positive_integer(name, value)
    if min_train_size > train_size:
        raise ValueError("min_train_size must not exceed train_size")
    if max_stale_sessions is not None:
        _positive_integer("max_stale_sessions", max_stale_sessions)
    for name, value in (
        ("transaction_cost_bps", transaction_cost_bps),
        ("slippage_bps", slippage_bps),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, Real)
            or not isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{name} must be finite and nonnegative")
    if not callable(allocator):
        raise ValueError("allocator must be callable")
    prices = _validate_prices(prices, as_of, max_stale_sessions)
    first_execution = min_train_size - 1 + execution_lag
    if len(prices) <= first_execution:
        raise ValueError("insufficient data for minimum training and one lagged execution session")
    decisions = range(min_train_size - 1, len(prices) - execution_lag, test_size)
    windows = []
    for decision in decisions:
        start = max(0, decision + 1 - train_size)
        execution = decision + execution_lag
        end = min(execution + test_size, len(prices)) - 1
        windows.append(
            {
                "train_start": prices.index[start],
                "train_end": prices.index[decision],
                "train_sample_days": decision - start + 1,
                "execution_day": prices.index[execution],
                "test_start": prices.index[execution],
                "test_end": prices.index[end],
                "sample_days": end - execution + 1,
            }
        )

    def run(callback: Allocator) -> dict[str, Any]:
        return _simulate(
            prices,
            callback,
            decisions,
            train_size,
            execution_lag,
            float(transaction_cost_bps) / 10000,
            float(slippage_bps) / 10000,
        )

    equal_weight = {symbol: 1.0 / len(prices.columns) for symbol in prices.columns}
    spy_present = "SPY" in prices.columns
    limitations = list(_LIMITATIONS)
    if not spy_present:
        limitations.append("SPY absent: spy_cash is a zero-return CASH fallback, not a SPY proxy.")
    if max_stale_sessions is None:
        limitations.append("Unchanged-close stale guard was explicitly disabled.")
    strategy = run(allocator)
    spy_cash = run(lambda train, weights: {"SPY": 1.0} if spy_present else {})
    spy_cash["asset"] = "SPY" if spy_present else "CASH"
    return {
        "strategy": strategy,
        "benchmarks": {
            "equal_weight": run(lambda train, weights: equal_weight),
            "spy_cash": spy_cash,
            "cash": run(lambda train, weights: {}),
        },
        "windows": pd.DataFrame(windows),
        "config": {
            "train_size": int(train_size),
            "test_size": int(test_size),
            "min_train_size": int(min_train_size),
            "execution_lag": int(execution_lag),
            "transaction_cost_bps": float(transaction_cost_bps),
            "slippage_bps": float(slippage_bps),
            "max_stale_sessions": max_stale_sessions,
            "parameter_selection": "none",
            "annualization_sessions": 252,
            "turnover_convention": "half_l1_including_cash",
            "traded_notional_convention": "l1_excluding_cash",
            "cost_basis": "asset_leg_traded_notional",
            "execution_timing": "lagged_close_then_next_session_returns",
        },
        "limitations": limitations,
    }
