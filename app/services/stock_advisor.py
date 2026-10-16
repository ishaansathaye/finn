from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import escape
import hashlib
import json
import re
from zoneinfo import ZoneInfo

import httpx
from typing import Any

from sqlmodel import Session, select

from app.config import get_settings
from app.models import (
    Holding,
    Recommendation,
    RecommendationBatch,
    RecommendationOutcome,
    StockAlert,
    StockDecisionSnapshot,
    StockFilingItem,
    StockListenerCheckpoint,
    StockNewsItem,
    StockPositionPolicy,
    StockSourceReliability,
    StockUniverseMember,
)
from app.services.market_research import MarketResearchService, StockIdea
from app.services import stock_alert_diagnostics as alert_diagnostics


class StockAdvisorService:
    """Always-on stock research and proactive alert coordinator."""

    def __init__(self, market_research: MarketResearchService | None = None) -> None:
        self.settings = get_settings()
        self.market_research = market_research or MarketResearchService()

    def _now(self) -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)

    def _local_now(self) -> datetime:
        return datetime.now(UTC).astimezone(ZoneInfo(self.settings.local_timezone))

    def _is_regular_market_hours(self, now_local: datetime | None = None) -> bool:
        local_now = now_local or self._local_now()
        if local_now.weekday() >= 5:
            return False
        minutes = local_now.hour * 60 + local_now.minute
        market_open = 6 * 60 + 30
        market_close = 13 * 60
        return market_open <= minutes < market_close

    def _core_symbols(self) -> set[str]:
        raw = getattr(self.settings, "core_stock_symbols", "") or ""
        return {symbol.strip().upper() for symbol in raw.split(",") if symbol.strip()}

    def _configured_symbols(self) -> set[str]:
        symbols: set[str] = set()
        for source in (
            self.settings.starter_symbols,
            self.settings.discovery_symbols,
            sorted(self._core_symbols()),
        ):
            for symbol in source:
                normalized = self._normalize_symbol(str(symbol or ""))
                if normalized:
                    symbols.add(normalized)
        return symbols

    def sector_for_symbol(self, symbol: str) -> str:
        symbol = self._normalize_symbol(symbol)
        if symbol in {"AAPL", "HPE", "INTC", "GFS"}:
            return "Tech / AI"
        if symbol == "XLE":
            return "Energy / Nuclear"
        if symbol == "TSLA":
            return "Consumer / Travel"
        sectors = {
            "Space": {"RKLB", "LUNR", "ASTS", "IRDM", "VSAT", "ARKX"},
            "Energy / Nuclear": {"XOM", "CVX", "COP", "EOG", "FANG", "OXY", "SLB", "LNG", "NEE", "VST", "CEG", "OKLO", "CCJ", "UEC", "URA"},
            "Defense / Aerospace": {"RTX", "LMT", "NOC", "GD", "HII", "KTOS", "AVAV", "GE", "BA", "ITA", "XAR"},
            "Tech / AI": {"NVDA", "AMD", "AVGO", "TSM", "MU", "MRVL", "ARM", "QCOM", "ANET", "DELL", "SMCI", "MSFT", "AMZN", "GOOGL", "GOOG", "META", "NFLX", "UBER", "CRM", "ORCL", "SNOW", "PLTR", "ADBE", "NOW", "CRWD", "NET", "DDOG", "PANW", "ZS", "SNDK", "APP", "TTD", "SHOP", "CSCO", "PENG", "SMH"},
            "Financials": {"JPM", "V", "MA", "AXP", "COIN", "HOOD", "BRK-B", "CB", "AIG", "BLK", "KKR", "SPGI", "ICE", "GS", "XLF"},
            "Healthcare": {"LLY", "NVO", "ISRG", "UNH", "ABBV", "BSX", "TMO", "DHR", "VRTX", "REGN", "XLV", "HIMS"},
            "Industrials": {"ETN", "CAT", "URI", "DE", "EMR", "HON", "XLI"},
            "Materials": {"FCX", "NEM", "SCCO", "LIN", "APD", "ALB", "XLB"},
            "Real Estate": {"PLD", "AMT", "EQIX", "XLRE"},
            "Utilities": {"DUK", "SO", "AEP", "XLU"},
            "Communication Services": {"DIS", "TMUS", "VZ", "T", "CMCSA"},
            "Consumer / Travel": {"COST", "WMT", "ABNB", "BKNG", "HD", "MCD", "XLP", "XLY"},
            "Broad Market / ETFs": {"VTI", "VT", "QQQM", "SPY", "VOO", "QQQ", "DIA", "XLC", "TQQQ", "SOXX", "XSD", "SPMO"},
        }
        for sector, symbols in sectors.items():
            if symbol in symbols:
                return sector
        return "Other Watchlist"

    def _diversified_discovery_symbols(self) -> list[str]:
        sectors: dict[str, list[str]] = {}
        for symbol in self.settings.discovery_symbols:
            sectors.setdefault(self.sector_for_symbol(symbol), []).append(symbol)
        return [
            bucket[index]
            for index in range(max((len(bucket) for bucket in sectors.values()), default=0))
            for bucket in sectors.values()
            if index < len(bucket)
        ]

    def _normalize_symbol(self, value: str) -> str:
        symbol = value.upper().strip().replace("$", "")
        symbol = symbol.replace(".", "-")
        if not re.fullmatch(r"[A-Z][A-Z0-9-]{0,6}", symbol):
            return ""
        blocked = {
            "THE", "AND", "FOR", "CEO", "CFO", "SEC", "USA", "US", "AI", "GDP", "ETF", "IPO", "EPS",
            "FDA", "EV", "OIL", "GAS", "WAR", "FED", "FOMC", "USD", "API", "NEW", "BUY", "SELL",
            "YTD", "PT", "FI", "SOX", "Q1", "Q2", "Q3", "Q4", "CPU", "GPU", "NVIDIA", "TSMC",
            "NYSE", "NASDAQ", "FY2026", "MI455X", "US-I", "IUXX", "ESM26", "DOWI", "SPX",
            "SUV", "EMJ", "CBOE", "STORY", "CRISPR", "GAAP", "EBITDA", "ARK", "R2",
            "PC", "EPYC", "BTC", "ABB", "NEOS", "XL", "FY27", "SPAC", "MY", "SK", "SPDR",
            "CHIPS", "CNBC", "MI450", "ERP", "CRYPTO", "JVC", "CIO",
        }
        return "" if symbol in blocked else symbol

    def _ticker_candidates_from_text(self, text: str, *, trusted: bool = False) -> set[str]:
        candidates: set[str] = set()
        configured = self._configured_symbols()
        pattern = re.compile(r"(?<![A-Z0-9])(?P<prefix>\$)?(?P<symbol>[A-Z][A-Z0-9]{1,5}(?:-[A-Z])?)(?![A-Z0-9])")
        for match in pattern.finditer(text or ""):
            raw = match.group("symbol")
            symbol = self._normalize_symbol(raw)
            if not symbol:
                continue
            has_cash_tag = bool(match.group("prefix"))
            if any(char.isdigit() for char in symbol) and symbol not in configured:
                continue
            if len(symbol.replace("-", "")) > 5 and symbol not in configured:
                continue
            if trusted or has_cash_tag or symbol in configured:
                candidates.add(symbol)
        return candidates

    def _upsert_universe_member(
        self,
        session: Session,
        symbol: str,
        *,
        source: str,
        source_detail: str | None = None,
        confidence: float = 0.5,
        sector_hint: str | None = None,
    ) -> None:
        normalized = self._normalize_symbol(symbol)
        if not normalized:
            return
        row = session.exec(
            select(StockUniverseMember).where(
                StockUniverseMember.symbol == normalized,
                StockUniverseMember.source == source,
            )
        ).first()
        if row is None:
            row = StockUniverseMember(symbol=normalized, source=source)
        row.source_detail = source_detail or row.source_detail
        row.sector_hint = sector_hint or row.sector_hint
        row.confidence = max(float(row.confidence or 0.0), min(1.0, confidence))
        row.status = "active"
        row.last_seen_at = self._now()
        session.add(row)

    def _discover_symbols_from_text(self, session: Session, *, text: str, source: str, confidence: float, detail: str | None = None) -> int:
        count = 0
        trusted = source not in {"global_news_text", "symbol_in_news"}
        for symbol in self._ticker_candidates_from_text(text, trusted=trusted):
            self._upsert_universe_member(session, symbol, source=source, source_detail=detail, confidence=confidence)
            count += 1
        return count

    def _listener_universe(self, session: Session) -> list[str]:
        holdings = self.market_research._primary_holdings(session)
        holdings.sort(key=lambda item: float(item.market_value or 0.0), reverse=True)
        dynamic_rows = list(
            session.exec(
                select(StockUniverseMember)
                .where(StockUniverseMember.status == "active")
                .order_by(StockUniverseMember.confidence.desc(), StockUniverseMember.last_seen_at.desc())
            ).all()
        )
        news_symbols = [row.symbol for row in session.exec(select(StockNewsItem).order_by(StockNewsItem.created_at.desc())).all()[:60]]
        filing_symbols = [row.symbol for row in session.exec(select(StockFilingItem).order_by(StockFilingItem.created_at.desc())).all()[:60]]
        symbols: list[str] = []
        seen: set[str] = set()
        for source in (
            [holding.symbol for holding in holdings if holding.symbol],
            sorted(self._core_symbols()),
            self.settings.starter_symbols,
            self._diversified_discovery_symbols(),
            [row.symbol for row in dynamic_rows],
            news_symbols,
            filing_symbols,
        ):
            for symbol in source:
                normalized = self._normalize_symbol(str(symbol or ""))
                if normalized and normalized not in seen:
                    symbols.append(normalized)
                    seen.add(normalized)
                if len(symbols) >= self.settings.stock_listener_universe_limit:
                    return symbols
        return symbols

    def _position_policy(self, session: Session, symbol: str) -> StockPositionPolicy:
        row = session.exec(select(StockPositionPolicy).where(StockPositionPolicy.symbol == symbol)).first()
        if row is None:
            row = StockPositionPolicy(symbol=symbol, lane="unknown")
        return row

    def _profit_contribution_score(self, holding: Holding) -> tuple[float, float, float]:
        market_value = float(holding.market_value or 0.0)
        cost_basis = float(holding.cost_basis or 0.0)
        unrealized_pnl = market_value - cost_basis if cost_basis > 0 else 0.0
        if market_value <= 0 or cost_basis <= 0:
            return 0.0, 0.0, unrealized_pnl
        return_pct = unrealized_pnl / max(cost_basis, 1.0)
        score = min(1.0, max(0.0, return_pct / 0.35))
        if unrealized_pnl >= 500:
            score = max(score, 0.75)
        elif unrealized_pnl >= 150:
            score = max(score, 0.55)
        return score, return_pct, unrealized_pnl

    def _known_quality_score(self, symbol: str) -> float:
        core_symbols = self._core_symbols()
        durable_leaders = {
            "NVDA", "MRVL", "SNDK", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "AVGO", "TSM",
            "V", "MA", "COST", "BRK-B", "LLY", "UNH", "JPM", "VTI", "VOO", "SPY", "QQQM",
        }
        if symbol in core_symbols:
            return 1.0
        if symbol in durable_leaders:
            return 0.82
        return 0.35

    def _core_score(self, holding: Holding) -> tuple[float, dict[str, float]]:
        symbol = holding.symbol.upper()
        pnl_score, return_pct, unrealized_pnl = self._profit_contribution_score(holding)
        holding_value = float(holding.market_value or 0.0)
        quality_score = self._known_quality_score(symbol)
        size_score = min(1.0, holding_value / 3500.0) if holding_value > 0 else 0.0
        confirmed_bonus = 0.2 if symbol in self._core_symbols() else 0.0
        score = min(1.0, 0.35 * pnl_score + 0.25 * quality_score + 0.20 * size_score + confirmed_bonus + 0.10)
        return score, {
            "pnl_score": round(pnl_score, 4),
            "return_pct": round(return_pct, 4),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "quality_score": round(quality_score, 4),
            "size_score": round(size_score, 4),
        }

    def infer_position_policies(self, session: Session) -> list[StockPositionPolicy]:
        rows: list[StockPositionPolicy] = []
        holdings = self.market_research._primary_holdings(session)
        now = self._now()
        owned = {holding.symbol.upper() for holding in holdings}
        for old in session.exec(select(StockPositionPolicy)).all():
            if old.symbol not in owned and old.symbol not in self._core_symbols() and not old.user_confirmed:
                old.lane = "unknown"
                old.core_score = 0
                old.total_profit_usd = old.unrealized_profit_usd = old.realized_profit_usd = 0
                old.reason_summary = "Not held in the primary investing account; historical inferred policy cleared."
                session.add(old)
        for holding in holdings:
            symbol = str(holding.symbol or "").upper().strip()
            if not symbol:
                continue
            row = self._position_policy(session, symbol)
            score, parts = self._core_score(holding)
            if symbol in self._core_symbols():
                lane = "confirmed_core"
                confirmed = True
            elif score >= self.settings.stock_core_inference_threshold:
                lane = "probable_core"
                confirmed = False
            elif row.lane == "confirmed_core":
                lane = row.lane
                confirmed = row.user_confirmed
            else:
                lane = "tactical"
                confirmed = False
            row.lane = lane
            row.user_confirmed = confirmed
            row.core_score = round(score, 4)
            row.total_profit_usd = parts["unrealized_pnl"]
            row.unrealized_profit_usd = parts["unrealized_pnl"]
            row.realized_profit_usd = 0.0
            row.holding_days = None
            row.reason_summary = self._policy_reason(symbol, lane, parts)
            row.updated_at = now
            session.add(row)
            rows.append(row)
        session.commit()
        return rows

    def _policy_reason(self, symbol: str, lane: str, parts: dict[str, float]) -> str:
        if lane == "confirmed_core":
            return f"User core holding; protect long-term thesis. Unrealized P/L ${parts['unrealized_pnl']:.2f}."
        if lane == "probable_core":
            return f"Probable core from portfolio gains and quality. Unrealized P/L ${parts['unrealized_pnl']:.2f}."
        return f"Tactical or unconfirmed holding. Unrealized P/L ${parts['unrealized_pnl']:.2f}."

    def _upsert_checkpoint(self, session: Session, provider: str, key: str, payload: dict[str, Any]) -> None:
        row = session.exec(select(StockListenerCheckpoint).where(StockListenerCheckpoint.provider == provider, StockListenerCheckpoint.key == key)).first()
        if row is None:
            row = StockListenerCheckpoint(provider=provider, key=key)
        row.cursor = json.dumps(payload, sort_keys=True)
        row.last_polled_at = self._now()
        session.add(row)

    def _upsert_source_reliability(self, session: Session, source: str, tier: int, score: float) -> None:
        row = session.exec(select(StockSourceReliability).where(StockSourceReliability.source == source)).first()
        if row is None:
            row = StockSourceReliability(source=source)
        row.tier = tier
        row.reliability_score = score
        row.updated_at = self._now()
        session.add(row)

    def _demote_invalid_universe_members(self, session: Session) -> int:
        demoted = 0
        rows = session.exec(select(StockUniverseMember).where(StockUniverseMember.status == "active")).all()
        for row in rows:
            if not self._normalize_symbol(row.symbol):
                row.status = "rejected"
                row.source_detail = (row.source_detail or "")[:220] or "Rejected by ticker hygiene filter."
                session.add(row)
                demoted += 1
        if demoted:
            session.commit()
        return demoted

    def _normalize_news_key(self, symbol: str, title: str, provider: str) -> str:
        raw = "|".join([symbol.upper(), provider.lower(), " ".join(title.lower().split())])
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def _news_importance(self, title: str, summary: str) -> tuple[float, str]:
        text = f"{title} {summary}".lower()
        positive = ["raises guidance", "beat", "contract", "partnership", "buyback", "upgrade", "approval", "launch"]
        negative = ["miss", "downgrade", "offering", "lawsuit", "probe", "investigation", "delay", "weak demand"]
        score = 0.25
        labels: list[str] = []
        for word in positive:
            if word in text:
                score += 0.22
                labels.append(word)
        for word in negative:
            if word in text:
                score += 0.28
                labels.append(word)
        return min(score, 1.0), ", ".join(labels[:3]) or "market news"

    def _parse_news_timestamp(self, value: object) -> datetime | None:
        if not value:
            return None
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(float(value), tz=UTC).replace(tzinfo=None)
            except Exception:
                return None
        text = str(value).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(UTC).replace(tzinfo=None)
            return parsed
        except ValueError:
            return None

    async def _store_news_for_symbol(self, session: Session, symbol: str, limit: int = 4) -> int:
        try:
            items = await self.market_research._get_news_items(session, symbol, limit=limit)
        except Exception:
            return 0
        stored = 0
        now = self._now()
        for item in items[:limit]:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            provider = str(item.get("provider") or "unknown").strip() or "unknown"
            summary = str(item.get("summary") or "").strip()
            dedupe_key = self._normalize_news_key(symbol, title, provider)
            existing = session.exec(select(StockNewsItem).where(StockNewsItem.dedupe_key == dedupe_key)).first()
            importance, tags = self._news_importance(title, summary)
            if existing is None:
                existing = StockNewsItem(symbol=symbol, source=provider, dedupe_key=dedupe_key, title=title)
                stored += 1
            existing.summary = summary[:1200]
            existing.url = str(item.get("url") or "") or None
            existing.importance_score = importance
            existing.classification = "research_candidate" if importance >= 0.55 else "store_context"
            existing.published_at = self._parse_news_timestamp(item.get("published_at"))
            existing.raw_payload = json.dumps({"tags": tags, "provider": provider})
            existing.created_at = existing.created_at or now
            session.add(existing)
            self._discover_symbols_from_text(
                session,
                text=f"{title} {summary}",
                source="symbol_in_news",
                confidence=0.42 + min(0.35, importance * 0.35),
                detail=provider,
            )
            self._upsert_source_reliability(session, provider, 2, 0.65)
        self._upsert_checkpoint(session, "stock_news", symbol, {"stored_at": now.isoformat(), "limit": limit})
        session.commit()
        return stored

    async def _store_filings_for_symbol(self, session: Session, symbol: str) -> int:
        try:
            payload = await self.market_research.sec.sync_symbol(session, symbol, limit=5)
        except Exception:
            return 0
        stored = 0
        for filing in payload.get("filings") or []:
            accession = str(filing.get("accession_number") or "").strip()
            if not accession:
                continue
            existing = session.exec(
                select(StockFilingItem).where(StockFilingItem.symbol == symbol, StockFilingItem.accession_number == accession)
            ).first()
            signals = filing.get("signals") or []
            risks = filing.get("risk_flags") or []
            importance = min(1.0, 0.35 + 0.15 * len(signals) + 0.20 * len(risks))
            if existing is None:
                existing = StockFilingItem(symbol=symbol, accession_number=accession)
                stored += 1
            existing.form_type = str(filing.get("form_type") or "")
            existing.filing_date = self._parse_news_timestamp(filing.get("filing_date"))
            existing.filing_url = str(filing.get("filing_url") or "") or None
            existing.significance_score = importance
            existing.classification = "research_candidate" if importance >= 0.55 else "store_context"
            existing.summary = "; ".join((signals + risks)[:4]) or str(filing.get("description") or "SEC filing")
            session.add(existing)
        self._upsert_source_reliability(session, "SEC EDGAR", 1, 0.95)
        self._upsert_checkpoint(session, "stock_filings", symbol, {"stored_at": self._now().isoformat()})
        session.commit()
        return stored


    def _global_news_provider_health(self, session: Session, provider: str) -> dict[str, Any]:
        row = session.exec(select(StockListenerCheckpoint).where(
            StockListenerCheckpoint.provider == "stock_global_news",
            StockListenerCheckpoint.key == provider,
        )).first()
        try:
            payload = json.loads(row.cursor or "{}") if row else {}
        except (ValueError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _global_news_provider_ready(self, session: Session, provider: str) -> bool:
        state = self._global_news_provider_health(session, provider)
        retry_at = self._parse_news_timestamp(state.get("next_retry_at"))
        return retry_at is None or self._now() >= retry_at

    def _record_global_news_provider_health(
        self, session: Session, provider: str, status: str, reason: str, *, item_count: int = 0,
    ) -> None:
        previous = self._global_news_provider_health(session, provider)
        now = self._now()
        hours = 1 if status in {"healthy", "empty"} else 24 if status == "blocked" else 6
        # Persist only controlled reason codes, never response bodies or request URLs.
        state = {
            "status": status,
            "reason": reason,
            "last_attempt_at": now.isoformat(),
            "next_retry_at": (now + timedelta(hours=hours)).isoformat(),
            "item_count": item_count,
        }
        for field in ("stored_at", "last_success_at"):
            timestamp = self._parse_news_timestamp(previous.get(field))
            if timestamp is not None:
                state[field] = timestamp.isoformat()
        if status in {"healthy", "empty"}:
            state["stored_at"] = state["last_success_at"] = now.isoformat()
        self._upsert_checkpoint(session, "stock_global_news", provider, state)
        session.commit()

    async def _request_global_news_provider(
        self, url: str, params: dict[str, Any],
    ) -> tuple[Any, str, str]:
        try:
            async with httpx.AsyncClient(timeout=25) as client:
                response = await client.get(url, params=params)
                if response.status_code == 429:
                    return None, "rate_limited", "http_429"
                if response.status_code in {401, 402, 403}:
                    return None, "blocked", "authentication_or_entitlement"
                if response.status_code in {404, 410}:
                    return None, "unavailable", "endpoint_missing"
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError:
            return None, "unavailable", "request_failed"
        except ValueError:
            return None, "unavailable", "invalid_json"
        if isinstance(payload, dict):
            messages = [str(value).lower() for key, value in payload.items() if key.lower() in {
                "information", "note", "error message", "error", "message", "error_message",
            } and value]
            if messages:
                message = " ".join(messages)
                # Rate-limit notices may advertise premium plans; throttling takes precedence.
                if any(token in message for token in (
                    "rate limit", "rate-limit", "rate_limit", "call frequency", "too many",
                    "requests per", "calls per", "limit reach", "limit exceeded",
                )):
                    return None, "rate_limited", "provider_rate_limit"
                if any(token in message for token in (
                    "premium", "subscription", "entitlement", "upgrade", "restricted",
                    "not authorized", "not authorised", "unauthorized", "invalid api",
                    "invalid key", "apikey", "api key", "access denied",
                )):
                    return None, "blocked", "authentication_or_entitlement"
                return None, "unavailable", "provider_error"
        return payload, "healthy", "ok"

    async def _store_alpha_vantage_global_news(self, session: Session) -> int:
        if not self._global_news_provider_ready(session, "alpha_vantage"):
            return 0
        api_key = self.settings.alpha_vantage_api_key
        if not api_key:
            self._record_global_news_provider_health(session, "alpha_vantage", "blocked", "missing_api_key")
            return 0
        topics = "technology,energy,financial_markets,manufacturing,life_sciences,real_estate,retail_wholesale"
        params = {
            "function": "NEWS_SENTIMENT",
            "topics": topics,
            "sort": "LATEST",
            "limit": "50",
            "apikey": api_key,
        }
        payload, status, reason = await self._request_global_news_provider(
            "https://www.alphavantage.co/query", params,
        )
        feed = payload.get("feed") if isinstance(payload, dict) else None
        if status == "healthy" and (
            not isinstance(feed, list) or any(
                not isinstance(item, dict) or not (item.get("title") or item.get("summary"))
                or not isinstance(item.get("ticker_sentiment", []), list)
                or any(not isinstance(ticker, dict) for ticker in item.get("ticker_sentiment", []))
                for item in feed
            )
        ):
            status, reason = "unavailable", "invalid_payload"
        if status != "healthy":
            self._record_global_news_provider_health(session, "alpha_vantage", status, reason)
            return 0
        discovered = 0
        for item in feed[:50]:
            title = str(item.get("title") or "").strip()
            summary = str(item.get("summary") or "").strip()
            tickers = item.get("ticker_sentiment") or []
            for ticker_row in tickers:
                symbol = self._normalize_symbol(str(ticker_row.get("ticker") or ""))
                if not symbol:
                    continue
                try:
                    relevance = float(ticker_row.get("relevance_score") or 0.0)
                except (ValueError, TypeError):
                    relevance = 0.0
                confidence = max(0.45, min(0.9, 0.45 + relevance * 0.45))
                self._upsert_universe_member(
                    session,
                    symbol,
                    source="alpha_vantage_news",
                    source_detail=title[:240],
                    confidence=confidence,
                )
                dedupe_key = self._normalize_news_key(symbol, title or summary or symbol, "Alpha Vantage")
                existing = session.exec(select(StockNewsItem).where(StockNewsItem.dedupe_key == dedupe_key)).first()
                if existing is None and title:
                    existing = StockNewsItem(symbol=symbol, source="Alpha Vantage", dedupe_key=dedupe_key, title=title)
                    discovered += 1
                if existing is not None:
                    existing.summary = summary[:1200]
                    existing.url = str(item.get("url") or "") or None
                    existing.importance_score = confidence
                    existing.classification = "research_candidate" if confidence >= 0.58 else "store_context"
                    published_at = self._parse_news_timestamp(item.get("time_published"))
                    if published_at is not None:
                        existing.published_at = published_at
                    session.add(existing)
            discovered += self._discover_symbols_from_text(
                session,
                text=f"{title} {summary}",
                source="global_news_text",
                confidence=0.35,
                detail="Alpha Vantage",
            )
        if feed:
            self._upsert_source_reliability(session, "Alpha Vantage", 2, 0.68)
        self._record_global_news_provider_health(
            session, "alpha_vantage", "healthy" if feed else "empty", "ok", item_count=len(feed),
        )
        return discovered

    async def _fmp_stable_quote_discovery(self, api_key: str) -> tuple[list[dict[str, Any]], str, str]:
        symbols = list(dict.fromkeys(self.settings.discovery_symbols[:30] + self.settings.starter_symbols[:12]))
        if not symbols:
            return [], "empty", "no_symbols"
        data, status, reason = await self._request_global_news_provider(
            "https://financialmodelingprep.com/stable/quote",
            {"symbol": ",".join(symbols), "apikey": api_key},
        )
        if status != "healthy":
            return [], status, reason
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            return [], "unavailable", "invalid_payload"
        rows: list[dict[str, Any]] = []
        for item in data:
            symbol = item.get("symbol")
            name = item.get("name") or symbol
            change = item.get("changePercentage") or item.get("changesPercentage") or 0
            try:
                change_value = abs(float(str(change).replace("%", "")))
            except ValueError:
                change_value = 0.0
            if symbol and change_value >= 0.8:
                rows.append({
                    "symbol": symbol,
                    "title": f"{name} notable price move from FMP stable quote",
                    "summary": f"FMP stable quote shows {symbol} moving {change}% today.",
                    "url": None,
                    "published_at": item.get("timestamp"),
                })
        return rows[:20], "healthy" if rows else "empty", "quote_fallback"

    async def _store_fmp_global_news(self, session: Session) -> int:
        if not self._global_news_provider_ready(session, "fmp"):
            return 0
        api_key = self.settings.fmp_api_key
        if not api_key:
            self._record_global_news_provider_health(session, "fmp", "blocked", "missing_api_key")
            return 0
        urls = [
            "https://financialmodelingprep.com/stable/news/stock-latest",
            "https://financialmodelingprep.com/api/v3/stock_news",
        ]
        for url in urls:
            payload, status, reason = await self._request_global_news_provider(
                url, {"apikey": api_key, "limit": 50},
            )
            # Only a missing endpoint warrants another endpoint, never a quota/auth outage.
            if reason != "endpoint_missing":
                break
        if reason == "endpoint_missing":
            payload, status, reason = await self._fmp_stable_quote_discovery(api_key)
        if status in {"healthy", "empty"} and (
            not isinstance(payload, list) or any(
                not isinstance(item, dict) or not (item.get("title") or item.get("text") or item.get("summary"))
                for item in payload
            )
        ):
            status, reason = "unavailable", "invalid_payload"
        if status not in {"healthy", "empty"}:
            self._record_global_news_provider_health(session, "fmp", status, reason)
            return 0
        discovered = 0
        for item in payload[:50]:
            symbol = self._normalize_symbol(str(item.get("symbol") or item.get("ticker") or ""))
            title = str(item.get("title") or "").strip()
            summary = str(item.get("text") or item.get("summary") or "").strip()
            if symbol:
                self._upsert_universe_member(session, symbol, source="fmp_news", source_detail=title[:240], confidence=0.62)
                dedupe_key = self._normalize_news_key(symbol, title or summary or symbol, "FMP")
                existing = session.exec(select(StockNewsItem).where(StockNewsItem.dedupe_key == dedupe_key)).first()
                if existing is None and title:
                    existing = StockNewsItem(symbol=symbol, source="FMP", dedupe_key=dedupe_key, title=title)
                    discovered += 1
                if existing is not None:
                    existing.summary = summary[:1200]
                    existing.url = str(item.get("url") or "") or None
                    existing.importance_score = 0.62
                    existing.classification = "research_candidate"
                    published_at = self._parse_news_timestamp(item.get("publishedDate") or item.get("published_at"))
                    if published_at is not None:
                        existing.published_at = published_at
                    session.add(existing)
            discovered += self._discover_symbols_from_text(session, text=f"{title} {summary}", source="global_news_text", confidence=0.35, detail="FMP")
        if payload:
            self._upsert_source_reliability(session, "FMP", 2, 0.64)
        self._record_global_news_provider_health(
            session, "fmp", "healthy" if payload else "empty", reason, item_count=len(payload),
        )
        return discovered

    async def refresh_always_on_state(self, session: Session) -> dict[str, Any]:
        demoted = self._demote_invalid_universe_members(session)
        policies = self.infer_position_policies(session)
        global_count = 0
        global_count += await self._store_alpha_vantage_global_news(session)
        global_count += await self._store_fmp_global_news(session)
        universe = self._listener_universe(session)
        active_symbols = self._rotating_symbols(session, universe, self.settings.stock_news_symbols_per_cycle, "news")
        filing_symbols = self._rotating_symbols(session, universe, self.settings.stock_filing_symbols_per_cycle, "filings")
        news_count = 0
        filing_count = 0
        for symbol in active_symbols:
            news_count += await self._store_news_for_symbol(session, symbol)
        for symbol in filing_symbols:
            filing_count += await self._store_filings_for_symbol(session, symbol)
        return {
            "policies": len(policies),
            "universe": len(universe),
            "demoted_invalid_symbols": demoted,
            "global_discoveries": global_count,
            "news_items": news_count,
            "filings": filing_count,
        }

    def _rotating_symbols(self, session: Session, universe: list[str], count: int, key: str) -> list[str]:
        if not universe or count <= 0:
            return []
        row = session.exec(
            select(StockListenerCheckpoint).where(
                StockListenerCheckpoint.provider == "stock_rotation",
                StockListenerCheckpoint.key == key,
            )
        ).first()
        try:
            start = int(json.loads(row.cursor or "{}").get("next_index", 0)) if row else 0
        except (ValueError, TypeError, json.JSONDecodeError):
            start = 0
        selected = [universe[(start + index) % len(universe)] for index in range(min(count, len(universe)))]
        self._upsert_checkpoint(
            session,
            "stock_rotation",
            key,
            {"next_index": (start + len(selected)) % len(universe)},
        )
        session.commit()
        return selected

    def _stock_alert_state_key(self, idea: StockIdea) -> str:
        amount_bucket = round(float(idea.amount_usd or 0.0) / 25.0) * 25
        return f"{idea.symbol}:{idea.side}:{amount_bucket}"

    def _recent_alert_exists(self, session: Session, idea: StockIdea) -> bool:
        key = self._stock_alert_state_key(idea)
        cutoff = self._now() - timedelta(minutes=self.settings.stock_alert_cooldown_minutes)
        exact_row = session.exec(
            select(StockAlert).where(StockAlert.alert_key == key, StockAlert.created_at >= cutoff)
        ).first()
        if exact_row is not None:
            return True
        symbol_row = session.exec(
            select(StockAlert).where(
                StockAlert.symbol == idea.symbol,
                StockAlert.action == idea.side,
                StockAlert.created_at >= cutoff,
            )
        ).first()
        return symbol_row is not None

    def _is_proactive_actionable(self, idea: StockIdea) -> bool:
        if idea.side == "sell":
            return float(idea.amount_usd or 0.0) > 0
        if idea.side == "buy":
            if idea.symbol in self._core_symbols() and str(idea.confidence or "").lower() != "high":
                return False
            return float(idea.amount_usd or 0.0) >= 10 and float(idea.priority_score or 0.0) >= self.settings.stock_proactive_min_priority
        return False

    def _is_after_hours_exception(self, idea: StockIdea) -> bool:
        if str(idea.confidence or "").lower() != "high":
            return False
        if float(idea.priority_score or 0.0) < self.settings.stock_after_hours_exception_min_priority:
            return False
        if idea.side == "sell":
            return float(idea.amount_usd or 0.0) > 0
        if idea.side == "buy":
            return float(idea.amount_usd or 0.0) >= self.settings.stock_after_hours_exception_min_buy_amount_usd
        return False

    def _policy_for_symbol(self, session: Session, symbol: str) -> StockPositionPolicy | None:
        return session.exec(select(StockPositionPolicy).where(StockPositionPolicy.symbol == symbol.upper())).first()

    def _reason_with_policy(self, session: Session, idea: StockIdea) -> str:
        policy = self._policy_for_symbol(session, idea.symbol)
        if policy is None:
            return idea.reason
        lane = policy.lane.replace("_", " ")
        if policy.lane in {"confirmed_core", "probable_core"} and idea.side == "sell":
            return f"Lane: {lane}. Core sell requires thesis-break evidence. {idea.reason}"
        return f"Lane: {lane}. {idea.reason}"

    async def create_proactive_stock_batch(self, session: Session, batch_type: str = "stock-alert") -> RecommendationBatch | None:
        scan = alert_diagnostics.begin_scan(session, self._now())
        try:
            batch = await self._create_proactive_stock_batch(session, batch_type, scan)
        except Exception:
            session.rollback()
            scan["error"] = "stock_alert_scan_failed"
            mode = getattr(getattr(self.market_research, "llm", None), "stock_analysis_mode", None)
            scan["analysis_mode"] = mode if mode in alert_diagnostics.MODES else "unknown"
            alert_diagnostics.finish_scan(session, scan, self._now(), failed=True)
            status = alert_diagnostics.close_status_batch(session, scan, self._now())
            if status is not None:
                return status
            raise
        alert_diagnostics.finish_scan(session, scan, self._now())
        if batch is not None:
            return batch
        return alert_diagnostics.close_status_batch(session, scan, self._now())

    async def _create_proactive_stock_batch(
        self, session: Session, batch_type: str, scan: dict,
    ) -> RecommendationBatch | None:
        try:
            await self.refresh_always_on_state(session)
        except Exception:
            scan["listener_failed"] = True
        llm = getattr(self.market_research, "llm", None)
        if llm is not None:
            llm.stock_analysis_mode = "not_run"
        ideas, news_briefs = await self.market_research.stock_report(
            session,
            stock_limit=self.settings.stock_proactive_idea_limit,
            news_limit=4,
        )
        alert_diagnostics.capture_analysis(session, scan, self.market_research, self._now())
        scan["reported_ideas"] = len(ideas)
        regular_market_hours = self._is_regular_market_hours()
        actionable = []
        rejected = scan["notification_rejections"]
        for idea in ideas:
            if not self._is_proactive_actionable(idea):
                reason = "hold_watch" if idea.side in {"hold", "watch"} else "action_threshold"
            elif not regular_market_hours and not self._is_after_hours_exception(idea):
                reason = "after_hours"
            elif self._recent_alert_exists(session, idea):
                reason = "cooldown"
            else:
                actionable.append(idea)
                continue
            rejected[reason] = rejected.get(reason, 0) + 1
        scan["eligible_ideas"] = len(actionable)
        if not actionable:
            return None
        batch = RecommendationBatch(batch_type=batch_type)
        session.add(batch)
        session.commit()
        session.refresh(batch)
        now = self._now()
        for idx, idea in enumerate(actionable[: self.settings.stock_proactive_idea_limit], start=1):
            reason = self._reason_with_policy(session, idea)
            if not regular_market_hours:
                reason = f"After-hours high-confidence exception: Finn thinks this is worth queuing for the next market session. {reason}"
            validity = now + timedelta(hours=self.settings.core_etf_validity_hours)
            if idea.side in {"buy", "sell"}:
                validity = now + timedelta(minutes=self.settings.earnings_trade_validity_minutes)
            rec = Recommendation(
                batch_id=batch.id,
                item_number=idx,
                recommendation_type=idea.recommendation_type,
                symbol=idea.symbol,
                side=idea.side,
                amount_usd=round(float(idea.amount_usd or 0.0), 2),
                reason=reason,
                risk=idea.risk,
                execution_mode="manual",
                valid_until=validity,
            )
            session.add(rec)
            session.flush()
            key = self._stock_alert_state_key(idea)
            session.add(
                StockAlert(
                    alert_key=key,
                    batch_id=batch.id,
                    recommendation_id=rec.id,
                    symbol=idea.symbol,
                    action=idea.side,
                    priority_score=float(idea.priority_score or 0.0),
                    reason_summary=reason[:900],
                    status="sent",
                )
            )
            policy = self._policy_for_symbol(session, idea.symbol)
            session.add(
                StockDecisionSnapshot(
                    symbol=idea.symbol,
                    lane=policy.lane if policy else "unknown",
                    action=idea.side,
                    recommended_stake=round(float(idea.amount_usd or 0.0), 2),
                    confidence=idea.confidence,
                    quant_score=float(idea.priority_score or 0.0),
                    reason_summary=reason[:900],
                )
            )
            if idea.side in {"buy", "sell", "watch", "hold"}:
                quote = (await self.market_research.quote_map(session, [idea.symbol])).get(idea.symbol) or {}
                price = float(quote.get("close") or 0.0)
                if price > 0:
                    session.add(
                        RecommendationOutcome(
                            recommendation_id=rec.id,
                            symbol=idea.symbol,
                            baseline_price=price,
                            latest_price=price,
                            return_pct=0.0,
                            status="open",
                            notes=f"proactive stock {idea.side}",
                        )
                    )
        next_item_number = len(actionable[: self.settings.stock_proactive_idea_limit]) + 1
        for brief in news_briefs[:2]:
            idx = next_item_number
            next_item_number += 1
            session.add(
                Recommendation(
                    batch_id=batch.id,
                    item_number=idx,
                    recommendation_type="news",
                    symbol=brief.symbol,
                    side="info",
                    amount_usd=0.0,
                    reason=f"{brief.headline} ({brief.reason})",
                    risk="medium",
                    execution_mode="none",
                    valid_until=now + timedelta(hours=24),
                )
            )
        session.commit()
        scan["alert_batch_created"] = True
        return batch


    def report_context_lines(self, session: Session) -> list[str]:
        policies = list(session.exec(select(StockPositionPolicy).order_by(StockPositionPolicy.core_score.desc())).all())
        lines: list[str] = []
        if policies:
            core = [row.symbol for row in policies if row.lane in {"confirmed_core", "probable_core"}][:6]
            tactical = [row.symbol for row in policies if row.lane == "tactical"][:6]
            if core:
                lines.append("Core lens: " + ", ".join(core))
            if tactical:
                lines.append("Tactical lens: " + ", ".join(tactical))
        dynamic = list(
            session.exec(
                select(StockUniverseMember)
                .where(StockUniverseMember.status == "active")
                .order_by(StockUniverseMember.confidence.desc(), StockUniverseMember.last_seen_at.desc())
            ).all()[:8]
        )
        lines.append("Always-on scan: tech/AI, space, energy/nuclear, defense, industrials, healthcare, financials, materials, consumer, ETFs")
        if dynamic:
            symbols = [self._normalize_symbol(row.symbol) for row in dynamic]
            symbols = [symbol for symbol in symbols if symbol]
            if symbols:
                lines.append("Dynamic discovery: " + ", ".join(symbols[:8]))
        return lines[:3]

    def resolve_stock_query(self, query: str) -> str:
        normalized = " ".join(query.upper().replace("$", "").split())
        aliases = {
            "NVIDIA": "NVDA", "NVIDIA CORP": "NVDA",
            "MARVELL": "MRVL", "MARVELL TECHNOLOGY": "MRVL",
            "SANDISK": "SNDK",
            "ROCKET LAB": "RKLB", "ROCKETLAB": "RKLB",
            "INTUITIVE MACHINES": "LUNR",
            "AST SPACEMOBILE": "ASTS",
            "EXXON": "XOM", "EXXON MOBIL": "XOM",
            "CHEVRON": "CVX", "OCCIDENTAL": "OXY",
            "LOCKHEED": "LMT", "LOCKHEED MARTIN": "LMT",
            "NORTHROP": "NOC", "NORTHROP GRUMMAN": "NOC",
            "PALANTIR": "PLTR", "BERKSHIRE": "BRK-B",
        }
        if normalized in aliases:
            return aliases[normalized]
        compact = normalized.replace(" ", "")
        if 1 <= len(compact) <= 6 and compact.replace("-", "").isalnum():
            return compact
        return aliases.get(normalized, compact[:6])

    async def analyze_asked_stock(self, session: Session, query: str) -> str:
        symbol = self.resolve_stock_query(query)
        if not symbol:
            return "Use `ask <ticker or company>`, for example `ask RKLB` or `ask Exxon`."
        await self.refresh_always_on_state(session)
        quote = (await self.market_research.quote_map(session, [symbol])).get(symbol) or {}
        history = await self.market_research._get_history(session, symbol)
        benchmark = await self.market_research._get_history(session, "SPY")
        news = await self.market_research._get_news_items(session, symbol, limit=5)
        try:
            sec = await self.market_research.sec.sync_symbol(session, symbol, limit=5)
        except Exception:
            sec = {"filings": [], "fundamentals": {}, "catalysts": []}
        close = float(quote.get("close") or 0.0)
        prev = float(quote.get("previous_close") or 0.0)
        change_pct = ((close - prev) / prev * 100.0) if close > 0 and prev > 0 else 0.0
        hist_metrics = self.market_research._history_metrics(history)
        bench_metrics = self.market_research._history_metrics(benchmark)
        trend_score, trend_reasons, trend_risks = self.market_research._trend_signal(hist_metrics, bench_metrics)
        news_score, news_reasons, news_risks = self.market_research._news_signal(news)
        fundamental_score, fundamental_reasons, fundamental_risks = self.market_research._fundamental_signal(sec)
        catalyst_score, catalyst_reasons, catalyst_risks = self.market_research._catalyst_signal(sec, news_score, news_reasons, news_risks, [])
        flow_score, flow_reasons, flow_risks = self.market_research._flow_signal(hist_metrics, news_score)
        risk_flags = self.market_research._dedupe(trend_risks + news_risks + fundamental_risks + catalyst_risks + flow_risks)
        risk_penalty = self.market_research._risk_penalty(
            risk_flags=risk_flags,
            history_metrics=hist_metrics,
            holding_weight=self.market_research._holding_weight(session, symbol),
            asset_class="etf" if symbol in {"SPY", "VTI", "VT", "QQQM"} else "stock",
        )
        composite = round(0.30 * trend_score + 0.30 * fundamental_score + 0.25 * catalyst_score + 0.15 * flow_score - risk_penalty, 2)
        holding = self.market_research._position_map(session).get(symbol)
        policy = self._policy_for_symbol(session, symbol)
        packet = {
            "symbol": symbol,
            "company_name": (sec or {}).get("company_name") or symbol,
            "currently_held": bool(holding),
            "holding_value": round(float(holding.market_value or 0.0), 2) if holding else 0.0,
            "holding_weight": round(self.market_research._holding_weight(session, symbol), 4),
            "asset_class": "stock",
            "close": close,
            "previous_close": prev,
            "momentum_pct": round(change_pct, 2),
            "trend_score": trend_score,
            "trend_reasons": trend_reasons,
            "fundamental_score": fundamental_score,
            "fundamental_reasons": fundamental_reasons,
            "catalyst_score": catalyst_score,
            "catalyst_reasons": catalyst_reasons,
            "flow_score": flow_score,
            "flow_reasons": flow_reasons,
            "risk_penalty": risk_penalty,
            "risk_flags": risk_flags,
            "composite_score": composite,
            "news_headlines": news[:3],
            "filing_signals": [signal for filing in (sec.get("filings") or [])[:3] for signal in (filing.get("signals") or [])][:4],
            "recent_filings": (sec.get("filings") or [])[:3],
            "fundamentals": sec.get("fundamentals") or {},
            "why_now": self.market_research._why_now({"trend_reasons": trend_reasons, "catalyst_reasons": catalyst_reasons, "flow_reasons": flow_reasons}),
        }
        decisions = await self.market_research.llm.analyze_stocks(
            portfolio_context=self.market_research._portfolio_context(session),
            stock_packets=[packet],
        )
        decision = decisions[0] if decisions else None
        action = decision.action.upper() if decision else ("WATCH" if composite > 0.8 else "AVOID")
        confidence = decision.confidence if decision else "medium"
        thesis = decision.thesis_summary if decision else self.market_research.llm._compact_stock_summary("watch", packet)
        amount = 0.0
        if decision:
            amount = self.market_research._size_stock_decision(decision.action, decision.sizing_intent, packet, total_value=max(self.market_research._current_total_value(session), 1.0))
        lines = [f"<b>Finn Ask: {escape(symbol)}</b>"]
        if close:
            lines.append(f"Price: <b>${close:.2f}</b> ({change_pct:+.2f}% today)")
        lines.append(f"Action: <b>{escape(action)}</b> • Confidence: <b>{escape(confidence)}</b>")
        if amount > 0:
            lines.append(f"Suggested size: <b>${amount:.2f}</b>")
        if policy:
            lines.append(f"Portfolio lane: <b>{escape(policy.lane)}</b> • core score {policy.core_score:.2f}")
        lines.append(f"Quant score: <b>{composite:.2f}</b>")
        lines.append(f"Thesis: {escape(thesis)}")
        reasons = (decision.key_reasons if decision else []) + trend_reasons[:2] + catalyst_reasons[:2] + fundamental_reasons[:2] + flow_reasons[:1]
        if reasons:
            lines.append("Why:")
            for reason in self.market_research._dedupe([str(r) for r in reasons])[:5]:
                lines.append(f"- {escape(reason)}")
        if risk_flags:
            lines.append("Risks:")
            for risk in risk_flags[:4]:
                lines.append(f"- {escape(str(risk))}")
        if news:
            lines.append("Important news:")
            for item in news[:3]:
                title = str(item.get("title") or "").strip()
                if title:
                    lines.append(f"- {escape(title[:180])}")
        return "\n".join(lines)

    def format_status(self, session: Session) -> str:
        policies = list(session.exec(select(StockPositionPolicy).order_by(StockPositionPolicy.core_score.desc())).all())
        recent_news = list(session.exec(select(StockNewsItem).order_by(StockNewsItem.created_at.desc())).all()[:5])
        recent_alerts = list(session.exec(select(StockAlert).order_by(StockAlert.created_at.desc())).all()[:5])
        lines = ["<b>Finn Stock Advisor</b>"]
        lines.append(f"Core policies tracked: <b>{len(policies)}</b>")
        if policies:
            lines.append("<b>Top core/tactical classifications</b>")
            for row in policies[:8]:
                lines.append(f"- <b>{escape(row.symbol)}</b>: {escape(row.lane)} • core score {row.core_score:.2f}")
        if recent_news:
            lines.append("<b>Recent listener items</b>")
            for item in recent_news[:4]:
                lines.append(f"- <b>{escape(item.symbol)}</b>: {escape(item.title[:110])}")
        if recent_alerts:
            lines.append("<b>Recent proactive alerts</b>")
            for alert in recent_alerts[:4]:
                lines.append(f"- <b>{escape(alert.symbol)}</b> {escape(alert.action.upper())}: {alert.priority_score:.1f}")
        return "\n".join(lines)
