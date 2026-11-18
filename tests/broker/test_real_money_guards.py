"""Kill switch, halt de 10 USD y aplanado, contra SimBroker. Sin red y sin secretos."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from mesa_trading.book.store import fresh_book
from mesa_trading.broker.fees import FeeSchedule
from mesa_trading.broker.sim import SimBroker
from mesa_trading.config import RealMoneyConfig
from mesa_trading.models import Position, RestingOrder
from mesa_trading.real_money import (
    FlattenFailed,
    SecretUnavailable,
    capped_notional,
    enforce_daily_loss,
    hyperliquid_cloid,
    kill_switch,
    next_cdmx_midnight,
    phase_capital,
    protect,
    reconcile_startup,
    submit_once,
)
from mesa_trading.models import Order

NOW = datetime(2026, 11, 18, 0, 36, tzinfo=timezone.utc)


def _key() -> str:
    return "llave-de-prueba"


def _broker() -> SimBroker:
    return SimBroker(
        fee_schedule=FeeSchedule(taker_bps=Decimal("0"), min_fee_usd=Decimal("0")),
        spread_bps=Decimal("0"),
        extra_bps=Decimal("0"),
        liquidity_usd=Decimal("1000000"),
        cash_usd=Decimal("100"),
        last_prices={"BTC-PERP": Decimal("100")},
        bid=Decimal("99"),
        ask=Decimal("101"),
    )


def _position() -> Position:
    return Position(
        market="BTC-PERP",
        bot="gambler",
        side="long",
        qty=Decimal("0.2"),
        entry_price=Decimal("100"),
        notional_usd=Decimal("20"),
        opened_at=NOW,
        kind="perp",
    )


def _resting() -> RestingOrder:
    return RestingOrder(
        client_order_id="resting-1",
        cycle_id="c",
        bot="house",
        market="WETH-USDC",
        side="buy",
        action="quote",
        qty=Decimal("0.01"),
        notional_usd=Decimal("20"),
        limit_price=Decimal("1800"),
        decision_time=NOW,
        expires_at=NOW,
    )


def _book():
    book = fresh_book(Decimal("100"), NOW)
    book.position = _position()
    book.resting_order = _resting()
    book.last_prices["BTC-PERP"] = "100"
    book.day_realized_pnl_usd = Decimal("-10")
    return book


class Counting:
    def __init__(self, inner: SimBroker) -> None:
        self.inner = inner
        self.submits = 0

    def submit(self, order):
        self.submits += 1
        return self.inner.submit(order)

    def cancel(self, client_order_id: str) -> None:
        self.inner.cancel(client_order_id)

    def fills(self, since=None):
        return self.inner.fills(since)

    def positions(self):
        return self.inner.positions()

    def balance(self):
        return self.inner.balance()


def test_caps_include_slippage_and_bind_capital_to_100():
    caps = RealMoneyConfig()
    assert phase_capital(Decimal("1000"), caps) == Decimal("100")
    assert phase_capital(Decimal("40"), caps) == Decimal("40")
    rate = Decimal("1") + Decimal("70") / Decimal("10000")
    sized = capped_notional(
        Decimal("25"),
        cash=Decimal("100"),
        caps=caps,
        fee_bps=Decimal("60"),
        slip_bps=Decimal("10"),
    )
    assert sized < Decimal("25")
    assert sized * rate <= Decimal("25")
    tight = capped_notional(
        Decimal("25"),
        cash=Decimal("10"),
        caps=caps,
        fee_bps=Decimal("60"),
        slip_bps=Decimal("10"),
    )
    assert tight * rate <= Decimal("10")


def test_retry_uses_the_same_hyperliquid_cloid():
    broker = Counting(_broker())
    order = Order(
        client_order_id="orden-1",
        cycle_id="c",
        bot="gambler",
        market="BTC-PERP",
        side="buy",
        action="long",
        order_type="market",
        qty=Decimal("0.1"),
        notional_usd=Decimal("10"),
        decision_time=NOW,
        kind="perp",
    )
    sent: dict = {}
    first = submit_once(broker, order, _key, sent)
    second = submit_once(broker, order, _key, sent)
    assert first.client_order_id == second.client_order_id
    assert broker.submits == 1
    assert len(broker.fills()) == 1
    assert hyperliquid_cloid("orden-1") in sent
    assert hyperliquid_cloid("orden-1").startswith("0x")
    assert len(hyperliquid_cloid("orden-1")) == 34


def test_secret_failure_does_not_send():
    broker = Counting(_broker())
    book = _book()

    def broken():
        raise TimeoutError("secret manager")

    with pytest.raises(SecretUnavailable):
        kill_switch(book, broker, NOW, loader=broken, sent={})
    assert broker.submits == 0
    assert book.position is not None
    assert book.resting_order is not None
    assert book.halt_rule is None

    def empty():
        return None

    crashed = fresh_book(Decimal("100"), NOW)
    crashed.position = _position()
    with pytest.raises(SecretUnavailable):
        reconcile_startup(crashed, broker, NOW, loader=empty, sent={})
    assert crashed.position is not None


def test_kill_switch_cancels_and_flattens_until_cdmx_midnight():
    broker = Counting(_broker())
    book = _book()
    sent: dict = {}
    kill_switch(book, broker, NOW, loader=_key, sent=sent)
    assert book.position is None
    assert book.resting_order is None
    assert book.halt_rule == "kill_switch"
    assert book.halted_until == next_cdmx_midnight(NOW)
    assert book.halted_until == datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)
    assert "resting-1" in broker.inner._cancelled
    fills = len(broker.fills())
    kill_switch(book, broker, NOW, loader=_key, sent=sent)
    assert len(broker.fills()) == fills
    assert broker.submits == 1


def test_daily_loss_of_ten_dollars_flattens_and_a_smaller_loss_does_not():
    caps = RealMoneyConfig()
    sent: dict = {}
    calm = _book()
    calm.day_realized_pnl_usd = Decimal("-9.99")
    broker = Counting(_broker())
    assert enforce_daily_loss(calm, broker, NOW, caps, loader=_key, sent=sent) is False
    assert calm.position is not None
    assert broker.submits == 0

    hit = _book()
    assert enforce_daily_loss(hit, Counting(_broker()), NOW, caps, loader=_key, sent={}) is True
    assert hit.position is None
    assert hit.resting_order is None
    assert hit.halt_rule == "perdida_diaria_real"
    assert hit.halted_until == datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)


def test_startup_flattens_a_position_left_by_a_crash():
    book = fresh_book(Decimal("100"), NOW)
    book.position = _position()
    book.last_prices["BTC-PERP"] = "100"
    broker = Counting(_broker())
    assert reconcile_startup(book, broker, NOW, loader=_key, sent={}) is True
    assert book.position is None
    assert book.halt_rule == "reconciliacion"
    assert broker.submits == 1
    idle = fresh_book(Decimal("100"), NOW)
    assert reconcile_startup(idle, broker, NOW, loader=_key, sent={}) is False


def test_flatten_without_a_fill_leaves_the_position():
    book = fresh_book(Decimal("100"), NOW)
    book.position = _position()
    broker = SimBroker(bid=None, ask=None, last_prices={})
    with pytest.raises(FlattenFailed):
        kill_switch(book, broker, NOW, loader=_key, sent={})
    assert book.position is not None


def test_protect_does_nothing_while_the_flag_is_off():
    book = _book()
    assert protect(RealMoneyConfig(), book) == "apagado"
    assert book.position is not None
