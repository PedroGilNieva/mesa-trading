"""Guardas de la fase con pocos dólares. No hay broker real ni lectura de secretos.

El flag `enabled` no puede ser true. El tick simulado solo aplana si aparece
`book/kill`. La wallet real no entra en este proceso.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Callable

from mesa_trading.book.store import close_position, equity_usd
from mesa_trading.broker.base import Broker
from mesa_trading.config import RealMoneyConfig
from mesa_trading.models import Ack, BookState, Order, Position
from mesa_trading.util import client_order_id, iso, sha256_text


class SecretUnavailable(RuntimeError):
    """Secret Manager no entregó la llave. No se manda la orden."""


class FlattenFailed(RuntimeError):
    """La posición sigue abierta. No se da por cerrada."""


class BookConflict(RuntimeError):
    """La escritura condicionada no pasó. No se mandó la orden."""


def phase_capital(desk_capital: Decimal, caps: RealMoneyConfig) -> Decimal:
    """El capital de la fase no pasa de max_capital_usd, aunque la mesa de papel tenga más."""
    if desk_capital <= 0:
        return Decimal("0")
    return min(desk_capital, caps.max_capital_usd)


def capped_notional(
    requested: Decimal,
    *,
    cash: Decimal,
    caps: RealMoneyConfig,
    fee_bps: Decimal,
    slip_bps: Decimal,
) -> Decimal:
    """Nocional cuyo costo, con comisión y slippage, cabe en el tope, en el saldo y en el capital."""
    if requested <= 0 or cash <= 0:
        return Decimal("0")
    rate = Decimal("1") + (fee_bps + slip_bps) / Decimal("10000")
    room = min(caps.max_order_usd, caps.max_capital_usd, cash)
    allowed = (room / rate).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if allowed <= 0:
        return Decimal("0")
    return min(requested, allowed)


def hyperliquid_cloid(client_order: str) -> str:
    """cloid de Hyperliquid: 0x y 16 bytes. El mismo client_order_id da el mismo cloid."""
    return "0x" + sha256_text(client_order)[:32]


def require_key(loader: Callable[[], str | None]) -> str:
    try:
        value = loader()
    except Exception as exc:
        raise SecretUnavailable("secret manager no respondió") from exc
    if value is None or not str(value).strip():
        raise SecretUnavailable("secret manager no entregó la llave")
    return str(value).strip()


def submit_once(
    broker: Broker,
    order: Order,
    loader: Callable[[], str | None],
    book: BookState,
    persist: Callable[[], bool] | None = None,
) -> Ack:
    """No envía si la llave falla. El mismo cloid no se vuelve a cruzar."""
    require_key(loader)
    cloid = hyperliquid_cloid(order.client_order_id)
    if cloid in book.sent_orders:
        return Ack(client_order_id=order.client_order_id, accepted=True, detail="idempotente")
    book.sent_orders[cloid] = order.client_order_id
    if len(book.sent_orders) > 50:
        book.sent_orders = dict(list(book.sent_orders.items())[-50:])
    if persist is not None and not persist():
        book.sent_orders.pop(cloid, None)
        raise BookConflict("escritura condicionada rechazada")
    return broker.submit(order)


def cancel_live(book: BookState, broker: Broker) -> bool:
    """Borra la orden del libro solo después de que el venue confirme."""
    resting = book.resting_order
    if resting is None:
        return True
    if not broker.cancel(resting.client_order_id):
        return False
    book.resting_order = None
    return True


def flatten(
    book: BookState,
    broker: Broker,
    now: datetime,
    *,
    loader: Callable[[], str | None],
    reason: str,
    persist: Callable[[], bool] | None = None,
) -> Decimal | None:
    pos = book.position
    if pos is None:
        return None
    _align_sim_venue(book, broker)
    mark = _mark(book, pos.market, pos.entry_price)
    if not book.inflight_close:
        book.flatten_nonce += 1
        book.inflight_close = client_order_id(f"{iso(now)}|{book.flatten_nonce}", pos.bot, pos.market, "close")
    order = Order(
        client_order_id=book.inflight_close,
        cycle_id=str(book.flatten_nonce),
        bot=pos.bot,
        market=pos.market,
        side="sell" if pos.side == "long" else "buy",
        action="close",
        order_type="market",
        qty=pos.qty,
        notional_usd=pos.qty * mark,
        decision_time=now,
        reason=reason,
        kind=pos.kind,
        reduce_only=True,
    )
    ack = submit_once(broker, order, loader, book, persist)
    fills = [fill for fill in broker.fills() if fill.client_order_id == order.client_order_id]
    if not fills or not ack.accepted:
        raise FlattenFailed("la orden de cierre no cruzó")
    fill = fills[-1]
    pnl = close_position(book, fill.price, fill.fee_usd, fill.slippage_usd, fill.filled_at)
    book.inflight_close = None
    return pnl


def kill_switch(
    book: BookState,
    broker: Broker,
    now: datetime,
    *,
    loader: Callable[[], str | None],
    persist: Callable[[], bool] | None = None,
) -> None:
    """El halt queda puesto antes de tocar la llave, la cancelación o el cierre."""
    book.halted_until = next_cdmx_midnight(now)
    book.halt_rule = "kill_switch"
    book.kill_latched = True
    if persist is not None and not persist():
        raise BookConflict("escritura condicionada rechazada")
    require_key(loader)
    if not cancel_live(book, broker):
        raise FlattenFailed("cancelacion no confirmada")
    flatten(book, broker, now, loader=loader, reason="kill_switch", persist=persist)


def day_pnl(book: BookState) -> Decimal:
    """Realizado del día más lo que la posición abierta vale ahora."""
    mark = None
    if book.position is not None:
        mark = _mark(book, book.position.market, book.position.entry_price)
    return equity_usd(book, mark) - book.day_start_equity_usd


def enforce_daily_loss(
    book: BookState,
    broker: Broker,
    now: datetime,
    caps: RealMoneyConfig,
    *,
    loader: Callable[[], str | None],
    persist: Callable[[], bool] | None = None,
) -> bool:
    """A −10 USD entre realizado y no realizado: cancela, aplana y frena."""
    if day_pnl(book) > -caps.max_daily_loss_usd:
        return False
    book.halted_until = next_cdmx_midnight(now)
    book.halt_rule = "perdida_diaria_real"
    if persist is not None and not persist():
        raise BookConflict("escritura condicionada rechazada")
    require_key(loader)
    if not cancel_live(book, broker):
        raise FlattenFailed("cancelacion no confirmada")
    flatten(book, broker, now, loader=loader, reason="perdida_diaria_real", persist=persist)
    return True


def reconcile_startup(
    book: BookState,
    broker: Broker,
    now: datetime,
    *,
    loader: Callable[[], str | None],
    persist: Callable[[], bool] | None = None,
) -> bool:
    """La posición que manda es la del venue. El libro se pone a la par."""
    venue = broker.positions()
    if not venue and book.position is None and book.resting_order is None:
        return False
    book.halted_until = next_cdmx_midnight(now)
    book.halt_rule = "reconciliacion"
    require_key(loader)
    if not cancel_live(book, broker):
        raise FlattenFailed("cancelacion no confirmada")
    if venue:
        _adopt_venue(book, venue[0], now)
        flatten(book, broker, now, loader=loader, reason="reconciliacion", persist=persist)
    elif book.position is not None:
        book.position = None
    return True


def protect(caps: RealMoneyConfig, book: BookState) -> str:
    """El tick solo llegaría aquí con el flag apagado. No toca el libro."""
    if caps.enabled:
        raise RuntimeError("dinero real no está implementado")
    return "apagado"


def next_cdmx_midnight(now: datetime) -> datetime:
    from mesa_trading.loop.cycle import _next_midnight

    return _next_midnight(now)


def _align_sim_venue(book: BookState, broker: Broker) -> None:
    if book.position is None or broker.positions():
        return
    seed = getattr(broker, "seed_position", None)
    if seed is None:
        return
    pos = book.position
    seed(pos.market, pos.side, pos.qty, pos.entry_price)


def _adopt_venue(book: BookState, venue: dict, now: datetime) -> None:
    market = str(venue["market"])
    side = venue["side"]
    qty = Decimal(str(venue["qty"]))
    if book.position is not None and book.position.market == market:
        book.position.qty = qty
        book.position.side = side
        return
    entry = Decimal(str(venue.get("entry") or _mark(book, market, Decimal("0"))))
    book.position = Position(
        market=market,
        bot="gambler",
        side=side,
        qty=qty,
        entry_price=entry,
        notional_usd=qty * entry,
        opened_at=now,
        kind="perp",
    )


def _mark(book: BookState, market: str, fallback: Decimal) -> Decimal:
    raw = book.last_prices.get(market)
    if not raw:
        return fallback
    return Decimal(raw)
