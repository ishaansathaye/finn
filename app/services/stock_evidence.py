"""Deterministic research features; scores are not probabilities of a profit."""
from __future__ import annotations

import math
import re
from statistics import stdev


def article_evidence(item: dict) -> tuple[float, list[str], list[str]]:
    text = " ".join(str(item.get(k) or "") for k in ("title", "summary")).lower()
    if re.search(r"stock price,? news,? quote|stock price.*quote and history", text):
        return 0.0, [], []
    positive = {
        r"rais(?:es|ed|ing) (?:its |full.year )?guidance": "raised company guidance",
        r"revenue (?:grew|grows|growth of|increased) \d": "reported revenue growth",
        r"wins? .{0,35}contract|awarded .{0,35}contract": "contract award reported",
        r"(?:fda|regulatory) approv(?:al|es|ed)": "regulatory approval reported",
        r"authoriz(?:es|ed) .{0,35}(?:buyback|repurchase)": "repurchase authorization reported",
    }
    negative = {
        r"cut[s]? .{0,20}guidance|lower[s]? .{0,20}guidance": "guidance cut reported",
        r"bear market|(?:drop|fall|declin)\w* .{0,8}\d+%": "price deterioration reported",
        r"weak demand|revenue (?:fell|falls|declined)": "weakening operating demand",
        r"dilut\w*|stock offering|share offering": "possible dilution",
        r"investigation|lawsuit|fraud|bankruptcy": "legal or solvency risk",
        r"earnings miss|miss(?:es|ed) .{0,20}estimates": "earnings disappointment",
    }
    reasons = [label for pattern, label in positive.items() if re.search(pattern, text)]
    risks = [label for pattern, label in negative.items() if re.search(pattern, text)]
    # A headline alone cannot verify the issuer, economic size, or expectations.
    if re.search(r"\b(?:not|denies|rumor|rumour|could|may|expected to)\b", text):
        reasons = []
    score = min(0.6, len(reasons) * 0.3) - min(1.2, len(risks) * 0.6)
    return score, reasons, risks


def news_evidence(items: list[dict]) -> tuple[float, list[str], list[str]]:
    seen: set[str] = set()
    score, reasons, risks = 0.0, [], []
    for item in items:
        title = re.sub(r"[^a-z0-9]+", " ", str(item.get("title") or "").lower()).strip()
        if not title or title in seen:
            continue
        seen.add(title)
        value, positive, negative = article_evidence(item)
        score += value
        reasons.extend(positive)
        risks.extend(negative)
    return round(max(-2.0, min(1.0, score)), 2), list(dict.fromkeys(reasons))[:3], list(dict.fromkeys(risks))[:3]


def risk_metrics(history: list[dict]) -> dict:
    closes = [float(row.get("close") or 0) for row in history]
    closes = [value for value in closes if value > 0 and math.isfinite(value)]
    if len(closes) < 64:
        return {"annualized_volatility_pct": None, "max_drawdown_pct": None, "history_sessions": len(closes)}
    returns = [math.log(b / a) for a, b in zip(closes[-64:-1], closes[-63:])]
    peak, drawdown = closes[0], 0.0
    for close in closes:
        peak = max(peak, close)
        drawdown = min(drawdown, (close / peak - 1) * 100)
    dollar_volumes = [float(row.get("close") or 0) * float(row.get("volume") or 0) for row in history[-20:]]
    return {
        "annualized_volatility_pct": round(stdev(returns) * math.sqrt(252) * 100, 2),
        "max_drawdown_pct": round(drawdown, 2),
        "avg_dollar_volume_20d": sum(dollar_volumes) / len(dollar_volumes),
        "history_sessions": len(closes),
    }


def balanced_shortlist(ranked: list[tuple], limit: int, sector_for_symbol) -> list[tuple]:
    """Reserve half the slots for strongest names, then cover distinct sectors."""
    selected = list(ranked[: max(1, limit // 2)])
    seen = {item[1] for item in selected}
    sectors = {sector_for_symbol(item[1]) for item in selected}
    for item in ranked:
        sector = sector_for_symbol(item[1])
        if len(selected) < limit and item[1] not in seen and sector not in sectors:
            selected.append(item)
            seen.add(item[1])
            sectors.add(sector)
    for item in ranked:
        if len(selected) < limit and item[1] not in seen:
            selected.append(item)
            seen.add(item[1])
    return selected[:limit]


def evidence_gaps(packet: dict) -> list[str]:
    gaps = []
    if packet.get("quote_source") in {"stale_cache", "unknown", None}:
        gaps.append("fresh quote unavailable")
    metrics = packet.get("history_metrics") or {}
    if metrics.get("return_3m_pct") is None:
        gaps.append("insufficient price history")
    if not packet.get("fundamental_reasons") and packet.get("asset_class") != "etf":
        gaps.append("no corroborating fundamental evidence")
    if float(metrics.get("avg_dollar_volume_20d") or 0) < 1_000_000:
        gaps.append("liquidity not established")
    return gaps
