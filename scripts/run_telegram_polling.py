from __future__ import annotations

import asyncio
from time import monotonic

import httpx
from sqlmodel import select

from app.command_service import CommandService
from app.db import get_session, init_db
from app.models import IntegrationState, RecommendationBatch, ReportLog
from app.telegram_bot import TelegramBot


def _save_telegram_chat_id(session, chat_id: str) -> None:
    row = session.exec(
        select(IntegrationState).where(
            IntegrationState.provider == "telegram",
            IntegrationState.key == "last_chat_id",
        )
    ).first()
    if row is None:
        row = IntegrationState(provider="telegram", key="last_chat_id", value="")
    row.value = str(chat_id)
    session.add(row)
    session.commit()


def _background_chat_id(session, bot: TelegramBot) -> str | None:
    # Stored chat state is not an authorization source.
    configured = bot.settings.telegram_chat_id
    return configured if configured and configured.strip() else None


async def _send_background_messages(bot: TelegramBot, messages: list[str]) -> list[dict]:
    if not messages:
        return []
    with get_session() as session:
        chat_id = _background_chat_id(session, bot)
    if chat_id is None:
        return []
    responses = []
    for outbound in messages:
        responses.extend(await bot.send_message_chunks(outbound, chat_id=chat_id))
    return responses


async def main() -> None:
    bot = TelegramBot()
    if not bot.settings.telegram_chat_id or not bot.settings.telegram_chat_id.strip():
        raise RuntimeError("TELEGRAM_CHAT_ID is not configured")
    init_db()
    service = CommandService(bot)
    try:
        await bot.set_commands()
    except httpx.HTTPError as exc:
        print(f"[telegram] command registration skipped after transient HTTP error: {exc}", flush=True)

    offset: int | None = None
    last_event_watch_check = 0.0
    last_event_scout_check = 0.0
    last_event_monitor_check = 0.0
    last_event_manual_monitor_check = 0.0
    last_stock_listener_check = 0.0
    last_stock_alert_check = 0.0
    while True:
        try:
            payload = await bot.get_updates(offset=offset, timeout=20)
        except httpx.HTTPError:
            await asyncio.sleep(3)
            continue
        results = payload.get("result", [])
        for item in results:
            offset = item["update_id"] + 1
            message = item.get("message")
            if not message or not message.get("text"):
                continue
            if not service.is_authorized_chat(str((message.get("chat") or {}).get("id"))):
                continue
            with get_session() as session:
                latest_batch_before = service.recommendations.latest_batch(session)
                latest_batch_before_id = latest_batch_before.id if latest_batch_before else None
                _save_telegram_chat_id(session, str(message["chat"]["id"]))
                reply = await service.handle_message(
                    session,
                    chat_id=str(message["chat"]["id"]),
                    sender_id=str(message.get("from", {}).get("id")),
                    external_message_id=str(message["message_id"]),
                    text=message["text"],
                    reply_to_external_message_id=(
                        str(message.get("reply_to_message", {}).get("message_id"))
                        if message.get("reply_to_message", {}).get("message_id") is not None
                        else None
                    ),
                )
                latest_batch_after = service.recommendations.latest_batch(session)
                latest_batch_after_id = latest_batch_after.id if latest_batch_after else None
                is_new_report = (
                    message["text"].strip().lower().lstrip("/") in {"report", "gamble", "g", "gx"}
                    and latest_batch_after_id is not None
                    and latest_batch_after_id != latest_batch_before_id
                )
                if is_new_report:
                    outbound_messages = await service.recommendations.format_batch_messages(
                        session,
                        latest_batch_after_id,
                    )
                else:
                    outbound_messages = [reply]
            try:
                responses = []
                for index, outbound in enumerate(outbound_messages):
                    responses.extend(
                        await bot.send_message_chunks(
                            outbound,
                            chat_id=str(message["chat"]["id"]),
                            reply_to_message_id=message["message_id"] if index == 0 else None,
                        )
                    )
            except httpx.HTTPError:
                continue
            with get_session() as session:
                if is_new_report:
                    latest_batch = session.get(RecommendationBatch, latest_batch_after_id)
                    if latest_batch is not None:
                        latest_batch.sent_at = latest_batch.created_at
                        session.add(latest_batch)
                        for response in responses:
                            session.add(
                                ReportLog(
                                    batch_id=latest_batch.id,
                                    channel="telegram",
                                    destination=str(message["chat"]["id"]),
                                    external_message_id=str(response.get("result", {}).get("message_id")),
                                )
                            )
                        session.commit()
        if service.settings.stock_listener_enabled and monotonic() - last_stock_listener_check >= service.settings.stock_listener_poll_seconds:
            last_stock_listener_check = monotonic()
            with get_session() as session:
                try:
                    stock_state = await service.recommendations.refresh_stock_advisor_state(session)
                    print(f"[stock-listener] refreshed {stock_state}", flush=True)
                except Exception as exc:
                    print(f"[stock-listener] refresh failed: {type(exc).__name__}: {exc}", flush=True)
        if service.settings.stock_listener_enabled and monotonic() - last_stock_alert_check >= service.settings.stock_alert_poll_seconds:
            last_stock_alert_check = monotonic()
            stock_batch_id: str | None = None
            outbound_messages: list[str] = []
            with get_session() as session:
                try:
                    stock_batch = await service.recommendations.create_stock_alert_batch(session)
                except Exception as exc:
                    print(f"[stock-alert] batch creation failed: {type(exc).__name__}: {exc}", flush=True)
                    stock_batch = None
                if stock_batch is not None:
                    stock_batch_id = stock_batch.id
                    outbound_messages.extend(await service.recommendations.format_batch_messages(session, stock_batch_id))
                    print(
                        f"[stock-alert] created batch={stock_batch_id} messages={len(outbound_messages)}",
                        flush=True,
                    )
                else:
                    print("[stock-alert] no actionable stock batch", flush=True)
            if outbound_messages:
                try:
                    responses = await _send_background_messages(bot, outbound_messages)
                    print(f"[stock-alert] sent batch={stock_batch_id} chunks={len(responses)}", flush=True)
                except httpx.HTTPStatusError as exc:
                    body = exc.response.text[:500] if exc.response is not None else ""
                    print(
                        f"[stock-alert] telegram send failed: status={exc.response.status_code if exc.response else '-'} body={body}",
                        flush=True,
                    )
                    responses = []
                except httpx.HTTPError as exc:
                    print(f"[stock-alert] telegram send failed: {type(exc).__name__}: {exc}", flush=True)
                    responses = []
                except Exception as exc:
                    print(f"[stock-alert] unexpected send failure: {type(exc).__name__}: {exc}", flush=True)
                    responses = []
                if responses and stock_batch_id is not None:
                    with get_session() as session:
                        stock_row = session.get(RecommendationBatch, stock_batch_id)
                        if stock_row is not None:
                            stock_row.sent_at = stock_row.created_at
                            session.add(stock_row)
                            for response in responses:
                                session.add(
                                    ReportLog(
                                        batch_id=stock_row.id,
                                        channel="telegram",
                                        destination=_background_chat_id(session, bot) or "",
                                        external_message_id=str(response.get("result", {}).get("message_id")),
                                    )
                                )
                            session.commit()
        if monotonic() - last_event_watch_check >= service.settings.event_watch_poll_seconds:
            last_event_watch_check = monotonic()
            with get_session() as session:
                try:
                    await service.recommendations.refresh_event_watch_state(session)
                except Exception:
                    pass
        if monotonic() - last_event_scout_check >= service.settings.event_scout_poll_seconds:
            last_event_scout_check = monotonic()
            with get_session() as session:
                scout_batch = await service.recommendations.create_event_scout_batch(session)
                if scout_batch is not None:
                    outbound_messages = await service.recommendations.format_batch_messages(session, scout_batch.id)
                else:
                    outbound_messages = []
            if outbound_messages:
                try:
                    responses = await _send_background_messages(bot, outbound_messages)
                except httpx.HTTPError:
                    responses = []
                if responses:
                    with get_session() as session:
                        latest_batch = session.get(RecommendationBatch, scout_batch.id)
                        if latest_batch is not None:
                            latest_batch.sent_at = latest_batch.created_at
                            session.add(latest_batch)
                            for response in responses:
                                session.add(
                                    ReportLog(
                                        batch_id=latest_batch.id,
                                        channel="telegram",
                                        destination=_background_chat_id(session, bot) or "",
                                        external_message_id=str(response.get("result", {}).get("message_id")),
                                    )
                                )
                            session.commit()
        if monotonic() - last_event_manual_monitor_check >= service.settings.event_manual_monitor_poll_seconds:
            last_event_manual_monitor_check = monotonic()
            control_batch_id: str | None = None
            outbound_messages: list[str] = []
            with get_session() as session:
                control_batch = await service.recommendations.process_event_position_controls(
                    session,
                    include_auto=False,
                    include_manual=True,
                )
                if control_batch is not None:
                    control_batch_id = control_batch.id
                    outbound_messages.extend(await service.recommendations.format_batch_messages(session, control_batch_id))
            if outbound_messages:
                try:
                    responses = await _send_background_messages(bot, outbound_messages)
                except httpx.HTTPError:
                    responses = []
                if responses and control_batch_id is not None:
                    with get_session() as session:
                        control_row = session.get(RecommendationBatch, control_batch_id)
                        if control_row is not None:
                            control_row.sent_at = control_row.created_at
                            session.add(control_row)
                            for response in responses:
                                session.add(
                                    ReportLog(
                                        batch_id=control_row.id,
                                        channel="telegram",
                                        destination=_background_chat_id(session, bot) or "",
                                        external_message_id=str(response.get("result", {}).get("message_id")),
                                    )
                                )
                            session.commit()
        if monotonic() - last_event_monitor_check >= service.settings.event_monitor_poll_seconds:
            last_event_monitor_check = monotonic()
            control_batch_id: str | None = None
            monitor_batch_id: str | None = None
            outbound_messages: list[str] = []
            with get_session() as session:
                control_batch = await service.recommendations.process_event_position_controls(
                    session,
                    include_auto=True,
                    include_manual=False,
                )
                if control_batch is not None:
                    control_batch_id = control_batch.id
                    outbound_messages.extend(await service.recommendations.format_batch_messages(session, control_batch_id))
                monitor_batch = await service.recommendations.create_event_monitor_batch(session)
                if monitor_batch is not None:
                    monitor_batch_id = monitor_batch.id
                    outbound_messages.extend(await service.recommendations.format_batch_messages(session, monitor_batch_id))
            if outbound_messages:
                try:
                    responses = await _send_background_messages(bot, outbound_messages)
                except httpx.HTTPError:
                    responses = []
                if responses:
                    with get_session() as session:
                        if control_batch_id is not None:
                            control_row = session.get(RecommendationBatch, control_batch_id)
                            if control_row is not None:
                                control_row.sent_at = control_row.created_at
                                session.add(control_row)
                                for response in responses[:1]:
                                    session.add(
                                        ReportLog(
                                            batch_id=control_row.id,
                                            channel="telegram",
                                            destination=_background_chat_id(session, bot) or "",
                                            external_message_id=str(response.get("result", {}).get("message_id")),
                                        )
                                    )
                        if monitor_batch_id is not None:
                            latest_batch = session.get(RecommendationBatch, monitor_batch_id)
                            if latest_batch is not None:
                                latest_batch.sent_at = latest_batch.created_at
                                session.add(latest_batch)
                                start_index = 1 if control_batch_id is not None else 0
                                for response in responses[start_index:]:
                                    session.add(
                                        ReportLog(
                                            batch_id=latest_batch.id,
                                            channel="telegram",
                                            destination=_background_chat_id(session, bot) or "",
                                            external_message_id=str(response.get("result", {}).get("message_id")),
                                        )
                                    )
                        session.commit()
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
