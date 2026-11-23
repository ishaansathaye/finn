"""Evidence gate for proposed sales of long-horizon holdings."""
from datetime import date
import math
import re


def protect_long_hold(decision, packet: dict, today: date) -> str:
    if decision.action not in {"reduce", "exit"} or packet.get("holding_lane") not in {"confirmed_core", "probable_core"}:
        return ""
    fresh = []
    for filing in packet.get("recent_filings") or []:
        try:
            filed = date.fromisoformat(str(filing.get("filing_date"))[:10])
        except ValueError:
            continue
        if 0 <= (today - filed).days <= 45:
            fresh.append((filed, filing))
    reason = ""
    key = ""
    for filed, f in sorted(fresh, key=lambda row: row[0], reverse=True):
        form = f.get("form_type")
        if form == "8-K" and re.search(r"(?<![\d.])1\.03(?!\d)", str(f.get("items") or "")):
            reason = f"The issuer disclosed bankruptcy or receivership in its {filed} 8-K (Item 1.03)."
            key = f"insolvency:{filed}"
            break
        if form not in {"10-Q", "10-K"} or packet.get("asset_class") == "etf":
            continue
        facts = packet.get("fundamentals") or {}
        trend = facts.get("trend_summary") or {}
        try:
            growth = float(trend["revenue_growth_yoy_pct"])
            eps = float(trend["eps_growth_yoy_pct"])
        except (KeyError, ValueError, TypeError):
            continue
        if (math.isfinite(growth) and math.isfinite(eps) and growth <= -10 and eps <= -20
                and decision.confidence == "high" and (facts.get("revenue") or {}).get("filed") == str(filed)
                and (facts.get("eps_diluted") or {}).get("filed") == str(filed)):
            reason = f"The {filed} {form} shows sales down {-growth:.1f}% and diluted EPS down {-eps:.1f}% year over year."
            key = f"earnings:{filed}"
            break
    if reason:
        if key.startswith("earnings:") and decision.action == "exit":
            decision.action, decision.sizing_intent = "reduce", "small"
        decision.thesis_summary = "Review long-term thesis. " + reason
        decision.key_reasons = [reason]
        return key
    decision.action, decision.sizing_intent = "hold", "none"
    decision.thesis_summary = "Hold for the long term. No verified thesis break supports selling; a price dip or portfolio weight alone is not an exit signal."
    decision.key_reasons = ["Reassess after material issuer evidence changes."]
    return ""


def sell_cost_context(action, holding) -> str:
    if action not in {"exit", "reduce"} or holding is None:
        return ""
    basis = getattr(holding, "cost_basis", None)
    value = getattr(holding, "market_value", None)
    if not basis or basis <= 0 or value is None or value <= 0:
        return "Cost basis or valuation unavailable; profit on a sale is unknown."
    move = 100 * (value / basis - 1)
    return f"Position is {abs(move):.1f}% {'above' if move >= 0 else 'below'} recorded cost. This is unrealized, before tax."
