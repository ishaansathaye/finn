"""Read-only independent play evidence for unambiguous full-game winner contracts."""
from datetime import UTC, datetime
import re
from time import monotonic
from zoneinfo import ZoneInfo

import httpx

from app.services.event_exit_policy import GameEvidence, number, timestamp


ROOT = "https://site.api.espn.com/apis/site/v2/sports"
LEAGUES = {"KXNFLGAME": "football/nfl", "KXNBAGAME": "basketball/nba", "KXMLBGAME": "baseball/mlb"}
CONTRACT = re.compile(r"(?P<series>KX(?:NFL|NBA|MLB)GAME)-(?P<day>\d{2}[A-Z]{3}\d{2})(?P<time>\d{4})?"
                      r"(?P<teams>[A-Z]{4,8})-(?P<pick>[A-Z]{2,4})")


def match_fixture(event, match, phase="in"):
    if not match:
        return False
    comp = event["competitions"][0]
    if comp["status"]["type"]["state"] != phase:
        return False
    local = datetime.fromisoformat(event["date"].replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York"))
    teams = {x["homeAway"]: x["team"]["abbreviation"] for x in comp["competitors"]}
    return (local.strftime("%y%b%d").upper() == match["day"]
            and (not match["time"] or local.strftime("%H%M") == match["time"])
            and teams["away"] + teams["home"] == match["teams"] and match["pick"] in teams.values())


def parse_summary(body, event_id, match):
    header = body["header"]
    if str(header["id"]) != event_id or not match_fixture({"date": header["competitions"][0]["date"],
                                                          "competitions": header["competitions"]}, match):
        return None
    comp = header["competitions"][0]
    teams = {x["homeAway"]: x["team"] for x in comp["competitors"]}
    prediction = body["winprobability"][-1]
    home = number(prediction.get("homeWinPercentage"))
    tie = number(prediction.get("tiePercentage"))
    if home is None or not 0 <= home <= 1 or tie not in (0, 0.0):
        return None
    if match["series"] == "KXNFLGAME":
        drives = body["drives"]
        plays = {str(p["id"]): p for d in [*drives.get("previous", []), drives.get("current") or {}]
                 for p in d.get("plays", [])}
    else:
        plays = {str(p["id"]): p for p in body["plays"]}
    key = str(prediction["playId"])
    play = plays.get(key)
    if not play:
        return None
    at = timestamp(play.get("wallclock"))
    if at is None or any((timestamp(p.get("wallclock")) or 0) > at for p in plays.values()):
        return None
    period = int(play["period"]["number"])
    if match["series"] == "KXMLBGAME":
        half = play["period"]["type"]
        if half not in {"Top", "Bottom"}:
            return None
        context = f"{half} of inning {period}"
    else:
        clock = (play.get("clock") or {}).get("displayValue")
        if not clock:
            return None
        context = f"period {period}, {clock}"
        if match["series"] == "KXNFLGAME":
            possessing = str((play.get("start") or {}).get("team", {}).get("id") or "")
            labels = {str(team["id"]): team["abbreviation"] for team in teams.values()}
            if possessing not in labels:
                return None
            context += f", possession at start of play: {labels[possessing]}"
    desc = (f"{teams['away']['abbreviation']} {int(play['awayScore'])}, {teams['home']['abbreviation']} "
            f"{int(play['homeScore'])}; {context} (ESPN)")
    return GameEvidence(home if teams["home"]["abbreviation"] == match["pick"] else 1 - home, at, f"{event_id}:{key}", desc)


class SportsExitEvidence:
    def __init__(self):
        self.cache = {}

    async def _json(self, path, params):
        key = (path, tuple(sorted(params.items())))
        now = monotonic()
        if key in self.cache and self.cache[key][0] > now:
            return self.cache[key][1]
        self.cache = {k: v for k, v in self.cache.items() if v[0] > now}
        data = {}
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{ROOT}/{path}", params=params)
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError):
            pass
        self.cache[key] = (now + 15, data)
        return data

    async def get(self, symbol):
        match = CONTRACT.fullmatch(str(symbol or "").upper())
        if not match:
            return None
        try:
            day = datetime.strptime(match["day"], "%y%b%d").date()
            if abs((datetime.now(UTC).date() - day).days) > 1:
                return None
            league = LEAGUES[match["series"]]
            board = await self._json(f"{league}/scoreboard", {"dates": day.strftime("%Y%m%d")})
            ids = [str(e["id"]) for e in board["events"] if match_fixture(e, match)]
            if len(ids) == 1:
                return parse_summary(await self._json(f"{league}/summary", {"event": ids[0]}), ids[0], match)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError):
            pass
        return None

    async def final_yes(self, symbol):
        match = CONTRACT.fullmatch(str(symbol or "").upper())
        if not match:
            return None
        try:
            day = datetime.strptime(match["day"], "%y%b%d")
            board = await self._json(f"{LEAGUES[match['series']]}/scoreboard", {"dates": day.strftime("%Y%m%d")})
            events = [e for e in board["events"] if match_fixture(e, match, "post")]
            if len(events) != 1:
                return None
            comp = events[0]["competitions"][0]
            teams = comp["competitors"]
            if (comp["status"]["type"].get("completed") is not True or len(teams) != 2
                    or {t.get("winner") for t in teams} != {False, True}):
                return None
            return next(t["winner"] for t in teams if t["team"]["abbreviation"] == match["pick"])
        except (KeyError, IndexError, TypeError, ValueError, StopIteration):
            return None
