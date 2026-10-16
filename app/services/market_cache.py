from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session

from app.services.integration_state import IntegrationStateStore


class MarketCacheService:
    def __init__(self) -> None:
        self.state_store = IntegrationStateStore()

    def get_json(
        self,
        session: Session,
        provider: str,
        key: str,
        *,
        max_age_seconds: int,
    ) -> Any | None:
        record = self.state_store.get_record(session, provider, key)
        if not record:
            return None
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=max_age_seconds)
        if record.updated_at < cutoff:
            return None
        return json.loads(record.value)

    def get_json_stale(self, session: Session, provider: str, key: str) -> Any | None:
        raw = self.state_store.get(session, provider, key)
        if raw is None:
            return None
        return json.loads(raw)

    def set_json(self, session: Session, provider: str, key: str, value: Any) -> None:
        self.state_store.set(session, provider, key, json.dumps(value))
