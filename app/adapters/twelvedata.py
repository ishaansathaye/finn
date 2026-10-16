from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings


class TwelveDataAdapter:
    base_url = "https://api.twelvedata.com"

    def __init__(self) -> None:
        self.settings = get_settings()

    def configured(self) -> bool:
        return bool(self.settings.effective_twelve_data_key)

    async def get_quotes(self, symbols: list[str]) -> dict[str, Any]:
        if not self.configured():
            raise RuntimeError("Twelve Data API key is not configured")
        params = {
            "symbol": ",".join(symbols),
            "apikey": self.settings.effective_twelve_data_key,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{self.base_url}/quote", params=params)
            response.raise_for_status()
            return response.json()
