"""Guardas de la fase con pocos dólares. No hay broker real ni lectura de secretos.

`enabled` no puede ser true, ni siquiera armado con model_construct. La mesa
de papel no lee `book/kill` y no se aplana por él.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Callable

from pydantic import ValidationError

from mesa_trading.book.store import close_position, equity_usd
from mesa_trading.broker.base import Broker
from mesa_trading.config import RealMoneyConfig
from mesa_trading.models import Ack, BookState, Fill, Order, Position, RestingOrder
from mesa_trading.util import client_order_id, iso, sha256_text

_ORDER_STATES = ("filled", "rejected", "sin_fill", "accepted", "pendiente")


class SecretUnavailable(RuntimeError):
    """Secret Manager no entregó la llave. No se manda la orden."""


class FlattenFailed(RuntimeError):
    """La posición sigue abierta. No se da por cerrada."""


class BookConflict(RuntimeError):
    """La escritura condicionada no pasó. No se mandó la orden."""


class VenueUnknown(RuntimeError):
    """No se pudo leer el venue. No se da la posición por cerrada."""


def phase_is_on(caps: RealMoneyConfig) -> bool:
    """Solo un modelo validado y encendido. model_construct no cuenta."""
    try:
        checked = RealMoneyConfig.model_validate(caps.model_dump())
    except (ValidationError, ValueError):
        return False
    return checked.enabled is True


def sized_notional(
    requested: Decimal,
    *,
    cash: Decimal,
    caps: RealMoneyConfig,
    fee_bps: Decimal,
    slip_bps: Decimal,
) -> Decimal:
    """Con la fase apagada el tamaño no cambia. Encendida, aplica el tope."""
    if not phase_is_on(caps):
        return requested
    return capped_notional(requested, cash=cash, caps=caps, fee_bps=fee_bps, slip_bps=slip_bps)


def rearm_kill(book: BookState, *, requested: bool) -> bool:
    """Después de la medianoche, y solo si el objeto kill ya no está."""
    if not book.kill_latched or requested:
        return False
    book.kill_latched = False
    if book.halt_rule == "kill_switch":
        book.halt_rule = None
    return True


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
    """No envía si la llave falla. Un cloid lleno no se vuelve a cruzar."""
    require_key(loader)
    cloid = hyperliquid_cloid(order.client_order_id)
    status = _order_state(broker, book, order.client_order_id)
    if status == "filled":
        _remember(book, cloid, "filled", order.client_order_id)
        return Ack(client_order_id=order.client_order_id, accepted=True, detail="idempotente")
    if status in {"rejected", "sin_fill"}:
        _remember(book, cloid, status, order.client_order_id)
        return Ack(client_order_id=order.client_order_id, accepted=False, detail=status)
    _remember(book, cloid, "pendiente", order.client_order_id)
    if persist is not None and not persist():
        book.sent_orders.pop(cloid, None)
        raise BookConflict("escritura condicionada rechazada")
    ack = broker.submit(order)
    _remember(book, cloid, _order_state(broker, book, order.client_order_id), order.client_order_id)
    return ack


def cancel_live(book: BookState, broker: Broker) -> bool:
    """Si la orden se llenó al cancelar, se adopta. Si no, no se borra del libro."""
    resting = book.resting_order
    if resting is None:
        return True
    if broker.cancel(resting.client_order_id):
        book.resting_order = None
        return True
    fills = [fill for fill in broker.fills() if fill.client_order_id == resting.client_order_id]
    if not fills:
        return False
    _adopt_fill(book, resting, fills[-1])
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
    mark = _usable_mark(book, pos.market)
    if mark is None:
        book.inflight_close = None
        raise FlattenFailed("sin precio")
    if book.inflight_close and _order_state(broker, book, book.inflight_close) != "filled":
        # Rechazada o sin fill: el cloid viejo no se vuelve a mandar.
        book.inflight_close = None
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
    fill = _fill_for(broker, order.client_order_id)
    if fill is None or not ack.accepted:
        book.inflight_close = None
        raise FlattenFailed("la orden de cierre no cruzó")
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
    """La posición que manda es la del venue. Si no se puede leer, no se toca el libro.

    Si el venue y el libro coinciden no se hace nada. Si no coinciden, se
    cancela lo abierto, se aplana y la mesa queda frenada hasta la medianoche
    de Ciudad de México: un desfase sin explicar no se opera.
    """
    positions, orders = read_venue(broker)
    if venue_matches(book, positions, orders):
        return False
    book.halted_until = next_cdmx_midnight(now)
    book.halt_rule = "reconciliacion"
    require_key(loader)
    for item in orders:
        _cancel_venue_order(book, broker, item)
    if not cancel_live(book, broker):
        raise FlattenFailed("cancelacion no confirmada")
    for venue in positions:
        _adopt_venue(book, venue, now)
        flatten(book, broker, now, loader=loader, reason="reconciliacion", persist=persist)
    if book.position is not None and not positions:
        book.position = None
    return True


def venue_matches(book: BookState, positions: list[dict], orders: list[dict]) -> bool:
    """Mismo mercado, lado y cantidad; y las órdenes abiertas son las del libro."""
    venue_orders = {str(item.get("client_order_id") or "") for item in orders}
    book_orders = {book.resting_order.client_order_id} if book.resting_order is not None else set()
    if venue_orders != book_orders:
        return False
    pos = book.position
    if pos is None:
        return not positions
    if len(positions) != 1:
        return False
    venue = positions[0]
    try:
        qty = Decimal(str(venue.get("qty")))
    except Exception:
        return False
    return str(venue.get("market")) == pos.market and venue.get("side") == pos.side and qty == pos.qty


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


def _adopt_fill(book: BookState, resting: RestingOrder, fill: Fill) -> None:
    if book.position is not None:
        return
    side = "short" if resting.side == "sell" or fill.side == "sell" else "long"
    book.position = Position(
        market=resting.market,
        bot=resting.bot,
        side=side,
        qty=fill.qty,
        entry_price=fill.price,
        notional_usd=fill.qty * fill.price,
        opened_at=fill.filled_at,
        fees_usd=fill.fee_usd,
        slippage_usd=fill.slippage_usd,
        kind=resting.kind,
    )
    book.last_prices[resting.market] = str(fill.price)


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


def read_venue(broker: Broker) -> tuple[list[dict], list[dict]]:
    try:
        positions = broker.positions()
    except Exception as exc:
        raise VenueUnknown("positions") from exc
    if positions is None:
        raise VenueUnknown("positions")
    reader = getattr(broker, "open_orders", None)
    if reader is None:
        return list(positions), []
    try:
        orders = reader()
    except Exception as exc:
        raise VenueUnknown("ordenes") from exc
    if orders is None:
        raise VenueUnknown("ordenes")
    return list(positions), list(orders)


def _usable_mark(book: BookState, market: str) -> Decimal | None:
    raw = book.last_prices.get(market)
    if raw:
        price = Decimal(raw)
        if price > 0:
            return price
    if book.position is not None and book.position.market == market and book.position.entry_price > 0:
        return book.position.entry_price
    return None


def _order_state(broker: Broker, book: BookState, client_order_id: str) -> str:
    ask = getattr(broker, "order_status", None)
    if ask is not None:
        try:
            status = ask(client_order_id)
        except Exception:
            status = "unknown"
        if status in _ORDER_STATES:
            return status
    if _fill_for(broker, client_order_id) is not None:
        return "filled"
    stored, _client = _split(book.sent_orders.get(hyperliquid_cloid(client_order_id), ""))
    return stored or "unknown"


def _split(value: str) -> tuple[str, str]:
    if ":" in value:
        status, client = value.split(":", 1)
        if status in _ORDER_STATES:
            return status, client
    if value:
        return "accepted", value
    return "", ""


def _remember(book: BookState, cloid: str, status: str, client_order_id: str) -> None:
    book.sent_orders[cloid] = f"{status}:{client_order_id}"
    if len(book.sent_orders) <= 50:
        return
    inflight = hyperliquid_cloid(book.inflight_close) if book.inflight_close else ""
    kept: dict[str, str] = {}
    rest: list[tuple[str, str]] = []
    for key, value in book.sent_orders.items():
        state, _client = _split(value)
        if key == inflight or state == "filled":
            kept[key] = value
        else:
            rest.append((key, value))
    room = 50 - len(kept)
    if room > 0:
        kept.update(dict(rest[-room:]))
    elif len(kept) > 50:
        filled = list(kept.items())[-50:]
        kept = dict(filled)
    book.sent_orders = kept


def _fill_for(broker: Broker, client_order_id: str) -> Fill | None:
    found = [fill for fill in broker.fills() if fill.client_order_id == client_order_id]
    return found[-1] if found else None


def _cancel_venue_order(book: BookState, broker: Broker, item: dict) -> None:
    client_order_id = str(item.get("client_order_id") or "")
    if not client_order_id:
        return
    if broker.cancel(client_order_id):
        return
    fill = _fill_for(broker, client_order_id)
    if fill is None:
        return
    resting = RestingOrder(
        client_order_id=client_order_id,
        cycle_id="venue",
        bot=str(item.get("bot") or "gambler"),
        market=str(item.get("market") or fill.market),
        side=item.get("side") or fill.side,
        action="close" if item.get("side") == "sell" else "long",
        qty=fill.qty,
        notional_usd=Decimal(str(item.get("notional_usd") or fill.qty * fill.price)),
        limit_price=fill.price,
        decision_time=fill.filled_at,
        expires_at=fill.filled_at,
        kind=item.get("kind") or "perp",
    )
    _adopt_fill(book, resting, fill)


def _mark(book: BookState, market: str, fallback: Decimal) -> Decimal:
    price = _usable_mark(book, market)
    return fallback if price is None else price
