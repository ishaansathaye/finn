from __future__ import annotations

import asyncio
from datetime import datetime
from html import unescape
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services.recommendation_service import RecommendationService


def stock(item_number=7, **overrides):
    return {
        "item_number": item_number,
        "recommendation_type": "stock",
        "symbol": "ABC",
        "event_code": None,
        "side": "buy",
        "amount_usd": 50.0,
        "reason": "Demand improved. Why now: Orders rose. Risk watch: Margins may fall.",
        "risk": "medium",
        "valid_until": datetime(2026, 10, 8, 20),
        **overrides,
    }


def renderer(rows, *, cash=100.0, quotes=None, batch_type="stock-alert", intelligence=None):
    # Bypass initialization so presentation tests cannot create live adapters.
    service = object.__new__(RecommendationService)
    service.settings = SimpleNamespace(local_timezone="UTC", minimum_cash_buffer_usd=25)
    service.market_research = SimpleNamespace(
        cache=SimpleNamespace(get_json_stale=Mock(return_value=intelligence)),
    )
    service.stock_advisor = SimpleNamespace(
        sector_for_symbol=Mock(return_value="Tech / AI"),
        report_context_lines=Mock(return_value=["Always-on scan: everything"]),
    )
    service._batch_context = AsyncMock(return_value=(rows, 2, 150.0, quotes or {}, cash))
    session = SimpleNamespace(get=lambda *_: SimpleNamespace(batch_type=batch_type))
    return service, session


def render(rows, **kwargs):
    service, session = renderer(rows, **kwargs)
    return asyncio.run(service.format_batch_messages(session, "test-batch"))


def test_stock_card_is_compact_and_does_not_load_advisor_context():
    rec = stock(reason=(
        "Lane: confirmed core. Core sell requires thesis-break evidence. "
        "Demand improved. Why now: Orders rose. Risk watch: Margins may fall."
    ))
    service, session = renderer([rec], quotes={"ABC": {"close": 25.0}})
    message = asyncio.run(service.format_batch_messages(session, "test-batch"))[0]
    service.stock_advisor.report_context_lines.assert_not_called()
    assert "7. <b>BUY ABC</b> | Tech / AI" in message
    assert "Size: $50.00 (~2.000 shares at $25.00)" in message
    assert "Why: Demand improved. Orders rose." in message
    assert "Risk: Margins may fall." in message
    for clutter in ("Advisor context", "Industry Breakdown", "Always-on scan", "Lane:",
                    "Core sell requires", "Confidence:", "Thesis:", "Why now:", "Risk watch:"):
        assert clutter not in message


def test_stock_cards_escape_all_dynamic_text_and_keep_original_numbers():
    rows = [
        stock(11, symbol="A&B<", side="sell", reason='Demand < supply & "pricing". Risk watch: <script>bad</script>'),
        stock(4, recommendation_type="etf", symbol="ETF", reason="Diversification."),
        stock(19, symbol="WATCH", side="watch", amount_usd=0),
    ]
    service, session = renderer(rows)
    service.stock_advisor.sector_for_symbol.return_value = "Energy & <Nuclear>"
    message = asyncio.run(service.format_batch_messages(session, "test-batch"))[0]
    assert "11. <b>SELL A&amp;B&lt;</b> | Energy &amp; &lt;Nuclear&gt;" in message
    assert "Demand &lt; supply &amp; &quot;pricing&quot;." in message
    assert "Risk: &lt;script&gt;bad&lt;/script&gt;" in message
    assert "4. <b>BUY ETF</b>" in message
    assert "19. <b>WATCH WATCH</b>" in message
    assert "Size: No trade." in message
    assert message.index("4. <b>") < message.index("11. <b>") < message.index("19. <b>")


@pytest.mark.parametrize("close", [None, 0, -10, "bad quote", float("nan"), float("inf")])
def test_cash_shortage_and_missing_or_invalid_quote_do_not_invent_shares(close):
    message = render([stock(amount_usd=150)], cash=100, quotes={"ABC": {"close": close}})[0]
    assert "Needs Cash Transfer" in message
    assert "Size: $150.00" in message
    assert "Cash needed: $50.00 above the $25 buffer." in message
    assert "shares at" not in message


def test_short_text_preserves_decimals_and_deduplicates_why():
    message = render([stock(reason=(
        "Revenue rose 12.5%. Why now: Revenue rose 12.5%. Risk watch: Debt is $1.25 billion."
    ))])[0]
    assert message.count("Revenue rose 12.5%.") == 1
    assert "Debt is $1.25 billion." in message


def test_long_why_risk_and_news_are_bounded_before_html_escaping():
    text = "Growth & margins < estimates " * 40
    message = render([
        stock(reason=f"{text} Why now: Orders increased. Risk watch: {text}"),
        stock(21, recommendation_type="news", side="info", reason=text),
    ])[0]
    for label, limit in (("Why: ", 240), ("Risk: ", 160)):
        line = next(line.strip() for line in message.splitlines() if line.strip().startswith(label))
        assert len(unescape(line.removeprefix(label))) <= limit
        assert line.endswith("...")
    news_line = next(line for line in message.splitlines() if line.startswith("21. "))
    assert len(unescape(news_line.split("</b> - ", 1)[1])) <= 180
    assert "&amp;" in message and "&lt;" in message


def test_news_keeps_material_headline_and_drops_only_known_generic_suffix():
    message = render([
        stock(23, recommendation_type="news", side="info", symbol="A&B",
              reason="  Orders rise 12.5% (headline worth watching)  "),
        stock(27, recommendation_type="news", side="info",
              reason="Drug trial (Phase 3) misses endpoint (regulatory risk)"),
    ])[0]
    assert "23. <b>A&amp;B</b> - Orders rise 12.5%" in message
    assert "headline worth watching" not in message
    assert "Drug trial (Phase 3) misses endpoint (regulatory risk)" in message


def test_after_hours_timing_survives_without_verbose_exception_or_lane():
    message = render([stock(reason=(
        "After-hours high-confidence exception: Finn thinks this is worth queuing for the next market session. "
        "Lane: probable core. Demand improved."
    ))])[0]
    assert "Next market session." in message
    assert "Demand improved." in message
    assert "Lane:" not in message
    assert "high-confidence exception" not in message


def test_empty_reason_uses_honest_fallback_not_invented_evidence():
    message = render([stock(reason="Lane: tactical. ", risk="high")])[0]
    assert "Why: No rationale supplied." in message
    assert "Risk: high" in message
    assert "Confidence:" not in message


@pytest.mark.parametrize(("mode", "footer"), [
    ("gemini", None),
    ("gemini_cached", "Analysis: cached Gemini."),
    ("rules_only", "Analysis: rules-based (AI review unavailable)."),
    ("rules_only_quota_backoff", "Analysis: rules-based (AI provider retry cooldown)."),
    ("rules_only_provider_error", "Analysis: rules-based (AI provider unavailable)."),
    ("rules_only_future_reason", "Analysis: rules-based (AI review unavailable)."),
    ("rules_only_daily_budget", "Analysis: rules-based (daily AI request allowance reached)."),
    ("rules_only_request_spacing", "Analysis: rules-based (between scheduled AI reviews)."),
    ("rules_only_market_reserve", "Analysis: rules-based (AI allowance reserved for market hours)."),
    ("unknown", None),
    (None, None),
    ([], None),
])
def test_stock_analysis_footer_uses_latest_cache_mode(mode, footer):
    service, session = renderer([stock()], intelligence={"analysis_mode": mode})
    message = asyncio.run(service.format_batch_messages(session, "test-batch"))[0]
    service.market_research.cache.get_json_stale.assert_called_once_with(
        session, "stock_intelligence", "latest",
    )
    if footer:
        assert message.endswith("\n\n" + footer)
        assert message.count("Analysis:") == 1
    else:
        assert "Analysis:" not in message


@pytest.mark.parametrize("intelligence", [None, {}, [], "rules_only", 42])
def test_missing_or_non_dict_intelligence_does_not_invent_analysis_mode(intelligence):
    message = render([stock()], intelligence=intelligence)[0]
    assert "Analysis:" not in message
    assert "BUY ABC" in message


@pytest.mark.parametrize("error", [ValueError("invalid JSON"), TypeError("invalid value")])
def test_invalid_intelligence_json_does_not_break_stock_report(error):
    service, session = renderer([stock()])
    service.market_research.cache.get_json_stale.side_effect = error
    message = asyncio.run(service.format_batch_messages(session, "test-batch"))[0]
    assert "Analysis:" not in message
    assert "BUY ABC" in message


def test_news_only_stock_report_includes_fallback_footer():
    message = render(
        [stock(recommendation_type="news", side="info", reason="Orders rose.")],
        intelligence={"analysis_mode": "rules_only"},
    )[0]
    assert message.endswith("\n\nAnalysis: rules-based (AI review unavailable).")


def test_event_only_report_does_not_read_stock_intelligence():
    event = stock(recommendation_type="event", symbol=None, side="yes")
    service, session = renderer([event], intelligence={"analysis_mode": "rules_only"})
    service._event_display = Mock(return_value=("A & B", "A < B", "Sport", "Tonight"))
    message = asyncio.run(service.format_batch_messages(session, "test-batch"))[0]
    assert "Analysis:" not in message
    service.market_research.cache.get_json_stale.assert_not_called()


def test_generic_event_output_is_unchanged_in_mixed_batch():
    event = stock(8, recommendation_type="event", symbol=None, side="yes",
                  amount_usd=30, reason="Lineup advantage.")
    service, session = renderer([stock(), event], intelligence={"analysis_mode": "rules_only"})
    service._event_display = Mock(return_value=("A & B", "A < B", "Sport", "Tonight"))
    messages = asyncio.run(service.format_batch_messages(session, "test-batch"))
    assert messages[0].endswith("Analysis: rules-based (AI review unavailable).")
    assert messages[1] == (
        "<b>Finn Report: Prediction Markets</b>\n"
        "Buying power context: <b>$150.00</b>\n\n"
        "<b>Prediction Market Ideas</b>\n"
        "8. <b>A &amp; B</b>\n"
        "   Sport \u2022 Tonight\n"
        "   Pick: <b>A &lt; B</b>\n"
        "   Stake: <b>$30.00</b> (20.0%)\n"
        "   Confidence: <b>Medium</b>\n"
        "   Why: Lineup advantage"
    )


@pytest.mark.parametrize("batch_type", ["gx", "gx-live", "small-event"])
def test_gx_event_routes_bypass_stock_presentation(batch_type):
    service, session = renderer([], batch_type=batch_type)
    service.format_gx_batch = AsyncMock(return_value="event format unchanged")
    assert asyncio.run(service.format_batch_messages(session, "test-batch")) == ["event format unchanged"]
    service.format_gx_batch.assert_awaited_once_with(session, "test-batch")
    service.market_research.cache.get_json_stale.assert_not_called()
    service.stock_advisor.report_context_lines.assert_not_called()


@pytest.mark.parametrize("batch_type", ["event-monitor", "event-control", "event-control-auto"])
def test_special_event_formats_do_not_use_stock_presentation(batch_type):
    service, session = renderer([], batch_type=batch_type)
    rec = SimpleNamespace(**stock(8, recommendation_type="event", side="yes",
                                  amount_usd=30, reason="Lineup advantage."))
    service.recommendations_for_batch = Mock(return_value=[rec])
    service._event_display = Mock(return_value=("A & B", "A < B", "Sport", "Tonight"))
    service._control_recommendation_action = Mock(return_value="add")
    service._position_for_control_recommendation = Mock(return_value=SimpleNamespace(amount_usd=20))
    message = asyncio.run(service.format_batch_messages(session, "test-batch"))[0]
    if batch_type == "event-control-auto":
        assert message == "Lineup advantage."
    else:
        assert "8. <b>A &amp; B</b>" in message
        assert "Current pick: <b>A &lt; B</b>" in message
        if batch_type == "event-monitor":
            assert "Possible add size: <b>$30.00</b>" in message
            assert "Why Finn is alerting: Lineup advantage" in message
        else:
            assert "Current stake: <b>$20.00</b>" in message
            assert "Suggested add: <b>$30.00</b>" in message
            assert "Why: Lineup advantage" in message
    service._batch_context.assert_not_awaited()
    service.market_research.cache.get_json_stale.assert_not_called()
    service.stock_advisor.report_context_lines.assert_not_called()
