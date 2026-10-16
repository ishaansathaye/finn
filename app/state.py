from __future__ import annotations

from dataclasses import dataclass


@dataclass
class HealthSnapshot:
    telegram_configured: bool
    snaptrade_configured: bool
    market_data_configured: bool
    kalshi_configured: bool
    gemini_configured: bool
    sec_user_agent_configured: bool
