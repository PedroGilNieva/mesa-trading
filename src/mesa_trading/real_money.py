"""Guardas de la fase con pocos dólares. No hay broker real ni lectura de secretos.

El flag `enabled` no puede ser true. El tick de staging no llama estas
funciones: el servicio simulado sigue con su capital de papel y sin la
wallet real. Las pruebas las ejercen contra SimBroker.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Callable
from zoneinfo import ZoneInfo

from mesa_trading.book.store import close_position
from mesa_trading.broker.base import Broker
from mesa_trading.config import RealMoneyConfig
from mesa_trading.models import Ack, BookState, Order
from mesa_trading.util import as_utc, client_order_id, sha256_text

CDMX = ZoneInfo("America/Mexico_City")


class SecretUnavailable(RuntimeError):
    """Secret Manager no entregó la llave. No se manda la orden."""


class FlattenFailed(RuntimeError):
    """La posición sigue abierta. No se da por cerrada."""


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


def submit_once(broker: Broker, order: Order, loader: Callable[[], str | None], sent: dict[str, Ack]) -> Ack:
    """No envía si la llave falla. Un reintento con el mismo cloid no vuelve a cruzar."""
    require_key(loader)
    cloid = hyperliquid_cloid(order.client_order_id)
    cached = sent.get(cloid)
    if cached is not None:
        return cached
    ack = broker.submit(order)
    sent[cloid] = ack
    return ack


def cancel_live(book: BookState, broker: Broker) -> None:
    resting = book.resting_order
    if resting is None:
        return
    broker.cancel(resting.client_order_id)
    book.resting_order = None


def flatten(
    book: BookState,
    broker: Broker,
    now: datetime,
    *,
    loader: Callable[[], str | None],
    sent: dict[str, Ack],
    reason: str,
) -> Decimal | None:
    pos = book.position
    if pos is None:
        return None
    mark = _mark(book, pos.market, pos.entry_price)
    order = Order(
        client_order_id=client_order_id("flatten", pos.bot, pos.market, "close"),
        cycle_id="flatten",
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
    )
    submit_once(broker, order, loader, sent)
    fills = [fill for fill in broker.fills() if fill.client_order_id == order.client_order_id]
    if not fills:
        raise FlattenFailed("la orden de cierre no cruzó")
    fill = fills[-1]
    return close_position(book, fill.price, fill.fee_usd, fill.slippage_usd, fill.filled_at)


def kill_switch(
    book: BookState,
    broker: Broker,
    now: datetime,
    *,
    loader: Callable[[], str | None],
    sent: dict[str, Ack],
) -> None:
    """Cancela la orden viva, aplana y no reabre hasta la medianoche de Ciudad de México."""
    require_key(loader)
    book.halted_until = next_cdmx_midnight(now)
    book.halt_rule = "kill_switch"
    cancel_live(book, broker)
    flatten(book, broker, now, loader=loader, sent=sent, reason="kill_switch")


def enforce_daily_loss(
    book: BookState,
    broker: Broker,
    now: datetime,
    caps: RealMoneyConfig,
    *,
    loader: Callable[[], str | None],
    sent: dict[str, Ack],
) -> bool:
    """A −10 USD realizados en el día CDMX: cancela, aplana y frena."""
    if book.day_realized_pnl_usd > -caps.max_daily_loss_usd:
        return False
    require_key(loader)
    book.halted_until = next_cdmx_midnight(now)
    book.halt_rule = "perdida_diaria_real"
    cancel_live(book, broker)
    flatten(book, broker, now, loader=loader, sent=sent, reason="perdida_diaria_real")
    return True


def reconcile_startup(
    book: BookState,
    broker: Broker,
    now: datetime,
    *,
    loader: Callable[[], str | None],
    sent: dict[str, Ack],
) -> bool:
    """Si el proceso cayó con posición u orden viva, cancela y aplana antes de operar."""
    if book.position is None and book.resting_order is None:
        return False
    require_key(loader)
    book.halted_until = next_cdmx_midnight(now)
    book.halt_rule = "reconciliacion"
    cancel_live(book, broker)
    flatten(book, broker, now, loader=loader, sent=sent, reason="reconciliacion")
    return True


def protect(caps: RealMoneyConfig, book: BookState) -> str:
    """El tick solo llegaría aquí con el flag apagado. No toca el libro."""
    if caps.enabled:
        raise RuntimeError("dinero real no está implementado")
    return "apagado"


def next_cdmx_midnight(now: datetime) -> datetime:
    local = as_utc(now).astimezone(CDMX)
    nxt = datetime(local.year, local.month, local.day, tzinfo=CDMX) + timedelta(days=1)
    return nxt.astimezone(timezone.utc)


def _mark(book: BookState, market: str, fallback: Decimal) -> Decimal:
    raw = book.last_prices.get(market)
    if not raw:
        return fallback
    return Decimal(raw)
