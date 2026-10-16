from __future__ import annotations

from snaptrade_client import SnapTrade

from app.config import get_settings
from app.db import get_session, init_db
from app.services.integration_state import IntegrationStateStore


def main() -> None:
    init_db()
    settings = get_settings()
    client = SnapTrade(
        client_id=settings.snaptrade_client_id or "",
        consumer_key=settings.snaptrade_consumer_key or "",
    )
    state_store = IntegrationStateStore()

    with get_session() as session:
        user_secret = state_store.get(session, "snaptrade", "user_secret")
        if not user_secret:
            register_response = client.authentication.register_snap_trade_user(
                user_id=settings.snaptrade_user_id
            )
            user_secret = register_response.body["userSecret"]
            state_store.set(session, "snaptrade", "user_secret", user_secret)

        login_response = client.authentication.login_snap_trade_user(
            user_id=settings.snaptrade_user_id,
            user_secret=user_secret,
            broker="ROBINHOOD",
            connection_type="read",
            connection_portal_version="v4",
            immediate_redirect=False,
        )
        url = login_response.body["redirectURI"]
    print(url)


if __name__ == "__main__":
    main()
