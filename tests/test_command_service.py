from __future__ import annotations

# ruff: noqa: E402

import os
import json
from pathlib import Path
from tempfile import TemporaryDirectory

TEST_DIRECTORY = TemporaryDirectory(prefix="finn-tests-")
TEST_DATABASE = Path(TEST_DIRECTORY.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DATABASE}"

import pytest
from sqlmodel import delete

from app.command_service import CommandService
from app.db import get_session, init_db
from app.models import (
    Account,
    AccountSnapshot,
    AgenticAccountSnapshot,
    AgenticDecisionJournal,
    AgenticOrderSnapshot,
    AgenticPositionSnapshot,
    Approval,
    EventBetOutcome,
    EventPosition,
    InboundMessage,
    IntegrationState,
    Recommendation,
    RecommendationBatch,
    RecommendationOutcome,
    Holding,
)
from app.services.market_research import EventIdea, EventSlateItem, StockIdea
from app.telegram_bot import TelegramBot


def _reset_tables() -> None:
    with get_session() as session:
        for model in (
            AgenticDecisionJournal,
            AgenticOrderSnapshot,
            AgenticPositionSnapshot,
            AgenticAccountSnapshot,
            Approval,
            EventBetOutcome,
            EventPosition,
            RecommendationOutcome,
            Recommendation,
            RecommendationBatch,
            InboundMessage,
            IntegrationState,
        ):
            session.exec(delete(model))
        session.commit()


@pytest.fixture(autouse=True)
def _clean_db_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_chat_id", "test-chat")
    # Explicit test scenarios, independent of private deployment settings.
    monkeypatch.setattr(settings, "core_stock_symbols", "SNDK")
    monkeypatch.setattr(settings, "employment_excluded_stock_symbols", "CSCO")
    init_db()
    _reset_tables()
    yield
    _reset_tables()


def test_portfolio_snapshot_includes_finance_context() -> None:
    service = CommandService(TelegramBot())
    # Fictional benefits, allocations, and dates, not a personal finance snapshot.
    context = {
        "espp": {
            "paycheck_contribution_pct": 4,
            "discount_pct": 8,
            "lookback": "Example Employer purchase-date price",
            "next_purchase_date": "2030-03-15",
            "planned_action": "synthetic purchase scenario",
        },
        "roth_ira": {
            "contribution_year": 2030,
            "contribution_amount": 1200,
            "holdings": ["AAA", "BBB", "CCC"],
        },
        "401k": {
            "paycheck_contribution_pct": 3,
            "current_allocation": "Example Target Fund 2050",
        },
        "cash_allocation": {
            "robinhood_gold_active": True,
            "robinhood_high_yield_cash_apy_pct": 2.0,
            "varo_plan": "Example Bank synthetic deposit scenario",
            "overflow_plan": "Example Reserve synthetic overflow scenario",
        },
    }
    with get_session() as session:
        session.add(IntegrationState(
            provider="finance_profile", key="snapshot_context", value=json.dumps(context),
        ))
        session.commit()
        lines = service.recommendations.market_research.portfolio_lines(session)

    assert "Finance context:" in lines
    assert any("ESPP" in line and "4% of paycheck" in line and "2030-03-15" in line for line in lines)
    assert any("Roth IRA" in line and "$1,200" in line and "AAA, BBB, CCC" in line for line in lines)
    assert any("Cash allocation" in line and "2% APY" in line and "Example Bank" in line for line in lines)


@pytest.mark.asyncio
async def test_agent_command_renders_read_only_agentic_snapshot() -> None:
    service = CommandService(TelegramBot())
    # Round synthetic balances and a fictional symbol, not broker observations.
    snapshot = {
        "as_of": "2030-03-15T17:00:00Z",
        "mode": "dry_run",
        "account": {
            "total_value": 2000.0,
            "cash": 1500.0,
            "buying_power": 1500.0,
            "realized_pnl_today": 20.0,
            "unrealized_pnl_today": 25.0,
        },
        "positions": [{
            "symbol": "AAA", "quantity": 5.0, "market_value": 500.0,
            "average_cost": 95.0, "unrealized_pnl": 25.0,
            "unrealized_pnl_pct": 5.0,
        }],
        "recent_decisions": [{
            "created_at": "2030-03-15T16:45:00Z", "action": "buy", "symbol": "AAA",
            "result": "dry_run", "confidence": 0.7,
            "thesis": "Synthetic strategy observation.",
        }],
        "risk": {
            "mode": "dry_run", "kill_switch": "not active",
            "daily_loss_guard": "normal", "max_position_guard": "normal",
        },
        "health": {"codex_available": True, "robinhood_mcp_available": True},
    }
    with get_session() as session:
        session.add(IntegrationState(
            provider="agentic_trader", key="latest_snapshot", value=json.dumps(snapshot),
        ))
        session.commit()
        response = await service.handle_message(
            session, chat_id="test-chat", sender_id="test-user",
            external_message_id="agent-1", text="agent",
        )
    assert "Finn Agentic Account Observer" in response
    assert "Value: $2,000.00" in response
    assert "Buying power: $1,500.00" in response
    assert "AAA" in response
    assert "Synthetic strategy observation" in response
    assert "Codex available: True" in response


@pytest.mark.asyncio
async def test_report_and_execute_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = TelegramBot()
    service = CommandService(bot)

    async def fake_top_stock_ideas(session, limit=7):  # noqa: ANN001
        return [
            StockIdea(
                symbol="NVDA",
                recommendation_type="stock",
                side="hold",
                amount_usd=0.0,
                reason="Keep holding NVIDIA; evidence is still constructive.",
                risk="low",
                change_pct=1.2,
                priority_score=5.0,
            ),
            StockIdea(
                symbol="VTI",
                recommendation_type="etf",
                side="buy",
                amount_usd=150.0,
                reason="Broad-market exposure still fits the portfolio.",
                risk="low",
                change_pct=0.4,
                priority_score=4.0,
            ),
        ]

    async def fake_top_event_ideas(session, limit=4, force_recommendations=False):  # noqa: ANN001
        return [
            EventIdea(
                ticker="TEST-EVENT-1",
                title="Will Team A beat Team B?",
                side="YES",
                amount_usd=10.0,
                reason="Readable same-day market. Market implied: 62.0%. Finn fair: 66.0%. Edge: 4.0 pts. Why: strong same-day pricing.",
                risk="high",
                pick_label="Team A",
                priority_score=2.0,
                category="Basketball",
                time_label="7:30 PM",
                execution_market_ticker="KXTEST-TEAMA",
                execution_event_ticker="KXTEST-EVENT",
                execution_contract_id="contract-1",
                contract_path="Main market",
                implied_probability=0.62,
                max_return_usd=13.0,
                potential_profit_usd=3.0,
            )
        ]

    async def fake_top_event_execution_ideas(session, limit=8):  # noqa: ANN001
        return [
            EventIdea(
                ticker="TEST-EVENT-1",
                title="Will Team A beat Team B?",
                side="YES",
                amount_usd=2.0,
                reason=(
                    "Small event still clears payout math. "
                    "Market implied: 50.0%. Finn fair: 54.0%. Edge: 4.0 pts. "
                    "Max return: $4.00. Potential profit: $2.00."
                ),
                risk="high",
                pick_label="Team A",
                priority_score=2.5,
                category="Basketball",
                time_label="Time TBD",
                execution_market_ticker="KXTEST-TEAMC",
                execution_event_ticker="KXTEST-EVENT-AUTO",
                execution_contract_id="contract-2",
                contract_path="Main market",
                implied_probability=0.5,
                max_return_usd=4.0,
                potential_profit_usd=2.0,
            )
        ]

    async def fake_quote_map(session, symbols):  # noqa: ANN001
        return {"VTI": {"close": 250.0}}

    async def fake_stock_report(session, stock_limit=10, news_limit=3):  # noqa: ANN001
        return await fake_top_stock_ideas(session, stock_limit), []

    async def fake_today_event_slate(limit=12):  # noqa: ANN001
        return [
            EventSlateItem(
                ticker="TEST-EVENT-1",
                title="Will Team A beat Team B?",
                event_day="2026-04-25",
                time_label="7:30 PM",
                sort_key=(0, "19:30", "TEST-EVENT-1"),
                options="Team A / Team B",
                is_same_day=True,
            )
        ]

    async def fake_quotes(contract_ids):  # noqa: ANN001
        rows = {}
        if "contract-1" in contract_ids:
            rows["contract-1"] = {"yes_ask_price": "0.5000"}
        if "contract-2" in contract_ids:
            rows["contract-2"] = {"yes_ask_price": "0.5000"}
        return rows

    monkeypatch.setattr(service.recommendations.market_research, "stock_report", fake_stock_report)
    monkeypatch.setattr(service.recommendations.market_research, "top_event_ideas", fake_top_event_ideas)
    monkeypatch.setattr(service.recommendations.market_research, "top_event_execution_ideas", fake_top_event_execution_ideas)
    monkeypatch.setattr(service.recommendations.market_research, "today_event_slate", fake_today_event_slate)
    monkeypatch.setattr(service.recommendations.market_research.robinhood_prediction, "get_marketdata_quotes", fake_quotes)
    monkeypatch.setattr(
        service.recommendations.market_research.kalshi,
        "list_markets",
        lambda **kwargs: __import__("asyncio").sleep(0, result={"markets": []}),
    )
    monkeypatch.setattr(service.recommendations.market_research, "portfolio_summary", lambda session: (2, 250.0))
    monkeypatch.setattr(service.recommendations.market_research, "quote_map", fake_quote_map)
    monkeypatch.setattr(
        service.recommendations.portfolio_sync,
        "sync",
        lambda session: __import__("asyncio").sleep(0, result="stubbed sync"),
    )

    with get_session() as session:
        report = await service.handle_message(
            session,
            chat_id="test-chat",
            sender_id="test-user",
            external_message_id="m-1",
            text="report",
        )
        assert "Finn Report" in report
        assert "<b>BUY VTI</b> | Broad Market / ETFs" in report
        assert "BUY VTI" in report
        assert "Keep Holding" not in report
        batch = service.recommendations.latest_batch(session)
        assert batch is not None
        messages = await service.recommendations.format_batch_messages(session, batch.id)
        assert len(messages) == 1
        assert "Stocks / ETFs" in messages[0]

        deprecated_approve = await service.handle_message(
            session,
            chat_id="test-chat",
            sender_id="test-user",
            external_message_id="m-2",
            text="approve 2",
        )
        assert "deprecated" in deprecated_approve.lower()

        deprecated_executed = await service.handle_message(
            session,
            chat_id="test-chat",
            sender_id="test-user",
            external_message_id="m-3",
            text="executed 2",
        )
        assert "deprecated" in deprecated_executed.lower()

        gamble = await service.handle_message(
            session,
            chat_id="test-chat",
            sender_id="test-user",
            external_message_id="m-3b",
            text="gamble",
        )
        assert "Prediction Markets" in gamble
        latest_batch = service.recommendations.latest_batch(session)
        assert latest_batch is not None
        gamble_messages = await service.recommendations.format_batch_messages(session, latest_batch.id)
        assert len(gamble_messages) == 1
        assert "Prediction Markets" in gamble_messages[0]
        assert (
            "Market / Edge" in gamble_messages[0]
            or "No broker-available near-term event markets passed the current filter" in gamble_messages[0]
        )

        gs = await service.handle_message(
            session,
            chat_id="test-chat",
            sender_id="test-user",
            external_message_id="m-3gx",
            text="gs",
        )
        assert "Finn Small Bets: gs" in gs
        assert "gs x" in gs

        gx_from_g = await service.handle_message(
            session,
            chat_id="test-chat",
            sender_id="test-user",
            external_message_id="m-3gxa",
            text="g x all",
        )
        assert "Execution Lane: gx" in gx_from_g
        assert "Will Team A beat Team B?" in gx_from_g

        gx_from_gs = await service.handle_message(
            session,
            chat_id="test-chat",
            sender_id="test-user",
            external_message_id="m-3gxs",
            text="gs x all",
        )
        assert "Execution Lane: gx" in gx_from_gs
        assert "Will Team A beat Team B?" in gx_from_gs

        slate = await service.handle_message(
            session,
            chat_id="test-chat",
            sender_id="test-user",
            external_message_id="m-3c",
            text="gsl",
        )
        assert "Finn Slate" in slate
        assert "Will Team A beat Team B?" in slate


@pytest.mark.asyncio
async def test_deny_and_betstats_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = TelegramBot()
    service = CommandService(bot)

    async def fake_top_stock_ideas(session, limit=7):  # noqa: ANN001
        return []

    async def fake_top_event_ideas(session, limit=4):  # noqa: ANN001
        return []

    async def fake_top_event_execution_ideas(session, limit=8):  # noqa: ANN001
        return []

    async def fake_stock_report(session, stock_limit=10, news_limit=3):  # noqa: ANN001
        return [], []

    async def fake_today_event_slate(limit=12):  # noqa: ANN001
        return []

    monkeypatch.setattr(service.recommendations.market_research, "stock_report", fake_stock_report)
    monkeypatch.setattr(service.recommendations.market_research, "top_event_ideas", fake_top_event_ideas)
    monkeypatch.setattr(service.recommendations.market_research, "top_event_execution_ideas", fake_top_event_execution_ideas)
    monkeypatch.setattr(service.recommendations.market_research, "today_event_slate", fake_today_event_slate)
    monkeypatch.setattr(
        service.recommendations.market_research.kalshi,
        "list_markets",
        lambda **kwargs: __import__("asyncio").sleep(0, result={"markets": []}),
    )
    monkeypatch.setattr(
        service.recommendations.portfolio_sync,
        "sync",
        lambda session: __import__("asyncio").sleep(0, result="stubbed sync"),
    )

    with get_session() as session:
        await service.handle_message(
            session,
            chat_id="test-chat",
            sender_id="test-user",
            external_message_id="m-4",
            text="report",
        )
        latest = service.recommendations.latest_batch(session)
        assert latest is not None

        event_recommendation = Recommendation(
            batch_id=latest.id,
            item_number=99,
            recommendation_type="event",
            symbol="Synthetic Event || Team A || Basketball || 7:30 PM",
            event_code="TEST-EVENT",
            side="buy",
            amount_usd=15.0,
            reason="Synthetic event recommendation for test coverage.",
            risk="high",
            execution_mode="manual",
            valid_until=service.recommendations.latest_recommendations(session)[0].valid_until,
        )
        session.add(event_recommendation)
        session.commit()

        deprecated_executed = await service.handle_message(
            session,
            chat_id="test-chat",
            sender_id="test-user",
            external_message_id="m-5",
            text="executed 99",
        )
        assert "deprecated" in deprecated_executed.lower()

        deny_reply = await service.handle_message(
            session,
            chat_id="test-chat",
            sender_id="test-user",
            external_message_id="m-5b",
            text="deny 99",
        )
        assert "Recorded `deny` for item(s): 99" == deny_reply

        bets = await service.handle_message(
            session,
            chat_id="test-chat",
            sender_id="test-user",
            external_message_id="m-6",
            text="bets",
        )
        assert "Finn Bets: Today" in bets

        betstats = await service.handle_message(
            session,
            chat_id="test-chat",
            sender_id="test-user",
            external_message_id="m-7",
            text="betstats",
        )
        assert "Finn Bet Stats" in betstats


@pytest.mark.asyncio
async def test_gx_preview_and_confirm_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = TelegramBot()
    service = CommandService(bot)

    async def fake_top_event_execution_ideas(session, limit=8):  # noqa: ANN001
        return [
            EventIdea(
                ticker="TEST-EVENT-1",
                title="Will Team A beat Team B?",
                side="YES",
                amount_usd=2.0,
                reason=(
                    "Small event still clears payout math. "
                    "Market implied: 50.0%. Finn fair: 54.0%. Edge: 4.0 pts. "
                    "Max return: $4.00. Potential profit: $2.00."
                ),
                risk="high",
                pick_label="Team A",
                priority_score=2.5,
                category="Basketball",
                time_label="Time TBD",
                execution_market_ticker="KXTEST-TEAMA",
                execution_event_ticker="KXTEST-EVENT",
                execution_contract_id="contract-1",
                contract_path="Main market",
                implied_probability=0.5,
                max_return_usd=4.0,
                potential_profit_usd=2.0,
            )
        ]

    async def fake_list_markets(**kwargs):  # noqa: ANN001
        return {"markets": []}

    async def fake_quotes(contract_ids):  # noqa: ANN001
        return {"contract-1": {"yes_ask_price": "0.5000"}}

    async def fake_create_event_order(**kwargs):  # noqa: ANN001
        return {
            "orderId": "order-1",
            "filledQuantity": "4",
            "quantity": "4",
            "averagePrice": "0.50",
        }

    async def fake_event_account():
        return None

    monkeypatch.setattr(service.recommendations.market_research, "top_event_execution_ideas", fake_top_event_execution_ideas)
    monkeypatch.setattr(service.recommendations.market_research.robinhood_prediction, "get_event_contract_account", fake_event_account)
    monkeypatch.setattr(service.recommendations.market_research.robinhood_prediction, "get_marketdata_quotes", fake_quotes)
    monkeypatch.setattr(service.recommendations.market_research.robinhood_prediction, "create_event_contract_order", fake_create_event_order)
    monkeypatch.setattr(service.recommendations.market_research.robinhood_prediction, "can_write", lambda: True)
    monkeypatch.setattr(service.recommendations.portfolio_sync, "sync", lambda session: __import__("asyncio").sleep(0, result="stubbed sync"))
    monkeypatch.setattr(service.recommendations.market_research, "portfolio_summary", lambda session: (3, 500.0))
    monkeypatch.setattr(service.recommendations.market_research, "_current_buying_power", lambda session: 500.0)

    with get_session() as session:
        gs = await service.handle_message(
            session,
            chat_id="test-chat",
            sender_id="test-user",
            external_message_id="m-gx-1",
            text="gs",
        )
        assert "Finn Small Bets: gs" in gs

        gx = await service.handle_message(
            session,
            chat_id="test-chat",
            sender_id="test-user",
            external_message_id="m-gx-1b",
            text="gs x all",
        )
        assert "Execution Lane: gx" in gx

        preview = await service.handle_message(
            session,
            chat_id="test-chat",
            sender_id="test-user",
            external_message_id="m-gx-2",
            text="x",
        )
        assert "Execution Preview: gx" in preview
        assert "Orders to place" in preview

        confirm = await service.handle_message(
            session,
            chat_id="test-chat",
            sender_id="test-user",
            external_message_id="m-gx-3",
            text="x c",
        )
        assert "Execution Result: gx" in confirm
        assert "filled" in confirm.lower()

        bets = await service.handle_message(
            session,
            chat_id="test-chat",
            sender_id="test-user",
            external_message_id="m-gx-4",
            text="bets",
        )
        assert "Will Team A beat Team B?" in bets


@pytest.mark.asyncio
async def test_gx_confirm_requires_robinhood_write_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = TelegramBot()
    service = CommandService(bot)

    async def fake_top_event_execution_ideas(session, limit=8):  # noqa: ANN001
        return [
            EventIdea(
                ticker="TEST-EVENT-1",
                title="Will Team A beat Team B?",
                side="YES",
                amount_usd=2.0,
                reason="Small event still clears payout math.",
                risk="high",
                pick_label="Team A",
                priority_score=2.5,
                category="Basketball",
                time_label="Time TBD",
                execution_market_ticker="RH-TEST-TEAMA",
                execution_event_ticker="RH-TEST-EVENT",
                execution_contract_id="contract-1",
                contract_path="Main market",
                implied_probability=0.5,
                max_return_usd=3.0,
                potential_profit_usd=1.0,
            )
        ]

    async def fake_quotes(contract_ids):  # noqa: ANN001
        return {"contract-1": {"yes_ask_price": "0.5000"}}

    monkeypatch.setattr(service.recommendations.market_research, "top_event_execution_ideas", fake_top_event_execution_ideas)
    monkeypatch.setattr(service.recommendations.market_research.robinhood_prediction, "get_marketdata_quotes", fake_quotes)
    monkeypatch.setattr(service.recommendations.market_research.robinhood_prediction, "can_write", lambda: False)
    monkeypatch.setattr(service.recommendations.portfolio_sync, "sync", lambda session: __import__("asyncio").sleep(0, result="stubbed sync"))
    monkeypatch.setattr(service.recommendations.market_research, "portfolio_summary", lambda session: (3, 500.0))

    with get_session() as session:
        await service.handle_message(
            session,
            chat_id="test-chat",
            sender_id="test-user",
            external_message_id="m-gx-r-1",
            text="gs",
        )
        await service.handle_message(
            session,
            chat_id="test-chat",
            sender_id="test-user",
            external_message_id="m-gx-r-1b",
            text="gs x all",
        )
        await service.handle_message(
            session,
            chat_id="test-chat",
            sender_id="test-user",
            external_message_id="m-gx-r-2",
            text="x",
        )
        confirm = await service.handle_message(
            session,
            chat_id="test-chat",
            sender_id="test-user",
            external_message_id="m-gx-r-3",
            text="x c",
        )
        assert "write auth is not configured" in confirm.lower()


@pytest.mark.asyncio
async def test_event_monitor_batch_is_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = TelegramBot()
    service = CommandService(bot)

    async def fake_refresh(session):  # noqa: ANN001
        return None

    async def fake_followups(session, limit=4):  # noqa: ANN001
        return [
            EventIdea(
                ticker="MONITOR-EVENT-1",
                title="Follow-up: Team A vs Team B",
                side="YES",
                amount_usd=12.0,
                reason="Existing position moved enough to justify a fresh look. Market implied: 61.0%. Finn fair: 67.0%. Edge: 4.0 pts.",
                risk="medium",
                pick_label="Team A",
                priority_score=8.0,
                confidence="high",
                category="Basketball",
                time_label="7:30 PM",
                is_same_day=True,
                is_relevant_now=True,
            )
        ]

    monkeypatch.setattr(service.recommendations, "_refresh_event_bet_outcomes", fake_refresh)
    monkeypatch.setattr(service.recommendations.market_research, "monitored_event_followups", fake_followups)

    with get_session() as session:
        batch = await service.recommendations.create_event_monitor_batch(session)
        assert batch is None


@pytest.mark.asyncio
async def test_plain_y_approves_latest_pending_position_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = TelegramBot()
    service = CommandService(bot)

    async def fake_execute(session, recommendations):  # noqa: ANN001
        for recommendation in recommendations:
            recommendation.status = "executed"
            session.add(recommendation)
        session.commit()
        return "executed control"

    monkeypatch.setattr(service.recommendations, "execute_position_control_recommendations", fake_execute)

    with get_session() as session:
        batch = RecommendationBatch(batch_type="event-control")
        session.add(batch)
        session.commit()
        session.refresh(batch)
        session.add(
            Recommendation(
                batch_id=batch.id,
                item_number=1,
                recommendation_type="event",
                symbol="Los Angeles D vs Arizona || Los Angeles D || Baseball || 6:40 PM",
                event_code="event-1",
                side="yes",
                amount_usd=6.88,
                reason="Finn recommends CUT for this live position.",
                risk="medium",
                execution_mode="manual",
                valid_until=batch.created_at,
            )
        )
        session.commit()

        reply = await service.handle_message(
            session,
            chat_id="test-chat",
            sender_id="test-user",
            external_message_id="plain-y-1",
            text="y",
        )
        assert reply == "executed control"


@pytest.mark.asyncio
async def test_event_scout_suppresses_low_conviction_new_bets(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = TelegramBot()
    service = CommandService(bot)

    async def fake_refresh(session):  # noqa: ANN001
        return None

    async def fake_top_event_ideas(session, limit=8, force_recommendations=True, proactive_manual=True):  # noqa: ANN001
        return [
            EventIdea(
                ticker="SCOUT-LOW-1",
                title="Team A vs Team B",
                side="YES",
                amount_usd=8.0,
                reason=(
                    "Readable but not urgent. Market implied: 61.0%. Finn fair: 63.0%. "
                    "Edge: 2.0 pts. EV/contract: $0.010. Potential profit: $2.00."
                ),
                risk="high",
                pick_label="Team A",
                priority_score=7.0,
                confidence="medium",
                execution_contract_id="contract-low",
                potential_profit_usd=2.0,
                is_same_day=True,
                is_relevant_now=True,
            )
        ]

    async def fake_top_event_execution_ideas(session, limit=32, include_live=True):  # noqa: ANN001
        return []

    monkeypatch.setattr(service.recommendations, "_refresh_event_bet_outcomes", fake_refresh)
    monkeypatch.setattr(service.recommendations.market_research, "top_event_ideas", fake_top_event_ideas)
    monkeypatch.setattr(service.recommendations.market_research, "top_event_execution_ideas", fake_top_event_execution_ideas)
    monkeypatch.setattr(service.recommendations.portfolio_sync.adapter, "configured", lambda: False)

    with get_session() as session:
        batch = await service.recommendations.create_event_scout_batch(session)
        assert batch is None


@pytest.mark.asyncio
async def test_event_scout_allows_high_conviction_new_bets(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = TelegramBot()
    service = CommandService(bot)

    async def fake_refresh(session):  # noqa: ANN001
        return None

    async def fake_top_event_ideas(session, limit=8, force_recommendations=True, proactive_manual=True):  # noqa: ANN001
        return [
            EventIdea(
                ticker="SCOUT-HIGH-1",
                title="Team A vs Team B",
                side="YES",
                amount_usd=12.0,
                reason=(
                    "Strong pricing dislocation. Market implied: 58.0%. Finn fair: 66.0%. "
                    "Edge: 8.0 pts. EV/contract: $0.045. Potential profit: $4.00."
                ),
                risk="medium",
                pick_label="Team A",
                priority_score=10.5,
                confidence="high",
                execution_contract_id="contract-high",
                potential_profit_usd=4.0,
                is_same_day=True,
                is_relevant_now=True,
            )
        ]

    async def fake_top_event_execution_ideas(session, limit=32, include_live=True):  # noqa: ANN001
        return []

    monkeypatch.setattr(service.recommendations, "_refresh_event_bet_outcomes", fake_refresh)
    monkeypatch.setattr(service.recommendations.market_research, "top_event_ideas", fake_top_event_ideas)
    monkeypatch.setattr(service.recommendations.market_research, "top_event_execution_ideas", fake_top_event_execution_ideas)
    monkeypatch.setattr(service.recommendations.portfolio_sync.adapter, "configured", lambda: False)

    with get_session() as session:
        batch = await service.recommendations.create_event_scout_batch(session)
        assert batch is not None
        messages = await service.recommendations.format_batch_messages(session, batch.id)
        assert "SCOUT-HIGH-1" in messages[0] or "Team A vs Team B" in messages[0]


@pytest.mark.asyncio
async def test_stock_report_excludes_employer_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = TelegramBot()
    service = CommandService(bot)

    async def fake_analyze_stock_universe(session):  # noqa: ANN001
        return (
            [
                {"symbol": "CSCO", "news_headlines": []},
                {"symbol": "RKLB", "news_headlines": []},
            ],
            {},
            [
                StockIdea(
                    symbol="CSCO",
                    recommendation_type="stock",
                    side="buy",
                    amount_usd=100.0,
                    reason="Employer stock should not be recommended.",
                    risk="medium",
                    change_pct=1.0,
                    priority_score=9.0,
                    confidence="high",
                ),
                StockIdea(
                    symbol="RKLB",
                    recommendation_type="stock",
                    side="buy",
                    amount_usd=100.0,
                    reason="Non-employer stock remains eligible.",
                    risk="medium",
                    change_pct=1.0,
                    priority_score=9.0,
                    confidence="high",
                ),
            ],
        )

    monkeypatch.setattr(service.recommendations.market_research, "_analyze_stock_universe", fake_analyze_stock_universe)

    with get_session() as session:
        ideas, _briefs = await service.recommendations.market_research.stock_report(session)
        assert [idea.symbol for idea in ideas] == ["RKLB"]


@pytest.mark.asyncio
async def test_stock_alert_suppresses_routine_after_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = TelegramBot()
    service = CommandService(bot)

    async def fake_refresh(session):  # noqa: ANN001
        return {}

    async def fake_stock_report(session, stock_limit=5, news_limit=4):  # noqa: ANN001
        return [
            StockIdea(
                symbol="ZZZZ",
                recommendation_type="stock",
                side="buy",
                amount_usd=100.0,
                reason="Routine buy idea.",
                risk="medium",
                change_pct=1.0,
                priority_score=6.0,
                confidence="medium",
            )
        ], []

    monkeypatch.setattr(service.recommendations.stock_advisor, "refresh_always_on_state", fake_refresh)
    monkeypatch.setattr(service.recommendations.stock_advisor.market_research, "stock_report", fake_stock_report)
    monkeypatch.setattr(service.recommendations.stock_advisor, "_is_regular_market_hours", lambda: False)

    with get_session() as session:
        batch = await service.recommendations.create_stock_alert_batch(session)

    assert batch is None


@pytest.mark.asyncio
async def test_stock_alert_allows_high_confidence_after_hours_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = TelegramBot()
    service = CommandService(bot)

    async def fake_refresh(session):  # noqa: ANN001
        return {}

    async def fake_stock_report(session, stock_limit=5, news_limit=4):  # noqa: ANN001
        return [
            StockIdea(
                symbol="RKLB",
                recommendation_type="stock",
                side="buy",
                amount_usd=150.0,
                reason="Strong catalyst with clear action window.",
                risk="medium",
                change_pct=4.0,
                priority_score=8.5,
                confidence="high",
            )
        ], []

    async def fake_quote_map(session, symbols):  # noqa: ANN001
        return {"RKLB": {"close": 10.0}}

    monkeypatch.setattr(service.recommendations.stock_advisor, "refresh_always_on_state", fake_refresh)
    monkeypatch.setattr(service.recommendations.stock_advisor.market_research, "stock_report", fake_stock_report)
    monkeypatch.setattr(service.recommendations.stock_advisor.market_research, "quote_map", fake_quote_map)
    monkeypatch.setattr(service.recommendations.stock_advisor, "_is_regular_market_hours", lambda: False)

    with get_session() as session:
        batch = await service.recommendations.create_stock_alert_batch(session)
        assert batch is not None
        messages = await service.recommendations.format_batch_messages(session, batch.id)

    assert "Next market session" in messages[0]
    assert "RKLB" in messages[0]


@pytest.mark.asyncio
async def test_stock_alert_allows_routine_during_market_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = TelegramBot()
    service = CommandService(bot)

    async def fake_refresh(session):  # noqa: ANN001
        return {}

    async def fake_stock_report(session, stock_limit=5, news_limit=4):  # noqa: ANN001
        return [
            StockIdea(
                symbol="VTI",
                recommendation_type="etf",
                side="buy",
                amount_usd=100.0,
                reason="Routine market-hours ETF buy idea.",
                risk="low",
                change_pct=0.5,
                priority_score=5.5,
                confidence="medium",
            )
        ], []

    async def fake_quote_map(session, symbols):  # noqa: ANN001
        return {"VTI": {"close": 250.0}}

    monkeypatch.setattr(service.recommendations.stock_advisor, "refresh_always_on_state", fake_refresh)
    monkeypatch.setattr(service.recommendations.stock_advisor.market_research, "stock_report", fake_stock_report)
    monkeypatch.setattr(service.recommendations.stock_advisor.market_research, "quote_map", fake_quote_map)
    monkeypatch.setattr(service.recommendations.stock_advisor, "_is_regular_market_hours", lambda: True)

    with get_session() as session:
        batch = await service.recommendations.create_stock_alert_batch(session)
        assert batch is not None
        messages = await service.recommendations.format_batch_messages(session, batch.id)

    assert "VTI" in messages[0]
    assert "After-hours high-confidence exception" not in messages[0]


@pytest.mark.asyncio
async def test_stock_alert_suppresses_repeat_symbol_action_even_if_reason_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = TelegramBot()
    service = CommandService(bot)
    calls = 0

    async def fake_refresh(session):  # noqa: ANN001
        return {}

    async def fake_stock_report(session, stock_limit=5, news_limit=4):  # noqa: ANN001
        nonlocal calls
        calls += 1
        return [
            StockIdea(
                symbol="ZZZY",
                recommendation_type="stock",
                side="buy",
                amount_usd=100.0,
                reason=f"Fresh wording {calls}.",
                risk="medium",
                change_pct=1.0,
                priority_score=7.0,
                confidence="medium",
            )
        ], []

    async def fake_quote_map(session, symbols):  # noqa: ANN001
        return {"ZZZY": {"close": 10.0}}

    monkeypatch.setattr(service.recommendations.stock_advisor, "refresh_always_on_state", fake_refresh)
    monkeypatch.setattr(service.recommendations.stock_advisor.market_research, "stock_report", fake_stock_report)
    monkeypatch.setattr(service.recommendations.stock_advisor.market_research, "quote_map", fake_quote_map)
    monkeypatch.setattr(service.recommendations.stock_advisor, "_is_regular_market_hours", lambda: True)

    with get_session() as session:
        first = await service.recommendations.create_stock_alert_batch(session)
        second = await service.recommendations.create_stock_alert_batch(session)

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_stock_alert_requires_high_confidence_for_core_buy(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = TelegramBot()
    service = CommandService(bot)

    async def fake_refresh(session):  # noqa: ANN001
        return {}

    async def fake_stock_report(session, stock_limit=5, news_limit=4):  # noqa: ANN001
        return [
            StockIdea(
                symbol="SNDK",
                recommendation_type="stock",
                side="buy",
                amount_usd=100.0,
                reason="Core buy should not spam on medium confidence.",
                risk="medium",
                change_pct=1.0,
                priority_score=9.0,
                confidence="medium",
            )
        ], []

    monkeypatch.setattr(service.recommendations.stock_advisor, "refresh_always_on_state", fake_refresh)
    monkeypatch.setattr(service.recommendations.stock_advisor.market_research, "stock_report", fake_stock_report)
    monkeypatch.setattr(service.recommendations.stock_advisor, "_is_regular_market_hours", lambda: True)

    with get_session() as session:
        batch = await service.recommendations.create_stock_alert_batch(session)

    assert batch is None


def test_buying_power_falls_back_to_cash_when_adapter_omits_buying_power() -> None:
    service = CommandService(TelegramBot())
    cash_balance, buying_power = service.recommendations.portfolio_sync._extract_cash_snapshot(
        {"balances": [{"currency": {"code": "USD"}, "cash": 500.0}]}, 0.0,
    )
    assert cash_balance == 500.0
    assert buying_power == 500.0


def test_live_extreme_sports_pick_is_suppressed() -> None:
    service = CommandService(TelegramBot())
    packet = {
        "category": "Soccer",
        "event_status": "EVENT_STATUS_IN_PROGRESS",
    }

    assert service.recommendations.market_research._is_bad_live_sports_new_pick(packet, 0.01)
    assert not service.recommendations.market_research._is_bad_live_sports_new_pick(packet, 0.45)


def test_prediction_buying_power_prefers_main_individual_investing_account() -> None:
    service = CommandService(TelegramBot())
    market = service.recommendations.market_research
    market.settings = market.settings.model_copy(update={"primary_portfolio_account_external_id": "test-main"})
    with get_session() as session:
        main = Account(external_id="test-main", provider="snaptrade", account_type="individual", name="Individual")
        savings = Account(external_id="test-savings", provider="snaptrade", account_type="cash", name="Savings")
        agentic = Account(external_id="test-agentic", provider="snaptrade", account_type="individual", name="Agentic")
        crypto = Account(external_id="test-crypto", provider="snaptrade", account_type="crypto", name="Crypto")
        for account in (main, savings, agentic, crypto):
            session.add(account)
        session.commit()
        for account in (main, savings, agentic, crypto):
            session.refresh(account)
        # Deliberately different round balances prove the primary account wins.
        session.add(AccountSnapshot(account_id=main.id, cash_balance=500.0, buying_power=500.0, total_value=1000.0))
        session.add(AccountSnapshot(account_id=savings.id, cash_balance=3000.0, buying_power=3000.0, total_value=3000.0))
        session.add(AccountSnapshot(account_id=agentic.id, cash_balance=1500.0, buying_power=1500.0, total_value=2000.0))
        session.add(AccountSnapshot(account_id=crypto.id, cash_balance=0.0, buying_power=0.0, total_value=0.0))
        session.add(Holding(account_id=main.id, symbol="AAA", quantity=5.0, market_value=500.0, asset_type="equity"))
        session.add(Holding(account_id=agentic.id, symbol="BBB", quantity=5.0, market_value=500.0, asset_type="equity"))
        session.commit()
        assert market._current_buying_power(session) == 500.0


@pytest.mark.asyncio
async def test_medium_event_position_uses_auto_manage_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = TelegramBot()
    service = CommandService(bot)

    async def fake_refresh_outcomes(session):  # noqa: ANN001
        return None

    async def fake_refresh_watch(session):  # noqa: ANN001
        return {}

    async def fake_quotes(contract_ids):  # noqa: ANN001
        return {"contract-1": {"yes_bid_price": "0.40", "yes_ask_price": "0.42"}}

    def fake_packet(session, position, quote, watch):  # noqa: ANN001
        return {
            "yes_price": 0.40,
            "no_price": 0.60,
            "edge_yes": -0.08,
            "edge_no": 0.01,
            "fair_yes_probability": 0.35,
            "yes_entry_price": 0.42,
            "no_entry_price": 0.60,
        }

    async def fake_auto_close(session, position, quote, packet, watch=None, *, auto=True):  # noqa: ANN001
        return "<b>Finn Auto-Manage: gs</b>\nEvent: medium stake\nAction: <b>cut submitted</b>"

    monkeypatch.setattr(service.recommendations, "_refresh_event_bet_outcomes", fake_refresh_outcomes)
    monkeypatch.setattr(service.recommendations, "refresh_event_watch_state", fake_refresh_watch)
    monkeypatch.setattr(service.recommendations, "_build_position_control_packet", fake_packet)
    monkeypatch.setattr(service.recommendations, "_auto_close_event_position", fake_auto_close)
    monkeypatch.setattr(service.recommendations.market_research.robinhood_prediction, "get_marketdata_quotes", fake_quotes)

    with get_session() as session:
        session.add(
            EventPosition(
                event_code="event-1",
                side="yes",
                amount_usd=15.00,
                source="robinhood",
                status="broker_filled",
                broker_contract_id="contract-1",
                broker_event_id="event-1",
                quantity=20,
                avg_fill_price=0.75,
                event_title="Medium Stake Event",
                pick_label="Team A",
                category="Soccer",
            )
        )
        session.commit()

        manual_batch = await service.recommendations.process_event_position_controls(session, include_auto=False, include_manual=True)
        assert manual_batch is None

        auto_batch = await service.recommendations.process_event_position_controls(session, include_auto=True, include_manual=False)
        assert auto_batch is not None
        messages = await service.recommendations.format_batch_messages(session, auto_batch.id)
        assert "Finn Auto-Manage: gs" in messages[0]


@pytest.mark.asyncio
async def test_auto_manage_failure_is_not_spammed(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = TelegramBot()
    service = CommandService(bot)

    async def fake_refresh_outcomes(session):  # noqa: ANN001
        return None

    async def fake_refresh_watch(session):  # noqa: ANN001
        return {}

    async def fake_quotes(contract_ids):  # noqa: ANN001
        return {"contract-1": {"yes_bid_price": "0.40", "yes_ask_price": "0.42", "symbol": "KXMLBGAME-26JUN011840DETTB-TB"}}

    def fake_packet(session, position, quote, watch):  # noqa: ANN001
        return {
            "yes_price": 0.40,
            "no_price": 0.60,
            "edge_yes": -0.08,
            "edge_no": 0.01,
            "fair_yes_probability": 0.35,
            "yes_entry_price": 0.42,
            "no_entry_price": 0.60,
        }

    async def fake_order(**kwargs):  # noqa: ANN001
        raise RuntimeError("Robinhood event order rejected (400): {\"detail\":\"position cannot be closed\"}")

    monkeypatch.setattr(service.recommendations, "_refresh_event_bet_outcomes", fake_refresh_outcomes)
    monkeypatch.setattr(service.recommendations, "refresh_event_watch_state", fake_refresh_watch)
    monkeypatch.setattr(service.recommendations, "_build_position_control_packet", fake_packet)
    monkeypatch.setattr(service.recommendations.market_research.robinhood_prediction, "get_marketdata_quotes", fake_quotes)
    monkeypatch.setattr(service.recommendations.market_research.robinhood_prediction, "create_event_contract_order", fake_order)

    with get_session() as session:
        session.add(
            EventPosition(
                event_code="event-1",
                side="yes",
                amount_usd=2.50,
                source="robinhood",
                status="broker_filled",
                broker_contract_id="contract-1",
                broker_event_id="event-1",
                quantity=5,
                avg_fill_price=0.50,
                event_title="Kansas City vs Minnesota",
                pick_label="Minnesota",
                category="Baseball",
                time_label="4:40 PM",
            )
        )
        session.commit()

        first = await service.recommendations.process_event_position_controls(session, include_auto=True, include_manual=False)
        assert first is not None
        first_messages = await service.recommendations.format_batch_messages(session, first.id)
        assert "cut failed" in first_messages[0]
        assert "position cannot be closed" in first_messages[0]

        second = await service.recommendations.process_event_position_controls(session, include_auto=True, include_manual=False)
        assert second is None


@pytest.mark.asyncio
async def test_auto_manage_code9_failure_is_humanized_and_not_spammed(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = TelegramBot()
    service = CommandService(bot)

    async def fake_refresh_outcomes(session):  # noqa: ANN001
        return None

    async def fake_refresh_watch(session):  # noqa: ANN001
        return {}

    async def fake_quotes(contract_ids):  # noqa: ANN001
        return {"contract-1": {"yes_bid_price": "0.40", "yes_ask_price": "0.42", "symbol": "KXTEST-YES"}}

    def fake_packet(session, position, quote, watch):  # noqa: ANN001
        return {
            "yes_price": 0.40,
            "no_price": 0.60,
            "edge_yes": -0.08,
            "edge_no": 0.01,
            "fair_yes_probability": 0.35,
            "yes_entry_price": 0.50,
            "no_entry_price": 0.60,
        }

    async def fake_order(**kwargs):  # noqa: ANN001
        raise RuntimeError(
            'Robinhood event order rejected (400): {"code":9,"message":"There was an error with this order. Try again.","details":[]}'
        )

    monkeypatch.setattr(service.recommendations, "_refresh_event_bet_outcomes", fake_refresh_outcomes)
    monkeypatch.setattr(service.recommendations, "refresh_event_watch_state", fake_refresh_watch)
    monkeypatch.setattr(service.recommendations, "_build_position_control_packet", fake_packet)
    monkeypatch.setattr(service.recommendations.market_research.robinhood_prediction, "get_marketdata_quotes", fake_quotes)
    monkeypatch.setattr(service.recommendations.market_research.robinhood_prediction, "create_event_contract_order", fake_order)

    with get_session() as session:
        session.add(
            EventPosition(
                event_code="event-1",
                side="yes",
                amount_usd=4.73,
                source="robinhood",
                status="broker_filled",
                broker_contract_id="contract-1",
                broker_event_id="event-1",
                quantity=11,
                avg_fill_price=0.43,
                event_title="2026 Group G Belgium vs Egypt",
                pick_label="Egypt",
                category="Soccer",
                time_label="12:00 PM",
            )
        )
        session.commit()

        first = await service.recommendations.process_event_position_controls(session, include_auto=True, include_manual=False)
        assert first is not None
        first_messages = await service.recommendations.format_batch_messages(session, first.id)
        assert "cut failed" in first_messages[0]
        assert "Robinhood did not give a specific reason" in first_messages[0]
        assert "locked" in first_messages[0]

        second = await service.recommendations.process_event_position_controls(session, include_auto=True, include_manual=False)
        assert second is None


@pytest.mark.asyncio
async def test_user_approved_cut_uses_position_alert_label(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = TelegramBot()
    service = CommandService(bot)

    async def fake_order(**kwargs):  # noqa: ANN001
        return {
            "orderId": "close-order-1",
            "filledQuantity": "8",
            "averagePrice": "0.68",
            "derivedState": "FILLED",
        }

    async def fake_account():  # noqa: ANN001
        return {}

    monkeypatch.setattr(service.recommendations.market_research.robinhood_prediction, "create_event_contract_order", fake_order)
    monkeypatch.setattr(service.recommendations.market_research.robinhood_prediction, "get_event_contract_account", fake_account)

    with get_session() as session:
        position = EventPosition(
            event_code="event-1",
            side="yes",
            amount_usd=6.88,
            source="robinhood",
            status="broker_filled",
            broker_contract_id="contract-1",
            broker_event_id="event-1",
            quantity=8,
            avg_fill_price=0.61,
            event_title="Los Angeles D vs Arizona",
            pick_label="Los Angeles D",
            category="Baseball",
            time_label="6:40 PM",
        )
        session.add(position)
        session.commit()
        session.refresh(position)

        message = await service.recommendations._auto_close_event_position(
            session,
            position,
            {"yes_bid_price": "0.68", "yes_ask_price": "0.69"},
            {
                "yes_price": 0.68,
                "no_price": 0.32,
                "edge_yes": -0.01,
                "edge_no": 0.01,
                "fair_yes_probability": 0.67,
            },
            auto=False,
        )

        assert "Finn Position Alert: g" in message
        assert "Finn Auto-Manage: gs" not in message
        assert "Entry: 61.0%" in message
        assert "Live exit market: 68.0%" in message
        assert session.get(EventPosition, position.id).status == "settled_loss"


@pytest.mark.asyncio
async def test_position_alert_ignores_already_settled_position() -> None:
    bot = TelegramBot()
    service = CommandService(bot)

    with get_session() as session:
        position = EventPosition(
            event_code="event-1",
            side="yes",
            amount_usd=6.88,
            source="robinhood",
            status="settled_loss",
            broker_contract_id="contract-1",
            broker_event_id="event-1",
            quantity=8,
            avg_fill_price=0.61,
            event_title="Los Angeles D vs Arizona",
            pick_label="Los Angeles D",
        )
        session.add(position)
        session.commit()
        session.refresh(position)

        batch = RecommendationBatch(batch_type="event-control")
        session.add(batch)
        session.commit()
        session.refresh(batch)
        recommendation = Recommendation(
            batch_id=batch.id,
            item_number=1,
            recommendation_type="event",
            symbol=f"Los Angeles D vs Arizona || Los Angeles D || Baseball || 6:40 PM || Main market || KX || event-1 || contract-1 || || {position.id} ||",
            event_code="event-1",
            side="yes",
            amount_usd=6.88,
            reason="Finn recommends CUT for this live position.",
            risk="medium",
            execution_mode="manual",
            valid_until=batch.created_at,
        )
        session.add(recommendation)
        session.commit()

        result = await service.recommendations.execute_position_control_recommendations(session, [recommendation])

        assert result == "No executable live position changes remained."
        assert session.get(Recommendation, recommendation.id).status == "failed"


def test_position_alert_reason_bits_keep_decimal_values() -> None:
    service = CommandService(TelegramBot()).recommendations
    reason = (
        "Finn recommends CUT for this live position. Finn is cutting to reduce downside because the live edge no longer justifies holding. "
        "Entry: 86.0%. Live exit market: 44.5%. Finn fair: 42.5%. Edge: -3.5 pts. Move since entry: -41.5 pts. "
        "Status: EVENT_STATUS_IN_PROGRESS. Score: 2 / 1. Scoreboard can look favorable while the Robinhood price/model edge says the risk-adjusted bet is no longer attractive."
    )

    why_text, numbers_text = service._event_reason_bits(reason)

    assert "0%" not in why_text
    assert "5 pts" not in why_text
    assert "Entry 86.0%" in numbers_text
    assert "Live exit market 44.5%" in numbers_text
    assert "Finn fair 42.5%" in numbers_text
    assert "Edge -3.5 pts" in numbers_text
    assert "Move since entry -41.5 pts" in numbers_text
    assert "Finn is cutting to reduce downside" in why_text
