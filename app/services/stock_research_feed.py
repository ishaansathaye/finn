"""Publish market-only evidence for independent consumers, never portfolio decisions."""
from datetime import UTC, datetime
import json
import math
import os
from pathlib import Path
import re
import tempfile


def publish_research(path, packets: list[dict], now: datetime) -> None:
    if not path:
        return
    rows = []
    for p in packets:
        if p.get("asset_class") != "stock" or p.get("quote_source") not in {"cache", "yfinance", "twelve_data"}:
            continue
        try:
            stamp = datetime.fromtimestamp(float(p["quote_timestamp"]), UTC)
            price = float(p["close"])
            momentum, financials, catalyst = (float(p[k]) for k in ("trend_score", "fundamental_score", "catalyst_score"))
        except (KeyError, ValueError, TypeError, OSError, OverflowError):
            continue
        if not (0 <= (now - stamp).total_seconds() <= 3600 and price > 0 and
                all(map(math.isfinite, (price, momentum, financials, catalyst))) and re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", p["symbol"])):
            continue
        rows.append({"symbol": p["symbol"], "sector": str(p.get("sector") or "Other")[:48], "price": price,
                     "quoted_at": stamp.isoformat(), "trend": momentum, "fundamentals": financials, "catalyst": catalyst})
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=dest.parent, delete=False) as f:
        try:
            json.dump({"version": 1, "as_of": now.isoformat(), "research": rows}, f, allow_nan=False)
            f.close()
            os.replace(f.name, dest)
        finally:
            Path(f.name).unlink(missing_ok=True)
