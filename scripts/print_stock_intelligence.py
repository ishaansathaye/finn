"""Sanitized persisted research health. Does not contact brokers or place orders."""
import json

from app.db import get_session
from app.services.market_cache import MarketCacheService
from app.services.stock_advisor import StockAdvisorService


def main() -> None:
    with get_session() as session:
        cache = MarketCacheService()
        advisor = StockAdvisorService()
        latest = cache.get_json_stale(session, "stock_intelligence", "latest") or {}
        sectors = {}
        for packet in latest.get("packets", []):
            sector = advisor.sector_for_symbol(packet["symbol"])
            sectors.setdefault(sector, []).append(packet["symbol"])
        print(json.dumps({
            "as_of": latest.get("as_of"), "analysis_mode": latest.get("analysis_mode"),
            "universe_count": latest.get("universe_count"), "analyzed_sectors": sectors,
            "llm_health": cache.get_json_stale(session, "stock_intelligence", "llm_health"),
            "providers": {name: advisor._global_news_provider_health(session, name)
                          for name in ("alpha_vantage", "fmp")},
            "evaluation_samples": len(cache.get_json_stale(session, "stock_intelligence", "evaluation_samples") or []),
            "portfolio_strategy": cache.get_json_stale(session, "stock_strategy", "latest"),
        }, indent=2))


if __name__ == "__main__":
    main()
