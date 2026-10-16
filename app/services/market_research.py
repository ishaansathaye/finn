from __future__ import annotations

from dataclasses import dataclass
import asyncio
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
import json
from math import floor, isfinite
from math import log10
import re
from typing import Any
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app.adapters.google_news import GoogleNewsAdapter
from app.adapters.kalshi import KalshiAdapter
from app.adapters.robinhood_prediction import RobinhoodCatalogSnapshot, RobinhoodPredictionAdapter
from app.adapters.twelvedata import TwelveDataAdapter
from app.adapters.yfinance_adapter import YFinanceAdapter
from app.config import get_settings
from app.models import (
    Account,
    AccountSnapshot,
    EventBetOutcome,
    EventWatch,
    EventPosition,
    Holding,
    IntegrationState,
    Recommendation,
    RecommendationBatch,
    RecommendationOutcome,
    StockFilingItem,
    StockNewsItem,
    StockUniverseMember,
)
from app.services.llm_analysis import LLMAnalysisService, StockDecisionModel
from app.services.market_cache import MarketCacheService
from app.services.sec_research import SECResearchService
from app.services.stock_evidence import article_evidence, balanced_shortlist, evidence_gaps, news_evidence, risk_metrics


ETF_SYMBOLS = {"SPY", "VOO", "VTI", "VT", "QQQ", "QQQM", "IVV", "DIA", "IWM"}

EVENT_CODE_LABELS = {
    "ARI": "Arizona", "AZ": "Arizona", "ATL": "Atlanta", "BAL": "Baltimore", "BOS": "Boston",
    "CHC": "Chicago C", "CWS": "Chicago WS", "CHW": "Chicago WS", "CIN": "Cincinnati", "CLE": "Cleveland",
    "COL": "Colorado", "DET": "Detroit", "HOU": "Houston", "KC": "Kansas City", "KCM": "Kansas City",
    "LAA": "Los Angeles A", "LAD": "Los Angeles D", "MIA": "Miami", "MIL": "Milwaukee", "MIN": "Minnesota",
    "NYM": "New York M", "NYY": "New York Y", "ATH": "Athletics", "OAK": "Athletics", "PHI": "Philadelphia",
    "PIT": "Pittsburgh", "SD": "San Diego", "SDP": "San Diego", "SEA": "Seattle", "SF": "San Francisco",
    "SFG": "San Francisco", "STL": "St. Louis", "TB": "Tampa Bay", "TBR": "Tampa Bay", "TEX": "Texas",
    "TOR": "Toronto", "WSH": "Washington",
}


@dataclass
class StockIdea:
    symbol: str
    recommendation_type: str
    side: str
    amount_usd: float
    reason: str
    risk: str
    change_pct: float
    source_status: str = "live"
    priority_score: float = 0.0
    confidence: str = "medium"


@dataclass
class EventIdea:
    ticker: str
    title: str
    side: str
    amount_usd: float
    reason: str
    risk: str
    pick_label: str
    bet_description: str = ""
    implied_probability: float = 0.0
    source_status: str = "live"
    priority_score: float = 0.0
    confidence: str = "medium"
    event_day: str | None = None
    is_same_day: bool = False
    category: str = ""
    time_label: str = ""
    is_relevant_now: bool = False
    event_starts_at: datetime | None = None
    execution_market_ticker: str | None = None
    execution_event_ticker: str | None = None
    execution_contract_id: str | None = None
    contract_path: str | None = None
    max_return_usd: float = 0.0
    potential_profit_usd: float = 0.0


@dataclass
class NewsBrief:
    symbol: str
    headline: str
    reason: str
    priority_score: float = 0.0


@dataclass
class EventSlateItem:
    ticker: str
    title: str
    event_day: str
    time_label: str
    sort_key: tuple[int, str, str]
    options: str
    is_same_day: bool = False
    category: str = ""


class MarketResearchService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.market_data = TwelveDataAdapter()
        self.yfinance = YFinanceAdapter()
        self.google_news = GoogleNewsAdapter()
        self.kalshi = KalshiAdapter()
        self.robinhood_prediction = RobinhoodPredictionAdapter()
        self.cache = MarketCacheService()
        self.sec = SECResearchService()
        self.llm = LLMAnalysisService()
        self.local_tz = ZoneInfo(self.settings.local_timezone)
        self._event_ticker_date_pattern = re.compile(r"(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})")
        self._event_ticker_datetime_pattern = re.compile(
            r"(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})(\d{3,4})?"
        )
        self._event_ticker_months = {
            "JAN": 1,
            "FEB": 2,
            "MAR": 3,
            "APR": 4,
            "MAY": 5,
            "JUN": 6,
            "JUL": 7,
            "AUG": 8,
            "SEP": 9,
            "OCT": 10,
            "NOV": 11,
            "DEC": 12,
        }

    def _portfolio_symbols(self, session: Session) -> list[str]:
        return sorted(self._position_map(session))

    def _primary_holdings(self, session: Session, *, include_crypto: bool = False) -> list[Holding]:
        snapshot = self._primary_account_snapshot(session)
        if snapshot is None:
            return []
        stmt = select(Holding).where(Holding.account_id == snapshot.account_id, Holding.quantity > 0)
        if not include_crypto:
            stmt = stmt.where(Holding.asset_type != "crypto")
        return [Holding(**row.model_dump()) for row in session.exec(stmt).all() if row.symbol]

    def _position_map(self, session: Session) -> dict[str, Holding]:
        # Sync replaces rows while network research awaits; never retain expirable ORM rows.
        return {holding.symbol: holding for holding in self._primary_holdings(session)}

    def _priority_stock_universe(self, session: Session) -> list[str]:
        ranked_holdings = sorted(self._primary_holdings(session), key=lambda row: row.market_value, reverse=True)
        prioritized: list[str] = []
        seen: set[str] = set()

        holding_limit = max(8, self.settings.llm_max_stock_symbols // 2)
        for holding in ranked_holdings[:holding_limit]:
            if holding.symbol and holding.symbol not in seen:
                prioritized.append(holding.symbol)
                seen.add(holding.symbol)

        # Reserve coverage before the large dynamic/technology lists fill the cap.
        from app.services.stock_advisor import StockAdvisorService
        advisor = StockAdvisorService(self)
        diverse = advisor._diversified_discovery_symbols() + ["DUK", "SO", "AEP", "PLD", "AMT", "EQIX", "DIS", "TMUS", "VZ"]
        rotation = int(datetime.now(UTC).timestamp() // 3600)
        buckets: dict[str, list[str]] = {}
        for symbol in diverse:
            buckets.setdefault(advisor.sector_for_symbol(symbol), []).append(symbol)
        diversified = []
        for index in range(3):
            for symbols in buckets.values():
                diversified.append(symbols[(rotation + index) % len(symbols)])
        for symbol in diversified + self.settings.starter_symbols:
            if symbol not in seen:
                prioritized.append(symbol)
                seen.add(symbol)

        dynamic_rows = list(
            session.exec(
                select(StockUniverseMember)
                .where(StockUniverseMember.status == "active")
                .order_by(StockUniverseMember.confidence.desc(), StockUniverseMember.last_seen_at.desc())
            ).all()
        )
        for symbol in [row.symbol for row in dynamic_rows]:
            if symbol not in seen:
                prioritized.append(symbol)
                seen.add(symbol)

        for symbol in [row.symbol for row in session.exec(select(StockNewsItem).order_by(StockNewsItem.created_at.desc())).all()[:80]]:
            if symbol and symbol not in seen:
                prioritized.append(symbol)
                seen.add(symbol)

        for symbol in [row.symbol for row in session.exec(select(StockFilingItem).order_by(StockFilingItem.created_at.desc())).all()[:80]]:
            if symbol and symbol not in seen:
                prioritized.append(symbol)
                seen.add(symbol)

        for symbol in self.settings.discovery_symbols:
            if symbol not in seen:
                prioritized.append(symbol)
                seen.add(symbol)

        return prioritized[: self.settings.stock_discovery_limit]

    def _extract_quote_items(self, payload: dict) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        if "symbol" in payload and "close" in payload:
            return [payload]
        items: list[dict] = []
        for symbol, item in payload.items():
            if isinstance(item, dict):
                normalized = dict(item)
                normalized.setdefault("symbol", symbol)
                items.append(normalized)
        return items

    def _quote_cache_key(self, symbols: list[str]) -> str:
        return ",".join(sorted(symbols))

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            number = float(value or 0)
            return number if isfinite(number) else 0.0
        except (TypeError, ValueError):
            return 0.0

    async def _get_quote_payload(self, session: Session, symbols: list[str]) -> tuple[dict, str]:
        cache_key = self._quote_cache_key(symbols)
        cached = self.cache.get_json(
            session,
            "market_data",
            f"quotes:{cache_key}",
            max_age_seconds=self.settings.market_quote_cache_seconds,
        )
        if cached is not None:
            return cached, "cache"

        try:
            payload = await self.market_data.get_quotes(symbols)
            valid = {row.get("symbol") for row in self._extract_quote_items(payload)
                     if self._to_float(row.get("close")) > 0 and self._to_float(row.get("previous_close")) > 0}
            if payload.get("status") != "error" and set(symbols) <= valid:
                self.cache.set_json(session, "market_data", f"quotes:{cache_key}", payload)
                return payload, "twelve_data"
        except Exception:
            pass

        try:
            payload = await self.yfinance.get_quotes(symbols)
            valid = {row.get("symbol") for row in self._extract_quote_items(payload)
                     if self._to_float(row.get("close")) > 0 and self._to_float(row.get("previous_close")) > 0}
            if valid:
                self.cache.set_json(session, "market_data", f"quotes:{cache_key}", payload)
                return payload, "yfinance"
        except Exception:
            pass
        stale = self.cache.get_json_stale(session, "market_data", f"quotes:{cache_key}")
        if stale is not None:
            return stale, "stale_cache"

        return {}, "unavailable"

    async def _get_news_items(self, session: Session, symbol: str, limit: int = 5) -> list[dict]:
        cache_key = f"news:v2:{symbol}"
        cached = self.cache.get_json(
            session,
            "market_data",
            cache_key,
            max_age_seconds=self.settings.market_news_cache_seconds,
        )
        if cached is not None:
            return cached
        news = await self.yfinance.get_news(symbol, limit=limit)
        if not news:
            news = await self.google_news.get_news(symbol, limit=limit)
        self.cache.set_json(session, "market_data", cache_key, news)
        return news

    async def _get_calendar(self, session: Session, symbol: str) -> dict:
        cache_key = f"calendar:{symbol}"
        cached = self.cache.get_json(
            session,
            "market_data",
            cache_key,
            max_age_seconds=self.settings.market_calendar_cache_seconds,
        )
        if cached is not None:
            return cached
        calendar = await self.yfinance.get_calendar(symbol)
        self.cache.set_json(session, "market_data", cache_key, calendar)
        return calendar

    async def _get_history(self, session: Session, symbol: str, *, period: str = "1y", interval: str = "1d") -> list[dict]:
        cache_key = f"history:{symbol}:{period}:{interval}"
        cached = self.cache.get_json(
            session,
            "market_data",
            cache_key,
            max_age_seconds=self.settings.market_history_cache_seconds,
        )
        if cached is not None:
            return cached
        history = await self.yfinance.get_history(symbol, period=period, interval=interval)
        self.cache.set_json(session, "market_data", cache_key, history)
        return history

    def _account_holding_count(self, session: Session, account_id: str) -> int:
        return len(session.exec(select(Holding).where(Holding.account_id == account_id)).all())

    def _primary_account_snapshot(self, session: Session) -> AccountSnapshot | None:
        configured_external_id = str(self.settings.primary_portfolio_account_external_id or "").strip()
        if configured_external_id:
            account = session.exec(
                select(Account).where(Account.external_id == configured_external_id)
            ).first()
            if account is not None and account.provider == "snaptrade" and account.account_type.upper() == "INDIVIDUAL":
                snapshot = session.exec(
                    select(AccountSnapshot)
                    .where(AccountSnapshot.account_id == account.id)
                    .order_by(AccountSnapshot.as_of.desc())
                ).first()
                if snapshot is not None and snapshot.as_of >= datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1):
                    return snapshot
            # A configured identity is authoritative; never silently switch accounts.
            return None

        accounts = session.exec(select(Account).where(Account.provider == "snaptrade")).all()
        candidates: list[tuple[int, float, datetime, AccountSnapshot]] = []
        for account in accounts:
            if str(account.account_type or "").upper() != "INDIVIDUAL":
                continue
            snapshot = session.exec(
                select(AccountSnapshot)
                .where(AccountSnapshot.account_id == account.id)
                .order_by(AccountSnapshot.as_of.desc())
            ).first()
            if snapshot is None or snapshot.as_of < datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1):
                continue
            holding_count = self._account_holding_count(session, account.id)
            candidates.append((holding_count, float(snapshot.total_value or 0.0), snapshot.as_of, snapshot))
        if len(candidates) == 1:
            return candidates[0][3]
        return None

    def _latest_snapshot(self, session: Session) -> AccountSnapshot | None:
        return self._primary_account_snapshot(session)

    def _holding_weight(self, session: Session, symbol: str) -> float:
        holding = self._position_map(session).get(symbol)
        snapshot = self._latest_snapshot(session)
        if not holding or not snapshot or snapshot.total_value <= 0:
            return 0.0
        return holding.market_value / snapshot.total_value

    def _holding_value(self, session: Session, symbol: str) -> float:
        holding = self._position_map(session).get(symbol)
        return holding.market_value if holding else 0.0

    def _current_buying_power(self, session: Session) -> float:
        latest = self._latest_snapshot(session)
        if not latest:
            return 0.0
        buying_power = float(latest.buying_power or 0.0)
        cash_balance = float(latest.cash_balance or 0.0)
        if buying_power <= 0 < cash_balance:
            return cash_balance
        return buying_power

    def _current_total_value(self, session: Session) -> float:
        latest = self._latest_snapshot(session)
        return latest.total_value if latest else 0.0

    def _portfolio_context(self, session: Session) -> dict[str, Any]:
        holdings = list(self._position_map(session).values())
        holdings.sort(key=lambda item: item.market_value, reverse=True)
        return {
            "account_scope": "primary_only",
            "account_id": (self._primary_account_snapshot(session).account_id if self._primary_account_snapshot(session) else None),
            "buying_power": round(self._current_buying_power(session), 2),
            "total_value": round(self._current_total_value(session), 2),
            "minimum_cash_buffer_usd": self.settings.minimum_cash_buffer_usd,
            "max_single_stock_allocation_pct": self.settings.max_single_stock_allocation_pct,
            "max_single_etf_allocation_pct": self.settings.max_single_etf_allocation_pct,
            "top_holdings": [
                {
                    "symbol": holding.symbol,
                    "market_value": round(holding.market_value, 2),
                    "quantity": holding.quantity,
                }
                for holding in holdings[:8]
            ],
        }

    def _news_published_recently(self, item: dict) -> bool:
        value = item.get("published_at")
        if value is None:
            return False
        try:
            if isinstance(value, (int, float)):
                published = datetime.fromtimestamp(value, UTC)
            else:
                try:
                    published = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                except ValueError:
                    published = parsedate_to_datetime(str(value))
            if published.tzinfo is None:
                published = published.replace(tzinfo=UTC)
            age = datetime.now(UTC) - published.astimezone(UTC)
            return timedelta(minutes=-5) <= age <= timedelta(hours=48)
        except (ValueError, TypeError, OverflowError):
            return False

    def _news_signal(self, news_items: list[dict]) -> tuple[float, list[str], list[str]]:
        return news_evidence([row for row in news_items if self._news_published_recently(row)][:6])

    def _earnings_signal(self, calendar: dict) -> tuple[float, list[str]]:
        earnings_values = calendar.get("Earnings Date") or []
        if not earnings_values:
            return 0.0, []
        first = earnings_values[0] if isinstance(earnings_values, list) else earnings_values
        try:
            earnings_day = date.fromisoformat(str(first))
        except ValueError:
            return 0.0, []
        days_until = (earnings_day - datetime.now(UTC).date()).days
        if days_until < 0:
            return 0.0, [f"earnings recently passed on {earnings_day.isoformat()}"]
        if days_until <= 10:
            return 0.0, [f"earnings in {days_until} day(s); outcome uncertain"]
        if days_until <= 30:
            return 0.0, [f"earnings on {earnings_day.isoformat()}"]
        return 0.0, []

    def _filing_signal(self, filings: list[dict[str, Any]]) -> tuple[float, list[str], list[str]]:
        score = 0.0
        reasons: list[str] = []
        risk_flags: list[str] = []
        for filing in filings[:4]:
            signal = str(filing.get("signal_summary") or "").lower()
            form_type = str(filing.get("form_type") or "")
            if "earnings release" in signal:
                score += 0.8
                reasons.append(f"{form_type}: earnings release")
            if "guidance mentioned" in signal:
                score += 0.5
                reasons.append(f"{form_type}: guidance language")
            if "management change" in signal:
                risk_flags.append(f"{form_type}: management change")
                score -= 0.3
            if "offering" in signal:
                risk_flags.append(f"{form_type}: offering risk")
                score -= 0.8
            if "ownership stake update" in signal:
                reasons.append(f"{form_type}: ownership change")
            if "insider transaction" in signal:
                risk_flags.append(f"{form_type}: insider activity")
            if "spin-off" in signal:
                reasons.append(f"{form_type}: spin-off language")
                score += 0.4
        return score, self._dedupe(reasons)[:3], self._dedupe(risk_flags)[:3]

    def _dedupe(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        deduped: list[str] = []
        for value in values:
            if value and value not in seen:
                deduped.append(value)
                seen.add(value)
        return deduped

    def _recent_recommendation_state(self, session: Session, symbol: str) -> dict[str, Any]:
        stmt = (
            select(Recommendation)
            .where(Recommendation.symbol == symbol)
            .where(Recommendation.recommendation_type.in_(("stock", "etf")))
            .order_by(Recommendation.created_at.desc())
        )
        recent = list(session.exec(stmt).all()[:5])
        if not recent:
            return {
                "recent_side": None,
                "recent_reason": None,
                "hours_since_last": None,
                "recent_execution": False,
                "recent_approval": False,
            }
        latest = recent[0]
        hours_since_last = (datetime.now(UTC).replace(tzinfo=None) - latest.created_at).total_seconds() / 3600.0
        outcome_stmt = (
            select(RecommendationOutcome)
            .where(RecommendationOutcome.symbol == symbol)
            .order_by(RecommendationOutcome.created_at.desc())
        )
        latest_outcome = session.exec(outcome_stmt).first()
        return {
            "recent_side": latest.side,
            "recent_reason": latest.reason,
            "hours_since_last": round(hours_since_last, 2),
            "recent_execution": False,
            "recent_approval": False,
            "recent_return_pct": latest_outcome.return_pct if latest_outcome else None,
        }

    def _history_metrics(self, history: list[dict[str, Any]]) -> dict[str, float | bool | None]:
        closes = [float(item.get("close") or 0.0) for item in history if float(item.get("close") or 0.0) > 0]
        volumes = [float(item.get("volume") or 0.0) for item in history if float(item.get("volume") or 0.0) > 0]
        if len(closes) < 30:
            return {
                "return_1m_pct": None,
                "return_3m_pct": None,
                "return_6m_pct": None,
                "distance_from_52w_high_pct": None,
                "above_50d": None,
                "above_200d": None,
                "ma_stack_bullish": None,
                "volume_vs_20d": None,
                "latest_close": closes[-1] if closes else None,
            }
        latest = closes[-1]
        sma_20 = sum(closes[-20:]) / min(20, len(closes))
        sma_50 = sum(closes[-50:]) / min(50, len(closes))
        sma_200 = sum(closes[-200:]) / min(200, len(closes))
        year_high = max(closes[-252:]) if len(closes) >= 2 else latest
        avg_volume_20 = (sum(volumes[-20:]) / 20.0) if len(volumes) >= 20 else None
        latest_volume = volumes[-1] if volumes else None
        return {
            "return_1m_pct": self._window_return_pct(closes, 21),
            "return_3m_pct": self._window_return_pct(closes, 63),
            "return_6m_pct": self._window_return_pct(closes, 126),
            "distance_from_52w_high_pct": round(((latest / year_high) - 1.0) * 100.0, 2) if year_high > 0 else None,
            "above_50d": latest > sma_50,
            "above_200d": latest > sma_200 if len(closes) >= 200 else None,
            "ma_stack_bullish": sma_20 > sma_50 > sma_200 if len(closes) >= 200 else None,
            "volume_vs_20d": round((latest_volume / avg_volume_20), 2) if latest_volume and avg_volume_20 else None,
            "latest_close": latest,
        }

    def _window_return_pct(self, closes: list[float], days_back: int) -> float | None:
        if len(closes) <= days_back:
            return None
        previous = closes[-(days_back + 1)]
        if previous <= 0:
            return None
        return round(((closes[-1] / previous) - 1.0) * 100.0, 2)

    def _trend_signal(self, history_metrics: dict[str, Any], benchmark_metrics: dict[str, Any]) -> tuple[float, list[str], list[str]]:
        score = 0.0
        reasons: list[str] = []
        risks: list[str] = []

        return_3m = history_metrics.get("return_3m_pct")
        return_6m = history_metrics.get("return_6m_pct")
        distance_high = history_metrics.get("distance_from_52w_high_pct")
        benchmark_3m = benchmark_metrics.get("return_3m_pct")
        benchmark_6m = benchmark_metrics.get("return_6m_pct")

        if isinstance(return_3m, float):
            if return_3m >= 18:
                score += 1.0
                reasons.append("strong 3M trend")
            elif return_3m >= 8:
                score += 0.55
                reasons.append("positive 3M trend")
            elif return_3m <= -12:
                score -= 0.9
                risks.append("weak 3M trend")

        if isinstance(return_6m, float):
            if return_6m >= 30:
                score += 1.1
                reasons.append("strong 6M trend")
            elif return_6m >= 15:
                score += 0.65
                reasons.append("positive 6M trend")
            elif return_6m <= -18:
                score -= 1.0
                risks.append("weak 6M trend")

        if isinstance(return_3m, float) and isinstance(benchmark_3m, float):
            spread = round(return_3m - benchmark_3m, 2)
            if spread >= 10:
                score += 0.8
                reasons.append("outperforming SPY over 3M")
            elif spread <= -8:
                score -= 0.8
                risks.append("lagging SPY over 3M")

        if isinstance(return_6m, float) and isinstance(benchmark_6m, float):
            spread = round(return_6m - benchmark_6m, 2)
            if spread >= 14:
                score += 0.8
                reasons.append("outperforming SPY over 6M")
            elif spread <= -10:
                score -= 0.8
                risks.append("lagging SPY over 6M")

        if history_metrics.get("ma_stack_bullish"):
            score += 0.6
            reasons.append("bullish moving-average stack")
        elif history_metrics.get("above_200d") is False:
            score -= 0.6
            risks.append("below 200-day trend")

        if isinstance(distance_high, float):
            if distance_high >= -8:
                score += 0.75
                reasons.append("trading near 52-week high")
            elif distance_high <= -28:
                score -= 0.7
                risks.append("well below 52-week high")

        return round(score, 2), self._dedupe(reasons)[:4], self._dedupe(risks)[:4]

    def _fundamental_signal(self, sec_context: dict[str, Any]) -> tuple[float, list[str], list[str]]:
        trend_summary = (sec_context.get("fundamentals") or {}).get("trend_summary") or {}
        score = 0.0
        reasons: list[str] = []
        risks: list[str] = []

        revenue_growth = trend_summary.get("revenue_growth_yoy_pct")
        revenue_accel = trend_summary.get("revenue_acceleration_pct")
        eps_growth = trend_summary.get("eps_growth_yoy_pct")
        margin = trend_summary.get("gross_margin_pct")
        dilution = trend_summary.get("share_dilution_yoy_pct")
        profitable = trend_summary.get("profitable_latest_quarter")

        if isinstance(revenue_growth, float):
            if revenue_growth >= 20:
                score += 1.05
                reasons.append(f"revenue growth {revenue_growth:.0f}% YoY")
            elif revenue_growth >= 10:
                score += 0.65
                reasons.append(f"revenue growth {revenue_growth:.0f}% YoY")
            elif revenue_growth <= -5:
                score -= 0.75
                risks.append(f"revenue down {abs(revenue_growth):.0f}% YoY")

        if isinstance(revenue_accel, float):
            if revenue_accel >= 5:
                score += 0.45
                reasons.append("revenue growth accelerating")
            elif revenue_accel <= -5:
                score -= 0.4
                risks.append("revenue growth decelerating")

        if isinstance(eps_growth, float):
            if eps_growth >= 20:
                score += 0.75
                reasons.append("EPS growth strong")
            elif eps_growth <= -10:
                score -= 0.65
                risks.append("EPS pressure")

        if profitable is True:
            score += 0.35
            reasons.append("latest quarter profitable")
        elif profitable is False:
            score -= 0.35
            risks.append("latest quarter unprofitable")

        # Absolute margins are not comparable across industries (e.g. retail vs software).
        if isinstance(margin, float) and margin < 0:
            score -= 0.25
            risks.append("negative gross margin")

        if isinstance(dilution, float):
            if dilution >= 4:
                score -= 0.6
                risks.append("share count dilution")
            elif dilution <= -2:
                score += 0.2
                reasons.append("share count stable or shrinking")

        return round(score, 2), self._dedupe(reasons)[:4], self._dedupe(risks)[:4]

    def _fresh_filings(self, filings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        today = datetime.now(UTC).date()
        cutoff = today - timedelta(days=14)
        fresh = []
        for filing in filings:
            try:
                filed = date.fromisoformat(str(filing.get("filing_date") or "")[:10])
            except ValueError:
                continue
            if cutoff <= filed <= today:
                fresh.append(filing)
        return fresh

    def _catalyst_signal(
        self,
        sec_context: dict[str, Any],
        news_score: float,
        headline_reasons: list[str],
        news_risks: list[str],
        earnings_reasons: list[str],
    ) -> tuple[float, list[str], list[str]]:
        filings = [filing for filing in self._fresh_filings(sec_context.get("filings") or [])
                   if filing.get("form_type") not in {"4", "4/A", "13F-HR", "SC 13G", "SC 13G/A"}]
        filing_score = sum(float(filing.get("signal_score") or 0.0) for filing in filings[:4])
        filing_reasons = self._dedupe([signal for filing in filings[:3] for signal in (filing.get("signals") or [])])[:4]
        filing_risks = self._dedupe([signal for filing in filings[:3] for signal in (filing.get("risk_flags") or [])])[:4]

        score = round(news_score + filing_score, 2)
        reasons = self._dedupe(headline_reasons + filing_reasons + earnings_reasons)[:6]
        risks = self._dedupe(news_risks + filing_risks)[:5]
        return score, reasons, risks

    def _flow_signal(self, history_metrics: dict[str, Any], news_score: float) -> tuple[float, list[str], list[str]]:
        score = 0.0
        reasons: list[str] = []
        risks: list[str] = []
        volume_ratio = history_metrics.get("volume_vs_20d")
        month_return = history_metrics.get("return_1m_pct")

        if isinstance(volume_ratio, float):
            if volume_ratio >= 1.8:
                score += 0.6
                reasons.append("volume elevated vs 20-day average")
            elif volume_ratio <= 0.55:
                score -= 0.2

        if isinstance(month_return, float) and month_return >= 12 and news_score > 0:
            score += 0.45
            reasons.append("positive flow after recent catalyst")
        elif isinstance(month_return, float) and month_return <= -10 and news_score < 0:
            score -= 0.45
            risks.append("negative flow confirms weakness")

        return round(score, 2), reasons[:3], risks[:3]

    def _risk_penalty(
        self,
        *,
        risk_flags: list[str],
        history_metrics: dict[str, Any],
        holding_weight: float,
        asset_class: str,
    ) -> float:
        penalty = 0.0
        penalty += min(1.6, 0.22 * len(risk_flags))
        distance_high = history_metrics.get("distance_from_52w_high_pct")
        if isinstance(distance_high, float) and distance_high >= -2 and asset_class == "stock":
            penalty += 0.15
        if asset_class == "stock" and holding_weight >= self.settings.max_single_stock_allocation_pct * 0.9:
            penalty += 0.55
        if asset_class == "etf" and holding_weight >= self.settings.max_single_etf_allocation_pct * 0.9:
            penalty += 0.35
        return round(penalty, 2)

    def _watch_reasons(self, reasons: list[str], risks: list[str]) -> list[str]:
        combined = reasons[:2]
        if risks:
            combined.append("watch risk: " + risks[0])
        return self._dedupe(combined)[:3]

    def _why_now(self, packet: dict[str, Any]) -> str:
        candidates = []
        candidates.extend(packet.get("catalyst_reasons") or [])
        candidates.extend(packet.get("trend_reasons") or [])
        candidates.extend(packet.get("flow_reasons") or [])
        if not candidates:
            return "No strong fresh catalyst yet; monitor for confirmation."
        return "; ".join(candidates[:2])

    def _is_etf(self, symbol: str, holding: Holding | None = None) -> bool:
        if symbol in ETF_SYMBOLS:
            return True
        if holding:
            try:
                asset_type = str(holding.asset_type or "").lower()
            except Exception:
                asset_type = ""
            if asset_type in {"etf", "fund"}:
                return True
        return False

    def _risk_from_decision(self, confidence: str, risk_flags: list[str], action: str) -> str:
        if action in {"reduce", "exit"}:
            return "medium"
        if risk_flags:
            return "medium"
        if confidence == "high":
            return "low"
        if confidence == "medium":
            return "medium"
        return "high"

    async def quote_map(self, session: Session, symbols: list[str]) -> dict[str, dict]:
        normalized_symbols = [symbol for symbol in symbols if symbol]
        if not normalized_symbols:
            return {}
        result: dict[str, dict] = {}
        chunk_size = max(1, self.settings.twelve_data_max_symbols_per_request)
        semaphore = asyncio.Semaphore(3)

        async def fetch(chunk):
            async with semaphore:
                return await self._get_quote_payload(session, chunk)

        responses = await asyncio.gather(*(
            fetch(normalized_symbols[idx:idx + chunk_size])
            for idx in range(0, len(normalized_symbols), chunk_size)
        ), return_exceptions=True)
        for response in responses:
            if isinstance(response, Exception):
                continue
            payload, source = response
            items = self._extract_quote_items(payload)
            for item in items:
                symbol = item.get("symbol")
                if not symbol:
                    continue
                normalized = dict(item)
                normalized["source_status"] = source
                result[symbol] = normalized
        return result

    async def _analyze_stock_universe(
        self, session: Session
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[StockIdea]]:
        universe = self._priority_stock_universe(session)
        if not universe:
            return [], {}, []

        raw_quotes = await self.quote_map(session, universe)
        position_map = self._position_map(session)
        benchmark_history = await self._get_history(session, "SPY")
        benchmark_metrics = self._history_metrics(benchmark_history)

        history_payloads = await asyncio.gather(
            *(self._get_history(session, symbol) for symbol in universe),
            return_exceptions=True,
        )
        history_map: dict[str, list[dict[str, Any]]] = {}
        for symbol, history_payload in zip(universe, history_payloads, strict=False):
            if isinstance(history_payload, Exception):
                history_map[symbol] = []
            else:
                history_map[symbol] = history_payload

        triaged: list[tuple[float, str, dict[str, Any]]] = []
        for symbol in universe:
            quote = raw_quotes.get(symbol) or {}
            close = float(quote.get("close") or 0.0)
            prev_close = float(quote.get("previous_close") or 0.0)
            if close <= 0 or prev_close <= 0:
                continue
            history_metrics = self._history_metrics(history_map.get(symbol) or [])
            history_metrics.update(risk_metrics(history_map.get(symbol) or []))
            trend_score, trend_reasons, trend_risks = self._trend_signal(history_metrics, benchmark_metrics)
            holding = position_map.get(symbol)
            weight = self._holding_weight(session, symbol)
            triage_bonus = 1.0 if holding else 0.0
            if symbol in self.settings.starter_symbols:
                triage_bonus += 0.25
            triaged.append(
                (
                    trend_score + triage_bonus,
                    symbol,
                    {
                        "quote": quote,
                        "history_metrics": history_metrics,
                        "trend_score": trend_score,
                        "trend_reasons": trend_reasons,
                        "trend_risks": trend_risks,
                        "holding": holding,
                        "holding_weight": weight,
                    },
                )
            )

        triaged.sort(key=lambda item: item[0], reverse=True)
        from app.services.stock_advisor import StockAdvisorService
        shortlist = balanced_shortlist(triaged, self.settings.llm_max_stock_symbols, StockAdvisorService(self).sector_for_symbol)
        shortlist_symbols = [item[1] for item in shortlist]
        shortlist_context = {symbol: context for _score, symbol, context in shortlist}

        news_payloads = await asyncio.gather(
            *(self._get_news_items(session, symbol) for symbol in shortlist_symbols),
            return_exceptions=True,
        )
        calendar_payloads = await asyncio.gather(
            *(
                self._get_calendar(session, symbol)
                if not self._is_etf(symbol, shortlist_context[symbol]["holding"])
                else asyncio.sleep(0, result={})
                for symbol in shortlist_symbols
            ),
            return_exceptions=True,
        )
        sec_payloads = await asyncio.gather(
            *(self.sec.sync_symbol(session, symbol) for symbol in shortlist_symbols),
            return_exceptions=True,
        )

        stock_packets: list[dict[str, Any]] = []
        for index, symbol in enumerate(shortlist_symbols):
            context = shortlist_context[symbol]
            quote = context["quote"]
            close = float(quote.get("close") or 0.0)
            prev_close = float(quote.get("previous_close") or 0.0)
            if close <= 0 or prev_close <= 0:
                continue
            change_pct = ((close - prev_close) / prev_close) * 100.0
            holding = context["holding"]
            history_metrics = context["history_metrics"]
            asset_class = "etf" if self._is_etf(symbol, holding) else "stock"
            news_items = [] if isinstance(news_payloads[index], Exception) else news_payloads[index]
            calendar = {} if isinstance(calendar_payloads[index], Exception) else calendar_payloads[index]
            sec_context = (
                {
                    "symbol": symbol,
                    "company_name": symbol,
                    "fundamentals": {},
                    "filings": [],
                    "catalysts": [],
                }
                if isinstance(sec_payloads[index], Exception)
                else sec_payloads[index]
            )
            news_score, headline_reasons, news_risks = self._news_signal(news_items)
            earnings_score, earnings_reasons = self._earnings_signal(calendar)
            trend_score = float(context["trend_score"])
            trend_reasons = list(context["trend_reasons"])
            trend_risks = list(context["trend_risks"])
            fundamental_score, fundamental_reasons, fundamental_risks = self._fundamental_signal(sec_context)
            catalyst_score, catalyst_reasons, catalyst_risks = self._catalyst_signal(
                sec_context,
                news_score + earnings_score,
                headline_reasons,
                news_risks,
                earnings_reasons,
            )
            flow_score, flow_reasons, flow_risks = self._flow_signal(history_metrics, news_score)
            weight = float(context["holding_weight"])
            base_risk_flags = self._dedupe(
                trend_risks
                + fundamental_risks
                + catalyst_risks
                + flow_risks
                + (["position is already concentrated"] if weight >= 0.18 else [])
            )
            risk_penalty = self._risk_penalty(
                risk_flags=base_risk_flags,
                history_metrics=history_metrics,
                holding_weight=weight,
                asset_class=asset_class,
            )
            composite_score = round(
                0.30 * trend_score
                + 0.30 * fundamental_score
                + 0.25 * catalyst_score
                + 0.15 * flow_score
                - risk_penalty,
                2,
            )
            recent_state = self._recent_recommendation_state(session, symbol)
            stock_packets.append(
                {
                    "symbol": symbol,
                    "company_name": sec_context.get("company_name") or symbol,
                    "currently_held": bool(holding),
                    "holding_value": round(holding.market_value, 2) if holding else 0.0,
                    "holding_weight": round(weight, 4),
                    "quantity": holding.quantity if holding else 0.0,
                    "asset_class": asset_class,
                    "quote_source": quote.get("source_status") or "unknown",
                    "close": round(close, 4),
                    "previous_close": round(prev_close, 4),
                    "momentum_pct": round(change_pct, 2),
                    "news_score": round(news_score, 2),
                    "headline_reasons": headline_reasons,
                    "earnings_reasons": earnings_reasons,
                    "filing_score": round(catalyst_score, 2),
                    "filing_signals": [signal for filing in self._fresh_filings(sec_context.get("filings") or [])[:3] for signal in (filing.get("signals") or [])][:4],
                    "risk_flags": base_risk_flags,
                    "news_headlines": [item for item in news_items if self._news_published_recently(item)][:3],
                    "upcoming_earnings": earnings_reasons[0] if earnings_reasons else None,
                    "recent_filings": self._fresh_filings(sec_context.get("filings") or [])[:4],
                    "fundamentals": sec_context.get("fundamentals") or {},
                    "sec_catalysts": catalyst_reasons,
                    "history_metrics": history_metrics,
                    "trend_score": trend_score,
                    "trend_reasons": trend_reasons,
                    "fundamental_score": fundamental_score,
                    "fundamental_reasons": fundamental_reasons,
                    "catalyst_score": catalyst_score,
                    "catalyst_reasons": catalyst_reasons,
                    "flow_score": flow_score,
                    "flow_reasons": flow_reasons,
                    "risk_penalty": risk_penalty,
                    "composite_score": composite_score,
                    "why_now": self._why_now(
                        {
                            "trend_reasons": trend_reasons,
                            "catalyst_reasons": catalyst_reasons,
                            "flow_reasons": flow_reasons,
                        }
                    ),
                    "recent_state": recent_state,
                }
            )

        now = datetime.now(UTC)
        decisions = await self._stock_decisions(session, stock_packets)
        decision_map = {decision.symbol: decision for decision in decisions}
        total_value = max(self._current_total_value(session), 1.0)
        ideas: list[StockIdea] = []
        for packet in stock_packets:
            decision = decision_map.get(packet["symbol"])
            if not decision:
                continue
            gaps = evidence_gaps(packet)
            if decision.action == "add" and (gaps or float(packet.get("trend_score") or 0) <= 0):
                decision.action = "hold" if packet.get("currently_held") else "watch"
                decision.sizing_intent = "none"
                decision.confidence = "low"
                decision.risk_flags = list(dict.fromkeys(decision.risk_flags + gaps + ["buy evidence is incomplete"]))
                decision.thesis_summary = "Wait for corroborating evidence before adding capital."
            packet_score = float(packet.get("composite_score") or 0.0)
            recent_state = packet.get("recent_state") or {}
            amount_usd = self._size_stock_decision(
                decision.action,
                decision.sizing_intent,
                packet,
                total_value=total_value,
            )
            if decision.action in {"add"} and amount_usd < 10 and not packet.get("currently_held"):
                continue
            if (
                decision.action == "add"
                and recent_state.get("recent_side") == "buy"
                and recent_state.get("hours_since_last") is not None
                and float(recent_state["hours_since_last"]) < self.settings.stock_recent_recommendation_cooldown_hours
                and packet_score < self.settings.stock_significant_score_delta + 2.5
            ):
                continue

            risk = self._risk_from_decision(decision.confidence, decision.risk_flags, decision.action)
            summary_parts = [decision.thesis_summary]
            why_line = str(packet.get("why_now") or "").strip()
            if not why_line and decision.key_reasons:
                why_line = "; ".join(decision.key_reasons[:3])
            if why_line:
                summary_parts.append("Why now: " + why_line)
            if decision.risk_flags:
                summary_parts.append("Risk watch: " + "; ".join(decision.risk_flags[:2]))
            compact_summary = " ".join(summary_parts).strip()

            action_priority = {
                "add": 5.0,
                "reduce": 4.0,
                "exit": 4.5,
                "hold": 3.0,
                "watch": 2.0,
                "avoid": 1.5,
            }.get(decision.action, 1.0)
            confidence_boost = {"high": 1.5, "medium": 1.0, "low": 0.5}.get(decision.confidence, 0.5)
            priority_score = action_priority + confidence_boost + max(0.0, packet_score)
            if decision.action == "watch":
                priority_score += max(0.0, packet_score * 0.4)

            side = {
                "add": "buy",
                "reduce": "sell",
                "exit": "sell",
                "hold": "hold",
                "watch": "watch",
                "avoid": "watch",
            }[decision.action]
            ideas.append(
                StockIdea(
                    symbol=packet["symbol"],
                    recommendation_type=packet["asset_class"],
                    side=side,
                    amount_usd=round(amount_usd, 2),
                    reason=compact_summary.strip(),
                    risk=risk,
                    change_pct=float(packet["momentum_pct"]),
                    source_status=packet["quote_source"],
                    priority_score=priority_score,
                    confidence=decision.confidence,
                )
            )

        packet_map = {packet["symbol"]: packet for packet in stock_packets}
        self._rebalance_add_sizes(session, ideas, packet_map)
        from app.services.stock_strategy import apply_portfolio_strategy
        strategy = await apply_portfolio_strategy(self, session, ideas, history_map, benchmark_history)
        ideas.sort(key=lambda item: item.priority_score, reverse=True)
        evidence_snapshot = {
            "as_of": now.isoformat(), "analysis_mode": self.llm.stock_analysis_mode,
            "universe_count": len(universe), "packets": stock_packets,
            "decisions": [decision.model_dump() for decision in decisions],
            "ideas": [vars(idea) for idea in ideas],
            "portfolio_strategy": strategy,
        }
        self.cache.set_json(session, "stock_intelligence", "latest", evidence_snapshot)
        # Bounded prospective samples, not a backtest reconstructed from future filings.
        samples = self.cache.get_json_stale(session, "stock_intelligence", "evaluation_samples") or []
        if not samples or (now - datetime.fromisoformat(samples[-1]["as_of"])).total_seconds() >= 1800:
            samples.append({"as_of": now.isoformat(), "analysis_mode": self.llm.stock_analysis_mode,
                            "symbols": [{"symbol": p["symbol"], "close": p["close"], "score": p["composite_score"]} for p in stock_packets],
                            "decisions": evidence_snapshot["decisions"]})
            self.cache.set_json(session, "stock_intelligence", "evaluation_samples", samples[-336:])
        return stock_packets, packet_map, ideas

    async def _stock_decisions(self, session: Session, packets: list[dict]) -> list[StockDecisionModel]:
        from app.services.stock_analysis_cache import analysis_fingerprint, prices_within_reuse_window

        now = datetime.now(UTC)
        context = self._portfolio_context(session)
        fingerprint = analysis_fingerprint(context, packets, self.settings.gemini_model_analyst)
        health = self.cache.get_json_stale(session, "stock_intelligence", "llm_health") or {}
        budget_day = now.date().isoformat()
        used = int(health.get("requests_today") or 0) if health.get("budget_day") == budget_day else 0

        def finish(decisions, mode, next_request_at=None):
            self.llm.stock_analysis_mode = mode
            self.cache.set_json(session, "stock_intelligence", "llm_scan", {
                "checked_at": now.isoformat(), "mode": mode, "budget_day": budget_day,
                "budget_timezone": "UTC", "requests_today": used,
                "request_limit": self.settings.stock_llm_daily_request_budget,
                "packet_count": len(packets), "next_request_at": next_request_at,
            })
            return decisions

        if not packets:
            return finish([], "rules_only_no_candidates")
        if not self.llm.configured():
            return finish(self.llm._fallback_stock_decisions(packets), "rules_only_not_configured")
        cached = self.cache.get_json(session, "stock_intelligence", "llm_result", max_age_seconds=3600)
        if (isinstance(cached, dict) and cached.get("fingerprint") == fingerprint
                and prices_within_reuse_window(packets, cached.get("prices") or {})):
            try:
                decisions = [StockDecisionModel.model_validate(row) for row in cached["decisions"]]
                if {d.symbol for d in decisions} == {p["symbol"] for p in packets} and len(decisions) == len(packets):
                    return finish(decisions, "gemini_cached")
            except (ValueError, TypeError, KeyError):
                pass
        try:
            retry_at = datetime.fromisoformat(health.get("retry_after") or "")
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            retry_at = now
        if retry_at > now or used >= self.settings.stock_llm_daily_request_budget:
            mode = "rules_only_quota_backoff" if retry_at > now else "rules_only_daily_budget"
            reset = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
            return finish(self.llm._fallback_stock_decisions(packets), mode,
                          max(retry_at, reset if used >= self.settings.stock_llm_daily_request_budget else now).isoformat())
        from zoneinfo import ZoneInfo
        eastern = now.astimezone(ZoneInfo("America/New_York"))
        market_window = eastern.weekday() < 5 and 510 <= eastern.hour * 60 + eastern.minute < 1020
        offhours_used = int(health.get("requests_offhours") or 0) if health.get("budget_day") == now.date().isoformat() else 0
        try:
            last_request = datetime.fromisoformat(health.get("last_request_at") or "")
        except (ValueError, TypeError):
            last_request = None
        if last_request is not None and last_request.tzinfo is None:
            last_request = last_request.replace(tzinfo=UTC)
        interval = 2700 if market_window else 14400
        if ((last_request is not None and (now-last_request).total_seconds() < interval)
                or (not market_window and offhours_used >= max(1, self.settings.stock_llm_daily_request_budget // 6))):
            mode = "rules_only_request_spacing" if market_window else "rules_only_market_reserve"
            next_at = (last_request + timedelta(seconds=interval)).isoformat() if last_request else None
            # An off-hours cap may last beyond the spacing window; do not promise an early request.
            if not market_window and offhours_used >= max(1, self.settings.stock_llm_daily_request_budget // 6):
                next_at = None
            return finish(self.llm._fallback_stock_decisions(packets), mode, next_at)
        # Reserve before the request so a timeout cannot bypass the persisted budget.
        state = {"checked_at": now.isoformat(), "last_request_at": now.isoformat(), "budget_day": now.date().isoformat(),
                 "requests_today": used + 1, "requests_offhours": offhours_used + int(not market_window)}
        used += 1
        self.cache.set_json(session, "stock_intelligence", "llm_health", state)
        decisions = await self.llm.analyze_stocks(portfolio_context=context, stock_packets=packets)
        retry_at = getattr(self.llm, "_stock_retry_after", None)
        state.update({"mode": self.llm.stock_analysis_mode, "retry_after": retry_at.isoformat() if retry_at else None})
        self.cache.set_json(session, "stock_intelligence", "llm_health", state)
        if self.llm.stock_analysis_mode in {"gemini", "gemini_cached"}:
            self.cache.set_json(session, "stock_intelligence", "llm_result", {
                "fingerprint": fingerprint, "decisions": [d.model_dump() for d in decisions],
                "prices": {p["symbol"]: p.get("close") for p in packets}})
        return finish(decisions, self.llm.stock_analysis_mode)

    async def top_stock_ideas(self, session: Session, limit: int = 7) -> list[StockIdea]:
        ideas, _news = await self.stock_report(session, stock_limit=limit, news_limit=3)
        return ideas

    async def important_news_briefs(self, session: Session, limit: int = 3) -> list[NewsBrief]:
        _ideas, briefs = await self.stock_report(session, stock_limit=10, news_limit=limit)
        return briefs

    async def stock_report(
        self,
        session: Session,
        *,
        stock_limit: int = 10,
        news_limit: int = 3,
    ) -> tuple[list[StockIdea], list[NewsBrief]]:
        stock_packets, _packet_map, ideas = await self._analyze_stock_universe(session)
        employment_excluded = self.settings.employment_excluded_symbols
        if employment_excluded:
            ideas = [idea for idea in ideas if idea.symbol.upper() not in employment_excluded]
            stock_packets = [packet for packet in stock_packets if str(packet.get("symbol") or "").upper() not in employment_excluded]
        briefs: list[NewsBrief] = []
        for packet in stock_packets:
            symbol = packet["symbol"]
            composite_score = float(packet.get("composite_score") or 0.0)
            for headline in packet.get("news_headlines") or []:
                title = (headline.get("title") or "").strip()
                if not title:
                    continue
                article_score, article_reasons, article_risks = article_evidence(headline)
                if not article_reasons and not article_risks:
                    continue
                priority = abs(article_score) + max(0.0, composite_score)
                reasons = article_reasons + article_risks
                briefs.append(
                    NewsBrief(
                        symbol=symbol,
                        headline=title,
                        reason=" | ".join(reasons[:2]) if reasons else "headline worth watching",
                        priority_score=priority,
                    )
                )
                break

            filings = [row for row in packet.get("recent_filings") or [] if row.get("form_type") not in {"4", "4/A"}]
            if filings:
                filing = filings[0]
                signal_parts = (filing.get("signals") or []) + (filing.get("risk_flags") or [])
                signal = "; ".join(signal_parts[:2]) or filing.get("signal_summary") or filing.get("description") or ""
                if signal:
                    briefs.append(
                        NewsBrief(
                            symbol=symbol,
                            headline=f"{filing.get('form_type', 'SEC filing')}: {signal}",
                            reason="SEC filing update",
                            priority_score=abs(float(packet.get("filing_score") or 0.0)) + max(0.0, composite_score) + 0.6,
                        )
                    )
        briefs.sort(key=lambda item: item.priority_score, reverse=True)
        unique: list[NewsBrief] = []
        seen: set[tuple[str, str]] = set()
        for brief in briefs:
            key = (brief.symbol, brief.headline)
            if key in seen:
                continue
            unique.append(brief)
            seen.add(key)
            if len(unique) >= news_limit:
                break
        # Hidden HOLD rows must not consume the visible report's idea slots.
        visible = [idea for idea in ideas if idea.side != "hold"]
        # Recheck ownership after network awaits: positions may have changed during analysis.
        positions = self._position_map(session)
        visible = [idea for idea in visible if idea.side != "sell" or idea.symbol in positions]
        for idea in visible:
            if idea.side == "sell":
                idea.amount_usd = min(idea.amount_usd, positions[idea.symbol].market_value)
        return visible[:stock_limit], unique

    def _size_stock_decision(
        self,
        action: str,
        sizing_intent: str,
        packet: dict[str, Any],
        *,
        total_value: float,
    ) -> float:
        holding_value = float(packet.get("holding_value") or 0.0)
        weight = float(packet.get("holding_weight") or 0.0)
        asset_class = packet.get("asset_class") or "stock"
        max_alloc_pct = (
            self.settings.max_single_etf_allocation_pct if asset_class == "etf" else self.settings.max_single_stock_allocation_pct
        )
        remaining_alloc_dollars = max(0.0, max_alloc_pct * total_value - holding_value)
        intent_weights = {
            "tiny": 0.0075,
            "small": 0.015,
            "medium": 0.03,
            "large": 0.05,
        }
        if action == "add":
            base = total_value * intent_weights.get(sizing_intent, 0.015)
            volatility = float((packet.get("history_metrics") or {}).get("annualized_volatility_pct") or 40)
            base *= min(1.0, 30.0 / max(volatility, 1.0))
            return min(base, remaining_alloc_dollars)
        if action == "reduce":
            reduce_frac = {"tiny": 0.1, "small": 0.18, "medium": 0.33, "large": 0.5}.get(sizing_intent, 0.25)
            if weight >= max_alloc_pct:
                reduce_frac = max(reduce_frac, 0.33)
            return holding_value * reduce_frac
        if action == "exit":
            return holding_value
        return 0.0

    def _rebalance_add_sizes(
        self,
        session: Session,
        ideas: list[StockIdea],
        packet_map: dict[str, dict[str, Any]],
    ) -> None:
        add_ideas = [idea for idea in ideas if idea.side == "buy" and idea.symbol in packet_map]
        if not add_ideas:
            return

        buying_power = max(0.0, self._current_buying_power(session))
        prediction_reserve = max(0.0, buying_power * self.settings.max_total_prediction_bankroll_pct)
        deployable_stock_cash = max(0.0, buying_power - self.settings.minimum_cash_buffer_usd - prediction_reserve)
        if deployable_stock_cash <= 0:
            return

        weighted: list[tuple[StockIdea, float, float]] = []
        total_weight = 0.0
        for idea in add_ideas:
            packet = packet_map[idea.symbol]
            confidence_weight = {"high": 1.35, "medium": 1.0, "low": 0.7}.get(idea.confidence, 1.0)
            conviction = max(0.25, float(packet.get("composite_score") or 0.0) + 0.75)
            novelty_bonus = 1.12 if not packet.get("currently_held") else 0.92
            weight = confidence_weight * conviction * novelty_bonus
            max_size = max(0.0, min(
                idea.amount_usd, deployable_stock_cash * 0.20,
                self._max_remaining_allocation_dollars(packet, total_value=max(self._current_total_value(session), 1.0)),
            ))
            weighted.append((idea, weight, max_size))
            total_weight += weight

        if total_weight <= 0:
            return

        remaining_cash = deployable_stock_cash
        remaining_weight = total_weight
        assigned: dict[str, float] = {}

        for idea, weight, max_size in sorted(weighted, key=lambda item: item[1], reverse=True):
            if remaining_cash <= 0:
                assigned[idea.symbol] = 0.0
                continue
            proportional = remaining_cash * (weight / remaining_weight) if remaining_weight > 0 else 0.0
            floor = min(60.0, remaining_cash)
            target = max(proportional, floor if idea.confidence == "high" else min(35.0, remaining_cash))
            amount = min(max_size, target, remaining_cash)
            if amount < 10:
                amount = min(max_size, remaining_cash)
            amount = round(max(0.0, amount), 2)
            assigned[idea.symbol] = amount
            remaining_cash = round(max(0.0, remaining_cash - amount), 2)
            remaining_weight -= weight

        for idea in add_ideas:
            if idea.symbol in assigned:
                idea.amount_usd = assigned[idea.symbol]

    def _max_remaining_allocation_dollars(self, packet: dict[str, Any], *, total_value: float) -> float:
        holding_value = float(packet.get("holding_value") or 0.0)
        asset_class = packet.get("asset_class") or "stock"
        max_alloc_pct = (
            self.settings.max_single_etf_allocation_pct if asset_class == "etf" else self.settings.max_single_stock_allocation_pct
        )
        return max(0.0, max_alloc_pct * total_value - holding_value)

    async def top_event_ideas(
        self,
        session: Session,
        limit: int = 4,
        *,
        force_recommendations: bool = False,
        proactive_manual: bool = False,
    ) -> list[EventIdea]:
        try:
            robinhood_limit = max(limit, self.settings.llm_max_event_candidates) if force_recommendations else max(limit * 4, self.settings.llm_max_event_candidates)
            event_packets = await self._discover_robinhood_event_packets(
                limit=robinhood_limit,
                same_day_only=force_recommendations,
            )
            if not event_packets:
                event_payloads = await self._discover_today_event_payloads()
                if not event_payloads:
                    event_payloads = await self._discover_broker_event_payloads()
                event_packets = []
                for event in event_payloads:
                    packet = self._build_event_packet(event)
                    if packet is None:
                        continue
                    if not packet.get("is_relevant_now"):
                        continue
                    event_packets.append(packet)
        except Exception as exc:  # noqa: BLE001
            return [
                EventIdea(
                    ticker="EVENT-DATA-UNAVAILABLE",
                    title="Prediction market data unavailable",
                    side="skip",
                    amount_usd=0.0,
                    reason=f"Kalshi fetch failed: {exc}",
                    risk="high",
                    pick_label="Skip",
                    implied_probability=0.0,
                    source_status="degraded",
                    priority_score=0.0,
                    confidence="low",
                )
            ]

        portfolio_context = self._portfolio_context(session)
        suppressed_event_codes = self._suppressed_event_codes(session)
        event_packets = [
            self._enrich_event_packet_with_pricing(session, packet)
            for packet in event_packets
            if str(packet["event_ticker"]) not in suppressed_event_codes
        ]
        if force_recommendations:
            event_packets = [
                packet
                for packet in event_packets
                if packet.get("is_same_day") and packet.get("is_relevant_now")
            ]
        else:
            event_packets = [
                packet
                for packet in event_packets
                if packet.get("edge_yes", 0.0) >= self.settings.event_min_quant_edge
                or packet.get("edge_no", 0.0) >= self.settings.event_min_quant_edge
            ]
        event_packets.sort(
            key=lambda packet: (
                1 if self._is_primary_event_market(packet) else 0,
                1 if packet.get("is_same_day") else 0,
                1 if packet.get("is_relevant_now") else 0,
                round(max(float(packet.get("edge_yes") or 0.0), float(packet.get("edge_no") or 0.0)), 4),
                round(float(packet.get("liquidity_dollars") or 0.0), 2),
                round(float(packet.get("priority_hint") or 0.0), 2),
            ),
            reverse=True,
        )
        primary_packets = [packet for packet in event_packets if self._is_primary_event_market(packet)]
        if force_recommendations:
            event_packets = event_packets[:limit]
        elif primary_packets:
            event_packets = primary_packets[: self.settings.llm_max_event_candidates]
        else:
            event_packets = event_packets[: self.settings.llm_max_event_candidates]

        decisions = await self.llm.analyze_events(
            portfolio_context=portfolio_context,
            event_packets=event_packets,
            force_recommendations=force_recommendations,
        )
        if not any(
            decision.action != "skip"
            and not (
                next((packet for packet in event_packets if packet["event_ticker"] == decision.event_ticker), {}).get(
                    "explicit_pick_only"
                )
                and decision.action != "buy_yes"
            )
            for decision in decisions
        ):
            decisions = self.llm._fallback_event_decisions(event_packets)
        decision_map = {decision.event_ticker: decision for decision in decisions}
        buying_power = self._current_buying_power(session)
        bankroll = max(0.0, buying_power * self.settings.max_total_prediction_bankroll_pct)
        ideas: list[EventIdea] = []
        for packet in event_packets:
            decision = decision_map.get(packet["event_ticker"])
            if not decision or decision.action == "skip":
                continue
            if packet.get("explicit_pick_only") and decision.action != "buy_yes":
                continue
            pick_side = "YES" if decision.action == "buy_yes" else "NO"
            pick_label = packet["yes_label"] if decision.action == "buy_yes" else packet["no_label"]
            selected_probability = (
                float(packet.get("yes_entry_price") or packet.get("yes_price") or 0.0)
                if decision.action == "buy_yes"
                else float(packet.get("no_entry_price") or packet.get("no_price") or 0.0)
            )
            if not self._event_metadata_quality_ok(packet):
                continue
            category = str(packet.get("event_type") or packet.get("category") or "")
            blocked_category = category.lower() in self._blocked_new_pick_categories()
            if self._is_bad_live_sports_new_pick(packet, selected_probability):
                continue
            if packet.get("explicit_pick_only") and selected_probability >= 0.86 and decision.confidence != "high":
                continue
            learning_priority_adj = float(packet.get("learning_priority_adj") or 0.0)
            learning_stake_mult = float(packet.get("learning_stake_mult") or 1.0)
            learning_reasons = list(packet.get("learning_reasons") or [])
            estimated_yes_probability = (
                float(decision.estimated_yes_probability)
                if decision.estimated_yes_probability is not None and 0 < decision.estimated_yes_probability < 1
                else float(packet.get("fair_yes_probability") or packet.get("yes_price") or 0.0)
            )
            estimated_no_probability = max(0.0, min(1.0, 1.0 - estimated_yes_probability))
            selected_fair_probability = estimated_yes_probability if decision.action == "buy_yes" else estimated_no_probability
            if decision.action == "buy_yes":
                edge_for_side = float(packet.get("edge_yes") or 0.0)
                ev_per_contract = float(packet.get("ev_yes_per_contract") or 0.0)
                kelly_fraction = float(packet.get("kelly_yes") or 0.0)
            else:
                edge_for_side = float(packet.get("edge_no") or 0.0)
                ev_per_contract = float(packet.get("ev_no_per_contract") or 0.0)
                kelly_fraction = float(packet.get("kelly_no") or 0.0)
            required_manual_edge = self._manual_event_required_edge(session, packet, decision.confidence)
            if not force_recommendations and (edge_for_side < required_manual_edge or ev_per_contract <= 0):
                continue
            if blocked_category and edge_for_side < 0.10:
                continue
            forced_small_only = (
                force_recommendations
                and not proactive_manual
                and (edge_for_side < self.settings.event_min_quant_edge or ev_per_contract <= 0)
            )
            proactive_manual_override = (
                force_recommendations
                and proactive_manual
                and self._is_primary_event_market(packet)
                and 0.18 <= selected_probability <= 0.82
            )
            if force_recommendations and not proactive_manual and (edge_for_side < self.settings.event_min_quant_edge or ev_per_contract <= 0):
                base_forced_amount = 2.0
                required_stake = self._minimum_stake_for_target_profit(
                    selected_probability,
                    self.settings.event_min_forced_potential_profit_usd,
                )
                max_tiny_amount = self._size_event_decision(
                    "tiny",
                    buying_power,
                    bankroll,
                    stake_multiplier=learning_stake_mult,
                    kelly_fraction=kelly_fraction,
                    edge=max(0.0, edge_for_side),
                )
                if required_stake > max_tiny_amount:
                    continue
                amount_usd = max(base_forced_amount, required_stake)
            elif proactive_manual_override and (edge_for_side < self.settings.event_min_quant_edge or ev_per_contract <= 0):
                manual_intent = "medium" if decision.confidence == "high" else "small"
                amount_usd = self._size_event_decision(
                    manual_intent,
                    buying_power,
                    bankroll,
                    stake_multiplier=max(0.75, learning_stake_mult * 0.9),
                    kelly_fraction=max(0.0, kelly_fraction),
                    edge=max(0.0, edge_for_side),
                )
            else:
                amount_usd = self._size_event_decision(
                    decision.stake_intent,
                    buying_power,
                    bankroll,
                    stake_multiplier=learning_stake_mult,
                    kelly_fraction=kelly_fraction,
                    edge=edge_for_side,
                )
            amount_usd, sizing_notes = self._cap_event_stake_for_regime(
                session,
                amount_usd,
                packet,
                edge=edge_for_side,
                confidence=decision.confidence,
            )
            if amount_usd < 1:
                continue
            max_return = self._event_max_return(amount_usd, selected_probability)
            potential_profit = self._event_potential_profit(amount_usd, selected_probability)
            if forced_small_only:
                if potential_profit < self.settings.event_min_forced_potential_profit_usd:
                    continue
            reasons = [
                decision.thesis_summary,
                f"Market implied: {selected_probability * 100:.1f}%.",
                f"Finn fair: {selected_fair_probability * 100:.1f}%.",
                f"Edge: {edge_for_side * 100:.1f} pts.",
                f"Friction: {float(packet.get('friction_yes') if decision.action == 'buy_yes' else packet.get('friction_no') or 0.0) * 100:.1f} pts.",
                f"EV/contract: ${ev_per_contract:.03f}.",
                f"Kelly cap: {kelly_fraction * 100:.1f}%.",
                f"Max return: ${max_return:.2f}.",
                f"Potential profit: ${potential_profit:.2f}.",
            ]
            if forced_small_only:
                reasons.append("Sizing: forced small stake only because the payout math still clears the minimum profit floor.")
            elif proactive_manual_override and (edge_for_side < self.settings.event_min_quant_edge or ev_per_contract <= 0):
                reasons.append("Sizing: proactive manual stake before full edge confirmation because the main market is near start and still readable.")
            reasons.extend(sizing_notes)
            if decision.key_reasons:
                reasons.append("Why: " + "; ".join(decision.key_reasons[:3]))
            if learning_reasons:
                reasons.append("History: " + "; ".join(learning_reasons))
            if packet.get("event_day"):
                reasons.append(f"Event day: {packet['event_day']}.")
            bet_description = self._event_bet_description(packet, decision.action)
            probability_fit = max(0.0, 1.0 - abs(selected_probability - 0.64) / 0.24)
            ideas.append(
                EventIdea(
                    ticker=packet["event_ticker"],
                    title=self._clean_event_title(packet["event_title"], packet.get("sub_title")),
                    side=pick_side,
                    amount_usd=round(amount_usd, 2),
                    reason=" ".join(reasons),
                    risk="high" if decision.confidence != "high" else "medium",
                    pick_label=pick_label,
                    bet_description=bet_description,
                    implied_probability=selected_probability,
                    source_status=str(packet.get("source_status") or "robinhood"),
                    priority_score=(
                        {"high": 3.0, "medium": 2.0, "low": 1.0}.get(decision.confidence, 1.0)
                        + self._event_preference_bonus(packet)
                        + min(2.5, log10(float(packet.get("liquidity_dollars") or 0.0) + 1))
                        + probability_fit
                        + (0.8 if packet.get("is_same_day") else 0.2)
                        + learning_priority_adj
                        + max(0.0, edge_for_side * 35.0)
                        + max(0.0, ev_per_contract * 40.0)
                        + max(0.0, kelly_fraction * 10.0)
                    ),
                    confidence=decision.confidence,
                    event_day=packet.get("event_day"),
                    is_same_day=bool(packet.get("is_same_day")),
                    category=str(packet.get("event_type") or packet.get("category") or ""),
                    time_label=self._format_event_time_label(self._market_event_datetime(packet)),
                    is_relevant_now=bool(packet.get("is_relevant_now")),
                    event_starts_at=self._market_event_datetime(packet),
                    execution_market_ticker=str(packet.get("contract_symbol") or packet.get("market_ticker") or ""),
                    execution_event_ticker=str(packet.get("kalshi_event_ticker") or packet.get("event_ticker") or ""),
                    execution_contract_id=str(packet.get("contract_id") or packet.get("ticker") or ""),
                    contract_path=str(packet.get("contract_path") or ""),
                    max_return_usd=max_return,
                    potential_profit_usd=potential_profit,
                )
            )

        ideas.sort(key=lambda item: (1 if item.is_same_day else 0, item.priority_score), reverse=True)
        same_day_ideas = [idea for idea in ideas if idea.is_same_day and idea.is_relevant_now]
        future_ideas = [idea for idea in ideas if (not idea.is_same_day) and idea.is_relevant_now]
        if force_recommendations:
            return same_day_ideas[:limit]
        if same_day_ideas:
            return same_day_ideas[:limit]
        return future_ideas[:limit]

    def _is_bad_live_sports_new_pick(self, packet: dict[str, Any], selected_probability: float) -> bool:
        category = str(packet.get("event_type") or packet.get("category") or "")
        if category not in {"Basketball", "Baseball", "Soccer", "Cricket", "Tennis", "Sports", "Hockey"}:
            return False
        status = str(packet.get("event_status") or packet.get("source_status") or "").lower()
        if "in_progress" not in status and "live" not in status:
            return False
        if selected_probability <= 0.10:
            return True
        if selected_probability >= 0.92:
            return True
        return False

    async def monitored_event_followups(self, session: Session, limit: int = 4) -> list[EventIdea]:
        open_statuses = {"broker_open", "broker_order_open", "broker_filled", "manual_confirmed", "assumed_open"}
        positions = [
            row
            for row in session.exec(select(EventPosition)).all()
            if row.status in open_statuses and row.broker_contract_id
        ]
        if not positions:
            return []
        watch_map = {
            row.event_key: row
            for row in session.exec(select(EventWatch)).all()
        }

        contract_ids = sorted({row.broker_contract_id for row in positions if row.broker_contract_id})
        try:
            quote_map = await self.robinhood_prediction.get_marketdata_quotes(contract_ids)
            fundamentals_map = await self.robinhood_prediction.get_marketdata_fundamentals(contract_ids)
        except Exception:
            quote_map = {}
            fundamentals_map = {}

        ideas: list[EventIdea] = []
        buying_power = self._current_buying_power(session)
        bankroll = max(0.0, buying_power * self.settings.max_total_prediction_bankroll_pct)
        for position in positions:
            if not position.broker_contract_id:
                continue
            quote = quote_map.get(position.broker_contract_id) or {}
            if not quote:
                continue
            yes_price = self._mid_price(
                quote.get("yes_bid_price"),
                quote.get("yes_ask_price"),
                quote.get("last_trade_price"),
            )
            if yes_price <= 0:
                continue
            no_price = self._mid_price(
                quote.get("no_bid_price"),
                quote.get("no_ask_price"),
                None,
            )
            if no_price <= 0:
                no_price = max(0.0, min(1.0, 1.0 - yes_price))
            quote_symbol = str(quote.get("symbol") or "")
            derived_display = self._decode_robinhood_event_symbol_for_display(quote_symbol)
            raw_title = str(position.event_title or position.event_code or position.broker_contract_id or "")
            raw_pick = str(position.pick_label or raw_title)
            display_title = raw_title
            display_pick = raw_pick
            display_category = position.category or "Event"
            if self._is_uuidish(display_title) or display_title == str(position.broker_contract_id or "") or display_category == "Event":
                display_title = derived_display.get("event_title") or display_title
                display_category = derived_display.get("category") or display_category
            if self._is_uuidish(display_pick) or display_pick == str(position.broker_contract_id or "") or display_pick == raw_title:
                display_pick = derived_display.get("pick_label") or display_pick
            event_time = None
            if position.time_label:
                try:
                    parsed = datetime.strptime(position.time_label, "%I:%M %p")
                    now_local = datetime.now(self.local_tz)
                    event_time = now_local.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
                except ValueError:
                    event_time = None
            packet = {
                "event_ticker": position.broker_event_id or position.event_code or position.broker_contract_id,
                "series_ticker": position.category or "EVENT",
                "event_title": display_title,
                "category": display_category,
                "event_type": display_category,
                "sub_title": display_pick,
                "ticker": position.broker_contract_id,
                "market_ticker": quote_symbol or str(position.broker_contract_id),
                "contract_id": position.broker_contract_id,
                "yes_label": display_pick,
                "no_label": f"Not {display_pick}",
                "yes_price": round(yes_price, 4),
                "no_price": round(no_price, 4),
                "yes_entry_price": round(float(quote.get("yes_ask_price") or yes_price or 0.0), 4),
                "no_entry_price": round(float(quote.get("no_ask_price") or no_price or 0.0), 4),
                "liquidity_dollars": round(
                    float((fundamentals_map.get(position.broker_contract_id) or {}).get("open_interest") or 0.0)
                    + float((fundamentals_map.get(position.broker_contract_id) or {}).get("volume") or 0.0),
                    2,
                ),
                "spread_dollars": round(
                    max(
                        0.0,
                        float(quote.get("yes_ask_price") or 0.0) - float(quote.get("yes_bid_price") or 0.0),
                    ),
                    4,
                ),
                "open_interest": float((fundamentals_map.get(position.broker_contract_id) or {}).get("open_interest") or 0.0),
                "analysis_reasons": [
                    f"current market {yes_price * 100:.1f}% yes",
                    f"entry {float(position.avg_fill_price or 0.0) * 100:.1f}% yes",
                ],
                "is_same_day": True,
                "event_day": self._local_today().isoformat(),
                "explicit_pick_only": True,
                "is_relevant_now": True,
                "event_time": event_time,
                "source_status": "robinhood-live",
            }
            packet = self._enrich_event_packet_with_pricing(
                session,
                packet,
                position_entry_probability=float(position.avg_fill_price or 0.0) if position.avg_fill_price else None,
            )
            entry_probability = float(position.avg_fill_price or 0.0)
            market_move = round(float(packet["yes_price"]) - entry_probability, 4) if entry_probability > 0 else 0.0
            side_edge = float(packet.get("edge_yes") or 0.0)
            ev_per_contract = float(packet.get("ev_yes_per_contract") or 0.0)
            kelly_fraction = float(packet.get("kelly_yes") or 0.0)
            if abs(market_move) < self.settings.event_probability_move_alert_threshold and side_edge < self.settings.event_edge_alert_threshold:
                continue
            if side_edge < self.settings.event_min_quant_edge or ev_per_contract <= 0:
                continue
            stake_intent = "medium" if side_edge >= 0.08 else "small" if side_edge >= 0.05 else "tiny"
            amount_usd = self._size_event_decision(
                stake_intent,
                buying_power,
                bankroll,
                stake_multiplier=0.7,
                kelly_fraction=kelly_fraction,
                edge=side_edge,
            )
            if amount_usd < 1:
                continue
            reasons = [
                "Existing position moved enough to justify a fresh look.",
                f"Entry: {entry_probability * 100:.1f}%.",
                f"Current market: {float(packet['yes_price']) * 100:.1f}%.",
                f"Finn fair: {float(packet['fair_yes_probability']) * 100:.1f}%.",
                f"Edge: {side_edge * 100:.1f} pts.",
                f"EV/contract: ${ev_per_contract:.03f}.",
                f"Kelly cap: {kelly_fraction * 100:.1f}%.",
                f"Move since entry: {market_move * 100:+.1f} pts.",
            ]
            watch = watch_map.get(str(packet["event_ticker"]))
            if watch and watch.last_event_status:
                reasons.append(f"Status: {watch.last_event_status}.")
            if watch and watch.last_score_summary:
                reasons.append(f"Score: {watch.last_score_summary}.")
            ideas.append(
                EventIdea(
                    ticker=str(packet["event_ticker"]),
                    title=f"Follow-up: {self._clean_event_title(str(packet['event_title']), None)}",
                    side="YES",
                    amount_usd=round(amount_usd, 2),
                    reason=" ".join(reasons),
                    risk="medium" if side_edge >= 0.06 else "high",
                    pick_label=str(packet["yes_label"]),
                    bet_description=f"Add to {packet['yes_label']}",
                    implied_probability=float(packet["yes_price"]),
                    source_status="robinhood-monitor",
                    priority_score=max(0.0, side_edge * 100.0 + abs(market_move) * 20.0 + ev_per_contract * 40.0),
                    confidence="high" if side_edge >= 0.08 else "medium",
                    event_day=str(packet.get("event_day")),
                    is_same_day=True,
                    category=str(packet.get("event_type") or packet.get("category") or ""),
                    time_label=position.time_label or self._format_event_time_label(event_time),
                    is_relevant_now=True,
                    event_starts_at=event_time,
                    execution_market_ticker=str(packet.get("contract_symbol") or packet.get("market_ticker") or ""),
                    execution_event_ticker=str(packet.get("event_ticker") or ""),
                    execution_contract_id=str(position.broker_contract_id or ""),
                    contract_path="Main market",
                )
            )

        ideas.sort(key=lambda item: item.priority_score, reverse=True)
        return ideas[:limit]

    def _is_uuidish(self, value: str | None) -> bool:
        return bool(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{27}", str(value or "").strip()))

    def _blocked_new_pick_categories(self) -> set[str]:
        raw = str(getattr(self.settings, "event_new_pick_blocked_categories", "") or "")
        return {item.strip().lower() for item in raw.split(",") if item.strip()}

    def _event_metadata_quality_ok(self, packet: dict[str, Any]) -> bool:
        title = self._clean_event_title(str(packet.get("event_title") or ""), packet.get("sub_title"))
        pick = str(packet.get("yes_label") or packet.get("sub_title") or "")
        if self._is_uuidish(title) or self._is_uuidish(pick):
            return False
        if title.strip().lower() in {"event", "time tbd", ""}:
            return False
        if str(packet.get("contract_path") or "").lower().startswith("set "):
            return False
        return True

    def _decode_robinhood_event_symbol_for_display(self, symbol: str) -> dict[str, str]:
        symbol = str(symbol or "").strip()
        if not symbol:
            return {}
        parts = symbol.split("-")
        if len(parts) < 3:
            return {}
        series = parts[0]
        pick_code = parts[-1]
        teams_blob = re.sub(r"^\d{2}[A-Z]{3}\d{2}\d{0,4}", "", parts[1])
        labels = EVENT_CODE_LABELS
        known_codes = sorted(labels, key=len, reverse=True)
        first = second = None
        for code in known_codes:
            if not teams_blob.startswith(code):
                continue
            remainder = teams_blob[len(code):]
            if remainder in labels:
                first = labels[code]
                second = labels[remainder]
                break
        title = f"{first} vs {second}" if first and second else ""
        pick = labels.get(pick_code, pick_code)
        category = {
            "KXMLBGAME": "Baseball",
            "KXNBAGAME": "Basketball",
            "KXWNBAGAME": "Basketball",
            "KXNHLGAME": "Hockey",
            "KXMLSGAME": "Soccer",
        }.get(series, "Event")
        return {key: value for key, value in {"event_title": title, "pick_label": pick, "category": category}.items() if value}

    async def top_event_execution_ideas(
        self,
        session: Session,
        limit: int | None = None,
        *,
        include_live: bool = True,
    ) -> list[EventIdea]:
        batch_limit = limit or self.settings.gx_batch_limit
        ideas = await self.top_event_ideas(
            session,
            limit=max(batch_limit * 4, self.settings.manual_event_report_limit),
            force_recommendations=False,
        )
        if not ideas:
            return []

        suppressed_event_codes = self._suppressed_event_codes(session)
        recommendations = {rec.id: rec for rec in session.exec(select(Recommendation)).all()}
        batches = {batch.id: batch for batch in session.exec(select(RecommendationBatch)).all()}
        open_auto_positions = []
        for row in session.exec(select(EventPosition)).all():
            if row.source not in {"robinhood", "kalshi"}:
                continue
            if row.status not in {"broker_open", "broker_order_open", "manual_confirmed", "assumed_open"}:
                continue
            rec = recommendations.get(row.source_recommendation_id or "")
            batch = batches.get(rec.batch_id) if rec else None
            if batch is None or batch.batch_type not in {"gx", "gx-live"}:
                continue
            open_auto_positions.append(row)
        if len(open_auto_positions) >= self.settings.gx_max_open_positions:
            return []

        filtered: list[EventIdea] = []
        for idea in ideas:
            if idea.side.upper() != "YES":
                continue
            if ":" in idea.title:
                continue
            if not idea.execution_market_ticker or not idea.execution_market_ticker.startswith("KX"):
                continue
            if idea.execution_event_ticker in suppressed_event_codes or idea.ticker in suppressed_event_codes:
                continue
            if idea.event_starts_at is not None:
                seconds_to_start = (idea.event_starts_at - datetime.now(self.local_tz)).total_seconds()
                if not include_live and seconds_to_start <= self.settings.gx_event_cutoff_minutes * 60:
                    continue
                if include_live and seconds_to_start <= -4 * 3600:
                    continue
            elif idea.time_label and idea.time_label != "Time TBD":
                try:
                    parsed = datetime.strptime(idea.time_label, "%I:%M %p")
                    starts_at = datetime.now(self.local_tz).replace(
                        hour=parsed.hour,
                        minute=parsed.minute,
                        second=0,
                        microsecond=0,
                    )
                    seconds_to_start = (starts_at - datetime.now(self.local_tz)).total_seconds()
                    if not include_live and seconds_to_start <= self.settings.gx_event_cutoff_minutes * 60:
                        continue
                    if include_live and seconds_to_start <= -4 * 3600:
                        continue
                except ValueError:
                    pass
            stake = min(float(idea.amount_usd), self.settings.gx_max_stake_usd)
            stake = max(2.0, round(stake, 2))
            required = self._minimum_stake_for_target_profit(
                float(idea.implied_probability or 0.0),
                self.settings.gx_min_profit_usd,
            )
            if required > self.settings.gx_max_stake_usd:
                continue
            stake = max(stake, required)
            if stake > self.settings.gx_max_stake_usd:
                continue
            max_return = self._event_max_return(stake, float(idea.implied_probability or 0.0))
            potential_profit = self._event_potential_profit(stake, float(idea.implied_probability or 0.0))
            if potential_profit < self.settings.gx_min_profit_usd:
                continue
            cloned = EventIdea(**{**idea.__dict__})
            cloned.amount_usd = round(stake, 2)
            cloned.max_return_usd = max_return
            cloned.potential_profit_usd = potential_profit
            filtered.append(cloned)

        filtered.sort(
            key=lambda item: (
                1 if item.is_same_day else 0,
                1 if item.is_relevant_now else 0,
                item.priority_score,
            ),
            reverse=True,
        )
        return filtered[:batch_limit]

    def _suppressed_event_codes(self, session: Session) -> set[str]:
        stmt = select(EventPosition).where(
            EventPosition.source.in_(("robinhood", "kalshi")),
            EventPosition.status.in_(
                (
                    "manual_confirmed",
                    "assumed_open",
                    "broker_open",
                    "broker_order_open",
                    "broker_filled",
                )
            )
        )
        positions = session.exec(stmt).all()
        suppressed: set[str] = set()
        for position in positions:
            if position.event_code:
                suppressed.add(position.event_code)
            if position.broker_event_id:
                suppressed.add(position.broker_event_id)
            if position.broker_contract_id:
                suppressed.add(position.broker_contract_id)
        return suppressed

    def _event_category_from_recommendation_symbol(self, symbol: str | None) -> str | None:
        if not symbol or " || " not in symbol:
            return None
        parts = symbol.split(" || ")
        if len(parts) >= 3 and parts[2]:
            return parts[2]
        return None

    def _probability_bucket(self, probability: float) -> str:
        if probability < 0.55:
            return "under_55"
        if probability < 0.65:
            return "55_64"
        if probability < 0.75:
            return "65_74"
        return "75_plus"

    def _event_learning_adjustment(
        self,
        session: Session,
        *,
        category: str,
        probability: float,
    ) -> tuple[float, float, list[str]]:
        outcomes = list(session.exec(select(EventBetOutcome)).all())
        if not outcomes:
            return 0.0, 1.0, []

        positions = {position.id: position for position in session.exec(select(EventPosition)).all()}
        recommendations = {
            recommendation.id: recommendation for recommendation in session.exec(select(Recommendation)).all()
        }

        settled_rows = [row for row in outcomes if row.outcome in {"won", "lost"}]
        if not settled_rows:
            return 0.0, 1.0, []

        target_bucket = self._probability_bucket(probability)
        category_rows: list[EventBetOutcome] = []
        bucket_rows: list[EventBetOutcome] = []
        for row in settled_rows:
            position = positions.get(row.event_position_id)
            recommendation = recommendations.get(position.source_recommendation_id) if position and position.source_recommendation_id else None
            rec_category = self._event_category_from_recommendation_symbol(recommendation.symbol) if recommendation else None
            if rec_category == category:
                category_rows.append(row)
            if row.implied_probability is not None and self._probability_bucket(float(row.implied_probability)) == target_bucket:
                bucket_rows.append(row)

        reasons: list[str] = []
        priority_adj = 0.0
        stake_mult = 1.0

        def apply(rows: list[EventBetOutcome], label: str) -> None:
            nonlocal priority_adj, stake_mult
            if len(rows) < 3:
                return
            wins = sum(1 for row in rows if row.outcome == "won")
            hit_rate = wins / len(rows)
            if hit_rate >= 0.60:
                priority_adj += 0.35
                stake_mult *= 1.12
                reasons.append(f"historical {label} hit rate {hit_rate * 100:.0f}%")
            elif hit_rate <= 0.40:
                priority_adj -= 0.35
                stake_mult *= 0.82
                reasons.append(f"historical {label} hit rate only {hit_rate * 100:.0f}%")

        apply(category_rows, f"{category.lower()} bets")
        apply(bucket_rows, f"{target_bucket.replace('_', '-')} odds bucket")
        recent_cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2)
        recent_rows = [
            row
            for row in settled_rows
            if (row.settled_at or row.checked_at) >= recent_cutoff
        ]
        if len(recent_rows) >= 5:
            wins = sum(1 for row in recent_rows if row.outcome == "won")
            hit_rate = wins / len(recent_rows)
            pnl = sum(float(row.profit_usd or row.realized_pnl_without_fees_usd or 0.0) for row in recent_rows)
            if hit_rate <= 0.42 or pnl < 0:
                priority_adj -= 0.75
                stake_mult *= 0.55
                reasons.append(f"recent global event performance weak: {wins}/{len(recent_rows)} wins, ${pnl:.2f} P/L")
            elif hit_rate >= 0.62 and pnl > 0:
                priority_adj += 0.25
                stake_mult *= 1.05
                reasons.append(f"recent global event performance constructive: {wins}/{len(recent_rows)} wins, ${pnl:.2f} P/L")
        return priority_adj, max(0.45, min(1.25, stake_mult)), reasons[:3]

    def _recent_event_loss_guard_active(self, session: Session) -> tuple[bool, float, int]:
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=self.settings.event_loss_guard_recent_days)
        rows = [
            row
            for row in session.exec(select(EventBetOutcome)).all()
            if row.outcome in {"won", "lost"} and (row.settled_at or row.checked_at) >= cutoff
        ]
        pnl = sum(float(row.profit_usd or row.realized_pnl_without_fees_usd or 0.0) for row in rows)
        return pnl <= self.settings.event_loss_guard_min_pnl_usd and len(rows) >= 5, pnl, len(rows)

    def _manual_event_required_edge(self, session: Session, packet: dict[str, Any], decision_confidence: str) -> float:
        required = self.settings.event_manual_min_quant_edge
        if decision_confidence == "high":
            required = min(required, self.settings.event_min_quant_edge)
        category = str(packet.get("event_type") or packet.get("category") or "")
        if category in {"Baseball", "Soccer", "Tennis"}:
            required = max(required, 0.045)
        if category.lower() in self._blocked_new_pick_categories():
            required = max(required, 0.10)
        guard_active, _pnl, _count = self._recent_event_loss_guard_active(session)
        if guard_active:
            required += 0.025
        return required

    def _cap_event_stake_for_regime(
        self,
        session: Session,
        amount_usd: float,
        packet: dict[str, Any],
        *,
        edge: float,
        confidence: str,
    ) -> tuple[float, list[str]]:
        notes: list[str] = []
        cap = self.settings.event_max_single_stake_usd
        guard_active, pnl, count = self._recent_event_loss_guard_active(session)
        if guard_active:
            cap = min(cap, self.settings.event_loss_guard_max_single_stake_usd)
            notes.append(f"Sizing guard: recent {count}-bet P/L is ${pnl:.2f}, so Finn capped new risk.")
        category = str(packet.get("event_type") or packet.get("category") or "")
        if category.lower() in self._blocked_new_pick_categories():
            cap = min(cap, 2.0)
            notes.append(f"Sizing guard: {category} is blocked for normal new picks after poor history.")
        if edge < self.settings.event_manual_high_confidence_edge or confidence != "high":
            cap = min(cap, 8.0)
            notes.append("Sizing guard: edge/confidence is not strong enough for a larger manual stake.")
        return round(max(0.0, min(amount_usd, cap)), 2), notes[:2]

    def _event_market_friction(
        self,
        *,
        market_probability: float,
        liquidity_dollars: float,
        spread_dollars: float,
    ) -> float:
        if market_probability <= 0 or market_probability >= 1:
            return 0.03
        floor = max(0.001, self.settings.event_fee_per_contract_usd)
        half_spread = max(0.0, spread_dollars) / 2.0
        if liquidity_dollars <= 1000:
            liquidity_slippage = 0.01
        elif liquidity_dollars <= 5000:
            liquidity_slippage = 0.007
        elif liquidity_dollars <= 20000:
            liquidity_slippage = 0.005
        elif liquidity_dollars <= 100000:
            liquidity_slippage = 0.0035
        else:
            liquidity_slippage = 0.002
        probability_floor = 0.002 if 0.15 <= market_probability <= 0.85 else 0.003
        return round(max(floor, half_spread, liquidity_slippage, probability_floor), 4)

    def _event_expected_value_metrics(
        self,
        *,
        fair_probability: float,
        market_probability: float,
        liquidity_dollars: float,
        spread_dollars: float,
    ) -> dict[str, float]:
        p = max(0.01, min(0.99, market_probability))
        q = max(0.01, min(0.99, fair_probability))
        friction = self._event_market_friction(
            market_probability=p,
            liquidity_dollars=liquidity_dollars,
            spread_dollars=spread_dollars,
        )
        edge = q - p - friction
        win_profit_per_contract = max(0.0, 1.0 - p - friction)
        lose_cost_per_contract = p + friction
        ev_per_contract = q * win_profit_per_contract - (1.0 - q) * lose_cost_per_contract
        raw_kelly = 0.0
        if win_profit_per_contract > 0 and lose_cost_per_contract > 0:
            raw_kelly = ((q * win_profit_per_contract) - ((1.0 - q) * lose_cost_per_contract)) / (
                win_profit_per_contract * lose_cost_per_contract
            )
        raw_kelly = max(0.0, raw_kelly)
        liquidity_penalty = 1.0
        if liquidity_dollars <= 2000:
            liquidity_penalty = 0.55
        elif liquidity_dollars <= 10000:
            liquidity_penalty = 0.72
        elif liquidity_dollars <= 50000:
            liquidity_penalty = 0.86
        spread_penalty = 1.0
        if spread_dollars >= 0.08:
            spread_penalty = 0.55
        elif spread_dollars >= 0.05:
            spread_penalty = 0.72
        elif spread_dollars >= 0.03:
            spread_penalty = 0.86
        adjusted_kelly = min(
            self.settings.event_kelly_fraction_cap,
            raw_kelly * liquidity_penalty * spread_penalty * self.settings.event_kelly_fraction_scale,
        )
        quant_score = edge * 100.0 + ev_per_contract * 50.0 + adjusted_kelly * 20.0
        return {
            "edge": round(edge, 4),
            "ev_per_contract": round(ev_per_contract, 4),
            "friction": round(friction, 4),
            "raw_kelly": round(raw_kelly, 4),
            "adjusted_kelly": round(adjusted_kelly, 4),
            "quant_score": round(quant_score, 4),
        }

    def _estimate_yes_fair_probability(
        self,
        packet: dict[str, Any],
        *,
        learning_priority_adj: float = 0.0,
        position_entry_probability: float | None = None,
    ) -> float:
        market_yes = float(packet.get("yes_price") or 0.0)
        if market_yes <= 0:
            return 0.0
        spread = float(packet.get("spread_dollars") or 0.0)
        liquidity = float(packet.get("liquidity_dollars") or 0.0)
        is_same_day = bool(packet.get("is_same_day"))
        is_relevant_now = bool(packet.get("is_relevant_now"))
        explicit_pick_only = bool(packet.get("explicit_pick_only"))

        bias = 0.0
        if explicit_pick_only:
            if 0.55 <= market_yes <= 0.78:
                bias += 0.012
            elif market_yes >= 0.78:
                bias += 0.006
            elif market_yes <= 0.42:
                bias -= 0.012
        if is_same_day:
            bias += 0.004
        if is_relevant_now:
            bias += 0.003
        if spread >= 0.06:
            bias -= 0.012
        elif spread <= 0.025:
            bias += 0.004
        if liquidity >= 250000:
            bias += 0.005
        elif liquidity >= 50000:
            bias += 0.003
        elif liquidity <= 5000:
            bias -= 0.01
        bias += learning_priority_adj * 0.01

        if position_entry_probability is not None and position_entry_probability > 0:
            move = market_yes - position_entry_probability
            if abs(move) >= self.settings.event_probability_move_alert_threshold:
                bias += 0.01 if move > 0 else -0.01

        return round(max(0.02, min(0.98, market_yes + bias)), 4)

    def _enrich_event_packet_with_pricing(
        self,
        session: Session,
        packet: dict[str, Any],
        *,
        position_entry_probability: float | None = None,
    ) -> dict[str, Any]:
        packet = dict(packet)
        learning_priority_adj, learning_stake_mult, learning_reasons = self._event_learning_adjustment(
            session,
            category=str(packet.get("event_type") or packet.get("category") or "Event"),
            probability=float(packet.get("yes_price") or 0.0),
        )
        fair_yes = self._estimate_yes_fair_probability(
            packet,
            learning_priority_adj=learning_priority_adj,
            position_entry_probability=position_entry_probability,
        )
        market_yes = float(packet.get("yes_entry_price") or packet.get("yes_price") or 0.0)
        market_no = float(packet.get("no_entry_price") or packet.get("no_price") or max(0.0, 1.0 - market_yes))
        fair_no = round(max(0.02, min(0.98, 1.0 - fair_yes)), 4)
        yes_metrics = self._event_expected_value_metrics(
            fair_probability=fair_yes,
            market_probability=market_yes,
            liquidity_dollars=float(packet.get("liquidity_dollars") or 0.0),
            spread_dollars=float(packet.get("spread_dollars") or 0.0),
        )
        no_metrics = self._event_expected_value_metrics(
            fair_probability=fair_no,
            market_probability=market_no,
            liquidity_dollars=float(packet.get("liquidity_dollars") or 0.0),
            spread_dollars=float(packet.get("spread_dollars") or 0.0),
        )
        packet["market_yes_probability"] = round(market_yes, 4)
        packet["market_no_probability"] = round(market_no, 4)
        packet["fair_yes_probability"] = fair_yes
        packet["fair_no_probability"] = fair_no
        packet["edge_yes"] = yes_metrics["edge"]
        packet["edge_no"] = no_metrics["edge"]
        packet["ev_yes_per_contract"] = yes_metrics["ev_per_contract"]
        packet["ev_no_per_contract"] = no_metrics["ev_per_contract"]
        packet["friction_yes"] = yes_metrics["friction"]
        packet["friction_no"] = no_metrics["friction"]
        packet["kelly_yes"] = yes_metrics["adjusted_kelly"]
        packet["kelly_no"] = no_metrics["adjusted_kelly"]
        packet["quant_score_yes"] = yes_metrics["quant_score"]
        packet["quant_score_no"] = no_metrics["quant_score"]
        packet["learning_priority_adj"] = learning_priority_adj
        packet["learning_stake_mult"] = learning_stake_mult
        packet["learning_reasons"] = learning_reasons
        return packet

    async def today_event_slate(self, limit: int = 12) -> list[EventSlateItem]:
        packets = await self._discover_robinhood_event_packets(limit=max(limit * 3, 40), same_day_only=True)
        items: list[EventSlateItem] = []
        seen: set[str] = set()
        for packet in packets:
            if not packet.get("is_same_day") or not packet.get("is_relevant_now"):
                continue
            ticker = str(packet["event_ticker"])
            if ticker in seen:
                continue
            seen.add(ticker)
            time_value = packet.get("event_time")
            items.append(
                EventSlateItem(
                    ticker=ticker,
                    title=self._clean_event_title(packet["event_title"], packet.get("sub_title")),
                    event_day=str(packet["event_day"]),
                    time_label=self._format_event_time_label(time_value),
                    sort_key=self._event_sort_tuple(time_value, ticker),
                    options=self._robinhood_event_options_summary(packet),
                    is_same_day=True,
                    category=str(packet.get("event_type") or packet.get("category") or ""),
                )
            )
        items.sort(key=lambda item: item.sort_key)
        if items:
            return items[:limit]

        event_payloads = await self._discover_today_event_payloads()
        fallback_items: list[EventSlateItem] = []
        seen.clear()
        for event in event_payloads:
            packet = self._build_event_packet(event)
            if packet is None or not packet.get("is_same_day") or not packet.get("is_relevant_now"):
                continue
            ticker = str(packet["event_ticker"])
            if ticker in seen:
                continue
            seen.add(ticker)
            time_value = self._market_event_datetime(packet)
            fallback_items.append(
                EventSlateItem(
                    ticker=ticker,
                    title=self._clean_event_title(packet["event_title"], packet.get("sub_title")),
                    event_day=str(packet["event_day"]),
                    time_label=self._format_event_time_label(time_value),
                    sort_key=self._event_sort_tuple(time_value, ticker),
                    options=self._event_options_summary(event),
                    is_same_day=True,
                    category=str(packet.get("event_type") or packet.get("category") or ""),
                )
            )
        fallback_items.sort(key=lambda item: item.sort_key)
        return fallback_items[:limit]

    def _size_event_decision(
        self,
        stake_intent: str,
        buying_power: float,
        bankroll: float,
        *,
        stake_multiplier: float = 1.0,
        kelly_fraction: float = 0.0,
        edge: float = 0.0,
    ) -> float:
        if buying_power <= 0:
            return 0.0
        stake_by_intent = {
            "tiny": 0.2,
            "small": 0.35,
            "medium": 0.55,
            "none": 0.0,
        }
        max_daily = max(10.0, buying_power * self.settings.max_daily_prediction_risk_pct)
        heuristic_amount = max_daily * stake_by_intent.get(stake_intent, 0.0) * stake_multiplier
        kelly_amount = bankroll * max(0.0, min(self.settings.event_kelly_fraction_cap, kelly_fraction))
        edge_floor = 0.0
        if edge > 0:
            edge_floor = max_daily * min(0.75, edge * 8.0)
        amount = min(bankroll, max(heuristic_amount, kelly_amount, edge_floor))
        if amount <= 0:
            return 0.0
        return max(2.0, amount)

    def _event_contract_count(self, amount_usd: float, implied_probability: float) -> int:
        if amount_usd <= 0 or implied_probability <= 0 or implied_probability >= 1:
            return 0
        return max(0, floor(amount_usd / implied_probability))

    def _event_max_return(self, amount_usd: float, implied_probability: float) -> float:
        if amount_usd <= 0 or implied_probability <= 0 or implied_probability >= 1:
            return 0.0
        return round(float(self._event_contract_count(amount_usd, implied_probability)), 2)

    def _event_potential_profit(self, amount_usd: float, implied_probability: float) -> float:
        max_return = self._event_max_return(amount_usd, implied_probability)
        return round(max_return - amount_usd, 2)

    def _minimum_stake_for_target_profit(self, implied_probability: float, target_profit_usd: float) -> float:
        if implied_probability <= 0 or implied_probability >= 1 or target_profit_usd <= 0:
            return 0.0
        required_contracts = max(1, int((target_profit_usd / max(0.01, 1.0 - implied_probability)) + 0.999999))
        required = required_contracts * implied_probability
        return round(required, 2)

    def _event_sort_tuple(self, value: datetime | None, fallback: str) -> tuple[int, str, str]:
        if value is None:
            return (1, "99:99", fallback)
        return (0, value.strftime("%H:%M"), fallback)

    def _is_event_relevant_now(self, value: datetime | None) -> bool:
        if value is None:
            return False
        now = datetime.now(self.local_tz)
        delta_hours = (value - now).total_seconds() / 3600.0
        return -4.0 <= delta_hours <= 8.0

    def _format_event_time_label(self, value: datetime | None) -> str:
        if value is None:
            return "Time TBD"
        return value.astimezone(self.local_tz).strftime("%-I:%M %p")

    def _event_options_summary(self, event: dict[str, Any]) -> str:
        markets = [
            market
            for market in (event.get("markets") or [])
            if market.get("market_type") == "binary" and market.get("status") == "active"
        ]
        labels: list[str] = []
        for market in markets:
            label = str(market.get("yes_sub_title") or "").strip()
            if not label:
                continue
            if label.lower() in {"tie", "draw"}:
                continue
            if label not in labels:
                labels.append(label)
        if not labels:
            return "Market open"
        return " / ".join(labels[:3])

    def _event_reasons(
        self,
        event: dict[str, Any],
        market: dict[str, Any],
        yes_price: float,
        no_price: float,
        liquidity_dollars: float,
    ) -> list[str]:
        reasons = [
            f"broker-available {event.get('category', 'event').lower()} market",
            f"yes {yes_price:.2f} / no {no_price:.2f}",
            f"liquidity ${liquidity_dollars:.0f}",
        ]
        if market.get("close_time"):
            reasons.append(f"settles by {market['close_time']}")
        return reasons

    def _clean_event_title(self, title: str, sub_title: str | None) -> str:
        if sub_title and sub_title.lower() not in title.lower():
            return f"{title} ({sub_title})"
        return title

    def _is_primary_event_market(self, packet: dict[str, Any]) -> bool:
        title = str(packet.get("event_title") or "")
        event_type = str(packet.get("event_type") or packet.get("category") or "")
        if event_type not in {"Basketball", "Baseball", "Soccer", "Cricket", "Tennis", "Fight", "Sports"}:
            return False
        return ":" not in title

    def _event_bet_description(self, packet: dict[str, Any], action: str) -> str:
        if action == "buy_yes":
            return f"Bet on {packet['yes_label']}"
        if packet.get("explicit_pick_only"):
            return f"Skip rather than fading {packet['yes_label']}"
        return f"Bet against {packet['yes_label']}"

    def _event_preference_bonus(self, packet: dict[str, Any]) -> float:
        ticker = str(packet.get("event_ticker") or "")
        title = str(packet.get("event_title") or "").lower()
        series_ticker = str(packet.get("series_ticker") or "")
        event_type = str(packet.get("event_type") or packet.get("category") or "")
        bonus = 0.0
        if event_type == "Basketball":
            bonus += 4.0
        elif event_type == "Baseball":
            bonus += 3.5
        elif event_type == "Soccer":
            bonus += 3.0
        elif event_type == "Cricket":
            bonus += 2.5
        elif event_type == "Tennis":
            bonus += 1.8
        if series_ticker == "KXNBAGAME" or ticker.startswith("KXNBAGAME"):
            bonus += 4.0
        elif series_ticker == "KXMLBGAME" or ticker.startswith("KXMLBGAME"):
            bonus += 3.5
        elif series_ticker == "KXMLSGAME":
            bonus += 3.2
        elif series_ticker == "KXIPLGAME" or ticker.startswith("KXIPLGAME"):
            bonus += 2.6
        elif series_ticker == "KXWNBAGAME" or ticker.startswith("KXWNBAGAME"):
            bonus += 2.4
        elif ticker.startswith("KXNFL"):
            bonus += 2.4
        elif series_ticker == "KXNHLGAME" or ticker.startswith("KXNHL"):
            bonus += 2.0
        elif "basketball" in title:
            bonus += 1.2
        elif "baseball" in title:
            bonus += 1.0
        elif "tennis" in title:
            bonus -= 0.4
        elif "esports" in title:
            bonus -= 0.6
        if "game " in title or title.startswith("game "):
            bonus += 0.5
        return bonus

    def _event_type_label(self, packet: dict[str, Any]) -> str:
        ticker = str(packet.get("event_ticker") or "")
        title = str(packet.get("event_title") or "").lower()
        series_ticker = str(packet.get("series_ticker") or "")
        if series_ticker in {"KXNBAGAME", "KXWNBAGAME", "KXBBLGAME", "KXACBGAME"} or ticker.startswith("KXNBAGAME") or "basketball" in title:
            return "Basketball"
        if series_ticker in {"KXMLBGAME", "KXNCAABBGAME"} or ticker.startswith("KXMLBGAME") or "baseball" in title:
            return "Baseball"
        if series_ticker in {"KXIPLGAME", "KXWPLGAME", "KXPSLGAME"} or ticker.startswith("KXIPLGAME") or "cricket" in title:
            return "Cricket"
        if series_ticker in {"KXWTA", "KXATP", "KXITFWMATCH", "KXITFMMATCH", "KXTENNISEXHIBITION"} or "tennis" in title:
            return "Tennis"
        if series_ticker in {"KXMLSGAME", "KXEPLGAME", "KXUEFAGAME", "KXUECLGAME", "KXUECLGAME", "KXSCOTTISHPREMGAME"} or "fc " in title or " united" in title or "city" in title:
            return "Soccer"
        if "fight" in title or "ufc" in title or "boxing" in title:
            return "Fight"
        if "esports" in title or "faze" in title or "optic" in title:
            return "Esports"
        return str(packet.get("category") or "Event")

    def _normalize_robinhood_category(self, category: str, title: str = "") -> str:
        raw = category.strip()
        lowered = raw.lower()
        title_l = title.lower()
        if any(token in lowered for token in ["basketball", "nba", "wnba"]) or "basketball" in title_l:
            return "Basketball"
        if "baseball" in lowered or "baseball" in title_l:
            return "Baseball"
        if "soccer" in lowered or "mls" in lowered or "premier league" in lowered or "fc " in title_l:
            return "Soccer"
        if "cricket" in lowered or "cricket" in title_l:
            return "Cricket"
        if "tennis" in lowered or "tennis" in title_l:
            return "Tennis"
        if any(token in lowered for token in ["fight", "boxing", "mma", "ufc"]) or any(token in title_l for token in ["fight", "boxing", "mma", "ufc"]):
            return "Fight"
        if "sports" in lowered:
            return "Sports"
        if "politic" in lowered:
            return "Politics"
        if "entertainment" in lowered:
            return "Entertainment"
        return raw or "Event"

    def _robinhood_score_summary(self, event_state: dict[str, Any]) -> str | None:
        gdp_title = str(event_state.get("gdpTitle") or "").strip()
        if gdp_title:
            return gdp_title
        score_map = event_state.get("contractIdToEventScores") or {}
        if isinstance(score_map, dict) and score_map:
            bits: list[str] = []
            for value in score_map.values():
                if isinstance(value, dict):
                    home = value.get("home") or value.get("homeScore")
                    away = value.get("away") or value.get("awayScore")
                    if home is not None or away is not None:
                        bits.append(f"{home}-{away}")
                elif value not in (None, "", {}):
                    bits.append(str(value))
                if len(bits) >= 2:
                    break
            if bits:
                return " / ".join(bits)
        progress = str(event_state.get("eventProgress") or "").strip()
        if progress:
            return progress.replace("EVENT_PROGRESS_", "").replace("_", " ").title()
        return None

    def _is_noisy_event_title(self, title: str) -> bool:
        lowered = title.lower()
        noisy_patterns = [
            "visit mars",
            "before 2099",
            "who will be",
            "which party",
        ]
        return any(pattern in lowered for pattern in noisy_patterns)

    async def _discover_robinhood_event_packets(
        self,
        *,
        limit: int = 32,
        same_day_only: bool = False,
    ) -> list[dict[str, Any]]:
        if not self.robinhood_prediction.configured():
            return []
        snapshot = await self.robinhood_prediction.get_catalog_snapshot(
            max_age_seconds=self.settings.robinhood_prediction_catalog_cache_seconds
        )
        packets: list[dict[str, Any]] = []
        for event in snapshot.events:
            packet = self._build_robinhood_event_packet(snapshot, event)
            if packet is None:
                continue
            if same_day_only and not packet.get("is_same_day"):
                continue
            packets.append(packet)
        packets.sort(
            key=lambda item: (
                1 if item.get("is_same_day") else 0,
                1 if item.get("is_relevant_now") else 0,
                float(item.get("priority_hint") or 0.0),
            ),
            reverse=True,
        )
        return packets[:limit]

    def _build_robinhood_event_packet(
        self,
        snapshot: RobinhoodCatalogSnapshot,
        event: dict[str, Any],
    ) -> dict[str, Any] | None:
        event_id = str(event.get("id") or "")
        if not event_id:
            return None
        event_state = snapshot.event_states.get(event_id) or {}
        title = str(event.get("name") or event.get("description") or "").strip()
        category = self._normalize_robinhood_category(
            str(event_state.get("category") or event.get("category") or "").strip(),
            title,
        )
        if category not in {"Basketball", "Baseball", "Soccer", "Cricket", "Tennis", "Sports", "Politics", "Entertainment"}:
            return None
        if not title or self._is_noisy_event_title(title):
            return None
        contracts = [
            contract
            for contract in (event.get("eventContracts") or {}).values()
            if isinstance(contract, dict)
            and str(contract.get("tradability") or "").endswith("TRADABLE")
            and contract.get("discoverable", True)
        ]
        if not contracts:
            return None

        event_time = self._robinhood_event_time(event)
        event_day = event_time.date() if event_time else None
        is_same_day = bool(event_day and event_day == self._local_today())
        is_relevant_now = self._is_event_relevant_now(event_time)
        if not is_same_day and not is_relevant_now:
            return None

        selected_contract = self._select_robinhood_contract(snapshot, contracts)
        if selected_contract is None:
            return None
        contract_id = str(selected_contract.get("id") or "")
        quote = snapshot.quotes.get(contract_id) or {}
        yes_price = self._mid_price(
            quote.get("yes_bid_price"),
            quote.get("yes_ask_price"),
            quote.get("last_trade_price"),
        )
        yes_entry_price = float(quote.get("yes_ask_price") or yes_price or 0.0)
        if yes_price <= 0:
            return None
        no_price = self._mid_price(
            quote.get("no_bid_price"),
            quote.get("no_ask_price"),
            None,
        )
        if no_price <= 0:
            no_price = max(0.0, min(1.0, 1.0 - yes_price))
        no_entry_price = float(quote.get("no_ask_price") or no_price or 0.0)
        fundamentals = snapshot.fundamentals.get(contract_id) or {}
        open_interest = float(fundamentals.get("open_interest") or 0.0)
        volume = float(fundamentals.get("volume") or 0.0)
        liquidity_dollars = open_interest + volume
        pick_label = (
            str(selected_contract.get("displayLongName") or "")
            or str(selected_contract.get("name") or "")
            or str(selected_contract.get("displayShortName") or "")
            or "Yes"
        )
        reasons = [
            f"yes {yes_price * 100:.1f}% / no {no_price * 100:.1f}%",
            f"open interest {open_interest:.0f}",
        ]
        event_status = str(event_state.get("eventStatus") or "")
        if event_status:
            reasons.append(event_status.replace("EVENT_STATUS_", "").lower())
        external_source_ts_raw = str(event_state.get("externalSourceTs") or "").strip()
        external_source_ts = None
        if external_source_ts_raw:
            try:
                external_source_ts = datetime.fromisoformat(external_source_ts_raw.replace("Z", "+00:00")).astimezone(self.local_tz)
            except ValueError:
                external_source_ts = None
        return {
            "event_ticker": event_id,
            "kalshi_event_ticker": str(selected_contract.get("symbol") or "").rsplit("-", 1)[0],
            "series_ticker": category.upper(),
            "event_title": title,
            "category": category,
            "event_type": category,
            "sub_title": str(event_state.get("subtitle") or event.get("description") or "").strip(),
            "ticker": contract_id,
            "market_ticker": contract_id,
            "contract_id": contract_id,
            "contract_symbol": str(selected_contract.get("symbol") or ""),
            "contract_path": self._robinhood_contract_path(title),
            "yes_label": pick_label,
            "no_label": f"Not {pick_label}",
            "yes_price": round(yes_price, 4),
            "no_price": round(no_price, 4),
            "yes_entry_price": round(yes_entry_price, 4),
            "no_entry_price": round(no_entry_price, 4),
            "liquidity_dollars": round(liquidity_dollars, 2),
            "spread_dollars": round(
                max(
                    0.0,
                    float(quote.get("yes_ask_price") or 0.0) - float(quote.get("yes_bid_price") or 0.0),
                ),
                4,
            ),
            "open_interest": open_interest,
            "close_time": selected_contract.get("expirationDate"),
            "analysis_reasons": reasons,
            "is_same_day": is_same_day,
            "event_day": event_day.isoformat() if event_day else None,
            "explicit_pick_only": True,
            "is_relevant_now": is_relevant_now,
            "event_time": event_time,
            "event_status": event_status or None,
            "score_summary": self._robinhood_score_summary(event_state),
            "supports_live_data": bool(event_state.get("supportsLiveData")),
            "external_source_ts": external_source_ts,
            "contracts": contracts,
            "priority_hint": self._robinhood_event_priority(category, title, event_time, liquidity_dollars),
            "source_status": "robinhood",
        }

    def _select_robinhood_contract(
        self,
        snapshot: RobinhoodCatalogSnapshot,
        contracts: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        ranked: list[tuple[float, dict[str, Any]]] = []
        for contract in contracts:
            contract_id = str(contract.get("id") or "")
            if not contract_id:
                continue
            quote = snapshot.quotes.get(contract_id) or {}
            yes_price = self._mid_price(
                quote.get("yes_bid_price"),
                quote.get("yes_ask_price"),
                quote.get("last_trade_price"),
            )
            if yes_price <= 0:
                continue
            fundamentals = snapshot.fundamentals.get(contract_id) or {}
            open_interest = float(fundamentals.get("open_interest") or 0.0)
            spread = max(
                0.0,
                float(quote.get("yes_ask_price") or 0.0) - float(quote.get("yes_bid_price") or 0.0),
            )
            target_price = 0.62
            price_fit = max(0.0, 1.0 - abs(yes_price - target_price) / 0.28)
            extreme_penalty = 0.7 if yes_price >= 0.9 else 0.0
            liquidity_bonus = min(2.0, log10(open_interest + 1))
            ranked.append((price_fit + liquidity_bonus - spread * 2.0 - extreme_penalty, contract))
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked[0][1]

    def _robinhood_event_time(self, event: dict[str, Any]) -> datetime | None:
        timeline = (event.get("timeline") or {}).get("entries") or []
        for entry in timeline:
            if str(entry.get("type")) == "TIMELINE_ENTRY_TYPE_EVENT_DAY" and entry.get("timestamp"):
                try:
                    return datetime.fromisoformat(str(entry["timestamp"]).replace("Z", "+00:00")).astimezone(self.local_tz)
                except ValueError:
                    continue
        for contract in (event.get("eventContracts") or {}).values():
            timestamp = contract.get("openDate")
            if not timestamp:
                continue
            try:
                return datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).astimezone(self.local_tz)
            except ValueError:
                continue
        return None

    def _robinhood_event_priority(
        self,
        category: str,
        title: str,
        event_time: datetime | None,
        liquidity_dollars: float,
    ) -> float:
        lowered = title.lower()
        bonus = 0.0
        if category == "Basketball":
            bonus += 4.0
        elif category == "Baseball":
            bonus += 3.6
        elif category == "Soccer":
            bonus += 2.9
        elif category == "Cricket":
            bonus += 2.4
        elif category == "Tennis":
            bonus += 1.8
        if "game " in lowered:
            bonus += 0.4
        if event_time is not None and self._is_event_relevant_now(event_time):
            bonus += 1.0
        return bonus + min(2.5, log10(liquidity_dollars + 1))

    def _robinhood_event_options_summary(self, packet: dict[str, Any]) -> str:
        labels: list[str] = []
        for contract in packet.get("contracts") or []:
            label = (
                str(contract.get("displayLongName") or "")
                or str(contract.get("name") or "")
                or str(contract.get("displayShortName") or "")
            ).strip()
            if not label:
                continue
            if label not in labels:
                labels.append(label)
        return " / ".join(labels[:4]) if labels else "Market open"

    def _robinhood_contract_path(self, title: str) -> str:
        title = title.strip()
        if ":" not in title:
            return "Main market"
        section = title.split(":", 1)[1].strip()
        if not section:
            return "Contracts"
        return f"Contracts > {section}"

    async def _discover_broker_event_payloads(self) -> list[dict[str, Any]]:
        series = await self._candidate_event_series()
        candidate_entries: dict[str, tuple[float, str]] = {}
        local_today = self._local_today()
        max_series = min(120, len(series))
        batch_size = 20
        for start in range(0, max_series, batch_size):
            series_batch = series[start : start + batch_size]
            market_lists = await self._fetch_series_markets([item["ticker"] for item in series_batch])
            for series_item, markets in zip(series_batch, market_lists, strict=False):
                for market in markets:
                    event_ticker = market.get("event_ticker")
                    if not event_ticker:
                        continue
                    event_day = self._market_event_date(market)
                    if event_day is None:
                        continue
                    delta_days = (event_day - local_today).days
                    if not (0 <= delta_days <= 2):
                        continue
                    score = self._candidate_event_score(series_item, market, delta_days)
                    prior = candidate_entries.get(event_ticker)
                    if prior is None or score > prior[0]:
                        candidate_entries[event_ticker] = (score, market.get("ticker") or "")
            if len(candidate_entries) >= 72:
                break
        ranked_event_tickers = [
            event_ticker
            for event_ticker, _details in sorted(
                candidate_entries.items(),
                key=lambda item: item[1][0],
                reverse=True,
            )
        ]
        event_payloads = await self._fetch_event_payloads(ranked_event_tickers[:80])
        filtered: list[dict[str, Any]] = []
        for event in event_payloads:
            if not event.get("available_on_brokers"):
                continue
            title = event.get("title") or ""
            if self._is_noisy_event_title(title):
                continue
            filtered.append(event)
        filtered.sort(key=self._event_sort_key, reverse=True)
        return filtered

    async def _discover_today_event_payloads(self) -> list[dict[str, Any]]:
        preferred_series = [
            "KXNBAGAME",
            "KXMLBGAME",
            "KXMLSGAME",
            "KXIPLGAME",
            "KXEPLGAME",
            "KXUEFAGAME",
            "KXWNBAGAME",
            "KXNHLGAME",
        ]
        local_today = self._local_today()
        event_tickers: set[str] = set()
        for series_ticker in preferred_series:
            cursor: str | None = None
            for _ in range(6):
                try:
                    payload = await self.kalshi.list_markets(
                        series_ticker=series_ticker,
                        status="open",
                        limit=200,
                        cursor=cursor,
                    )
                except Exception:
                    break
                markets = payload.get("markets", [])
                for market in markets:
                    if market.get("market_type") != "binary" or market.get("status") != "active":
                        continue
                    event_day = self._market_event_date(market)
                    if event_day != local_today:
                        continue
                    event_time = self._market_event_datetime(market)
                    if not self._is_event_relevant_now(event_time):
                        continue
                    event_ticker = market.get("event_ticker")
                    if event_ticker:
                        event_tickers.add(str(event_ticker))
                cursor = payload.get("cursor")
                if not cursor:
                    break
        event_payloads = await self._fetch_event_payloads(sorted(event_tickers))
        filtered: list[dict[str, Any]] = []
        for event in event_payloads:
            if not event.get("available_on_brokers"):
                continue
            title = event.get("title") or ""
            if self._is_noisy_event_title(title):
                continue
            filtered.append(event)
        filtered.sort(key=self._event_sort_key, reverse=True)
        return filtered

    async def _candidate_event_series(self) -> list[dict[str, Any]]:
        series: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(15):
            payload = await self.kalshi.list_series(limit=200, cursor=cursor)
            series.extend(payload.get("series", []))
            cursor = payload.get("cursor")
            if not cursor:
                break
        keywords = (
            "game",
            "match",
            "fight",
            "draft",
            "vs",
            "winner",
            "cup",
            "league",
            "playoff",
            "tournament",
        )
        candidates: list[dict[str, Any]] = []
        for item in series:
            category = item.get("category")
            title = str(item.get("title") or "").lower()
            if category not in {"Sports", "Entertainment", "Politics", "Economics"}:
                continue
            if not any(keyword in title for keyword in keywords):
                continue
            candidates.append(item)
        candidates.sort(key=self._series_priority, reverse=True)
        preferred_tickers = [
            "KXNBAGAME",
            "KXMLBGAME",
            "KXIPLGAME",
            "KXWNBAGAME",
            "KXNHLGAME",
        ]
        preferred = [item for ticker in preferred_tickers for item in candidates if item.get("ticker") == ticker]
        remaining = [item for item in candidates if item.get("ticker") not in set(preferred_tickers)]
        return preferred + remaining

    async def _fetch_series_markets(self, series_tickers: list[str]) -> list[list[dict[str, Any]]]:
        semaphore = asyncio.Semaphore(8)

        async def fetch(series_ticker: str) -> list[dict[str, Any]]:
            async with semaphore:
                for _attempt in range(2):
                    try:
                        payload = await self.kalshi.list_markets(series_ticker=series_ticker, status="open", limit=80)
                        return payload.get("markets", [])
                    except Exception:
                        continue
                return []

        tasks = [fetch(series_ticker) for series_ticker in series_tickers]
        return list(await asyncio.gather(*tasks))

    async def _fetch_event_payloads(self, event_tickers: list[str]) -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(8)

        async def fetch(event_ticker: str) -> dict[str, Any] | None:
            async with semaphore:
                try:
                    payload = await self.kalshi.get_event(event_ticker, with_nested_markets=True)
                    return payload.get("event")
                except Exception:
                    return None

        results = await asyncio.gather(*(fetch(event_ticker) for event_ticker in event_tickers))
        return [result for result in results if result]

    def _build_event_packet(self, event: dict[str, Any]) -> dict[str, Any] | None:
        title = event.get("title") or event.get("event_ticker")
        category = event.get("category") or "Unknown"
        if not title or category not in {"Sports", "Entertainment", "Politics", "Economics"}:
            return None
        markets = [
            market
            for market in (event.get("markets") or [])
            if market.get("market_type") == "binary" and market.get("status") == "active"
        ]
        if not markets:
            return None

        selected_market = self._select_best_market_for_event(event, markets)
        if selected_market is None:
            return None
        explicit_pick_only = bool(event.get("mutually_exclusive"))

        yes_price = self._mid_price(
            selected_market.get("yes_bid_dollars"),
            selected_market.get("yes_ask_dollars"),
            selected_market.get("last_price_dollars"),
        )
        yes_entry_price = float(selected_market.get("yes_ask_dollars") or yes_price or 0.0)
        no_price = self._mid_price(
            selected_market.get("no_bid_dollars"),
            selected_market.get("no_ask_dollars"),
            None,
        )
        no_entry_price = float(selected_market.get("no_ask_dollars") or no_price or 0.0)
        if explicit_pick_only:
            no_price = 0.0
            no_entry_price = 0.0
        if yes_price <= 0 or no_price <= 0:
            if not explicit_pick_only:
                return None
        liquidity_dollars = float(selected_market.get("liquidity_dollars") or 0.0)
        spread = max(
            0.0,
            float(selected_market.get("yes_ask_dollars") or 0.0)
            - float(selected_market.get("yes_bid_dollars") or 0.0),
        )
        event_day = self._market_event_date(selected_market)
        is_same_day = bool(event_day and event_day == self._local_today())
        event_time = self._market_event_datetime(selected_market)
        is_relevant_now = self._is_event_relevant_now(event_time)
        no_label = selected_market.get("no_sub_title") or "No"
        if explicit_pick_only:
            no_label = ""
        return {
            "event_ticker": event.get("event_ticker"),
            "series_ticker": event.get("series_ticker"),
            "event_title": title,
            "category": category,
            "event_type": self._event_type_label(
                {
                    "event_ticker": event.get("event_ticker"),
                    "series_ticker": event.get("series_ticker"),
                    "event_title": title,
                    "category": category,
                }
            ),
            "sub_title": event.get("sub_title"),
            "ticker": selected_market.get("ticker"),
            "market_ticker": selected_market.get("ticker"),
            "yes_label": selected_market.get("yes_sub_title") or "Yes",
            "no_label": no_label,
            "yes_price": round(yes_price, 4),
            "no_price": round(no_price, 4),
            "yes_entry_price": round(yes_entry_price, 4),
            "no_entry_price": round(no_entry_price, 4),
            "liquidity_dollars": round(liquidity_dollars, 2),
            "spread_dollars": round(spread, 4),
            "open_interest": float(selected_market.get("open_interest_fp") or 0.0),
            "close_time": selected_market.get("close_time") or selected_market.get("expiration_time"),
            "analysis_reasons": self._event_reasons(event, selected_market, yes_price, no_price, liquidity_dollars),
            "is_same_day": is_same_day,
            "event_day": event_day.isoformat() if event_day else None,
            "explicit_pick_only": explicit_pick_only,
            "is_relevant_now": is_relevant_now,
        }

    def _select_best_market_for_event(self, event: dict[str, Any], markets: list[dict[str, Any]]) -> dict[str, Any] | None:
        ranked: list[tuple[float, dict[str, Any]]] = []
        explicit_pick_only = bool(event.get("mutually_exclusive"))
        local_today = self._local_today()
        for market in markets:
            event_day = self._market_event_date(market)
            if event_day is None:
                continue
            delta_days = (event_day - local_today).days
            if not (0 <= delta_days <= 2):
                continue
            yes_bid = float(market.get("yes_bid_dollars") or 0.0)
            yes_ask = float(market.get("yes_ask_dollars") or 0.0)
            if yes_ask <= 0:
                continue
            mid_price = self._mid_price(yes_bid, yes_ask, market.get("last_price_dollars"))
            spread = max(0.0, yes_ask - yes_bid)
            same_day_bonus = 1.4 if delta_days == 0 else 0.7
            spread_penalty = spread * 2.5
            liquidity_bonus = min(1.0, log10(float(market.get("liquidity_dollars") or 0.0) + 1))
            if explicit_pick_only:
                label = str(market.get("yes_sub_title") or "").strip().lower()
                if label in {"tie", "draw"}:
                    continue
                target_price = 0.64
                price_fit = max(0.0, 1.0 - abs(mid_price - target_price) / 0.28)
                extreme_penalty = 0.5 if mid_price >= 0.9 else 0.0
                priority = same_day_bonus + liquidity_bonus + price_fit - spread_penalty - extreme_penalty
            else:
                confidence_score = min(mid_price, 1 - mid_price)
                priority = confidence_score + same_day_bonus + liquidity_bonus - spread_penalty
            ranked.append((priority, market))
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked[0][1]

    def _event_sort_key(self, event: dict[str, Any]) -> tuple[float, float]:
        markets = event.get("markets") or []
        same_day_score = 0.0
        best_price = 0.0
        local_today = self._local_today()
        for market in markets:
            event_day = self._market_event_date(market)
            if event_day and event_day == local_today:
                same_day_score = 1.0
            yes_ask = float(market.get("yes_ask_dollars") or 0.0)
            best_price = max(best_price, yes_ask)
        return same_day_score, best_price

    def _local_today(self) -> date:
        return datetime.now(self.local_tz).date()

    def _market_event_date(self, market: dict[str, Any]) -> date | None:
        ticker_value = self._ticker_date(market.get("ticker") or market.get("market_ticker") or "")
        if ticker_value is not None:
            return ticker_value
        close_time = market.get("close_time") or market.get("expiration_time")
        if not close_time:
            return None
        try:
            return datetime.fromisoformat(str(close_time).replace("Z", "+00:00")).astimezone(self.local_tz).date()
        except ValueError:
            return None

    def _market_event_day(self, market: dict[str, Any]) -> datetime | None:
        ticker_raw = market.get("ticker") or market.get("market_ticker") or ""
        ticker_value = self._ticker_datetime(ticker_raw)
        if ticker_value is not None:
            return ticker_value
        ticker_date = self._ticker_date(ticker_raw)
        close_time = market.get("close_time") or market.get("expiration_time")
        if not close_time:
            if ticker_date is None:
                return None
            return datetime.combine(ticker_date, datetime.min.time(), tzinfo=self.local_tz)
        try:
            close_dt = datetime.fromisoformat(str(close_time).replace("Z", "+00:00")).astimezone(self.local_tz)
            if ticker_date is not None:
                return datetime(
                    ticker_date.year,
                    ticker_date.month,
                    ticker_date.day,
                    close_dt.hour,
                    close_dt.minute,
                    tzinfo=self.local_tz,
                )
            return close_dt
        except ValueError:
            if ticker_date is None:
                return None
            return datetime.combine(ticker_date, datetime.min.time(), tzinfo=self.local_tz)

    def _market_event_datetime(self, market: dict[str, Any]) -> datetime | None:
        return self._market_event_day(market)

    def _ticker_date(self, ticker: str) -> date | None:
        match = self._event_ticker_date_pattern.search(ticker or "")
        if not match:
            return None
        year_s, month_s, day_s = match.groups()
        try:
            return date(
                2000 + int(year_s),
                self._event_ticker_months[month_s],
                int(day_s),
            )
        except ValueError:
            return None

    def _ticker_datetime(self, ticker: str) -> datetime | None:
        match = self._event_ticker_datetime_pattern.search(ticker or "")
        if not match:
            return None
        year_s, month_s, day_s, time_s = match.groups()
        try:
            event_date = date(
                2000 + int(year_s),
                self._event_ticker_months[month_s],
                int(day_s),
            )
        except ValueError:
            return None
        if not time_s:
            return None
        normalized_time = time_s.zfill(4)
        hours = int(normalized_time[:2])
        minutes = int(normalized_time[2:])
        if hours > 23 or minutes > 59:
            return None
        return datetime(
            event_date.year,
            event_date.month,
            event_date.day,
            hours,
            minutes,
            tzinfo=self.local_tz,
        )

    def _mid_price(self, bid: Any, ask: Any, fallback_last: Any) -> float:
        bid_value = float(bid or 0.0)
        ask_value = float(ask or 0.0)
        if bid_value > 0 and ask_value > 0:
            return (bid_value + ask_value) / 2.0
        if ask_value > 0:
            return ask_value
        if bid_value > 0:
            return bid_value
        return float(fallback_last or 0.0)

    def _is_near_term(self, value: str, *, max_days: int) -> bool:
        try:
            event_time = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        now = datetime.now(UTC)
        delta = event_time - now
        return 0 <= delta.days <= max_days

    def _series_priority(self, item: dict[str, Any]) -> float:
        title = str(item.get("title") or "").lower()
        category = str(item.get("category") or "")
        score = 0.0
        if category == "Sports":
            score += 4.0
        elif category == "Entertainment":
            score += 2.5
        elif category == "Economics":
            score += 1.5
        else:
            score += 1.0
        exact_title_bonuses = {
            "professional basketball game": 4.0,
            "professional baseball game": 4.0,
            "nba games": 3.5,
            "indian premier league cricket game": 3.0,
        }
        for phrase, bonus in exact_title_bonuses.items():
            if phrase in title:
                score += bonus
        bonuses = {
            "game": 3.0,
            "match": 2.7,
            "fight": 2.7,
            "basketball": 2.2,
            "nba": 2.0,
            "professional basketball": 2.0,
            "football": 1.8,
            "nfl": 1.8,
            "soccer": 1.8,
            "premier": 1.7,
            "cricket": 1.8,
            "baseball": 1.9,
            "mlb": 1.9,
            "floyd": 2.2,
            "tyson": 2.2,
            "tennis": 1.5,
            "hockey": 1.3,
            "draft": 0.6,
        }
        for keyword, bonus in bonuses.items():
            if keyword in title:
                score += bonus
        if "draft" in title and any(word in title for word in {"pick", "top", "first", "second", "third", "fourth"}):
            score -= 1.5
        return score

    def _candidate_event_score(self, series_item: dict[str, Any], market: dict[str, Any], delta_days: int) -> float:
        same_day_bonus = 3.0 if delta_days == 0 else 1.5
        liquidity_bonus = min(1.5, log10(float(market.get("liquidity_dollars") or 0.0) + 1))
        spread = max(
            0.0,
            float(market.get("yes_ask_dollars") or 0.0) - float(market.get("yes_bid_dollars") or 0.0),
        )
        label = str(market.get("yes_sub_title") or "").lower()
        readability_bonus = 0.6 if label not in {"tie", "draw", "yes"} else 0.0
        return self._series_priority(series_item) + same_day_bonus + liquidity_bonus + readability_bonus - spread * 2.0

    def portfolio_summary(self, session: Session) -> tuple[int, float]:
        symbols = self._portfolio_symbols(session)
        buying_power = self._current_buying_power(session)
        return len(symbols), buying_power

    def _finance_context_lines(self, session: Session) -> list[str]:
        row = session.exec(
            select(IntegrationState).where(
                IntegrationState.provider == "finance_profile",
                IntegrationState.key == "snapshot_context",
            )
        ).first()
        if row is None or not row.value:
            return []
        try:
            payload = json.loads(row.value)
        except json.JSONDecodeError:
            return []
        lines: list[str] = []
        espp = payload.get("espp") if isinstance(payload, dict) else None
        if isinstance(espp, dict):
            contribution_pct = espp.get("paycheck_contribution_pct")
            discount_pct = espp.get("discount_pct")
            lookback = espp.get("lookback")
            next_purchase_date = espp.get("next_purchase_date")
            planned_action = espp.get("planned_action")
            pieces = []
            if contribution_pct is not None:
                pieces.append(f"{float(contribution_pct):g}% of paycheck")
            if discount_pct is not None:
                pieces.append(f"{float(discount_pct):g}% discount")
            if lookback:
                pieces.append(str(lookback))
            if next_purchase_date:
                pieces.append(f"next purchase {next_purchase_date}")
            if planned_action:
                pieces.append(str(planned_action))
            if pieces:
                lines.append(f"- ESPP: {'; '.join(pieces)}")
        roth = payload.get("roth_ira") if isinstance(payload, dict) else None
        if isinstance(roth, dict):
            contribution_year = roth.get("contribution_year")
            contribution_amount = roth.get("contribution_amount")
            holdings = roth.get("holdings")
            pieces = []
            if contribution_year and contribution_amount is not None:
                pieces.append(f"{contribution_year} contribution ${float(contribution_amount):,.0f}")
            if isinstance(holdings, list) and holdings:
                pieces.append("invested in " + ", ".join(str(item).upper() for item in holdings))
            if pieces:
                lines.append(f"- Roth IRA: {'; '.join(pieces)}")
        k401 = payload.get("401k") if isinstance(payload, dict) else None
        if isinstance(k401, dict):
            contribution_pct = k401.get("paycheck_contribution_pct")
            current_allocation = k401.get("current_allocation")
            pieces = []
            if contribution_pct is not None:
                pieces.append(f"{float(contribution_pct):g}% paycheck contribution")
            if current_allocation:
                pieces.append(str(current_allocation))
            if pieces:
                lines.append(f"- 401(k): {'; '.join(pieces)}")
        cash = payload.get("cash_allocation") if isinstance(payload, dict) else None
        if isinstance(cash, dict):
            apy = cash.get("robinhood_high_yield_cash_apy_pct")
            varo_plan = cash.get("varo_plan")
            overflow_plan = cash.get("overflow_plan")
            pieces = []
            if apy is not None:
                pieces.append(f"Robinhood Gold cash {float(apy):g}% APY")
            if varo_plan:
                pieces.append(str(varo_plan))
            if overflow_plan:
                pieces.append(str(overflow_plan))
            if pieces:
                lines.append(f"- Cash allocation: {'; '.join(pieces)}")
        return lines

    def portfolio_lines(self, session: Session, limit: int = 8) -> list[str]:
        latest_snapshot = self._latest_snapshot(session)
        accounts = session.exec(select(Account)).all()
        holdings = sorted(self._primary_holdings(session, include_crypto=True), key=lambda row: row.market_value, reverse=True)
        snapshot_age_line = "Snapshot age: unavailable"
        if latest_snapshot and latest_snapshot.as_of:
            as_of = latest_snapshot.as_of.replace(tzinfo=UTC)
            age = datetime.now(UTC) - as_of
            hours = age.total_seconds() / 3600
            if hours < 1:
                snapshot_age_line = f"Snapshot age: {int(age.total_seconds() // 60)} minute(s)"
            elif hours < 48:
                snapshot_age_line = f"Snapshot age: {hours:.1f} hour(s)"
            else:
                snapshot_age_line = f"Snapshot age: {age.days} day(s)"

        lines = [
            "Portfolio scope: primary investing account only",
            f"Tracked accounts: {len(accounts)}",
            (
                "Latest buying power: "
                f"${latest_snapshot.buying_power:.2f}" if latest_snapshot else "Latest buying power: unavailable"
            ),
            (
                "Latest total value: "
                f"${latest_snapshot.total_value:.2f}" if latest_snapshot else "Latest total value: unavailable"
            ),
            snapshot_age_line,
            f"Tracked holdings (including crypto): {len(holdings)}",
        ]
        finance_context = self._finance_context_lines(session)
        if finance_context:
            lines.append("Finance context:")
            lines.extend(finance_context)
        if holdings:
            lines.append("Top holdings:")
            for holding in holdings[:limit]:
                lines.append(
                    f"- {holding.symbol}: {holding.quantity:g} share(s), value ${holding.market_value:.2f}"
                )
        else:
            lines.append("Top holdings: none synced yet")
        return lines
