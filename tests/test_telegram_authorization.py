from contextlib import nullcontext
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest


@pytest.fixture
def auth_modules(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from app import config

    # Never load deployment dotenv credentials while importing application modules.
    settings = config.Settings.model_construct(
        database_url="sqlite://",
        telegram_chat_id="1",
        telegram_bot_token=None,
        telegram_webhook_secret="test-webhook-secret",
    )
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    commands = importlib.import_module("app.command_service")
    main = importlib.import_module("app.main")
    polling = importlib.import_module("scripts.run_telegram_polling")
    service = commands.CommandService.__new__(commands.CommandService)
    service.settings = settings
    service._apply_command = AsyncMock(return_value="accepted")
    bot = SimpleNamespace(settings=settings, send_message=AsyncMock(), send_message_chunks=AsyncMock())
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "command_service", service)
    monkeypatch.setattr(main, "telegram_bot", bot)
    return SimpleNamespace(
        commands=commands, main=main, polling=polling,
        service=service, settings=settings, bot=bot,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("configured,incoming", [
    (None, "1"), ("", "1"), (" ", " "), ("1", "2"),
    ("1", "01"), ("1", "1 "), ("1", None),
])
async def test_command_rejects_before_storage_or_execution(auth_modules, monkeypatch, configured, incoming):
    m = auth_modules
    m.settings.telegram_chat_id = configured
    session = MagicMock()
    parser = MagicMock(side_effect=AssertionError("unauthorized parsing"))
    monkeypatch.setattr(m.commands, "parse_command", parser)
    with pytest.raises(PermissionError):
        await m.service.handle_message(
            session, chat_id=incoming, sender_id="test-user",
            external_message_id="test-message", text="x confirm",
        )
    session.add.assert_not_called()
    session.commit.assert_not_called()
    parser.assert_not_called()
    m.service._apply_command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_id", ["1", "-100123"])
async def test_command_accepts_exact_configured_chat(auth_modules, chat_id):
    m = auth_modules
    m.settings.telegram_chat_id = chat_id
    session = MagicMock()
    result = await m.service.handle_message(
        session, chat_id=chat_id, sender_id="test-user",
        external_message_id="test-message", text="help",
    )
    assert result == "accepted"
    session.add.assert_called_once()
    session.commit.assert_called_once()
    m.service._apply_command.assert_awaited_once()


def update_payload(chat_id=1):
    return {"update_id": 1, "message": {
        "message_id": 1, "date": 0, "chat": {"id": chat_id, "type": "private"},
        "from": {"id": 1}, "text": "x confirm",
    }}


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_secret,header,chat,configured_chat,status", [
    (None, None, 1, "1", 503),
    ("", "test-webhook-secret", 1, "1", 503),
    ("test-webhook-secret", None, 1, "1", 403),
    ("test-webhook-secret", "wrong-secret", 1, "1", 403),
    ("test-webhook-secret", "test-webhook-secret", 2, "1", 403),
    ("test-webhook-secret", "test-webhook-secret", 1, None, 503),
])
async def test_webhook_fails_closed_before_storage_or_send(
    auth_modules, monkeypatch, configured_secret, header, chat, configured_chat, status,
):
    m = auth_modules
    m.settings.telegram_webhook_secret = configured_secret
    m.settings.telegram_chat_id = configured_chat
    storage = MagicMock(side_effect=AssertionError("unauthorized storage"))
    monkeypatch.setattr(m.main, "get_session", storage)
    headers = {"X-Telegram-Bot-Api-Secret-Token": header} if header is not None else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.main.app), base_url="http://test") as client:
        response = await client.post("/telegram/webhook", json=update_payload(chat), headers=headers)
    assert response.status_code == status
    storage.assert_not_called()
    m.service._apply_command.assert_not_awaited()
    m.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_accepts_secret_and_authorized_chat(auth_modules, monkeypatch):
    m = auth_modules
    session = MagicMock()
    monkeypatch.setattr(m.main, "get_session", lambda: nullcontext(session))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=m.main.app), base_url="http://test") as client:
        response = await client.post(
            "/telegram/webhook", json=update_payload(),
            headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"},
        )
    assert response.status_code == 200
    session.add.assert_called_once()
    m.service._apply_command.assert_awaited_once()
    m.bot.send_message.assert_awaited_once_with("accepted", chat_id="1", reply_to_message_id=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [None, "", " ", "1"])
async def test_background_destination_ignores_saved_chat(auth_modules, monkeypatch, configured):
    m = auth_modules
    m.settings.telegram_chat_id = configured
    session = MagicMock()
    session.exec.return_value.first.return_value = SimpleNamespace(value="untrusted-chat")
    monkeypatch.setattr(m.polling, "get_session", lambda: nullcontext(session))
    m.bot.send_message_chunks.return_value = []
    expected = "1" if configured == "1" else None
    assert m.polling._background_chat_id(session, m.bot) == expected
    await m.polling._send_background_messages(m.bot, ["private report"])
    session.exec.assert_not_called()
    if expected:
        m.bot.send_message_chunks.assert_awaited_once_with("private report", chat_id="1")
    else:
        m.bot.send_message_chunks.assert_not_awaited()


class StopPolling(Exception):
    pass


@pytest.mark.asyncio
async def test_polling_discards_unauthorized_before_any_storage(auth_modules, monkeypatch):
    m = auth_modules
    m.bot.set_commands = AsyncMock()
    m.bot.get_updates = AsyncMock(side_effect=[
        {"result": [update_payload(2)]}, StopPolling(),
    ])
    storage = MagicMock(side_effect=AssertionError("unauthorized storage"))
    save_chat = MagicMock(side_effect=AssertionError("unauthorized last chat"))
    monkeypatch.setattr(m.polling, "TelegramBot", lambda: m.bot)
    monkeypatch.setattr(m.polling, "CommandService", lambda bot: m.service)
    monkeypatch.setattr(m.polling, "init_db", MagicMock())
    monkeypatch.setattr(m.polling, "get_session", storage)
    monkeypatch.setattr(m.polling, "_save_telegram_chat_id", save_chat)
    monkeypatch.setattr(m.polling, "monotonic", lambda: 0)
    monkeypatch.setattr(m.polling.asyncio, "sleep", AsyncMock())
    with pytest.raises(StopPolling):
        await m.polling.main()
    storage.assert_not_called()
    save_chat.assert_not_called()
    m.service._apply_command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [None, "", " "])
async def test_polling_missing_chat_stops_before_startup(auth_modules, monkeypatch, configured):
    m = auth_modules
    m.settings.telegram_chat_id = configured
    m.bot.set_commands = AsyncMock(side_effect=AssertionError("unauthorized startup"))
    startup = MagicMock()
    monkeypatch.setattr(m.polling, "TelegramBot", lambda: m.bot)
    monkeypatch.setattr(m.polling, "CommandService", lambda bot: m.service)
    monkeypatch.setattr(m.polling, "init_db", startup)
    with pytest.raises(RuntimeError, match="TELEGRAM_CHAT_ID"):
        await m.polling.main()
    startup.assert_not_called()
    m.bot.set_commands.assert_not_awaited()
