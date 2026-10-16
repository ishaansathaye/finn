from __future__ import annotations

import asyncio
import getpass
import os

from app.adapters.robinhood_prediction import RobinhoodPredictionAdapter
from app.config import get_settings


async def main() -> None:
    os.environ.setdefault("FINN_RH_DEBUG", "1")
    settings = get_settings()
    adapter = RobinhoodPredictionAdapter()
    username = settings.robinhood_username or input("Robinhood username/email: ").strip()
    password = settings.robinhood_password or getpass.getpass("Robinhood password: ")
    mfa_code = settings.robinhood_mfa_code or input("MFA code (press Enter to skip): ").strip() or None

    print("Starting Robinhood write-session bootstrap...", flush=True)
    print("Submitting mobile-style login request...", flush=True)

    session = await adapter.bootstrap_write_session(
        username=username,
        password=password,
        mfa_code=mfa_code,
        timeout_seconds=150,
        force_new_device_token=True,
    )
    print("Robinhood write session saved.")
    print(f"Session path: {adapter._write_session_path()}")  # noqa: SLF001
    print(f"Device token: {session.device_token}")


if __name__ == "__main__":
    asyncio.run(main())
