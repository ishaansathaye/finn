from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.llm_analysis import LLMAnalysisService
from app.services.market_research import MarketResearchService, StockIdea
from app.services.agentic_observer import AgenticObserverService


def test_relative_underperformance_is_not_automatic_exit():
    decision = LLMAnalysisService()._fallback_stock_decisions([{
        "symbol": "RKLB", "currently_held": True, "holding_weight": .003,
        "composite_score": -2, "trend_score": -3, "fundamental_score": .1,
        "risk_flags": ["lagging SPY over 3M", "lagging SPY over 6M"],
    }])[0]
    assert decision.action == "hold"


@pytest.mark.asyncio
async def test_final_report_drops_sell_if_position_closed_during_research():
    market = MarketResearchService()
    market._analyze_stock_universe = AsyncMock(return_value=([], {}, [StockIdea("RKLB", "stock", "sell", 120, "risk", "high", -2)]))
    market._position_map = lambda _: {}
    ideas, _ = await market.stock_report(None)
    assert ideas == []


@pytest.mark.asyncio
async def test_llm_cache_includes_account_context(monkeypatch):
    model = LLMAnalysisService()
    monkeypatch.setattr(LLMAnalysisService, "_stock_cache", {})
    monkeypatch.setattr(LLMAnalysisService, "_stock_retry_after", None)
    monkeypatch.setattr(model, "configured", lambda: True)
    model.gemini.generate_json = AsyncMock(return_value={"decisions": [{"symbol": "XOM", "action": "watch", "thesis_summary": "Wait"}]})
    for account in ("main", "agent"):
        await model.analyze_stocks(portfolio_context={"account_id": account}, stock_packets=[{"symbol":"XOM"}])
    assert model.gemini.generate_json.await_count == 2
    assert model.gemini.generate_json.call_args.kwargs["thinking_budget"] == 1024
    assert model.gemini.generate_json.call_args.kwargs["timeout_seconds"] == 120


@pytest.mark.asyncio
@pytest.mark.parametrize("hour,used_offhours,last_request,expected_calls", [
    (14, 2, None, 1), (2, 2, None, 0), (14, 0, "2026-10-14T14:00:00+00:00", 0),
    (2, 0, None, 1),
])
async def test_llm_paces_and_reserves_market_budget(monkeypatch, hour, used_offhours, last_request, expected_calls):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 14, hour, 10, tzinfo=UTC)
    monkeypatch.setattr("app.services.market_research.datetime", Clock)
    market = MarketResearchService()
    market._portfolio_context = lambda _: {}
    state = {"llm_health": {"budget_day": "2026-10-14", "requests_today": 2,
                             "requests_offhours": used_offhours, "last_request_at": last_request}}
    market.cache = SimpleNamespace(get_json=lambda *a, **kw: None,
        get_json_stale=lambda s,p,k: state.get(k), set_json=lambda s,p,k,v: state.update({k:v}))
    market.llm = SimpleNamespace(stock_analysis_mode="gemini", _stock_retry_after=None,
        configured=lambda: True, analyze_stocks=AsyncMock(return_value=[]), _fallback_stock_decisions=lambda p: [])
    await market._stock_decisions(None, [{"symbol": "XOM"}])
    assert market.llm.analyze_stocks.await_count == expected_calls


def test_observer_distinguishes_service_heartbeat_from_account_age():
    text = AgenticObserverService().format_snapshot({"as_of": "2026-10-15T05:00:00Z",
        "account_as_of": "2026-10-14T19:00:00Z", "positions_reconciliation_required": True})
    assert "Account last observed:" in text
    assert "may be pre-trade" in text
    assert "Last observed open orders: unavailable" in text
