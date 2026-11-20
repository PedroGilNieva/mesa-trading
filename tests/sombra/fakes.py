"""Fuente de datos falsa para sombra. No sale a la red."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from mesa_trading.sombra.funding import HORA_MS, ms

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)


def horas(desde: datetime, n: int, tasa: str) -> list[tuple[int, Decimal]]:
    base = ms(desde)
    return [(base + i * HORA_MS, Decimal(tasa)) for i in range(n)]


def historial(series: dict[str, list[tuple[int, Decimal]]]) -> dict:
    return {"series": {a: [[t, format(r, "f")] for t, r in filas] for a, filas in series.items()}}


def velas(inicio: datetime, paso: timedelta, cierres: list[str], rango: str = "1") -> list[dict]:
    out = []
    r = Decimal(rango)
    for i, c in enumerate(cierres):
        px = Decimal(c)
        ts = inicio + i * paso
        out.append({"ts": ts, "close_ts": ts + paso, "open": px, "high": px + r, "low": px - r, "close": px, "volume": Decimal("1")})
    return out


class FuenteFalsa:
    def __init__(self, funding=None, ctx=None, books=None, velas4h=None, velas1d=None, pagina=500):
        self.funding = funding or {}
        self.ctx = ctx or {}
        self.books = books or {}
        self.v4 = velas4h or {}
        self.v1 = velas1d or {}
        self.pagina = pagina
        self.llamadas: list[tuple] = []

    def funding_history(self, coin, start_ms, end_ms):
        self.llamadas.append(("funding", coin, start_ms))
        filas = [(t, r) for t, r in self.funding.get(coin, []) if start_ms <= t <= end_ms]
        return filas[: self.pagina]

    def asset_context(self, coin):
        return self.ctx.get(coin)

    def order_book(self, coin, depth=5):
        return self.books.get(coin)

    def candles(self, coin, interval, since=None):
        self.llamadas.append(("velas", coin, interval))
        rows = (self.v4 if interval == "4h" else self.v1).get(coin, [])
        return [r for r in rows if since is None or r["ts"] >= since]
