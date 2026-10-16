from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.config import get_settings


class KalshiAdapter:
    base_url = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(self) -> None:
        self.settings = get_settings()
        self._private_key = None

    def configured(self) -> bool:
        return bool(self.settings.kalshi_api_id and self.settings.effective_kalshi_private_key)

    def _load_private_key(self):  # noqa: ANN202
        if self._private_key is not None:
            return self._private_key
        key_text = self.settings.effective_kalshi_private_key
        if not key_text:
            raise RuntimeError("Kalshi private key is not configured.")
        self._private_key = serialization.load_pem_private_key(key_text.encode("utf-8"), password=None)
        return self._private_key

    def _signature(self, timestamp_ms: str, method: str, path: str) -> str:
        path_without_query = path.split("?", 1)[0]
        message = f"{timestamp_ms}{method.upper()}{path_without_query}".encode("utf-8")
        signature = self._load_private_key().sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        if not self.settings.kalshi_api_id:
            raise RuntimeError("Kalshi API key id is not configured.")
        timestamp_ms = str(int(datetime.now(UTC).timestamp() * 1000))
        sign_path = urlparse(self.base_url + path).path
        return {
            "KALSHI-ACCESS-KEY": self.settings.kalshi_api_id,
            "KALSHI-ACCESS-SIGNATURE": self._signature(timestamp_ms, method, sign_path),
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        authenticated: bool = False,
    ) -> dict[str, Any]:
        headers = {}
        if authenticated:
            headers.update(self._auth_headers(method, path))
            if json_body is not None:
                headers["Content-Type"] = "application/json"
        timeout = httpx.Timeout(connect=15.0, read=30.0, write=15.0, pool=15.0)
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            response = await client.request(
                method,
                f"{self.base_url}{path}",
                params=params,
                json=json_body,
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                return payload
            return {"results": payload}

    async def list_open_markets(self, limit: int | None = None) -> dict[str, Any]:
        params = {
            "status": "open",
            "limit": limit or self.settings.kalshi_market_limit,
        }
        return await self._request("GET", "/markets", params=params)

    async def list_open_events(self, limit: int | None = None) -> dict[str, Any]:
        params = {
            "status": "open",
            "limit": limit or self.settings.kalshi_market_limit,
            "with_nested_markets": "true",
        }
        return await self._request("GET", "/events", params=params)

    async def list_series(self, *, limit: int = 200, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/series", params=params)

    async def list_markets(
        self,
        *,
        status: str | None = "open",
        limit: int = 200,
        cursor: str | None = None,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        tickers: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if tickers:
            params["tickers"] = tickers
        return await self._request("GET", "/markets", params=params)

    async def get_event(self, event_ticker: str, *, with_nested_markets: bool = True) -> dict[str, Any]:
        params = {"with_nested_markets": str(with_nested_markets).lower()}
        return await self._request("GET", f"/events/{event_ticker}", params=params)

    async def get_balance(self) -> dict[str, Any]:
        return await self._request("GET", "/portfolio/balance", authenticated=True)

    async def get_orders(self, *, limit: int = 50, status: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        return await self._request("GET", "/portfolio/orders", params=params, authenticated=True)

    async def create_event_order(
        self,
        *,
        market_ticker: str,
        count: int,
        yes_price: float,
        client_order_id: str,
    ) -> dict[str, Any]:
        body = {
            "ticker": market_ticker,
            "client_order_id": client_order_id,
            "side": "bid",
            "count": f"{float(count):.2f}",
            "price": f"{float(yes_price):.4f}",
            "time_in_force": "fill_or_kill",
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": False,
            "cancel_order_on_pause": True,
            "reduce_only": False,
            "subaccount": 0,
        }
        return await self._request("POST", "/portfolio/events/orders", json_body=body, authenticated=True)
