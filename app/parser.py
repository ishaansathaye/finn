from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ParsedCommand:
    action: str
    targets: list[str]


def parse_command(text: str) -> ParsedCommand | None:
    normalized = " ".join(text.strip().lower().split())
    if not normalized:
        return None
    if normalized.startswith("/"):
        normalized = normalized[1:]

    parts = normalized.split()
    action = parts[0].rstrip(":")
    aliases = {
        "r": "report",
        "g": "gamble",
        "gx": "gamble_small",
        "gs": "gamble_small",
        "gsl": "slate",
        "b": "bets",
        "bs": "betstats",
        "l": "latest",
        "pd": "pending",
        "pf": "portfolio",
        "st": "status",
        "sy": "sync",
        "c": "connect",
        "dn": "deny",
        "denied": "deny",
        "sk": "skip",
        "y": "accept",
        "yes": "accept",
        "n": "reject",
        "no": "reject",
        "ap": "approve",
        "approved": "approve",
        "x": "execute",
        "execute": "execute",
        "done": "executed",
        "executed": "executed",
        "h": "help",
        "latest": "latest",
        "pending": "pending",
        "help": "help",
        "ab": "about",
        "ask": "ask",
        "agent": "agent",
        "about": "about",
    }
    action = aliases.get(action, action)
    if action not in {
        "deny",
        "skip",
        "accept",
        "reject",
        "approve",
        "executed",
        "status",
        "portfolio",
        "connect",
        "sync",
        "report",
        "gamble",
        "gamble_small",
        "gamble_stage",
        "gamblex_select",
        "slate",
        "bets",
        "betstats",
        "execute",
        "latest",
        "pending",
        "help",
        "about",
        "ask",
        "agent",
    }:
        return None

    targets = [part.rstrip(",") for part in parts[1:]]
    if action == "gamble" and targets[:1] == ["slate"]:
        action = "slate"
        targets = targets[1:]
    if action == "gamble" and targets[:1] == ["x"]:
        action = "gamble_stage"
        targets = targets[1:]
    if action == "gamble_small" and targets[:1] == ["x"]:
        action = "gamblex_select"
        targets = targets[1:]
    if targets[:1] == ["pm"]:
        targets = targets[1:]
    return ParsedCommand(action=action, targets=targets)
