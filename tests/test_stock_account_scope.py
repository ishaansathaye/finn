from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import SQLModel, Session, create_engine

from app.models import Account, AccountSnapshot, Holding
from app.services.market_research import MarketResearchService
from app.services.stock_advisor import StockAdvisorService


@pytest.fixture
def scope():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for key in ("main", "agent", "savings", "old"):
            session.add(Account(id=key, external_id=key, provider="snaptrade", account_type="INDIVIDUAL", name=key))
            session.add(AccountSnapshot(account_id=key, total_value=10000, buying_power=100 if key == "main" else 9000,
                                        as_of=datetime.now(UTC).replace(tzinfo=None)-timedelta(days=100 if key == "old" else 0)))
        session.add(Holding(account_id="main", symbol="MSFT", quantity=2, market_value=1000))
        session.add(Holding(account_id="agent", symbol="RKLB", quantity=2, market_value=120))
        session.add(Holding(account_id="agent", symbol="MSFT", quantity=20, market_value=10000))
        session.add(Holding(account_id="old", symbol="OLD", quantity=5, market_value=500))
        session.commit()
        service = MarketResearchService()
        service.settings = service.settings.model_copy(update={"primary_portfolio_account_external_id": "main"})
        yield session, service


def test_primary_holdings_and_cash_never_mix(scope):
    session, service = scope
    assert service._portfolio_symbols(session) == ["MSFT"]
    assert set(service._position_map(session)) == {"MSFT"}
    assert service._position_map(session)["MSFT"].quantity == 2
    assert service._holding_value(session, "RKLB") == 0
    assert service._holding_weight(session, "MSFT") == .1
    assert service._current_buying_power(session) == 100
    assert service._portfolio_context(session)["top_holdings"][0]["symbol"] == "MSFT"


@pytest.mark.parametrize("external", ["missing", None])
def test_missing_or_ambiguous_primary_fails_closed(scope, external):
    session, service = scope
    service.settings = service.settings.model_copy(update={"primary_portfolio_account_external_id": external})
    assert service._latest_snapshot(session) is None
    assert service._position_map(session) == {}
    assert service._current_buying_power(session) == 0


def test_policies_only_infer_from_primary(scope):
    session, service = scope
    advisor = StockAdvisorService(service)
    rows = advisor.infer_position_policies(session)
    assert {row.symbol for row in rows} == {"MSFT"}


def test_portfolio_display_only_primary(scope):
    session, service = scope
    lines = '\n'.join(service.portfolio_lines(session))
    assert 'RKLB' not in lines and 'OLD' not in lines
    assert 'Tracked holdings (including crypto): 1' in lines


def test_configured_stale_account_does_not_switch_to_funded_agent(scope):
    session, service = scope
    service.settings = service.settings.model_copy(update={"primary_portfolio_account_external_id": "old"})
    assert service._position_map(session) == {}
    assert service._current_buying_power(session) == 0
