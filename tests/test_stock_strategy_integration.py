"""Coordinator regressions with isolated SQL persistence and no external I/O."""

from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pandas as pd
import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import Account, AccountSnapshot, Holding, IntegrationState
from app.services import stock_strategy
from app.services.market_cache import MarketCacheService
from app.services.market_research import MarketResearchService, StockIdea


NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)
PRIMARY = "integration-primary"
OTHER = "integration-other"


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.replace(tzinfo=None) if tz is None else NOW.astimezone(tz)


def history(seed=1):
    dates = pd.bdate_range(end="2026-10-06", periods=252, tz="UTC")
    returns = np.random.default_rng(seed).normal(0.0003, 0.003, len(dates))
    prices = 100 * np.exp(np.cumsum(returns))
    return [{"datetime": stamp.isoformat(), "close": float(price)}
            for stamp, price in zip(dates, prices, strict=True)]


def idea(symbol="XOM", side="buy", amount=100.0, reason="Verified evidence"):
    return StockIdea(
        symbol=symbol, recommendation_type="stock", side=side, amount_usd=amount,
        reason=reason, risk="medium", change_pct=1.5, priority_score=8, confidence="high",
    )


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.setattr(stock_strategy, "datetime", FrozenDatetime)
    monkeypatch.setattr("app.services.market_research.datetime", FrozenDatetime)
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    settings = SimpleNamespace(
        stock_strategy_enabled=True,
        primary_portfolio_account_external_id="primary-external",
        max_total_prediction_bankroll_pct=0.2,
        minimum_cash_buffer_usd=500,
        max_single_stock_allocation_pct=0.12,
        stock_strategy_max_sector_weight=0.35,
        stock_strategy_max_new_weight=0.05,
        stock_strategy_max_volatility_increase=0.01,
        employment_excluded_symbols={"CSCO"},
    )
    market = SimpleNamespace(
        settings=settings, cache=MarketCacheService(),
        _get_history=AsyncMock(side_effect=AssertionError("unexpected history request")),
    )
    market._primary_account_snapshot = MethodType(
        MarketResearchService._primary_account_snapshot, market,
    )
    market._account_holding_count = MethodType(MarketResearchService._account_holding_count, market)
    histories = {"XOM": history(1), "MSFT": history(2), "CSCO": history(3)}
    benchmark = [{**row, "close": 100 + index * 0.1}
                 for index, row in enumerate(history())]
    try:
        with Session(engine) as session:
            session.add_all([
                Account(id=PRIMARY, external_id="primary-external", provider="snaptrade",
                        account_type="INDIVIDUAL", name="Primary test account"),
                Account(id=OTHER, external_id="other-external", provider="snaptrade",
                        account_type="INDIVIDUAL", name="Other test account"),
                AccountSnapshot(account_id=PRIMARY, total_value=100000, buying_power=10000,
                                as_of=NOW.replace(tzinfo=None)),
                AccountSnapshot(account_id=OTHER, total_value=200000, buying_power=50000,
                                as_of=(NOW + timedelta(minutes=1)).replace(tzinfo=None)),
                Holding(account_id=PRIMARY, symbol="XOM", quantity=10, market_value=1000),
                Holding(account_id=PRIMARY, symbol="XOM", quantity=2.5, market_value=250),
                Holding(account_id=OTHER, symbol="XOM", quantity=800, market_value=80000),
                Holding(account_id=OTHER, symbol="LLY", quantity=100, market_value=40000),
                Holding(account_id=PRIMARY, symbol="BTC", quantity=0.1,
                        market_value=9000, asset_type="crypto"),
            ])
            session.commit()
            yield SimpleNamespace(engine=engine, session=session, market=market,
                                  histories=histories, benchmark=benchmark)
    finally:
        engine.dispose()


async def run(environment, ideas, *, histories=None, benchmark=None):
    return await stock_strategy.apply_portfolio_strategy(
        environment.market, environment.session, ideas,
        environment.histories if histories is None else histories,
        environment.benchmark if benchmark is None else benchmark,
    )


def optimizer_stub(monkeypatch, allocations=None, *, status="optimized", error=None):
    optimizer = Mock(
        return_value={"status": status, "allocations": allocations or {}}, side_effect=error,
    )
    monkeypatch.setattr(stock_strategy, "optimize_incremental", optimizer)
    return optimizer


def persisted(environment):
    # A fresh session proves diagnostics were committed, not merely cached in memory.
    with Session(environment.engine) as session:
        rows = session.exec(select(IntegrationState).where(
            IntegrationState.provider == "stock_strategy", IntegrationState.key == "latest",
        )).all()
        assert len(rows) == 1
        return MarketCacheService().get_json_stale(session, "stock_strategy", "latest")


def assert_watch(candidate):
    assert candidate.side == "watch"
    assert candidate.amount_usd == 0
    assert candidate.confidence == "low"
    assert candidate.priority_score <= 3
    assert "No additional capital suggested" in candidate.reason


@pytest.mark.asyncio
async def test_primary_holdings_isolation_and_optimizer_inputs(environment, monkeypatch):
    optimizer = optimizer_stub(monkeypatch, {"XOM": 75, "MSFT": 50})
    result = await run(environment, [idea(), idea("MSFT")])
    optimizer.assert_called_once()
    panel, holdings, caps, sectors, nav, cash = optimizer.call_args.args
    assert holdings == {"XOM": 1250}
    assert caps == {"XOM": 100, "MSFT": 100}
    assert set(panel.columns) == {"XOM", "MSFT"}
    assert sectors == {"XOM": "Energy / Nuclear", "MSFT": "Tech / AI"}
    assert nav == 100000
    assert cash == 7500  # Prediction reserve and minimum cash buffer stay unallocated.
    assert optimizer.call_args.kwargs["deployment_scale"] == 1
    limits = optimizer.call_args.kwargs["limits"]
    assert limits.max_stock_weight == 0.12
    assert limits.max_sector_weight == 0.35
    assert limits.max_new_weight == 0.05
    assert limits.max_volatility_increase == 0.01
    environment.market._get_history.assert_not_awaited()
    assert result["account_scope"] == "primary_only"
    assert result["modeled_assets"] == "stocks_and_etfs_only"
    assert result["core_sales_allowed"] is False
    assert result["orders_enabled"] is False
    assert persisted(environment) == result


@pytest.mark.asyncio
async def test_disabled_flag_has_no_mutation_reads_or_diagnostic_overwrite(environment, monkeypatch):
    environment.market.settings.stock_strategy_enabled = False
    environment.market._primary_account_snapshot = Mock(side_effect=AssertionError("disabled read"))
    optimizer = optimizer_stub(monkeypatch)
    previous = {"status": "previous", "sentinel": "keep"}
    environment.market.cache.set_json(environment.session, "stock_strategy", "latest", previous)
    ideas = [idea(), idea("MSFT", "sell", 75), idea("CSCO", "hold", 0)]
    before = deepcopy(ideas)
    assert await run(environment, ideas) == {"status": "disabled"}
    assert ideas == before
    environment.market._primary_account_snapshot.assert_not_called()
    environment.market._get_history.assert_not_awaited()
    optimizer.assert_not_called()
    assert persisted(environment) == previous


@pytest.mark.asyncio
async def test_missing_snapshot_fails_closed_and_persists_diagnostics(environment, monkeypatch):
    for snapshot in environment.session.exec(select(AccountSnapshot)).all():
        environment.session.delete(snapshot)
    environment.session.commit()
    optimizer = optimizer_stub(monkeypatch)
    candidate = idea()
    result = await run(environment, [candidate])
    optimizer.assert_not_called()
    environment.market._get_history.assert_not_awaited()
    assert_watch(candidate)
    assert result["status"] == "unavailable"
    assert result["reason"] == "portfolio_risk_data_unavailable"
    assert result["allocations"] == {}
    assert result["orders_enabled"] is False
    assert persisted(environment) == result


@pytest.mark.asyncio
async def test_optimizer_exception_fails_closed_without_persisting_exception_text(environment, monkeypatch):
    optimizer_stub(monkeypatch, error=RuntimeError("private-provider-error"))
    candidate = idea()
    result = await run(environment, [candidate])
    assert_watch(candidate)
    assert result["status"] == "unavailable"
    assert result["reason"] == "portfolio_risk_data_unavailable"
    assert "private-provider-error" not in str(persisted(environment))
    assert persisted(environment) == result


@pytest.mark.asyncio
@pytest.mark.parametrize("allocation,expected", [(1000, 100), (100, 100), (42.5, 42.5), (10, 10)])
async def test_optimizer_never_increases_proposed_buy(environment, monkeypatch, allocation, expected):
    optimizer_stub(monkeypatch, {"XOM": allocation})
    candidate = idea()
    result = await run(environment, [candidate])
    assert candidate.side == "buy"
    assert candidate.amount_usd == expected
    assert candidate.amount_usd <= 100
    assert result["recommendation_adjustments"] == [
        {"symbol": "XOM", "before_usd": 100, "after_usd": expected},
    ]


@pytest.mark.asyncio
async def test_cent_rounding_never_increases_fractional_cent_proposal(environment, monkeypatch):
    optimizer_stub(monkeypatch, {"XOM": 1000})
    candidate = idea(amount=100.006)
    original = candidate.amount_usd
    await run(environment, [candidate])
    assert 0 <= candidate.amount_usd <= original


@pytest.mark.asyncio
@pytest.mark.parametrize("allocations", [{}, {"XOM": -1}, {"XOM": 0}, {"XOM": 9.99}])
async def test_unallocated_negative_or_tiny_buy_becomes_watch(environment, monkeypatch, allocations):
    optimizer_stub(monkeypatch, allocations)
    candidate = idea()
    result = await run(environment, [candidate])
    assert_watch(candidate)
    assert "concentration/cash limits" in candidate.reason
    assert result["orders_enabled"] is False
    stored = persisted(environment)
    assert stored == result
    assert stored["recommendation_adjustments"] == [
        {"symbol": "XOM", "before_usd": 100, "after_usd": candidate.amount_usd},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_sell_hold_watch_and_zero_buy_are_unchanged(environment, monkeypatch, fail):
    optimizer_stub(monkeypatch, {"XOM": 25, "MSFT": 500, "CSCO": 500},
                   error=RuntimeError("unavailable") if fail else None)
    untouched = [idea("MSFT", "sell", 75), idea("CSCO", "hold", 60),
                 idea("XOM", "watch", 30), idea("MSFT", "buy", 0)]
    before = [asdict(item) for item in untouched]
    result = await run(environment, [idea(), *untouched])
    assert [asdict(item) for item in untouched] == before
    assert [row["symbol"] for row in result["recommendation_adjustments"]] == ["XOM"]


@pytest.mark.asyncio
async def test_employment_exclusion_is_removed_before_optimizer(environment, monkeypatch):
    optimizer = optimizer_stub(monkeypatch, {"XOM": 50})
    excluded = idea("CSCO")
    result = await run(environment, [idea(), excluded])
    assert optimizer.call_args.args[2] == {"XOM": 100}
    assert_watch(excluded)
    assert result["recommendation_adjustments"][1]["after_usd"] == 0
    assert persisted(environment) == result


@pytest.mark.asyncio
async def test_real_optimizer_success_exclusion_and_persisted_diagnostics(environment):
    candidates = [idea(), idea("MSFT"), idea("CSCO")]
    untouched = idea("XOM", "hold", 1250)
    before = asdict(untouched)
    result = await run(environment, [*candidates, untouched])
    assert result["status"] == "optimized"
    for candidate in candidates[:2]:
        assert candidate.side == "buy"
        assert 10 <= candidate.amount_usd <= 100
    assert_watch(candidates[2])
    assert "CSCO" not in result["allocations"]
    assert asdict(untouched) == before
    assert result["coverage"] == 1
    assert result["observations"] == 252
    assert len(result["data_fingerprint"]) == 64
    assert result["risk_before"]["annualized_volatility_pct"] >= 0
    assert result["risk_after"]["annualized_volatility_pct"] >= 0
    assert result["sector_weights_before"] == {"Energy / Nuclear": 0.0125}
    assert result["regime"]["status"] == "normal"
    assert result["excluded_history"] == {}
    assert result["as_of"] == NOW.isoformat()
    assert result["portfolio_as_of"] == str(NOW.replace(tzinfo=None))
    assert result["account_scope"] == "primary_only"
    assert result["orders_enabled"] is False
    assert result["core_sales_allowed"] is False
    assert persisted(environment) == result
    environment.market._get_history.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("fetch_error", [False, True])
async def test_real_missing_material_holding_history_fails_closed(environment, fetch_error):
    environment.session.add(Holding(account_id=PRIMARY, symbol="MSFT", quantity=100,
                                    market_value=10000))
    environment.session.commit()
    environment.market._get_history = AsyncMock(
        return_value=[], side_effect=RuntimeError("provider unavailable") if fetch_error else None,
    )
    candidate = idea()
    result = await run(environment, [candidate], histories={"XOM": environment.histories["XOM"]})
    environment.market._get_history.assert_awaited_once_with(environment.session, "MSFT")
    assert_watch(candidate)
    assert result["status"] == "blocked"
    assert result["reason"] == "incomplete_holdings_risk_coverage"
    assert result["excluded_history"] == {"MSFT": "insufficient_history"}
    assert result["allocations"] == {"XOM": 0}
    assert result["orders_enabled"] is False
    assert persisted(environment) == result


@pytest.mark.asyncio
async def test_real_empty_histories_fail_closed_and_replace_previous_diagnostics(environment):
    await run(environment, [idea()])
    assert persisted(environment)["status"] == "optimized"
    environment.market._get_history = AsyncMock(return_value=[])
    candidate = idea()
    result = await run(environment, [candidate], histories={}, benchmark=[])
    assert_watch(candidate)
    assert result["status"] == "blocked"
    assert result["reason"] == "insufficient_risk_history"
    assert result["regime"]["status"] == "unknown"
    assert result["excluded_history"] == {"XOM": "insufficient_history"}
    assert persisted(environment) == result


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["Verified evidence", "Verified evidence Risk watch: existing concern"])
async def test_reduced_buy_explanation_preserves_existing_diagnostics(environment, monkeypatch, reason):
    optimizer_stub(monkeypatch, {"XOM": 40})
    candidate = idea(reason=reason)
    result = await run(environment, [candidate])
    assert candidate.reason.startswith("Verified evidence")
    assert candidate.reason.count("Risk watch:") == 1
    assert "portfolio correlation, sector exposure and cash-risk limits" in candidate.reason
    if "existing concern" in reason:
        assert "existing concern" in candidate.reason
    assert persisted(environment)["recommendation_adjustments"] == [
        {"symbol": "XOM", "before_usd": 100, "after_usd": 40},
    ]
    assert result["orders_enabled"] is False
