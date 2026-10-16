from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path

import pytest

from app.reports.options_readiness import build_report


NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
ACCOUNT = "123456789"
SECRET = "never-output-access-token"


class Client:
    account_number = ACCOUNT

    def __init__(self, responses=None, discovery_error=False):
        self.calls = []
        self.responses = responses or {}
        self.discovery_error = discovery_error

    def discover(self):
        if self.discovery_error:
            raise RuntimeError(SECRET)
        return {
            "schemas": {}, "schema_fingerprint": "a" * 64, "as_of": NOW.isoformat(),
            "catalog_complete": True,
        }

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        value = self.responses.get(name, {})
        if isinstance(value, Exception):
            raise value
        return value


def symbol_client(stamp=NOW.isoformat(), count=3):
    return Client({
        "get_accounts": {"accounts": [{
            "account_number": ACCOUNT, "option_level": "option_level_2", "user_option_level": 2,
            "agentic_allowed": True, "deactivated": False, "access_token": SECRET,
        }]},
        "get_option_level_upgrade_info": {},
        "get_option_positions": {"positions": [{"account_number": ACCOUNT}], "next": SECRET},
        "get_option_chains": {"chains": [{
            "id": "chain-1", "symbol": "AAPL", "expiration_dates": ["2026-10-16"],
        }]},
        "get_option_instruments": {"instruments": [{
            "id": "option-1", "chain_id": "chain-1", "strike_price": "100",
            "expiration_date": "2026-10-16",
            "type": "call", "account_number": ACCOUNT, "private": SECRET,
        }] * count, "next": SECRET},
        "get_option_quotes": {"results": [{"quote": {
            "instrument_id": "option-1", "bid_price": "1.20", "ask_price": "1.40",
            "delta": "0.4", "gamma": "0.05", "updated_at": stamp,
            "account_number": ACCOUNT, "raw": SECRET,
        }, "close": {"raw": SECRET}}]},
    })


def test_empty_approval_needs_user_action_and_never_execution():
    report = build_report(symbol_client(), now=NOW)
    assert report["approval"] == "requires_user_confirmation"
    assert report["user_action_required"] is True
    assert report["execution_allowed"] is False
    assert report["execution_status"] == "blocked"
    assert report["account_found"] is True
    assert report["account_readiness"]["option_level"] == 2
    assert report["positions_count"] == 1
    assert report["positions_truncated"] is True
    assert report["research"] is None


def test_bound_symbol_reads_and_sanitize_projection():
    client = symbol_client()
    report = build_report(client, "aapl", max_instruments=2, now=NOW)
    serialized = json.dumps(report)
    assert SECRET not in serialized
    assert ACCOUNT not in serialized
    assert report["account_suffix"] == "6789"
    research = report["research"]
    assert research["symbol"] == "AAPL"
    assert len(research["instruments"]) == 2
    assert research["instruments_truncated"] is True
    quote = research["instruments"][0]["quote"]
    assert quote["bid_price"] == 1.2
    assert quote["delta"] == 0.4
    assert quote["freshness"]["status"] == "fresh"
    assert quote["greeks_freshness"]["status"] == "fresh"
    assert quote["fetched_at"]
    assert ("get_option_chains", {"underlying_symbol": "AAPL"}) in client.calls
    assert ("get_option_quotes", {"instrument_ids": ["option-1"]}) in client.calls
    assert all(not any(word in name for word in ("order", "review", "write"))
               for name, _ in client.calls)
    assert len(client.calls) == 7


@pytest.mark.parametrize("stamp,status", [
    ((NOW - timedelta(minutes=6)).isoformat(), "stale"),
    ((NOW + timedelta(minutes=6)).isoformat(), "future"),
    ((NOW + timedelta(microseconds=1)).isoformat(), "future"),
    (None, "unknown"), (SECRET, "unknown"), ("2026-10-07T12:00:00", "unknown"),
])
def test_quote_and_greek_freshness(stamp, status):
    report = build_report(symbol_client(stamp), "AAPL", now=NOW)
    quote = report["research"]["instruments"][0]["quote"]
    assert quote["freshness"]["status"] == status
    assert quote["greeks_freshness"]["status"] == status
    assert SECRET not in json.dumps(report)


def test_error_details_never_exposed():
    client = Client({"get_accounts": RuntimeError(f"{SECRET} {ACCOUNT}")})
    report = build_report(client, now=NOW)
    assert report["checks"]["get_accounts"] == "unavailable"
    assert report["account_found"] is False
    assert SECRET not in json.dumps(report)
    assert ACCOUNT not in json.dumps(report)
    discovery_client = Client(discovery_error=True)
    report = build_report(discovery_client, now=NOW)
    assert report["execution_allowed"] is False
    assert discovery_client.calls == []
    assert SECRET not in json.dumps(report)


@pytest.mark.parametrize("kwargs", [
    {"symbol": "../../secret"}, {"max_instruments": 0}, {"max_instruments": 21},
    {"max_age_seconds": 0}, {"max_age_seconds": 86401},
])
def test_invalid_inputs_do_not_touch_provider(kwargs):
    client = Client()
    with pytest.raises(ValueError):
        build_report(client, **kwargs)
    assert client.calls == []


def test_mismatched_quote_and_invalid_instrument_not_emitted():
    client = symbol_client(count=1)
    client.responses["get_option_quotes"]["results"][0]["quote"]["instrument_id"] = "other"
    assert build_report(client, "AAPL", now=NOW)["research"]["instruments"][0]["quote"] is None
    client.responses["get_option_instruments"]["instruments"][0]["id"] = "https://secret"
    client.calls.clear()
    report = build_report(client, "AAPL", now=NOW)
    assert report["research"]["instruments"] == []
    assert not any(name == "get_option_quotes" for name, _ in client.calls)


def test_nonempty_upgrade_is_not_approval():
    client = symbol_client()
    client.responses["get_option_level_upgrade_info"] = {"upgrade_url": SECRET}
    report = build_report(client, now=NOW)
    assert report["approval"] == "requires_user_confirmation"
    assert report["execution_allowed"] is False
    assert report["user_action_required"] is True
    assert SECRET not in json.dumps(report)


@pytest.mark.parametrize("matches", [0, 2])
def test_exact_single_account_required_for_scoped_reads(matches):
    client = symbol_client(count=1)
    selected = client.responses["get_accounts"]["accounts"][0]
    client.responses["get_accounts"]["accounts"] = [selected] * matches + [{
        "account_number": "OTHER1234", "option_level": "option_level_3", "agentic_allowed": True,
    }]
    report = build_report(client, "AAPL", now=NOW)
    assert report["account_found"] is False
    assert report["account_readiness_status"] == "blocked"
    assert "account_readiness" not in report
    assert not any(name in ("get_option_positions", "get_option_level_upgrade_info")
                   for name, _ in client.calls)
    assert report["research"]["instruments"]


def test_trade_access_false_or_unknown_blocks_readiness():
    for access in (False, None, "true"):
        client = symbol_client()
        account = client.responses["get_accounts"]["accounts"][0]
        account.update({"agentic_allowed": access, "state": "active",
                        "deactivated": False, "permanently_deactivated": False})
        report = build_report(client, now=NOW)
        assert report["account_readiness_status"] == "blocked"
        assert report["execution_allowed"] is False


def test_missing_nonfinite_greeks_are_unknown_not_zero():
    client = symbol_client(count=1)
    quote = client.responses["get_option_quotes"]["results"][0]["quote"]
    quote.update({"delta": "NaN", "gamma": "Infinity", "theta": None})
    report = build_report(client, "AAPL", now=NOW)
    quote = report["research"]["instruments"][0]["quote"]
    assert quote["delta"] is None and quote["gamma"] is None
    assert quote["greeks_freshness"]["status"] == "unknown"


def test_single_upcoming_expiry_and_no_cursor_follow():
    client = symbol_client()
    client.responses["get_option_chains"]["chains"][0]["expiration_dates"] = [
        "2026-10-30", "2026-09-01", "2026-10-16",
    ]
    report = build_report(client, "AAPL", now=NOW)
    assert report["research"]["expiration_date"] == "2026-10-16"
    arguments = [args for name, args in client.calls if name == "get_option_instruments"]
    assert len(arguments) == 1
    assert arguments[0]["expiration_dates"] == "2026-10-16"
    assert all("cursor" not in args for _, args in client.calls)


def _cli_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "print_options_readiness.py"
    spec = importlib.util.spec_from_file_location("print_options_readiness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_config_failure_has_no_network(monkeypatch, capsys):
    cli = _cli_module()

    monkeypatch.setattr("sys.argv", ["print_options_readiness"])
    monkeypatch.setattr(cli, "load_account_number", lambda: (_ for _ in ()).throw(ValueError(SECRET)))
    monkeypatch.setattr(cli, "OptionsMCPClient", lambda *_: pytest.fail("network on config failure"))
    assert cli.main() == 1
    output = capsys.readouterr().out
    assert SECRET not in output
    assert json.loads(output)["execution_allowed"] is False


@pytest.mark.parametrize("failure", [None, "positions", "accounts", "approval"])
def test_cli_exit_status_critical_checks(monkeypatch, capsys, failure):
    cli = _cli_module()

    client = symbol_client()
    if failure == "accounts":
        client.responses["get_accounts"] = {"accounts": []}
    elif failure == "positions":
        client.responses["get_option_positions"] = RuntimeError(SECRET)
    elif failure == "approval":
        client.responses["get_option_level_upgrade_info"] = RuntimeError(SECRET)
    monkeypatch.setattr("sys.argv", ["print_options_readiness"])
    monkeypatch.setattr(cli, "load_account_number", lambda: ACCOUNT)
    monkeypatch.setattr(cli, "OptionsMCPClient", lambda *_: client)
    assert cli.main() == (0 if failure is None else 1)
    output = capsys.readouterr().out
    assert SECRET not in output and ACCOUNT not in output
    assert json.loads(output)["execution_allowed"] is False


def test_upgrade_link_never_substitutes_missing_approval():
    client = symbol_client()
    client.responses["get_accounts"]["accounts"][0]["option_level"] = None
    client.responses["get_option_level_upgrade_info"] = {"upgrade_url": SECRET}
    report = build_report(client, now=NOW)
    assert report["approval"] == "needs_user_action"
    assert report["observed_options_level"] is None
    assert report["user_action_required"] is True


def test_cli_requested_symbol_failure_is_nonzero(monkeypatch, capsys):
    cli = _cli_module()
    client = symbol_client()
    client.responses["get_option_quotes"] = RuntimeError(SECRET)
    monkeypatch.setattr("sys.argv", ["print_options_readiness", "--symbol", "AAPL"])
    monkeypatch.setattr(cli, "load_account_number", lambda: ACCOUNT)
    monkeypatch.setattr(cli, "OptionsMCPClient", lambda *_: client)
    assert cli.main() == 1
    output = capsys.readouterr().out
    assert SECRET not in output and ACCOUNT not in output
    assert json.loads(output)["research"]["status"] == "partial"


def test_malformed_positions_are_unknown_not_counted():
    client = symbol_client()
    client.responses["get_option_positions"] = {"error": SECRET}
    report = build_report(client, now=NOW)
    assert report["positions_count"] is None
    assert report["positions_truncated"] is None
    assert report["checks"]["get_option_positions"] == "unavailable"
    assert SECRET not in json.dumps(report)


def test_close_never_substitutes_quote():
    client = symbol_client(count=1)
    client.responses["get_option_quotes"] = {"results": [{
        "close": {"instrument_id": "option-1", "price": "123", "date": "2026-10-07"},
    }]}
    report = build_report(client, "AAPL", now=NOW)
    assert report["research"]["instruments"][0]["quote"] is None
    assert report["research"]["status"] == "partial"


def test_empty_nearest_expiry_falls_back_once_and_stops():
    client = symbol_client(count=1)
    client.responses["get_option_chains"]["chains"][0]["expiration_dates"] = [
        "2026-10-08", "2026-10-09", "2026-10-16", "2026-10-30",
    ]
    original_call = client.call

    def call(name, arguments):
        if name == "get_option_instruments" and arguments["expiration_dates"] == "2026-10-08":
            client.calls.append((name, arguments))
            return {"instruments": []}
        if name == "get_option_instruments":
            client.responses[name]["instruments"][0]["expiration_date"] = arguments[
                "expiration_dates"
            ]
        return original_call(name, arguments)

    client.call = call
    report = build_report(client, "AAPL", now=NOW)
    research = report["research"]
    assert research["expirations_examined"] == ["2026-10-08", "2026-10-09"]
    assert research["expiration_date"] == "2026-10-09"
    assert research["expirations_truncated"] is True
    assert research["instruments"]
    assert len([name for name, _ in client.calls if name == "get_option_instruments"]) == 2


def test_expiry_fallback_is_bounded_to_three_pages():
    client = symbol_client()
    client.responses["get_option_chains"]["chains"][0]["expiration_dates"] = [
        "2026-10-08", "2026-10-09", "2026-10-16", "2026-10-30",
    ]
    client.responses["get_option_instruments"] = {"instruments": []}
    report = build_report(client, "AAPL", now=NOW)
    research = report["research"]
    assert research["expirations_examined"] == ["2026-10-08", "2026-10-09", "2026-10-16"]
    assert research["expirations_truncated"] is True
    assert research["status"] == "no_instruments"
    assert research["instruments"] == []
    assert len([name for name, _ in client.calls if name == "get_option_instruments"]) == 3
    assert not any(name == "get_option_quotes" for name, _ in client.calls)


@pytest.mark.parametrize("field,value", [
    ("chain_id", "other-chain"), ("expiration_date", "2026-10-30"),
])
def test_mismatched_instrument_not_quoted(field, value):
    client = symbol_client(count=1)
    client.responses["get_option_instruments"]["instruments"][0][field] = value
    report = build_report(client, "AAPL", now=NOW)
    assert report["research"]["instruments"] == []
    assert report["research"]["status"] == "partial"
    assert not any(name == "get_option_quotes" for name, _ in client.calls)


def test_mismatched_chain_symbol_not_used():
    client = symbol_client()
    client.responses["get_option_chains"]["chains"][0]["symbol"] = "SPY"
    report = build_report(client, "AAPL", now=NOW)
    assert report["research"]["status"] == "mismatched_chain"
    assert not any(name in ("get_option_instruments", "get_option_quotes")
                   for name, _ in client.calls)
