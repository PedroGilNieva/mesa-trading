"""Un short gana si el precio baja, y las comisiones salen del PnL en los dos lados."""

from datetime import datetime, timezone
from decimal import Decimal

from mesa_trading.book.store import close_position, fresh_book, open_position
from mesa_trading.models import Position


def test_short_pnl_subtracts_open_and_close_fees():
    when = datetime(2026, 11, 18, 18, 0, tzinfo=timezone.utc)
    book = fresh_book(Decimal("1000"), when)
    position = Position(
        market="BTC-PERP",
        bot="gambler",
        side="short",
        qty=Decimal("1"),
        entry_price=Decimal("100"),
        notional_usd=Decimal("100"),
        opened_at=when,
        fees_usd=Decimal("0.10"),
        slippage_usd=Decimal("0.05"),
        kind="perp",
    )
    open_position(book, position, Decimal("0.10"), Decimal("0.05"))
    pnl = close_position(book, Decimal("90"), Decimal("0.10"), Decimal("0.05"), when)
    assert pnl == Decimal("9.70")
    assert book.realized_pnl_usd == Decimal("9.70")
    assert book.day_realized_pnl_usd == Decimal("9.70")
    assert book.fees_usd == Decimal("0.20")
    assert book.cash_usd == Decimal("1009.70")
    assert book.position is None

    losing = fresh_book(Decimal("1000"), when)
    again = position.model_copy(deep=True)
    open_position(losing, again, Decimal("0.10"), Decimal("0.05"))
    down = close_position(losing, Decimal("110"), Decimal("0.10"), Decimal("0.05"), when)
    assert down == Decimal("-10.30")
    assert losing.cash_usd == Decimal("989.70")
