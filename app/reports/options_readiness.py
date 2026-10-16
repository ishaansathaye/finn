"""Sanitized, research-only options readiness; never an execution authorization."""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re


READ_TOOLS = frozenset({
    "get_accounts", "get_option_chains", "get_option_instruments", "get_option_quotes",
    "get_option_historicals", "get_option_positions", "get_option_level_upgrade_info",
})


def _rows(data):
    if not isinstance(data, dict):
        return []
    for key in ("results", "accounts", "chains", "instruments", "quotes", "positions"):
        value = data.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return [data] if data else []


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _level(value):
    if isinstance(value, str) and re.fullmatch(r"option_level_[0-3]", value):
        return int(value[-1])
    number = _number(value)
    return int(number) if not isinstance(value, bool) and number in (0, 1, 2, 3) else None


def _freshness(value, now, max_age_seconds):
    if not isinstance(value, str):
        return {"status": "unknown", "as_of": None}
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return {"status": "unknown", "as_of": None}
        age = (now - stamp).total_seconds()
        status = "future" if age < 0 else "fresh" if age <= max_age_seconds else "stale"
        return {"status": status, "as_of": stamp.isoformat()}
    except ValueError:
        return {"status": "unknown", "as_of": None}


def build_report(client, symbol=None, *, max_instruments=5, max_age_seconds=300, now=None):
    """Return a bounded safe projection. Empty or unknown approval requires user action."""
    if not 1 <= max_instruments <= 20 or not 1 <= max_age_seconds <= 86400:
        raise ValueError("invalid research bounds")
    if symbol is not None:
        symbol = symbol.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", symbol):
            raise ValueError("invalid symbol")
    reference_now = now
    now = now or datetime.now(timezone.utc)
    account = str(client.account_number)
    report = {
        "mode": "read_only_research", "execution_allowed": False,
        "execution_status": "blocked", "generated_at": now.isoformat(),
        "account_suffix": account[-4:], "approval": "unknown",
        "user_action_required": True, "checks": {}, "research": None,
        "user_action": "Confirm options approval and account trade access in Robinhood; "
                       "this report cannot authorize execution.",
    }
    try:
        metadata = client.discover()
    except Exception:
        report["checks"]["discovery"] = "unavailable"
        return report
    if not isinstance(metadata, dict):
        report["checks"]["discovery"] = "unavailable"
        return report
    report["checks"]["discovery"] = "available"
    fingerprint = metadata.get("schema_fingerprint", "")
    report["schema_fingerprint"] = (
        fingerprint if isinstance(fingerprint, str) and re.fullmatch(r"[a-f0-9]{64}", fingerprint)
        else None
    )
    report["catalog_freshness"] = _freshness(
        metadata.get("as_of"), reference_now or datetime.now(timezone.utc), max_age_seconds,
    )
    report["catalog_complete"] = metadata.get("catalog_complete") is True
    catalog_tools = metadata.get("tools", {})
    report["available_read_tools"] = sorted(READ_TOOLS.intersection(
        catalog_tools if isinstance(catalog_tools, dict) else {},
    ))
    for key in ("server_name", "server_version"):
        value = metadata.get(key)
        report[key] = value if isinstance(value, str) and re.fullmatch(
            r"[A-Za-z0-9._-]{1,64}", value,
        ) else None
    count = metadata.get("tool_count")
    report["tool_count"] = count if type(count) is int and 0 <= count <= 10000 else None
    report["fetched_at"] = {}

    def read(name, arguments):
        try:
            data = client.call(name, arguments)
            if not isinstance(data, dict):
                raise ValueError("invalid provider result")
            list_key = {
                "get_accounts": "accounts", "get_option_positions": "positions",
                "get_option_chains": "chains", "get_option_instruments": "instruments",
                "get_option_quotes": "results",
            }.get(name)
            if list_key is not None and (
                not isinstance(data.get(list_key), list)
                or any(not isinstance(row, dict) for row in data[list_key])
            ):
                raise ValueError("invalid provider rows")
            if report["checks"].get(name) != "unavailable":
                report["checks"][name] = "available" if data else "empty"
            report["fetched_at"][name] = datetime.now(timezone.utc).isoformat()
            return data
        except Exception:
            report["checks"][name] = "unavailable"
            return {}

    accounts = _rows(read("get_accounts", {}))
    selected = [row for row in accounts if str(row.get("account_number", "")) == account]
    report["account_found"] = len(selected) == 1
    report["account_match_status"] = (
        "matched" if len(selected) == 1 else "missing" if not selected else "ambiguous"
    )
    report["account_readiness_status"] = "blocked"
    if len(selected) == 1:
        row = selected[0]
        levels = {
            key: _level(row.get(key)) for key in ("option_level", "user_option_level")
        }
        report["account_readiness"] = {
            key: value if value in (0, 1, 2, 3) else None for key, value in levels.items()
        }
        report["account_readiness"].update({
            key: row.get(key) if isinstance(row.get(key), bool) else None
            for key in ("agentic_allowed", "deactivated", "permanently_deactivated")
        })
        account_type = row.get("type")
        report["account_readiness"]["type"] = account_type if account_type in (
            "cash", "margin", "individual", "roth_ira", "traditional_ira", "joint",
        ) else "unknown"
        brokerage_type = row.get("brokerage_account_type")
        report["account_readiness"]["brokerage_account_type"] = (
            brokerage_type if brokerage_type in (
                "individual", "joint", "roth_ira", "traditional_ira",
            ) else "unknown"
        )
        state = row.get("state")
        report["account_readiness"]["state"] = (
            state if state in ("active", "inactive", "closed", "restricted") else "unknown"
        )
        report["account_readiness"]["trade_access"] = (
            row.get("agentic_allowed") if isinstance(row.get("agentic_allowed"), bool) else None
        )
        report["observed_options_level"] = levels["option_level"]
        readiness = report["account_readiness"]
        if (readiness["trade_access"] is True and readiness["option_level"] in (1, 2, 3)
                and readiness["deactivated"] is False
                and readiness["permanently_deactivated"] is False
                and readiness["state"] == "active"):
            report["account_readiness_status"] = "read_only_ready"
        approval = read("get_option_level_upgrade_info", {})
        report["approval"] = (
            "requires_user_confirmation" if levels["option_level"] in (1, 2, 3)
            else "needs_user_action"
        )
        report["approval_evidence"] = "account.option_level"
        report["approval_link_available"] = bool(approval.get("upgrade_url"))
        positions = read("get_option_positions", {"nonzero": True})
        positions_ok = report["checks"].get("get_option_positions") != "unavailable"
        report["positions_count"] = len(_rows(positions)) if positions_ok else None
        report["positions_truncated"] = bool(positions.get("next")) if positions_ok else None
    else:
        report["approval"] = "needs_user_action"
        report["positions_count"] = None
        report["positions_truncated"] = None
    if symbol is None:
        return report
    research = {"symbol": symbol, "chains_count": 0, "instruments": [], "bounded": True}
    report["research"] = research
    chains = _rows(read("get_option_chains", {"underlying_symbol": symbol}))
    research["chains_count"] = len(chains)
    if not chains:
        research["status"] = "unavailable" if report["checks"].get(
            "get_option_chains",
        ) == "unavailable" else "empty"
        return report
    chain_symbol = chains[0].get("symbol", chains[0].get("underlying_symbol"))
    if chain_symbol is not None and chain_symbol != symbol:
        research["status"] = "mismatched_chain"
        return report
    chain_id = chains[0].get("id")
    if not isinstance(chain_id, str) or not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", chain_id):
        research["status"] = "invalid_chain"
        return report
    research["chains_truncated"] = len(chains) > 1
    expirations = chains[0].get("expiration_dates", [])
    expirations = sorted({value for value in expirations if isinstance(value, str)
                          and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
                          and value >= now.date().isoformat()}) if isinstance(expirations, list) else []
    if not expirations:
        research["status"] = "no_upcoming_expiration"
        return report
    research["expirations_examined"] = []
    instruments = []
    instrument_data = {}
    for expiration in expirations[:3]:
        research["expirations_examined"].append(expiration)
        instrument_data = read("get_option_instruments", {
            "chain_id": chain_id, "state": "active", "tradability": "tradable",
            "expiration_dates": expiration,
        })
        instruments = _rows(instrument_data)
        if instruments:
            research["expiration_date"] = expiration
            break
        if report["checks"].get("get_option_instruments") == "unavailable":
            break
    research["expirations_truncated"] = len(expirations) > len(research["expirations_examined"])
    research["instruments_truncated"] = (
        len(instruments) > max_instruments or bool(instrument_data.get("next"))
    )
    for instrument in instruments[:max_instruments]:
        if (instrument.get("chain_id", chain_id) != chain_id
                or instrument.get("expiration_date", research["expiration_date"])
                != research["expiration_date"]):
            continue
        identifier = instrument.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", identifier):
            continue
        entry = {
            "strike_price": _number(instrument.get("strike_price")),
            "type": instrument.get("type") if instrument.get("type") in ("call", "put") else None,
            "expiration_date": None, "quote": None,
        }
        expiration = instrument.get("expiration_date")
        if isinstance(expiration, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", expiration):
            entry["expiration_date"] = expiration
        quotes = _rows(read("get_option_quotes", {"instrument_ids": [identifier]}))
        quotes = [row["quote"] for row in quotes if isinstance(row.get("quote"), dict)
                  and row["quote"].get("instrument_id") == identifier]
        if quotes:
            quote = quotes[0]
            entry["quote"] = {
                key: _number(quote.get(key)) for key in (
                    "bid_price", "ask_price", "mark_price", "delta", "gamma", "theta",
                    "vega", "rho", "implied_volatility",
                )
            }
            entry["quote"]["freshness"] = _freshness(
                quote.get("updated_at"), reference_now or datetime.now(timezone.utc), max_age_seconds,
            )
            entry["quote"]["greeks_freshness"] = dict(entry["quote"]["freshness"])
            if not any(entry["quote"][key] is not None for key in (
                "delta", "gamma", "theta", "vega", "rho",
            )):
                entry["quote"]["greeks_freshness"] = {"status": "unknown", "as_of": None}
            entry["quote"]["fetched_at"] = report["fetched_at"].get("get_option_quotes")
        research["instruments"].append(entry)
    research["status"] = "available"
    if report["checks"].get("get_option_instruments") == "unavailable":
        research["status"] = "unavailable"
    elif not instruments:
        research["status"] = "no_instruments"
    elif len(research["instruments"]) != min(len(instruments), max_instruments) or any(
        entry["quote"] is None for entry in research["instruments"]
    ):
        research["status"] = "partial"
    return report
