from __future__ import annotations

import asyncio
import json

from app.adapters.twelvedata import TwelveDataAdapter


async def main() -> None:
    adapter = TwelveDataAdapter()
    payload = await adapter.get_quotes(["AAPL", "NVDA", "VTI"])
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
