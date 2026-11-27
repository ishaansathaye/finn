from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlmodel import SQLModel, Session, create_engine, select

from app.models import EventBetOutcome, EventPosition, Recommendation
from app.services.event_exit_policy import GameEvidence
from app.services.recommendation_service import RecommendationService
import app.services.recommendation_service as control


@pytest.fixture
def setup(monkeypatch):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        svc = RecommendationService()
        adapter = svc.market_research.robinhood_prediction
        t = [datetime.now(UTC)]

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return t[0] if tz else t[0].replace(tzinfo=None)

        monkeypatch.setattr(control, "datetime", Clock)
        pos = EventPosition(amount_usd=3., quantity=5, avg_fill_price=.60, side="yes", event_code="fixture",
                            broker_contract_id="contract", broker_account_id="account", source="robinhood",
                            status="broker_open", event_title="Home vs Away", pick_label="Home",
                            opened_at=(t[0]-timedelta(minutes=10)).replace(tzinfo=None))
        db.add(pos)
        db.commit()
        book = {"yes_bid_price":.52,"yes_ask_price":.54,"symbol":"synthetic"}
        monkeypatch.setattr(svc, "_refresh_event_bet_outcomes", AsyncMock())
        monkeypatch.setattr(svc, "refresh_event_watch_state", AsyncMock(return_value={}))
        monkeypatch.setattr(adapter, "get_marketdata_quotes", AsyncMock(return_value={"contract":book}))
        monkeypatch.setattr(adapter, "get_event_contract_account", AsyncMock(return_value={"id":"account"}))
        monkeypatch.setattr(adapter, "list_event_positions", AsyncMock(return_value=[{"contractId":"contract","quantity":5,"side":"YES"}]))
        monkeypatch.setattr(adapter, "create_event_contract_order", AsyncMock(return_value={
            "id":"close","filledQuantity":"5","averagePrice":".52","derivedState":"FILLED"}))
        monkeypatch.setattr(svc, "_build_position_control_packet", lambda *a: {"yes_price":.53, "edge_yes":-.03,
                            "fair_yes_probability":.55, "edge_no":.0, "no_price":.47})
        def evidence(_symbol):
            return GameEvidence(.32, t[0].timestamp(), t[0].isoformat(), "Q3 5:01, Away possession")
        monkeypatch.setattr(svc.exit_evidence, "get", AsyncMock(side_effect=evidence))
        yield db, svc, adapter, pos, book, t
    engine.dispose()


@pytest.mark.asyncio
async def test_small_confirmed_bad_hold_really_exits_and_audits(setup):
    db, svc, adapter, pos, book, t = setup
    assert await svc.process_event_position_controls(db) is None
    adapter.create_event_contract_order.assert_not_awaited()
    t[0] += timedelta(seconds=25)
    batch = await svc.process_event_position_controls(db)
    assert batch.batch_type == "event-control-auto"
    assert adapter.create_event_contract_order.await_args.kwargs["quantity"] == 5
    assert adapter.create_event_contract_order.await_args.kwargs["limit_price"] == book["yes_bid_price"]
    assert db.get(EventPosition,pos.id).status == "settled_loss"
    assert "1 unresolved" in await svc._exit_review_summary(db)
    assert await svc.process_event_position_controls(db) is None
    assert adapter.create_event_contract_order.await_count == 1


@pytest.mark.asyncio
async def test_large_bet_alert_then_recovery_cancels_sale(setup):
    db, svc, adapter, pos, _, t = setup
    pos.amount_usd = 6.
    pos.quantity = 10
    db.add(pos)
    db.commit()
    await svc.process_event_position_controls(db)
    t[0] += timedelta(seconds=25)
    batch = await svc.process_event_position_controls(db)
    assert batch.batch_type == "event-control"
    rec = db.exec(select(Recommendation).where(Recommendation.batch_id == batch.id)).one()
    svc.exit_evidence.get.side_effect = lambda _: GameEvidence(.8, t[0].timestamp(), "new-play", "Home recovered")
    assert "no longer supports" in await svc.execute_position_control_recommendations(db,[rec])
    assert rec.status == "skipped"
    adapter.create_event_contract_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_exit_accounts_only_for_filled_contracts(setup):
    db, svc, adapter, pos, book, _ = setup
    adapter.create_event_contract_order.return_value = {"id":"order","filledQuantity":"2","averagePrice":".52","derivedState":"CANCELED"}
    msg = await svc._auto_close_event_position(db,pos,book,{"exit_reason":"Confirmed"})
    assert "partially filled" in msg
    assert pos.status == "broker_open" and pos.quantity == 3 and pos.amount_usd == 1.80
    row = db.exec(select(EventBetOutcome)).one()
    assert row.event_position_id != pos.id
    assert row.profit_usd == -.16 and row.payout_usd == 1.04
    adapter.get_event_order = AsyncMock(return_value=adapter.create_event_contract_order.return_value)
    # Broker still reports five; do not sell the same inventory twice.
    assert not await svc._verify_exit_inventory(db,pos)
    assert adapter.create_event_contract_order.await_count == 1


@pytest.mark.asyncio
async def test_no_duplicate_order_on_unknown_ack_or_wrong_account(setup):
    db, svc, adapter, pos, book, _ = setup
    adapter.get_event_contract_account.return_value = {"id":"different"}
    assert not await svc._verify_exit_inventory(db,pos)
    adapter.get_event_contract_account.return_value = {"id":"account"}
    adapter.create_event_contract_order.side_effect = TimeoutError("unknown acknowledgement")
    await svc._auto_close_event_position(db,pos,book,{"exit_reason":"Confirmed"})
    msg = await svc._auto_close_event_position(db,pos,book,{"exit_reason":"Confirmed"})
    assert "not reconciled" in msg and adapter.create_event_contract_order.await_count == 1


@pytest.mark.asyncio
async def test_severe_unverified_move_requests_y_even_for_small_bet(setup):
    db, svc, adapter, pos, book, t = setup
    svc.exit_evidence.get.side_effect = None
    svc.exit_evidence.get.return_value = None
    book.update(yes_bid_price=.40,yes_ask_price=.42)
    await svc.process_event_position_controls(db)
    t[0] += timedelta(seconds=25)
    book["yes_bid_price"] = .39
    batch = await svc.process_event_position_controls(db)
    assert batch.batch_type == "event-control"
    rec = db.exec(select(Recommendation).where(Recommendation.batch_id == batch.id)).one()
    assert "Independent game evidence is unavailable" in rec.reason
    adapter.create_event_contract_order.assert_not_awaited()
    msg = await svc.execute_position_control_recommendations(db,[rec])
    assert "cut filled" in msg and rec.status == "executed"
    assert adapter.create_event_contract_order.await_count == 1


@pytest.mark.asyncio
async def test_approved_flip_cannot_open_other_side_on_unfilled_exit(setup):
    db, svc, adapter, pos, book, t = setup
    pos.amount_usd = 6.
    db.add(pos)
    db.commit()
    svc._build_position_control_packet = lambda *a: {"yes_price":.53, "fair_yes_probability":.55, "edge_no":.30}
    follow = AsyncMock(return_value="Opened opposite side")
    svc._open_flipped_event_position = follow
    adapter.create_event_contract_order.return_value = {"id":"order","filledQuantity":"0","derivedState":"CANCELED"}
    await svc.process_event_position_controls(db)
    t[0] += timedelta(seconds=25)
    batch = await svc.process_event_position_controls(db)
    rec = db.exec(select(Recommendation).where(Recommendation.batch_id == batch.id)).one()
    assert "recommends FLIP" in rec.reason
    assert "cut not filled" in await svc.execute_position_control_recommendations(db,[rec])
    follow.assert_not_awaited()


@pytest.mark.asyncio
async def test_clock_expired_approval_does_not_touch_broker(setup):
    db, svc, adapter, pos, _, t = setup
    pos.amount_usd = 6.
    db.add(pos)
    db.commit()
    await svc.process_event_position_controls(db)
    t[0] += timedelta(seconds=25)
    batch = await svc.process_event_position_controls(db)
    rec = db.exec(select(Recommendation).where(Recommendation.batch_id == batch.id)).one()
    t[0] = rec.valid_until.replace(tzinfo=UTC) + timedelta(seconds=1)
    assert "No executable" in await svc.execute_position_control_recommendations(db,[rec])
    adapter.create_event_contract_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_shadow_result_not_inferred_from_early_close_pnl(setup):
    db, svc, _, pos, _, t = setup
    await svc.process_event_position_controls(db)
    t[0] += timedelta(seconds=25)
    await svc.process_event_position_controls(db)
    assert "1 unresolved" in await svc._exit_review_summary(db)
    row = db.exec(select(EventBetOutcome)).one()
    row.notes = "Robinhood settlement side=YES"
    row.settlement_price = 0.
    db.add(row)
    db.commit()
    assert "1 better, 0 worse" in await svc._exit_review_summary(db)


def test_cut_losses_are_real_losses_but_not_wrong_picks(setup):
    db, svc, *_ = setup
    for i in range(5):
        db.add(EventBetOutcome(event_position_id=str(i), event_code="example", outcome="lost",
               notes="Finn user-approved cut due to adverse live edge", implied_probability=.7, profit_usd=-2.))
    db.commit()
    _, _, reasons = svc.market_research._event_learning_adjustment(db, category="Football", probability=.7)
    assert not any("odds bucket" in r for r in reasons)
    assert any("global event performance weak" in r for r in reasons)


@pytest.mark.asyncio
async def test_inventory_never_guesses_a_direction_or_uses_an_unfilled_order(setup):
    db, svc, adapter, pos, _, _ = setup
    adapter.list_event_positions.return_value = [{"contractId":"contract","quantity":5}]
    buy = {"orderLegs":[{"contractId":"contract","orderSide":"BUY"}],"createdAt":"2025-06-01T10:00Z",
           "filledQuantity":"5","positionEffectAtPlacementTime":"OPENING"}
    sell = {**buy,"orderLegs":[{"contractId":"contract","orderSide":"SELL"}],"createdAt":"2025-06-01T10:01Z"}
    adapter.list_event_orders = AsyncMock(return_value=[{**sell,"filledQuantity":"0"}])
    assert not await svc._verify_exit_inventory(db,pos)
    adapter.list_event_orders.return_value = [sell,buy]
    assert not await svc._verify_exit_inventory(db,pos)
    adapter.list_event_orders.return_value = [{**sell,"filledQuantity":"0"},buy]
    assert await svc._verify_exit_inventory(db,pos)
