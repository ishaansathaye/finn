from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from html import escape
import json
from pathlib import Path
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app.adapters.snaptrade import SnapTradeAdapter
from app.config import get_settings
from app.models import EventBetOutcome, EventPosition, Execution, IntegrationState, Recommendation, RecommendationBatch, RecommendationOutcome, ReportLog
from app.services.market_research import MarketResearchService
from app.services.portfolio_sync import PortfolioSyncService


SERIES_CODE_MAPS: dict[str, dict[str, str]] = {
    "KXMLBGAME": {
        "ARI": "Arizona", "AZ": "Arizona", "ATL": "Atlanta", "BAL": "Baltimore", "BOS": "Boston",
        "CHC": "Chicago C", "CWS": "Chicago WS", "CHW": "Chicago WS", "CIN": "Cincinnati", "CLE": "Cleveland",
        "COL": "Colorado", "DET": "Detroit", "HOU": "Houston", "KC": "Kansas City", "KC1": "Kansas City",
        "LAA": "Los Angeles A", "LAD": "Los Angeles D", "MIA": "Miami", "MIL": "Milwaukee", "MIN": "Minnesota",
        "NYM": "New York M", "NYY": "New York Y", "ATH": "Athletics", "OAK": "Athletics", "PHI": "Philadelphia",
        "PIT": "Pittsburgh", "SD": "San Diego", "SDP": "San Diego", "SEA": "Seattle", "SF": "San Francisco",
        "SFG": "San Francisco", "STL": "St. Louis", "TB": "Tampa Bay", "TBR": "Tampa Bay", "TEX": "Texas",
        "TOR": "Toronto", "WSH": "Washington",
    },
    "KXMLSGAME": {
        "ATL": "Atlanta United", "AUS": "Austin FC", "CHI": "Chicago Fire FC", "CIN": "Cincinnati",
        "CLB": "Columbus Crew", "COL": "Colorado Rapids SC", "DAL": "FC Dallas", "DC": "DC United",
        "HOU": "Houston Dynamo", "LAG": "Los Angeles Galaxy", "LAFC": "Los Angeles FC", "MIA": "Inter Miami",
        "MIN": "Minnesota United", "MTL": "CF Montreal", "NE": "New England", "NSH": "Nashville SC",
        "NYC": "New York City FC", "NYRB": "New York RB", "ORL": "Orlando City", "PHI": "Philadelphia Union",
        "POR": "Portland Timbers", "RSL": "Real Salt Lake", "SEA": "Seattle Sounders", "SJ": "San Jose Earthquakes",
        "SKC": "Sporting Kansas City", "STL": "St. Louis City", "TOR": "Toronto FC", "VAN": "Vancouver Whitecaps FC",
    },
    "KXNHLGAME": {
        "ANA": "Anaheim", "BOS": "Boston", "BUF": "Buffalo", "CAR": "Carolina", "CBJ": "Columbus",
        "CGY": "Calgary", "CHI": "Chicago", "COL": "Colorado", "DAL": "Dallas", "DET": "Detroit",
        "EDM": "Edmonton", "FLA": "Florida", "LAK": "Los Angeles", "MIN": "Minnesota", "MTL": "Montreal",
        "NJD": "New Jersey", "NSH": "Nashville", "NYI": "NY Islanders", "NYR": "NY Rangers", "OTT": "Ottawa",
        "PHI": "Philadelphia", "PIT": "Pittsburgh", "SEA": "Seattle", "SJS": "San Jose", "STL": "St. Louis",
        "TBL": "Tampa Bay", "TOR": "Toronto", "UTA": "Utah", "VAN": "Vancouver", "VGK": "Vegas", "WPG": "Winnipeg",
        "WSH": "Washington",
    },
}


class RecommendationService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.market_research = MarketResearchService()
        self.portfolio_sync = PortfolioSyncService(SnapTradeAdapter())

    def _integration_state(self, session: Session, provider: str, key: str) -> IntegrationState | None:
        return session.exec(
            select(IntegrationState).where(IntegrationState.provider == provider, IntegrationState.key == key)
        ).first()

    def _load_json_state(self, session: Session, provider: str, key: str) -> dict:
        row = self._integration_state(session, provider, key)
        if row is None or not row.value:
            return {}
        try:
            return json.loads(row.value)
        except Exception:
            return {}

    def _save_json_state(self, session: Session, provider: str, key: str, payload: dict | None) -> None:
        row = self._integration_state(session, provider, key)
        if payload is None:
            if row is not None:
                session.delete(row)
                session.commit()
            return
        if row is None:
            row = IntegrationState(provider=provider, key=key, value="")
        row.value = json.dumps(payload)
        session.add(row)
        session.commit()

    def latest_batch_by_type(self, session: Session, batch_type: str) -> RecommendationBatch | None:
        stmt = select(RecommendationBatch).where(RecommendationBatch.batch_type == batch_type).order_by(RecommendationBatch.created_at.desc())
        return session.exec(stmt).first()

    def latest_gx_batch(self, session: Session) -> RecommendationBatch | None:
        return self.latest_batch_by_type(session, "gx")

    def _gx_provider(self) -> str:
        return "gx_execution"

    def create_placeholder_batch(self, session: Session, batch_type: str = "manual") -> RecommendationBatch:
        batch = RecommendationBatch(batch_type=batch_type)
        session.add(batch)
        session.commit()
        session.refresh(batch)

        now = datetime.now(UTC).replace(tzinfo=None)
        recommendations = [
            Recommendation(
                batch_id=batch.id,
                item_number=1,
                recommendation_type="stock",
                symbol="AMD",
                side="buy",
                amount_usd=500.0,
                reason="Placeholder stock idea while research engines are being implemented.",
                risk="medium",
                execution_mode="manual",
                valid_until=now + timedelta(minutes=self.settings.earnings_trade_validity_minutes),
            ),
            Recommendation(
                batch_id=batch.id,
                item_number=2,
                recommendation_type="etf",
                symbol="VTI",
                side="buy",
                amount_usd=750.0,
                reason="Placeholder ETF add while allocation logic is being implemented.",
                risk="low",
                execution_mode="manual",
                valid_until=now + timedelta(hours=self.settings.core_etf_validity_hours),
            ),
        ]
        for rec in recommendations:
            session.add(rec)
        session.commit()
        return batch

    def _event_parts(
        self,
        recommendation_type: str,
        symbol: str | None,
        event_code: str | None,
        reason: str,
    ) -> tuple[str, str, str | None, str | None]:
        if recommendation_type != "event":
            label = symbol or event_code or "UNKNOWN"
            return label, label, None, None
        if symbol and " || " in symbol:
            parts = symbol.split(" || ")
            title = parts[0] if len(parts) >= 1 else (event_code or "UNKNOWN")
            pick = parts[1] if len(parts) >= 2 else title
            category = parts[2] if len(parts) >= 3 and parts[2] else None
            time_label = parts[3] if len(parts) >= 4 and parts[3] else None
            return title, pick, category, time_label
        label = symbol or event_code or "UNKNOWN"
        return label, label, None, None

    def _event_meta_parts(self, symbol: str | None) -> list[str]:
        if not symbol or " || " not in symbol:
            return []
        parts = symbol.split(" || ")
        return parts[4:]

    def _event_meta(self, symbol: str | None) -> dict[str, str | None]:
        parts = self._event_meta_parts(symbol)
        return {
            "contract_path": parts[0] if len(parts) >= 1 and parts[0] else None,
            "execution_market_ticker": parts[1] if len(parts) >= 2 and parts[1] else None,
            "execution_event_ticker": parts[2] if len(parts) >= 3 and parts[2] else None,
            "execution_contract_id": parts[3] if len(parts) >= 4 and parts[3] else None,
            "event_starts_at": parts[4] if len(parts) >= 5 and parts[4] else None,
        }

    def _encode_event_symbol(self, idea) -> str:  # noqa: ANN001
        extras = [
            idea.contract_path or "",
            idea.execution_market_ticker or "",
            idea.execution_event_ticker or "",
            idea.execution_contract_id or "",
            idea.event_starts_at.isoformat() if getattr(idea, "event_starts_at", None) else "",
        ]
        return " || ".join(
            [
                idea.title,
                idea.pick_label,
                idea.category or "",
                idea.time_label or "",
                *extras,
            ]
        )

    def _event_label(self, recommendation_type: str, symbol: str | None, event_code: str | None, reason: str) -> str:
        if recommendation_type != "event":
            return symbol or event_code or "UNKNOWN"
        title, _pick, _category, _time_label = self._event_parts(recommendation_type, symbol, event_code, reason)
        return title

    def _format_local_time(self, value: datetime) -> str:
        tz = ZoneInfo(self.settings.local_timezone)
        localized = value.replace(tzinfo=UTC).astimezone(tz)
        return localized.strftime("%b %d, %Y %I:%M %p %Z")

    def _local_date(self, value: datetime) -> datetime.date:
        return value.replace(tzinfo=UTC).astimezone(ZoneInfo(self.settings.local_timezone)).date()

    def _format_local_date(self, value: datetime.date) -> str:
        return value.strftime("%b %-d")

    def _confidence_label(self, risk: str, amount_usd: float) -> str:
        if risk == "low":
            return "High"
        if risk == "medium" and amount_usd >= 30:
            return "Medium"
        if risk == "medium":
            return "Medium-Low"
        return "Low"

    def _event_display(
        self,
        recommendation_type: str,
        symbol: str | None,
        event_code: str | None,
        reason: str,
    ) -> tuple[str, str, str | None, str | None]:
        label, pick, category, time_label = self._event_parts(recommendation_type, symbol, event_code, reason)
        return (label, pick, category, time_label)

    def _event_pick_line(self, side: str, pick: str) -> str:
        if side.lower() == "yes":
            return f"Pick: {pick}"
        return f"Pick: fade {pick}"

    def _event_thesis_line(self, reason: str) -> str:
        sentence = reason.split(". ", 1)[0].strip()
        if sentence.endswith("."):
            sentence = sentence[:-1]
        return sentence

    def _confidence_marker(self, risk: str) -> str:
        return {
            "low": "High",
            "medium": "Medium",
            "high": "Low",
        }.get(risk, "Medium")

    def _event_reason_bits(self, reason: str) -> tuple[str, str | None]:
        text = reason.strip()
        thesis = text
        numeric_bits: list[str] = []
        for label in ("Market implied", "Finn fair", "Edge", "Entry", "Current market", "Move since entry", "Max return", "Potential profit"):
            pattern = rf"{label}:\s*([^.]*)\."
            match = re.search(pattern, text)
            if match:
                numeric_bits.append(f"{label} {match.group(1).strip()}")
                thesis = re.sub(pattern, "", thesis).strip()
        numbers = " • ".join(numeric_bits) if numeric_bits else None
        thesis = re.sub(r"\s+", " ", thesis).strip().rstrip(".")
        return thesis, numbers

    def _local_now(self) -> datetime:
        return datetime.now(ZoneInfo(self.settings.local_timezone))

    def _is_same_local_day(self, value: datetime) -> bool:
        return self._local_date(value) == self._local_now().date()

    def _parse_implied_probability(self, reason: str) -> float | None:
        match = re.search(r"(?:Implied odds|Market implied):\s*([0-9]+(?:\.[0-9]+)?)%", reason)
        if not match:
            return None
        probability = float(match.group(1)) / 100.0
        if not (0 < probability < 1):
            return None
        return probability

    def _parse_labeled_probability(self, reason: str, label: str) -> float | None:
        match = re.search(rf"{re.escape(label)}:\s*([0-9]+(?:\.[0-9]+)?)%", reason)
        if not match:
            return None
        probability = float(match.group(1)) / 100.0
        if not (0 < probability < 1):
            return None
        return probability

    def _refresh_execution_reason(
        self,
        reason: str,
        *,
        entry_price: float | None = None,
        contract_count: int | None = None,
        max_return: float | None = None,
        potential_profit: float | None = None,
    ) -> str:
        text = reason.strip()
        for label in ("Entry", "Contracts", "Max return", "Potential profit"):
            text = re.sub(rf"{re.escape(label)}:\s*.*?(?:\.(?=\s|$)|$)", "", text).strip()
        text = re.sub(r"\s+", " ", text).strip().rstrip(".")
        bits = [text] if text else []
        if entry_price is not None and 0 < entry_price < 1:
            bits.append(f"Entry: {round(entry_price * 100):.0f}%.")
        if contract_count is not None and contract_count > 0:
            bits.append(f"Contracts: {int(contract_count)}.")
        if max_return is not None:
            bits.append(f"Max return: ${round(float(max_return), 2):.2f}.")
        if potential_profit is not None:
            bits.append(f"Potential profit: ${round(float(potential_profit), 2):.2f}.")
        return " ".join(bits).strip()

    def _parse_contract_count(self, reason: str) -> int | None:
        match = re.search(r"Contracts:\s*([0-9]+)", reason)
        if not match:
            return None
        count = int(match.group(1))
        return count if count > 0 else None

    def _stock_section_title(self, side: str) -> str:
        return {
            "buy": "Potential Buys",
            "sell": "Reduce / Sell",
            "hold": "Keep Holding",
            "watch": "Watching",
            "needs_cash": "Needs Cash Transfer",
        }.get(side, "Stock / ETF Ideas")

    def _stock_action_label(self, side: str, target: str) -> str:
        return {
            "buy": f"BUY {target}",
            "needs_cash": f"BUY {target}",
            "sell": f"SELL {target}",
            "hold": f"HOLD {target}",
            "watch": f"WATCH {target}",
        }.get(side, f"{side.upper()} {target}")

    def _parse_stock_reason(self, reason: str) -> tuple[str, str | None, str | None]:
        thesis = reason.strip()
        why_now: str | None = None
        risk_watch: str | None = None

        if " Why now: " in thesis:
            thesis, rest = thesis.split(" Why now: ", 1)
            why_now = rest
        if why_now and " Risk watch: " in why_now:
            why_now, risk_watch = why_now.split(" Risk watch: ", 1)
        elif " Risk watch: " in thesis:
            thesis, risk_watch = thesis.split(" Risk watch: ", 1)

        return thesis.strip(), (why_now.strip() if why_now else None), (risk_watch.strip() if risk_watch else None)

    async def _batch_context(
        self,
        session: Session,
        batch_id: str,
    ) -> tuple[list[dict[str, object]], int, float, dict[str, dict[str, object]], float]:
        stmt = (
            select(Recommendation)
            .where(Recommendation.batch_id == batch_id)
            .order_by(Recommendation.item_number)
        )
        recommendations = [
            {
                "item_number": rec.item_number,
                "recommendation_type": rec.recommendation_type,
                "symbol": rec.symbol,
                "event_code": rec.event_code,
                "side": rec.side,
                "amount_usd": rec.amount_usd,
                "reason": rec.reason,
                "risk": rec.risk,
                "valid_until": rec.valid_until,
            }
            for rec in session.exec(stmt).all()
        ]
        holdings_count, buying_power = self.market_research.portfolio_summary(session)
        stock_recommendations = [
            rec for rec in recommendations if rec["recommendation_type"] in {"stock", "etf"}
        ]
        quote_symbols = [rec["symbol"] for rec in stock_recommendations if rec["symbol"]]
        quote_map = await self.market_research.quote_map(session, quote_symbols) if quote_symbols else {}
        prediction_reserve = max(0.0, buying_power * self.settings.max_total_prediction_bankroll_pct)
        deployable_cash = max(0.0, buying_power - self.settings.minimum_cash_buffer_usd - prediction_reserve)
        return recommendations, holdings_count, buying_power, quote_map, deployable_cash

    async def format_batch_messages(self, session: Session, batch_id: str) -> list[str]:
        batch = session.get(RecommendationBatch, batch_id)
        if batch is None:
            return ["No batch found."]
        if batch.batch_type in {"gx", "gx-live"}:
            return [await self.format_gx_batch(session, batch_id)]
        recommendations, holdings_count, buying_power, quote_map, deployable_cash = await self._batch_context(
            session,
            batch_id,
        )
        stock_recommendations = [
            rec for rec in recommendations if rec["recommendation_type"] in {"stock", "etf"}
        ]
        event_recommendations = [rec for rec in recommendations if rec["recommendation_type"] == "event"]
        news_recommendations = [rec for rec in recommendations if rec["recommendation_type"] == "news"]
        slate_items: list = []

        messages: list[str] = []

        stock_lines = [
            "<b>Finn Report: Stocks / ETFs</b>",
            f"Portfolio context: {holdings_count} synced stock/ETF symbol(s), estimated buying power <b>${buying_power:.2f}</b>",
        ]
        if stock_recommendations:
            potential_buys = [rec for rec in stock_recommendations if rec["side"] == "buy" and rec["amount_usd"] <= deployable_cash]
            needs_cash = [rec for rec in stock_recommendations if rec["side"] == "buy" and rec["amount_usd"] > deployable_cash]
            ordered_sections = ["buy", "needs_cash", "sell", "hold", "watch"]
            for side in ordered_sections:
                if side == "buy":
                    section_items = potential_buys
                elif side == "needs_cash":
                    section_items = needs_cash
                else:
                    section_items = [rec for rec in stock_recommendations if rec["side"] == side]
                if not section_items:
                    continue
                section_limit = {
                    "buy": 4,
                    "needs_cash": 4,
                    "sell": 3,
                    "hold": 3,
                    "watch": 3,
                }.get(side, len(section_items))
                section_items = section_items[:section_limit]
                stock_lines.append("")
                stock_lines.append(f"<b>{self._stock_section_title(side)}</b>")
                for rec in section_items:
                    target = rec["symbol"] or "UNKNOWN"
                    thesis, why_now, risk_watch = self._parse_stock_reason(str(rec["reason"]))
                    quote = quote_map.get(target, {})
                    close = float(quote.get("close") or 0.0) if quote else 0.0
                    approx_shares = (rec["amount_usd"] / close) if close and rec["amount_usd"] > 0 else 0.0
                    if side in {"buy", "needs_cash"}:
                        detail_text = (
                            f"Approx size: ${rec['amount_usd']:.2f} or about {approx_shares:.3f} share(s) at ${close:.2f}"
                            if close
                            else f"Approx size: ${rec['amount_usd']:.2f}"
                        )
                    elif side == "sell":
                        detail_text = (
                            f"Approx trim: ${rec['amount_usd']:.2f} or about {approx_shares:.3f} share(s) at ${close:.2f}"
                            if close
                            else f"Approx trim: ${rec['amount_usd']:.2f}"
                        )
                    else:
                        detail_text = "No immediate trade size."
                    stock_lines.append(
                        f"{rec['item_number']}. <b>{escape(self._stock_action_label(side, target))}</b>"
                    )
                    stock_lines.append(f"   {escape(detail_text)}")
                    if side == "needs_cash":
                        shortage = max(0.0, rec["amount_usd"] - deployable_cash)
                        stock_lines.append(
                            f"   Cash note: needs about ${shortage:.2f} more deployable cash above your ${self.settings.minimum_cash_buffer_usd:.0f} buffer."
                        )
                    stock_lines.append(
                        f"   Confidence: {self._confidence_label(rec['risk'], rec['amount_usd'])}"
                    )
                    stock_lines.append(f"   Thesis: {escape(thesis)}")
                    if why_now:
                        stock_lines.append(f"   Why now: {escape(why_now)}")
                    if risk_watch:
                        stock_lines.append(f"   Risk watch: {escape(risk_watch)}")
                    stock_lines.append(
                        f"   Valid until: {escape(self._format_local_time(rec['valid_until']))}"
                    )
        if news_recommendations:
            stock_lines.append("")
            stock_lines.append("<b>Important News / Watchouts</b>")
            for rec in news_recommendations:
                target = rec["symbol"] or "UNKNOWN"
                stock_lines.append(f"{rec['item_number']}. <b>{escape(target)}</b> — {escape(rec['reason'])}")
        elif news_recommendations:
            pass
        elif not stock_recommendations:
            stock_lines.append("")
            stock_lines.append("No stock or ETF actions are active right now.")
        if stock_recommendations or news_recommendations:
            messages.append("\n".join(stock_lines))

        event_lines = [
            "<b>Finn Report: Prediction Markets</b>",
            f"Buying power context: <b>${buying_power:.2f}</b>",
        ]
        slate_by_ticker = {item.ticker: item for item in slate_items}
        if event_recommendations:
            event_lines.append("")
            event_lines.append("<b>Prediction Market Ideas</b>")
            for rec in event_recommendations:
                event_name, pick, category, time_label = self._event_display(
                    rec["recommendation_type"],
                    rec["symbol"],
                    rec["event_code"],
                    rec["reason"],
                )
                buying_power_pct = (rec["amount_usd"] / buying_power * 100.0) if buying_power > 0 else 0.0
                event_lines.append(f"{rec['item_number']}. <b>{escape(event_name)}</b>")
                slate_item = slate_by_ticker.get(str(rec["event_code"] or ""))
                if slate_item is not None:
                    event_lines.append(f"   {escape(slate_item.category)} • {escape(slate_item.time_label)}")
                elif category or time_label:
                    meta_bits = [bit for bit in (category, time_label) if bit]
                    event_lines.append(f"   {escape(' • '.join(meta_bits))}")
                event_lines.append(f"   Pick: <b>{escape(pick)}</b>")
                event_lines.append(f"   Stake: <b>${rec['amount_usd']:.2f}</b> ({buying_power_pct:.1f}%)")
                event_lines.append(f"   Confidence: <b>{escape(self._confidence_marker(str(rec['risk'])))}</b>")
                why_text, numbers_text = self._event_reason_bits(str(rec["reason"]))
                if numbers_text:
                    event_lines.append(f"   Market / Edge: <b>{escape(numbers_text)}</b>")
                event_lines.append(f"   Why: {escape(self._event_thesis_line(why_text))}")
        else:
            event_lines.append("")
            event_lines.append("<b>Prediction Market Ideas</b>")
            event_lines.append(
                "No broker-available near-term event markets passed the current filter. Finn skipped low-signal or unclear contracts."
            )
        if event_recommendations or slate_items or (not stock_recommendations and not news_recommendations):
            messages.append("\n".join(event_lines))
        return messages

    async def format_gx_batch(self, session: Session, batch_id: str) -> str:
        recommendations, _holdings_count, buying_power, _quote_map, _deployable_cash = await self._batch_context(
            session,
            batch_id,
        )
        items = [rec for rec in recommendations if rec["recommendation_type"] == "event"]
        total_stake = sum(float(rec["amount_usd"]) for rec in items)
        total_max_return = 0.0
        total_potential_profit = 0.0
        for rec in items:
            implied_probability = (
                self._parse_labeled_probability(str(rec["reason"]), "Entry")
                or self._parse_implied_probability(str(rec["reason"]))
                or 0.0
            )
            total_max_return += self.market_research._event_max_return(float(rec["amount_usd"]), implied_probability)
            total_potential_profit += self.market_research._event_potential_profit(float(rec["amount_usd"]), implied_probability)

        lines = [
            "<b>Finn Execution Lane: gx</b>",
            f"Buying power context: <b>${buying_power:.2f}</b>",
        ]
        if not items:
            lines.append("")
            lines.append(
                "No low-stake auto-executable prediction bets are active right now. "
                "Finn only includes small same-day bets that still clear payout math and remain tradable."
            )
            return "\n".join(lines)

        lines.extend(
            [
                f"Batch stake if executed: <b>${total_stake:.2f}</b>",
                f"Batch max return: <b>${total_max_return:.2f}</b>",
                f"Batch potential profit: <b>${total_potential_profit:.2f}</b>",
                "",
                "<b>Auto-Executable Small Bets</b>",
            ]
        )
        for rec in items:
            event_name, pick, category, time_label = self._event_display(
                rec["recommendation_type"],
                rec["symbol"],
                rec["event_code"],
                rec["reason"],
            )
            meta = self._event_meta(rec["symbol"])
            why_text, numbers_text = self._event_reason_bits(str(rec["reason"]))
            lines.append(f"{rec['item_number']}. <b>{escape(event_name)}</b>")
            meta_bits = [bit for bit in (category, time_label) if bit]
            if meta_bits:
                lines.append(f"   {escape(' • '.join(meta_bits))}")
            if meta.get("contract_path"):
                lines.append(f"   Robinhood path: <b>{escape(str(meta['contract_path']))}</b>")
            lines.append(f"   Pick: <b>{escape(pick)}</b>")
            lines.append(f"   Stake: <b>${float(rec['amount_usd']):.2f}</b>")
            if numbers_text:
                lines.append(f"   Market / Edge: <b>{escape(numbers_text)}</b>")
            lines.append(f"   Why: {escape(self._event_thesis_line(why_text))}")
        lines.extend(
            [
                "",
                "Reply with <code>x</code> to build the final execution preview.",
                "Then reply with <code>x confirm</code> to place only these low-stake bets.",
            ]
        )
        return "\n".join(lines)

    async def create_gx_batch(self, session: Session, batch_type: str = "gx", *, include_live: bool = True) -> RecommendationBatch:
        try:
            if self.portfolio_sync.adapter.configured():
                await self.portfolio_sync.sync(session)
        except Exception:
            pass
        try:
            ideas = await self.market_research.top_event_execution_ideas(
                session,
                limit=self.settings.gx_batch_limit,
                include_live=include_live,
            )
        except TypeError:
            ideas = await self.market_research.top_event_execution_ideas(
                session,
                limit=self.settings.gx_batch_limit,
            )
        batch = RecommendationBatch(batch_type=batch_type)
        session.add(batch)
        session.commit()
        session.refresh(batch)
        if not ideas:
            return batch

        contract_ids = [str(idea.execution_contract_id or "") for idea in ideas if str(idea.execution_contract_id or "")]
        quote_rows = await self.market_research.robinhood_prediction.get_marketdata_quotes(contract_ids) if contract_ids else {}
        executable_ideas = []
        for idea in ideas:
            contract_id = str(idea.execution_contract_id or "")
            quote = quote_rows.get(contract_id) if contract_id else None
            yes_ask = float((quote or {}).get("yes_ask_price") or 0.0)
            if yes_ask <= 0 or yes_ask >= 1:
                continue
            count = self.market_research._event_contract_count(float(idea.amount_usd), yes_ask)
            if count <= 0:
                continue
            estimated_cost = round(count * yes_ask, 2)
            max_return = round(float(count), 2)
            potential_profit = round(max_return - estimated_cost, 2)
            if potential_profit < self.settings.gx_min_profit_usd:
                continue
            idea.amount_usd = estimated_cost
            idea.implied_probability = yes_ask
            idea.max_return_usd = max_return
            idea.potential_profit_usd = potential_profit
            idea.reason = self._refresh_execution_reason(
                idea.reason,
                entry_price=yes_ask,
                contract_count=count,
                max_return=max_return,
                potential_profit=potential_profit,
            )
            executable_ideas.append(idea)
        ideas = executable_ideas
        if not ideas:
            return batch

        now = datetime.now(UTC).replace(tzinfo=None)
        for idx, idea in enumerate(ideas, start=1):
            session.add(
                Recommendation(
                    batch_id=batch.id,
                    item_number=idx,
                    recommendation_type="event",
                    symbol=self._encode_event_symbol(idea),
                    event_code=idea.execution_event_ticker or idea.ticker,
                    side=idea.side.lower(),
                    amount_usd=round(float(idea.amount_usd), 2),
                    reason=idea.reason,
                    risk=idea.risk,
                    execution_mode="robinhood_auto_candidate",
                    valid_until=now + timedelta(minutes=self.settings.event_trade_validity_minutes),
                )
            )
        session.commit()
        return batch

    async def format_gamble_slate(self, session: Session) -> str:
        holdings_count, buying_power = self.market_research.portfolio_summary(session)
        slate_items = await self.market_research.today_event_slate(limit=60)
        lines = [
            "<b>Finn Slate: Prediction Markets</b>",
            f"Buying power context: <b>${buying_power:.2f}</b>",
        ]
        if not slate_items:
            lines.append("")
            lines.append("No same-day broker-available slate items are visible right now.")
            return "\n".join(lines)

        lines.append("")
        lines.append("<b>Today's Full Slate</b>")
        for idx, item in enumerate(slate_items, start=1):
            category = item.category or "Event"
            lines.append(f"{idx}. <b>{escape(item.title)}</b>")
            lines.append(f"   {escape(category)} • {escape(item.time_label)}")
            lines.append(f"   Sides: {escape(item.options)}")
        return "\n".join(lines)

    def _position_timestamp(self, position: EventPosition) -> datetime:
        return position.opened_at or position.created_at

    def _effective_outcome_timestamp(self, position: EventPosition, outcome: EventBetOutcome) -> datetime:
        settled_at = outcome.settled_at
        checked_at = outcome.checked_at
        opened_at = self._position_timestamp(position)
        if settled_at is not None and opened_at is not None and settled_at < opened_at - timedelta(minutes=5):
            return checked_at or opened_at
        return settled_at or checked_at or opened_at

    def _compact_unknown_event_label(self, value: str | None) -> str:
        text = (value or "").strip()
        if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{27}", text):
            return f"Contract {text[:8]}"
        return text or "Unknown Event"

    def _catalog_contract_metadata(self, events: list[dict], event_states: dict[str, dict]) -> dict[str, dict[str, str | None]]:
        metadata: dict[str, dict[str, str | None]] = {}
        for event in events:
            event_id = str(event.get("id") or "")
            if not event_id:
                continue
            event_state = event_states.get(event_id) or {}
            event_time = self.market_research._robinhood_event_time(event)
            time_label = self.market_research._format_event_time_label(event_time)
            category = str(event_state.get("category") or event.get("category") or "Event")
            event_title = str(event.get("name") or event.get("description") or event_id)
            for contract in (event.get("eventContracts") or {}).values():
                if not isinstance(contract, dict):
                    continue
                contract_id = str(contract.get("id") or "")
                if not contract_id:
                    continue
                pick_label = (
                    str(contract.get("displayLongName") or "")
                    or str(contract.get("name") or "")
                    or str(contract.get("displayShortName") or "")
                    or None
                )
                metadata[contract_id] = {
                    "event_id": event_id,
                    "event_title": event_title,
                    "pick_label": pick_label,
                    "category": category,
                    "time_label": time_label,
                    "symbol": str(contract.get("symbol") or ""),
                }
        return metadata

    def _derive_team_code_maps(self, contract_meta: dict[str, dict[str, str | None]]) -> dict[str, dict[str, str]]:
        code_maps: dict[str, dict[str, str]] = {}
        for meta in contract_meta.values():
            symbol = str(meta.get("symbol") or "")
            pick_label = str(meta.get("pick_label") or "")
            if not symbol or not pick_label:
                continue
            series = symbol.split("-", 1)[0]
            pick_code = symbol.rsplit("-", 1)[-1]
            if not pick_code or pick_code == symbol:
                continue
            code_maps.setdefault(series, {})[pick_code] = pick_label
        return code_maps

    def _decode_contract_from_symbol(
        self,
        symbol: str,
        code_maps: dict[str, dict[str, str]],
    ) -> dict[str, str | None]:
        symbol = symbol.strip()
        parts = symbol.split("-")
        if len(parts) < 3:
            return {}
        series = parts[0]
        pick_code = parts[-1]
        teams_blob = parts[1]
        # strip leading YYMONDD[HHMM] from middle segment
        teams_blob = re.sub(r"^\d{2}[A-Z]{3}\d{2}\d{0,4}", "", teams_blob)
        teams_blob = re.sub(r"^\d{2}[A-Z]{3}\d{2}", "", teams_blob)
        code_map = dict(SERIES_CODE_MAPS.get(series, {}))
        code_map.update(code_maps.get(series, {}))
        known_codes = sorted(code_map.keys(), key=len, reverse=True)

        pick_label = code_map.get(pick_code)
        event_title = None
        if series in {"KXMLBGAME", "KXNHLGAME", "KXNBAGAME", "KXWNBAGAME", "KXMLSGAME"} and teams_blob:
            home = away = None
            for code in known_codes:
                if not teams_blob.startswith(code):
                    continue
                remainder = teams_blob[len(code) :]
                if remainder in code_map:
                    home = code_map.get(code)
                    away = code_map.get(remainder)
                    break
            if home and away:
                event_title = f"{home} vs {away}"
        if pick_label is None:
            pick_label = pick_code if pick_code != "TIE" else "Tie"
        time_label = None
        dt = self.market_research._ticker_datetime(symbol)
        if dt is not None:
            time_label = self.market_research._format_event_time_label(dt)
        category = {
            "KXMLBGAME": "Baseball",
            "KXNHLGAME": "Men's Pro Hockey",
            "KXNBAGAME": "Basketball",
            "KXWNBAGAME": "Basketball",
            "KXMLSGAME": "Soccer",
        }.get(series, "Event")
        return {
            "symbol": symbol,
            "event_title": event_title,
            "pick_label": pick_label,
            "category": category,
            "time_label": time_label,
        }

    def _historic_robinhood_contract_metadata(self, session: Session) -> dict[str, dict[str, str | None]]:
        cached = self.market_research.cache.get_json_stale(session, "robinhood_prediction", "contract_meta_history")
        metadata: dict[str, dict[str, str | None]] = cached if isinstance(cached, dict) else {}
        if metadata:
            return metadata

        fallback_paths = [Path("/tmp/rh_pred.html")]
        for path in fallback_paths:
            if not path.exists():
                continue
            try:
                text = path.read_text()
                match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', text, re.DOTALL)
                if not match:
                    continue
                page_props = json.loads(match.group(1))["props"]["pageProps"]
                metadata.update(
                    self._catalog_contract_metadata(
                        page_props.get("events") or [],
                        page_props.get("eventStates") or {},
                    )
                )
            except Exception:
                continue
        if metadata:
            self.market_research.cache.set_json(session, "robinhood_prediction", "contract_meta_history", metadata)
        return metadata

    def _robinhood_contract_metadata(self, session: Session, snapshot) -> dict[str, dict[str, str | None]]:  # noqa: ANN001
        metadata = self._historic_robinhood_contract_metadata(session)
        metadata.update(self._catalog_contract_metadata(snapshot.events, snapshot.event_states))
        self.market_research.cache.set_json(session, "robinhood_prediction", "contract_meta_history", metadata)
        return metadata

    def _lookup_event_position(
        self,
        session: Session,
        *,
        broker_position_id: str | None = None,
        broker_order_id: str | None = None,
        broker_contract_id: str | None = None,
        broker_event_id: str | None = None,
    ) -> EventPosition | None:
        rows = list(session.exec(select(EventPosition)).all())
        if broker_position_id:
            for row in rows:
                if row.broker_position_id == broker_position_id:
                    return row
        if broker_order_id:
            for row in rows:
                if row.broker_order_id == broker_order_id:
                    return row
        if broker_contract_id:
            for row in rows:
                if row.broker_contract_id == broker_contract_id and row.source == "robinhood":
                    return row
        if broker_event_id:
            for row in rows:
                if row.broker_event_id == broker_event_id and row.status in {"manual_confirmed", "assumed_open"}:
                    return row
        return None

    async def _sync_robinhood_event_positions(self, session: Session) -> None:
        adapter = self.market_research.robinhood_prediction
        if not adapter.configured():
            return
        try:
            account = await adapter.get_event_contract_account()
            if not account:
                return
            snapshot = await adapter.get_catalog_snapshot(
                max_age_seconds=self.settings.robinhood_prediction_catalog_cache_seconds
            )
            contract_meta = self._robinhood_contract_metadata(session, snapshot)
            account_id = str(account.get("id") or "")
            positions = await adapter.list_event_positions(account_id=account_id, page_size=200)
            orders = await adapter.list_event_orders(account_id=account_id, page_size=100)
            settlements = await adapter.list_cash_settlement_executions(account_id=account_id, page_size=100)
        except Exception:
            return

        all_contract_ids = {
            str(row.get("contractId") or "")
            for row in positions
        }
        all_contract_ids.update(
            str((row.get("orderLegs") or [{}])[0].get("contractId") or "")
            for row in orders
            if row.get("orderLegs")
        )
        all_contract_ids.update(str(row.get("contractId") or "") for row in settlements)
        existing_placeholder_rows = session.exec(select(EventPosition).where(EventPosition.source == "robinhood")).all()
        for row in existing_placeholder_rows:
            if not row.broker_contract_id:
                continue
            if (
                str(row.event_title or "").startswith("Contract ")
                or str(row.pick_label or "").startswith("Contract ")
                or row.category == "Event"
                or str(row.event_title or "") == row.broker_contract_id
            ):
                all_contract_ids.add(row.broker_contract_id)
        unresolved_contract_ids = []
        for contract_id in all_contract_ids:
            if not contract_id:
                continue
            meta = contract_meta.get(contract_id)
            if meta is None:
                unresolved_contract_ids.append(contract_id)
                continue
            event_title = str(meta.get("event_title") or "")
            pick_label = str(meta.get("pick_label") or "")
            if event_title.startswith("Contract ") or pick_label.startswith("Contract "):
                unresolved_contract_ids.append(contract_id)
        if unresolved_contract_ids:
            try:
                quote_map = await adapter.get_marketdata_quotes(unresolved_contract_ids)
            except Exception:
                quote_map = {}
            code_maps = self._derive_team_code_maps(contract_meta)
            for contract_id, quote in quote_map.items():
                derived = self._decode_contract_from_symbol(str(quote.get("symbol") or ""), code_maps)
                if not derived:
                    continue
                contract_meta[contract_id] = {
                    "event_id": contract_id,
                    "event_title": str(derived.get("event_title") or f"Contract {contract_id[:8]}"),
                    "pick_label": str(derived.get("pick_label") or f"Contract {contract_id[:8]}"),
                    "category": str(derived.get("category") or "Event"),
                    "time_label": derived.get("time_label"),
                    "symbol": str(derived.get("symbol") or ""),
                }
            self.market_research.cache.set_json(session, "robinhood_prediction", "contract_meta_history", contract_meta)

        changed = False
        for row in existing_placeholder_rows:
            if not row.broker_contract_id:
                continue
            if not (
                str(row.event_title or "").startswith("Contract ")
                or str(row.pick_label or "").startswith("Contract ")
                or row.category == "Event"
                or str(row.event_title or "") == row.broker_contract_id
            ):
                continue
            meta = contract_meta.get(row.broker_contract_id)
            if not meta:
                continue
            row.broker_event_id = str(meta.get("event_id") or row.broker_event_id or row.event_code)
            row.event_code = row.broker_event_id or row.event_code
            row.event_title = str(meta.get("event_title") or row.event_title or row.event_code)
            row.pick_label = str(meta.get("pick_label") or row.pick_label or row.event_title)
            row.category = str(meta.get("category") or row.category or "Event")
            row.time_label = str(meta.get("time_label") or row.time_label or "")
            session.add(row)
            changed = True

        live_position_ids: set[str] = set()

        for broker_position in positions:
            broker_position_id = str(broker_position.get("id") or "")
            broker_contract_id = str(broker_position.get("contractId") or "")
            meta = contract_meta.get(broker_contract_id, {})
            row = self._lookup_event_position(
                session,
                broker_position_id=broker_position_id,
                broker_contract_id=broker_contract_id,
                broker_event_id=str(meta.get("event_id") or ""),
            )
            if row is None:
                row = EventPosition(
                    event_code=str(meta.get("event_id") or broker_contract_id or "UNKNOWN"),
                    side="yes",
                    amount_usd=0.0,
                    source="robinhood",
                )
            quantity = float(broker_position.get("quantity") or 0.0)
            avg_fill_price = float(broker_position.get("tradePrice") or 0.0)
            row.source = "robinhood"
            row.broker_account_id = account_id
            row.broker_position_id = broker_position_id
            row.broker_contract_id = broker_contract_id
            row.broker_event_id = str(meta.get("event_id") or row.broker_event_id or row.event_code)
            row.event_code = row.broker_event_id or row.event_code
            row.quantity = quantity
            row.avg_fill_price = avg_fill_price
            row.amount_usd = round(quantity * avg_fill_price, 2)
            row.event_title = str(meta.get("event_title") or row.event_title or row.event_code)
            row.pick_label = str(meta.get("pick_label") or row.pick_label or row.event_title)
            row.category = str(meta.get("category") or row.category or "Event")
            row.time_label = str(meta.get("time_label") or row.time_label or "")
            opened_at_raw = broker_position.get("acquiredAt")
            if opened_at_raw:
                try:
                    row.opened_at = datetime.fromisoformat(str(opened_at_raw).replace("Z", "+00:00")).replace(tzinfo=None)
                except ValueError:
                    pass
            row.status = "broker_open"
            session.add(row)
            live_position_ids.add(broker_position_id)

            outcome = session.exec(select(EventBetOutcome).where(EventBetOutcome.event_position_id == row.id)).first()
            if outcome is None:
                outcome = EventBetOutcome(event_position_id=row.id, event_code=row.event_code)
            outcome.outcome = "in_progress"
            outcome.implied_probability = avg_fill_price if 0 < avg_fill_price < 1 else None
            outcome.payout_usd = round(quantity, 2) if quantity > 0 else None
            outcome.profit_usd = round(quantity - row.amount_usd, 2) if quantity > 0 else None
            outcome.checked_at = datetime.now(UTC).replace(tzinfo=None)
            session.add(outcome)
            changed = True

        for order in orders:
            order_id = str(order.get("orderId") or "")
            order_legs = order.get("orderLegs") or []
            if not order_id or not order_legs:
                continue
            leg = order_legs[0]
            broker_contract_id = str(leg.get("contractId") or "")
            meta = contract_meta.get(broker_contract_id, {})
            filled_quantity = float(order.get("filledQuantity") or 0.0)
            average_price = float(order.get("averagePrice") or leg.get("averagePrice") or 0.0)
            derived_state = str(order.get("derivedState") or order.get("orderState") or "").upper()
            if filled_quantity <= 0 and derived_state not in {"QUEUED", "CONFIRMED", "PENDING", "OPEN", "ACTIVE"}:
                continue
            row = self._lookup_event_position(
                session,
                broker_order_id=order_id,
                broker_contract_id=broker_contract_id,
                broker_event_id=str(meta.get("event_id") or ""),
            )
            if row is None:
                row = EventPosition(
                    event_code=str(meta.get("event_id") or broker_contract_id or "UNKNOWN"),
                    side="yes",
                    amount_usd=0.0,
                    source="robinhood",
                )
            row.source = "robinhood"
            row.broker_account_id = account_id
            row.broker_order_id = order_id
            row.broker_contract_id = broker_contract_id
            row.broker_event_id = str(meta.get("event_id") or row.broker_event_id or row.event_code)
            row.event_code = row.broker_event_id or row.event_code
            row.quantity = filled_quantity if filled_quantity > 0 else row.quantity
            row.avg_fill_price = average_price if average_price > 0 else row.avg_fill_price
            if row.quantity and row.avg_fill_price:
                row.amount_usd = round(float(row.quantity) * float(row.avg_fill_price), 2)
            row.event_title = str(meta.get("event_title") or row.event_title or row.event_code)
            row.pick_label = str(meta.get("pick_label") or row.pick_label or row.event_title)
            row.category = str(meta.get("category") or row.category or "Event")
            row.time_label = str(meta.get("time_label") or row.time_label or "")
            created_at_raw = order.get("createdAt")
            if created_at_raw:
                try:
                    row.opened_at = datetime.fromisoformat(str(created_at_raw).replace("Z", "+00:00")).replace(tzinfo=None)
                except ValueError:
                    pass
            row.status = "broker_filled" if filled_quantity > 0 else "broker_order_open"
            session.add(row)
            changed = True

        for settlement in settlements:
            contract_id = str(settlement.get("contractId") or "")
            execution_id = str(settlement.get("id") or "")
            meta = contract_meta.get(contract_id, {})
            row = self._lookup_event_position(
                session,
                broker_contract_id=contract_id,
                broker_event_id=str(meta.get("event_id") or ""),
            )
            if row is None:
                price_per_contract = float(settlement.get("pricePerContract") or 0.0)
                quantity = float(settlement.get("quantity") or 0.0)
                realized_without_fees = float(
                    ((settlement.get("realizedPnlWithoutFees") or {}).get("amount")) or 0.0
                )
                stake_estimate = max(0.0, round(quantity * price_per_contract - realized_without_fees, 2))
                row = EventPosition(
                    event_code=str(meta.get("event_id") or contract_id or "UNKNOWN"),
                    side="yes",
                    amount_usd=stake_estimate,
                    source="robinhood",
                )
            row.source = "robinhood"
            row.broker_account_id = account_id
            row.broker_contract_id = contract_id
            row.broker_event_id = str(meta.get("event_id") or row.broker_event_id or row.event_code)
            row.event_code = row.broker_event_id or row.event_code
            row.event_title = str(meta.get("event_title") or row.event_title or row.event_code)
            row.pick_label = str(meta.get("pick_label") or row.pick_label or row.event_title)
            row.category = str(meta.get("category") or row.category or "Event")
            row.time_label = str(meta.get("time_label") or row.time_label or "")
            event_time_raw = settlement.get("eventTime")
            parsed_event_time = None
            if event_time_raw:
                try:
                    parsed_event_time = datetime.fromisoformat(str(event_time_raw).replace("Z", "+00:00")).replace(tzinfo=None)
                except ValueError:
                    parsed_event_time = None
            if row.opened_at is None and parsed_event_time is not None:
                row.opened_at = parsed_event_time
            row.status = "settled_win" if float(((settlement.get("realizedPnl") or {}).get("amount")) or 0.0) > 0 else "settled_loss"
            session.add(row)

            outcome = session.exec(select(EventBetOutcome).where(EventBetOutcome.event_position_id == row.id)).first()
            if outcome is None:
                outcome = EventBetOutcome(event_position_id=row.id, event_code=row.event_code)
            quantity = float(settlement.get("quantity") or 0.0)
            settlement_price = float(settlement.get("pricePerContract") or 0.0)
            payout = round(quantity * settlement_price, 2)
            realized = float(((settlement.get("realizedPnl") or {}).get("amount")) or 0.0)
            realized_without_fees = float(
                ((settlement.get("realizedPnlWithoutFees") or {}).get("amount")) or realized
            )
            if row.amount_usd <= 0 and payout > 0:
                row.amount_usd = max(0.0, round(payout - realized_without_fees, 2))
            outcome.outcome = "won" if realized >= 0 else "lost"
            outcome.payout_usd = payout
            outcome.profit_usd = realized
            outcome.implied_probability = row.avg_fill_price if row.avg_fill_price and 0 < row.avg_fill_price < 1 else outcome.implied_probability
            outcome.settlement_price = settlement_price
            outcome.realized_pnl_without_fees_usd = realized_without_fees
            outcome.broker_execution_id = execution_id
            if parsed_event_time is not None:
                outcome.settled_at = parsed_event_time
            outcome.checked_at = datetime.now(UTC).replace(tzinfo=None)
            outcome.notes = f"Robinhood settlement side={settlement.get('side')}"
            session.add(outcome)
            changed = True

        stale_open_rows = session.exec(
            select(EventPosition).where(EventPosition.source == "robinhood", EventPosition.status == "broker_open")
        ).all()
        for row in stale_open_rows:
            if row.broker_position_id and row.broker_position_id not in live_position_ids:
                row.status = "broker_closed"
                session.add(row)
                changed = True

        if changed:
            session.commit()

    async def _event_position_status(self, session: Session, position: EventPosition) -> dict[str, object]:
        if position.source == "robinhood":
            outcome_row = session.exec(select(EventBetOutcome).where(EventBetOutcome.event_position_id == position.id)).first()
            status = outcome_row.outcome if outcome_row else (
                "in_progress" if position.status in {"broker_open", "broker_order_open", "broker_filled"} else "lost"
            )
            stake = round(float(position.amount_usd or 0.0), 2)
            payout = outcome_row.payout_usd if outcome_row and outcome_row.payout_usd is not None else (
                round(float(position.quantity or 0.0), 2) if position.quantity else None
            )
            profit = outcome_row.profit_usd if outcome_row and outcome_row.profit_usd is not None else (
                round(float(position.quantity or 0.0) - stake, 2) if position.quantity else None
            )
            event_name = self._compact_unknown_event_label(position.event_title or position.event_code)
            pick = self._compact_unknown_event_label(position.pick_label or position.event_title or position.event_code)
            category = position.category if position.category and position.category != "Event" else None
            time_label = position.time_label or None
            return {
                "event_name": event_name,
                "pick": pick,
                "category": category,
                "time_label": time_label,
                "status": status,
                "stake": stake,
                "payout": payout,
                "profit": profit,
                "created_at": self._position_timestamp(position),
                "implied_probability": outcome_row.implied_probability if outcome_row else position.avg_fill_price,
                "settled_at": outcome_row.settled_at if outcome_row else None,
                "notes": outcome_row.notes if outcome_row else None,
            }

        recommendation = session.get(Recommendation, position.source_recommendation_id) if position.source_recommendation_id else None
        event_name, pick, category, time_label = self._event_display(
            "event",
            recommendation.symbol if recommendation else None,
            position.event_code,
            recommendation.reason if recommendation else "",
        )
        implied_probability = self._parse_implied_probability(recommendation.reason) if recommendation and recommendation.reason else None

        if position.event_code.count("-") >= 4 and recommendation:
            payout = round(position.amount_usd / implied_probability, 2) if implied_probability and implied_probability > 0 else None
            profit = round((payout or 0.0) - position.amount_usd, 2) if payout is not None else None
            return {
                "event_name": event_name,
                "pick": pick,
                "category": category,
                "time_label": time_label,
                "status": "in_progress",
                "stake": round(position.amount_usd, 2),
                "payout": payout,
                "profit": profit,
                "created_at": self._position_timestamp(position),
                "implied_probability": implied_probability,
                "settled_at": None,
                "notes": "Awaiting Robinhood fill/settlement sync",
            }

        markets: list[dict] = []
        for status in ("settled", "closed", None):
            try:
                payload = await self.market_research.kalshi.list_markets(
                    event_ticker=position.event_code,
                    status=status,
                    limit=100,
                )
                markets = payload.get("markets") or []
            except Exception:
                markets = []
            if markets:
                break

        outcome = "in_progress"
        payout = None
        profit = None
        notes = None
        settled_now = None
        matched_market_probability = None

        if markets:
            explicit_pick_only = len({(market.get("yes_sub_title") or "").strip() for market in markets}) > 1
            if explicit_pick_only:
                matched_market = next(
                    (
                        market
                        for market in markets
                        if (market.get("yes_sub_title") or "").strip().lower() == pick.strip().lower()
                    ),
                    None,
                )
                if matched_market is not None:
                    yes_bid = float(matched_market.get("yes_bid_dollars") or 0.0)
                    yes_ask = float(matched_market.get("yes_ask_dollars") or 0.0)
                    last_price = float(matched_market.get("last_price_dollars") or 0.0)
                    matched_market_probability = (
                        (yes_bid + yes_ask) / 2.0 if yes_bid > 0 and yes_ask > 0 else (last_price if last_price > 0 else None)
                    )
                if matched_market and str(matched_market.get("result") or "").lower() == "yes":
                    outcome = "won"
                    notes = f"winner market: {pick}"
                elif any(str(market.get("result") or "").lower() == "yes" for market in markets):
                    outcome = "lost"
                    notes = f"winner market differed from {pick}"
            else:
                market = markets[0]
                result = str(market.get("result") or "").lower()
                side = position.side.lower()
                if side == "yes":
                    yes_bid = float(market.get("yes_bid_dollars") or 0.0)
                    yes_ask = float(market.get("yes_ask_dollars") or 0.0)
                    last_price = float(market.get("last_price_dollars") or 0.0)
                    matched_market_probability = (
                        (yes_bid + yes_ask) / 2.0 if yes_bid > 0 and yes_ask > 0 else (last_price if last_price > 0 else None)
                    )
                elif side == "no":
                    no_bid = float(market.get("no_bid_dollars") or 0.0)
                    no_ask = float(market.get("no_ask_dollars") or 0.0)
                    matched_market_probability = (
                        (no_bid + no_ask) / 2.0 if no_bid > 0 and no_ask > 0 else None
                    )
                if result in {"yes", "no"}:
                    outcome = "won" if side == result else "lost"
                    notes = f"market settled {result}"

        if outcome in {"won", "lost"}:
            settled_now = datetime.now(UTC).replace(tzinfo=None)

        entry_probability = implied_probability or matched_market_probability
        if entry_probability and entry_probability > 0:
            potential_payout = round(position.amount_usd / entry_probability, 2)
            if outcome == "won":
                payout = potential_payout
                profit = round(potential_payout - position.amount_usd, 2)
            elif outcome == "lost":
                payout = 0.0
                profit = round(-position.amount_usd, 2)
            else:
                payout = potential_payout
                profit = round(potential_payout - position.amount_usd, 2)

        return {
            "event_name": event_name,
            "pick": pick,
            "category": category,
            "time_label": time_label,
            "status": outcome,
            "stake": round(position.amount_usd, 2),
            "payout": payout,
            "profit": profit,
            "created_at": position.created_at,
            "implied_probability": entry_probability,
            "settled_at": settled_now,
            "notes": notes,
        }

    async def _refresh_event_bet_outcomes(self, session: Session) -> None:
        await self._sync_robinhood_event_positions(session)
        positions = list(session.exec(select(EventPosition).order_by(EventPosition.created_at.desc())).all())
        if not positions:
            return
        changed = False
        for position in positions:
            if position.source == "robinhood":
                continue
            summary = await self._event_position_status(session, position)
            stmt = select(EventBetOutcome).where(EventBetOutcome.event_position_id == position.id)
            outcome_row = session.exec(stmt).first()
            if outcome_row is None:
                outcome_row = EventBetOutcome(
                    event_position_id=position.id,
                    event_code=position.event_code,
                )
            outcome_row.outcome = str(summary["status"])
            outcome_row.payout_usd = float(summary["payout"]) if summary["payout"] is not None else None
            outcome_row.profit_usd = float(summary["profit"]) if summary["profit"] is not None else None
            outcome_row.implied_probability = (
                float(summary["implied_probability"]) if summary["implied_probability"] is not None else None
            )
            outcome_row.settled_at = summary["settled_at"] if summary["settled_at"] is not None else outcome_row.settled_at
            outcome_row.checked_at = datetime.now(UTC).replace(tzinfo=None)
            outcome_row.notes = str(summary["notes"]) if summary["notes"] else outcome_row.notes
            session.add(outcome_row)

            next_status = {
                "won": "settled_win",
                "lost": "settled_loss",
                "in_progress": "manual_confirmed",
            }.get(str(summary["status"]), position.status)
            if position.status != next_status:
                position.status = next_status
                session.add(position)
            changed = True
        if changed:
            session.commit()

    async def format_daily_event_positions(self, session: Session) -> str:
        await self._refresh_event_bet_outcomes(session)
        stmt = select(EventPosition).order_by(EventPosition.created_at.desc())
        positions = [
            position
            for position in session.exec(stmt).all()
            if position.source in {"robinhood", "kalshi"} and self._is_same_local_day(self._position_timestamp(position))
        ]
        lines = ["<b>Finn Bets: Today</b>"]
        if not positions:
            lines.append("No prediction-market bets are recorded for today yet.")
            return "\n".join(lines)

        summaries = []
        for position in positions:
            summaries.append(await self._event_position_status(session, position))

        open_items = [item for item in summaries if item["status"] == "in_progress"]
        wins = [item for item in summaries if item["status"] == "won"]
        losses = [item for item in summaries if item["status"] == "lost"]

        total_staked = sum(float(item["stake"]) for item in summaries)
        open_stake = sum(float(item["stake"]) for item in open_items)
        realized_payout = sum(float(item["payout"] or 0.0) for item in wins)
        realized_profit = sum(float(item["profit"] or 0.0) for item in wins + losses)

        grouped_open_max: dict[tuple[str, str | None, str | None], float] = {}
        for item in open_items:
            key = (
                str(item["event_name"]),
                str(item.get("category") or "") or None,
                str(item.get("time_label") or "") or None,
            )
            payout = float(item["payout"] or 0.0)
            current = grouped_open_max.get(key, 0.0)
            if payout > current:
                grouped_open_max[key] = payout
        open_max_return = sum(grouped_open_max.values())
        open_max_profit = open_max_return - open_stake

        lines.append(f"Your cash put into bets today: <b>${total_staked:.2f}</b>")
        lines.append(f"Cash paid back so far: <b>${realized_payout:.2f}</b>")
        lines.append(f"Realized P/L so far: <b>${realized_profit:.2f}</b>")
        lines.append(f"Open stake: <b>${open_stake:.2f}</b>")
        lines.append(f"Open max return: <b>${open_max_return:.2f}</b>")
        lines.append(f"Open max profit if best-side outcomes hit: <b>${open_max_profit:.2f}</b>")
        lines.append(f"Open bets: <b>{len(open_items)}</b>")

        if not open_items:
            lines.append("")
            lines.append("No in-progress bets right now.")
            return "\n".join(lines)

        lines.append("")
        lines.append("<b>In Progress</b>")
        for idx, item in enumerate(open_items, start=1):
            lines.append(f"{idx}. <b>{escape(str(item['event_name']))}</b>")
            meta_bits = [bit for bit in (item.get("category"), item.get("time_label")) if bit]
            if meta_bits:
                lines.append(f"   {escape(' • '.join(str(bit) for bit in meta_bits))}")
            lines.append(f"   Pick: <b>{escape(str(item['pick']))}</b>")
            stake_line = f"Your cash in: <b>${float(item['stake']):.2f}</b>"
            if item["payout"] is not None:
                stake_line += f" • Max <b>${float(item['payout']):.2f}</b>"
            lines.append(f"   {stake_line}")
        return "\n".join(lines)

    async def format_event_performance(self, session: Session) -> str:
        await self._refresh_event_bet_outcomes(session)
        all_positions = {
            position.id: position
            for position in session.exec(select(EventPosition)).all()
            if position.source in {"robinhood", "kalshi"}
        }
        outcomes = [
            row
            for row in session.exec(select(EventBetOutcome).order_by(EventBetOutcome.checked_at.desc())).all()
            if row.event_position_id in all_positions
        ]
        lines = ["<b>Finn Bet Stats</b>"]
        if not outcomes:
            lines.append("No tracked prediction-market outcomes exist yet.")
            return "\n".join(lines)

        settled = [row for row in outcomes if row.outcome in {"won", "lost"}]
        open_rows = [row for row in outcomes if row.outcome == "in_progress"]
        wins = [row for row in settled if row.outcome == "won"]
        losses = [row for row in settled if row.outcome == "lost"]
        hit_rate = (len(wins) / len(settled) * 100.0) if settled else 0.0
        total_staked = sum(float(position.amount_usd) for position in all_positions.values())
        total_payout = sum(float(row.payout_usd or 0.0) for row in settled)
        total_profit = sum(float(row.profit_usd or 0.0) for row in settled)
        roi = (total_profit / total_staked * 100.0) if total_staked > 0 else 0.0

        lines.append(f"All-time cash in: <b>${total_staked:.2f}</b>")
        lines.append(f"Cash back: <b>${total_payout:.2f}</b>")
        lines.append(f"Realized P/L: <b>${total_profit:.2f}</b>")
        lines.append(f"ROI: <b>{roi:.1f}%</b>")
        lines.append(f"Hit rate: <b>{hit_rate:.1f}%</b>")
        lines.append(f"Wins: <b>{len(wins)}</b>")
        lines.append(f"Losses: <b>{len(losses)}</b>")
        lines.append(f"Open: <b>{len(open_rows)}</b>")

        recent_cutoff = self._local_now().date() - timedelta(days=6)
        daily: dict[datetime.date, dict[str, float]] = {}
        for row in settled:
            position = all_positions.get(row.event_position_id)
            if position is None:
                continue
            dt = self._effective_outcome_timestamp(position, row)
            local_day = self._local_date(dt)
            if local_day < recent_cutoff:
                continue
            bucket = daily.setdefault(local_day, {"profit": 0.0, "payout": 0.0, "count": 0.0})
            bucket["profit"] += float(row.profit_usd or 0.0)
            bucket["payout"] += float(row.payout_usd or 0.0)
            bucket["count"] += 1.0

        if daily:
            days = [recent_cutoff + timedelta(days=offset) for offset in range(7)]
            week_profit = sum(daily.get(day, {}).get("profit", 0.0) for day in days)
            week_payout = sum(daily.get(day, {}).get("payout", 0.0) for day in days)
            week_count = int(sum(daily.get(day, {}).get("count", 0.0) for day in days))
            winning_days = sum(1 for day in days if daily.get(day, {}).get("profit", 0.0) > 0)
            losing_days = sum(1 for day in days if daily.get(day, {}).get("profit", 0.0) < 0)
            avg_daily = week_profit / 7.0
            lines.append("")
            lines.append(f"Past 7 days realized P/L: <b>${week_profit:.2f}</b>")
            lines.append(f"Past 7 days average/day: <b>${avg_daily:.2f}</b>")
            lines.append(f"Past 7 days bets settled in window: <b>{week_count}</b>")
            lines.append(f"Past 7 days winning days: <b>{winning_days}</b>")
            lines.append(f"Past 7 days losing days: <b>{losing_days}</b>")
            lines.append(f"Past 7 days cash back: <b>${week_payout:.2f}</b>")
            lines.append("")
            lines.append("<b>Daily Realized P/L (by settlement day)</b>")
            for day in days:
                profit = daily.get(day, {}).get("profit", 0.0)
                count = int(daily.get(day, {}).get("count", 0.0))
                lines.append(f"{escape(self._format_local_date(day))}: <b>${profit:.2f}</b> • Bets settled that day <b>{count}</b>")
        return "\n".join(lines)

    async def format_batch(self, session: Session, batch_id: str) -> str:
        return "\n\n".join(await self.format_batch_messages(session, batch_id))

    async def _refresh_stock_recommendation_outcomes(self, session: Session) -> None:
        stmt = select(RecommendationOutcome).where(RecommendationOutcome.status == "open")
        open_outcomes = list(session.exec(stmt).all())
        if not open_outcomes:
            return
        symbols = sorted({outcome.symbol for outcome in open_outcomes if outcome.symbol})
        quote_map = await self.market_research.quote_map(session, symbols)
        changed = False
        for outcome in open_outcomes:
            quote = quote_map.get(outcome.symbol) or {}
            latest_price = float(quote.get("close") or 0.0)
            if latest_price <= 0 or outcome.baseline_price <= 0:
                continue
            outcome.latest_price = latest_price
            outcome.return_pct = round(((latest_price / outcome.baseline_price) - 1.0) * 100.0, 2)
            outcome.evaluated_at = datetime.now(UTC).replace(tzinfo=None)
            horizon_hours = (outcome.evaluated_at - outcome.created_at).total_seconds() / 3600.0
            if horizon_hours >= 48:
                outcome.status = "closed"
                outcome.notes = f"48h follow-through {outcome.return_pct:.2f}%"
            session.add(outcome)
            changed = True
        if changed:
            session.commit()

    async def _seed_stock_recommendation_outcomes(
        self,
        session: Session,
        batch: RecommendationBatch,
        stock_symbols: list[str],
    ) -> None:
        if not stock_symbols:
            return
        quote_map = await self.market_research.quote_map(session, stock_symbols)
        recommendations = [
            rec
            for rec in self.recommendations_for_batch(session, batch.id)
            if rec.recommendation_type in {"stock", "etf"} and rec.symbol and rec.side in {"buy", "hold", "watch", "sell"}
        ]
        for rec in recommendations:
            quote = quote_map.get(rec.symbol or "") or {}
            baseline_price = float(quote.get("close") or 0.0)
            if baseline_price <= 0:
                continue
            session.add(
                RecommendationOutcome(
                    recommendation_id=rec.id,
                    symbol=rec.symbol or "",
                    baseline_price=baseline_price,
                    latest_price=baseline_price,
                    return_pct=0.0,
                    status="open",
                    notes=f"baseline for {rec.side}",
                )
            )
        session.commit()

    async def create_live_batch(
        self,
        session: Session,
        batch_type: str = "premarket",
        *,
        include_stocks: bool = True,
        include_events: bool = True,
        force_event_recommendations: bool = False,
        event_limit: int = 8,
    ) -> RecommendationBatch:
        try:
            if self.portfolio_sync.adapter.configured():
                await self.portfolio_sync.sync(session)
        except Exception:
            pass
        if include_stocks:
            await self._refresh_stock_recommendation_outcomes(session)
        if include_events:
            await self._refresh_event_bet_outcomes(session)

        stock_ideas = []
        news_briefs = []
        event_ideas = []
        if include_stocks:
            stock_ideas, news_briefs = await self.market_research.stock_report(
                session,
                stock_limit=10,
                news_limit=3,
            )
        if include_events:
            event_ideas = await self.market_research.top_event_ideas(
                session,
                limit=event_limit,
                force_recommendations=force_event_recommendations,
            )
            if batch_type == "manual-event":
                try:
                    auto_ideas = await self.market_research.top_event_execution_ideas(
                        session,
                        limit=max(event_limit, self.settings.gx_batch_limit * 4),
                        include_live=True,
                    )
                except TypeError:
                    auto_ideas = await self.market_research.top_event_execution_ideas(
                        session,
                        limit=max(event_limit, self.settings.gx_batch_limit * 4),
                    )
                auto_contract_ids = {
                    str(idea.execution_contract_id or "")
                    for idea in auto_ideas
                    if str(idea.execution_contract_id or "")
                }
                if auto_contract_ids:
                    event_ideas = [
                        idea
                        for idea in event_ideas
                        if str(idea.execution_contract_id or "") not in auto_contract_ids
                        and float(idea.potential_profit_usd or 0.0) >= 1.0
                        and float(idea.amount_usd or 0.0) > self.settings.gx_max_stake_usd
                    ]
        if not stock_ideas and not event_ideas and not news_briefs:
            if include_stocks and not include_events:
                return self.create_placeholder_batch(session, batch_type=batch_type)
            batch = RecommendationBatch(batch_type=batch_type)
            session.add(batch)
            session.commit()
            session.refresh(batch)
            return batch

        batch = RecommendationBatch(batch_type=batch_type)
        session.add(batch)
        session.commit()
        session.refresh(batch)

        now = datetime.now(UTC).replace(tzinfo=None)
        item_number = 1
        stock_ranked = sorted(stock_ideas, key=lambda idea: idea.priority_score, reverse=True)
        news_ranked = sorted(news_briefs, key=lambda idea: idea.priority_score, reverse=True)
        event_ranked = sorted(event_ideas, key=lambda idea: idea.priority_score, reverse=True)

        ordered_ideas: list[tuple[str, object]] = []
        ordered_ideas.extend(("stock", idea) for idea in stock_ranked)
        ordered_ideas.extend(("news", idea) for idea in news_ranked)
        ordered_ideas.extend(("event", idea) for idea in event_ranked)

        for idea_type, idea in ordered_ideas:
            if idea_type == "stock":
                validity = now + timedelta(hours=self.settings.core_etf_validity_hours)
                if idea.side in {"buy", "sell"}:
                    validity = now + timedelta(minutes=self.settings.earnings_trade_validity_minutes)
                session.add(
                    Recommendation(
                        batch_id=batch.id,
                        item_number=item_number,
                        recommendation_type=idea.recommendation_type,
                        symbol=idea.symbol,
                        side=idea.side,
                        amount_usd=idea.amount_usd,
                        reason=idea.reason,
                        risk=idea.risk,
                        execution_mode="manual",
                        valid_until=validity,
                    )
                )
            elif idea_type == "news":
                session.add(
                    Recommendation(
                        batch_id=batch.id,
                        item_number=item_number,
                        recommendation_type="news",
                        symbol=idea.symbol,
                        side="info",
                        amount_usd=0.0,
                        reason=f"{idea.headline} ({idea.reason})",
                        risk="medium",
                        execution_mode="none",
                        valid_until=now + timedelta(hours=24),
                    )
                )
            else:
                session.add(
                    Recommendation(
                        batch_id=batch.id,
                        item_number=item_number,
                        recommendation_type="event",
                        symbol=f"{idea.title} || {idea.pick_label} || {idea.category} || {idea.time_label}",
                        event_code=idea.ticker,
                        side=idea.side.lower(),
                        amount_usd=idea.amount_usd,
                        reason=idea.reason,
                        risk=idea.risk,
                        execution_mode="manual",
                        valid_until=now + timedelta(minutes=self.settings.event_trade_validity_minutes),
                    )
                )
            item_number += 1
        session.commit()
        stock_symbols = [idea.symbol for idea in stock_ideas if idea.symbol]
        if stock_symbols:
            await self._seed_stock_recommendation_outcomes(session, batch, stock_symbols)
        return batch

    async def create_stock_batch(self, session: Session, batch_type: str = "stock-report") -> RecommendationBatch:
        return await self.create_live_batch(
            session,
            batch_type=batch_type,
            include_stocks=True,
            include_events=False,
        )

    async def create_event_batch(self, session: Session, batch_type: str = "event-report") -> RecommendationBatch:
        return await self.create_live_batch(
            session,
            batch_type=batch_type,
            include_stocks=False,
            include_events=True,
            force_event_recommendations=False,
            event_limit=self.settings.manual_event_report_limit,
        )

    def _monitor_state_key(self, idea) -> str:  # noqa: ANN001
        return f"event-monitor:{idea.ticker}:{idea.pick_label}"

    def _scout_state_key(self, idea) -> str:  # noqa: ANN001
        return f"event-scout:{idea.ticker}:{idea.pick_label}"

    def _load_alert_state(self, session: Session, provider: str, key: str) -> dict:
        row = session.exec(
            select(IntegrationState).where(
                IntegrationState.provider == provider,
                IntegrationState.key == key,
            )
        ).first()
        if not row or not row.value:
            return {}
        try:
            return json.loads(row.value)
        except Exception:
            return {}

    def _save_alert_state(self, session: Session, provider: str, key: str, payload: dict) -> None:
        row = session.exec(
            select(IntegrationState).where(
                IntegrationState.provider == provider,
                IntegrationState.key == key,
            )
        ).first()
        if row is None:
            row = IntegrationState(provider=provider, key=key, value="")
        row.value = json.dumps(payload)
        session.add(row)

    def _should_emit_monitor_idea(self, session: Session, idea) -> bool:  # noqa: ANN001
        key = self._monitor_state_key(idea)
        state = self._load_alert_state(session, "event_monitor", key)
        now = datetime.now(UTC).replace(tzinfo=None)
        if state:
            last_sent = state.get("sent_at")
            if last_sent:
                try:
                    sent_at = datetime.fromisoformat(last_sent)
                    age_minutes = (now - sent_at).total_seconds() / 60.0
                    if age_minutes < self.settings.event_alert_cooldown_minutes:
                        return False
                except ValueError:
                    pass
        return True

    def _mark_monitor_idea_sent(self, session: Session, idea) -> None:  # noqa: ANN001
        self._save_alert_state(
            session,
            "event_monitor",
            self._monitor_state_key(idea),
            {
                "sent_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
                "amount_usd": idea.amount_usd,
                "reason": idea.reason,
            },
        )

    def _should_emit_scout_idea(self, session: Session, idea) -> bool:  # noqa: ANN001
        key = self._scout_state_key(idea)
        state = self._load_alert_state(session, "event_scout", key)
        now = datetime.now(UTC).replace(tzinfo=None)
        if state:
            last_sent = state.get("sent_at")
            if last_sent:
                try:
                    sent_at = datetime.fromisoformat(last_sent)
                    age_minutes = (now - sent_at).total_seconds() / 60.0
                    if age_minutes < self.settings.event_scout_cooldown_minutes:
                        return False
                except ValueError:
                    pass
        return True

    def _mark_scout_idea_sent(self, session: Session, idea) -> None:  # noqa: ANN001
        self._save_alert_state(
            session,
            "event_scout",
            self._scout_state_key(idea),
            {
                "sent_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
                "amount_usd": idea.amount_usd,
                "reason": idea.reason,
            },
        )

    async def create_event_monitor_batch(self, session: Session, batch_type: str = "event-monitor") -> RecommendationBatch | None:
        try:
            await self._refresh_event_bet_outcomes(session)
        except Exception:
            return None
        followups = await self.market_research.monitored_event_followups(
            session,
            limit=self.settings.event_follow_up_limit,
        )
        followups = [idea for idea in followups if self._should_emit_monitor_idea(session, idea)]
        if not followups:
            return None

        batch = RecommendationBatch(batch_type=batch_type)
        session.add(batch)
        session.commit()
        session.refresh(batch)
        now = datetime.now(UTC).replace(tzinfo=None)
        for idx, idea in enumerate(followups, start=1):
            session.add(
                Recommendation(
                    batch_id=batch.id,
                    item_number=idx,
                    recommendation_type="event",
                    symbol=f"{idea.title} || {idea.pick_label} || {idea.category} || {idea.time_label}",
                    event_code=idea.ticker,
                    side=idea.side.lower(),
                    amount_usd=idea.amount_usd,
                    reason=idea.reason,
                    risk=idea.risk,
                    execution_mode="manual",
                    valid_until=now + timedelta(minutes=self.settings.event_trade_validity_minutes),
                )
            )
            self._mark_monitor_idea_sent(session, idea)
        session.commit()
        return batch

    async def create_event_scout_batch(self, session: Session, batch_type: str = "event-scout") -> RecommendationBatch | None:
        try:
            if self.portfolio_sync.adapter.configured():
                await self.portfolio_sync.sync(session)
        except Exception:
            pass
        try:
            await self._refresh_event_bet_outcomes(session)
        except Exception:
            return None
        ideas = await self.market_research.top_event_ideas(
            session,
            limit=self.settings.event_scout_limit,
            force_recommendations=True,
        )
        ideas = [idea for idea in ideas if self._should_emit_scout_idea(session, idea)]
        if not ideas:
            return None

        batch = RecommendationBatch(batch_type=batch_type)
        session.add(batch)
        session.commit()
        session.refresh(batch)
        now = datetime.now(UTC).replace(tzinfo=None)
        for idx, idea in enumerate(ideas, start=1):
            session.add(
                Recommendation(
                    batch_id=batch.id,
                    item_number=idx,
                    recommendation_type="event",
                    symbol=f"{idea.title} || {idea.pick_label} || {idea.category} || {idea.time_label}",
                    event_code=idea.ticker,
                    side=idea.side.lower(),
                    amount_usd=idea.amount_usd,
                    reason=idea.reason,
                    risk=idea.risk,
                    execution_mode="manual",
                    valid_until=now + timedelta(minutes=self.settings.event_trade_validity_minutes),
                )
            )
            self._mark_scout_idea_sent(session, idea)
        session.commit()
        return batch

    async def build_gx_preview(self, session: Session) -> str:
        batch = self.latest_gx_batch(session)
        if batch is None:
            return "No `gx` batch exists yet. Run `gx` first."
        recommendations = [
            rec
            for rec in self.recommendations_for_batch(session, batch.id)
            if rec.recommendation_type == "event"
            and rec.execution_mode in {"robinhood_auto_candidate", "kalshi_auto_candidate"}
        ]
        if not recommendations:
            return "The latest `gx` batch has no executable items."

        contract_ids = [self._event_meta(rec.symbol).get("execution_contract_id") for rec in recommendations]
        contract_ids = [contract_id for contract_id in contract_ids if contract_id]
        quote_rows = await self.market_research.robinhood_prediction.get_marketdata_quotes(contract_ids)
        if not quote_rows:
            refreshed = await self.create_gx_batch(session, batch_type="gx", include_live=True)
            if refreshed.id != batch.id:
                batch = refreshed
                recommendations = [
                    rec
                    for rec in self.recommendations_for_batch(session, batch.id)
                    if rec.recommendation_type == "event"
                    and rec.execution_mode in {"robinhood_auto_candidate", "kalshi_auto_candidate"}
                ]
                contract_ids = [self._event_meta(rec.symbol).get("execution_contract_id") for rec in recommendations]
                contract_ids = [contract_id for contract_id in contract_ids if contract_id]
                quote_rows = await self.market_research.robinhood_prediction.get_marketdata_quotes(contract_ids)
            if not quote_rows:
                quote_rows = {}

        preview_items: list[dict[str, object]] = []
        now_local = self._local_now()
        for rec in recommendations:
            meta = self._event_meta(rec.symbol)
            market_ticker = str(meta.get("execution_market_ticker") or "")
            event_ticker = str(meta.get("execution_event_ticker") or rec.event_code or "")
            contract_id = str(meta.get("execution_contract_id") or "")
            quote = quote_rows.get(contract_id)
            event_name, pick, category, time_label = self._event_display(
                rec.recommendation_type,
                rec.symbol,
                rec.event_code,
                rec.reason,
            )
            yes_ask = float((quote or {}).get("yes_ask_price") or 0.0)
            if yes_ask <= 0 or yes_ask >= 1:
                yes_ask = self._parse_labeled_probability(str(rec.reason), "Entry") or 0.0
            if yes_ask <= 0 or yes_ask >= 1:
                continue
            count = self._parse_contract_count(str(rec.reason)) or self.market_research._event_contract_count(
                float(rec.amount_usd), yes_ask
            )
            if count <= 0:
                continue
            estimated_cost = round(count * yes_ask, 2)
            max_return = round(float(count), 2)
            potential_profit = round(max_return - estimated_cost, 2)
            if potential_profit + 0.05 < self.settings.gx_min_profit_usd:
                continue
            starts_at = None
            starts_at_raw = meta.get("event_starts_at")
            if starts_at_raw:
                try:
                    starts_at = datetime.fromisoformat(str(starts_at_raw))
                    if starts_at.tzinfo is None:
                        starts_at = starts_at.replace(tzinfo=ZoneInfo(self.settings.local_timezone))
                    else:
                        starts_at = starts_at.astimezone(ZoneInfo(self.settings.local_timezone))
                except ValueError:
                    starts_at = None
            if starts_at is None and time_label and time_label != "Time TBD":
                try:
                    parsed = datetime.strptime(time_label, "%I:%M %p")
                    starts_at = now_local.replace(
                        hour=parsed.hour,
                        minute=parsed.minute,
                        second=0,
                        microsecond=0,
                    )
                except ValueError:
                    starts_at = None
            if starts_at is not None:
                seconds_to_start = (starts_at - now_local).total_seconds()
                if seconds_to_start <= -4 * 3600:
                    continue
            preview_items.append(
                {
                    "recommendation_id": rec.id,
                    "item_number": rec.item_number,
                    "market_ticker": market_ticker,
                    "event_ticker": event_ticker,
                    "contract_id": contract_id,
                    "event_name": event_name,
                    "pick": pick,
                    "category": category,
                    "time_label": time_label,
                    "contract_path": meta.get("contract_path"),
                    "entry_price": round(yes_ask, 4),
                    "count": count,
                    "estimated_cost": estimated_cost,
                    "max_return": max_return,
                    "potential_profit": potential_profit,
                    "reason": rec.reason,
                }
            )

        if not preview_items:
            return "No `gx` items are currently executable after live quote, payout, and budget checks."

        preview = {
            "batch_id": batch.id,
            "created_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "expires_at": (datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=self.settings.gx_preview_ttl_minutes)).isoformat(),
            "total_estimated_cost": round(sum(float(item["estimated_cost"]) for item in preview_items), 2),
            "total_max_return": round(sum(float(item["max_return"]) for item in preview_items), 2),
            "total_potential_profit": round(sum(float(item["potential_profit"]) for item in preview_items), 2),
            "items": preview_items,
        }
        self._save_json_state(session, self._gx_provider(), "active_preview", preview)

        lines = [
            "<b>Finn Execution Preview: gx</b>",
            f"Preview expires at: <b>{escape(self._format_local_time(datetime.fromisoformat(preview['expires_at'])))}</b>",
            f"Estimated cash to use: <b>${float(preview['total_estimated_cost']):.2f}</b>",
            f"Estimated max return: <b>${float(preview['total_max_return']):.2f}</b>",
            f"Estimated potential profit: <b>${float(preview['total_potential_profit']):.2f}</b>",
            "",
            "<b>Orders to place</b>",
        ]
        for item in preview_items:
            lines.append(f"{int(item['item_number'])}. <b>{escape(str(item['event_name']))}</b>")
            meta_bits = [bit for bit in (item.get("category"), item.get("time_label")) if bit]
            if meta_bits:
                lines.append(f"   {escape(' • '.join(str(bit) for bit in meta_bits))}")
            if item.get("contract_path"):
                lines.append(f"   Robinhood path: <b>{escape(str(item['contract_path']))}</b>")
            lines.append(f"   Pick: <b>{escape(str(item['pick']))}</b>")
            lines.append(
                f"   Buy <b>{int(item['count'])}</b> contract(s) at about <b>${float(item['entry_price']):.4f}</b> each"
            )
            lines.append(
                f"   Cash <b>${float(item['estimated_cost']):.2f}</b> • Max <b>${float(item['max_return']):.2f}</b> • Profit <b>${float(item['potential_profit']):.2f}</b>"
            )
        lines.extend(["", "Reply with <code>x confirm</code> to place these small bets.", "Reply with <code>x cancel</code> to discard this preview."])
        return "\n".join(lines)

    def gx_preview_status(self, session: Session) -> str:
        preview = self._load_json_state(session, self._gx_provider(), "active_preview")
        if not preview:
            return "No active `gx` execution preview exists."
        lines = ["<b>Finn Execution Status: gx</b>"]
        expires_at = preview.get("expires_at")
        if expires_at:
            lines.append(f"Preview expires at: <b>{escape(self._format_local_time(datetime.fromisoformat(str(expires_at))))}</b>")
        lines.append(f"Items: <b>{len(preview.get('items') or [])}</b>")
        lines.append(f"Estimated cash: <b>${float(preview.get('total_estimated_cost') or 0.0):.2f}</b>")
        lines.append(f"Estimated max return: <b>${float(preview.get('total_max_return') or 0.0):.2f}</b>")
        lines.append(f"Estimated potential profit: <b>${float(preview.get('total_potential_profit') or 0.0):.2f}</b>")
        return "\n".join(lines)

    def cancel_gx_preview(self, session: Session) -> str:
        preview = self._load_json_state(session, self._gx_provider(), "active_preview")
        if not preview:
            return "No active `gx` execution preview exists."
        self._save_json_state(session, self._gx_provider(), "active_preview", None)
        return "Cleared the active `gx` execution preview."

    async def confirm_gx_preview(self, session: Session) -> str:
        preview = self._load_json_state(session, self._gx_provider(), "active_preview")
        if not preview:
            rebuilt = await self.build_gx_preview(session)
            if rebuilt.startswith("<b>Finn Execution Preview: gx</b>"):
                return rebuilt
            return "No active `gx` preview exists. Run `gx`, then `x` first."
        if not self.market_research.robinhood_prediction.can_write():
            return (
                "Robinhood write auth is not configured for `gx` execution. "
                "Run `PYTHONPATH=. .venv/bin/python scripts/robinhood_write_login.py` first."
            )
        expires_at = preview.get("expires_at")
        if expires_at and datetime.now(UTC).replace(tzinfo=None) > datetime.fromisoformat(str(expires_at)):
            self._save_json_state(session, self._gx_provider(), "active_preview", None)
            return "The active `gx` preview expired. Run `gx` and `x` again."

        buying_power = self.market_research._current_buying_power(session)
        if buying_power < float(preview.get("total_estimated_cost") or 0.0):
            return (
                "Robinhood buying power is too low for this `gx` preview. "
                f"Available: ${buying_power:.2f} • Needed: ${float(preview.get('total_estimated_cost') or 0.0):.2f}"
            )

        contract_ids = [str(item.get("contract_id") or "") for item in preview.get("items") or []]
        quote_rows = await self.market_research.robinhood_prediction.get_marketdata_quotes(contract_ids)
        execution_rows: list[dict[str, object]] = []
        skipped_rows: list[str] = []
        running_total = 0.0
        for item in preview.get("items") or []:
            quote = quote_rows.get(str(item.get("contract_id") or ""))
            if not quote:
                skipped_rows.append(f"{item.get('event_name')}: live quote unavailable")
                continue
            latest_yes_ask = float(quote.get("yes_ask_price") or 0.0)
            if latest_yes_ask <= 0 or latest_yes_ask >= 1:
                skipped_rows.append(f"{item.get('event_name')}: market became untradable")
                continue
            count = int(item.get("count") or 0)
            if count <= 0:
                skipped_rows.append(f"{item.get('event_name')}: executable size disappeared")
                continue
            latest_estimated_cost = round(count * latest_yes_ask, 2)
            latest_max_return = round(float(count), 2)
            latest_potential_profit = round(latest_max_return - latest_estimated_cost, 2)
            if latest_potential_profit + 0.05 < self.settings.gx_min_profit_usd:
                skipped_rows.append(f"{item.get('event_name')}: payout fell below the gx profit floor")
                continue
            if running_total + latest_estimated_cost > buying_power:
                skipped_rows.append(f"{item.get('event_name')}: buying power is no longer enough")
                continue
            running_total += latest_estimated_cost
            execution_rows.append(
                {
                    **item,
                    "latest_yes_ask": round(latest_yes_ask, 4),
                    "latest_estimated_cost": latest_estimated_cost,
                    "latest_max_return": latest_max_return,
                    "latest_potential_profit": latest_potential_profit,
                }
            )
        if not execution_rows:
            if skipped_rows:
                return "No `gx` orders remained executable.\n" + "\n".join(
                    f"• {escape(line)}" for line in skipped_rows[:6]
                )
            return "No `gx` orders remained executable."

        created_at = datetime.now(UTC).replace(tzinfo=None)
        event_account = await self.market_research.robinhood_prediction.get_event_contract_account()
        event_account_id = str((event_account or {}).get("id") or "")
        lines = ["<b>Finn Execution Result: gx</b>"]
        for item in execution_rows:
            recommendation = session.get(Recommendation, str(item["recommendation_id"]))
            if recommendation is None:
                continue
            client_order_id = str(uuid4())
            try:
                response = await self.market_research.robinhood_prediction.create_event_contract_order(
                    contract_id=str(item["contract_id"]),
                    quantity=int(item["count"]),
                    limit_price=float(item["latest_yes_ask"]),
                    ref_id=client_order_id,
                    entered_notional_amount=round(float(item["latest_estimated_cost"]), 2),
                )
            except Exception as exc:  # noqa: BLE001
                lines.append(f"• <b>{escape(str(item['event_name']))}</b>: failed to place — {escape(str(exc))}")
                continue
            order_id = str(response.get("orderId") or response.get("id") or "")
            latest_response = dict(response)
            if event_account_id and order_id:
                for _ in range(4):
                    await asyncio.sleep(1)
                    latest_order = await self.market_research.robinhood_prediction.get_event_order(
                        account_id=event_account_id,
                        order_id=order_id,
                    )
                    if latest_order:
                        latest_response = latest_order
                    derived_state = str(
                        latest_response.get("derivedState") or latest_response.get("orderState") or ""
                    ).upper()
                    latest_filled = float(latest_response.get("filledQuantity") or 0.0)
                    if latest_filled > 0 or derived_state in {"FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "FAILED"}:
                        break

            fill_count = int(float(latest_response.get("filledQuantity") or 0.0))
            average_fill_price = float(latest_response.get("averagePrice") or response.get("averagePrice") or item["latest_yes_ask"] or 0.0)
            estimated_cost = round(fill_count * average_fill_price, 2)
            max_return = round(float(fill_count), 2)
            potential_profit = round(max_return - estimated_cost, 2)
            derived_state = str(
                latest_response.get("derivedState") or latest_response.get("orderState") or response.get("orderState") or ""
            ).upper()
            is_final_without_fill = fill_count <= 0 and derived_state in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "FAILED"}
            session.add(
                Execution(
                    recommendation_id=recommendation.id,
                    provider="robinhood_auto",
                    provider_order_id=order_id or client_order_id,
                    status="filled" if fill_count > 0 else ("canceled" if is_final_without_fill else "submitted"),
                    details=json.dumps(latest_response),
                )
            )
            if fill_count > 0:
                position = EventPosition(
                    event_code=str(item["event_ticker"]),
                    side="yes",
                    amount_usd=estimated_cost,
                    source_recommendation_id=recommendation.id,
                    source="robinhood",
                    broker_order_id=order_id or client_order_id,
                    broker_contract_id=str(item["contract_id"]),
                    broker_event_id=str(item["event_ticker"]),
                    quantity=float(fill_count),
                    avg_fill_price=average_fill_price,
                    event_title=str(item["event_name"]),
                    pick_label=str(item["pick"]),
                    category=str(item.get("category") or ""),
                    time_label=str(item.get("time_label") or ""),
                    opened_at=created_at,
                    status="broker_filled",
                )
                session.add(position)
                session.commit()
                session.refresh(position)
                session.add(
                    EventBetOutcome(
                        event_position_id=position.id,
                        event_code=position.event_code,
                        outcome="in_progress",
                        payout_usd=max_return,
                        profit_usd=potential_profit,
                        implied_probability=average_fill_price,
                        checked_at=created_at,
                        notes="Robinhood gx execution",
                    )
                )
                recommendation.status = "executed"
                session.add(recommendation)
                lines.append(
                    f"• <b>{escape(str(item['event_name']))}</b>: filled <b>{fill_count}</b> at <b>${average_fill_price:.4f}</b> "
                    f"for <b>${estimated_cost:.2f}</b>"
                )
            elif is_final_without_fill:
                recommendation.status = "skipped"
                session.add(recommendation)
                lines.append(
                    f"• <b>{escape(str(item['event_name']))}</b>: accepted, then finished <b>{escape(derived_state.lower())}</b> with no fill"
                )
            else:
                position = EventPosition(
                    event_code=str(item["event_ticker"]),
                    side="yes",
                    amount_usd=round(float(item["latest_estimated_cost"]), 2),
                    source_recommendation_id=recommendation.id,
                    source="robinhood",
                    broker_order_id=order_id or client_order_id,
                    broker_contract_id=str(item["contract_id"]),
                    broker_event_id=str(item["event_ticker"]),
                    event_title=str(item["event_name"]),
                    pick_label=str(item["pick"]),
                    category=str(item.get("category") or ""),
                    time_label=str(item.get("time_label") or ""),
                    opened_at=created_at,
                    status="broker_order_open",
                )
                session.add(position)
                recommendation.status = "submitted"
                session.add(recommendation)
                lines.append(f"• <b>{escape(str(item['event_name']))}</b>: submitted and still waiting for fills")
            session.commit()

        if skipped_rows:
            lines.append("• Some gx items were skipped during confirm because live quotes or payout math changed.")
            for line in skipped_rows[:4]:
                lines.append(f"  - {escape(line)}")

        self._save_json_state(session, self._gx_provider(), "active_preview", None)
        return "\n".join(lines)

    def latest_batch(self, session: Session) -> RecommendationBatch | None:
        stmt = select(RecommendationBatch).order_by(RecommendationBatch.created_at.desc())
        return session.exec(stmt).first()

    def batch_for_report_message(
        self,
        session: Session,
        *,
        external_message_id: str,
        destination: str | None = None,
    ) -> RecommendationBatch | None:
        stmt = select(ReportLog).where(ReportLog.external_message_id == external_message_id)
        if destination is not None:
            stmt = stmt.where(ReportLog.destination == destination)
        stmt = stmt.order_by(ReportLog.created_at.desc())
        report_log = session.exec(stmt).first()
        if not report_log:
            return None
        return session.get(RecommendationBatch, report_log.batch_id)

    def latest_recommendations(self, session: Session) -> list[Recommendation]:
        batch = self.latest_batch(session)
        if not batch:
            return []
        stmt = (
            select(Recommendation)
            .where(Recommendation.batch_id == batch.id)
            .order_by(Recommendation.item_number)
        )
        return list(session.exec(stmt).all())

    def recommendations_for_batch(self, session: Session, batch_id: str) -> list[Recommendation]:
        stmt = (
            select(Recommendation)
            .where(Recommendation.batch_id == batch_id)
            .order_by(Recommendation.item_number)
        )
        return list(session.exec(stmt).all())

    def resolve_item_numbers(
        self,
        session: Session,
        targets: list[str],
        *,
        preferred_batch_id: str | None = None,
    ) -> list[Recommendation]:
        recommendations = (
            self.recommendations_for_batch(session, preferred_batch_id)
            if preferred_batch_id
            else self.latest_recommendations(session)
        )
        if not recommendations:
            return []
        if any(target == "all" for target in targets):
            return recommendations

        item_numbers: list[int] = []
        for target in targets:
            try:
                item_numbers.append(int(target))
            except ValueError:
                continue
        if not item_numbers:
            return []
        item_number_set = set(item_numbers)
        return [rec for rec in recommendations if rec.item_number in item_number_set]
