from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlmodel import SQLModel, Session, create_engine

from app.services.stock_evidence import article_evidence, balanced_shortlist, news_evidence, risk_metrics
from app.services.llm_analysis import LLMAnalysisService
from app.services.market_research import MarketResearchService, StockIdea
from app.services.sec_research import SECResearchService
from app.models import Holding, Account, AccountSnapshot
from app.services.stock_advisor import StockAdvisorService


def test_generic_theme_is_not_a_buy_signal():
    assert article_evidence({"title": "AI cloud memory semiconductor demand"}) == (0, [], [])
    assert article_evidence({"title": "MSFT Stock Enters Bear Market After 21% Drop: Analysts Bullish on AI"})[0] < 0
    assert article_evidence({"title": "TSM stock price, news, quote and history"}) == (0, [], [])
    assert article_evidence({"title": "MU target lifted to $3000 on AI memory demand"}) == (0, [], [])


def test_core_symbols_have_sector_labels():
    advisor = StockAdvisorService()
    assert advisor.sector_for_symbol("AAPL") == "Tech / AI"
    assert advisor.sector_for_symbol("XLE") == "Energy / Nuclear"
    assert advisor.sector_for_symbol("PLD") == "Real Estate"
    assert advisor.sector_for_symbol("DUK") == "Utilities"


def test_news_deduplication_and_uncertainty():
    item = {"title": "XOM raises guidance"}
    assert news_evidence([item, item]) == news_evidence([item])
    assert article_evidence({"title": "XOM may win contract"})[0] == 0
    assert article_evidence({"title": "XOM raises guidance"})[0] > 0
    assert article_evidence({"title": "MSFT raises guidance"})[0] == article_evidence(item)[0]


def test_shortlist_covers_other_sectors_without_forcing_buys():
    rows = [(10-i, name, {}) for i, name in enumerate(["NVDA", "MSFT", "AMD", "XOM", "LLY", "PLD"])]
    sectors = {"NVDA": "tech", "MSFT": "tech", "AMD": "tech", "XOM": "energy", "LLY": "health", "PLD": "property"}
    assert [row[1] for row in balanced_shortlist(rows, 4, sectors.get)] == ["NVDA", "MSFT", "XOM", "LLY"]


def test_quant_metrics_require_actual_history():
    assert risk_metrics([{"close": 1}] * 20)["annualized_volatility_pct"] is None
    rows = [{"close": 100+i, "volume": 1_000_000} for i in range(200)]
    metrics = risk_metrics(rows)
    assert 0 < metrics["annualized_volatility_pct"] < 10
    assert metrics["max_drawdown_pct"] == 0
    rows[-1]["close"] = 100
    assert risk_metrics(rows)["max_drawdown_pct"] < -60
    service = MarketResearchService()
    assert service._history_metrics(rows[:80])["above_200d"] is None


def test_insider_filing_and_earnings_are_not_automatic_positive_signals():
    service = MarketResearchService()
    filing = {"form_type": "4", "filing_date": datetime.now(UTC).date().isoformat(), "signals": ["insider filing"], "signal_score": 4}
    assert service._catalyst_signal({"filings": [filing]}, 0, [], [], []) == (0, [], [])
    assert service._earnings_signal({"Earnings Date": [datetime.now(UTC).date().isoformat()]})[0] == 0
    signals = SECResearchService()._filing_signals(form_type="8-K", item_codes="2.02", description="AI backlog capacity", excerpt="")
    assert signals["score"] == 0


def test_quarters_exclude_ytd_and_deduplicate_restatements():
    facts = {"Revenues": {"units": {"USD": [
        {"start": "2025-04-01", "end": "2025-06-30", "filed": "2025-08-01", "form": "10-Q", "val": 100},
        {"start": "2026-01-01", "end": "2026-06-30", "filed": "2026-08-01", "form": "10-Q", "val": 999},
        {"start": "2026-04-01", "end": "2026-06-30", "filed": "2026-08-01", "form": "10-Q", "val": 120},
        {"start": "2026-04-01", "end": "2026-06-30", "filed": "2026-08-02", "form": "10-Q/A", "val": 125},
    ]}}}
    service = SECResearchService()
    series = service._quarterly_fact_series(facts, ["Revenues"], unit="USD")
    assert [row["value"] for row in series] == [125, 100]
    assert service._series_yoy_growth(series) == 25
    assert service._margin_from_series([{"start": "a", "end": "b", "value": 5}], [{"start": "c", "end": "d", "value": 10}]) is None


def packet():
    return {"symbol": "XOM", "asset_class": "stock", "quote_source": "yfinance", "currently_held": False,
            "composite_score": 3, "trend_score": 3, "fundamental_score": 3, "fundamental_reasons": ["revenue growth 20% YoY"],
            "trend_reasons": ["outperforming SPY"], "history_metrics": {"return_3m_pct": 15, "avg_dollar_volume_20d": 2_000_000}}


def test_fallback_never_claims_high_confidence_and_requires_evidence():
    service = LLMAnalysisService()
    decision = service._fallback_stock_decisions([packet()])[0]
    assert decision.action == "add" and decision.confidence == "medium"
    assert "revenue" in decision.thesis_summary
    for update in ({"quote_source": "stale_cache"}, {"fundamental_reasons": []}, {"history_metrics": {}}):
        assert service._fallback_stock_decisions([{**packet(), **update}])[0].action == "watch"


@pytest.mark.asyncio
async def test_quota_failure_is_visible_and_backed_off(monkeypatch):
    service = LLMAnalysisService()
    monkeypatch.setattr(LLMAnalysisService, "_stock_retry_after", None)
    monkeypatch.setattr(LLMAnalysisService, "_stock_cache", {})
    monkeypatch.setattr(service, "configured", lambda: True)
    response = httpx.Response(429, json={"error": {"details": [{"retryDelay": "80000s"}]}}, request=httpx.Request("POST", "https://example.test"))
    mock = AsyncMock(side_effect=httpx.HTTPStatusError("rate limited", request=response.request, response=response))
    service.gemini.generate_json = mock
    for _ in range(2):
        result = await service.analyze_stocks(portfolio_context={}, stock_packets=[packet()])
        assert result[0].confidence != "high"
    assert mock.await_count == 1
    assert service.stock_analysis_mode == "rules_only_quota_backoff"


@pytest.mark.asyncio
async def test_llm_validates_symbols_and_caches_success(monkeypatch):
    service = LLMAnalysisService()
    monkeypatch.setattr(LLMAnalysisService, "_stock_retry_after", None)
    monkeypatch.setattr(LLMAnalysisService, "_stock_cache", {})
    monkeypatch.setattr(service, "configured", lambda: True)
    mock = AsyncMock(return_value={"decisions": [{"symbol": "XOM", "action": "watch", "thesis_summary": "Wait"}]})
    service.gemini.generate_json = mock
    for _ in range(2):
        await service.analyze_stocks(portfolio_context={}, stock_packets=[packet()])
    assert mock.await_count == 1
    assert service.stock_analysis_mode == "gemini_cached"
    assert "output_schema" in mock.call_args.kwargs["user_payload"]


@pytest.mark.asyncio
async def test_quotes_try_fresh_fallback_before_stale_cache():
    service = MarketResearchService()
    service.cache = SimpleNamespace(get_json=lambda *a, **kw: None, get_json_stale=lambda *a: {"XOM": {"close": 1}}, set_json=lambda *a: None)
    service.market_data.get_quotes = AsyncMock(return_value={"XOM": {"status": "error"}})
    service.yfinance.get_quotes = AsyncMock(return_value={"XOM": {"symbol": "XOM", "close": 100, "previous_close": 99}})
    rows, source = await service._get_quote_payload(None, ["XOM"])
    assert source == "yfinance" and rows["XOM"]["close"] == 100
    service.yfinance.get_quotes = AsyncMock(side_effect=RuntimeError("down"))
    assert (await service._get_quote_payload(None, ["XOM"]))[1] == "stale_cache"


def test_sizing_never_increases_to_consume_cash(monkeypatch):
    service = MarketResearchService()
    monkeypatch.setattr(service, "_current_buying_power", lambda s: 2500)
    monkeypatch.setattr(service, "_current_total_value", lambda s: 50000)
    idea = StockIdea("XOM", "stock", "buy", 100, "evidence", "medium", 1)
    service._rebalance_add_sizes(None, [idea], {"XOM": packet()})
    assert 0 <= idea.amount_usd <= 100
    risky = {**packet(), "history_metrics": {"annualized_volatility_pct": 90}}
    calm = {**packet(), "history_metrics": {"annualized_volatility_pct": 20}}
    assert service._size_stock_decision("add", "small", risky, total_value=50000) < service._size_stock_decision("add", "small", calm, total_value=50000)


def test_holdings_snapshot_survives_sync_deletion():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        original = Holding(account_id="account", symbol="XOM", asset_type="stock", quantity=2, market_value=200)
        session.add(Account(id="account", external_id="fixture", provider="snaptrade", account_type="INDIVIDUAL", name="Main"))
        session.add(AccountSnapshot(account_id="account", total_value=1000))
        session.add(original)
        session.commit()
        service = MarketResearchService()
        service.settings = service.settings.model_copy(update={"primary_portfolio_account_external_id": "fixture"})
        snapshot = service._position_map(session)
        session.delete(original)
        session.commit()
        session.expire_all()
        assert snapshot["XOM"].market_value == 200
        assert snapshot["XOM"].quantity == 2


@pytest.mark.asyncio
async def test_stock_budget_is_persistent(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        service = MarketResearchService()
        service.settings = SimpleNamespace(stock_llm_daily_request_budget=1)
        monkeypatch.setattr(service, "_portfolio_context", lambda s: {})
        service.llm.stock_analysis_mode = "rules_only"
        service.llm.analyze_stocks = AsyncMock(return_value=service.llm._fallback_stock_decisions([packet()]))
        monkeypatch.setattr(LLMAnalysisService, "_stock_retry_after", None)
        await service._stock_decisions(session, [packet()])
        second = MarketResearchService()
        second.settings = service.settings
        monkeypatch.setattr(second, "_portfolio_context", lambda s: {})
        second.llm.analyze_stocks = AsyncMock(side_effect=AssertionError("must not call"))
        await second._stock_decisions(session, [packet()])
        assert second.llm.stock_analysis_mode == "rules_only_daily_budget"


@pytest.mark.asyncio
async def test_stock_pipeline_persists_evidence_and_keeps_bounded_sizes(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        service = MarketResearchService()
        monkeypatch.setattr(service, "_priority_stock_universe", lambda s: ["XOM", "LLY", "PLD"])
        monkeypatch.setattr(service, "_current_buying_power", lambda s: 2500)
        monkeypatch.setattr(service, "_current_total_value", lambda s: 50000)
        monkeypatch.setattr(service, "_holding_weight", lambda s, symbol: 0)
        monkeypatch.setattr(service.llm, "configured", lambda: False)
        service.quote_map = AsyncMock(return_value={symbol: {"close": 150, "previous_close": 149, "source_status": "yfinance"} for symbol in ["XOM", "LLY", "PLD"]})
        service._get_history = AsyncMock(return_value=[{"close": 100 * 1.003 ** i, "volume": 1_000_000} for i in range(253)])
        service._get_news_items = AsyncMock(return_value=[])
        service._get_calendar = AsyncMock(return_value={})
        service.sec.sync_symbol = AsyncMock(return_value={"fundamentals": {"trend_summary": {"revenue_growth_yoy_pct": 30.0, "revenue_acceleration_pct": 10.0, "eps_growth_yoy_pct": 30.0, "profitable_latest_quarter": True}}})
        packets, _, ideas = await service._analyze_stock_universe(session)
        assert len(packets) == 3 and len(ideas) == 3
        assert all(idea.confidence != "high" for idea in ideas)
        assert all(idea.amount_usd <= 500 for idea in ideas)
        latest = service.cache.get_json_stale(session, "stock_intelligence", "latest")
        assert latest["analysis_mode"] == "rules_only"
        assert len(latest["packets"]) == 3
        assert len(service.cache.get_json_stale(session, "stock_intelligence", "evaluation_samples")) == 1


@pytest.mark.asyncio
async def test_hidden_holds_do_not_crowd_out_visible_report():
    service = MarketResearchService()
    ideas = [StockIdea("NVDA", "stock", "hold", 0, "Hold", "low", 0),
             StockIdea("XOM", "stock", "watch", 0, "Watch", "medium", 0)]
    service._analyze_stock_universe = AsyncMock(return_value=([], {}, ideas))
    service._position_map = lambda session: {}
    visible, _ = await service.stock_report(None, stock_limit=1)
    assert [idea.symbol for idea in visible] == ["XOM"]
