"""Una posición para toda la mesa. Escritura con precondición de generación y candado."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from pathlib import Path

from mesa_trading.models import BookState, Position
from mesa_trading.util import as_utc, iso

# 1: el realizado restaba el deslizamiento dos veces y no cobraba gas.
# 2 (#29): el deslizamiento va una vez, dentro del precio del fill; el gas se cobra por pata.
BOOK_ACCOUNTING_VERSION = 2


def fresh_book(capital: Decimal, now: datetime) -> BookState:
    day = as_utc(now).astimezone(ZoneInfo("America/Mexico_City")).date().isoformat()
    return BookState(
        cash_usd=capital,
        day_start_equity_usd=capital,
        day_start_cdmx=day,
        peak_equity_usd=capital,
        equity_hwm_usd=capital,
        slippage_double_counted_legacy_usd=Decimal("0"),
    )


def note_drawdown(book: BookState, equity: Decimal) -> Decimal:
    """Sube el pico y guarda el peor pico-a-valle, no solo el hueco de ahora."""
    if equity > book.peak_equity_usd:
        book.peak_equity_usd = equity
    current = book.peak_equity_usd - equity
    if current < 0:
        current = Decimal("0")
    if current > book.max_drawdown_usd:
        book.max_drawdown_usd = current
    return book.max_drawdown_usd


def unrealized_pnl(book: BookState, mark: Decimal | None = None) -> Decimal:
    """PnL de precio de la posición abierta. Las comisiones ya salieron del cash."""
    if book.position is None:
        return Decimal("0")
    price = mark if mark is not None else book.position.entry_price
    if book.position.side == "long":
        return (price - book.position.entry_price) * book.position.qty
    return (book.position.entry_price - price) * book.position.qty


def equity_usd(book: BookState, mark: Decimal | None = None) -> Decimal:
    if book.position is None:
        return book.cash_usd
    price = mark if mark is not None else book.position.entry_price
    if book.position.side == "long":
        return book.cash_usd + book.position.qty * price
    unrealized = (book.position.entry_price - price) * book.position.qty
    return book.cash_usd + book.position.notional_usd + unrealized


class BookStore:
    def read(self) -> tuple[BookState, int]:
        raise NotImplementedError

    def write(self, state: BookState, expected_generation: int) -> bool:
        raise NotImplementedError

    def try_lock(self, owner: str, now: datetime, ttl_s: int = 50) -> bool:
        raise NotImplementedError

    def unlock(self, owner: str) -> None:
        raise NotImplementedError

    def kill_requested(self) -> bool:
        return False


class FileBookStore(BookStore):
    def __init__(self, root: Path, capital: Decimal = Decimal("1000"), now: datetime | None = None) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._mu = threading.Lock()
        self._state_path = root / "state.json"
        self._lock_path = root / "lock"
        if not self._state_path.exists():
            initial = fresh_book(capital, now or datetime.now(timezone.utc))
            self._state_path.write_text(_dump(initial, 0), encoding="utf-8")

    def read(self) -> tuple[BookState, int]:
        payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        return BookState.model_validate(payload["state"]), int(payload["generation"])

    def write(self, state: BookState, expected_generation: int) -> bool:
        with self._mu:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            if int(payload["generation"]) != expected_generation:
                return False
            self._state_path.write_text(_dump(state, expected_generation + 1), encoding="utf-8")
            return True

    def try_lock(self, owner: str, now: datetime, ttl_s: int = 50) -> bool:
        with self._mu:
            current = self._read_lock()
            moment = as_utc(now)
            if current and current["owner"] != owner and as_utc(datetime.fromisoformat(current["expires"])) > moment:
                return False
            expires = moment + timedelta(seconds=ttl_s)
            self._lock_path.write_text(
                json.dumps({"owner": owner, "expires": iso(expires)}),
                encoding="utf-8",
            )
            return True

    def unlock(self, owner: str) -> None:
        with self._mu:
            current = self._read_lock()
            if current and current["owner"] == owner:
                self._lock_path.unlink(missing_ok=True)

    def kill_requested(self) -> bool:
        # book/kill junto a state.json. Se lee al empezar cada operación.
        path = self.root / "kill"
        if not path.is_file():
            return False
        return bool(path.read_text(encoding="utf-8").strip())

    def _read_lock(self) -> dict | None:
        if not self._lock_path.exists():
            return None
        return json.loads(self._lock_path.read_text(encoding="utf-8"))


def mark_price(book: BookState, market: str) -> Decimal | None:
    raw = book.last_prices.get(market)
    if raw is None:
        if book.position and book.position.market == market:
            return book.position.entry_price
        return None
    return Decimal(raw)


def open_position(
    book: BookState,
    position: Position,
    fee: Decimal,
    slip: Decimal,
    gas: Decimal = Decimal("0"),
) -> BookState:
    """Abre la posición. Sale del cash: lo pagado al precio del fill + comisión + gas.

    El precio del fill ya trae el deslizamiento (compra más caro, vende más
    barato), así que ``slip`` no se vuelve a restar: solo se anota para el
    desglose de costos.
    """
    _freeze_legacy_slippage(book)
    position.gas_usd = gas
    book.position = position
    book.fees_usd += fee
    book.slippage_usd += slip
    book.gas_usd += gas
    _note_market_costs(book, position.market, fee, slip, gas)
    book.cash_usd -= _committed_usd(position) + fee + gas
    return book


def close_position(
    book: BookState,
    exit_price: Decimal,
    fee: Decimal,
    slip: Decimal,
    when: datetime,
    reason: str | None = None,
    gas: Decimal = Decimal("0"),
) -> Decimal:
    """Cierra y devuelve el PnL realizado.

    pnl = diferencia entre los precios de los fills × cantidad - comisiones - gas
    (entrada y salida). El deslizamiento ya está en esos precios: se cobra una
    sola vez (issue #29). Antes también se restaba aquí.
    """
    assert book.position is not None
    _freeze_legacy_slippage(book)
    pos = book.position
    gross = (exit_price - pos.entry_price) * pos.qty if pos.side == "long" else (pos.entry_price - exit_price) * pos.qty
    pnl = gross - fee - gas - pos.fees_usd - pos.gas_usd
    if pos.side == "long":
        book.cash_usd += pos.qty * exit_price - fee - gas
    else:
        book.cash_usd += pos.notional_usd + (pos.entry_price - exit_price) * pos.qty - fee - gas
    book.realized_pnl_usd += pnl
    book.day_realized_pnl_usd += pnl
    book.closed_trades += 1
    key = reason or "sin_motivo"
    book.closed_by_reason[key] = int(book.closed_by_reason.get(key, 0)) + 1
    book.fees_usd += fee
    book.slippage_usd += slip
    book.gas_usd += gas
    _note_market_costs(book, pos.market, fee, slip, gas)
    book.position = None
    _ = when
    return pnl


def _committed_usd(position: Position) -> Decimal:
    """Lo que la entrada saca del cash, sin costos.

    Largo: cantidad × precio del fill (lo que se pagó de verdad). Corto: el
    nocional queda de margen y vuelve entero al cerrar, más o menos el movimiento.
    """
    if position.side == "long":
        return position.qty * position.entry_price
    return position.notional_usd


def _freeze_legacy_slippage(book: BookState) -> None:
    """Primera escritura con la contabilidad de #29 sobre un libro viejo.

    El realizado viejo restó dos veces el deslizamiento de las operaciones ya
    cerradas. No se reescribe: se anota cuánto fue, para que el reporte separe
    el PnL del libro del neto corregido. Un libro nuevo empieza en 0.
    """
    if book.slippage_double_counted_legacy_usd is not None:
        return
    open_slip = book.position.slippage_usd if book.position is not None else Decimal("0")
    book.slippage_double_counted_legacy_usd = book.slippage_usd - open_slip


def _note_market_costs(book: BookState, market: str, fee: Decimal, slip: Decimal, gas: Decimal) -> None:
    current = book.costs_by_market.get(market) or {}
    book.costs_by_market[market] = {
        "fees_usd": format(Decimal(str(current.get("fees_usd", "0"))) + fee, "f"),
        "slippage_usd": format(Decimal(str(current.get("slippage_usd", "0"))) + slip, "f"),
        "gas_usd": format(Decimal(str(current.get("gas_usd", "0"))) + gas, "f"),
        "legs": str(int(current.get("legs", "0")) + 1),
    }


def _dump(state: BookState, generation: int) -> str:
    return json.dumps(
        {"generation": generation, "state": json.loads(state.model_dump_json())},
        indent=2,
        sort_keys=True,
    )
