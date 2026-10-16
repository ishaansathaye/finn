from __future__ import annotations

from datetime import UTC, datetime
import asyncio
import time
from typing import Any

import httpx

from app.config import get_settings


class SECAdapter:
    base_url = "https://data.sec.gov"
    ticker_map_url = "https://www.sec.gov/files/company_tickers.json"
    _next_request = 0.0

    async def _pace(self) -> None:
        # Reserve before awaiting; concurrent tasks share this process-wide budget.
        now = time.monotonic()
        slot = max(now, type(self)._next_request)
        type(self)._next_request = slot + 0.2
        await asyncio.sleep(max(0.0, slot - now))

    def __init__(self) -> None:
        self.settings = get_settings()

    def configured(self) -> bool:
        return True

    async def ticker_map(self) -> dict[str, dict[str, Any]]:
        data = await self._request_json(self.ticker_map_url)
        mapping: dict[str, dict[str, Any]] = {}
        for item in data.values():
            ticker = str(item.get("ticker") or "").upper().strip()
            if not ticker:
                continue
            cik = str(item.get("cik_str") or "").zfill(10)
            mapping[ticker] = {
                "symbol": ticker,
                "cik": cik,
                "name": item.get("title") or ticker,
            }
        return mapping

    async def resolve_symbol(self, symbol: str) -> dict[str, Any] | None:
        mapping = await self.ticker_map()
        return mapping.get(symbol.upper())

    async def get_submissions(self, cik: str) -> dict[str, Any]:
        return await self._request_json(f"{self.base_url}/submissions/CIK{cik}.json")

    async def get_companyfacts(self, cik: str) -> dict[str, Any]:
        return await self._request_json(f"{self.base_url}/api/xbrl/companyfacts/CIK{cik}.json")

    async def get_primary_document_text(
        self,
        *,
        cik: str,
        accession_number: str,
        primary_document: str,
    ) -> str:
        accession_path = accession_number.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_path}/{primary_document}"
        await self._pace()
        async with httpx.AsyncClient(timeout=45, headers=self._headers()) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    async def _request_json(self, url: str) -> dict[str, Any]:
        await self._pace()
        async with httpx.AsyncClient(timeout=45, headers=self._headers()) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.settings.sec_user_agent,
            "Accept-Encoding": "gzip, deflate",
        }


def parse_sec_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=UTC).replace(tzinfo=None)
    except ValueError:
        return None
