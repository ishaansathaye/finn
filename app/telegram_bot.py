from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings


class TelegramBot:
    max_message_chars = 3900

    def __init__(self) -> None:
        self.settings = get_settings()
        self.base_url = (
            f"https://api.telegram.org/bot{self.settings.telegram_bot_token}"
            if self.settings.telegram_bot_token
            else None
        )

    def _chunk_text(self, text: str) -> list[str]:
        if len(text) <= self.max_message_chars:
            return [text]

        lines = text.split("\n")
        chunks: list[str] = []
        current = ""
        for line in lines:
            candidate = line if not current else f"{current}\n{line}"
            if len(candidate) <= self.max_message_chars:
                current = candidate
                continue
            if current:
                chunks.append(current)
                current = line
                continue
            start = 0
            while start < len(line):
                end = min(start + self.max_message_chars, len(line))
                chunks.append(line[start:end])
                start = end
            current = ""
        if current:
            chunks.append(current)
        return chunks

    async def send_message(
        self,
        text: str,
        chat_id: str | None = None,
        *,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        if not self.base_url:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        target_chat = chat_id or self.settings.telegram_chat_id
        if not target_chat:
            raise RuntimeError("TELEGRAM_CHAT_ID is not configured")

        responses = await self.send_message_chunks(
            text,
            chat_id=target_chat,
            reply_to_message_id=reply_to_message_id,
        )
        return responses[0]

    async def send_message_chunks(
        self,
        text: str,
        chat_id: str | None = None,
        *,
        reply_to_message_id: int | None = None,
    ) -> list[dict[str, Any]]:
        if not self.base_url:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        target_chat = chat_id or self.settings.telegram_chat_id
        if not target_chat:
            raise RuntimeError("TELEGRAM_CHAT_ID is not configured")

        responses: list[dict[str, Any]] = []
        chunks = self._chunk_text(text)
        timeout = httpx.Timeout(connect=15.0, read=30.0, write=15.0, pool=15.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for index, chunk in enumerate(chunks):
                payload: dict[str, Any] = {
                    "chat_id": target_chat,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }
                if reply_to_message_id is not None and index == 0:
                    payload["reply_parameters"] = {"message_id": reply_to_message_id}
                response = await client.post(
                    f"{self.base_url}/sendMessage",
                    json=payload,
                )
                response.raise_for_status()
                responses.append(response.json())
        return responses

    async def set_commands(self) -> dict[str, Any]:
        if not self.base_url:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        commands = [
            {"command": "status", "description": "Show Finn status"},
            {"command": "portfolio", "description": "Show latest synced portfolio snapshot"},
            {"command": "report", "description": "Generate a stock / ETF recommendation report"},
            {"command": "gamble", "description": "Generate a prediction-market report"},
            {"command": "gx", "description": "Generate low-stake auto-executable prediction bets"},
            {"command": "bets", "description": "Show today's prediction-market bet statuses"},
            {"command": "betstats", "description": "Show all-time prediction-market performance"},
            {"command": "x", "description": "Build or confirm the latest gx execution preview"},
            {"command": "sync", "description": "Refresh synced Robinhood data from SnapTrade"},
            {"command": "connect", "description": "Generate a SnapTrade Robinhood connect link"},
            {"command": "latest", "description": "Show the latest numbered recommendation batch"},
            {"command": "pending", "description": "Show pending recommendation items"},
            {"command": "about", "description": "Explain how Finn behaves and what commands do"},
            {"command": "deny", "description": "Deny recommendation numbers"},
            {"command": "skip", "description": "Skip recommendation numbers"},
        ]
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self.base_url}/setMyCommands",
                json={"commands": commands},
            )
            response.raise_for_status()
            return response.json()

    async def get_updates(self, offset: int | None = None, timeout: int = 30) -> dict[str, Any]:
        if not self.base_url:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        timeout_config = httpx.Timeout(connect=15.0, read=timeout + 10.0, write=15.0, pool=15.0)
        async with httpx.AsyncClient(timeout=timeout_config) as client:
            response = await client.post(f"{self.base_url}/getUpdates", json=payload)
            response.raise_for_status()
            return response.json()
