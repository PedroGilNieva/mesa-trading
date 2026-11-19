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
    BookConflict,
    FlattenFailed,
    SecretUnavailable,
    capped_notional,
    day_pnl,
    enforce_daily_loss,
    flatten,
    hyperliquid_cloid,
    kill_switch,
    next_cdmx_midnight,
    phase_capital,
    protect,
    rearm_kill,
    reconcile_startup,
    submit_once,
    VenueUnknown,
    _remember,
    _usable_mark,
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

    def cancel(self, client_order_id: str) -> bool:
        return self.inner.cancel(client_order_id)

    def seed_position(self, market: str, side: str, qty: Decimal, entry: Decimal) -> None:
        self.inner.seed_position(market, side, qty, entry)

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
    book = fresh_book(Decimal("100"), NOW)
    first = submit_once(broker, order, _key, book)
    second = submit_once(broker, order, _key, book)
    assert first.client_order_id == second.client_order_id
    assert broker.submits == 1
    assert len(broker.fills()) == 1
    cloid = hyperliquid_cloid("orden-1")
    assert book.sent_orders[cloid] == "filled:orden-1"
    assert cloid.startswith("0x")
    assert len(cloid) == 34


def test_secret_failure_does_not_send():
    broker = Counting(_broker())
    book = _book()

    def broken():
        raise TimeoutError("secret manager")

    with pytest.raises(SecretUnavailable):
        kill_switch(book, broker, NOW, loader=broken)
    assert broker.submits == 0
    assert book.position is not None
    assert book.resting_order is not None
    assert book.halt_rule == "kill_switch"
    assert book.halted_until == datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)

    def empty():
        return None

    crashed = fresh_book(Decimal("100"), NOW)
    crashed.position = _position()
    with pytest.raises(SecretUnavailable):
        reconcile_startup(crashed, broker, NOW, loader=empty)
    assert crashed.position is not None


def test_kill_switch_cancels_and_flattens_until_cdmx_midnight():
    broker = Counting(_broker())
    book = _book()
    kill_switch(book, broker, NOW, loader=_key)
    assert book.position is None
    assert book.resting_order is None
    assert book.halt_rule == "kill_switch"
    assert book.halted_until == next_cdmx_midnight(NOW)
    assert book.halted_until == datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)
    assert "resting-1" in broker.inner._cancelled
    fills = len(broker.fills())
    kill_switch(book, broker, NOW, loader=_key)
    assert len(broker.fills()) == fills
    assert broker.submits == 1


def test_daily_loss_counts_unrealized_and_a_smaller_loss_does_not():
    caps = RealMoneyConfig()
    calm = _book()
    calm.cash_usd = Decimal("70.01")
    broker = Counting(_broker())
    assert day_pnl(calm) == Decimal("-9.99")
    assert enforce_daily_loss(calm, broker, NOW, caps, loader=_key) is False
    assert calm.position is not None
    assert broker.submits == 0

    marked = _book()
    marked.day_realized_pnl_usd = Decimal("0")
    marked.cash_usd = Decimal("80")
    marked.last_prices["BTC-PERP"] = "50"
    assert day_pnl(marked) == Decimal("-10")
    assert enforce_daily_loss(marked, Counting(_broker()), NOW, caps, loader=_key) is True
    assert marked.position is None
    assert marked.halt_rule == "perdida_diaria_real"


def test_startup_follows_the_venue_position():
    book = fresh_book(Decimal("100"), NOW)
    book.position = _position()
    book.last_prices["BTC-PERP"] = "100"
    empty = Counting(_broker())
    assert reconcile_startup(book, empty, NOW, loader=_key) is True
    assert book.position is None
    assert empty.submits == 0

    venue = Counting(_broker())
    venue.seed_position("BTC-PERP", "long", Decimal("0.2"), Decimal("100"))
    bare = fresh_book(Decimal("100"), NOW)
    bare.last_prices["BTC-PERP"] = "100"
    assert reconcile_startup(bare, venue, NOW, loader=_key) is True
    assert bare.position is None
    assert venue.submits == 1
    assert venue.positions() == []


def test_flatten_without_a_fill_leaves_the_position():
    book = fresh_book(Decimal("100"), NOW)
    book.position = _position()
    broker = SimBroker(bid=None, ask=None, last_prices={})
    with pytest.raises(FlattenFailed):
        kill_switch(book, broker, NOW, loader=_key)
    assert book.position is not None


def test_second_flatten_uses_a_new_cloid_and_does_not_reuse_the_old_fill():
    broker = Counting(
        SimBroker(
            fee_schedule=FeeSchedule(taker_bps=Decimal("10"), min_fee_usd=Decimal("0")),
            spread_bps=Decimal("8"),
            extra_bps=Decimal("2"),
            liquidity_usd=Decimal("100000"),
            cash_usd=Decimal("100"),
            last_prices={"BTC-PERP": Decimal("100")},
            bid=Decimal("99"),
            ask=Decimal("101"),
        )
    )
    book = fresh_book(Decimal("100"), NOW)
    book.position = _position()
    book.last_prices["BTC-PERP"] = "100"
    from mesa_trading.real_money import flatten

    first = flatten(book, broker, NOW, loader=_key, reason="kill_switch")
    first_id = broker.fills()[0].client_order_id
    assert first is not None
    assert book.position is None
    book.position = _position()
    book.last_prices["BTC-PERP"] = "90"
    broker.inner.bid = Decimal("89")
    broker.inner.ask = Decimal("91")
    second = flatten(book, broker, NOW, loader=_key, reason="kill_switch")
    assert book.position is None
    assert len(broker.fills()) == 2
    assert broker.fills()[1].client_order_id != first_id
    assert hyperliquid_cloid(broker.fills()[0].client_order_id) != hyperliquid_cloid(broker.fills()[1].client_order_id)
    assert second != first
    assert broker.fills()[1].fee_usd > 0
    assert broker.fills()[1].slippage_usd > 0


def test_cancel_must_be_confirmed_before_dropping_the_order():
    book = _book()
    broker = Counting(_broker())
    broker.cancel = lambda _cid: False  # type: ignore[method-assign]
    with pytest.raises(FlattenFailed):
        kill_switch(book, broker, NOW, loader=_key)
    assert book.resting_order is not None
    assert book.halt_rule == "kill_switch"
    assert broker.submits == 0


def test_rejected_book_write_does_not_send():
    book = _book()
    broker = Counting(_broker())
    with pytest.raises(BookConflict):
        kill_switch(book, broker, NOW, loader=_key, persist=lambda: False)
    assert broker.submits == 0
    assert book.position is not None
    assert book.halt_rule == "kill_switch"


def test_reduce_only_does_not_open_a_position():
    broker = _broker()
    from mesa_trading.models import Order

    ack = broker.submit(
        Order(
            client_order_id="solo-cierre",
            cycle_id="1",
            bot="gambler",
            market="BTC-PERP",
            side="sell",
            action="close",
            order_type="market",
            qty=Decimal("0.1"),
            notional_usd=Decimal("10"),
            decision_time=NOW,
            kind="perp",
            reduce_only=True,
        )
    )
    assert ack.accepted is False
    assert ack.detail == "reduce_only_sin_posicion"
    assert broker.positions() == []


def test_failed_flatten_retries_with_a_new_nonce():
    broker = _broker()
    broker.bid = None
    broker.ask = None
    book = fresh_book(Decimal("100"), NOW)
    book.position = _position()
    book.last_prices["BTC-PERP"] = "100"
    with pytest.raises(FlattenFailed):
        flatten(book, broker, NOW, loader=_key, reason="kill_switch")
    assert book.position is not None
    assert book.inflight_close is None
    assert len(broker._orders) == 1
    broker.bid = Decimal("99")
    broker.ask = Decimal("101")
    flatten(book, broker, NOW, loader=_key, reason="kill_switch")
    assert book.position is None
    assert len(broker.fills()) == 1
    assert len(broker._orders) == 2
    assert broker.fills()[0].client_order_id == list(broker._orders)[-1]


def test_partial_reduce_only_keeps_the_remainder():
    broker = _broker()
    broker.seed_position("BTC-PERP", "long", Decimal("1"), Decimal("100"))
    ack = broker.submit(
        Order(
            client_order_id="parcial",
            cycle_id="1",
            bot="gambler",
            market="BTC-PERP",
            side="sell",
            action="close",
            order_type="market",
            qty=Decimal("0.4"),
            notional_usd=Decimal("40"),
            decision_time=NOW,
            kind="perp",
            reduce_only=True,
        )
    )
    assert ack.accepted is True
    assert broker.positions()[0]["qty"] == Decimal("0.6")
    assert broker.fills()[0].qty == Decimal("0.4")


def test_filled_cloid_survives_the_cap_of_fifty():
    book = fresh_book(Decimal("100"), NOW)
    book.inflight_close = "vivo"
    for index in range(60):
        _remember(book, f"0x{index:032x}", "rejected", f"r{index}")
    _remember(book, hyperliquid_cloid("vivo"), "filled", "vivo")
    assert len(book.sent_orders) <= 50
    assert book.sent_orders[hyperliquid_cloid("vivo")] == "filled:vivo"


def test_cancel_that_fills_is_adopted_and_flattened():
    broker = _broker()
    opened = broker.submit(
        Order(
            client_order_id="se-lleno",
            cycle_id="1",
            bot="house",
            market="BTC-PERP",
            side="buy",
            action="long",
            order_type="market",
            qty=Decimal("0.2"),
            notional_usd=Decimal("20"),
            decision_time=NOW,
            kind="perp",
        )
    )
    assert opened.detail == "filled"
    book = fresh_book(Decimal("100"), NOW)
    book.last_prices["BTC-PERP"] = "100"
    book.resting_order = RestingOrder(
        client_order_id="se-lleno",
        cycle_id="1",
        bot="house",
        market="BTC-PERP",
        side="buy",
        action="long",
        qty=Decimal("0.2"),
        notional_usd=Decimal("20"),
        limit_price=Decimal("100"),
        decision_time=NOW,
        expires_at=NOW,
        kind="perp",
    )
    kill_switch(book, broker, NOW, loader=_key)
    assert book.resting_order is None
    assert book.position is None
    assert book.halt_rule == "kill_switch"


def test_reconcile_cancels_open_orders_and_all_positions():
    broker = _broker()
    broker.submit(
        Order(
            client_order_id="limite-viva",
            cycle_id="1",
            bot="house",
            market="BTC-PERP",
            side="buy",
            action="quote",
            order_type="limit",
            qty=Decimal("0.1"),
            notional_usd=Decimal("10"),
            limit_price=Decimal("50"),
            decision_time=NOW,
            kind="perp",
        )
    )
    assert broker.open_orders()
    book = fresh_book(Decimal("100"), NOW)
    assert reconcile_startup(book, broker, NOW, loader=_key) is True
    assert broker.open_orders() == []


def test_reconcile_fails_closed_when_the_venue_cannot_be_read():
    book = fresh_book(Decimal("100"), NOW)
    book.position = _position()

    class Broken:
        def positions(self):
            raise TimeoutError("venue")

        def open_orders(self):
            return []

        def cancel(self, _client_order_id):
            return True

        def fills(self, since=None):
            return []

    with pytest.raises(VenueUnknown):
        reconcile_startup(book, Broken(), NOW, loader=_key)
    assert book.position is not None

    class EmptyByError:
        def positions(self):
            return None

        def open_orders(self):
            return []

        def cancel(self, _client_order_id):
            return True

        def fills(self, since=None):
            return []

    with pytest.raises(VenueUnknown):
        reconcile_startup(book, EmptyByError(), NOW, loader=_key)
    assert book.position is not None


def test_rearm_waits_until_the_kill_object_is_gone():
    book = fresh_book(Decimal("100"), NOW)
    book.kill_latched = True
    book.halt_rule = "kill_switch"
    assert rearm_kill(book, requested=True) is False
    assert book.kill_latched is True
    assert rearm_kill(book, requested=False) is True
    assert book.kill_latched is False
    assert book.halt_rule is None


def test_missing_price_is_not_one_dollar():
    book = fresh_book(Decimal("100"), NOW)
    assert _usable_mark(book, "BTC-PERP") is None


def test_protect_does_nothing_while_the_flag_is_off():
    book = _book()
    assert protect(RealMoneyConfig(), book) == "apagado"
    assert book.position is not None
