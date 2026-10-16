from app.config import get_settings


if __name__ == "__main__":
    settings = get_settings()
    print(
        {
            "telegram_chat_id": bool(settings.telegram_chat_id),
            "snaptrade": bool(settings.snaptrade_client_id and settings.snaptrade_consumer_key),
            "twelve_data": bool(settings.effective_twelve_data_key),
            "gemini": bool(settings.gemini_api_key),
            "sec_user_agent": bool(settings.sec_user_agent),
            "kalshi": bool(settings.kalshi_api_id and settings.effective_kalshi_private_key),
        }
    )
