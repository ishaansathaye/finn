"""Pure, account-scoped expiration scenarios, never recommendations or orders.

Interface: construct Holding, UnderlyingQuote, optional OptionQuote objects and
call keyword-only evaluate_hedges(). Quotes are dollars per share; fees, cash,
premium_budget and output P&L are dollars. Expiry, as_of and now must be aware
datetimes; expiry is the actual contract expiration instant, not a date string.
The caller supplies verified unencumbered holdings in exactly one account and
cash_available in that same account. No broker state is fetched or inferred.

P&L is incremental to current shares * underlying midpoint, NOT historical tax
cost basis. Strategies are independent alternatives, not concurrent orders.
Buy puts at ask, sell calls/stock at bid. Reserve settlement fees on every option
leg even when it expires worthless. Cash must fund the gross put purchase plus
all reserved fees without assuming call proceeds; premium_budget limits positive
net debit including fees. No new shares or margin financing are modeled.

Result is a JSON-safe projection without account identifiers. Global malformed
inputs raise ValueError; unavailable/invalid option candidates or infeasible
strategies return status='rejected' with stable reason codes and no payoff data.
max_loss/max_profit are nonnegative magnitudes; min_pnl/max_pnl retain signed
bounds. max_profit=None means unbounded ONLY when profit_unbounded=True. Partial
hedges retain residual stock risk and upside. Unknown inputs are not coerced.

OIC payoff references (see Max Loss/Max Gain narrative):
https://www.optionseducation.org/strategies/all-strategies/protective-put-married-put
https://www.optionseducation.org/strategies/all-strategies/collar-protective-collar
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal


@dataclass(frozen=True)
class Holding:
    account_id: str
    symbol: str
    shares: float
    core_holding: bool = False
    encumbered_shares: float = 0.0


@dataclass(frozen=True)
class UnderlyingQuote:
    symbol: str
    bid: float
    ask: float
    as_of: datetime


@dataclass(frozen=True)
class OptionQuote:
    symbol: str
    option_type: Literal["put", "call"]
    strike: float
    expiry: datetime
    bid: float
    ask: float
    as_of: datetime
    multiplier: int = 100
    standard: bool = True


@dataclass(frozen=True)
class HedgeFees:
    option_per_contract: float = 0.65
    stock_order: float = 0.0
    settlement_per_contract: float = 0.0


def _finite(value: object, name: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{name} exceeds numeric range") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _nonnegative(value: object, name: str) -> float:
    number = _finite(value, name)
    if number < 0:
        raise ValueError(f"{name} must be nonnegative")
    return number


def _aware(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise ValueError(f"{name} is outside the supported UTC datetime range") from None


def _symbol(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9.-]{0,14}", value):
        raise ValueError("symbol must be a ticker")
    return value.upper()


def _quote(quote: UnderlyingQuote | OptionQuote, now: datetime, max_age: float) -> None:
    bid = _nonnegative(quote.bid, "bid")
    ask = _nonnegative(quote.ask, "ask")
    if bid > ask:
        raise ValueError("crossed quote")
    age = (now - _aware(quote.as_of, "as_of")).total_seconds()
    if not 0 <= age <= max_age:
        raise ValueError("quote must be fresh and not future-dated")


def _option_reasons(
    quote: OptionQuote | None, kind: str, symbol: str, now: datetime, max_age: float,
) -> list[str]:
    if quote is None:
        return [f"missing_{kind}"]
    if not isinstance(quote, OptionQuote):
        return [f"invalid_{kind}"]
    try:
        if _symbol(quote.symbol) != symbol or quote.option_type != kind:
            return [f"{kind}_underlying_or_type_mismatch"]
        if type(quote.multiplier) is not int or quote.multiplier != 100 or quote.standard is not True:
            return [f"{kind}_nonstandard_contract"]
        _nonnegative(quote.strike, "strike")
        if _aware(quote.expiry, "expiry") <= now:
            return [f"{kind}_expired"]
        _quote(quote, now, max_age)
    except ValueError:
        return [f"invalid_{kind}"]
    return []


def _rejected(reasons: list[str]) -> dict:
    return {
        "status": "rejected", "reasons": reasons, "scenarios": [],
        "max_loss": None, "max_profit": None, "min_pnl": None, "max_pnl": None,
        "profit_unbounded": None, "execution_allowed": False,
    }


def _scenario_row(
    *, shares: float, mark: float, prices: list[float], net_debit: float,
    cash_required: float, fees: float, covered: int = 0, sold: float = 0,
    put: OptionQuote | None = None, call: OptionQuote | None = None,
) -> dict:
    baseline = _finite(shares * mark, "baseline_value")
    residual = shares - sold - covered

    def terminal(price: float) -> float:
        # Piecewise terminal value avoids cancellation between large stock/call legs.
        protected_price = max(price, put.strike) if put is not None else price
        if call is not None:
            protected_price = min(protected_price, call.strike)
        return _finite(residual * price + covered * protected_price - net_debit, "terminal_value")

    minimum = _finite(terminal(0) - baseline, "min_pnl")
    unbounded = residual > 0 or (covered > 0 and call is None)
    maximum = None if unbounded else _finite(
        terminal(call.strike if call is not None else 0) - baseline, "max_pnl"
    )
    scenarios = []
    for price in prices:
        value = terminal(price)
        scenarios.append({
            "underlying_price": price, "terminal_value": value,
            "pnl": _finite(value - baseline, "scenario_pnl"),
        })
    return {
        "status": "evaluated", "reasons": [], "execution_allowed": False,
        "net_debit": _finite(net_debit, "net_debit"),
        "cash_required": _nonnegative(cash_required, "cash_required"),
        "fees_reserved": _nonnegative(fees, "fees_reserved"),
        "covered_shares": covered, "unhedged_shares": residual, "shares_sold": sold,
        "put_purchase_price": float(put.ask) if put is not None else None,
        "call_sale_price": float(call.bid) if call is not None else None,
        "protected_share_floor": float(put.strike) if put is not None else None,
        "protected_share_ceiling": float(call.strike) if call is not None else None,
        "expiry": _aware(put.expiry, "expiry").isoformat() if put is not None else None,
        "min_pnl": minimum, "max_pnl": maximum,
        "max_loss": max(0.0, -minimum),
        "max_profit": None if unbounded else max(0.0, maximum),
        "profit_unbounded": unbounded, "scenarios": scenarios,
    }


def evaluate_hedges(
    *, account_id: str, holding: Holding, underlying_quote: UnderlyingQuote,
    cash_available: float, premium_budget: float, now: datetime,
    put: OptionQuote | None = None, call: OptionQuote | None = None,
    contracts: int = 1, reduce_shares: float = 0.0,
    stress_prices: list[float] | tuple[float, ...] | None = None,
    allow_call_away: bool = False, fees: HedgeFees = HedgeFees(),
    max_quote_age_seconds: float = 300.0,
) -> dict:
    """Evaluate independent hold, sell-to-cash, protective-put and covered-collar alternatives.

    Explicit call-away opt-in is required even for non-core holdings. Availability
    is analytical only: every strategy and the complete result disable execution.
    No confidence labels, profit probabilities, backtests or broker calls exist.
    """
    if not isinstance(holding, Holding) or not isinstance(underlying_quote, UnderlyingQuote):
        raise ValueError("holding and underlying_quote must be typed dataclasses")
    if not isinstance(account_id, str) or not account_id.strip() or holding.account_id != account_id:
        raise ValueError("holding must match the requested account")
    symbol = _symbol(holding.symbol)
    if _symbol(underlying_quote.symbol) != symbol:
        raise ValueError("underlying quote does not match holding")
    if type(holding.core_holding) is not bool or type(allow_call_away) is not bool:
        raise ValueError("core_holding and allow_call_away must be booleans")
    shares = _nonnegative(holding.shares, "shares")
    encumbered = _nonnegative(holding.encumbered_shares, "encumbered_shares")
    if encumbered > shares:
        raise ValueError("encumbered_shares cannot exceed shares")
    cash = _nonnegative(cash_available, "cash_available")
    budget = _nonnegative(premium_budget, "premium_budget")
    sold = _nonnegative(reduce_shares, "reduce_shares")
    if type(contracts) is not int or contracts < 1:
        raise ValueError("contracts must be a positive whole integer")
    covered = contracts * 100
    _nonnegative(covered, "covered_shares")
    if not isinstance(fees, HedgeFees):
        raise ValueError("fees must be HedgeFees")
    option_fee = _nonnegative(fees.option_per_contract, "option_per_contract")
    settlement_fee = _nonnegative(fees.settlement_per_contract, "settlement_per_contract")
    stock_fee = _nonnegative(fees.stock_order, "stock_order")
    now = _aware(now, "now")
    max_age = _nonnegative(max_quote_age_seconds, "max_quote_age_seconds")
    _quote(underlying_quote, now, max_age)
    mark = underlying_quote.bid / 2 + underlying_quote.ask / 2
    if stress_prices is None:
        stress_prices = [0, mark * 0.7, mark * 0.9, mark, mark * 1.1, mark * 1.3]
    if not isinstance(stress_prices, (list, tuple)):
        raise ValueError("stress_prices must be a list or tuple of numbers")
    prices = [_nonnegative(price, "stress_price") for price in stress_prices]
    common = dict(shares=shares, mark=mark, prices=prices)
    strategies = {
        "hold_underlying": _scenario_row(**common, net_debit=0, cash_required=0, fees=0),
    }
    cash_fee = stock_fee if sold > 0 else 0.0
    cash_debit = _finite(cash_fee - sold * underlying_quote.bid, "cash_reduction_debit")
    cash_reasons = []
    # Subtract the requested quantity first so tiny encumbrances cannot round away.
    if sold > shares or encumbered > shares - sold:
        cash_reasons.append("insufficient_unencumbered_shares")
    if max(0, cash_debit) > cash:
        cash_reasons.append("insufficient_cash")
    strategies["cash_reduction"] = _rejected(cash_reasons) if cash_reasons else _scenario_row(
        **common, net_debit=cash_debit, cash_required=max(0, cash_debit), fees=cash_fee, sold=sold,
    )

    put_reasons = _option_reasons(put, "put", symbol, now, max_age)
    call_reasons = _option_reasons(call, "call", symbol, now, max_age)
    coverage_reasons = (
        ["insufficient_unencumbered_shares"]
        if covered > shares or encumbered > shares - covered else []
    )
    for name, use_call in (("protective_put", False), ("covered_collar", True)):
        reasons = put_reasons + coverage_reasons
        if use_call:
            reasons = reasons + call_reasons
            if not allow_call_away:
                reasons.append("call_away_not_allowed")
            if not put_reasons and not call_reasons:
                if _aware(put.expiry, "expiry") != _aware(call.expiry, "expiry"):
                    reasons.append("expiry_mismatch")
                if put.strike > call.strike:
                    reasons.append("put_strike_above_call_strike")
        if reasons:
            strategies[name] = _rejected(reasons)
            continue
        legs = 2 if use_call else 1
        reserved = _nonnegative(contracts * legs * (option_fee + settlement_fee), "reserved_fees")
        required = _nonnegative(covered * put.ask + reserved, "cash_required")
        net_debit = _finite(required - (covered * call.bid if use_call else 0), "net_debit")
        if required > cash:
            reasons.append("insufficient_cash")
        if max(0, net_debit) > budget:
            reasons.append("premium_budget_exceeded")
        strategies[name] = _rejected(reasons) if reasons else _scenario_row(
            **common, net_debit=net_debit, cash_required=required, fees=reserved,
            covered=covered, put=put, call=call if use_call else None,
        )

    return {
        "schema_version": 1, "symbol": symbol, "account_scoped": True,
        "as_of": now.isoformat(), "reference_price": mark,
        "baseline_value": _finite(shares * mark, "baseline_value"),
        "pnl_basis": "current_underlying_midpoint_not_historical_cost",
        "analysis_kind": "expiration_scenario_estimate", "is_backtest": False,
        "profit_probability": None, "option_execution_enabled": False,
        "execution_allowed": False, "strategies": strategies,
        "limitations": [
            "Independent alternatives, not a combined portfolio or trading recommendation.",
            "Expiry-only intrinsic payoffs; no pre-expiry mark-to-market or path simulation.",
            "No backtest, expected return, or probability of profit is estimated.",
            "Bid/ask fills are assumptions, not guarantees; quote size and liquidity are unverified.",
            "Caller must verify account cash and unencumbered shares; no account aggregation.",
            "Gross put cost plus all fees must be funded without call-sale proceeds.",
            "Settlement fees reserved per leg even if worthless; premium budget includes fees.",
            "Taxes, dividends, interest, borrow costs and corporate actions are not modeled.",
            "Short calls may be assigned early; call-away can remove underlying shares.",
            "Put exercise may sell shares, including core holdings; closing before expiry differs.",
            "Bounds apply to modeled expiration payoffs only, not post-expiry exposure.",
        ],
        "sources": [
            "https://www.optionseducation.org/strategies/all-strategies/protective-put-married-put",
            "https://www.optionseducation.org/strategies/all-strategies/collar-protective-collar",
        ],
    }
