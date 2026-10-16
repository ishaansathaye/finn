"""Durable aggregate scan diagnostics and a close-window informational batch.

Scheduling is deliberately weekday-only, 16:00 <= Eastern time < 17:00.
There is no exchange calendar: weekday holidays are included and early closes
do not move this window. Delivery acknowledgment remains the poller's job.
"""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from html import escape
import json
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app.models import IntegrationState, RecommendationBatch, StockListenerCheckpoint


PROVIDER = "stock_alert_diagnostics"
STATUS_TYPE = "stock-status"
PACIFIC = ZoneInfo("America/Los_Angeles")
EASTERN = ZoneInfo("America/New_York")
MODES = {
    "gemini": "AI review",
    "gemini_cached": "cached AI review",
    "rules_only": "rules-based (AI unavailable)",
    "rules_only_daily_budget": "rules-based (daily AI allowance reached)",
    "rules_only_request_spacing": "rules-based (between scheduled AI reviews)",
    "rules_only_market_reserve": "rules-based (AI allowance reserved for market hours)",
    "rules_only_quota_backoff": "rules-based (AI provider cooldown)",
    "rules_only_provider_error": "rules-based (AI provider error)",
    "rules_only_not_configured": "rules-based (AI not configured)",
    "rules_only_no_candidates": "not run (no candidates)",
    "not_run": "not run",
    "unknown": "unavailable",
}


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def timestamp(value) -> datetime | None:
    try:
        return utc(datetime.fromisoformat(str(value)))
    except (ValueError, TypeError):
        return None


def _object(value: str) -> dict:
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def _read(session: Session, provider: str, key: str) -> dict:
    row = session.exec(select(IntegrationState).where(
        IntegrationState.provider == provider, IntegrationState.key == key,
    )).first()
    return _object(row.value) if row else {}


def _save(session: Session, key: str, payload: dict, now: datetime) -> None:
    row_id = f"{PROVIDER}:{key}"
    row = session.get(IntegrationState, row_id)
    if row is None:
        row = IntegrationState(id=row_id, provider=PROVIDER, key=key, value="")
    row.value = json.dumps(payload, sort_keys=True)
    row.updated_at = utc(now).replace(tzinfo=None)
    session.add(row)
    session.commit()


def begin_scan(session: Session, now: datetime) -> dict:
    scan = {
        "schema_version": 1, "scan_id": str(uuid4()), "scope": "primary_only",
        "started_at": utc(now).isoformat(), "status": "running",
        "analysis_mode": "not_run", "reported_ideas": 0, "eligible_ideas": 0,
        "notification_rejections": {},
    }
    _save(session, "scan:" + scan["scan_id"], scan, now)
    return scan


def _current_snapshot(session: Session, key: str, scan: dict, now: datetime) -> dict:
    value = _read(session, "stock_intelligence", key)
    checked = timestamp(value.get("checked_at") if key == "llm_scan" else value.get("as_of"))
    started = timestamp(scan["started_at"])
    return value if checked and started and started <= checked <= utc(now) else {}


def capture_analysis(session: Session, scan: dict, market, now: datetime) -> None:
    mode = getattr(getattr(market, "llm", None), "stock_analysis_mode", "unknown")
    scan["analysis_mode"] = mode if mode in MODES else "unknown"
    current = _current_snapshot(session, "llm_scan", scan, now)
    health = _read(session, "stock_intelligence", "llm_health")
    day = utc(now).date().isoformat()
    limit = getattr(getattr(market, "settings", None), "stock_llm_daily_request_budget", None)
    limit = current.get("request_limit", current.get("limit", limit))
    used = current.get("requests_today") if current else (
        health.get("requests_today") if health.get("budget_day") == day else 0 if health else None
    )
    budget = {"budget_day": day, "timezone": "UTC"}
    for key, value in (("requests_today", used), ("limit", limit)):
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            budget[key] = value
    next_at = timestamp(current.get("next_request_at"))
    if next_at and next_at > utc(now):
        budget["next_request_at"] = next_at.isoformat()
    elif mode == "rules_only_daily_budget":
        budget["next_request_at"] = (
            utc(now).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        ).isoformat()
        budget["next_request_is_lower_bound"] = True
    scan["model_budget"] = budget

    latest = _current_snapshot(session, "latest", scan, now)
    packets = latest.get("packets")
    if isinstance(packets, list):
        scan["analyzed_packets"] = len(packets)
        scan["quote_sources"] = dict(Counter(
            p.get("quote_source") if p.get("quote_source") in {
                "yfinance", "twelve_data", "cache", "stale_cache", "unavailable",
            } else "unknown" for p in packets if isinstance(p, dict)
        ))
    ideas = latest.get("ideas")
    if isinstance(ideas, list):
        scan["post_strategy_sides"] = dict(Counter(
            p.get("side") if p.get("side") in {"buy", "sell", "hold", "watch"} else "unknown"
            for p in ideas if isinstance(p, dict)
        ))
    strategy = latest.get("portfolio_strategy") or {}
    if isinstance(strategy, dict):
        reasons = []
        rejections = strategy.get("candidate_rejections") or {}
        for reason in rejections.values() if isinstance(rejections, dict) else []:
            text = str(reason).lower()
            reasons.append("sector_limit" if "sector limit" in text else "portfolio_risk_limit")
        scan["strategy_rejections"] = dict(Counter(reasons))
    sources = {}
    for row in session.exec(select(StockListenerCheckpoint).where(
        StockListenerCheckpoint.provider == "stock_global_news",
    )).all():
        if row.key not in {"alpha_vantage", "fmp"}:
            continue
        value = _object(row.cursor or "{}")
        status = value.get("status")
        sources[row.key] = {
            "status": status if status in {
                "healthy", "empty", "blocked", "unavailable", "rate_limited",
            } else "unknown",
            "last_polled_at": utc(row.last_polled_at).isoformat() if row.last_polled_at else None,
        }
    scan["global_news_sources"] = sources


def finish_scan(session: Session, scan: dict, now: datetime, *, failed: bool = False) -> None:
    scan["status"] = "failed" if failed else "completed"
    scan["finished_at"] = utc(now).isoformat()
    _save(session, "scan:" + scan["scan_id"], scan, now)


def _day_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = utc(now).astimezone(PACIFIC).replace(hour=0, minute=0, second=0, microsecond=0)
    return (start.astimezone(UTC).replace(tzinfo=None),
            (start + timedelta(days=1)).astimezone(UTC).replace(tzinfo=None))


def status_due(session: Session, now: datetime) -> bool:
    eastern = utc(now).astimezone(EASTERN)
    if eastern.weekday() >= 5 or eastern.hour != 16:
        return False
    start, end = _day_bounds(now)
    sent = session.exec(select(RecommendationBatch).where(
        RecommendationBatch.batch_type.in_(["stock-alert", STATUS_TYPE]),
        RecommendationBatch.sent_at >= start, RecommendationBatch.sent_at < end,
    )).first()
    return sent is None


def _summary(session: Session, scan: dict, now: datetime) -> str:
    start, end = _day_bounds(now)
    rows = session.exec(select(IntegrationState).where(
        IntegrationState.provider == PROVIDER, IntegrationState.key.startswith("scan:"),
        IntegrationState.updated_at >= start, IntegrationState.updated_at < end,
    )).all()
    counts = Counter(_object(row.value).get("status", "unknown") for row in rows)
    lines = [
        "<b>Finn stock close status</b>",
        "No stock alerts sent today (Pacific).",
        f"Alert scans: {counts['completed']} completed, {counts['failed']} failed.",
    ]
    if scan["status"] == "failed":
        lines.append("Latest scan failed; no-action conclusions are unavailable.")
    else:
        lines[-1] += (
            f" Latest: {scan.get('reported_ideas', 0)} post-strategy ideas, "
            f"{scan.get('eligible_ideas', 0)} alert-eligible."
        )
    analysis = "Analysis: " + MODES.get(scan.get("analysis_mode"), "unavailable") + "."
    budget = scan.get("model_budget", {})
    used, limit = budget.get("requests_today"), budget.get("limit")
    if isinstance(used, int) and isinstance(limit, int):
        analysis += f" Budget {used}/{limit} ({escape(budget['budget_day'])} UTC)."
    lines.append(analysis)
    next_at = timestamp(budget.get("next_request_at"))
    if next_at:
        lines.append("Next AI eligibility no earlier than " + next_at.strftime("%m-%d %H:%M UTC") + ".")
    reasons = scan.get("notification_rejections", {})
    labels = {
        "hold_watch": "HOLD/WATCH only", "action_threshold": "size/confidence/priority checks",
        "after_hours": "after-hours checks", "cooldown": "alert cooldown",
    }
    why = [f"{labels[k]} ({v})" for k, v in reasons.items() if v and k in labels]
    if scan.get("strategy_rejections"):
        why.append("portfolio risk limits")
    if why:
        lines.append("Filtered: " + "; ".join(why) + ".")
    elif scan["status"] == "completed" and not scan.get("reported_ideas"):
        lines.append("No report candidates; this does not establish complete market coverage.")
    packets = scan.get("analyzed_packets")
    sources = scan.get("quote_sources", {})
    quote_text = (f"quotes {packets} packets, "
                  f"{sources.get('stale_cache', 0)} stale, {sources.get('unknown', 0)} unknown"
                  if packets is not None else "quote coverage unknown")
    news = scan.get("global_news_sources", {})
    lines.append("Data: " + quote_text + "; global news " + (
        ", ".join(f"{k} {v['status']}" for k, v in sorted(news.items())) if news else "unknown"
    ) + ".")
    if scan.get("listener_failed"):
        lines.append("News/filing refresh failed during this scan.")
    lines.append("Informational only; no trade suggested.")
    return "\n".join(lines)


def close_status_batch(session: Session, scan: dict, now: datetime) -> RecommendationBatch | None:
    if not status_due(session, now):
        return None
    day = utc(now).astimezone(PACIFIC).date().isoformat()
    batch_id = f"stock-status:primary:{day}"
    batch = session.get(RecommendationBatch, batch_id)
    if batch is not None and batch.sent_at is not None:
        return None
    if batch is None:
        batch = RecommendationBatch(
            id=batch_id, batch_type=STATUS_TYPE, created_at=utc(now).replace(tzinfo=None),
        )
        session.add(batch)
    # The current poller owns sent_at. Reuse this batch after a failed delivery.
    _save(session, "status:" + batch_id, {"text": _summary(session, scan, now)}, now)
    return batch


def status_messages(session: Session, batch: RecommendationBatch, now: datetime) -> list[str]:
    if batch.sent_at is not None or not status_due(session, now):
        return []
    if batch.id != f"stock-status:primary:{utc(now).astimezone(PACIFIC).date().isoformat()}":
        return []
    text = _read(session, PROVIDER, "status:" + batch.id).get("text")
    return [text] if isinstance(text, str) and text else []
