from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import IntegrationState, Recommendation, RecommendationBatch, StockListenerCheckpoint
from app.services import stock_alert_diagnostics as diagnostics
from app.services.market_research import StockIdea
from app.services.recommendation_service import RecommendationService
from app.services.stock_advisor import StockAdvisorService


CLOSE = datetime(2026, 10, 15, 20, 15, tzinfo=UTC)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as value:
        yield value
    engine.dispose()


def advisor_at(now=CLOSE, ideas=None, mode="rules_only_daily_budget"):
    advisor = StockAdvisorService.__new__(StockAdvisorService)
    settings = SimpleNamespace(
        stock_proactive_idea_limit=5, stock_llm_daily_request_budget=12,
        stock_alert_cooldown_minutes=240, stock_proactive_min_priority=5,
        stock_after_hours_exception_min_priority=8,
        stock_after_hours_exception_min_buy_amount_usd=50,
        core_stock_symbols="VTI", core_etf_validity_hours=72,
        earnings_trade_validity_minutes=90,
    )
    market = SimpleNamespace(
        settings=settings, llm=SimpleNamespace(stock_analysis_mode="gemini"),
        # No cache API or snapshot dependency: existing fake markets must still work.
        cache=object(), quote_map=AsyncMock(return_value={}),
    )

    async def report(*args, **kwargs):
        market.llm.stock_analysis_mode = mode
        return list(ideas or []), []

    market.stock_report = AsyncMock(side_effect=report)
    advisor.market_research = market
    advisor.settings = settings
    advisor._now = lambda: now.replace(tzinfo=None)
    advisor._local_now = lambda: now.astimezone(diagnostics.PACIFIC)
    advisor.refresh_always_on_state = AsyncMock(return_value={})
    return advisor


def scans(session):
    return [json.loads(row.value) for row in session.exec(
        select(IntegrationState).where(
            IntegrationState.provider == diagnostics.PROVIDER,
            IntegrationState.key.startswith("scan:"),
        )
    ).all()]


def store(session, key, value):
    session.add(IntegrationState(
        provider="stock_intelligence", key=key, value=json.dumps(value),
    ))
    session.commit()


def idea(side="watch", *, confidence="medium", priority=6, amount=100):
    return StockIdea("FIXTURE", "stock", side, amount, "fixture", "medium", 0,
                     priority_score=priority, confidence=confidence)


@pytest.mark.asyncio
async def test_empty_scan_persists_without_snapshot_and_status_has_no_recommendation(session):
    advisor = advisor_at()
    batch = await advisor.create_proactive_stock_batch(session)
    assert batch.batch_type == "stock-status" and batch.sent_at is None
    assert session.exec(select(Recommendation)).all() == []
    record = scans(session)[0]
    assert record["status"] == "completed"
    assert record["analysis_mode"] == "rules_only_daily_budget"
    assert record["reported_ideas"] == record["eligible_ideas"] == 0
    text = diagnostics.status_messages(session, batch, CLOSE)[0]
    assert "daily AI allowance reached" in text
    assert "quote coverage unknown" in text
    assert "no trade suggested" in text
    advisor.market_research.quote_map.assert_not_awaited()


@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 10, 15, 19, 59, tzinfo=UTC), False),
    (datetime(2026, 10, 15, 20, 0, tzinfo=UTC), True),
    (datetime(2026, 10, 15, 20, 59, tzinfo=UTC), True),
    (datetime(2026, 10, 15, 21, 0, tzinfo=UTC), False),
    (datetime(2026, 10, 17, 20, 15, tzinfo=UTC), False),
    (datetime(2026, 11, 2, 20, 15, tzinfo=UTC), False),
    (datetime(2026, 11, 2, 21, 15, tzinfo=UTC), True),
    # Weekday-only policy intentionally includes holidays; no exchange calendar.
    (datetime(2026, 12, 25, 21, 15, tzinfo=UTC), True),
])
def test_weekday_window_observes_eastern_dst(session, now, expected):
    assert diagnostics.status_due(session, now) is expected


@pytest.mark.asyncio
async def test_failed_delivery_reuses_pending_batch_then_sent_at_stops_retries(session):
    advisor = advisor_at()
    first = await advisor.create_proactive_stock_batch(session)
    second = await advisor.create_proactive_stock_batch(session)
    assert first.id == second.id
    assert len(session.exec(select(RecommendationBatch)).all()) == 1
    first.sent_at = first.created_at  # Existing poller's acknowledgment contract.
    session.add(first)
    session.commit()
    assert await advisor.create_proactive_stock_batch(session) is None
    assert len(scans(session)) == 3
    assert diagnostics.status_messages(session, first, CLOSE) == []


@pytest.mark.parametrize("kind,sent_at,expected", [
    ("stock-alert", CLOSE.replace(tzinfo=None), False),
    ("stock-alert", None, True),
    ("stock-report", CLOSE.replace(tzinfo=None), True),
    ("event-report", CLOSE.replace(tzinfo=None), True),
    ("stock-alert", datetime(2026, 10, 15, 6, 59), True),
    ("stock-alert", datetime(2026, 10, 15, 7, 0), False),
])
def test_only_sent_stock_alert_today_suppresses_status(session, kind, sent_at, expected):
    session.add(RecommendationBatch(batch_type=kind, sent_at=sent_at))
    session.commit()
    assert diagnostics.status_due(session, CLOSE) is expected


@pytest.mark.asyncio
async def test_pending_status_expiry_and_alert_race_are_rechecked_at_format(session):
    batch = await advisor_at().create_proactive_stock_batch(session)
    assert diagnostics.status_messages(session, batch, CLOSE + timedelta(hours=1)) == []
    assert diagnostics.status_messages(session, batch, CLOSE + timedelta(days=1)) == []
    session.add(RecommendationBatch(batch_type="stock-alert", sent_at=CLOSE.replace(tzinfo=None)))
    session.commit()
    assert diagnostics.status_messages(session, batch, CLOSE) == []


@pytest.mark.asyncio
async def test_latest_batch_excludes_status_but_reply_to_status_resolves_nothing(session):
    previous = RecommendationBatch(batch_type="stock-report", created_at=datetime(2026, 10, 15, 10))
    session.add(previous)
    session.commit()
    batch = await advisor_at().create_proactive_stock_batch(session)
    service = RecommendationService.__new__(RecommendationService)
    assert service.latest_batch(session).id == previous.id
    assert service.resolve_item_numbers(session, ["all"], preferred_batch_id=batch.id) == []


@pytest.mark.asyncio
async def test_formatter_short_circuits_without_portfolio_or_quote_calls(session, monkeypatch):
    batch = await advisor_at().create_proactive_stock_batch(session)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return CLOSE

    monkeypatch.setattr("app.services.recommendation_service.datetime", Clock)
    service = RecommendationService.__new__(RecommendationService)
    service._batch_context = AsyncMock(side_effect=AssertionError("must not fetch data"))
    messages = await service.format_batch_messages(session, batch.id)
    assert len(messages) == 1 and "Informational only" in messages[0]
    service._batch_context.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_alert_precedes_status_and_filter_behavior_is_unchanged(session):
    advisor = advisor_at(ideas=[idea("buy", confidence="high", priority=9)])
    batch = await advisor.create_proactive_stock_batch(session)
    assert batch.batch_type == "stock-alert"
    record = scans(session)[0]
    assert record["eligible_ideas"] == 1 and record["alert_batch_created"]
    assert len(session.exec(select(Recommendation)).all()) == 1


@pytest.mark.asyncio
async def test_filter_reason_counts_and_mode_do_not_come_from_old_health(session):
    store(session, "llm_health", {"mode": "gemini", "budget_day": "2026-10-15",
                                "requests_today": 12})
    advisor = advisor_at(ideas=[idea(), idea("sell"), idea("buy", priority=1)])
    batch = await advisor.create_proactive_stock_batch(session)
    record = scans(session)[0]
    assert record["notification_rejections"] == {
        "hold_watch": 1, "after_hours": 1, "action_threshold": 1,
    }
    assert record["model_budget"]["requests_today"] == 12
    assert record["model_budget"]["timezone"] == "UTC"
    assert "rules-based" in diagnostics.status_messages(session, batch, CLOSE)[0]


@pytest.mark.asyncio
async def test_current_llm_scan_contract_and_known_quote_cache_status(session):
    store(session, "llm_scan", {
        "checked_at": CLOSE.isoformat(), "mode": "rules_only_request_spacing",
        "budget_day": "2026-10-15", "requests_today": 4, "request_limit": 10,
        "next_request_at": (CLOSE + timedelta(minutes=30)).isoformat(),
    })
    store(session, "latest", {
        "as_of": CLOSE.isoformat(),
        "packets": [{"quote_source": "cache"}, {"quote_source": "twelve_data"},
                    {"quote_source": "yfinance"}, {"quote_source": "stale_cache"}],
        "ideas": [{"side": "watch"}],
        "portfolio_strategy": {"candidate_rejections": {"FIXTURE": "above the sector limit"}},
    })
    session.add(StockListenerCheckpoint(
        provider="stock_global_news", key="fmp",
        cursor=json.dumps({"status": "blocked", "reason": "authentication_or_entitlement"}),
        last_polled_at=CLOSE.replace(tzinfo=None),
    ))
    session.commit()
    batch = await advisor_at(mode="rules_only_request_spacing").create_proactive_stock_batch(session)
    record = scans(session)[0]
    assert record["model_budget"]["limit"] == 10
    assert record["quote_sources"]["cache"] == 1
    assert record["strategy_rejections"] == {"sector_limit": 1}
    text = diagnostics.status_messages(session, batch, CLOSE)[0]
    assert "Budget 4/10" in text and "fmp blocked" in text
    assert "1 stale, 0 unknown" in text
    assert len(text.splitlines()) <= 9


@pytest.mark.asyncio
async def test_stale_snapshots_not_used_for_empty_scan(session):
    store(session, "latest", {
        "as_of": (CLOSE - timedelta(hours=1)).isoformat(),
        "analysis_mode": "gemini", "packets": [{"quote_source": "yfinance"}],
    })
    store(session, "llm_scan", {
        "checked_at": (CLOSE - timedelta(hours=1)).isoformat(),
        "requests_today": 99, "request_limit": 100,
    })
    advisor = advisor_at()
    advisor.market_research.stock_report = AsyncMock(return_value=([], []))
    await advisor.create_proactive_stock_batch(session)
    record = scans(session)[0]
    assert record["analysis_mode"] == "not_run"
    assert "analyzed_packets" not in record
    assert "requests_today" not in record["model_budget"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["rules_only_not_configured", "rules_only_no_candidates"])
async def test_additional_analysis_modes_are_preserved(session, mode):
    batch = await advisor_at(mode=mode).create_proactive_stock_batch(session)
    assert scans(session)[0]["analysis_mode"] == mode
    assert diagnostics.MODES[mode] in diagnostics.status_messages(session, batch, CLOSE)[0]


@pytest.mark.asyncio
async def test_failed_scan_is_persisted_without_secret_error_text(session):
    advisor = advisor_at()
    advisor.market_research.stock_report = AsyncMock(
        side_effect=RuntimeError("fixture-error-do-not-persist"),
    )
    batch = await advisor.create_proactive_stock_batch(session)
    record = scans(session)[0]
    assert record["status"] == "failed"
    assert "fixture-error-do-not-persist" not in json.dumps(record)
    assert "Latest scan failed" in diagnostics.status_messages(session, batch, CLOSE)[0]


@pytest.mark.asyncio
async def test_failed_scan_outside_window_is_recorded_and_reraised(session):
    advisor = advisor_at(CLOSE - timedelta(hours=2))
    advisor.market_research.stock_report = AsyncMock(side_effect=RuntimeError("failure"))
    with pytest.raises(RuntimeError):
        await advisor.create_proactive_stock_batch(session)
    assert scans(session)[0]["status"] == "failed"
    assert session.exec(select(RecommendationBatch)).all() == []


@pytest.mark.asyncio
async def test_listener_failure_does_not_suppress_diagnostic_or_other_analysis(session):
    advisor = advisor_at()
    advisor.refresh_always_on_state = AsyncMock(side_effect=RuntimeError("fixture-listener-error"))
    batch = await advisor.create_proactive_stock_batch(session)
    assert scans(session)[0]["listener_failed"] is True
    assert "News/filing refresh failed" in diagnostics.status_messages(session, batch, CLOSE)[0]
