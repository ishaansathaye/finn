from datetime import UTC, date, datetime
import json
from types import SimpleNamespace

import pytest

from app.services.holding_horizon import protect_long_hold, sell_cost_context
from app.services.llm_analysis import StockDecisionModel
from app.services.stock_research_feed import publish_research


def signal():
    return StockDecisionModel(symbol="CORE", action="exit", confidence="high",
                              sizing_intent="large", thesis_summary="Overweight and lagging the benchmark.")


@pytest.mark.parametrize("lane", ["probable_core", "confirmed_core"])
def test_price_and_concentration_do_not_force_core_sale(lane):
    decision = signal()
    assert not protect_long_hold(decision, {"holding_lane":lane, "holding_weight":.23, "trend_score":-4}, date(2026,3,19))
    assert (decision.action, decision.sizing_intent) == ("hold", "none")


def test_tactical_exit_and_unrealized_context():
    d = signal()
    assert not protect_long_hold(d, {"holding_lane":"tactical"}, date(2026,3,19))
    assert d.action == "exit"
    assert "20.0% below" in sell_cost_context(d.action, SimpleNamespace(cost_basis=100, market_value=80))
    assert "unknown" in sell_cost_context(d.action, SimpleNamespace(cost_basis=None, market_value=80))


def test_filing_can_support_trim_but_stale_or_future_evidence_cannot():
    day = date(2026,3,19)
    for filed, expected in (("2026-03-10","reduce"), ("2025-12-01","hold"), ("2026-03-20","hold")):
        d = signal()
        p = {"holding_lane":"confirmed_core", "asset_class":"stock", "fundamentals":{
            "revenue":{"filed":filed}, "eps_diluted":{"filed":filed},
            "trend_summary":{"revenue_growth_yoy_pct":-12,"eps_growth_yoy_pct":-31}},
             "recent_filings":[{"form_type":"10-Q","filing_date":filed}]}
        key = protect_long_hold(d, p, day)
        assert d.action == expected
        if key:
            assert filed in d.thesis_summary and "12.0%" in d.thesis_summary


def test_bankruptcy_is_a_distinct_material_alert():
    d = signal()
    key = protect_long_hold(d, {"holding_lane":"probable_core","recent_filings":[
        {"filing_date":"2026-03-18","form_type":"8-K","items":"1.03"}]}, date(2026,3,19))
    assert d.action == "exit" and key == "insolvency:2026-03-18"


def test_research_export_omits_portfolio_and_stale_data(tmp_path):
    stamp = datetime(2026,3,19,16,tzinfo=UTC)
    p = dict(symbol="TEST", sector="Healthcare", asset_class="stock", close=50, trend_score=2,
             fundamental_score=1, catalyst_score=0, quote_source="cache", quote_timestamp=stamp.timestamp(),
             holding_value=10000, account_id="private", action="buy")
    path = tmp_path/"market.json"
    publish_research(path, [p,dict(p,symbol="STALE",quote_timestamp=0)], stamp)
    payload = json.loads(path.read_text())
    assert {r["symbol"] for r in payload["research"]} == {"TEST"}
    assert payload["research"][0].keys() == {"symbol","sector","price","quoted_at","trend","fundamentals","catalyst"}
