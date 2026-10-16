from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlmodel import Session, SQLModel, create_engine, select

from app.adapters.google_news import GoogleNewsAdapter
from app.models import StockFilingItem, StockUniverseMember
from app.services.market_research import MarketResearchService
from app.services.stock_advisor import StockAdvisorService


def test_google_news_fallback_keeps_only_fresh_matching_headlines() -> None:
    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    fresh = (now - timedelta(hours=2)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    stale = (now - timedelta(days=4)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    feed = f"""
    <rss><channel>
      <item><title>MSFT raises guidance</title><pubDate>{fresh}</pubDate><link>https://example.com/1</link><source>Example</source></item>
      <item><title>MSFT old report</title><pubDate>{stale}</pubDate></item>
      <item><title>AMD raises guidance</title><pubDate>{fresh}</pubDate></item>
      <item><title>MSFT undated</title></item>
    </channel></rss>
    """.encode()
    items = GoogleNewsAdapter().parse_rss(feed, "MSFT", 5, now=now)
    assert [item["title"] for item in items] == ["MSFT raises guidance"]
    assert items[0]["provider"] == "Example"
    assert GoogleNewsAdapter().parse_rss(b"bad xml", "MSFT", 5, now=now) == []


def test_empty_yahoo_news_uses_fallback(monkeypatch) -> None:
    service = MarketResearchService()

    async def no_yahoo_news(_symbol, *, limit):
        return []

    async def rss_news(_symbol, *, limit):
        return [{"title": "MSFT raises guidance", "published_at": datetime.now(UTC).isoformat()}]

    class Cache:
        key = None

        def get_json(self, *_args, **_kwargs):
            self.key = _args[2]
            return None

        def set_json(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(service.yfinance, "get_news", no_yahoo_news)
    monkeypatch.setattr(service.google_news, "get_news", rss_news)
    service.cache = Cache()
    assert asyncio.run(service._get_news_items(None, "MSFT"))[0]["title"] == "MSFT raises guidance"
    assert service.cache.key == "news:v2:MSFT"


def test_stale_filing_and_news_do_not_create_catalyst() -> None:
    service = MarketResearchService()
    old = (datetime.now(UTC) - timedelta(days=30)).date().isoformat()
    fresh = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    filing = {"filing_date": old, "signals": ["insider filing"], "signal_score": 2.0}
    assert service._fresh_filings([filing]) == []
    assert service._fresh_filings([{**filing, "filing_date": fresh}])
    assert service._catalyst_signal({"filings": [filing], "catalysts": ["insider filing"]}, 0, [], [], []) == (0, [], [])
    # A vague delay without operating context is not a measured negative catalyst.
    assert service._news_signal([{"title": "Chair said there is a delay", "published_at": datetime.now(UTC).isoformat()}])[0] == 0.0
    assert service._news_signal([{"title": "AI partnership", "published_at": old}]) == (0.0, [], [])


def test_listener_rotates_through_all_symbols() -> None:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        service = StockAdvisorService()
        universe = ["AAA", "BBB", "CCC", "DDD", "EEE"]
        assert service._rotating_symbols(session, universe, 2, "news") == ["AAA", "BBB"]
        assert service._rotating_symbols(session, universe, 2, "news") == ["CCC", "DDD"]
        assert service._rotating_symbols(session, universe, 2, "news") == ["EEE", "AAA"]
        assert service._rotating_symbols(session, universe, 2, "filings") == ["AAA", "BBB"]


def test_listener_prioritizes_diverse_configured_symbols_over_unverified_dynamic_symbols() -> None:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(StockUniverseMember(symbol="FAKE", source="global_news_text", status="active", confidence=1.0))
        session.commit()
        service = StockAdvisorService()
        service.settings = SimpleNamespace(
            core_stock_symbols="",
            starter_symbols=[],
            discovery_symbols=["NVDA", "AMD", "XOM", "LLY", "JPM"],
            stock_listener_universe_limit=4,
        )
        assert service._listener_universe(session) == ["NVDA", "XOM", "LLY", "JPM"]


def test_stock_filing_store_parses_sec_date() -> None:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        service = StockAdvisorService()

        async def sync_symbol(_session, _symbol, *, limit):
            return {"filings": [{
                "accession_number": "0000000000-26-000001",
                "form_type": "8-K",
                "filing_date": "2026-10-06T00:00:00",
                "filing_url": "https://example.com/filing",
                "signals": ["earnings release"],
                "risk_flags": [],
            }]}

        service.market_research.sec.sync_symbol = sync_symbol
        assert asyncio.run(service._store_filings_for_symbol(session, "MSFT")) == 1
        row = session.exec(select(StockFilingItem)).one()
        assert row.accession_number == "0000000000-26-000001"
        assert row.filing_date.date().isoformat() == "2026-10-06"
