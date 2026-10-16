from __future__ import annotations

import hmac

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from app.command_service import CommandService
from app.config import get_settings
from app.db import get_session, init_db
from app.state import HealthSnapshot
from app.telegram_bot import TelegramBot
from app.telegram_types import TelegramUpdate


settings = get_settings()
app = FastAPI(title=settings.app_name)
telegram_bot = TelegramBot()
command_service = CommandService(telegram_bot)


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/healthz")
def healthcheck() -> dict[str, object]:
    snapshot = HealthSnapshot(
        telegram_configured=bool(settings.telegram_bot_token and settings.telegram_chat_id),
        snaptrade_configured=bool(settings.snaptrade_client_id and settings.snaptrade_consumer_key),
        market_data_configured=bool(settings.effective_twelve_data_key),
        kalshi_configured=bool(settings.kalshi_api_id and settings.effective_kalshi_private_key),
        gemini_configured=bool(settings.gemini_api_key),
        sec_user_agent_configured=bool(settings.sec_user_agent),
    )
    return {"ok": True, "config": snapshot.__dict__}


@app.post("/telegram/webhook")
async def telegram_webhook(
    update: TelegramUpdate,
    telegram_secret: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
) -> JSONResponse:
    configured_secret = settings.telegram_webhook_secret
    if not configured_secret or not configured_secret.strip():
        raise HTTPException(status_code=503, detail="Telegram webhook is not configured")
    if telegram_secret is None or not hmac.compare_digest(
        telegram_secret.encode("utf-8"), configured_secret.encode("utf-8")
    ):
        raise HTTPException(status_code=403, detail="Forbidden")
    if not settings.telegram_chat_id or not settings.telegram_chat_id.strip():
        raise HTTPException(status_code=503, detail="Telegram chat is not configured")

    message = update.message
    if message and not command_service.is_authorized_chat(str(message.chat.id)):
        raise HTTPException(status_code=403, detail="Forbidden")
    if not message or not message.text:
        return JSONResponse({"ok": True, "ignored": True})

    with get_session() as session:
        response_text = await command_service.handle_message(
            session,
            chat_id=str(message.chat.id),
            sender_id=str(message.from_.id) if message.from_ else None,
            external_message_id=str(message.message_id),
            text=message.text,
            reply_to_external_message_id=(
                str(message.reply_to_message.get("message_id"))
                if message.reply_to_message and message.reply_to_message.get("message_id") is not None
                else None
            ),
        )

    await telegram_bot.send_message(
        response_text,
        chat_id=str(message.chat.id),
        reply_to_message_id=message.message_id,
    )
    return JSONResponse({"ok": True})
