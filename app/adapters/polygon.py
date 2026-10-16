from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings


class PolygonAdapter:
    base_url = "https://api.massive.com"

    def __init__(self) -> None:
        self.settings = get_settings()

    def configured(self) -> bool:
        return bool(self.settings.effective_polygon_key)

    async def get_snapshots(self, tickers: list[str]) -> dict[str, Any]:
        if not self.configured():
            raise RuntimeError("Massive/Polygon API key is not configured")
        params = {
            "tickers": ",".join(tickers),
            "apiKey": self.settings.effective_polygon_key,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.base_url}/v2/snapshot/locale/us/markets/stocks/tickers",
                params=params,
            )
            response.raise_for_status()
            return response.json()
