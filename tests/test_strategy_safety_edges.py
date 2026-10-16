"""Regression coverage for three reproduced strategy safety failures; no external I/O."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pandas as pd
import pytest

from app.services import stock_strategy
from app.services.portfolio_strategy import optimize_incremental

NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.replace(tzinfo=None) if tz is None else NOW.astimezone(tz)


def price_panel(symbols):
    return pd.DataFrame(
        {symbol: 100 + np.arange(252) * 0.001 for symbol in symbols},
        index=pd.bdate_range(end="2026-10-06", periods=252, tz="UTC"),
    )


def histories_for(symbols):
    panel = price_panel(symbols)
    return {
        symbol: [
            {"datetime": stamp.isoformat(), "close": float(value)}
            for stamp, value in panel[symbol].items()
        ]
        for symbol in symbols
    }


def idea(symbol, amount):
    return SimpleNamespace(
        symbol=symbol,
        side="buy",
        amount_usd=amount,
        confidence="high",
        priority_score=8,
        reason="Safety regression",
    )


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.setattr(stock_strategy, "datetime", FrozenDatetime)
    monkeypatch.setattr(
        "app.services.stock_advisor.StockAdvisorService",
        lambda market: SimpleNamespace(sector_for_symbol=lambda symbol: symbol.upper()),
    )
    holdings = []
    session = SimpleNamespace(exec=Mock(return_value=SimpleNamespace(all=lambda: holdings)))
    snapshot = SimpleNamespace(
        account_id="safety-primary",
        total_value=10000,
        buying_power=1000,
        as_of=NOW,
    )
    market = SimpleNamespace(
        settings=SimpleNamespace(
            stock_strategy_enabled=True,
            max_total_prediction_bankroll_pct=0,
            minimum_cash_buffer_usd=0,
            max_single_stock_allocation_pct=0.12,
            stock_strategy_max_sector_weight=0.35,
            stock_strategy_max_new_weight=0.05,
            stock_strategy_max_volatility_increase=0.01,
            employment_excluded_symbols=set(),
        ),
        _primary_account_snapshot=Mock(return_value=snapshot),
        _get_history=AsyncMock(side_effect=AssertionError("unexpected external history request")),
        cache=SimpleNamespace(set_json=Mock()),
    )
    # Observe cap inputs without replacing the actual optimizer or its risk checks.
    optimizer = Mock(wraps=stock_strategy.optimize_incremental)
    monkeypatch.setattr(stock_strategy, "optimize_incremental", optimizer)
    return SimpleNamespace(
        market=market,
        session=session,
        holdings=holdings,
        snapshot=snapshot,
        optimizer=optimizer,
    )


async def run(environment, ideas, symbols):
    result = await stock_strategy.apply_portfolio_strategy(
        environment.market,
        environment.session,
        ideas,
        histories_for(symbols),
        histories_for(["SPY"])["SPY"],
    )
    environment.market._get_history.assert_not_awaited()
    environment.market.cache.set_json.assert_called_once_with(
        environment.session,
        "stock_strategy",
        "latest",
        result,
    )
    assert result["orders_enabled"] is False
    return result


def assert_watch(candidate):
    assert candidate.side == "watch"
    assert candidate.amount_usd == 0
    assert candidate.confidence == "low"


@pytest.mark.asyncio
@pytest.mark.parametrize("symbols", [("XOM", "XOM"), ("XOM", "xom")])
async def test_all_duplicate_symbol_buys_become_watch_without_symbol_allocation(
    environment, symbols
):
    duplicates = [idea(symbols[0], 20), idea(symbols[1], 100)]
    valid = idea("MSFT", 40)
    result = await run(environment, [*duplicates, valid], [*set(symbols), "MSFT"])

    for candidate in duplicates:
        assert_watch(candidate)
    assert not any(symbol.upper() == "XOM" for symbol in result["allocations"])
    environment.optimizer.assert_called_once()
    assert environment.optimizer.call_args.args[2] == {"MSFT": 40}
    assert result["status"] == "optimized"
    assert valid.side == "buy"
    assert valid.amount_usd == 40


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "amount",
    [
        pytest.param(None, id="none"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
        pytest.param(-1, id="negative"),
        pytest.param("not-a-number", id="malformed-string"),
        pytest.param({}, id="mapping"),
        pytest.param([], id="list"),
        pytest.param(True, id="boolean"),
    ],
)
async def test_malformed_buy_becomes_zero_watch_without_aborting_valid_buy(environment, amount):
    malformed = idea("XOM", amount)
    valid = idea("MSFT", 40)
    result = await run(environment, [malformed, valid], ["XOM", "MSFT"])

    assert_watch(malformed)
    assert "XOM" not in result["allocations"]
    environment.optimizer.assert_called_once()
    assert environment.optimizer.call_args.args[2] == {"MSFT": 40}
    assert result["status"] == "optimized"
    assert valid.side == "buy"
    assert valid.amount_usd == 40


def test_incremental_additions_cannot_exceed_nav_less_existing_holdings():
    symbols = [f"S{i}" for i in range(10)]
    holdings = {symbol: 980.0 for symbol in symbols}
    caps = {symbol: 100.0 for symbol in symbols}
    result = optimize_incremental(
        price_panel(symbols),
        holdings,
        caps,
        {symbol: symbol for symbol in symbols},
        nav=10000,
        cash_budget=500,
    )

    assert result["status"] == "optimized"
    added = sum(result["allocations"].values())
    assert 0 < added <= 200
    assert sum(holdings.values()) + added <= 10000
    assert all(0 <= value <= caps[symbol] for symbol, value in result["allocations"].items())
    assert holdings == {symbol: 980.0 for symbol in symbols}
    assert caps == {symbol: 100.0 for symbol in symbols}


@pytest.mark.asyncio
async def test_bridge_limits_9800_holdings_to_200_additions_despite_500_buying_power(environment):
    symbols = [f"S{i}" for i in range(10)]
    environment.snapshot.buying_power = 500
    environment.holdings.extend(
        SimpleNamespace(symbol=symbol, market_value=980.0) for symbol in symbols
    )
    candidates = [idea(symbol, 100) for symbol in symbols]
    result = await run(environment, candidates, symbols)

    environment.optimizer.assert_called_once()
    assert environment.optimizer.call_args.args[4:6] == (10000, 500)
    assert result["status"] == "optimized"
    proposed_total = sum(candidate.amount_usd for candidate in candidates)
    assert 0 < proposed_total <= 200
    assert 9800 + proposed_total <= environment.snapshot.total_value
    assert sum(result["allocations"].values()) == pytest.approx(proposed_total)
    for candidate in candidates:
        assert 0 <= candidate.amount_usd <= 100
        if candidate.amount_usd == 0:
            assert_watch(candidate)
