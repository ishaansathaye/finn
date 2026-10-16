"""Explicit account configuration for the isolated read-only options probe."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping


class OptionsConfigError(ValueError):
    """A safe-to-display configuration error, never containing account values."""


def load_account_number(environ: Mapping[str, str] | None = None) -> str:
    """Require the trader's full account number, never infer from account listings.

    This deliberately does not import TraderConfig: its defaults could silently
    select an account when the operator has not supplied explicit configuration.
    """
    env = os.environ if environ is None else environ
    account = env.get("AGENTIC_ACCOUNT_NUMBER", "").strip()
    if not account:
        raise OptionsConfigError("AGENTIC_ACCOUNT_NUMBER must be explicitly configured")
    if not re.fullmatch(r"[0-9]{6,20}", account):
        raise OptionsConfigError("AGENTIC_ACCOUNT_NUMBER must be a full numeric account identifier")
    denied = {
        value.strip()
        for key in ("AGENTIC_MAIN_ACCOUNT_DENYLIST", "AGENTIC_SAVINGS_ACCOUNT_DENYLIST")
        for value in env.get(key, "").split(",")
        if value.strip()
    }
    if account in denied:
        raise OptionsConfigError("Configured account is in the trader account denylist")
    return account
