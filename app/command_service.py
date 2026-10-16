from __future__ import annotations

from html import escape

from sqlmodel import Session, select

from app.adapters.snaptrade import SnapTradeAdapter
from app.config import get_settings
from app.models import Approval, InboundMessage, Recommendation, RecommendationBatch
from app.parser import ParsedCommand, parse_command
from app.services.portfolio_sync import PortfolioSyncService
from app.services.recommendation_service import RecommendationService
from app.services.agentic_observer import AgenticObserverService
from app.telegram_bot import TelegramBot


HELP_TEXT = (
    "<b>Finn Commands</b>\n"
    "You can type commands with or without <code>/</code>. Short aliases are preferred.\n\n"
    "<b>Reports</b>\n"
    "• <code>r</code> = <code>report</code>: Stock / ETF report.\n"
    "• <code>ask NVDA</code>: Deep analysis on one ticker or company.\n"
    "• <code>g</code> = <code>gamble</code>: Medium/high-stake prediction-market picks.\n"
    "• <code>gs</code>: Small executable prediction bets.\n"
    "• <code>gsl</code> = <code>gamble slate</code>: Full same-day prediction slate.\n"
    "• <code>b</code> = <code>bets</code>: Today's bet status and payouts.\n"
    "• <code>bs</code> = <code>betstats</code>: All-time bet stats.\n"
    "• <code>l</code> = <code>latest</code>: Latest saved batch.\n"
    "• <code>pd</code> = <code>pending</code>: Pending items.\n"
    "• <code>pf</code> = <code>portfolio</code>: Portfolio snapshot.\n"
    "• <code>agent</code>: Read-only Agentic account observer.\n"
    "• <code>st</code> = <code>status</code>: Bot status.\n\n"
    "<b>Actions</b>\n"
    "• <code>g x 1 3</code>: Stage selected <code>g</code> items into <code>gx</code>.\n"
    "• <code>gs x 2 5</code>: Stage selected <code>gs</code> items into <code>gx</code>.\n"
    "• <code>y</code> / <code>n</code>: Approve or reject proactive position-change alerts.\n"
    "• <code>dn 3</code>: Deny item 3.\n"
    "• <code>sk 4</code>: Skip item 4.\n"
    "• <code>x</code>: Build the final <code>gx</code> execution preview.\n"
    "• <code>x c</code> (or <code>x confirm</code>): Place the active <code>gx</code> previewed bets through Robinhood.\n"
    "• <code>x status</code>: Show the active <code>gx</code> preview.\n"
    "• <code>x cancel</code>: Discard the active <code>gx</code> preview.\n"
    "• Trades and event bets no longer need <code>approve</code> or <code>executed</code>; Finn now checks Robinhood directly.\n\n"
    "<b>Sync</b>\n"
    "• <code>sy</code> = <code>sync</code>: Refresh Robinhood data.\n"
    "• <code>c</code> = <code>connect</code>: New SnapTrade link.\n"
    "• <code>ab</code> = <code>about</code>: How Finn works.\n"
)

ABOUT_TEXT = (
    "<b>About Finn</b>\n"
    "Finn is a Telegram-only personal market bot.\n\n"
    "<b>What it does</b>\n"
    "• Stock side: Finn syncs your Robinhood portfolio, scans a broader discovery universe, scores names on trend, fundamentals, catalysts, and risk, then uses Gemini to classify them as hold, buy, reduce, exit, or watch.\n"
    "• Stock strategy: it is built to find rerating setups, not random hype. It prefers improving trends, strong earnings or filing signals, and avoids repeatedly telling you to buy the same name after you already acted.\n"
    "• Prediction side: Finn now reads Robinhood’s prediction-market catalog directly, estimates fair probability versus market implied probability, ranks same-day and near-term events by event type, pricing, timing, liquidity, and edge, then sizes manual bets from your buying power.\n"
    "• Prediction strategy: it prefers readable winner-style events, avoids blindly re-suggesting bets you already hold, monitors your open Robinhood event positions for material probability moves, tracks wins/losses/payouts from broker data when available, and uses settled history to slightly adjust future event ranking and stake sizing.\n"
    "• Auto-execution strategy: <code>gx</code> is Finn's execution lane. <code>gs</code> lists the low-stake executable board, while <code>g x ...</code> and <code>gs x ...</code> stage selected event items into <code>gx</code> for Robinhood execution.\n"
    "• Both sides share account-state awareness through buying power, but the stock and prediction report paths now run independently.\n\n"
    "<b>How commands behave</b>\n"
    "• <code>r</code> creates a stock-only batch.\n"
    "• <code>g</code> creates the medium/high-stake manual event batch.\n"
    "• <code>gs</code> creates the low-stake executable event batch.\n"
    "• <code>g x 2 5</code> stages selected medium/high-stake items into <code>gx</code>.\n"
    "• <code>gs x 2 5</code> stages selected small-stake items into <code>gx</code>.\n"
    "• <code>gsl</code> / <code>gamble slate</code> shows the full readable prediction slate without creating trade items.\n"
    "• <code>b</code> summarizes today's executed prediction-market bets and their current outcomes.\n"
    "• <code>bs</code> summarizes all-time settled prediction-market performance Finn has tracked.\n"
    "• <code>x</code> builds a live execution preview from the latest <code>gx</code> batch using current Robinhood quotes and contract counts.\n"
    "• <code>x c</code> / <code>x confirm</code> only places the previewed <code>gx</code> bets if the preview is still fresh, quotes have not drifted too far, Robinhood buying power can fund the batch, and Robinhood write auth has been bootstrapped.\n"
    "• <code>l</code> shows the newest batch again.\n"
    "• <code>dn</code> and <code>sk</code> update item state in the local database.\n"
    "• <code>approve</code> and <code>executed</code> are deprecated because Finn now infers fills and open event positions from Robinhood data.\n"
    "• If you reply directly to a report message, Finn uses that report as the reference batch.\n"
    "• You do not need to type <code>/</code> before commands.\n\n"
    "<b>Current limits</b>\n"
    "• Stock trades are still manual.\n"
    "• Robinhood prediction execution uses a private mobile-style event-order surface and can break if Robinhood changes it.\n"
    "• Finn reads Robinhood prediction-market positions and settlements directly when your local Robinhood browser session is available.\n"
    "• Robinhood write auth is separate from the browser read session. If you have not run <code>scripts/robinhood_write_login.py</code> yet, Finn stays read-only for <code>gx</code> confirms.\n"
    "• Stock execution detection depends on Robinhood/SnapTrade positions and balances updating after your trade settles; open stock orders are still not a first-class synced object.\n"
    "• SEC filing access is configured through <code>SEC_USER_AGENT</code>; if you already added your real email there, you are fine."
)


class CommandService:
    def __init__(self, bot: TelegramBot) -> None:
        self.bot = bot
        self.settings = get_settings()
        self.snaptrade = SnapTradeAdapter()
        self.portfolio_sync = PortfolioSyncService(self.snaptrade)
        self.recommendations = RecommendationService()
        self.agentic_observer = AgenticObserverService()

    def is_authorized_chat(self, chat_id: str | None) -> bool:
        configured = self.settings.telegram_chat_id
        return bool(configured and configured.strip() and chat_id == configured)

    async def handle_message(
        self,
        session: Session,
        *,
        chat_id: str,
        sender_id: str | None,
        external_message_id: str,
        text: str,
        reply_to_external_message_id: str | None = None,
    ) -> str:
        if not self.is_authorized_chat(chat_id):
            raise PermissionError("Telegram chat is not authorized")

        inbound = InboundMessage(
            external_message_id=external_message_id,
            sender_id=sender_id,
            chat_id=chat_id,
            text=text,
        )
        session.add(inbound)
        session.commit()

        parsed = parse_command(text)
        if parsed is None:
            return HELP_TEXT

        return await self._apply_command(
            session,
            parsed,
            chat_id=chat_id,
            source_message_id=external_message_id,
            reply_to_external_message_id=reply_to_external_message_id,
        )

    def _pending_control_batch_for_reply(
        self,
        session: Session,
        *,
        reply_to_external_message_id: str | None,
        chat_id: str,
    ) -> RecommendationBatch | None:
        if not reply_to_external_message_id:
            return None
        batch = self.recommendations.batch_for_report_message(
            session,
            external_message_id=reply_to_external_message_id,
            destination=chat_id,
        )
        if batch and batch.batch_type == "event-control":
            return batch
        return None

    def _latest_pending_control_batch(self, session: Session) -> RecommendationBatch | None:
        batches = session.exec(
            select(RecommendationBatch)
            .where(RecommendationBatch.batch_type == "event-control")
            .order_by(RecommendationBatch.created_at.desc())
        ).all()
        for batch in batches:
            pending = session.exec(
                select(Recommendation).where(
                    Recommendation.batch_id == batch.id,
                    Recommendation.status == "pending",
                )
            ).first()
            if pending is not None:
                return batch
        return None

    async def _handle_position_control_gate(
        self,
        session: Session,
        *,
        approve: bool,
        targets: list[str],
        chat_id: str,
        source_message_id: str,
        reply_to_external_message_id: str | None,
    ) -> str:
        control_batch = self._pending_control_batch_for_reply(
            session,
            reply_to_external_message_id=reply_to_external_message_id,
            chat_id=chat_id,
        )
        if control_batch is None:
            control_batch = self._latest_pending_control_batch(session)
        if control_batch is None:
            return "No pending Finn position-change alert is waiting for `y` or `n`."
        resolved_targets = targets or ["all"]
        recommendations = self.recommendations.resolve_item_numbers(
            session,
            resolved_targets,
            preferred_batch_id=control_batch.id,
        )
        if not recommendations:
            return "Could not resolve those alert item numbers. Reply with `y`, `n`, `y 1 2`, or `n 1 2`."
        recommendations = [rec for rec in recommendations if rec.status == "pending"]
        if not recommendations:
            return "Those alert items were already handled."
        if not approve:
            for recommendation in recommendations:
                session.add(Approval(recommendation_id=recommendation.id, action="rejected", source_message_id=source_message_id))
                recommendation.status = "rejected"
                session.add(recommendation)
            session.commit()
            item_numbers = ", ".join(str(r.item_number) for r in recommendations)
            return f"Recorded `n` for item(s): {item_numbers}"
        result = await self.recommendations.execute_position_control_recommendations(session, recommendations)
        for recommendation in recommendations:
            session.add(Approval(recommendation_id=recommendation.id, action="approved", source_message_id=source_message_id))
            if recommendation.status == "pending":
                recommendation.status = "approved"
            session.add(recommendation)
        session.commit()
        return result
    async def _apply_command(
        self,
        session: Session,
        parsed: ParsedCommand,
        *,
        chat_id: str,
        source_message_id: str,
        reply_to_external_message_id: str | None,
    ) -> str:
        if parsed.action == "accept":
            return await self._handle_position_control_gate(
                session,
                approve=True,
                targets=parsed.targets,
                chat_id=chat_id,
                source_message_id=source_message_id,
                reply_to_external_message_id=reply_to_external_message_id,
            )
        if parsed.action == "reject":
            return await self._handle_position_control_gate(
                session,
                approve=False,
                targets=parsed.targets,
                chat_id=chat_id,
                source_message_id=source_message_id,
                reply_to_external_message_id=reply_to_external_message_id,
            )
        if parsed.action == "status":
            latest_batch = self.recommendations.latest_batch(session)
            recommendations = self.recommendations.latest_recommendations(session)
            pending_count = sum(1 for rec in recommendations if rec.status == "pending")
            return (
                "Finn is running.\n"
                f"- Telegram configured: {bool(self.settings.telegram_bot_token and self.settings.telegram_chat_id)}\n"
                f"- SnapTrade configured: {self.snaptrade.configured()}\n"
                f"- Twelve Data configured: {bool(self.settings.effective_twelve_data_key)}\n"
                f"- Gemini configured: {bool(self.settings.gemini_api_key)}\n"
                f"- Kalshi configured: {bool(self.settings.kalshi_api_id and self.settings.effective_kalshi_private_key)}\n"
                f"- SEC user-agent configured: {bool(self.settings.sec_user_agent)}\n"
                f"- Latest batch: {latest_batch.batch_type if latest_batch else 'none'}\n"
                f"- Pending recommendations: {pending_count}"
            )
        if parsed.action == "help":
            return HELP_TEXT
        if parsed.action == "about":
            return ABOUT_TEXT
        if parsed.action == "ask":
            query = " ".join(parsed.targets).strip()
            if not query:
                return "Use `ask <ticker or company>`, for example `ask RKLB` or `ask Exxon`."
            return await self.recommendations.ask_stock(session, query)
        if parsed.action == "portfolio":
            return "\n".join(["Portfolio Snapshot", *self.recommendations.market_research.portfolio_lines(session)])
        if parsed.action == "agent":
            return self.agentic_observer.format_agent_report(session)
        if parsed.action == "latest":
            latest_batch = self.recommendations.latest_batch(session)
            if latest_batch is None:
                return "No recommendation batch exists yet. Send `report` first."
            return await self.recommendations.format_batch(session, latest_batch.id)
        if parsed.action == "pending":
            latest_batch = self.recommendations.latest_batch(session)
            if latest_batch is None:
                return "No recommendation batch exists yet. Send `report` first."
            recommendations = [
                rec
                for rec in self.recommendations.latest_recommendations(session)
                if rec.status == "pending"
            ]
            if not recommendations:
                return "No pending recommendation items in the latest batch."
            lines = ["<b>Pending Items</b>"]
            for rec in recommendations:
                target = escape(rec.symbol or rec.event_code or "UNKNOWN")
                lines.append(
                    f"{rec.item_number}. <b>{rec.side.upper()}</b> {target} — ${rec.amount_usd:.2f} ({escape(rec.risk)})"
                )
            return "\n".join(lines)
        if parsed.action == "connect":
            if not self.snaptrade.configured():
                return "SnapTrade is not configured."
            url = await self.snaptrade.generate_connection_portal_url(session)
            return f"Open this Robinhood connection URL:\n{url}"
        if parsed.action == "sync":
            if not self.snaptrade.configured():
                return "SnapTrade is not configured."
            try:
                return await self.portfolio_sync.sync(session)
            except Exception as exc:  # noqa: BLE001
                return (
                    "Sync failed. "
                    f"{exc} "
                    "If this is a SnapTrade reconnect issue, run `connect`."
                )
        if parsed.action == "slate":
            return await self.recommendations.format_gamble_slate(session)
        if parsed.action == "bets":
            return await self.recommendations.format_daily_event_positions(session)
        if parsed.action == "betstats":
            return await self.recommendations.format_event_performance(session)
        if parsed.action == "report":
            batch = await self.recommendations.create_stock_batch(session, batch_type="manual-stock")
            return await self.recommendations.format_batch(session, batch.id)
        if parsed.action == "gamble":
            batch = await self.recommendations.create_event_batch(session, batch_type="manual-event")
            return await self.recommendations.format_batch(session, batch.id)
        if parsed.action == "gamble_small":
            if parsed.targets:
                small_batch = self.recommendations.latest_small_event_batch(session)
                if small_batch is None:
                    return "No small-bet `gs` batch exists yet. Run `gs` first."
                selected = self.recommendations.resolve_item_numbers(
                    session,
                    parsed.targets,
                    preferred_batch_id=small_batch.id,
                )
                selected = [
                    rec for rec in selected
                    if rec.recommendation_type == "event" and rec.execution_mode == "robinhood_auto_candidate"
                ]
                if not selected:
                    return "No executable `gs` event items matched. Run `gs` first, then use `gs x 2 5`."
                batch = await self.recommendations.create_gx_batch_from_recommendations(
                    session,
                    selected,
                    batch_type="gx",
                )
                return await self.recommendations.format_batch(session, batch.id)
            batch = await self.recommendations.create_gx_batch(
                session,
                batch_type="small-event",
                include_live=True,
            )
            return await self.recommendations.format_batch(session, batch.id)
        if parsed.action == "gamble_stage":
            manual_batch = self.recommendations.latest_manual_event_like_batch(session)
            if manual_batch is None:
                return "No medium/high-stake `g` batch exists yet. Run `g` first."
            selected = self.recommendations.resolve_item_numbers(
                session,
                parsed.targets,
                preferred_batch_id=manual_batch.id,
            )
            selected = [
                rec for rec in selected
                if rec.recommendation_type == "event" and rec.execution_mode == "manual"
            ]
            if not selected:
                return "No executable `g` event items matched. Run `g` first, then use `g x 2 5`."
            batch = await self.recommendations.create_gx_batch_from_recommendations(
                session,
                selected,
                batch_type="gx",
            )
            return await self.recommendations.format_batch(session, batch.id)
        if parsed.action == "gamblex_select":
            small_batch = self.recommendations.latest_small_event_batch(session)
            if small_batch is None:
                return "No small-bet `gs` batch exists yet. Run `gs` first."
            selected = self.recommendations.resolve_item_numbers(
                session,
                parsed.targets,
                preferred_batch_id=small_batch.id,
            )
            selected = [
                rec for rec in selected
                if rec.recommendation_type == "event" and rec.execution_mode == "robinhood_auto_candidate"
            ]
            if not selected:
                return "No executable `gs` event items matched. Run `gs` first, then use `gs x 2 5`."
            batch = await self.recommendations.create_gx_batch_from_recommendations(
                session,
                selected,
                batch_type="gx",
            )
            return await self.recommendations.format_batch(session, batch.id)
        if parsed.action == "execute":
            command = (parsed.targets[:1] or ["preview"])[0]
            if command == "preview":
                return await self.recommendations.build_gx_preview(session)
            if command in {"confirm", "c"}:
                return await self.recommendations.confirm_gx_preview(session)
            if command == "status":
                return self.recommendations.gx_preview_status(session)
            if command == "cancel":
                return self.recommendations.cancel_gx_preview(session)
            return "Use `x`, `x confirm`, `x status`, or `x cancel`."

        targets = parsed.targets or []
        if not targets:
            return "No targets provided. Try `y`, `n`, `deny 1`, `skip 1`, `latest`, `pending`, `portfolio`, `connect`, `sync`, `report`, `g`, `gs`, `gsl`, `bets`, `betstats`, `x`, or `status`."

        if parsed.action in {"approve", "executed"}:
            return (
                "Manual `approve` / `executed` tracking is deprecated. "
                "Place the trade in Robinhood and let Finn pick it up from the next sync. "
                "Use `deny` or `skip` only when you want Finn to suppress a recommendation."
            )

        if parsed.action in {"deny", "skip"}:
            preferred_batch = None
            if reply_to_external_message_id:
                batch = self.recommendations.batch_for_report_message(
                    session,
                    external_message_id=reply_to_external_message_id,
                    destination=chat_id,
                )
                preferred_batch = batch.id if batch else None
            recommendations = self.recommendations.resolve_item_numbers(
                session,
                targets,
                preferred_batch_id=preferred_batch,
            )
            if not recommendations:
                return "Could not resolve those recommendation numbers against the latest batch."
            recommendations = [
                recommendation
                for recommendation in recommendations
                if recommendation.recommendation_type in {"stock", "etf", "event"}
                and recommendation.side not in {"hold", "watch", "info"}
            ]
            if not recommendations:
                return "Those items are informational only. Action commands apply to buy, sell, and event trade items."
            for recommendation in recommendations:
                approval = Approval(
                    recommendation_id=recommendation.id,
                    action=parsed.action,
                    source_message_id=source_message_id,
                )
                session.add(approval)
                recommendation.status = self._next_recommendation_status(recommendation, parsed)
                session.add(recommendation)
            session.commit()
            item_numbers = ", ".join(str(r.item_number) for r in recommendations)
            return f"Recorded `{parsed.action}` for item(s): {item_numbers}"

        return HELP_TEXT

    def _next_recommendation_status(self, recommendation: Recommendation, parsed: ParsedCommand) -> str:
        if parsed.action == "deny":
            return "denied"
        if parsed.action == "skip":
            return "skipped"
        return recommendation.status
