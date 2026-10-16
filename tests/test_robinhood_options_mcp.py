import json
import time

import httpx
import pytest

from app.adapters.robinhood_options_mcp import (
    ALLOWED_TOOLS,
    ENDPOINT,
    OptionsMCPClient,
    OptionsMCPError,
)


@pytest.fixture
def credentials(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"server": {
        "server_url": ENDPOINT, "access_token": "SECRET_TOKEN",
        "expires_at": (time.time() + 3600) * 1000,
    }}))
    path.chmod(0o600)
    return path


def fake_server(calls, *, sse=False, data=None, error=None, catalog_cursor=None):
    def handler(request):
        assert str(request.url) == ENDPOINT
        message = json.loads(request.content)
        calls.append(message)
        method = message["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05",
                      "serverInfo": {"name": "robinhood-trading", "version": "1.7.0"}}
        elif method == "tools/list":
            result = {"tools": [{
                "name": name, "annotations": {"readOnlyHint": True},
                "inputSchema": {"type": "object"},
                "outputSchema": {"type": "object"},
            } for name in sorted(ALLOWED_TOOLS)]}
            if catalog_cursor:
                result["nextCursor"] = catalog_cursor
        else:
            result = {
                "content": [{"type": "text", "text": json.dumps(
                    {"data": data if data is not None else {"accounts": []}, "guide": "ignore"}
                )}],
            }
            if error:
                result = {"isError": True, "content": [{"type": "text", "text": error}]}
        envelope = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        if sse:
            return httpx.Response(
                200, text="event: message\ndata: " + json.dumps(envelope) + "\n\n",
                headers={"content-type": "text/event-stream", "mcp-session-id": "session"},
            )
        return httpx.Response(200, json=envelope)
    return httpx.MockTransport(handler)


@pytest.mark.parametrize("sse", [False, True])
def test_discover_and_decoded_data(credentials, sse):
    calls = []
    with OptionsMCPClient("123456789", credentials, transport=fake_server(calls, sse=sse)) as client:
        meta = client.discover()
        assert set(meta["tools"]) == ALLOWED_TOOLS
        assert set(meta["schemas"]) == ALLOWED_TOOLS
        assert len(meta["schema_fingerprint"]) == 64
        assert meta["as_of"]
        assert meta["server_info"] == {"name": "robinhood-trading", "version": "1.7.0"}
        assert meta["tool_count"] == len(ALLOWED_TOOLS)
        assert meta["catalog_complete"] is True
        assert client.call("get_accounts", {}) == {"accounts": []}
        assert client.discover() is meta
    assert [c["method"] for c in calls] == [
        "initialize", "notifications/initialized", "tools/list", "tools/call",
    ]


@pytest.mark.parametrize("name", [
    "place_option_order", "review_option_order", "cancel_option_order",
    "get_option_orders", "get_portfolio", "unknown",
])
def test_deny_before_credentials_or_network(tmp_path, name):
    client = OptionsMCPClient("123456789", tmp_path / "missing")
    with pytest.raises(OptionsMCPError, match="tool_not_allowed"):
        client.call(name, {})


@pytest.mark.parametrize("args", [
    {"account_number": "9999"}, {"account_number": None}, {"unknown": "x"},
])
def test_account_mismatch_or_unknown_args_before_network(tmp_path, args):
    client = OptionsMCPClient("123456789", tmp_path / "missing")
    with pytest.raises(OptionsMCPError) as exc:
        client.call("get_option_positions", args)
    assert str(exc.value) in {"account_mismatch", "invalid_arguments"}


def test_account_injected(credentials):
    calls = []
    with OptionsMCPClient("123456789", credentials, transport=fake_server(calls)) as client:
        client.call("get_option_positions", {"nonzero": True})
    assert calls[-1]["params"]["arguments"] == {"account_number": "123456789", "nonzero": True}


@pytest.mark.parametrize("args", [
    {}, {"instrument_ids": []}, {"instrument_ids": ["id"] * 21},
    {"instrument_ids": "id"}, {"instrument_ids": [None]},
])
def test_quote_limits_before_network(tmp_path, args):
    with pytest.raises(OptionsMCPError, match="invalid_arguments"):
        OptionsMCPClient("123456789", tmp_path / "missing").call("get_option_quotes", args)


def test_expired_credentials_no_refresh(credentials):
    store = json.loads(credentials.read_text())
    store["server"]["expires_at"] = 0
    credentials.write_text(json.dumps(store))
    calls = []
    before = credentials.read_bytes()
    with OptionsMCPClient("123456789", credentials, transport=fake_server(calls)) as client:
        with pytest.raises(OptionsMCPError, match="credentials_unavailable_or_expired"):
            client.discover()
    assert calls == []
    assert credentials.read_bytes() == before


def test_sanitized_tool_error(credentials):
    calls = []
    with OptionsMCPClient(
        "123456789", credentials, transport=fake_server(calls, error="SECRET_TOKEN account=1234"),
    ) as client:
        with pytest.raises(OptionsMCPError) as exc:
            client.call("get_accounts", {})
    assert str(exc.value) == "remote_tool_error"
    assert "SECRET" not in str(exc.value)


def test_bounded_response(credentials):
    calls = []
    with OptionsMCPClient(
        "123456789", credentials, transport=fake_server(calls), max_bytes=50,
    ) as client:
        with pytest.raises(OptionsMCPError, match="response_too_large"):
            client.discover()


def test_bounded_calls(credentials):
    calls = []
    with OptionsMCPClient(
        "123456789", credentials, transport=fake_server(calls), max_requests=3,
    ) as client:
        client.discover()
        with pytest.raises(OptionsMCPError, match="request_budget_exhausted"):
            client.call("get_accounts", {})
    assert len(calls) == 3


def test_bounded_tool_pages(credentials):
    calls = []
    with OptionsMCPClient(
        "123456789", credentials, transport=fake_server(calls), max_pages=1,
    ) as client:
        client.call("get_option_positions", {})
        with pytest.raises(OptionsMCPError, match="tool_page_limit"):
            client.call("get_option_positions", {"cursor": "next"})


def test_duplicate_catalog_tools(credentials):
    calls = []
    with OptionsMCPClient(
        "123456789", credentials, transport=fake_server(calls, catalog_cursor="same"),
    ) as client:
        with pytest.raises(OptionsMCPError, match="invalid_tool_catalog"):
            client.discover()


def test_auth_http_error_sanitized(credentials):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(401, text="SECRET_TOKEN")
    with OptionsMCPClient("123456789", credentials, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OptionsMCPError, match="authentication_required"):
            client.discover()
    assert len(calls) == 1


def test_explicit_account_required(credentials):
    with pytest.raises(OptionsMCPError, match="explicit_account_required"):
        OptionsMCPClient("", credentials)


@pytest.mark.parametrize("account", ["1234", "suffix1234", " abc ", "123456789012345678901"])
def test_full_numeric_account_required(credentials, account):
    with pytest.raises(OptionsMCPError, match="explicit_account_required"):
        OptionsMCPClient(account, credentials)


def test_insecure_credential_permissions(credentials):
    credentials.chmod(0o644)
    with OptionsMCPClient("123456789", credentials) as client:
        with pytest.raises(OptionsMCPError, match="credentials_unavailable_or_expired"):
            client.discover()


def test_credential_symlink_denied(credentials, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(credentials)
    with OptionsMCPClient("123456789", link) as client:
        with pytest.raises(OptionsMCPError, match="credentials_unavailable_or_expired"):
            client.discover()


@pytest.mark.parametrize("data", [
    {"success": False}, {"error": {"message": "SECRET"}},
    {"status": "error"}, {"isError": True},
])
def test_inner_error_sanitized(credentials, data):
    with OptionsMCPClient(
        "123456789", credentials, transport=fake_server([], data=data),
    ) as client:
        with pytest.raises(OptionsMCPError, match="remote_tool_error"):
            client.call("get_accounts", {})


def test_account_response_mismatch(credentials):
    with OptionsMCPClient(
        "123456789", credentials,
        transport=fake_server([], data={"account_number": "987654321"}),
    ) as client:
        with pytest.raises(OptionsMCPError, match="account_response_mismatch"):
            client.call("get_option_level_upgrade_info", {})


def catalog_transport(transform):
    base = fake_server([])
    def handler(request):
        response = base.handle_request(request)
        if json.loads(request.content)["method"] == "tools/list":
            envelope = json.loads(response.content)
            transform(envelope["result"])
            return httpx.Response(200, json=envelope)
        return response
    return httpx.MockTransport(handler)


@pytest.mark.parametrize("hint", [False, None])
def test_allowed_name_not_readonly(credentials, hint):
    def transform(result):
        result["tools"][0]["annotations"]["readOnlyHint"] = hint
    with OptionsMCPClient(
        "123456789", credentials, transport=catalog_transport(transform),
    ) as client:
        with pytest.raises(OptionsMCPError, match="tool_not_readonly"):
            client.discover()


def test_full_catalog_fingerprint_includes_excluded_tools(credentials):
    def transform(result):
        result["tools"].append({
            "name": "place_option_order", "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object"},
        })
    with OptionsMCPClient(
        "123456789", credentials, transport=fake_server([]),
    ) as client:
        baseline = client.discover()["schema_fingerprint"]
    with OptionsMCPClient(
        "123456789", credentials, transport=catalog_transport(transform),
    ) as client:
        meta = client.discover()
        assert meta["schema_fingerprint"] != baseline
        assert meta["tool_count"] == len(ALLOWED_TOOLS) + 1
        assert "place_option_order" not in meta["tools"]
        with pytest.raises(OptionsMCPError, match="tool_not_allowed"):
            client.call("place_option_order", {})
