from __future__ import annotations

from datetime import datetime, UTC

from app.command_service import CommandService
from app.db import get_session, init_db
from app.models import RecommendationBatch, ReportLog
from app.telegram_bot import TelegramBot


class ReportRunner:
    def __init__(self) -> None:
        self.bot = TelegramBot()
        self.commands = CommandService(self.bot)

    async def send_batch(self, command_text: str) -> str:
        init_db()
        with get_session() as session:
            latest_batch_before = self.commands.recommendations.latest_batch(session)
            latest_batch_before_id = latest_batch_before.id if latest_batch_before else None
            message = await self.commands.handle_message(
                session,
                chat_id=self.bot.settings.telegram_chat_id or "",
                sender_id="scheduler",
                external_message_id=f"scheduler:{command_text}",
                text=command_text,
            )
            latest_batch = self.commands.recommendations.latest_batch(session)
            latest_batch_id = latest_batch.id if latest_batch else None
            if command_text.strip().lower().lstrip("/") in {"report", "gamble"} and latest_batch_id != latest_batch_before_id:
                outbound_messages = await self.commands.recommendations.format_batch_messages(session, latest_batch_id)
            else:
                outbound_messages = [message]
        responses = []
        for outbound in outbound_messages:
            responses.extend(await self.bot.send_message_chunks(outbound))
        with get_session() as session:
            if latest_batch_id is not None and latest_batch_id != latest_batch_before_id:
                latest_batch = session.get(RecommendationBatch, latest_batch_id)
                if latest_batch is None:
                    return message
                latest_batch.sent_at = datetime.now(UTC).replace(tzinfo=None)
                session.add(latest_batch)
                for response in responses:
                    session.add(
                        ReportLog(
                            batch_id=latest_batch.id,
                            channel="telegram",
                            destination=self.bot.settings.telegram_chat_id or "",
                            external_message_id=str(response.get("result", {}).get("message_id")),
                        )
                    )
                session.commit()
        return message
