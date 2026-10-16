from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["dev", "prod", "test"] = "dev"
    app_name: str = "Finn"
    database_url: str = "sqlite:///./finn.db"
    log_level: str = "INFO"

    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")
    telegram_webhook_secret: str | None = Field(default=None, alias="TELEGRAM_WEBHOOK_SECRET")
    telegram_polling_enabled: bool = True

    snaptrade_client_id: str | None = Field(default=None, alias="SNAPTRADE_CLIENT_ID")
    snaptrade_consumer_key: str | None = Field(default=None, alias="SNAPTRADE_CONSUMER_KEY")
    snaptrade_user_id: str = Field(default="finn-local-user", alias="SNAPTRADE_USER_ID")

    twelve_data_api_key: str | None = Field(default=None, alias="TWELVE_DATA_API_KEY")
    twelvedata_api_key: str | None = Field(default=None, alias="TWELVEDATA_API_KEY")
    polygon_api_key: str | None = Field(default=None, alias="POLYGON_API_KEY")
    massive_key: str | None = Field(default=None, alias="MASSIVE_KEY")

    kalshi_api_id: str | None = Field(default=None, alias="KALSHI_API_ID")
    kalshi_private_key: str | None = Field(default=None, alias="KALSHI_PRIVATE_KEY")
    kalshi_private_key_path: str | None = Field(default=None, alias="KALSHI_PRIVATE_KEY_PATH")

    fmp_api_key: str | None = Field(default=None, alias="FMP_API_KEY")
    alpha_vantage_api_key: str | None = Field(default=None, alias="ALPHA_VANTAGE_API_KEY")
    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    gemini_model_analyst: str = Field(default="gemini-2.5-flash", alias="GEMINI_MODEL_ANALYST")
    stock_llm_daily_request_budget: int = Field(default=12, ge=0, alias="STOCK_LLM_DAILY_REQUEST_BUDGET")
    stock_strategy_enabled: bool = Field(default=True, alias="STOCK_STRATEGY_ENABLED")
    stock_strategy_max_sector_weight: float = Field(default=.35, gt=0, le=1, alias="STOCK_STRATEGY_MAX_SECTOR_WEIGHT")
    stock_strategy_max_new_weight: float = Field(default=.05, gt=0, le=1, alias="STOCK_STRATEGY_MAX_NEW_WEIGHT")
    stock_strategy_max_volatility_increase: float = Field(default=.01, ge=0, le=.1, alias="STOCK_STRATEGY_MAX_VOLATILITY_INCREASE")
    gemini_model_triage: str = Field(default="gemini-2.5-flash-lite", alias="GEMINI_MODEL_TRIAGE")
    sec_user_agent: str = Field(
        default="finn/0.1 (configure SEC_USER_AGENT with your contact)",
        alias="SEC_USER_AGENT",
    )

    premarket_report_time_pt: str = "06:20"
    midday_report_time_pt: str = "11:30"
    after_close_report_time_pt: str = "17:10"

    max_single_stock_allocation_pct: float = 0.12
    max_single_etf_allocation_pct: float = 0.35
    max_daily_prediction_risk_pct: float = 0.02
    max_total_prediction_bankroll_pct: float = 0.05
    minimum_cash_buffer_usd: float = 500.0

    momentum_trade_validity_minutes: int = 90
    earnings_trade_validity_minutes: int = 240
    core_etf_validity_hours: int = 72
    event_trade_validity_minutes: int = 180

    local_timezone: str = "America/Los_Angeles"
    starter_equity_universe: str = "NVDA,AMD,MSFT,AAPL,AMZN,META,GOOGL,PLTR,MU,TSM,SNDK,VTI,VT,QQQM,SPY"
    discovery_equity_universe: str = (
        "NVDA,AMD,AVGO,TSM,MU,MRVL,ARM,QCOM,ANET,DELL,SMCI,"
        "MSFT,AMZN,GOOGL,META,NFLX,UBER,CRM,ORCL,SNOW,PLTR,ADBE,NOW,"
        "JPM,V,MA,AXP,COIN,HOOD,BRK-B,CB,AIG,BLK,KKR,SPGI,ICE,"
        "RTX,LMT,NOC,GD,HII,KTOS,AVAV,GE,BA,ETN,CEG,CAT,URI,DE,EMR,HON,"
        "RKLB,LUNR,ASTS,IRDM,VSAT,"
        "XOM,CVX,COP,EOG,FANG,OXY,SLB,LNG,NEE,VST,CEG,OKLO,CCJ,UEC,"
        "LLY,NVO,ISRG,UNH,ABBV,BSX,TMO,DHR,VRTX,REGN,"
        "CRWD,NET,DDOG,PANW,ZS,"
        "SNDK,APP,HIMS,RDDT,TTD,SHOP,COST,WMT,ABNB,BKNG,HD,MCD,"
        "FCX,NEM,SCCO,LIN,APD,ALB,"
        "VTI,VT,QQQM,SPY,SMH,XLI,XLF,XLE,XLV,XLP,XLY,XLC,XLB,ITA,XAR,ARKX,URA"
    )
    kalshi_market_limit: int = 25
    twelve_data_max_symbols_per_request: int = 8
    market_quote_cache_seconds: int = 55
    market_news_cache_seconds: int = 3600
    market_calendar_cache_seconds: int = 21600
    market_history_cache_seconds: int = 21600
    sec_filings_cache_seconds: int = 21600
    sec_companyfacts_cache_seconds: int = 43200
    llm_max_stock_symbols: int = 24
    llm_max_event_candidates: int = 8
    stock_discovery_limit: int = 80
    stock_recent_recommendation_cooldown_hours: int = 20
    stock_significant_score_delta: float = 0.5
    core_stock_symbols: str = ""
    stock_core_inference_threshold: float = 0.62
    stock_listener_enabled: bool = True
    stock_listener_poll_seconds: int = 300
    stock_alert_poll_seconds: int = 900
    stock_listener_universe_limit: int = 80
    stock_news_symbols_per_cycle: int = 12
    stock_filing_symbols_per_cycle: int = 6
    stock_proactive_min_priority: float = 5.0
    stock_proactive_idea_limit: int = 5
    stock_after_hours_exception_min_priority: float = 8.0
    stock_after_hours_exception_min_buy_amount_usd: float = 50.0
    stock_alert_cooldown_minutes: int = 240
    employment_excluded_stock_symbols: str = ""
    robinhood_prediction_catalog_cache_seconds: int = 120
    robinhood_write_auth_mode: str = Field(default="session", alias="ROBINHOOD_WRITE_AUTH_MODE")
    robinhood_write_access_token: str | None = Field(default=None, alias="ROBINHOOD_WRITE_ACCESS_TOKEN")
    robinhood_write_refresh_token: str | None = Field(default=None, alias="ROBINHOOD_WRITE_REFRESH_TOKEN")
    robinhood_write_device_id: str | None = Field(default=None, alias="ROBINHOOD_WRITE_DEVICE_ID")
    robinhood_write_user_agent: str = Field(
        default="Robinhood/2026.17.0 (iPhone; iOS 18.3; Scale/3.00)",
        alias="ROBINHOOD_WRITE_USER_AGENT",
    )
    robinhood_write_session_path: str = Field(
        default="~/.finn/robinhood_write_session.json",
        alias="ROBINHOOD_WRITE_SESSION_PATH",
    )
    robinhood_prediction_rhs_account_number: str = Field(
        default="",
        alias="ROBINHOOD_PREDICTION_RHS_ACCOUNT_NUMBER",
    )
    robinhood_prediction_account_id: str | None = Field(
        default=None,
        alias="ROBINHOOD_PREDICTION_ACCOUNT_ID",
    )
    primary_portfolio_account_external_id: str | None = Field(
        default=None,
        alias="PRIMARY_PORTFOLIO_ACCOUNT_EXTERNAL_ID",
    )
    robinhood_username: str | None = Field(default=None, alias="ROBINHOOD_USERNAME")
    robinhood_password: str | None = Field(default=None, alias="ROBINHOOD_PASSWORD")
    robinhood_mfa_code: str | None = Field(default=None, alias="ROBINHOOD_MFA_CODE")
    manual_event_report_limit: int = 60
    event_watch_poll_seconds: int = 30
    event_scout_poll_seconds: int = 120
    event_scout_limit: int = 8
    event_scout_cooldown_minutes: int = 360
    event_scout_min_priority: float = 9.0
    event_scout_min_edge: float = 0.055
    event_scout_min_ev_per_contract: float = 0.025
    event_scout_min_profit_usd: float = 2.0
    event_monitor_poll_seconds: int = 45
    event_manual_monitor_poll_seconds: int = 15
    event_alert_cooldown_minutes: int = 20
    event_probability_move_alert_threshold: float = 0.08
    event_edge_alert_threshold: float = 0.04
    event_follow_up_limit: int = 4
    event_fee_per_contract_usd: float = 0.003
    event_min_quant_edge: float = 0.004
    event_manual_min_quant_edge: float = 0.035
    event_manual_high_confidence_edge: float = 0.07
    event_max_single_stake_usd: float = 15.0
    event_loss_guard_max_single_stake_usd: float = 6.0
    event_loss_guard_recent_days: int = 7
    event_loss_guard_min_pnl_usd: float = -25.0
    event_new_pick_blocked_categories: str = "Esports"
    event_min_forced_potential_profit_usd: float = 1.0
    event_kelly_fraction_cap: float = 0.2
    event_kelly_fraction_scale: float = 0.15
    gx_batch_limit: int = 8
    gx_max_stake_usd: float = 3.0
    gx_daily_budget_usd: float = 15.0
    gx_max_open_positions: int = 5
    gx_min_profit_usd: float = 1.0
    gx_preview_ttl_minutes: int = 10
    gx_event_cutoff_minutes: int = 12
    gx_price_drift_threshold: float = 0.03
    gs_auto_manage_max_stake_usd: float = 3.5
    gs_auto_cut_edge_threshold: float = -0.015
    gs_auto_cut_move_threshold: float = 0.04
    gs_auto_add_edge_threshold: float = 0.12
    gs_auto_add_price_improvement_threshold: float = 0.10
    gs_auto_add_enabled: bool = True
    event_manual_add_edge_threshold: float = 0.085
    event_manual_add_price_improvement_threshold: float = 0.08
    event_position_max_adds: int = 2
    event_control_cooldown_minutes: int = 20
    event_control_failure_cooldown_minutes: int = 360
    agentic_trader_snapshot_path: str = Field(
        default="/var/lib/agentic-trader/snapshots/latest.json",
        alias="AGENTIC_TRADER_SNAPSHOT_PATH",
    )

    @property
    def effective_twelve_data_key(self) -> str | None:
        return self.twelve_data_api_key or self.twelvedata_api_key

    @property
    def starter_symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.starter_equity_universe.split(",") if s.strip()]

    @property
    def discovery_symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.discovery_equity_universe.split(",") if s.strip()]

    @property
    def employment_excluded_symbols(self) -> set[str]:
        return {s.strip().upper() for s in self.employment_excluded_stock_symbols.split(",") if s.strip()}

    @property
    def effective_kalshi_private_key(self) -> str | None:
        if self.kalshi_private_key:
            return self.kalshi_private_key
        if self.kalshi_private_key_path:
            path = Path(self.kalshi_private_key_path)
            if path.exists():
                return path.read_text()
        return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
