"""Synthetic cache/quotas regressions; no adapters, credentials, or network calls."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from app.services.llm_analysis import LLMAnalysisService, StockDecisionModel
from app.services.market_research import MarketResearchService
from app.services.stock_analysis_cache import (
    analysis_fingerprint,
    prices_within_reuse_window,
)


class JsonCache:
    """Share serialized state across service instances, with explicit TTL semantics."""

    def __init__(self, clock):
        self.clock = clock
        self.rows = {}
        self.reads = []
        self.writes = []

    def get_json(self, session, provider, key, *, max_age_seconds):
        self.reads.append((provider, key, max_age_seconds))
        record = self.rows.get((provider, key))
        if record is None:
            return None
        written_at, raw = record
        if (self.clock.now - written_at).total_seconds() > max_age_seconds:
            return None
        return json.loads(raw)

    def get_json_stale(self, session, provider, key):
        record = self.rows.get((provider, key))
        return json.loads(record[1]) if record else None

    def set_json(self, session, provider, key, value):
        self.rows[provider, key] = (self.clock.now, json.dumps(value))
        self.writes.append((provider, key))


@pytest.fixture
def harness(monkeypatch):
    clock = SimpleNamespace(now=datetime(2026, 10, 14, 14, tzinfo=UTC))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now.astimezone(tz) if tz else clock.now.replace(tzinfo=None)

    monkeypatch.setattr("app.services.market_research.datetime", Clock)
    context = {
        "account_scope": "synthetic_primary",
        "account_id": "synthetic-account-a",
        "buying_power": 500.25,
        "total_value": 2500.25,
        "minimum_cash_buffer_usd": 100.0,
        "max_single_stock_allocation_pct": 10.0,
        "top_holdings": [{"symbol": "SYNTH", "quantity": 2.0, "market_value": 200.0}],
    }
    packets = [{
        "symbol": "SYNTH", "asset_class": "stock", "close": 100.0,
        "previous_close": 99.0, "momentum_pct": 1.0, "quote_source": "yfinance",
        "currently_held": True, "holding_value": 200.0, "holding_weight": 0.08,
        "composite_score": 1.0, "trend_score": 0.5, "fundamental_score": 0.5,
        "catalyst_score": 0.0,
        "fundamental_reasons": ["Synthetic revenue growth: 4%"],
        "fundamentals": {"revenue_growth_pct": 4.0},
        "history_metrics": {"return_3m_pct": 2.0, "avg_dollar_volume_20d": 2000000.0},
        "news_headlines": [{"title": "Synthetic operating update",
                            "url": "https://example.com/synthetic-update"}],
        "filing_signals": ["Synthetic annual filing"],
        "filings": [{"form": "10-K", "accession": "synthetic-filing-a"}],
        "risk_flags": [],
    }]
    settings = SimpleNamespace(gemini_model_analyst="test-model", stock_llm_daily_request_budget=12)
    cache = JsonCache(clock)

    def make_market(*, configured=True):
        # Bypass constructors so no configured adapters or runtime credentials are read.
        market = MarketResearchService.__new__(MarketResearchService)
        market.settings = settings
        market.cache = cache
        market.local_tz = ZoneInfo("America/New_York")
        market._portfolio_context = lambda _: deepcopy(context)
        model = LLMAnalysisService.__new__(LLMAnalysisService)
        model.stock_analysis_mode = "not_run"
        model._stock_retry_after = None
        model.configured = Mock(return_value=configured)

        async def analyze_stocks(*, portfolio_context, stock_packets):
            model.stock_analysis_mode = "gemini"
            return [StockDecisionModel(symbol=p["symbol"], action="watch",
                                       thesis_summary="Synthetic model result")
                    for p in stock_packets]

        model.analyze_stocks = AsyncMock(side_effect=analyze_stocks)
        market.llm = model
        return market

    return SimpleNamespace(clock=clock, context=context, packets=packets,
                           settings=settings, cache=cache, make_market=make_market)


def state(harness, key):
    return harness.cache.get_json_stale(None, "stock_intelligence", key)


def put(harness, key, value):
    harness.cache.set_json(None, "stock_intelligence", key, value)


def seed_result(harness):
    put(harness, "llm_result", {
        "fingerprint": analysis_fingerprint(harness.context, harness.packets,
                                            harness.settings.gemini_model_analyst),
        "prices": {p["symbol"]: p["close"] for p in harness.packets},
        "decisions": [{"symbol": "SYNTH", "action": "watch",
                       "thesis_summary": "Synthetic cached result"}],
    })


def seed_health(harness, used):
    health = {"budget_day": harness.clock.now.date().isoformat(),
              "requests_today": used, "requests_offhours": 0,
              "last_request_at": None, "retry_after": None}
    put(harness, "llm_health", health)
    return health


def assert_scan(harness, mode, used):
    scan = state(harness, "llm_scan")
    assert scan["mode"] == mode
    assert scan["requests_today"] == used
    assert scan["budget_day"] == harness.clock.now.date().isoformat()
    assert scan["budget_timezone"] == "UTC"
    assert scan["checked_at"] == harness.clock.now.isoformat()
    assert scan["request_limit"] == harness.settings.stock_llm_daily_request_budget


@pytest.mark.asyncio
@pytest.mark.parametrize("price", [99.75, 100.0, 100.25])
async def test_identical_evidence_small_price_move_reuses_across_instances(harness, price):
    first = harness.make_market()
    expected = await first._stock_decisions(None, harness.packets)
    first.llm.analyze_stocks.assert_awaited_once()
    health = state(harness, "llm_health")
    result = state(harness, "llm_result")
    harness.clock.now += timedelta(minutes=50)
    harness.packets[0].update(close=price, momentum_pct=1.25,
                              holding_value=price * 2, holding_weight=0.0801)
    harness.context["total_value"] += (price - 100) * 2
    harness.context["top_holdings"][0]["market_value"] = price * 2
    writes_before = len(harness.cache.writes)
    restarted = harness.make_market()
    actual = await restarted._stock_decisions(None, harness.packets)
    assert actual == expected
    assert actual[0] is not expected[0]
    restarted.llm.analyze_stocks.assert_not_awaited()
    assert state(harness, "llm_health") == health
    assert state(harness, "llm_result") == result
    assert harness.cache.writes[writes_before:] == [("stock_intelligence", "llm_scan")]
    assert ("stock_intelligence", "llm_result", 3600) in harness.cache.reads
    assert_scan(harness, "gemini_cached", 1)


def change_evidence(harness, change):
    packet = harness.packets[0]
    if change == "price":
        packet["close"] = 100.51
    elif change == "filing":
        packet["filings"].append({"form": "8-K", "accession": "synthetic-filing-b"})
    elif change == "fundamentals_same_score":
        packet["fundamentals"]["revenue_growth_pct"] = -4.0
    elif change == "previous_close":
        packet["previous_close"] = 98.0
    elif change == "account":
        harness.context["account_id"] = "synthetic-account-b"
    elif change == "account_scope":
        harness.context["account_scope"] = "synthetic_other_scope"
    elif change == "cash":
        harness.context["buying_power"] += 0.01
    elif change == "ownership":
        packet["currently_held"] = False
    elif change == "quantity":
        harness.context["top_holdings"][0]["quantity"] = 3.0
    elif change == "weight_band":
        packet["holding_weight"] = 0.10
    elif change == "model":
        harness.settings.gemini_model_analyst = "test-model-other"
    else:
        raise AssertionError(change)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "price", "filing", "fundamentals_same_score", "previous_close", "account",
    "account_scope", "cash", "ownership", "quantity", "weight_band", "model",
])
async def test_material_change_invalidates_and_reserves_one_request(harness, change):
    seed_result(harness)
    seed_health(harness, 3)
    original_fingerprint = state(harness, "llm_result")["fingerprint"]
    change_evidence(harness, change)
    fingerprint = analysis_fingerprint(harness.context, harness.packets,
                                       harness.settings.gemini_model_analyst)
    assert (fingerprint == original_fingerprint) is (change == "price")
    assert harness.packets[0]["composite_score"] == 1.0
    market = harness.make_market()
    decisions = await market._stock_decisions(None, harness.packets)
    market.llm.analyze_stocks.assert_awaited_once_with(
        portfolio_context=harness.context, stock_packets=harness.packets)
    assert decisions[0].thesis_summary == "Synthetic model result"
    assert state(harness, "llm_health")["requests_today"] == 4
    assert state(harness, "llm_result")["fingerprint"] == fingerprint
    assert_scan(harness, "gemini", 4)


@pytest.mark.asyncio
@pytest.mark.parametrize("age_seconds,expected_calls", [(3599, 0), (3601, 1)])
async def test_persisted_result_has_one_hour_ttl(harness, age_seconds, expected_calls):
    seed_result(harness)
    seed_health(harness, 3)
    harness.clock.now += timedelta(seconds=age_seconds)
    market = harness.make_market()
    await market._stock_decisions(None, harness.packets)
    assert market.llm.analyze_stocks.await_count == expected_calls
    assert state(harness, "llm_health")["requests_today"] == 3 + expected_calls
    assert_scan(harness, "gemini" if expected_calls else "gemini_cached", 3 + expected_calls)


@pytest.mark.parametrize("price", [99.5, 100.5])
def test_price_window_includes_exact_half_percent_boundary(harness, price):
    harness.packets[0]["close"] = price
    assert prices_within_reuse_window(harness.packets, {"SYNTH": 100.0})


@pytest.mark.parametrize("price", [99.49, 100.51])
def test_price_window_rejects_moves_outside_half_percent(harness, price):
    harness.packets[0]["close"] = price
    assert not prices_within_reuse_window(harness.packets, {"SYNTH": 100.0})


@pytest.mark.parametrize("source", [None, "unknown", "stale_cache", "live", "arbitrary_source"])
def test_stale_or_missing_quote_source_rejects_reuse(harness, source):
    harness.packets[0]["quote_source"] = source
    assert not prices_within_reuse_window(harness.packets, {"SYNTH": 100.0})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"),
                                  0.0, -1.0, None, "invalid"])
@pytest.mark.parametrize("which", ["current", "cached"])
def test_invalid_current_or_cached_price_rejects_reuse(harness, value, which):
    previous = {"SYNTH": 100.0}
    if which == "current":
        harness.packets[0]["close"] = value
    else:
        previous["SYNTH"] = value
    assert not prices_within_reuse_window(harness.packets, previous)


@pytest.mark.asyncio
@pytest.mark.parametrize("prices", [None, {}, {"OTHER": 100.0}])
async def test_missing_or_mismatched_cached_price_metadata_falls_back(harness, prices):
    seed_result(harness)
    cached = state(harness, "llm_result")
    if prices is None:
        cached.pop("prices")
    else:
        cached["prices"] = prices
    put(harness, "llm_result", cached)
    health = seed_health(harness, harness.settings.stock_llm_daily_request_budget)
    market = harness.make_market()
    actual = await market._stock_decisions(None, harness.packets)
    assert actual == market.llm._fallback_stock_decisions(harness.packets)
    market.llm.analyze_stocks.assert_not_awaited()
    assert state(harness, "llm_health") == health
    assert_scan(harness, "rules_only_daily_budget", 12)


@pytest.mark.asyncio
@pytest.mark.parametrize("decisions", [
    None, [], "not-a-list", [{}],
    [{"symbol": "SYNTH", "action": "invalid", "thesis_summary": "Synthetic"}],
    [{"symbol": "OTHER", "action": "watch", "thesis_summary": "Synthetic"}],
    [{"symbol": "SYNTH", "action": "watch", "thesis_summary": "Synthetic"}] * 2,
])
async def test_corrupt_cached_decisions_fail_safely_at_exhausted_budget(harness, decisions):
    seed_result(harness)
    cached = state(harness, "llm_result")
    cached["decisions"] = decisions
    put(harness, "llm_result", cached)
    health = seed_health(harness, 12)
    market = harness.make_market()
    actual = await market._stock_decisions(None, harness.packets)
    assert actual == market.llm._fallback_stock_decisions(harness.packets)
    market.llm.analyze_stocks.assert_not_awaited()
    assert state(harness, "llm_health") == health
    assert_scan(harness, "rules_only_daily_budget", 12)


@pytest.mark.asyncio
async def test_missing_decisions_key_can_regenerate_with_budget(harness):
    seed_result(harness)
    cached = state(harness, "llm_result")
    cached.pop("decisions")
    put(harness, "llm_result", cached)
    seed_health(harness, 3)
    market = harness.make_market()
    result = await market._stock_decisions(None, harness.packets)
    assert result[0].thesis_summary == "Synthetic model result"
    market.llm.analyze_stocks.assert_awaited_once()
    assert_scan(harness, "gemini", 4)


@pytest.mark.asyncio
async def test_budget_exhaustion_returns_real_deterministic_decisions_without_llm(harness):
    health = seed_health(harness, 12)
    market = harness.make_market()
    actual = await market._stock_decisions(None, harness.packets)
    assert actual == market.llm._fallback_stock_decisions(harness.packets)
    assert actual[0].action == "hold"
    market.llm.analyze_stocks.assert_not_awaited()
    assert state(harness, "llm_health") == health
    assert state(harness, "llm_result") is None
    assert_scan(harness, "rules_only_daily_budget", 12)
    assert state(harness, "llm_scan")["next_request_at"] == "2026-10-15T00:00:00+00:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("configured,empty", [(True, True), (False, True), (False, False)])
@pytest.mark.parametrize("used", [0, 3])
async def test_empty_or_unconfigured_consumes_no_quota(harness, configured, empty, used):
    health = seed_health(harness, used) if used else None
    seed_result(harness)
    market = harness.make_market(configured=configured)
    packets = [] if empty else harness.packets
    actual = await market._stock_decisions(None, packets)
    assert actual == ([] if empty else market.llm._fallback_stock_decisions(packets))
    market.llm.analyze_stocks.assert_not_awaited()
    assert state(harness, "llm_health") == health
    assert_scan(harness, "rules_only_no_candidates" if empty else "rules_only_not_configured", used)
    assert state(harness, "llm_scan")["packet_count"] == len(packets)


@pytest.mark.asyncio
@pytest.mark.parametrize("before,after,reset", [
    ("2026-03-08T06:30:00+00:00", "2026-03-08T07:30:00+00:00", False),
    ("2026-11-01T05:30:00+00:00", "2026-11-01T06:30:00+00:00", False),
    ("2026-10-15T03:30:00+00:00", "2026-10-15T04:30:00+00:00", False),
    ("2026-07-01T23:30:00+00:00", "2026-07-02T00:30:00+00:00", True),
    ("2026-12-01T23:30:00+00:00", "2026-12-02T00:30:00+00:00", True),
], ids=["spring-forward", "fall-back-fold", "local-midnight", "utc-midnight-edt",
        "utc-midnight-est"])
async def test_budget_day_is_utc_across_dst_and_local_midnight(harness, before, after, reset):
    harness.clock.now = datetime.fromisoformat(before)
    health = seed_health(harness, 12)
    health["requests_offhours"] = 2
    put(harness, "llm_health", health)
    harness.clock.now = datetime.fromisoformat(after)
    market = harness.make_market()
    actual = await market._stock_decisions(None, harness.packets)
    assert market.llm.analyze_stocks.await_count == int(reset)
    if reset:
        assert actual[0].thesis_summary == "Synthetic model result"
        assert state(harness, "llm_health")["budget_day"] == harness.clock.now.date().isoformat()
        assert state(harness, "llm_health")["requests_today"] == 1
        assert state(harness, "llm_health")["requests_offhours"] == 1
        assert_scan(harness, "gemini", 1)
    else:
        assert actual == market.llm._fallback_stock_decisions(harness.packets)
        assert state(harness, "llm_health") == health
        reset_at = datetime.combine(harness.clock.now.date() + timedelta(days=1),
                                    datetime.min.time(), tzinfo=UTC)
        assert state(harness, "llm_scan")["next_request_at"] == reset_at.isoformat()
        assert_scan(harness, "rules_only_daily_budget", 12)
