import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.services.hedge_strategies import (
    HedgeFees,
    Holding,
    OptionQuote,
    UnderlyingQuote,
    evaluate_hedges,
)


NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
EXPIRY = NOW + timedelta(days=30)
ACCOUNT = "private-account-never-serialize"
HOLDING = Holding(account_id=ACCOUNT, symbol="XYZ", shares=100, core_holding=True)
STOCK = UnderlyingQuote(symbol="XYZ", bid=99, ask=101, as_of=NOW)
PUT = OptionQuote("XYZ", "put", 95, EXPIRY, 2, 3, NOW)
CALL = OptionQuote("XYZ", "call", 110, EXPIRY, 1, 2, NOW)


def evaluate(**changes):
    kwargs = dict(
        account_id=ACCOUNT, holding=HOLDING, underlying_quote=STOCK,
        cash_available=1000, premium_budget=1000, now=NOW,
        put=PUT, call=CALL, contracts=1, reduce_shares=50,
        stress_prices=[0, 90, 95, 100, 110, 150], allow_call_away=True,
        fees=HedgeFees(option_per_contract=1, stock_order=2),
    )
    kwargs.update(changes)
    return evaluate_hedges(**kwargs)


def strategy(name, **changes):
    return evaluate(**changes)["strategies"][name]


def pnl(row):
    return [point["pnl"] for point in row["scenarios"]]


def test_hold_and_cash_payoffs_use_stock_bid_and_fees():
    hold = strategy("hold_underlying")
    assert pnl(hold) == [-10000, -1000, -500, 0, 1000, 5000]
    assert hold["max_loss"] == 10000
    assert hold["max_profit"] is None and hold["profit_unbounded"] is True
    cash = strategy("cash_reduction")
    assert pnl(cash) == [-5052, -552, -302, -52, 448, 2448]
    assert cash["max_loss"] == 5052
    assert cash["net_debit"] == -4948


def test_protective_put_uses_ask_not_mid_and_reports_floor():
    put = strategy("protective_put")
    assert put["status"] == "evaluated"
    assert put["net_debit"] == 301
    assert pnl(put) == [-801, -801, -801, -301, 699, 4699]
    assert put["max_loss"] == 801
    assert put["min_pnl"] == -801
    assert put["max_profit"] is None and put["profit_unbounded"] is True


def test_collar_uses_call_bid_and_has_explicit_profit_cap():
    collar = strategy("covered_collar")
    assert collar["net_debit"] == 202
    assert collar["cash_required"] == 302
    assert pnl(collar) == [-702, -702, -702, -202, 798, 798]
    assert collar["max_loss"] == 702
    assert collar["max_profit"] == 798
    assert collar["max_pnl"] == 798
    assert collar["profit_unbounded"] is False


def test_partial_coverage_does_not_claim_whole_position_cap():
    row = strategy("covered_collar", holding=replace(HOLDING, shares=150))
    assert row["covered_shares"] == 100
    assert row["unhedged_shares"] == 50
    assert row["max_loss"] == 5702
    assert row["max_profit"] is None and row["profit_unbounded"] is True
    assert pnl(row)[-1] == 3298


def test_fully_cash_has_constant_loss_from_spread_and_fee():
    row = strategy("cash_reduction", reduce_shares=100)
    assert pnl(row) == [-102] * 6
    assert row["max_loss"] == 102
    assert row["max_profit"] == 0
    assert row["max_pnl"] == -102
    assert row["profit_unbounded"] is False


def test_default_core_policy_rejects_calls_not_puts():
    result = evaluate(allow_call_away=False)["strategies"]
    assert result["covered_collar"]["status"] == "rejected"
    assert "call_away_not_allowed" in result["covered_collar"]["reasons"]
    assert result["protective_put"]["status"] == "evaluated"


def test_no_call_away_is_respected_for_noncore_too():
    row = strategy("covered_collar", allow_call_away=False,
                   holding=replace(HOLDING, core_holding=False))
    assert row["status"] == "rejected"


@pytest.mark.parametrize("shares,encumbered", [(99, 0), (100, 1), (0, 0), (199.5, 100)])
def test_account_free_share_coverage_no_naked_calls(shares, encumbered):
    result = evaluate(holding=replace(HOLDING, shares=shares, encumbered_shares=encumbered))
    for name in ("protective_put", "covered_collar"):
        assert "insufficient_unencumbered_shares" in result["strategies"][name]["reasons"]


def test_no_cross_account_coverage():
    with pytest.raises(ValueError, match="account"):
        evaluate(account_id="different-account")


@pytest.mark.parametrize("contracts", [0, -1, 0.5, 1.0, True, "1", float("nan")])
def test_contracts_must_be_positive_whole_integers(contracts):
    with pytest.raises(ValueError):
        evaluate(contracts=contracts)


@pytest.mark.parametrize("field,value", [
    ("symbol", "OTHER"), ("option_type", "call"), ("option_type", "confident_put"),
    ("multiplier", 10), ("multiplier", 100.0), ("multiplier", True),
    ("standard", False), ("standard", "true"),
    ("strike", -1), ("strike", float("nan")), ("strike", "95"),
    ("bid", -1), ("ask", -1), ("ask", float("inf")), ("ask", True),
    ("bid", 4), ("expiry", NOW), ("expiry", NOW - timedelta(days=1)),
    ("expiry", EXPIRY.replace(tzinfo=None)), ("expiry", "2026-11-06"),
    ("as_of", NOW - timedelta(seconds=301)), ("as_of", NOW + timedelta(microseconds=1)),
    ("as_of", NOW.replace(tzinfo=None)), ("as_of", "high-confidence"),
])
def test_invalid_put_rejects_options_but_keeps_hold(field, value):
    result = evaluate(put=replace(PUT, **{field: value}))["strategies"]
    assert result["protective_put"]["status"] == "rejected"
    assert result["covered_collar"]["status"] == "rejected"
    assert result["hold_underlying"]["status"] == "evaluated"
    assert result["protective_put"]["scenarios"] == []


@pytest.mark.parametrize("call", [
    replace(CALL, expiry=EXPIRY + timedelta(days=1)), replace(CALL, symbol="OTHER"),
    replace(CALL, strike=94), replace(CALL, option_type="put"),
    replace(CALL, as_of=NOW - timedelta(seconds=301)), None, "high-confidence",
])
def test_invalid_call_does_not_disable_valid_put(call):
    result = evaluate(call=call)["strategies"]
    assert result["covered_collar"]["status"] == "rejected"
    assert result["protective_put"]["status"] == "evaluated"


def test_absent_options_keep_nonoption_comparisons():
    result = evaluate(put=None, call=None)["strategies"]
    assert result["protective_put"]["reasons"] == ["missing_put"]
    assert result["covered_collar"]["status"] == "rejected"
    assert result["cash_reduction"]["status"] == "evaluated"


@pytest.mark.parametrize("field,value", [
    ("bid", -1), ("ask", 98), ("ask", float("nan")), ("symbol", "OTHER"),
    ("as_of", NOW - timedelta(seconds=301)), ("as_of", NOW.replace(tzinfo=None)),
    ("as_of", NOW + timedelta(microseconds=1)),
])
def test_invalid_underlying_quote_fails_closed(field, value):
    with pytest.raises(ValueError):
        evaluate(underlying_quote=replace(STOCK, **{field: value}))


@pytest.mark.parametrize("kwargs", [
    {"cash_available": -1}, {"cash_available": "1000"}, {"premium_budget": float("inf")},
    {"premium_budget": True}, {"reduce_shares": -1}, {"stress_prices": [float("nan")]},
    {"stress_prices": [-1]}, {"stress_prices": ["100"]}, {"stress_prices": "100"},
    {"max_quote_age_seconds": float("inf")}, {"now": NOW.replace(tzinfo=None)},
    {"holding": replace(HOLDING, shares=-1)}, {"holding": replace(HOLDING, shares=True)},
    {"holding": replace(HOLDING, core_holding="false")}, {"allow_call_away": "false"},
    {"holding": replace(HOLDING, encumbered_shares=101)},
    {"holding": replace(HOLDING, symbol="secret arbitrary string")},
    {"fees": HedgeFees(option_per_contract=-1)},
    {"fees": HedgeFees(settlement_per_contract=float("nan"))},
])
def test_invalid_global_inputs_fail_without_coercion(kwargs):
    with pytest.raises(ValueError):
        evaluate(**kwargs)


def test_affordability_and_budget_include_fees():
    assert strategy("protective_put", cash_available=300)["status"] == "rejected"
    assert strategy("protective_put", premium_budget=300)["status"] == "rejected"
    assert strategy("protective_put", cash_available=301, premium_budget=301)["status"] == "evaluated"
    assert strategy("covered_collar", cash_available=301)["status"] == "rejected"
    assert strategy("covered_collar", premium_budget=201)["status"] == "rejected"
    assert strategy("covered_collar", cash_available=302, premium_budget=202)["status"] == "evaluated"


def test_net_credit_collar_zero_budget_still_requires_gross_cash():
    call = replace(CALL, bid=4, ask=5)
    row = strategy("covered_collar", call=call, premium_budget=0)
    assert row["net_debit"] == -98
    assert row["max_loss"] == 402
    assert row["max_profit"] == 1098
    assert strategy("covered_collar", call=call, cash_available=0)["status"] == "rejected"


def test_settlement_fees_are_reserved_for_every_option_leg():
    fees = HedgeFees(option_per_contract=1, settlement_per_contract=2)
    assert strategy("protective_put", fees=fees)["net_debit"] == 303
    assert strategy("covered_collar", fees=fees)["net_debit"] == 206


def test_cash_reduction_cannot_sell_encumbered_or_unowned_shares():
    assert strategy("cash_reduction", reduce_shares=101)["status"] == "rejected"
    assert strategy("cash_reduction", holding=replace(HOLDING, encumbered_shares=60))["status"] == "rejected"
    assert strategy("cash_reduction", reduce_shares=0)["net_debit"] == 0


def test_defaults_are_deterministic_json_and_explicitly_not_probabilities():
    kwargs = dict(account_id=ACCOUNT, holding=HOLDING, underlying_quote=STOCK,
                  cash_available=1000, premium_budget=1000, now=NOW)
    result = evaluate_hedges(**kwargs)
    assert result == evaluate_hedges(**kwargs)
    assert result["analysis_kind"] == "expiration_scenario_estimate"
    assert result["is_backtest"] is False
    assert result["profit_probability"] is None
    assert result["option_execution_enabled"] is False
    assert result["execution_allowed"] is False
    assert result["strategies"]["hold_underlying"]["scenarios"]
    encoded = json.dumps(result, allow_nan=False)
    assert ACCOUNT not in encoded
    assert "confidence" not in encoded


def test_freshness_boundary_and_timezone_equivalence():
    row = strategy("covered_collar", put=replace(PUT, as_of=NOW - timedelta(seconds=300)),
                   call=replace(CALL, expiry=EXPIRY.astimezone(timezone(timedelta(hours=-7)))))
    assert row["status"] == "evaluated"


def test_more_than_one_contract_and_fractional_residual():
    row = strategy("protective_put", contracts=2, holding=replace(HOLDING, shares=250.5))
    assert row["covered_shares"] == 200
    assert row["unhedged_shares"] == 50.5
    assert row["max_loss"] == 6652


def test_overflow_never_reaches_json():
    with pytest.raises(ValueError):
        evaluate(holding=replace(HOLDING, shares=1e308))
    with pytest.raises(ValueError):
        evaluate(stress_prices=[1e308])


def test_extreme_protection_preserves_signed_bounds_without_negative_loss():
    row = strategy("protective_put", put=replace(PUT, strike=150))
    assert row["min_pnl"] == 4699
    assert row["max_loss"] == 0


def test_unknown_confidence_keyword_is_not_accepted():
    with pytest.raises(TypeError):
        evaluate(confidence="very high")


def test_out_of_range_timezone_conversion_follows_validation_contract():
    stamp = datetime.min.replace(tzinfo=timezone(timedelta(hours=14)))
    with pytest.raises(ValueError):
        evaluate(now=stamp)
    assert strategy("protective_put", put=replace(PUT, expiry=stamp))["status"] == "rejected"


def test_even_tiny_encumbrance_cannot_be_rounded_away_for_coverage():
    holding = replace(HOLDING, encumbered_shares=1e-15)
    result = evaluate(holding=holding, reduce_shares=100)["strategies"]
    for name in ("cash_reduction", "protective_put", "covered_collar"):
        assert result[name]["status"] == "rejected"


@pytest.mark.parametrize("shares,contracts", [(100, 1), (125.5, 1), (200, 2), (250, 2)])
@pytest.mark.parametrize("put_strike,call_strike", [(0, 100), (80, 120), (100, 100), (110, 130)])
def test_payoff_grid_matches_independent_leg_arithmetic(shares, contracts, put_strike, call_strike):
    prices = [0, 50, 80, 99, 100, 110, 120, 200, 1e6]
    result = evaluate(
        holding=replace(HOLDING, shares=shares), contracts=contracts, stress_prices=prices,
        put=replace(PUT, strike=put_strike), call=replace(CALL, strike=call_strike),
    )["strategies"]
    quantity = contracts * 100
    for name in ("protective_put", "covered_collar"):
        row = result[name]
        for point in row["scenarios"]:
            price = point["underlying_price"]
            expected = shares * (price - 100) + quantity * max(put_strike - price, 0)
            expected -= quantity * 3 + contracts
            if name == "covered_collar":
                expected += quantity * (1 - max(price - call_strike, 0)) - contracts
            assert point["pnl"] == pytest.approx(expected)
            assert point["pnl"] >= row["min_pnl"]
            if row["max_pnl"] is not None:
                assert point["pnl"] <= row["max_pnl"]
        json.dumps(row, allow_nan=False)
