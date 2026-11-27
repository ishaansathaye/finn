"""Exit value is not the edge for buying another contract."""
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite


def timestamp(value):
    try:
        if isinstance(value, (float, int)):
            return value if isfinite(value) else None
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def number(value):
    try:
        val = float(value)
        return val if isfinite(val) else None
    except (TypeError, ValueError):
        return None


def exit_bid(quote, side, now):
    bid, ask = number(quote.get("yes_bid_price")), number(quote.get("yes_ask_price"))
    if bid is None or ask is None or not 0 < bid <= ask < 1 or ask - bid > .060001:
        return None
    for key in ("updated_at", "timestamp", "as_of", "_observed"):
        if quote.get(key) is not None:
            stamp = timestamp(quote[key])
            if stamp is None or not -3 <= now - stamp <= 30:
                return None
    return (bid if side == "yes" else 1 - ask) if side in {"yes", "no"} else None


@dataclass(frozen=True)
class GameEvidence:
    yes_probability: float
    at: float
    play: str
    label: str


@dataclass(frozen=True)
class ExitDecision:
    mode: str = "hold"
    reason: str = ""
    state: dict | None = None
    opposite_edge: float = 0.


def evaluate(*, side, entry, opened, quote, game, previous, now):
    bid = exit_bid(quote, side, now)
    entry = number(entry)
    if bid is None or entry is None or not 0 < entry < 1 or opened is None or opened > now:
        return ExitDecision()
    cooling = now - opened < 120
    severe = entry - bid >= .15 and bid <= .75 * entry
    valid_game = (game and 0 <= game.yes_probability <= 1 and -3 <= now - game.at <= 120)
    if valid_game:
        fair = game.yes_probability if side == "yes" else 1 - game.yes_probability
        if bid - .02 - fair < .06:
            return ExitDecision()
        if cooling and (not severe or bid - .02 - fair < .15 or game.at <= opened):
            return ExitDecision()
        mode, tick, asof = "early_review" if cooling else "cut", game.play, game.at
        reason = (f"{game.label}. Holding is estimated to be worth {fair:.0%} of payout; "
                  f"selling returns about {bid - .02:.0%} after a 2-cent cost allowance.")
        if cooling:
            reason += " The bet is new: this exceptional deterioration needs your approval."
    elif severe and not cooling:
        # An executable drawdown alone can request a review, never force a sale.
        mode, tick, asof = "review", f"{bid:.4f}:{quote.get('yes_ask_price')}:{quote.get('updated_at') or quote.get('timestamp') or ''}", now
        reason = (f"The exit price fell from an entry of ${entry:.2f} to ${bid:.2f}. "
                  "Independent game evidence is unavailable; selling limits further downside but gives up any recovery.")
    else:
        return ExitDecision()
    old = previous or {}
    same_run = old.get("mode") == mode and 0 <= now - old.get("first", 0) <= 180
    state = dict(old) if same_run else {"mode": mode, "first": now, "tick": tick, "asof": asof, "confirmed": False}
    if same_run and asof < state["asof"]:
        return ExitDecision()
    if same_run and tick != state["tick"] and asof > state["asof"] and now - state["first"] >= 15:
        state["confirmed"] = True
        state.update(tick=tick, asof=asof, first=now)
    if state["confirmed"]:
        return ExitDecision("review" if mode == "early_review" else mode, reason, state,
                            bid - .02 - fair if valid_game else 0.)
    return ExitDecision("wait", reason, state)
