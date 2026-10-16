from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import asyncio
import json
import logging
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any
from uuid import uuid4

import httpx

from app.config import get_settings


logger = logging.getLogger(__name__)

try:
    import browser_cookie3
except ImportError:  # pragma: no cover - dependency is optional at import time
    browser_cookie3 = None


_NEXT_DATA_PATTERN = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)

# Public mobile OAuth application identifier, not a user credential or client secret.
_OAUTH_CLIENT_ID = "c82SH0WZOsabOXGP2sxqcj34FxkvfnWRZBKlBjFS"  # gitleaks:allow


@dataclass
class RobinhoodCatalogSnapshot:
    events: list[dict[str, Any]]
    event_states: dict[str, dict[str, Any]]
    quotes: dict[str, dict[str, Any]]
    fundamentals: dict[str, dict[str, Any]]
    navigation_nodes: list[dict[str, Any]]
    loaded_at: datetime


@dataclass
class RobinhoodWriteSession:
    access_token: str
    token_type: str
    device_token: str
    refresh_token: str | None = None
    saved_at: float | None = None

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "RobinhoodWriteSession":
        return cls(
            access_token=str(payload.get("access_token") or payload.get("accessToken") or ""),
            token_type=str(payload.get("token_type") or payload.get("tokenType") or "Bearer"),
            device_token=str(payload.get("device_token") or payload.get("deviceToken") or ""),
            refresh_token=(
                str(payload.get("refresh_token") or payload.get("refreshToken"))
                if payload.get("refresh_token") or payload.get("refreshToken")
                else None
            ),
            saved_at=float(payload.get("saved_at")) if payload.get("saved_at") is not None else None,
        )

    def to_json(self) -> dict[str, Any]:
        payload = {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "device_token": self.device_token,
            "saved_at": self.saved_at if self.saved_at is not None else time.time(),
        }
        if self.refresh_token:
            payload["refresh_token"] = self.refresh_token
        return payload


class RobinhoodPredictionAdapter:
    api_base_url = "https://api.robinhood.com"
    oauth_token_url = "https://api.robinhood.com/oauth2/token/"
    public_prediction_url = "https://robinhood.com/us/en/prediction-markets/"

    def __init__(self) -> None:
        self.settings = get_settings()
        self._bearer_token_cache: str | None = None
        self._catalog_cache: RobinhoodCatalogSnapshot | None = None
        self._catalog_cache_expires_at: datetime | None = None
        self._write_session_cache: RobinhoodWriteSession | None = None

    def _debug_enabled(self) -> bool:
        return os.environ.get("FINN_RH_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}

    def _debug(self, message: str) -> None:
        if self._debug_enabled():
            print(f"[robinhood-write] {message}", flush=True)

    def configured(self) -> bool:
        return browser_cookie3 is not None

    def can_write(self) -> bool:
        if self.settings.robinhood_write_access_token and self._configured_write_device_id():
            return True
        if self._load_write_session_file() is not None:
            return True
        return bool(self.settings.robinhood_username and self.settings.robinhood_password)

    def write_bootstrap_configured(self) -> bool:
        return bool(self.settings.robinhood_username and self.settings.robinhood_password)

    def _configured_write_device_id(self) -> str | None:
        return self.settings.robinhood_write_device_id or self._browser_device_id()

    def _write_session_path(self) -> Path:
        return Path(self.settings.robinhood_write_session_path).expanduser()

    def _load_write_session_file(self) -> RobinhoodWriteSession | None:
        path = self._write_session_path()
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
        except Exception:
            return None
        session = RobinhoodWriteSession.from_json(payload)
        if session.access_token and session.device_token:
            return session
        return None

    def _save_write_session_file(self, session: RobinhoodWriteSession) -> None:
        path = self._write_session_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(session.to_json(), indent=2))

    def _load_bearer_token(self) -> str:
        if self._bearer_token_cache:
            return self._bearer_token_cache
        if browser_cookie3 is None:
            raise RuntimeError("browser_cookie3 is not installed in the active environment.")

        cookie_loaders = [
            ("safari", browser_cookie3.safari),
            ("chrome", browser_cookie3.chrome),
            ("chromium", browser_cookie3.chromium),
            ("edge", browser_cookie3.edge),
            ("firefox", browser_cookie3.firefox),
            ("brave", browser_cookie3.brave),
        ]
        last_error: Exception | None = None
        for _name, loader in cookie_loaders:
            try:
                jar = loader(domain_name="robinhood.com")
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
            for cookie in jar:
                if cookie.name == "__Host-Web-App-Secondary-Access-Token" and cookie.value:
                    self._bearer_token_cache = cookie.value
                    return cookie.value
        raise RuntimeError(f"Could not load Robinhood web access token from local browser cookies: {last_error}")

    def _try_cookie_read_auth_headers(self) -> dict[str, str] | None:
        try:
            token = self._load_bearer_token()
        except Exception as exc:  # noqa: BLE001
            self._debug(f"cookie-based Robinhood read auth unavailable: {exc}")
            return None
        return {
            "Authorization": f"Bearer {token}",
            "X-Robinhood-API-Version": "1.431.4",
            "X-Hyper-Ex": "enabled",
            "User-Agent": "Mozilla/5.0",
        }

    def _read_headers_from_write_session(self, session: RobinhoodWriteSession) -> dict[str, str]:
        return {
            "Authorization": f"{session.token_type} {session.access_token}",
            "X-Robinhood-API-Version": "1.431.4",
            "X-Hyper-Ex": "enabled",
            "User-Agent": self.settings.robinhood_write_user_agent or "Mozilla/5.0",
            "X-Robinhood-Device-Id": session.device_token,
            "X-Device-Id": session.device_token,
        }

    def _browser_device_id(self) -> str | None:
        if browser_cookie3 is None:
            return None
        try:
            jar = browser_cookie3.safari(domain_name="robinhood.com")
        except Exception:
            return None
        for cookie in jar:
            if cookie.name == "device_id" and cookie.value:
                return cookie.value
        return None

    def _generate_device_token(self) -> str:
        raw = [secrets.randbelow(256) for _ in range(16)]
        out = ""
        for idx, value in enumerate(raw):
            out += f"{value:02x}"
            if idx in (3, 5, 7, 9):
                out += "-"
        return out

    def _mobile_headers(
        self,
        session: RobinhoodWriteSession | None = None,
        *,
        json_content: bool = False,
        challenge_response_id: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip,deflate,br",
            "Accept-Language": "en-US,en;q=1",
            "Content-Type": "application/json" if json_content else "application/x-www-form-urlencoded; charset=utf-8",
            "Connection": "keep-alive",
            "User-Agent": self.settings.robinhood_write_user_agent,
            "X-Robinhood-API-Version": "1.431.4",
            "X-Hyper-Ex": "enabled",
        }
        if session is not None:
            headers["Authorization"] = f"{session.token_type} {session.access_token}"
            headers["X-Robinhood-Device-Id"] = session.device_token
            headers["X-Device-Id"] = session.device_token
        if challenge_response_id:
            headers["X-ROBINHOOD-CHALLENGE-RESPONSE-ID"] = challenge_response_id
        return headers

    def _session_saved_recently(self, session: RobinhoodWriteSession) -> bool:
        if session.saved_at is None:
            return False
        return (time.time() - session.saved_at) < 20 * 60 * 60

    async def _read_json_response(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if response.status_code >= 400:
            detail = payload.get("detail") or payload.get("message") or response.text
            raise RuntimeError(f"Robinhood request failed ({response.status_code}): {detail}")
        if isinstance(payload, dict):
            return payload
        return {"results": payload}

    async def _post_oauth_token(
        self,
        payload: dict[str, Any],
        *,
        challenge_response_id: str | None = None,
    ) -> dict[str, Any]:
        self._debug(
            "POST /oauth2/token "
            f"grant_type={payload.get('grant_type')} "
            f"has_mfa={'mfa_code' in payload} "
            f"secondary={payload.get('create_read_only_secondary_token')}"
        )
        headers = self._mobile_headers(challenge_response_id=challenge_response_id)
        async with httpx.AsyncClient(headers=headers, timeout=45) as client:
            response = await client.post(self.oauth_token_url, data=payload)
            try:
                body = response.json() if response.text else {}
            except Exception:
                body = {}
            if response.status_code in {400, 401, 402, 403}:
                return body if isinstance(body, dict) else {"results": body}
            response.raise_for_status()
            return body if isinstance(body, dict) else {"results": body}

    async def _handle_verification_workflow(
        self,
        *,
        device_token: str,
        workflow_id: str,
        timeout_seconds: float,
    ) -> None:
        self._debug(f"verification workflow started id={workflow_id}")
        machine_payload = {
            "device_id": device_token,
            "flow": "suv",
            "input": {"workflow_id": workflow_id},
        }
        start = time.monotonic()
        async with httpx.AsyncClient(headers=self._mobile_headers(json_content=True), timeout=45) as client:
            machine_response = await client.post(
                f"{self.api_base_url}/pathfinder/user_machine/",
                json=machine_payload,
            )
            machine_data = await self._read_json_response(machine_response)
            machine_id = str(machine_data.get("id") or "")
            if not machine_id:
                raise RuntimeError("Robinhood verification did not return a machine id.")
            self._debug(f"pathfinder machine id={machine_id}")
            inquiries_url = f"{self.api_base_url}/pathfinder/inquiries/{machine_id}/user_view/"
            challenge_id = ""
            while time.monotonic() - start < timeout_seconds:
                await asyncio.sleep(5)
                inquiry_response = await client.get(inquiries_url)
                inquiry_data = await self._read_json_response(inquiry_response)
                challenge = ((inquiry_data.get("context") or {}).get("sheriff_challenge") or {})
                challenge_type = str(challenge.get("type") or "")
                challenge_status = str(challenge.get("status") or "")
                challenge_id = str(challenge.get("id") or challenge_id)
                self._debug(
                    f"inquiry challenge_type={challenge_type or '-'} "
                    f"challenge_status={challenge_status or '-'} "
                    f"challenge_id={challenge_id or '-'}"
                )
                if challenge_type == "prompt" and challenge_id:
                    self._debug("waiting for Robinhood app approval prompt validation")
                    prompt_url = f"{self.api_base_url}/push/{challenge_id}/get_prompts_status/"
                    prompt_timed_out = True
                    while time.monotonic() - start < timeout_seconds:
                        await asyncio.sleep(5)
                        prompt_response = await client.get(prompt_url)
                        prompt_data = await self._read_json_response(prompt_response)
                        prompt_status = str(prompt_data.get("challenge_status") or "")
                        self._debug(f"prompt status={prompt_status or '-'}")
                        if prompt_status == "validated":
                            self._debug("device approval validated")
                            prompt_timed_out = False
                            break
                        if prompt_status in {"expired", "failed", "canceled", "cancelled"}:
                            self._debug(
                                "prompt is no longer active; returning to inquiry polling to check overall workflow status"
                            )
                            prompt_timed_out = False
                            break
                    if prompt_timed_out:
                        raise RuntimeError("Robinhood device approval timed out. Approve the login in the Robinhood app and retry.")
                    break
                if challenge_status == "validated":
                    self._debug("verification challenge validated")
                    break
            else:
                raise RuntimeError("Robinhood verification timed out. Approve the login in the Robinhood app and retry.")

            retry_attempts = 5
            while time.monotonic() - start < timeout_seconds and retry_attempts > 0:
                await asyncio.sleep(5)
                inquiries_payload = {"sequence": 0, "user_input": {"status": "continue"}}
                self._debug("posting workflow continuation to inquiries endpoint")
                try:
                    continue_response = await client.post(inquiries_url, json=inquiries_payload)
                    continue_data = await self._read_json_response(continue_response)
                except Exception as exc:  # noqa: BLE001
                    retry_attempts -= 1
                    self._debug(f"workflow continuation failed: {exc}")
                    continue
                type_context = continue_data.get("type_context") or {}
                workflow = continue_data.get("verification_workflow") or {}
                result = str(type_context.get("result") or "")
                workflow_status = str(workflow.get("workflow_status") or "")
                self._debug(
                    "workflow continuation result="
                    f"{result or '-'} status={workflow_status or '-'} retries_left={retry_attempts}"
                )
                if result == "workflow_status_approved" or workflow_status == "workflow_status_approved":
                    self._debug("workflow approved")
                    return
                if workflow_status == "workflow_status_internal_pending":
                    retry_attempts -= 1
                    continue
                retry_attempts -= 1
            self._debug("workflow continuation timed out; proceeding to token retry anyway")
            return

    async def bootstrap_write_session(
        self,
        *,
        username: str | None = None,
        password: str | None = None,
        mfa_code: str | None = None,
        timeout_seconds: float = 120,
        force_new_device_token: bool = False,
    ) -> RobinhoodWriteSession:
        username = username or self.settings.robinhood_username
        password = password or self.settings.robinhood_password
        mfa_code = mfa_code or self.settings.robinhood_mfa_code
        if not username or not password:
            raise RuntimeError("Robinhood username/password are required to bootstrap write auth.")

        existing = None if force_new_device_token else self._load_write_session_file()
        device_token = (
            self.settings.robinhood_write_device_id
            or (None if force_new_device_token else (existing.device_token if existing is not None else None))
            or (None if force_new_device_token else self._browser_device_id())
            or self._generate_device_token()
        )
        self._debug(f"using device token {device_token}")
        login_payload: dict[str, Any] = {
            "client_id": _OAUTH_CLIENT_ID,
            "expires_in": 86400,
            "grant_type": "password",
            "password": password,
            "scope": "internal",
            "username": username,
            "device_token": device_token,
            "try_passkeys": False,
            "token_request_path": "/login",
        }
        if self.settings.robinhood_write_auth_mode != "secondary":
            login_payload["create_read_only_secondary_token"] = False
        if mfa_code:
            login_payload["mfa_code"] = mfa_code

        started_at = time.monotonic()
        data = await self._post_oauth_token(login_payload)
        while "verification_workflow" in data:
            workflow = data.get("verification_workflow") or {}
            workflow_id = str(workflow.get("id") or "")
            workflow_status = str(workflow.get("workflow_status") or "")
            self._debug(f"login returned verification_workflow status={workflow_status or '-'} id={workflow_id}")
            if not workflow_id:
                raise RuntimeError("Robinhood requested verification without a workflow id.")
            await self._handle_verification_workflow(
                device_token=device_token,
                workflow_id=workflow_id,
                timeout_seconds=max(30.0, timeout_seconds - (time.monotonic() - started_at)),
            )
            if (time.monotonic() - started_at) >= timeout_seconds:
                raise RuntimeError("Robinhood verification timed out before write auth completed.")
            await asyncio.sleep(4)
            self._debug("retrying token exchange after verification")
            data = await self._post_oauth_token(login_payload)
            if "access_token" in data:
                self._debug("write access token received after verification")
                break
            if "verification_workflow" in data:
                next_status = str((data.get("verification_workflow") or {}).get("workflow_status") or "")
                self._debug(f"verification still pending status={next_status or '-'}")
                if next_status not in {"workflow_status_internal_pending", "workflow_status_pending", workflow_status}:
                    break
        if data.get("mfa_required") and not mfa_code:
            raise RuntimeError("Robinhood MFA is required. Set ROBINHOOD_MFA_CODE or rerun the login script with a code.")
        if "challenge" in data:
            raise RuntimeError("Robinhood requested an SMS/email challenge. Complete device approval in the Robinhood app or provide MFA and retry.")
        if "access_token" not in data:
            raise RuntimeError(f"Robinhood write login failed: {data.get('detail') or data}")
        session = RobinhoodWriteSession(
            access_token=str(data["access_token"]),
            token_type=str(data.get("token_type") or "Bearer"),
            device_token=device_token,
            refresh_token=str(data["refresh_token"]) if data.get("refresh_token") else None,
            saved_at=time.time(),
        )
        self._debug("write session bootstrapped successfully")
        self._write_session_cache = session
        self._save_write_session_file(session)
        return session

    async def _refresh_write_session(self, session: RobinhoodWriteSession) -> RobinhoodWriteSession:
        if not session.refresh_token:
            raise RuntimeError("Robinhood write auth refresh token is missing. Re-run the write login bootstrap.")
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": session.refresh_token,
            "scope": "internal",
            "client_id": _OAUTH_CLIENT_ID,
            "device_token": session.device_token,
            "expires_in": 86400,
        }
        data = await self._post_oauth_token(payload)
        if "verification_workflow" in data:
            raise RuntimeError("Robinhood write auth refresh now requires device approval. Re-run the write login bootstrap.")
        if "access_token" not in data:
            raise RuntimeError(f"Robinhood write auth refresh failed: {data.get('detail') or data}")
        refreshed = RobinhoodWriteSession(
            access_token=str(data["access_token"]),
            token_type=str(data.get("token_type") or session.token_type or "Bearer"),
            device_token=session.device_token,
            refresh_token=str(data["refresh_token"]) if data.get("refresh_token") else session.refresh_token,
            saved_at=time.time(),
        )
        self._write_session_cache = refreshed
        self._save_write_session_file(refreshed)
        return refreshed

    async def _ensure_write_session(self) -> RobinhoodWriteSession:
        if self._write_session_cache is not None:
            return self._write_session_cache
        if self.settings.robinhood_write_access_token:
            device_token = self._configured_write_device_id()
            if not device_token:
                raise RuntimeError("ROBINHOOD_WRITE_DEVICE_ID is required when using ROBINHOOD_WRITE_ACCESS_TOKEN.")
            session = RobinhoodWriteSession(
                access_token=self.settings.robinhood_write_access_token,
                token_type="Bearer",
                device_token=device_token,
                refresh_token=self.settings.robinhood_write_refresh_token,
                saved_at=time.time(),
            )
            self._write_session_cache = session
            return session
        stored = self._load_write_session_file()
        if stored is not None:
            self._write_session_cache = stored
            return stored
        if self.settings.robinhood_username and self.settings.robinhood_password:
            return await self.bootstrap_write_session()
        raise RuntimeError(
            "Robinhood write auth is not configured. Run scripts/robinhood_write_login.py once, "
            "or set ROBINHOOD_WRITE_ACCESS_TOKEN and ROBINHOOD_WRITE_DEVICE_ID."
        )

    def write_auth_headers(self, session: RobinhoodWriteSession) -> dict[str, str]:
        return self._mobile_headers(session=session, json_content=True)

    async def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{self.api_base_url}{path}"
        cookie_headers = self._try_cookie_read_auth_headers()
        if cookie_headers is not None:
            async with httpx.AsyncClient(headers=cookie_headers, timeout=30) as client:
                response = await client.get(url, params=params)
                if response.status_code < 400:
                    payload = response.json()
                    if isinstance(payload, dict):
                        return payload
                    return {"results": payload}
                self._debug(f"cookie-based Robinhood read request failed status={response.status_code}; falling back to write session")

        write_session = await self._ensure_write_session()
        headers = self._read_headers_from_write_session(write_session)
        async with httpx.AsyncClient(headers=headers, timeout=30) as client:
            response = await client.get(url, params=params)
            if response.status_code in {401, 403} and write_session.refresh_token:
                latest = self._load_write_session_file()
                if latest and (
                    latest.access_token != write_session.access_token
                    or latest.refresh_token != write_session.refresh_token
                ):
                    self._write_session_cache = latest
                    latest_headers = self._read_headers_from_write_session(latest)
                    async with httpx.AsyncClient(headers=latest_headers, timeout=30) as latest_client:
                        response = await latest_client.get(url, params=params)
                    write_session = latest
                if response.status_code in {401, 403} and write_session.refresh_token:
                    try:
                        refreshed = await self._refresh_write_session(write_session)
                    except RuntimeError as exc:
                        raise RuntimeError(
                            "Robinhood write auth expired. Re-run `PYTHONPATH=. .venv/bin/python scripts/robinhood_write_login.py`."
                        ) from exc
                    retry_headers = self._read_headers_from_write_session(refreshed)
                    async with httpx.AsyncClient(headers=retry_headers, timeout=30) as retry_client:
                        response = await retry_client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                return payload
            return {"results": payload}

    async def list_event_accounts(self) -> list[dict[str, Any]]:
        payload = await self._get_json("/ceres/v1/accounts")
        return payload.get("results") or payload.get("accounts") or []

    async def get_event_contract_account(self) -> dict[str, Any] | None:
        accounts = await self.list_event_accounts()
        configured_account_id = str(self.settings.robinhood_prediction_account_id or "").strip()
        if configured_account_id:
            for account in accounts:
                if str(account.get("id") or "") == configured_account_id:
                    return account
            raise RuntimeError(
                "Configured Robinhood prediction account id was not returned by Robinhood; "
                "refusing to choose another account."
            )
        configured_rhs = str(self.settings.robinhood_prediction_rhs_account_number or "").strip()
        if configured_rhs:
            matching = [
                account
                for account in accounts
                if str(account.get("rhsAccountNumber") or account.get("rhfAccountNumber") or "") == configured_rhs
            ]
            swap_matching = [account for account in matching if str(account.get("accountType")) == "SWAP"]
            if swap_matching:
                return swap_matching[0]
            if matching:
                return matching[0]
            raise RuntimeError(
                f"Configured Robinhood prediction RHS account {configured_rhs} was not returned by Robinhood; "
                "refusing to choose another account."
            )
        swap_accounts = [account for account in accounts if str(account.get("accountType")) == "SWAP"]
        if swap_accounts:
            return swap_accounts[0]
        futures_accounts = [account for account in accounts if str(account.get("accountType")) == "FUTURES"]
        if futures_accounts:
            return futures_accounts[0]
        return accounts[0] if accounts else None

    async def list_event_positions(self, *, account_id: str, page_size: int = 200) -> list[dict[str, Any]]:
        payload = await self._get_json(
            f"/ceres/v1/accounts/{account_id}/positions",
            params={"contract_type": "EVENT_CONTRACT", "page_size": page_size},
        )
        return payload.get("results") or payload.get("positions") or []

    async def list_event_orders(self, *, account_id: str, page_size: int = 100) -> list[dict[str, Any]]:
        payload = await self._get_json(
            f"/ceres/v1/accounts/{account_id}/orders",
            params={"contract_type": "EVENT_CONTRACT", "page_size": page_size},
        )
        return payload.get("results") or payload.get("orders") or []

    async def get_event_order(self, *, account_id: str, order_id: str, page_size: int = 100) -> dict[str, Any] | None:
        orders = await self.list_event_orders(account_id=account_id, page_size=page_size)
        for order in orders:
            candidate_id = str(order.get("orderId") or order.get("id") or "")
            if candidate_id == order_id:
                return order
        return None

    async def list_cash_settlement_executions(self, *, account_id: str, page_size: int = 100) -> list[dict[str, Any]]:
        payload = await self._get_json(
            "/ceres/v1/cash_settlement_executions",
            params={"account_id": account_id, "page_size": page_size},
        )
        return payload.get("results") or payload.get("cashSettlementExecutions") or []

    async def get_catalog_snapshot(self, *, max_age_seconds: int = 120) -> RobinhoodCatalogSnapshot:
        now = datetime.now(UTC)
        if (
            self._catalog_cache is not None
            and self._catalog_cache_expires_at is not None
            and now < self._catalog_cache_expires_at
        ):
            return self._catalog_cache

        async with httpx.AsyncClient(timeout=45, headers={"User-Agent": "Mozilla/5.0"}) as client:
            response = await client.get(self.public_prediction_url, follow_redirects=True)
            response.raise_for_status()
            text = response.text

        match = _NEXT_DATA_PATTERN.search(text)
        if not match:
            raise RuntimeError("Robinhood prediction-markets page did not contain __NEXT_DATA__.")

        page_props = json.loads(match.group(1))["props"]["pageProps"]
        snapshot = RobinhoodCatalogSnapshot(
            events=page_props.get("events") or [],
            event_states=page_props.get("eventStates") or {},
            quotes=page_props.get("quotes") or {},
            fundamentals=page_props.get("fundamentals") or {},
            navigation_nodes=page_props.get("navigationNodes") or [],
            loaded_at=now,
        )
        self._catalog_cache = snapshot
        self._catalog_cache_expires_at = now + timedelta(seconds=max_age_seconds)
        return snapshot

    async def get_contract_state(self, *, event_ids: list[str]) -> dict[str, Any]:
        if not event_ids:
            return {"results": []}
        payload = await self._get_json(
            "/prediction-markets/v1/event_state",
            params={"event_ids": ",".join(event_ids)},
        )
        return payload

    async def get_marketdata_quotes(self, contract_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not contract_ids:
            return {}
        async def fetch(ids: list[str]) -> dict[str, dict[str, Any]]:
            payload = await self._get_json(
                "/marketdata/event/contract/quotes/v1/",
                params={"ids": ",".join(ids)},
            )
            rows = payload.get("data") or []
            mapped: dict[str, dict[str, Any]] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                data = row.get("data") if "data" in row else row
                if not isinstance(data, dict):
                    continue
                row_contract_id = str(data.get("instrument_id") or data.get("instrumentId") or "")
                if row_contract_id:
                    mapped[row_contract_id] = data
            return mapped

        mapped = await fetch(contract_ids)
        missing = [contract_id for contract_id in contract_ids if contract_id not in mapped]
        if missing:
            for contract_id in missing:
                try:
                    mapped.update(await fetch([contract_id]))
                except Exception:
                    continue
        return mapped

    async def get_marketdata_fundamentals(self, contract_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not contract_ids:
            return {}
        payload = await self._get_json(
            "/marketdata/event/contract/fundamentals/v1/",
            params={"ids": ",".join(contract_ids)},
        )
        rows = payload.get("data") or []
        mapped: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            data = row.get("data") if "data" in row else row
            if not isinstance(data, dict):
                continue
            contract_id = str(data.get("instrument_id") or data.get("instrumentId") or "")
            if contract_id:
                mapped[contract_id] = data
        return mapped

    def _format_limit_price(self, limit_price: float) -> str:
        return f"{float(limit_price):.3f}".rstrip("0").rstrip(".")

    def _format_decimal_string(self, value: float) -> str:
        if float(value).is_integer():
            return str(int(round(float(value))))
        return f"{float(value):.4f}".rstrip("0").rstrip(".")

    async def create_event_contract_order(
        self,
        *,
        contract_id: str,
        quantity: int,
        limit_price: float,
        ref_id: str | None = None,
        entered_notional_amount: float | None = None,
        order_side: str = "BUY",
    ) -> dict[str, Any]:
        write_session = await self._ensure_write_session()
        account = await self.get_event_contract_account()
        if not account:
            raise RuntimeError("No Robinhood event-contract account is available.")
        account_id = str(account.get("id") or "")
        if not account_id:
            raise RuntimeError("Robinhood event-contract account id is missing.")

        auth_device_token = str(write_session.device_token or self._configured_write_device_id() or "")
        if not auth_device_token:
            raise RuntimeError("Robinhood write device token is missing.")

        quotes = await self.get_marketdata_quotes([contract_id])
        quote = quotes.get(contract_id) or {}
        updated_at = quote.get("updated_at") or quote.get("updatedAt")
        estimated_notional = float(entered_notional_amount if entered_notional_amount is not None else quantity * limit_price)
        body: dict[str, Any] = {
            "accountId": account_id,
            "legs": [
                {
                    "contractType": "EVENT_CONTRACT",
                    "contractId": contract_id,
                    "ratioQuantity": 1,
                    "orderSide": str(order_side or "BUY").upper(),
                }
            ],
            "quantity": str(int(quantity)),
            "limitPrice": self._format_limit_price(limit_price),
            "refId": ref_id or str(uuid4()),
            "clientMarketdata": {
                "platform": "Android",
                "timestamp": {"value": str(updated_at)} if updated_at else None,
                "marketable": False,
            },
            "timeInForce": "IOC",
            "enteredNotionalAmount": {
                "amount": self._format_decimal_string(estimated_notional),
                "currency": "USD",
            },
        }
        body["clientMarketdata"] = {k: v for k, v in body["clientMarketdata"].items() if v is not None}

        headers = self.write_auth_headers(write_session)
        headers["X-Robinhood-Device-Id"] = auth_device_token
        headers["X-Device-Id"] = auth_device_token
        self._debug(
            "submit exact-app payload "
            f"device={auth_device_token} "
            f"limit={body['limitPrice']} qty={body['quantity']} "
            f"notional={body['enteredNotionalAmount']['amount']}"
        )
        async with httpx.AsyncClient(headers=headers, timeout=30) as client:
            response = await client.post(
                f"{self.api_base_url}/ceres/v1/event_contract_orders",
                json=body,
            )
            if response.status_code in {401, 403} and write_session.refresh_token:
                latest = self._load_write_session_file()
                if latest and (
                    latest.access_token != write_session.access_token
                    or latest.refresh_token != write_session.refresh_token
                ):
                    self._write_session_cache = latest
                    latest_headers = self.write_auth_headers(latest)
                    latest_headers["X-Robinhood-Device-Id"] = auth_device_token
                    latest_headers["X-Device-Id"] = auth_device_token
                    async with httpx.AsyncClient(headers=latest_headers, timeout=30) as latest_client:
                        response = await latest_client.post(
                            f"{self.api_base_url}/ceres/v1/event_contract_orders",
                            json=body,
                        )
                    write_session = latest
                if response.status_code in {401, 403} and write_session.refresh_token:
                    try:
                        refreshed = await self._refresh_write_session(write_session)
                    except RuntimeError as exc:
                        raise RuntimeError(
                            "Robinhood write auth expired. Re-run `PYTHONPATH=. .venv/bin/python scripts/robinhood_write_login.py`."
                        ) from exc
                    retry_headers = self.write_auth_headers(refreshed)
                    retry_headers["X-Robinhood-Device-Id"] = auth_device_token
                    retry_headers["X-Device-Id"] = auth_device_token
                    async with httpx.AsyncClient(headers=retry_headers, timeout=30) as retry_client:
                        response = await retry_client.post(
                            f"{self.api_base_url}/ceres/v1/event_contract_orders",
                            json=body,
                        )
            if response.status_code >= 400:
                self._debug(f"submit failed status={response.status_code} body={response.text[:400]}")
                logger.warning(
                    "Robinhood event order rejected status=%s contract_id=%s side=%s quantity=%s "
                    "limit_price=%s notional=%s response=%s",
                    response.status_code,
                    contract_id,
                    body.get("side"),
                    body.get("quantity"),
                    body.get("limitPrice"),
                    (body.get("enteredNotionalAmount") or {}).get("amount"),
                    response.text[:500],
                )
                raise RuntimeError(
                    f"Robinhood event order rejected ({response.status_code}): {response.text[:500]}"
                )
            payload = response.json()
            if isinstance(payload, dict):
                return payload
            return {"results": payload}
