from __future__ import annotations

from datetime import UTC, datetime
from html import escape
import json
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app.config import get_settings
from app.models import IntegrationState


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "unavailable"


def _number(value: Any) -> str:
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return "unavailable"
    if as_float.is_integer():
        return f"{as_float:,.0f}"
    return f"{as_float:,.4f}".rstrip("0").rstrip(".")


def _pct(value: Any) -> str:
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "unavailable"


def _format_time(value: Any) -> str:
    if not value:
        return "unavailable"
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone().strftime("%Y-%m-%d %I:%M %p %Z")


class AgenticObserverService:
    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def snapshot_path(self) -> Path:
        return Path(self.settings.agentic_trader_snapshot_path).expanduser()

    def _load_snapshot_from_state(self, session: Session) -> dict[str, Any] | None:
        row = session.exec(
            select(IntegrationState).where(
                IntegrationState.provider == "agentic_trader",
                IntegrationState.key == "latest_snapshot",
            )
        ).first()
        if row is None or not row.value:
            return None
        try:
            payload = json.loads(row.value)
        except json.JSONDecodeError:
            return {"error": "Stored Agentic snapshot is invalid JSON."}
        return payload if isinstance(payload, dict) else {"error": "Stored Agentic snapshot is not an object."}

    def load_snapshot(self, session: Session) -> dict[str, Any] | None:
        state_snapshot = self._load_snapshot_from_state(session)
        if state_snapshot is not None:
            return state_snapshot
        path = self.snapshot_path
        if path.exists():
            try:
                payload = json.loads(path.read_text())
            except json.JSONDecodeError:
                return {"error": f"Agentic snapshot at {path} is invalid JSON."}
            return payload if isinstance(payload, dict) else {"error": f"Agentic snapshot at {path} is not an object."}
        return None

    def format_snapshot(self, snapshot: dict[str, Any] | None) -> str:
        if snapshot is None:
            return (
                "Finn Agentic Account Observer\n"
                "No Agentic snapshot exists yet.\n\n"
                "The autonomous trader service should write "
                f"`{self.snapshot_path}`. Finn is read-only and will not trade this account."
            )
        if snapshot.get("error"):
            return f"Finn Agentic Account Observer\n{escape(str(snapshot['error']))}"

        account = snapshot.get("account") if isinstance(snapshot.get("account"), dict) else {}
        risk = snapshot.get("risk") if isinstance(snapshot.get("risk"), dict) else {}
        health = snapshot.get("health") if isinstance(snapshot.get("health"), dict) else {}
        positions = snapshot.get("positions") if isinstance(snapshot.get("positions"), list) else []
        orders = snapshot.get("recent_orders") if isinstance(snapshot.get("recent_orders"), list) else []
        decisions = snapshot.get("recent_decisions") if isinstance(snapshot.get("recent_decisions"), list) else []

        lines = [
            "Finn Agentic Account Observer",
            f"Service updated: {_format_time(snapshot.get('as_of'))}",
            f"Account last observed: {_format_time(snapshot.get('account_as_of'))}",
            "",
            "Account",
            f"- Value: {_money(account.get('total_value'))}",
            f"- Cash: {_money(account.get('cash'))}",
            f"- Buying power: {_money(account.get('buying_power'))}",
            f"- Today's realized P/L: {_money(account.get('realized_pnl_today'))}",
            f"- Today's unrealized P/L: {_money(account.get('unrealized_pnl_today'))}",
            f"- Open positions: {len(positions)}",
            f"- Last observed open orders: {_number(snapshot.get('observed_open_orders'))}",
            "",
        ]

        if snapshot.get("positions_reconciliation_required"):
            lines.append("An order was attempted after these holdings were observed. They may be pre-trade; wait for the next account refresh.")
        lines.append("Last Observed Positions")
        if positions:
            for index, position in enumerate(positions[:8], start=1):
                symbol = escape(str(position.get("symbol") or "UNKNOWN"))
                lines.extend(
                    [
                        f"{index}. {symbol}",
                        f"   Quantity: {_number(position.get('quantity'))}",
                        f"   Market value: {_money(position.get('market_value'))}",
                        f"   Average cost: {_money(position.get('average_cost'))}",
                        f"   Unrealized P/L: {_money(position.get('unrealized_pnl'))} ({_pct(position.get('unrealized_pnl_pct'))})",
                    ]
                )
        else:
            lines.append("- No open Agentic positions.")

        lines.extend(["", "Recent Agent Actions"])
        if decisions:
            for index, decision in enumerate(decisions[:8], start=1):
                action = escape(str(decision.get("action") or "unknown").upper())
                symbol = escape(str(decision.get("symbol") or "UNKNOWN"))
                result = escape(str(decision.get("result") or decision.get("status") or "logged"))
                thesis = escape(str(decision.get("thesis") or "Reason unavailable."))
                lines.extend(
                    [
                        f"{index}. {_format_time(decision.get('created_at'))} - {action} {symbol}",
                        f"   Result: {result}",
                        f"   Confidence: {_number(decision.get('confidence'))}",
                        f"   Reason: {thesis}",
                    ]
                )
        elif orders:
            for index, order in enumerate(orders[:8], start=1):
                side = escape(str(order.get("side") or "unknown").upper())
                symbol = escape(str(order.get("symbol") or "UNKNOWN"))
                state = escape(str(order.get("state") or "unknown"))
                lines.extend(
                    [
                        f"{index}. {_format_time(order.get('created_at'))} - {side} {symbol}",
                        f"   Status: {state}",
                        f"   Quantity / Notional: {_number(order.get('quantity'))} / {_money(order.get('notional'))}",
                        "   Reason: unavailable because this order came from Robinhood history only.",
                    ]
                )
        else:
            lines.append("- No recent Agentic actions found.")

        lines.extend(
            [
                "",
                "Risk State",
                f"- Mode: {escape(str(risk.get('mode') or snapshot.get('mode') or 'unknown'))}",
                f"- Kill switch: {escape(str(risk.get('kill_switch') or 'not active'))}",
                f"- Daily loss guard: {escape(str(risk.get('daily_loss_guard') or 'unknown'))}",
                f"- Max position guard: {escape(str(risk.get('max_position_guard') or 'unknown'))}",
                f"- Codex available: {bool(health.get('codex_available'))}",
                f"- Robinhood MCP available: {bool(health.get('robinhood_mcp_available'))}",
            ]
        )
        return "\n".join(lines)

    def format_agent_report(self, session: Session) -> str:
        return self.format_snapshot(self.load_snapshot(session))
