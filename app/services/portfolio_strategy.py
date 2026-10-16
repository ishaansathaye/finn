"""Constrained risk allocation. Scores/estimated risk are not profit probabilities."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib

import numpy as np
import pandas as pd
from scipy.optimize import minimize


VERSION = "stock-strategy-v1"


@dataclass(frozen=True)
class PortfolioLimits:
    min_observations: int = 126
    shrinkage: float = 0.25
    max_stock_weight: float = 0.12
    max_sector_weight: float = 0.35
    max_new_weight: float = 0.05
    min_risk_coverage: float = 0.95
    max_volatility_increase: float = 0.01

    def __post_init__(self):
        if self.min_observations < 30:
            raise ValueError("insufficient configured history")
        for field in ("shrinkage", "max_stock_weight", "max_sector_weight", "max_new_weight", "min_risk_coverage"):
            value = getattr(self, field)
            if not np.isfinite(value) or not 0 < value <= 1:
                raise ValueError(f"invalid {field}")
        if not np.isfinite(self.max_volatility_increase) or not 0 <= self.max_volatility_increase <= .1:
            raise ValueError("invalid volatility allowance")


def align_histories(histories: dict[str, list[dict]], as_of: datetime, min_observations=126):
    """Completed UTC-dated bars only; no forward/back filling or inferred dates."""
    cutoff = pd.Timestamp(as_of)
    if cutoff.tzinfo is None:
        raise ValueError("as_of must be timezone aware")
    cutoff = cutoff.tz_convert("UTC").normalize()
    series, excluded = {}, {}
    for symbol, rows in histories.items():
        values = {}
        invalid = False
        for row in rows:
            try:
                stamp = pd.Timestamp(row.get("datetime"))
                if pd.isna(stamp):
                    continue
                stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
                stamp = stamp.normalize()
                price = float(row.get("adjusted_close") or row.get("close") or 0)
            except (ValueError, TypeError, OverflowError):
                continue
            if stamp >= cutoff or not np.isfinite(price) or price <= 0:
                continue
            if stamp in values and values[stamp] != price:
                invalid = True
                break
            values[stamp] = price
        if invalid or len(values) < min_observations:
            excluded[symbol] = "ambiguous_dates" if invalid else "insufficient_history"
            continue
        if (cutoff - max(values)).days > 5:
            excluded[symbol] = "stale_history"
            continue
        series[symbol] = pd.Series(values, dtype=float).sort_index()
    frame = pd.DataFrame(series).sort_index()
    # Remove sparse series explicitly rather than fabricating observations.
    while len(frame.columns) and len(frame.dropna()) < min_observations:
        missing = frame.isna().sum().sort_values(ascending=False)
        symbol = str(missing.index[0])
        excluded[symbol] = "insufficient_shared_history"
        frame = frame.drop(columns=symbol)
    return frame.dropna().tail(253), excluded


def validate_prices(prices: pd.DataFrame, minimum=30) -> pd.DataFrame:
    if (not isinstance(prices, pd.DataFrame) or prices.empty or len(prices) < minimum
            or not isinstance(prices.index, pd.DatetimeIndex) or prices.index.has_duplicates
            or not prices.index.is_monotonic_increasing or prices.columns.has_duplicates):
        raise ValueError("invalid price panel")
    values = prices.to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError("prices must be complete, finite and positive")
    return prices


def shrink_covariance(prices: pd.DataFrame, shrinkage=.25) -> np.ndarray:
    validate_prices(prices)
    if not np.isfinite(shrinkage) or not 0 <= shrinkage <= 1:
        raise ValueError("invalid shrinkage")
    returns = prices.pct_change(fill_method=None).iloc[1:].to_numpy()
    covariance = np.atleast_2d(np.cov(returns, rowvar=False, ddof=1)) * 252
    covariance = (1 - shrinkage) * covariance + shrinkage * np.diag(np.diag(covariance))
    return (covariance + covariance.T) / 2 + np.eye(len(prices.columns)) * 1e-10


def panel_fingerprint(prices: pd.DataFrame) -> str:
    return hashlib.sha256(prices.to_csv(float_format="%.10g").encode()).hexdigest()


def market_regime(benchmark: pd.Series | None) -> dict:
    if benchmark is None or len(benchmark) < 200:
        return {"status": "unknown", "deployment_scale": .5, "reason": "insufficient benchmark history"}
    p = benchmark.to_numpy(dtype=float)
    if not np.isfinite(p).all() or (p <= 0).any():
        return {"status": "unknown", "deployment_scale": .5, "reason": "invalid benchmark history"}
    volatility = float(np.std(np.diff(p[-22:]) / p[-22:-1], ddof=1) * np.sqrt(252))
    drawdown = float(p[-1] / max(p[-63:]) - 1)
    below_trend = bool(p[-1] < np.mean(p[-200:]))
    stressed = below_trend and (volatility > .25 or drawdown < -.10)
    scale = .25 if stressed else .5 if below_trend or volatility > .30 else 1.0
    return {"status": "stress" if stressed else "cautious" if scale < 1 else "normal",
            "deployment_scale": scale, "volatility_21d_pct": round(volatility * 100, 2),
            "drawdown_63d_pct": round(drawdown * 100, 2), "below_200d": below_trend,
            "reason": "benchmark trend/volatility risk filter; not a direction forecast"}


def risk_summary(prices: pd.DataFrame, weights: np.ndarray, covariance: np.ndarray) -> dict:
    volatility = float(np.sqrt(max(0, weights @ covariance @ weights)))
    daily = prices.pct_change(fill_method=None).iloc[1:].to_numpy() @ weights
    quantile = float(np.quantile(daily, .05))
    tail = daily[daily <= quantile]
    contribution = weights * (covariance @ weights)
    total = float(contribution.sum())
    return {"annualized_volatility_pct": round(volatility * 100, 3),
            "historical_daily_expected_shortfall_95_pct": round(max(0, -float(tail.mean())) * 100, 3),
            "risk_contribution_pct": {str(s): round(float(v / total) * 100, 2) if total > 0 else 0
                                      for s, v in zip(prices.columns, contribution, strict=True)}}


def optimize_incremental(
    prices: pd.DataFrame, holdings_values: dict[str, float], candidate_caps: dict[str, float],
    sectors: dict[str, str], nav: float, cash_budget: float, *, limits: PortfolioLimits | None = None,
    deployment_scale: float = 1.0,
) -> dict:
    """Optimize only additions; current holdings cannot be sold or borrowed against."""
    limits = limits or PortfolioLimits()
    result = {"version": VERSION, "allocations": {s: 0.0 for s in candidate_caps},
              "status": "blocked", "method": "incremental_shrinkage_minimum_variance",
              "limits": asdict(limits), "warnings": []}
    inputs = [nav, cash_budget, deployment_scale, *holdings_values.values(), *candidate_caps.values()]
    if not all(np.isfinite(x) for x in inputs) or nav <= 0 or cash_budget < 0 or not 0 <= deployment_scale <= 1:
        result["reason"] = "invalid_portfolio_inputs"
        return result
    if any(x < 0 for x in list(holdings_values.values()) + list(candidate_caps.values())):
        result["reason"] = "negative_position_or_order"
        return result
    held_total = sum(holdings_values.values())
    if held_total > nav * 1.01 or cash_budget > nav:
        result["reason"] = "inconsistent_portfolio_snapshot"
        return result
    uninvested_value = max(0., nav - held_total)
    if cash_budget > uninvested_value:
        cash_budget = uninvested_value
        result["warnings"].append("cash allowance reduced to uninvested account value; no leverage permitted")
    try:
        validate_prices(prices, limits.min_observations)
        covariance = shrink_covariance(prices, limits.shrinkage)
    except (ValueError, TypeError):
        result["reason"] = "insufficient_risk_history"
        return result
    symbols = list(prices.columns)
    known_value = sum(value for symbol, value in holdings_values.items() if symbol in symbols)
    coverage = known_value / held_total if held_total else 1.0
    result.update({"coverage": round(coverage, 6), "data_fingerprint": panel_fingerprint(prices),
                   "history_start": prices.index[0].isoformat(), "history_end": prices.index[-1].isoformat(),
                   "observations": len(prices)})
    if coverage < 1:
        result["warnings"].append("Risk estimates exclude holdings without usable history; coverage is not whole-account coverage.")
    if coverage < limits.min_risk_coverage:
        result["reason"] = "incomplete_holdings_risk_coverage"
        return result
    current = np.array([holdings_values.get(s, 0) / nav for s in symbols])
    result["risk_before"] = risk_summary(prices, current, covariance)
    sector_current = {}
    for symbol, value in holdings_values.items():
        sector = sectors.get(symbol, "Unknown")
        sector_current[sector] = sector_current.get(sector, 0) + value / nav
    result["sector_weights_before"] = sector_current
    rejections = {}
    for symbol in candidate_caps:
        sector = sectors.get(symbol, "Unknown")
        if symbol not in symbols:
            rejections[symbol] = "insufficient aligned price history"
        elif holdings_values.get(symbol, 0) / nav >= limits.max_stock_weight:
            rejections[symbol] = f"{symbol} already reaches the {limits.max_stock_weight:.0%} position limit"
        elif sector_current.get(sector, 0) >= limits.max_sector_weight:
            rejections[symbol] = f"{sector} exposure is {sector_current[sector]:.0%}, above the {limits.max_sector_weight:.0%} sector limit"
    result["candidate_rejections"] = rejections
    bounds = np.array([min(candidate_caps.get(s, 0) / nav, max(0, limits.max_stock_weight - current[i]))
                       for i, s in enumerate(symbols)])
    groups = {sectors.get(s, "Unknown") for s in symbols}
    headroom = {g: max(0, limits.max_sector_weight - sector_current.get(g, 0)) for g in groups}
    masks = {g: np.array([sectors.get(s, "Unknown") == g for s in symbols]) for g in groups}
    feasible = sum(min(float(bounds[masks[g]].sum()), headroom[g]) for g in groups)
    budget = min(cash_budget / nav * deployment_scale, limits.max_new_weight, feasible)
    if budget < 10 / nav or bounds.sum() == 0:
        result.update({"status": "cash_only", "reason": "cash_or_concentration_limits", "risk_after": result["risk_before"]})
        return result
    # Build a feasible initial point sector by sector, with no forced full-cash investment.
    start = np.zeros(len(symbols))
    remaining = budget
    for g in sorted(groups):
        indices = np.flatnonzero(masks[g])
        available = float(bounds[indices].sum())
        amount = min(remaining, headroom[g], available)
        if available > 0:
            start[indices] = bounds[indices] * amount / available
            remaining -= amount
    constraints = [{"type": "eq", "fun": lambda x: float(x.sum() - budget)}]
    constraints.extend({"type": "ineq", "fun": lambda x, m=masks[g], h=headroom[g]: float(h - x[m].sum())} for g in sorted(groups))
    try:
        fitted = minimize(lambda x: float((current+x) @ covariance @ (current+x)), start,
                          jac=lambda x: 2 * covariance @ (current+x), method="SLSQP",
                          bounds=list(zip(np.zeros(len(bounds)), bounds, strict=True)),
                          constraints=constraints, options={"ftol": 1e-12, "maxiter": 200})
        added = np.array(fitted.x)
        valid = (fitted.success and np.isfinite(added).all() and (added >= -1e-8).all()
                 and (added <= bounds + 1e-8).all() and abs(added.sum() - budget) < 1e-6
                 and all(added[masks[g]].sum() <= headroom[g] + 1e-7 for g in groups))
    except Exception:
        valid = False
    if not valid:
        result["reason"] = "optimizer_failed_constraints"
        return result
    added = np.clip(added, 0, bounds)
    before_vol = np.sqrt(current @ covariance @ current)
    max_vol = before_vol + limits.max_volatility_increase
    if np.sqrt((current+added) @ covariance @ (current+added)) > max_vol:
        low, high = 0., 1.
        for _ in range(50):
            middle = (low+high)/2
            if np.sqrt((current+added*middle) @ covariance @ (current+added*middle)) <= max_vol:
                low = middle
            else:
                high = middle
        added *= low
        result["warnings"].append("additions scaled down to respect incremental volatility limit")
    # Floor cents, never round above a cap; tiny orders remain cash.
    allocated = np.floor(added * nav * 100 + 1e-8) / 100
    allocated[allocated < 10] = 0
    result["allocations"].update({s: float(allocated[i]) for i, s in enumerate(symbols) if s in candidate_caps})
    result.update({"status": "optimized", "reason": "risk_constrained_additions",
                   "risk_after": risk_summary(prices, current + allocated/nav, covariance),
                   "cash_retained_usd": round(cash_budget - float(allocated.sum()), 2),
                   "sector_weights_before": sector_current,
                   "sector_weights_after": {g: sector_current.get(g, 0) + float(allocated[masks[g]].sum())/nav for g in groups},
                   "notes": "Current holdings fixed. ETF sector labels are not holdings look-through. Risk estimates are historical, not guaranteed loss bounds."})
    return result


def research_weights(train_prices: pd.DataFrame, method="minimum_variance") -> dict[str, float]:
    """Fixed-model shadow allocator; not a historical replay of Finn's news decisions."""
    validate_prices(train_prices)
    if method == "regime_minimum_variance":
        if "SPY" not in train_prices:
            raise ValueError("regime strategy requires an explicit SPY benchmark")
        weights = research_weights(train_prices, "minimum_variance")
        scale = market_regime(train_prices["SPY"])["deployment_scale"]
        return {symbol: weight * scale for symbol, weight in weights.items()}
    count = len(train_prices.columns)
    covariance = shrink_covariance(train_prices)
    cap = max(.35, 1/count)
    initial = np.ones(count) / count
    if method == "equal_weight":
        return dict(zip(train_prices.columns, initial.tolist(), strict=True))
    if method == "inverse_volatility":
        target = 1 / np.sqrt(np.maximum(np.diag(covariance), 1e-8))
        target /= target.sum()
        def objective(x):
            return float(((x-target)**2).sum())
    elif method == "minimum_variance":
        def objective(x):
            return float(x @ covariance @ x)
    else:
        raise ValueError("unknown strategy method")
    fitted = minimize(objective, initial, method="SLSQP", bounds=[(0, cap)]*count,
                      constraints=[{"type": "eq", "fun": lambda x: float(x.sum()-1)}],
                      options={"ftol": 1e-12, "maxiter": 200})
    weights = np.array(fitted.x)
    if (not fitted.success or not np.isfinite(weights).all() or (weights < -1e-8).any()
            or (weights > cap+1e-8).any() or abs(weights.sum()-1) > 1e-6):
        raise ValueError("research optimizer failed")
    weights = np.clip(weights, 0, cap)
    weights /= weights.sum()
    return dict(zip(train_prices.columns, weights.tolist(), strict=True))
