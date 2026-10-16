"""Bounded, read-only Robinhood options MCP transport; never refreshes OAuth."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ENDPOINT = "https://agent.robinhood.com/mcp/trading"
CREDENTIALS_PATH = Path("/root/.codex/.credentials.json")
ALLOWED_TOOLS = frozenset({
    "get_accounts", "get_option_chains", "get_option_instruments", "get_option_quotes",
    "get_option_historicals", "get_option_positions", "get_option_level_upgrade_info",
})
SCOPED_TOOLS = frozenset({"get_option_positions", "get_option_level_upgrade_info"})
ARGUMENTS = {
    "get_accounts": set(),
    "get_option_chains": {"ids", "underlying_symbol"},
    "get_option_instruments": {
        "chain_id", "chain_symbol", "expiration_dates", "strike_price", "type", "state",
        "tradability", "ids", "cursor",
    },
    "get_option_quotes": {"instrument_ids"},
    "get_option_historicals": {"instrument_ids", "start_time", "end_time", "interval", "bounds"},
    "get_option_positions": {
        "account_number", "nonzero", "chain_ids", "option_ids", "type", "option_type",
        "expiration_date", "expiration_date_lte", "expiration_date_gte", "cursor",
    },
    "get_option_level_upgrade_info": {"account_number"},
}


class OptionsMCPError(RuntimeError):
    """Stable safe error; never includes credentials or upstream bodies."""


class OptionsMCPClient:
    """One bounded research session. call() returns one page's inner data object."""

    def __init__(
        self, account_number: str, credentials_path: str | Path = CREDENTIALS_PATH,
        *, transport: httpx.BaseTransport | None = None, timeout: float = 15.0,
        max_bytes: int = 2_000_000, max_pages: int = 4, max_requests: int = 32,
        total_timeout: float = 60.0,
    ) -> None:
        if not isinstance(account_number, str) or not re.fullmatch(r"[0-9]{6,20}", account_number):
            raise OptionsMCPError("explicit_account_required")
        if not (0 < timeout <= 30 and 0 < total_timeout <= 120):
            raise OptionsMCPError("invalid_limits")
        if not (0 < max_bytes <= 4_000_000 and 0 < max_pages <= 8 and 0 < max_requests <= 64):
            raise OptionsMCPError("invalid_limits")
        self.account_number = account_number
        self.credentials_path = Path(credentials_path)
        self._transport = transport
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._max_pages = max_pages
        self._max_requests = max_requests
        self._total_timeout = total_timeout
        self._client: httpx.Client | None = None
        self._deadline = 0.0
        self._request_id = 0
        self._metadata: dict[str, Any] | None = None
        self._page_counts: dict[str, int] = {}
        self._server_info: dict[str, Any] = {}

    def __enter__(self) -> OptionsMCPClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        self._metadata = None

    def _connect(self) -> None:
        if self._client is not None:
            return
        try:
            fd = os.open(self.credentials_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_uid not in {0, os.geteuid()}):
                    raise ValueError()
                raw = stream.read(128_001)
            if len(raw) > 128_000:
                raise ValueError()
            store = json.loads(raw)
            matches = [v for v in store.values() if isinstance(v, dict)
                       and v.get("server_url") == ENDPOINT]
            if len(matches) != 1:
                raise ValueError()
            credential = matches[0]
            token = credential["access_token"]
            expiry = credential["expires_at"]
            if (not isinstance(token, str) or not token or "\n" in token or "\r" in token
                    or isinstance(expiry, bool) or not isinstance(expiry, (int, float))
                    or not math.isfinite(expiry) or expiry <= (time.time() + 5) * 1000):
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
            raise OptionsMCPError("credentials_unavailable_or_expired") from None
        self._deadline = time.monotonic() + self._total_timeout
        self._client = httpx.Client(
            transport=self._transport, follow_redirects=False, trust_env=False,
            timeout=self._timeout,
            headers={"Authorization": "Bearer " + token,
                     "Accept": "application/json, text/event-stream"},
        )
        try:
            result = self._rpc("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "finn-options-readonly", "version": "1"},
            })
            version = result.get("protocolVersion")
            info = result.get("serverInfo", {})
            if isinstance(info, dict):
                self._server_info = {
                    k: info[k] for k in ("name", "version") if isinstance(info.get(k), str)
                }
            if version != "2024-11-05":
                raise OptionsMCPError("unsupported_protocol")
            self._client.headers["MCP-Protocol-Version"] = version
            self._rpc("notifications/initialized", None, notification=True)
        except OptionsMCPError:
            self.close()
            raise

    def _rpc(
        self, method: str, params: dict[str, Any] | None, *, notification: bool = False,
    ) -> dict[str, Any]:
        remaining = self._deadline - time.monotonic()
        if self._request_id >= self._max_requests or remaining <= 0:
            raise OptionsMCPError("request_budget_exhausted")
        self._request_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notification:
            payload["id"] = self._request_id
        if params is not None:
            payload["params"] = params
        assert self._client is not None
        try:
            with self._client.stream(
                "POST", ENDPOINT, json=payload, timeout=min(self._timeout, remaining),
            ) as response:
                if response.status_code in (401, 403):
                    raise OptionsMCPError("authentication_required")
                if not 200 <= response.status_code < 300:
                    raise OptionsMCPError("transport_http_error")
                session = response.headers.get("mcp-session-id")
                if session:
                    self._client.headers["Mcp-Session-Id"] = session
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() >= self._deadline:
                        raise OptionsMCPError("request_budget_exhausted")
                    raw.extend(chunk)
                    if len(raw) > self._max_bytes:
                        raise OptionsMCPError("response_too_large")
                if notification:
                    return {}
                body = raw.decode("utf-8")
                if "text/event-stream" in response.headers.get("content-type", ""):
                    messages = []
                    for event in body.replace("\r\n", "\n").split("\n\n"):
                        data = "\n".join(
                            line[5:].lstrip() for line in event.splitlines()
                            if line.startswith("data:")
                        )
                        if data:
                            messages.append(json.loads(data))
                else:
                    messages = [json.loads(body)]
                matches = [m for m in messages if isinstance(m, dict)
                           and m.get("id") == self._request_id]
                if len(matches) != 1 or matches[0].get("jsonrpc") != "2.0":
                    raise OptionsMCPError("invalid_rpc_response")
                message = matches[0]
                if "error" in message:
                    raise OptionsMCPError("remote_rpc_error")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise OptionsMCPError("invalid_rpc_response")
                return result
        except (httpx.HTTPError, UnicodeError, ValueError):
            raise OptionsMCPError("transport_or_decode_error") from None

    def discover(self) -> dict[str, Any]:
        self._connect()
        if self._metadata is not None:
            return self._metadata
        tools: dict[str, dict[str, Any]] = {}
        catalog_tools: dict[str, dict[str, Any]] = {}
        params: dict[str, Any] = {}
        seen: set[str] = set()
        for _ in range(self._max_pages):
            result = self._rpc("tools/list", params)
            catalog = result.get("tools")
            if not isinstance(catalog, list):
                raise OptionsMCPError("invalid_tool_catalog")
            for tool in catalog:
                if not isinstance(tool, dict):
                    raise OptionsMCPError("invalid_tool_catalog")
                name = tool.get("name")
                if (not isinstance(name, str) or name in catalog_tools
                        or not isinstance(tool.get("inputSchema"), dict)):
                    raise OptionsMCPError("invalid_tool_catalog")
                catalog_tools[name] = {k: tool[k] for k in ("inputSchema", "outputSchema")
                                       if k in tool}
                if name in ALLOWED_TOOLS:
                    annotations = tool.get("annotations")
                    if (not isinstance(annotations, dict)
                            or annotations.get("readOnlyHint") is not True):
                        raise OptionsMCPError("tool_not_readonly")
                    tools[name] = catalog_tools[name]
            cursor = result.get("nextCursor")
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise OptionsMCPError("invalid_catalog_cursor")
            seen.add(cursor)
            params = {"cursor": cursor}
        else:
            raise OptionsMCPError("catalog_page_limit")
        canonical = json.dumps(catalog_tools, sort_keys=True, separators=(",", ":")).encode()
        self._metadata = {
            "tools": tools, "schemas": {n: t["inputSchema"] for n, t in tools.items()},
            "schema_fingerprint": hashlib.sha256(canonical).hexdigest(),
            "as_of": datetime.now(timezone.utc).isoformat(),
            "server_info": dict(self._server_info),
            "server_name": self._server_info.get("name"),
            "server_version": self._server_info.get("version"),
            "tool_count": len(catalog_tools), "catalog_tools": sorted(catalog_tools),
            "catalog_complete": True, "fingerprint_scope": "full_catalog_input_output_schemas",
        }
        return self._metadata

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in ALLOWED_TOOLS:
            raise OptionsMCPError("tool_not_allowed")
        if not isinstance(arguments, dict) or set(arguments) - ARGUMENTS[name]:
            raise OptionsMCPError("invalid_arguments")
        args = dict(arguments)
        if name in SCOPED_TOOLS:
            if args.get("account_number", self.account_number) != self.account_number:
                raise OptionsMCPError("account_mismatch")
            args["account_number"] = self.account_number
        for key, value in args.items():
            if key == "nonzero":
                valid = isinstance(value, bool)
            elif key == "instrument_ids":
                cap = 10 if name == "get_option_historicals" else 20
                valid = (isinstance(value, list) and 0 < len(value) <= cap
                         and all(isinstance(v, str) and 0 < len(v) <= 128 for v in value))
            else:
                valid = isinstance(value, str) and 0 < len(value) <= 4096
            if not valid:
                raise OptionsMCPError("invalid_arguments")
        if name in {"get_option_quotes", "get_option_historicals"}:
            if "instrument_ids" not in args:
                raise OptionsMCPError("invalid_arguments")
        if name == "get_option_historicals" and "start_time" not in args:
            raise OptionsMCPError("invalid_arguments")
        if name == "get_option_chains" and not (args.get("ids") or args.get("underlying_symbol")):
            raise OptionsMCPError("invalid_arguments")
        if name == "get_option_instruments" and not any(
            args.get(k) for k in ("chain_id", "chain_symbol", "ids")
        ):
            raise OptionsMCPError("invalid_arguments")
        count = self._page_counts.get(name, 0)
        if name in {"get_option_instruments", "get_option_positions"} and count >= self._max_pages:
            raise OptionsMCPError("tool_page_limit")
        metadata = self.discover()
        tool = metadata["tools"].get(name)
        if tool is None:
            raise OptionsMCPError("tool_unavailable")
        schema = tool["inputSchema"]
        if set(schema.get("required", [])) - set(args):
            raise OptionsMCPError("schema_required_argument")
        if schema.get("additionalProperties") is False:
            if set(args) - set(schema.get("properties", {})):
                raise OptionsMCPError("schema_argument_changed")
        self._page_counts[name] = count + 1
        result = self._rpc("tools/call", {"name": name, "arguments": args})
        if result.get("isError"):
            raise OptionsMCPError("remote_tool_error")
        decoded = result.get("structuredContent")
        if decoded is None:
            content = result.get("content", [])
            if not isinstance(content, list):
                raise OptionsMCPError("invalid_tool_result")
            texts = [c.get("text") for c in content if isinstance(c, dict)
                     and c.get("type") == "text"]
            if len(texts) != 1 or not isinstance(texts[0], str):
                raise OptionsMCPError("invalid_tool_result")
            try:
                decoded = json.loads(texts[0])
            except ValueError:
                raise OptionsMCPError("invalid_tool_result") from None
        if not isinstance(decoded, dict) or not isinstance(decoded.get("data"), dict):
            raise OptionsMCPError("invalid_tool_result")
        data = decoded["data"]
        for envelope in (decoded, data):
            if (envelope.get("isError") or envelope.get("error")
                    or envelope.get("success") is False or envelope.get("status") == "error"):
                raise OptionsMCPError("remote_tool_error")
        if name in SCOPED_TOOLS and data.get("account_number", self.account_number) != (
            self.account_number
        ):
            raise OptionsMCPError("account_response_mismatch")
        return data
