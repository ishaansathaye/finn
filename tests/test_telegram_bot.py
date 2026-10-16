from __future__ import annotations

from app.telegram_bot import TelegramBot


def test_chunk_text_keeps_each_message_under_limit() -> None:
    bot = TelegramBot()
    bot.max_message_chars = 100
    text = "\n".join([f"line {i} " + ("x" * 20) for i in range(20)])

    chunks = bot._chunk_text(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert "\n".join(chunks) == text

