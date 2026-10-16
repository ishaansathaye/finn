from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session, delete, select

from app.adapters.snaptrade import SnapTradeAdapter
from app.models import Account, AccountSnapshot, Holding


class PortfolioSyncService:
    def __init__(self, adapter: SnapTradeAdapter) -> None:
        self.adapter = adapter

    def _extract_cash_snapshot(self, holdings_payload: dict, total_amount: float) -> tuple[float, float]:
        balances = holdings_payload.get("balances", []) if isinstance(holdings_payload, dict) else []
        usd_balances = []
        for balance in balances:
            currency = (balance.get("currency") or {}).get("code")
            if currency == "USD":
                usd_balances.append(balance)
        if not usd_balances:
            return total_amount, total_amount
        cash_balance = sum(float(balance.get("cash") or 0.0) for balance in usd_balances)
        buying_power = sum(float(balance.get("buying_power") or 0.0) for balance in usd_balances)
        if buying_power <= 0 < cash_balance:
            buying_power = cash_balance
        return cash_balance, buying_power

    def _extract_symbol(self, position: dict) -> str:
        symbol_obj = position.get("symbol")
        if isinstance(symbol_obj, dict):
            nested_symbol = symbol_obj.get("symbol")
            if isinstance(nested_symbol, dict):
                nested_value = nested_symbol.get("symbol") or nested_symbol.get("raw_symbol")
                if isinstance(nested_value, str) and nested_value:
                    return nested_value
            direct_value = symbol_obj.get("symbol") or symbol_obj.get("raw_symbol")
            if isinstance(direct_value, str) and direct_value:
                return direct_value
        direct_position_symbol = position.get("symbol")
        if isinstance(direct_position_symbol, str) and direct_position_symbol:
            return direct_position_symbol
        ticker = position.get("ticker")
        if isinstance(ticker, str) and ticker:
            return ticker
        return "UNKNOWN"

    def _extract_asset_type(self, position: dict) -> str:
        symbol_obj = position.get("symbol")
        if isinstance(symbol_obj, dict):
            nested_symbol = symbol_obj.get("symbol")
            if isinstance(nested_symbol, dict):
                type_obj = nested_symbol.get("type")
                if isinstance(type_obj, dict):
                    code = type_obj.get("code")
                    if isinstance(code, str) and code:
                        return code
        return "equity"

    async def sync(self, session: Session) -> str:
        accounts_payload = await self.adapter.list_accounts(session)
        if not accounts_payload:
            return "No SnapTrade accounts are connected yet. Run `connect` to reconnect Robinhood through SnapTrade."
        synced_accounts = 0
        synced_holdings = 0

        for payload in accounts_payload:
            external_id = payload.get("id")
            stmt = select(Account).where(Account.provider == "snaptrade", Account.external_id == external_id)
            account = session.exec(stmt).first()
            if not account:
                account = Account(
                    external_id=external_id,
                    provider="snaptrade",
                    account_type=payload.get("raw_type") or payload.get("meta", {}).get("type", "unknown"),
                    name=payload.get("name") or "Robinhood Account",
                    currency=((payload.get("balance") or {}).get("total") or {}).get("currency", "USD"),
                )
                session.add(account)
                session.commit()
                session.refresh(account)
            else:
                account.name = payload.get("name") or account.name
                account.account_type = payload.get("raw_type") or account.account_type
                session.add(account)
                session.commit()

            total_balance = ((payload.get("balance") or {}).get("total") or {})
            total_amount = float(total_balance.get("amount") or 0.0)
            holdings_payload = await self.adapter.get_account_holdings(session, external_id)
            cash_balance, buying_power = self._extract_cash_snapshot(holdings_payload, total_amount)
            session.add(
                AccountSnapshot(
                    account_id=account.id,
                    cash_balance=cash_balance,
                    buying_power=buying_power,
                    total_value=total_amount,
                    as_of=datetime.now(UTC).replace(tzinfo=None),
                )
            )
            session.commit()
            positions = holdings_payload.get("positions", []) if isinstance(holdings_payload, dict) else []
            session.exec(delete(Holding).where(Holding.account_id == account.id))
            session.commit()
            for position in positions:
                symbol = self._extract_symbol(position)
                quantity = float(position.get("units") or position.get("quantity") or 0.0)
                price = float(position.get("price") or 0.0)
                market_value = position.get("market_value")
                if isinstance(market_value, dict):
                    market_value = market_value.get("amount")
                market_value_amount = float(market_value or 0.0) if market_value is not None else 0.0
                if market_value_amount == 0.0 and price and quantity:
                    market_value_amount = price * quantity
                average_purchase_price = float(position.get("average_purchase_price") or 0.0)
                cost_basis = average_purchase_price * quantity if average_purchase_price and quantity else None
                session.add(
                    Holding(
                        account_id=account.id,
                        symbol=symbol,
                        quantity=quantity,
                        market_value=market_value_amount,
                        cost_basis=cost_basis,
                        asset_type=self._extract_asset_type(position),
                    )
                )
                synced_holdings += 1
            session.commit()
            synced_accounts += 1

        return f"Synced {synced_accounts} account(s) and {synced_holdings} holding(s)."
