"""Libro de papel común a las estrategias de sombra. No hay órdenes ni broker.

Contabilidad (igual en carry y tendencia):

- bruto: movimiento de precio a precios medios (sin deslizamiento).
- costos: comisión + deslizamiento + gas de cada pata. El deslizamiento se cobra
  una sola vez, aquí como costo y no dentro del precio.
- funding: cobrado (+) o pagado (-) por la pata perp.
- neto = bruto + funding - costos.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from mesa_trading.broker.fees import FeeSchedule, commission_usd, slippage_usd
from mesa_trading.util import as_utc

CERO = Decimal("0")
MAX_CIERRES_GUARDADOS = 200


class Costos(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comisiones_usd: Decimal = CERO
    deslizamiento_usd: Decimal = CERO
    gas_usd: Decimal = CERO

    @property
    def total(self) -> Decimal:
        return self.comisiones_usd + self.deslizamiento_usd + self.gas_usd

    def sumar(self, otro: "Costos") -> "Costos":
        return Costos(
            comisiones_usd=self.comisiones_usd + otro.comisiones_usd,
            deslizamiento_usd=self.deslizamiento_usd + otro.deslizamiento_usd,
            gas_usd=self.gas_usd + otro.gas_usd,
        )


class Totales(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bruto_usd: Decimal = CERO
    funding_usd: Decimal = CERO
    costos: Costos = Field(default_factory=Costos)

    @property
    def neto(self) -> Decimal:
        return self.bruto_usd + self.funding_usd - self.costos.total

    def sumar(self, bruto: Decimal, funding: Decimal, costos: Costos) -> None:
        self.bruto_usd += bruto
        self.funding_usd += funding
        self.costos = self.costos.sumar(costos)


class Drawdown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pico_equity_usd: Decimal
    max_drawdown_usd: Decimal = CERO
    max_drawdown_pct: Decimal = CERO

    def anotar(self, equity: Decimal) -> None:
        """Peor pico-a-valle, no el hueco de ahora."""
        if equity > self.pico_equity_usd:
            self.pico_equity_usd = equity
        hueco = self.pico_equity_usd - equity
        if hueco > self.max_drawdown_usd:
            self.max_drawdown_usd = hueco
            if self.pico_equity_usd > 0:
                self.max_drawdown_pct = hueco / self.pico_equity_usd


def costo_pata(
    nocional: Decimal,
    schedule: FeeSchedule,
    spread_bps: Decimal,
    liquidez_usd: Decimal,
    extra_bps: Decimal,
) -> Costos:
    """Comisión, deslizamiento (medio spread + impacto + extra) y gas de una pata."""
    return Costos(
        comisiones_usd=commission_usd(nocional, schedule),
        deslizamiento_usd=slippage_usd(nocional, spread_bps, liquidez_usd, extra_bps),
        gas_usd=schedule.gas_usd,
    )


def spread_bps(bid: Decimal, ask: Decimal) -> Decimal:
    mid = (bid + ask) / 2
    if mid <= 0:
        return Decimal("10000")
    return (ask - bid) / mid * Decimal("10000")


def dias_entre(desde: str | datetime | None, now: datetime) -> Decimal:
    if desde is None:
        return CERO
    delta = as_utc(now) - as_utc(desde)
    return Decimal(str(delta.total_seconds())) / Decimal("86400")


def guardar_cierre(cierres: list[dict], cierre: dict) -> list[dict]:
    cierres = list(cierres) + [cierre]
    return cierres[-MAX_CIERRES_GUARDADOS:]


def s(value: Decimal | None, places: int = 6) -> str | None:
    if value is None:
        return None
    return format(value.quantize(Decimal(10) ** -places), "f")


def totales_dict(t: Totales) -> dict:
    return {
        "bruto_usd": s(t.bruto_usd),
        "funding_usd": s(t.funding_usd),
        "comisiones_usd": s(t.costos.comisiones_usd),
        "deslizamiento_usd": s(t.costos.deslizamiento_usd),
        "gas_usd": s(t.costos.gas_usd),
        "costos_usd": s(t.costos.total),
        "neto_usd": s(t.neto),
    }
