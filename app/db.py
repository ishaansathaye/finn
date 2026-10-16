from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings
import app.models  # noqa: F401


settings = get_settings()
engine = create_engine(settings.database_url, echo=False)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _migrate_sqlite_schema()


def _table_columns(table_name: str) -> set[str]:
    with engine.begin() as connection:
        rows = connection.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    return {str(row[1]) for row in rows}


def _ensure_columns(table_name: str, additions: dict[str, str]) -> None:
    existing = _table_columns(table_name)
    with engine.begin() as connection:
        for column, ddl in additions.items():
            if column in existing:
                continue
            connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column} {ddl}"))


def _migrate_sqlite_schema() -> None:
    _ensure_columns(
        "eventposition",
        {
            "source": "VARCHAR DEFAULT 'manual'",
            "broker_account_id": "VARCHAR",
            "broker_position_id": "VARCHAR",
            "broker_order_id": "VARCHAR",
            "broker_contract_id": "VARCHAR",
            "broker_event_id": "VARCHAR",
            "quantity": "FLOAT",
            "avg_fill_price": "FLOAT",
            "event_title": "VARCHAR",
            "pick_label": "VARCHAR",
            "category": "VARCHAR",
            "time_label": "VARCHAR",
            "opened_at": "DATETIME",
        },
    )
    _ensure_columns(
        "eventbetoutcome",
        {
            "settlement_price": "FLOAT",
            "realized_pnl_without_fees_usd": "FLOAT",
            "broker_execution_id": "VARCHAR",
        },
    )


@contextmanager
def get_session() -> Session:
    with Session(engine) as session:
        yield session
