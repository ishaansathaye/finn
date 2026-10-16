"""Reuse bounded language analysis, never stale execution prices or sizing."""
from __future__ import annotations

import hashlib
import json
import math


def analysis_fingerprint(context: dict, packets: list[dict], model: str) -> str:
    # Market values move every tick. Ownership and cash changes still invalidate.
    portfolio = {k: v for k, v in context.items() if k not in {"total_value", "top_holdings"}}
    portfolio["top_holdings"] = [
        {k: v for k, v in row.items() if k != "market_value"}
        for row in context.get("top_holdings", [])
    ]
    evidence = []
    for packet in packets:
        row = {k: v for k, v in packet.items()
               if k not in {"close", "momentum_pct", "holding_value", "holding_weight"}}
        row["holding_weight"] = round(float(packet.get("holding_weight") or 0), 2)
        evidence.append(row)
    payload = {"version": 2, "model": model, "portfolio": portfolio,
               "evidence": sorted(evidence, key=lambda row: row["symbol"])}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def prices_within_reuse_window(packets: list[dict], previous: dict) -> bool:
    if not packets or not isinstance(previous, dict) or len(previous) != len(packets):
        return False
    for packet in packets:
        try:
            current = float(packet.get("close") or 0)
            prior = float(previous.get(packet["symbol"]) or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        if not all(math.isfinite(v) and v > 0 for v in (current, prior)):
            return False
        if packet.get("quote_source") not in {"cache", "twelve_data", "yfinance"}:
            return False
        drift = abs(current - prior) / prior
        if drift > .005 and not math.isclose(drift, .005, rel_tol=1e-12):
            return False
    return True
