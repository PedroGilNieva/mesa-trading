"""Tipos compartidos de la mesa. El dinero es Decimal, nunca float de negocio."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Order(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_order_id: str
    cycle_id: str
    bot: str
    market: str
    side: Literal["buy", "sell"]
    action: str
    order_type: Literal["market", "limit"]
    qty: Decimal
    notional_usd: Decimal
    limit_price: Decimal | None = None
    decision_time: datetime
    reason: str = ""
    leverage: Decimal = Decimal("1")
    kind: Literal["spot", "perp"] = "spot"
    reduce_only: bool = False


class Ack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_order_id: str
    accepted: bool
    detail: str = ""


class Fill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_order_id: str
    market: str
    side: Literal["buy", "sell"]
    qty: Decimal
    price: Decimal
    fee_usd: Decimal
    slippage_usd: Decimal
    gas_usd: Decimal
    filled_at: datetime
    price_source: str
    reason: str = ""


class RestingOrder(BaseModel):
    """Límite que no cruzó en el tick en que se mandó. El siguiente tick la resuelve."""

    model_config = ConfigDict(extra="forbid")

    client_order_id: str
    cycle_id: str
    bot: str
    market: str
    side: Literal["buy", "sell"]
    action: str
    qty: Decimal
    notional_usd: Decimal
    limit_price: Decimal
    decision_time: datetime
    expires_at: datetime
    reason: str = ""
    kind: Literal["spot", "perp"] = "spot"
    threshold_version: int = 0
    cuenta_como_evidencia: bool = False


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: str
    bot: str
    side: Literal["long", "short"]
    qty: Decimal
    entry_price: Decimal
    notional_usd: Decimal
    opened_at: datetime
    fees_usd: Decimal = Decimal("0")
    # Solo informativo: ya está dentro de entry_price. No se resta otra vez.
    slippage_usd: Decimal = Decimal("0")
    gas_usd: Decimal = Decimal("0")
    kind: Literal["spot", "perp"] = "spot"
    threshold_version: int = 0
    cuenta_como_evidencia: bool = False


class BookState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cash_usd: Decimal
    day_start_equity_usd: Decimal
    day_start_cdmx: str
    realized_pnl_usd: Decimal = Decimal("0")
    day_realized_pnl_usd: Decimal = Decimal("0")
    fees_usd: Decimal = Decimal("0")
    # Deslizamiento acumulado. Va dentro del precio de los fills; es desglose, no un cargo aparte.
    slippage_usd: Decimal = Decimal("0")
    # Gas cobrado por pata en fills DEX (#29). Los libros viejos no lo traen.
    gas_usd: Decimal = Decimal("0")
    # Deslizamiento que el realizado viejo restó dos veces (antes de #29). Se congela
    # en la primera escritura con la contabilidad nueva; None = libro que aún no pasó por ella.
    slippage_double_counted_legacy_usd: Decimal | None = None
    # Comisión, deslizamiento y gas por mercado, desde #29: mercado -> montos.
    costs_by_market: dict[str, dict[str, str]] = Field(default_factory=dict)
    peak_equity_usd: Decimal
    position: Position | None = None
    resting_order: RestingOrder | None = None
    halted_until: datetime | None = None
    halt_rule: str | None = None
    last_prices: dict[str, str] = Field(default_factory=dict)
    bank: dict[str, str] = Field(default_factory=dict)
    seen_orders: list[str] = Field(default_factory=list)
    equity_hwm_usd: Decimal | None = None
    # Resultados por resolver, sin releer toda la bitácora en cada ciclo.
    pending_outcomes: list[dict[str, Any]] = Field(default_factory=list)
    last_cycle_id: str | None = None
    last_cycle_at: str | None = None
    closed_trades: int = 0
    # Cierres por motivo (p. ej. cierre_sin_datos). Los libros viejos no lo traen;
    # la diferencia con closed_trades se reporta como sin_motivo_registrado.
    closed_by_reason: dict[str, int] = Field(default_factory=dict)
    started_at: str | None = None
    max_drawdown_usd: Decimal = Decimal("0")
    # Los últimos fills, para /estado, sin releer la bitácora.
    recent_fills: list[dict[str, Any]] = Field(default_factory=list)
    # cloid ya mandado -> client_order_id. Sobrevive un reintento del mismo intento.
    sent_orders: dict[str, str] = Field(default_factory=dict)
    kill_latched: bool = False
    flatten_nonce: int = 0
    inflight_close: str | None = None

    @model_validator(mode="before")
    @classmethod
    def read_legacy_day(cls, data):
        """El libro viejo guardaba el día UTC en day_start_utc."""
        if not isinstance(data, dict) or "day_start_utc" not in data:
            return data
        data = dict(data)
        data.setdefault("day_start_cdmx", data.pop("day_start_utc"))
        data.pop("day_start_utc", None)
        return data


class Proposal(BaseModel):
    """Acción que sale de la política. Sin probabilidades: el riesgo no las lee."""

    model_config = ConfigDict(extra="forbid")

    bot: str
    market: str
    action: Literal["long", "short", "hold", "quote", "pause", "mirror", "skip", "close"]
    notional_usd: Decimal = Decimal("0")
    leverage: Decimal = Decimal("1")
    kind: Literal["spot", "perp"] = "spot"
    threshold_version: int
    thresholds: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    is_entry: bool = False
    condiciones: list[str] = Field(default_factory=list)
    observados: dict[str, str] = Field(default_factory=dict)


class Veto(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule: str
    detail: str
    halt: bool = False
    close_position: bool = False
