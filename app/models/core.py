from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlmodel import Field, SQLModel


def _uuid() -> str:
    return str(uuid4())


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Account(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    external_id: str | None = None
    provider: str
    account_type: str
    name: str
    currency: str = "USD"
    created_at: datetime = Field(default_factory=_utcnow)


class AccountSnapshot(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    account_id: str
    cash_balance: float = 0.0
    buying_power: float = 0.0
    total_value: float = 0.0
    as_of: datetime = Field(default_factory=_utcnow)


class Holding(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    account_id: str
    symbol: str
    quantity: float
    market_value: float = 0.0
    cost_basis: float | None = None
    asset_type: str = "equity"
    as_of: datetime = Field(default_factory=_utcnow)


class Transaction(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    account_id: str
    external_id: str | None = None
    symbol: str | None = None
    transaction_type: str
    amount: float
    quantity: float | None = None
    occurred_at: datetime = Field(default_factory=_utcnow)


class RecommendationBatch(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    batch_type: str
    channel: str = "telegram"
    sent_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class Recommendation(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    batch_id: str
    item_number: int
    recommendation_type: str
    symbol: str | None = None
    event_code: str | None = None
    side: str
    amount_usd: float
    reason: str
    risk: str
    execution_mode: str
    valid_until: datetime
    status: str = "pending"
    created_at: datetime = Field(default_factory=_utcnow)


class Approval(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    recommendation_id: str
    action: str
    source_message_id: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class Execution(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    recommendation_id: str
    provider: str
    provider_order_id: str | None = None
    status: str = "queued"
    details: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class RecommendationOutcome(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    recommendation_id: str
    symbol: str
    baseline_price: float
    latest_price: float | None = None
    return_pct: float | None = None
    status: str = "open"
    notes: str | None = None
    evaluated_at: datetime = Field(default_factory=_utcnow)
    created_at: datetime = Field(default_factory=_utcnow)


class EventPosition(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    event_code: str
    side: str
    amount_usd: float
    source_recommendation_id: str | None = None
    source: str = "manual"
    broker_account_id: str | None = None
    broker_position_id: str | None = None
    broker_order_id: str | None = None
    broker_contract_id: str | None = None
    broker_event_id: str | None = None
    quantity: float | None = None
    avg_fill_price: float | None = None
    event_title: str | None = None
    pick_label: str | None = None
    category: str | None = None
    time_label: str | None = None
    opened_at: datetime | None = None
    status: str = "assumed_open"
    created_at: datetime = Field(default_factory=_utcnow)


class EventBetOutcome(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    event_position_id: str
    event_code: str
    outcome: str = "in_progress"
    payout_usd: float | None = None
    profit_usd: float | None = None
    implied_probability: float | None = None
    settlement_price: float | None = None
    realized_pnl_without_fees_usd: float | None = None
    broker_execution_id: str | None = None
    settled_at: datetime | None = None
    checked_at: datetime = Field(default_factory=_utcnow)
    notes: str | None = None


class EventWatch(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    event_key: str
    robinhood_event_id: str | None = None
    primary_contract_id: str | None = None
    event_title: str
    sport: str | None = None
    league: str | None = None
    participants_csv: str | None = None
    scheduled_start: datetime | None = None
    state: str = "discovered"
    last_event_status: str | None = None
    last_score_summary: str | None = None
    last_market_implied_prob: float | None = None
    last_fair_prob: float | None = None
    last_recommended_lane: str | None = None
    last_decision_hash: str | None = None
    last_alert_sent_at: datetime | None = None
    supports_live_data: bool = False
    source: str = "robinhood"
    updated_at: datetime = Field(default_factory=_utcnow)
    created_at: datetime = Field(default_factory=_utcnow)


class EventStateSnapshot(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    event_key: str
    event_status: str | None = None
    score_summary: str | None = None
    source: str = "robinhood"
    source_timestamp: datetime | None = None
    freshness_seconds: float | None = None
    supports_live_data: bool = False
    polled_at: datetime = Field(default_factory=_utcnow)


class EventPriceSnapshot(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    event_key: str
    contract_id: str
    yes_bid: float | None = None
    yes_ask: float | None = None
    no_bid: float | None = None
    no_ask: float | None = None
    last_trade: float | None = None
    spread: float | None = None
    polled_at: datetime = Field(default_factory=_utcnow)


class EventDecisionSnapshot(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    event_key: str
    lane: str
    action: str
    recommended_side: str | None = None
    recommended_stake: float | None = None
    market_implied: float | None = None
    fair_implied: float | None = None
    confidence: str | None = None
    reason_summary: str | None = None
    decision_time: datetime = Field(default_factory=_utcnow)


class Watchlist(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    name: str
    symbols_csv: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class ReportLog(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    batch_id: str
    channel: str
    destination: str
    external_message_id: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class InboundMessage(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    source: str = "telegram"
    external_message_id: str | None = None
    sender_id: str | None = None
    chat_id: str | None = None
    text: str
    created_at: datetime = Field(default_factory=_utcnow)


class IntegrationState(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    provider: str
    key: str
    value: str
    updated_at: datetime = Field(default_factory=_utcnow)


class AgenticAccountSnapshot(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    as_of: datetime = Field(default_factory=_utcnow)
    account_number_masked: str | None = None
    total_value: float = 0.0
    cash: float = 0.0
    buying_power: float = 0.0
    equity_value: float = 0.0
    options_value: float = 0.0
    crypto_value: float = 0.0
    event_contracts_value: float = 0.0
    raw_payload_json: str = "{}"


class AgenticPositionSnapshot(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    snapshot_id: str
    asset_type: str = "equity"
    symbol: str
    quantity: float = 0.0
    average_cost: float | None = None
    market_value: float = 0.0
    unrealized_pl: float | None = None
    raw_payload_json: str = "{}"


class AgenticOrderSnapshot(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    broker_order_id: str | None = None
    account_number_masked: str | None = None
    asset_type: str = "equity"
    symbol: str | None = None
    side: str | None = None
    quantity: float | None = None
    notional: float | None = None
    order_type: str | None = None
    state: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime | None = None
    filled_at: datetime | None = None
    average_fill_price: float | None = None
    raw_payload_json: str = "{}"


class AgenticDecisionJournal(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=_utcnow)
    run_id: str | None = None
    cycle_id: str | None = None
    decision_type: str | None = None
    symbol: str | None = None
    action: str
    confidence: float | None = None
    thesis: str | None = None
    risk_summary: str | None = None
    order_id: str | None = None
    result: str | None = None
    raw_context_json: str = "{}"


class RiskRule(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    name: str
    value: str


class CashRule(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    name: str
    value: str


class SecCompany(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    symbol: str
    cik: str
    name: str
    exchange: str | None = None
    updated_at: datetime = Field(default_factory=_utcnow)


class SecFiling(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    symbol: str
    cik: str
    accession_number: str
    form_type: str
    filing_date: datetime
    report_date: str | None = None
    primary_document: str | None = None
    item_codes: str | None = None
    description: str | None = None
    filing_url: str
    text_excerpt: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)

class StockPositionPolicy(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    symbol: str
    lane: str = "unknown"
    user_confirmed: bool = False
    core_score: float = 0.0
    total_profit_usd: float = 0.0
    realized_profit_usd: float = 0.0
    unrealized_profit_usd: float = 0.0
    holding_days: int | None = None
    reason_summary: str | None = None
    updated_at: datetime = Field(default_factory=_utcnow)
    created_at: datetime = Field(default_factory=_utcnow)


class StockNewsItem(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    symbol: str
    source: str
    dedupe_key: str
    title: str
    summary: str | None = None
    url: str | None = None
    published_at: datetime | None = None
    importance_score: float = 0.0
    classification: str = "store_context"
    raw_payload: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class StockFilingItem(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    symbol: str
    accession_number: str
    form_type: str | None = None
    filing_date: datetime | None = None
    filing_url: str | None = None
    significance_score: float = 0.0
    classification: str = "store_context"
    summary: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class StockSourceReliability(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    source: str
    tier: int = 3
    reliability_score: float = 0.5
    updated_at: datetime = Field(default_factory=_utcnow)


class StockAlert(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    alert_key: str
    batch_id: str
    recommendation_id: str | None = None
    symbol: str
    action: str
    priority_score: float = 0.0
    reason_summary: str | None = None
    status: str = "sent"
    created_at: datetime = Field(default_factory=_utcnow)


class StockDecisionSnapshot(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    symbol: str
    lane: str = "unknown"
    action: str
    recommended_stake: float = 0.0
    confidence: str | None = None
    quant_score: float | None = None
    reason_summary: str | None = None
    decision_time: datetime = Field(default_factory=_utcnow)


class StockListenerCheckpoint(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    provider: str
    key: str
    cursor: str | None = None
    last_polled_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class StockUniverseMember(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    symbol: str
    source: str
    source_detail: str | None = None
    sector_hint: str | None = None
    confidence: float = 0.5
    status: str = "active"
    last_seen_at: datetime = Field(default_factory=_utcnow)
    created_at: datetime = Field(default_factory=_utcnow)
