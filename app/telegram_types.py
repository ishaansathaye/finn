from __future__ import annotations

from pydantic import BaseModel, Field


class TelegramUser(BaseModel):
    id: int
    is_bot: bool | None = None
    first_name: str | None = None
    username: str | None = None


class TelegramChat(BaseModel):
    id: int
    type: str
    title: str | None = None
    username: str | None = None


class TelegramMessage(BaseModel):
    message_id: int
    date: int
    chat: TelegramChat
    text: str | None = None
    from_: TelegramUser | None = Field(default=None, alias="from")
    reply_to_message: dict | None = None

    model_config = {"populate_by_name": True}


class TelegramUpdate(BaseModel):
    update_id: int
    message: TelegramMessage | None = None
