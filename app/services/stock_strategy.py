"""Bridge pure portfolio research into stock recommendations, never broker orders."""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime
import logging
from decimal import Decimal, ROUND_DOWN
import numpy as np
from sqlmodel import select

from app.models import Holding

from app.services.portfolio_strategy import PortfolioLimits, align_histories, market_regime, optimize_incremental


async def apply_portfolio_strategy(market, session, ideas, histories, benchmark_history):
    if not market.settings.stock_strategy_enabled:
        return {"status": "disabled"}
    buy_rows = [idea for idea in ideas if idea.side == "buy"]
    counts = Counter(str(idea.symbol).upper() for idea in buy_rows)
    rejected = []
    buys = []
    for idea in buy_rows:
        try:
            amount = float(idea.amount_usd)
            valid = not isinstance(idea.amount_usd, bool) and np.isfinite(amount) and amount >= 0
        except (TypeError, ValueError, OverflowError):
            valid = False
        duplicate = counts[str(idea.symbol).upper()] > 1
        if not valid or duplicate:
            why = "duplicate buy candidates require a fresh analysis" if duplicate else "invalid proposed dollar amount"
            rejected.append({"symbol": idea.symbol, "reason": why})
            idea.side, idea.amount_usd, idea.confidence = "watch", 0, "low"
            idea.priority_score = min(idea.priority_score, 3)
            idea.reason = f"Watch, not a buy: {why}. No additional capital suggested."
        else:
            if amount == 0:
                continue
            idea.amount_usd = amount
            buys.append(idea)
    original = {idea.symbol: idea.amount_usd for idea in buys}
    try:
        snapshot = market._primary_account_snapshot(session)
        if snapshot is None:
            raise ValueError("primary portfolio snapshot unavailable")
        account_id, nav, buying_power = snapshot.account_id, float(snapshot.total_value), float(snapshot.buying_power)
        as_of = str(snapshot.as_of)
        holdings = {}
        for holding in session.exec(select(Holding).where(Holding.account_id == account_id, Holding.asset_type != "crypto")).all():
            holdings[holding.symbol] = holdings.get(holding.symbol, 0) + float(holding.market_value)
        from app.services.stock_advisor import StockAdvisorService
        advisor = StockAdvisorService(market)
        sectors = {s: advisor.sector_for_symbol(s) for s in set(holdings) | set(original)}
        needed = set(holdings) | set(original)
        panel_histories = {s: histories[s] for s in needed if s in histories}
        semaphore = asyncio.Semaphore(3)

        async def fetch(symbol):
            async with semaphore:
                return await market._get_history(session, symbol)

        missing = sorted(needed - set(panel_histories))
        responses = await asyncio.gather(*(fetch(s) for s in missing), return_exceptions=True)
        for symbol, rows in zip(missing, responses, strict=True):
            panel_histories[symbol] = [] if isinstance(rows, Exception) else rows
        now = datetime.now(UTC)
        panel, excluded = align_histories(panel_histories, now)
        benchmark_panel, _ = align_histories({"SPY": benchmark_history}, now)
        regime = market_regime(benchmark_panel.get("SPY"))
        reserve = buying_power * market.settings.max_total_prediction_bankroll_pct
        cash_budget = max(0, buying_power - reserve - market.settings.minimum_cash_buffer_usd)
        limits = PortfolioLimits(max_stock_weight=market.settings.max_single_stock_allocation_pct,
                                 max_sector_weight=market.settings.stock_strategy_max_sector_weight,
                                 max_new_weight=market.settings.stock_strategy_max_new_weight,
                                 max_volatility_increase=market.settings.stock_strategy_max_volatility_increase)
        caps = {s: value for s, value in original.items() if s not in market.settings.employment_excluded_symbols}
        result = optimize_incremental(panel, holdings, caps, sectors, nav, cash_budget,
                                      limits=limits, deployment_scale=regime["deployment_scale"])
        result.update({"as_of": now.isoformat(), "portfolio_as_of": as_of, "regime": regime,
                       "excluded_history": excluded, "account_scope": "primary_only",
                       "modeled_assets": "stocks_and_etfs_only",
                       "core_sales_allowed": False, "orders_enabled": False})
        if nav > 0 and not panel.empty and "SPY" in benchmark_panel:
            combined = panel.join(benchmark_panel.rename(columns={"SPY": "__benchmark"}), how="inner")
            if len(combined) >= 126:
                returns = combined.pct_change(fill_method=None).iloc[1:]
                weights = np.array([holdings.get(s, 0) / nav for s in panel.columns])
                portfolio_returns = returns[panel.columns].to_numpy() @ weights
                benchmark_returns = returns["__benchmark"].to_numpy()
                variance = float(np.var(benchmark_returns, ddof=1))
                result["benchmark_beta"] = (round(float(np.cov(portfolio_returns, benchmark_returns)[0, 1]) / variance, 4)
                                            if variance > 1e-12 else None)
                result["beta_scope"] = "modeled stock/ETF holdings relative to total primary-account value; not a forecast"
    except Exception as exc:
        logging.getLogger(__name__).warning("Stock portfolio risk unavailable: %s", type(exc).__name__)
        result = {"status": "unavailable", "reason": "portfolio_risk_data_unavailable",
                  "allocations": {}, "orders_enabled": False, "as_of": datetime.now(UTC).isoformat()}
    allocations = result.get("allocations", {})
    adjustments = []
    for idea in buys:
        amount = min(original[idea.symbol], max(0., float(allocations.get(idea.symbol, 0))))
        idea.amount_usd = float(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))
        if idea.amount_usd < 10:
            idea.side = "watch"
            idea.amount_usd = 0
            idea.confidence = "low"
            idea.priority_score = min(idea.priority_score, 3)
            why = "portfolio concentration/cash limits" if result.get("status") in {"cash_only", "optimized"} else "portfolio risk data unavailable"
            why = result.get("candidate_rejections", {}).get(idea.symbol, why)
            idea.reason = f"Watch, not a buy: {why}. No additional capital suggested."
        elif amount < original[idea.symbol] - .01:
            explanation = "portfolio correlation, sector exposure and cash-risk limits reduced this size"
            if " Risk watch: " in idea.reason:
                idea.reason = idea.reason.replace(" Risk watch: ", f" Risk watch: {explanation}; ", 1)
            else:
                idea.reason += f" Risk watch: {explanation}"
        adjustments.append({"symbol": idea.symbol, "before_usd": original[idea.symbol], "after_usd": idea.amount_usd})
        if idea.symbol in allocations:
            allocations[idea.symbol] = idea.amount_usd
    result["allocations"] = allocations
    result["rejected_candidates"] = rejected
    result["recommendation_adjustments"] = adjustments
    market.cache.set_json(session, "stock_strategy", "latest", result)
    return result
