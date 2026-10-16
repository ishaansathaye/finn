from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
import re
from xml.etree import ElementTree

import httpx


class GoogleNewsAdapter:
    """A time-bounded RSS fallback for research context when Yahoo has no news."""

    url = "https://news.google.com/rss/search"

    def parse_rss(self, content: bytes, symbol: str, limit: int, *, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(UTC)
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError:
            return []
        pattern = re.compile(rf"(?<![A-Z0-9]){re.escape(symbol.upper())}(?![A-Z0-9])")
        rows: list[dict] = []
        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            if not pattern.search(title.upper()):
                continue
            try:
                published = parsedate_to_datetime(item.findtext("pubDate") or "")
            except (TypeError, ValueError):
                continue
            if published.tzinfo is None:
                published = published.replace(tzinfo=UTC)
            age = now - published.astimezone(UTC)
            if age < timedelta(minutes=-5) or age > timedelta(hours=48):
                continue
            rows.append(
                {
                    "title": title,
                    "summary": "",
                    "published_at": published.astimezone(UTC).isoformat(),
                    "provider": (item.findtext("source") or "Google News").strip(),
                    "url": item.findtext("link"),
                }
            )
            if len(rows) >= limit:
                break
        return rows

    async def get_news(self, symbol: str, limit: int = 5) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    self.url,
                    params={
                        "q": f'"{symbol}" stock when:2d',
                        "hl": "en-US",
                        "gl": "US",
                        "ceid": "US:en",
                    },
                )
                response.raise_for_status()
            return self.parse_rss(response.content, symbol, limit)
        except httpx.HTTPError:
            return []
