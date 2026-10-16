from __future__ import annotations

from base64 import b64encode
import hashlib
import hmac
import json
import time
import asyncio
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlmodel import Session

from app.config import get_settings
from app.services.integration_state import IntegrationStateStore


class SnapTradeAdapter:
    base_url = "https://api.snaptrade.com/api/v1"

    def __init__(self) -> None:
        self.settings = get_settings()
        self.state_store = IntegrationStateStore()

    def configured(self) -> bool:
        return bool(self.settings.snaptrade_client_id and self.settings.snaptrade_consumer_key)

    def _user_id(self, session: Session) -> str:
        return self.state_store.get(session, "snaptrade", "user_id_override") or self.settings.snaptrade_user_id

    def _compute_signature(self, path_with_query: str, body: Any) -> str:
        if "?" in path_with_query:
            subpath, query = path_with_query.split("?", 1)
        else:
            subpath, query = path_with_query, ""
        sig_object = {
            "content": None if body in (None, {}) else body,
            "path": f"/api/v1{subpath}",
            "query": query,
        }
        sig_content = json.dumps(sig_object, separators=(",", ":"), sort_keys=True)
        sig_digest = hmac.new(
            (self.settings.snaptrade_consumer_key or "").encode(),
            sig_content.encode(),
            hashlib.sha256,
        ).digest()
        return b64encode(sig_digest).decode()

    def _headers(self, path_with_query: str, body: Any = None) -> dict[str, str]:
        return {
            "Signature": self._compute_signature(path_with_query, body),
            "Content-Type": "application/json",
        }

    def _with_auth_params(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "clientId": self.settings.snaptrade_client_id or "",
            "timestamp": int(time.time()),
            **params,
        }

    async def ensure_user(self, session: Session) -> tuple[str, str]:
        user_id = self._user_id(session)
        existing_secret = self.state_store.get(session, "snaptrade", "user_secret")
        if existing_secret:
            return user_id, existing_secret

        async def register(candidate_user_id: str) -> tuple[int, dict[str, Any]]:
            params = self._with_auth_params({})
            query = urlencode(params)
            path = f"/snapTrade/registerUser?{query}"
            body = {"userId": candidate_user_id}
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self.base_url}/snapTrade/registerUser",
                    headers=self._headers(path, body),
                    params=params,
                    json=body,
                )
            payload: dict[str, Any]
            try:
                payload = response.json()
            except Exception:  # noqa: BLE001
                payload = {"detail": response.text}
            return response.status_code, payload

        async def delete_user(candidate_user_id: str) -> tuple[int, dict[str, Any]]:
            params = self._with_auth_params({"userId": candidate_user_id})
            query = urlencode(params)
            path = f"/snapTrade/deleteUser?{query}"
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.delete(
                    f"{self.base_url}/snapTrade/deleteUser",
                    headers=self._headers(path),
                    params=params,
                )
            try:
                payload = response.json()
            except Exception:  # noqa: BLE001
                payload = {"detail": response.text}
            return response.status_code, payload

        status_code, payload = await register(user_id)
        if status_code == 200:
            user_secret = payload["userSecret"]
            self.state_store.set(session, "snaptrade", "user_secret", user_secret)
            return user_id, user_secret

        if status_code == 400 and str(payload.get("code") or "") == "1010":
            new_user_id = f"{self.settings.snaptrade_user_id}-{int(time.time())}"
            status_code, payload = await register(new_user_id)
            if status_code == 200:
                user_secret = payload["userSecret"]
                self.state_store.set(session, "snaptrade", "user_id_override", new_user_id)
                self.state_store.set(session, "snaptrade", "user_secret", user_secret)
                return new_user_id, user_secret
            if status_code == 400 and str(payload.get("code") or "") == "1012":
                delete_status, delete_payload = await delete_user(user_id)
                if delete_status == 200:
                    for _ in range(8):
                        await asyncio.sleep(2)
                        status_code, payload = await register(user_id)
                        if status_code == 200:
                            self.state_store.delete(session, "snaptrade", "user_id_override")
                            user_secret = payload["userSecret"]
                            self.state_store.set(session, "snaptrade", "user_secret", user_secret)
                            return user_id, user_secret
                    raise RuntimeError(
                        "SnapTrade queued the stale user for deletion. "
                        "Wait a minute, then run `connect` again to reconnect Robinhood."
                    )
                raise RuntimeError(
                    delete_payload.get("detail")
                    or "SnapTrade could not delete the stale user needed for reconnect."
                )
            raise RuntimeError(
                "SnapTrade user secret is missing and automatic recovery failed. "
                "Run `connect` to reconnect Robinhood through SnapTrade."
            )

        raise RuntimeError(payload.get("detail") or f"SnapTrade user registration failed ({status_code}).")

    async def generate_connection_portal_url(self, session: Session) -> str:
        user_id, user_secret = await self.ensure_user(session)
        params = self._with_auth_params({"userId": user_id, "userSecret": user_secret})
        query = urlencode(params)
        path = f"/snapTrade/login?{query}"
        body = {
            "broker": "ROBINHOOD",
            "connectionType": "read",
            "connectionPortalVersion": "v4",
            "immediateRedirect": False,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/snapTrade/login",
                headers=self._headers(path, body),
                params=params,
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
        return payload["redirectURI"]

    async def list_accounts(self, session: Session) -> list[dict[str, Any]]:
        user_id, user_secret = await self.ensure_user(session)
        params = self._with_auth_params({"userId": user_id, "userSecret": user_secret})
        query = urlencode(params)
        path = f"/accounts?{query}"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.base_url}/accounts",
                headers=self._headers(path),
                params=params,
            )
            response.raise_for_status()
            return response.json()

    async def get_account_holdings(self, session: Session, account_id: str) -> dict[str, Any]:
        user_id, user_secret = await self.ensure_user(session)
        params = self._with_auth_params({"userId": user_id, "userSecret": user_secret})
        query = urlencode(params)
        path = f"/accounts/{account_id}/holdings?{query}"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.base_url}/accounts/{account_id}/holdings",
                headers=self._headers(path),
                params=params,
            )
            response.raise_for_status()
            return response.json()
