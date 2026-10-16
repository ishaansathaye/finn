from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import (
    StockListenerCheckpoint,
    StockNewsItem,
    StockSourceReliability,
    StockUniverseMember,
)
from app.services.stock_advisor import StockAdvisorService


NOW = datetime(2026, 10, 7, 12)
SECRET = "test-secret-never-persist"


def service(now=NOW):
    instance = StockAdvisorService.__new__(StockAdvisorService)
    instance.settings = SimpleNamespace(
        alpha_vantage_api_key=SECRET, fmp_api_key=SECRET,
        discovery_symbols=["MSFT"], starter_symbols=[], core_stock_symbols="",
    )
    instance._now = lambda: now
    return instance


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'provider.db'}")
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


def mock_http(monkeypatch, replies):
    calls = []
    real_client = httpx.AsyncClient

    def handle(request):
        calls.append(request.url.path)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        status, body = reply
        if isinstance(body, bytes):
            return httpx.Response(status, content=body)
        return httpx.Response(status, json=body)

    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    return calls


def run(instance, session, provider):
    return asyncio.run(getattr(instance, f"_store_{provider}_global_news")(session))


def health(session, provider):
    row = session.exec(select(StockListenerCheckpoint).where(
        StockListenerCheckpoint.provider == "stock_global_news",
        StockListenerCheckpoint.key == provider,
    )).one()
    return json.loads(row.cursor)


@pytest.mark.parametrize("provider,status,body,expected,hours", [
    ("alpha_vantage", 200, {"Information": f"Premium endpoint {SECRET}"}, "blocked", 24),
    ("alpha_vantage", 200, {"Note": f"API call frequency exceeded {SECRET}"}, "rate_limited", 6),
    ("alpha_vantage", 200, {"Information": f"API rate limit reached; premium plans {SECRET}"}, "rate_limited", 6),
    ("alpha_vantage", 200, {"Error Message": f"Invalid API key {SECRET}"}, "blocked", 24),
    ("alpha_vantage", 200, {"Information": SECRET}, "unavailable", 6),
    ("fmp", 429, {"error": SECRET}, "rate_limited", 6),
    ("fmp", 403, {"error": SECRET}, "blocked", 24),
    ("fmp", 200, {"Error Message": f"Premium subscription required {SECRET}"}, "blocked", 24),
    ("fmp", 200, {"error": f"Too many requests {SECRET}"}, "rate_limited", 6),
    ("fmp", 503, {"error": SECRET}, "unavailable", 6),
    ("fmp", 200, b"not-json", "unavailable", 6),
    ("alpha_vantage", 200, {"feed": "invalid"}, "unavailable", 6),
    ("fmp", 200, [None], "unavailable", 6),
    ("alpha_vantage", 200, {"feed": []}, "empty", 1),
    ("fmp", 200, [], "empty", 1),
])
def test_health_is_sanitized_persistent_and_recovers(
    monkeypatch, engine, provider, status, body, expected, hours,
):
    empty = {"feed": []} if provider == "alpha_vantage" else []
    calls = mock_http(monkeypatch, [(status, body), (200, empty)])
    with Session(engine) as session:
        assert run(service(), session, provider) == 0
        state = health(session, provider)
        assert state["status"] == expected
        assert state["next_retry_at"] == (NOW + timedelta(hours=hours)).isoformat()
        assert SECRET not in json.dumps(state)
        assert len(calls) == 1
        assert not session.exec(select(StockSourceReliability)).all()
    engine.dispose()
    with Session(engine) as session:
        assert run(service(NOW + timedelta(hours=hours, microseconds=-1)), session, provider) == 0
        assert len(calls) == 1
        assert health(session, provider) == state
        assert run(service(NOW + timedelta(hours=hours)), session, provider) == 0
        assert len(calls) == 2
        assert health(session, provider)["status"] == "empty"


@pytest.mark.parametrize("provider", ["alpha_vantage", "fmp"])
def test_transport_failure_is_cached_without_exception_text(monkeypatch, engine, provider):
    calls = mock_http(monkeypatch, [httpx.ReadTimeout(f"https://example.com?apikey={SECRET}")])
    with Session(engine) as session:
        assert run(service(), session, provider) == 0
        state = health(session, provider)
        assert state["status"] == "unavailable"
        assert SECRET not in json.dumps(state)
        assert run(service(), session, provider) == 0
        assert len(calls) == 1


@pytest.mark.parametrize("provider", ["alpha_vantage", "fmp"])
def test_success_preserves_discoveries_and_publication_across_failures(monkeypatch, engine, provider):
    item = {
        "title": "MSFT raises guidance", "summary": "Improved outlook",
        "url": "https://example.com/news", "symbol": "MSFT",
        "publishedDate": "2026-10-07 10:00:00", "time_published": "20261007T100000",
        "ticker_sentiment": [{"ticker": "MSFT", "relevance_score": "0.9"}],
    }
    updated = {k: v for k, v in item.items() if k not in {"publishedDate", "time_published"}}
    def wrap(row):
        return {"feed": [row]} if provider == "alpha_vantage" else [row]

    calls = mock_http(monkeypatch, [(200, wrap(item)), (429, {}), (200, wrap(updated))])
    with Session(engine) as session:
        assert run(service(), session, provider) >= 1
        state = health(session, provider)
        assert state["status"] == "healthy"
        assert state["next_retry_at"] == (NOW + timedelta(hours=1)).isoformat()
        news = session.exec(select(StockNewsItem)).one()
        news_id, created_at = news.id, news.created_at
        assert news.published_at == datetime(2026, 10, 7, 10)
        members = {row.id for row in session.exec(select(StockUniverseMember)).all()}
        reliability = session.exec(select(StockSourceReliability)).one().updated_at
        assert run(service(NOW + timedelta(minutes=5)), session, provider) == 0
        assert len(calls) == 1
        assert run(service(NOW + timedelta(hours=1)), session, provider) == 0
        assert health(session, provider)["status"] == "rate_limited"
        assert health(session, provider)["last_success_at"] == NOW.isoformat()
        assert session.exec(select(StockSourceReliability)).one().updated_at == reliability
        assert {row.id for row in session.exec(select(StockUniverseMember)).all()} == members
        run(service(NOW + timedelta(hours=7)), session, provider)
        assert health(session, provider)["status"] == "healthy"
        news = session.exec(select(StockNewsItem)).one()
        assert (news.id, news.created_at, news.published_at) == (news_id, created_at, datetime(2026, 10, 7, 10))


def test_fmp_missing_endpoint_fallback_stops_at_rate_limit(monkeypatch, engine):
    calls = mock_http(monkeypatch, [(404, {}), (429, {})])
    with Session(engine) as session:
        assert run(service(), session, "fmp") == 0
        assert calls == ["/stable/news/stock-latest", "/api/v3/stock_news"]
        assert health(session, "fmp")["status"] == "rate_limited"


@pytest.mark.parametrize("status,body,expected", [
    (429, {}, "rate_limited"),
    (200, {"Error Message": f"Premium required {SECRET}"}, "blocked"),
    (200, [], "empty"),
    (200, [{"symbol": "MSFT", "changePercentage": 1.2, "timestamp": 1791367200}], "healthy"),
])
def test_quote_fallback_preserves_health(monkeypatch, engine, status, body, expected):
    calls = mock_http(monkeypatch, [(404, {}), (410, {}), (status, body)])
    with Session(engine) as session:
        run(service(), session, "fmp")
        assert len(calls) == 3
        state = health(session, "fmp")
        assert state["status"] == expected
        assert SECRET not in json.dumps(state)
        assert run(service(NOW + timedelta(minutes=5)), session, "fmp") == 0
        assert len(calls) == 3
        if expected == "healthy":
            assert session.exec(select(StockNewsItem)).one().published_at is not None


@pytest.mark.parametrize("provider", ["alpha_vantage", "fmp"])
@pytest.mark.parametrize("cursor", ["broken-json", "[]", '{"stored_at":"2026-10-07T11:59:00"}'])
def test_older_or_corrupt_checkpoint_does_not_block_provider(monkeypatch, engine, provider, cursor):
    empty = {"feed": []} if provider == "alpha_vantage" else []
    calls = mock_http(monkeypatch, [(200, empty)])
    with Session(engine) as session:
        session.add(StockListenerCheckpoint(provider="stock_global_news", key=provider, cursor=cursor))
        session.commit()
        assert run(service(), session, provider) == 0
        assert health(session, provider)["status"] == "empty"
        assert len(calls) == 1


@pytest.mark.parametrize("provider", ["alpha_vantage", "fmp"])
def test_missing_credentials_are_blocked_without_requests(monkeypatch, engine, provider):
    calls = mock_http(monkeypatch, [])
    instance = service()
    setattr(instance.settings, f"{provider}_api_key", "")
    with Session(engine) as session:
        assert run(instance, session, provider) == 0
        assert health(session, provider)["status"] == "blocked"
        assert health(session, provider)["reason"] == "missing_api_key"
        assert not calls


def test_sector_mapping_does_not_inject_discovery_seeds():
    instance = service()
    for sector, symbols in {
        "Real Estate": ["PLD", "AMT", "EQIX", "XLRE"],
        "Utilities": ["DUK", "SO", "AEP", "XLU"],
        "Communication Services": ["DIS", "TMUS", "VZ", "T", "CMCSA"],
    }.items():
        assert all(instance.sector_for_symbol(symbol) == sector for symbol in symbols)
    assert instance._diversified_discovery_symbols() == ["MSFT"]
