from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session, select

from app.models import IntegrationState


class IntegrationStateStore:
    def get_record(self, session: Session, provider: str, key: str) -> IntegrationState | None:
        stmt = select(IntegrationState).where(
            IntegrationState.provider == provider,
            IntegrationState.key == key,
        )
        return session.exec(stmt).first()

    def get(self, session: Session, provider: str, key: str) -> str | None:
        record = self.get_record(session, provider, key)
        return record.value if record else None

    def set(self, session: Session, provider: str, key: str, value: str) -> None:
        record = self.get_record(session, provider, key)
        if record:
            record.value = value
            record.updated_at = datetime.now(UTC).replace(tzinfo=None)
        else:
            record = IntegrationState(provider=provider, key=key, value=value)
            session.add(record)
        session.commit()

    def delete(self, session: Session, provider: str, key: str) -> None:
        record = self.get_record(session, provider, key)
        if not record:
            return
        session.delete(record)
        session.commit()
