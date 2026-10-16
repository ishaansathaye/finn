from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import yfinance as yf


class YFinanceAdapter:
    def configured(self) -> bool:
        return True

    async def get_quotes(self, symbols: list[str]) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_quotes_sync, symbols)

    def _get_quotes_sync(self, symbols: list[str]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for symbol in symbols:
            try:
                ticker = yf.Ticker(symbol)
                fast_info = ticker.fast_info
                payload[symbol] = {
                    "symbol": symbol,
                    "close": fast_info.get("lastPrice"),
                    "previous_close": fast_info.get("previousClose"),
                    "high": fast_info.get("dayHigh"),
                    "low": fast_info.get("dayLow"),
                    "year_high": fast_info.get("yearHigh"),
                    "year_low": fast_info.get("yearLow"),
                    "market_cap": fast_info.get("marketCap"),
                }
            except Exception:
                payload[symbol] = {
                    "symbol": symbol,
                    "close": None,
                    "previous_close": None,
                    "high": None,
                    "low": None,
                    "year_high": None,
                    "year_low": None,
                    "market_cap": None,
                }
        return payload

    async def get_news(self, symbol: str, limit: int = 6) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._get_news_sync, symbol, limit)

    def _get_news_sync(self, symbol: str, limit: int) -> list[dict[str, Any]]:
        ticker = yf.Ticker(symbol)
        try:
            raw_items = ticker.news[:limit]
        except Exception:
            return []
        normalized: list[dict[str, Any]] = []
        for item in raw_items:
            content = item.get("content") or {}
            normalized.append(
                {
                    "title": content.get("title") or item.get("title"),
                    "summary": content.get("summary") or content.get("description") or "",
                    "published_at": content.get("pubDate") or item.get("providerPublishTime"),
                    "provider": ((content.get("provider") or {}).get("displayName") if isinstance(content.get("provider"), dict) else None),
                }
            )
        return normalized

    async def get_calendar(self, symbol: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_calendar_sync, symbol)

    def _get_calendar_sync(self, symbol: str) -> dict[str, Any]:
        ticker = yf.Ticker(symbol)
        try:
            calendar = ticker.calendar or {}
        except Exception:
            return {}
        normalized: dict[str, Any] = {}
        for key, value in calendar.items():
            if isinstance(value, list):
                normalized[key] = [str(v) for v in value]
            else:
                normalized[key] = str(value)
        return normalized

    async def get_history(
        self,
        symbol: str,
        *,
        period: str = "1y",
        interval: str = "1d",
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._get_history_sync, symbol, period, interval)

    def _get_history_sync(self, symbol: str, period: str, interval: str) -> list[dict[str, Any]]:
        ticker = yf.Ticker(symbol)
        try:
            history = ticker.history(period=period, interval=interval, auto_adjust=False)
        except Exception:
            return []
        if history is None or history.empty:
            return []

        rows: list[dict[str, Any]] = []
        for index, row in history.iterrows():
            timestamp = index.to_pydatetime() if hasattr(index, "to_pydatetime") else index
            if getattr(timestamp, "tzinfo", None) is not None:
                timestamp = timestamp.astimezone(UTC).replace(tzinfo=None)
            elif isinstance(timestamp, datetime):
                timestamp = timestamp.replace(tzinfo=None)
            rows.append(
                {
                    "datetime": timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp),
                    "open": float(row.get("Open") or 0.0),
                    "high": float(row.get("High") or 0.0),
                    "low": float(row.get("Low") or 0.0),
                    "close": float(row.get("Close") or 0.0),
                    "adjusted_close": float(row.get("Adj Close", row.get("Close")) or 0.0),
                    "volume": float(row.get("Volume") or 0.0),
                }
            )
        return rows
