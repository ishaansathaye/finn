from datetime import UTC, datetime

import pytest

from app.services.event_exit_policy import GameEvidence, evaluate, exit_bid
from app.services.sports_exit_evidence import CONTRACT, match_fixture, parse_summary


def decide(now=1000, opened=600, quote=None, game=None, previous=None, side="yes", entry=.70):
    return evaluate(side=side, entry=entry, opened=opened, quote=quote or {"yes_bid_price": .69, "yes_ask_price": .71},
                    game=game, previous=previous, now=now)


def play(t=996, key="a", p=.48):
    return GameEvidence(p, t, key, "Q3, possession AWY")


def test_recent_purchase_and_ordinary_spread_are_not_reasons_to_sell():
    assert decide(opened=965, game=play()).mode == "hold"
    assert decide().mode == "hold"
    assert decide(game=play(p=.70)).mode == "hold"
    assert decide(game=play(p=.65)).mode == "hold"  # exit margin doesn't clear uncertainty and costs


def test_independent_plays_confirm_even_without_large_entry_drawdown():
    first = decide(game=play())
    assert first.mode == "wait"
    assert decide(now=1020, previous=first.state, game=play()).mode == "wait"
    assert decide(now=1004, previous=first.state, game=play(1003,"b")).mode == "wait"
    assert decide(now=1020, previous=first.state, game=play(1019,"b")).mode == "cut"
    assert decide(now=1020, previous=first.state, game=play(1019,"b",.85)).state is None
    assert decide(now=1250, previous=first.state, game=play(1249,"c")).mode == "wait"
    assert decide(now=1250, previous=first.state, game=play()).mode == "hold"


def test_severe_price_only_decline_never_auto_sells_and_can_still_request_review():
    book = {"yes_bid_price": .42, "yes_ask_price": .45}
    first = decide(quote=book)
    assert first.mode == "wait"
    assert decide(quote=book, previous=first.state, now=1035).mode == "wait"
    second = decide(quote={**book, "yes_bid_price": .41}, previous=first.state, now=1035)
    assert second.mode == "review"
    assert "Independent game evidence is unavailable" in second.reason
    assert decide(previous=second.state, now=1040).mode == "hold"


def test_no_uses_complement_of_executable_yes_ask_and_independent_yes_probability():
    book = {"yes_bid_price": .28, "yes_ask_price": .33, "no_bid_price": .99}
    assert exit_bid(book, "no", 1000) == pytest.approx(.67)
    first = decide(quote=book, side="no", game=play(p=.58))
    assert decide(now=1020, quote=book, side="no", game=play(1019,"b",.58), previous=first.state).mode == "cut"
    assert decide(quote=book, side="no", game=play(p=.15)).mode == "hold"


def test_exceptional_early_deterioration_can_request_approval_but_not_force_exit():
    book = {"yes_bid_price":.44, "yes_ask_price":.46}
    assert decide(opened=980, quote=book, game=play(t=978,p=.10)).mode == "hold"
    first = decide(opened=980, quote=book, game=play(p=.10))
    check = decide(now=1020, opened=980, quote=book, game=play(1018,"b",.12), previous=first.state)
    assert check.mode == "review" and "new" in check.reason


@pytest.mark.parametrize("extra", [
    {"yes_bid_price": "nan"}, {"yes_ask_price": 1}, {"yes_bid_price": .85}, {"yes_ask_price": .89},
    {"yes_ask_price": None}, {"timestamp": "malformed"}, {"timestamp": "1970-01-01T00:00:00Z"},
])
def test_bad_books_cannot_support_any_exit(extra):
    assert exit_bid({"yes_bid_price": .69, "yes_ask_price": .70, **extra}, "yes", 1000) is None


def test_independent_feed_checks_identity_clock_and_latest_play():
    match = CONTRACT.fullmatch("KXNFLGAME-26SEP10NYGBAL-BAL")
    c = {"date": "2026-09-11T00:20Z", "status":{"type":{"state":"in"}}, "competitors":[
        {"homeAway":"home", "team":{"abbreviation":"BAL","id":"1"}},
        {"homeAway":"away","team":{"abbreviation":"NYG","id":"2"}}]}
    p = {"id":"44","period":{"number":3},"clock":{"displayValue":"4:00"},"homeScore":3,"awayScore":7,
         "start":{"team":{"id":"2"}}, "wallclock":"2026-09-11T02:10Z"}
    body = {"header":{"id":"abc","competitions":[c]}, "drives":{"current":{"plays":[p]}},
            "winprobability":[{"playId":"44", "homeWinPercentage":.22,"tiePercentage":0.0}]}
    assert match_fixture({"date":c["date"],"competitions":[c]}, match)
    ev = parse_summary(body, "abc", match)
    assert ev.yes_probability == .22 and "possession at start of play: NYG" in ev.label
    assert ev.at == datetime(2026,9,11,2,10,tzinfo=UTC).timestamp()
    assert parse_summary(body, "different", match) is None
    assert parse_summary(body, "abc", CONTRACT.fullmatch("KXNFLGAME-26SEP10NYGBUF-BUF")) is None
    body["drives"]["current"]["plays"].append({**p, "id":"45", "wallclock":"2026-09-11T02:11Z"})
    assert parse_summary(body, "abc", match) is None
    c["status"]["type"]["state"] = "post"
    assert parse_summary(body, "abc", match) is None


@pytest.mark.parametrize(("symbol","period","clock","expected"),[
    ("KXNBAGAME-26OCT15BOSNYK-BOS", {"number":4}, {"displayValue":"0:40"}, "period 4, 0:40"),
    ("KXMLBGAME-26OCT152030NYMBOS-NYM", {"number":8,"type":"Top"}, None, "Top of inning 8"),
])
def test_other_sports_play_alignment_and_away_probability(symbol,period,clock,expected):
    match = CONTRACT.fullmatch(symbol)
    away, home = match["teams"][:3], match["teams"][3:]
    c = {"date":"2026-10-16T00:30Z","status":{"type":{"state":"in"}},"competitors":[
        {"homeAway":"away","team":{"abbreviation":away}},{"homeAway":"home","team":{"abbreviation":home}}]}
    p = {"id":"play","period":period,"clock":clock,"wallclock":"2026-10-16T01:30Z",
         "awayScore":2,"homeScore":4}
    body = {"header":{"id":"fixture","competitions":[c]}, "plays":[p], "winprobability":[
        {"playId":"play","homeWinPercentage":.8,"tiePercentage":0.}]}
    ev = parse_summary(body,"fixture",match)
    assert expected in ev.label and ev.yes_probability == pytest.approx(.2)
    if match["time"]:
        assert parse_summary(body,"fixture",CONTRACT.fullmatch(symbol.replace("2030","1600"))) is None
    body["winprobability"][0]["playId"] = "unknown"
    assert parse_summary(body,"fixture",match) is None
