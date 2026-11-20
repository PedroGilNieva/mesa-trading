"""Paso 0 del carry: historial de funding de los perps y su persistencia.

Fuente: Hyperliquid `POST /info` con `{"type": "fundingHistory"}`, el mismo host y
ruta que ya usa la mesa (lista blanca). Hyperliquid paga funding cada hora, así
que la tasa anualizada es la tasa horaria × 8760.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from mesa_trading.util import as_utc, iso

HORAS_ANIO = Decimal("8760")
HORA_MS = 3_600_000
# Páginas de `fundingHistory` (hasta 500 h cada una) por activo y por tick. El
# relleno inicial de 120 días (~6 páginas) se reparte en varios ticks.
PAGINAS_POR_TICK = 2
# Hyperliquid publica la hora con un poco de retraso: al día = última fila hace < 2 h.
HORAS_AL_DIA = 2

HISTORIAL_KEY = "sombra/funding/historial.json"


def ms(when: datetime) -> int:
    return int(as_utc(when).timestamp() * 1000)


def actualizar_historial(
    store: dict | None,
    fuente,
    activos: list[str],
    now: datetime,
    dias: int,
    paginas: int = PAGINAS_POR_TICK,
    agotado: Callable[[], bool] | None = None,
) -> dict:
    """Agrega las horas nuevas de cada activo. Recorta a `dias` hacia atrás.

    Pide solo si ya debería existir una hora nueva (≥ 60 min desde la última fila).
    Pide a lo más `paginas` por activo; lo que falte sigue en el próximo tick
    desde la última fila guardada (el avance es la serie misma). `agotado()` se
    revisa antes de cada llamada: si es True, corta y anota `deadline`. Un error
    en un activo no borra lo guardado; queda anotado en `errores`.
    """
    store = dict(store or {})
    series: dict[str, list[list]] = {k: list(v) for k, v in (store.get("series") or {}).items()}
    errores: dict[str, str] = {}
    now_ms = ms(now)
    piso = now_ms - dias * 24 * HORA_MS
    for activo in activos:
        filas = series.get(activo) or []
        ultimo = int(filas[-1][0]) if filas else None
        if ultimo is not None and now_ms - ultimo < HORA_MS:
            continue
        # Si lo guardado quedó antes del piso (mesa apagada mucho tiempo), arranca en el piso.
        inicio = piso if ultimo is None or ultimo < piso else ultimo + 1
        try:
            for _ in range(max(0, paginas)):
                if agotado is not None and agotado():
                    errores[activo] = "deadline"
                    break
                nuevas = fuente.funding_history(activo, inicio, now_ms)
                nuevas = [(t, r) for t, r in nuevas if t >= inicio]
                if not nuevas:
                    break
                filas.extend([int(t), format(Decimal(str(r)), "f")] for t, r in nuevas)
                inicio = int(nuevas[-1][0]) + 1
                # Ya llegó a la última hora pagada: no hace falta otra página.
                if inicio > now_ms - HORA_MS:
                    break
        except Exception as exc:  # noqa: BLE001
            errores[activo] = exc.__class__.__name__
        # Sin duplicados, en orden, y solo la ventana pedida.
        unicas = {int(t): r for t, r in filas if int(t) >= piso}
        series[activo] = [[t, unicas[t]] for t in sorted(unicas)]
    store["series"] = series
    store["actualizado_en"] = iso(now)
    store["fuente"] = "hyperliquid POST /info fundingHistory (cada hora)"
    store["errores"] = errores
    store["al_dia"] = {activo: al_dia(store, activo, now) for activo in activos}
    return store


def al_dia(store: dict | None, activo: str, now: datetime) -> bool:
    """True si la serie llega hasta las últimas `HORAS_AL_DIA` horas (relleno terminado)."""
    filas = (store or {}).get("series", {}).get(activo) or []
    if not filas:
        return False
    return ms(now) - int(filas[-1][0]) < HORAS_AL_DIA * HORA_MS


def tasas(store: dict | None, activo: str, desde_ms: int | None = None, hasta_ms: int | None = None) -> list[tuple[int, Decimal]]:
    out = []
    for t, r in (store or {}).get("series", {}).get(activo) or []:
        t = int(t)
        if desde_ms is not None and t < desde_ms:
            continue
        if hasta_ms is not None and t > hasta_ms:
            continue
        out.append((t, Decimal(str(r))))
    return out


def ultimas(store: dict | None, activo: str, horas: int, hasta_ms: int) -> list[Decimal]:
    filas = tasas(store, activo, hasta_ms=hasta_ms)
    return [r for _t, r in filas[-horas:]] if horas > 0 else []


def anualizar(tasa_horaria: Decimal) -> Decimal:
    return tasa_horaria * HORAS_ANIO


def media(valores: list[Decimal]) -> Decimal | None:
    if not valores:
        return None
    return sum(valores, Decimal("0")) / Decimal(len(valores))


def persistencia(store: dict | None, activo: str, now: datetime, dias: int) -> dict:
    """% de horas con funding > 0 y funding anualizado medio de los últimos `dias`."""
    now_ms = ms(now)
    filas = [r for _t, r in tasas(store, activo, desde_ms=now_ms - dias * 24 * HORA_MS, hasta_ms=now_ms)]
    esperadas = dias * 24
    if not filas:
        return {"dias": dias, "periodos": 0, "periodos_esperados": esperadas, "pct_positivo": None, "funding_anual_medio": None}
    positivos = sum(1 for r in filas if r > 0)
    return {
        "dias": dias,
        "periodos": len(filas),
        "periodos_esperados": esperadas,
        "pct_positivo": _fmt(Decimal(positivos) / Decimal(len(filas)), 4),
        "funding_anual_medio": _fmt(anualizar(media(filas) or Decimal("0")), 6),
    }


def resumen_persistencia(store: dict | None, activos: list[str], now: datetime) -> dict:
    return {
        "fuente": (store or {}).get("fuente"),
        "actualizado_en": (store or {}).get("actualizado_en"),
        "errores": (store or {}).get("errores") or {},
        "al_dia": (store or {}).get("al_dia") or {},
        "activos": {
            activo: {"30d": persistencia(store, activo, now, 30), "90d": persistencia(store, activo, now, 90)}
            for activo in activos
        },
    }


def _fmt(value: Decimal, places: int) -> str:
    return format(value.quantize(Decimal(10) ** -places), "f")


