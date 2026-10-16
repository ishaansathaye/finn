from __future__ import annotations

import asyncio

from app.command_service import CommandService
from app.db import get_session, init_db
from app.telegram_bot import TelegramBot


async def main() -> None:
    init_db()
    bot = TelegramBot()
    service = CommandService(bot)
    with get_session() as session:
        message = await service.handle_message(
            session,
            chat_id=bot.settings.telegram_chat_id or "",
            sender_id="local-script",
            external_message_id="local-report",
            text="report",
        )
    await bot.send_message(message)
    print("Sent placeholder report.")


if __name__ == "__main__":
    asyncio.run(main())

