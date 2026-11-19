"""Funding carry en papel (#31): spot largo + perp corto del mismo nocional.

Reglas:

- Abre un activo solo si `funding anualizado esperado - costos de ida y vuelta
  anualizados > umbral_neto_anual`. El esperado es la media de las últimas
  `ventana_funding_horas` horas × 8760. Los costos (comisión, deslizamiento, gas
  de las dos patas, entrada y salida) se reparten en `horizonte_dias`.
- Cierra si el funding medio de las últimas `horas_negativo_cierre` horas es
  negativo, o si la base `(perp - spot) / spot` se aleja más de `limite_base` de
  la base de entrada.
- Funding: por cada hora nueva, la pata corta cobra `qty × spot × tasa` (paga si
  la tasa es negativa). El spot es el oráculo de Hyperliquid en el tick en que se
  anota la hora; es una aproximación al precio exacto de esa hora.
- Descarte: tras `descarte_dias` días, si el neto (realizado + abierto) es ≤ 0 o
  el funding fue positivo menos del `descarte_min_pct_positivo` de las horas, se
  marca descartada, se cierran las posiciones y no abre más.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from mesa_trading.broker.fees import FeeSchedule
from mesa_trading.config import CarryConfig
from mesa_trading.sombra import funding as fh
from mesa_trading.sombra.libro import (
    CERO,
    Costos,
    Drawdown,
    Totales,
    costo_pata,
    dias_entre,
    guardar_cierre,
    s,
    spread_bps,
    totales_dict,
)
from mesa_trading.util import as_utc, iso


class PosicionCarry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activo: str
    abierta_en: str
    qty: Decimal
    nocional_usd: Decimal
    spot_entrada: Decimal
    perp_entrada: Decimal
    base_entrada: Decimal
    ultimo_funding_ms: int
    funding_usd: Decimal = CERO
    horas_funding: int = 0
    horas_positivas: int = 0
    costos: Costos = Field(default_factory=Costos)
    funding_anual_esperado: Decimal = CERO


class EstadoCarry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    estrategia: str = "carry"
    capital_usd: Decimal
    iniciado_en: str
    actualizado_en: str | None = None
    posiciones: dict[str, PosicionCarry] = Field(default_factory=dict)
    realizado: Totales = Field(default_factory=Totales)
    operaciones_cerradas: int = 0
    cierres: list[dict] = Field(default_factory=list)
    drawdown: Drawdown
    descartada: bool = False
    motivo_descarte: str | None = None
    vetos: dict[str, int] = Field(default_factory=dict)
    ultima_evaluacion: dict[str, dict] = Field(default_factory=dict)
    ultimos_precios: dict[str, dict] = Field(default_factory=dict)


def nuevo(cfg: CarryConfig, now: datetime) -> EstadoCarry:
    return EstadoCarry(
        capital_usd=cfg.capital_usd,
        iniciado_en=iso(now),
        drawdown=Drawdown(pico_equity_usd=cfg.capital_usd),
    )


def base(spot: Decimal, perp: Decimal) -> Decimal:
    return (perp - spot) / spot


def bruto_abierto(pos: PosicionCarry, spot: Decimal, perp: Decimal) -> Decimal:
    """PnL de base: spot largo + perp corto, a precios medios."""
    return pos.qty * (spot - pos.spot_entrada) + pos.qty * (pos.perp_entrada - perp)


def costos_ida_vuelta(
    nocional: Decimal, cfg: CarryConfig, sched_spot: FeeSchedule, sched_perp: FeeSchedule, perp_spread_bps: Decimal
) -> Costos:
    spot = costo_pata(nocional, sched_spot, cfg.spread_spot_bps, cfg.liquidez_supuesta_usd, cfg.extra_bps)
    perp = costo_pata(nocional, sched_perp, perp_spread_bps, cfg.liquidez_supuesta_usd, cfg.extra_bps)
    una = spot.sumar(perp)
    return una.sumar(una)


def costos_una_ida(
    nocional: Decimal, cfg: CarryConfig, sched_spot: FeeSchedule, sched_perp: FeeSchedule, perp_spread_bps: Decimal
) -> Costos:
    spot = costo_pata(nocional, sched_spot, cfg.spread_spot_bps, cfg.liquidez_supuesta_usd, cfg.extra_bps)
    perp = costo_pata(nocional, sched_perp, perp_spread_bps, cfg.liquidez_supuesta_usd, cfg.extra_bps)
    return spot.sumar(perp)


def evaluar_entrada(
    historial: dict | None,
    activo: str,
    nocional: Decimal,
    cfg: CarryConfig,
    sched_spot: FeeSchedule,
    sched_perp: FeeSchedule,
    perp_spread_bps: Decimal,
    now: datetime,
) -> dict:
    """Funding anualizado esperado, costos anualizados y si pasa el umbral."""
    ultimas = fh.ultimas(historial, activo, cfg.ventana_funding_horas, fh.ms(now))
    minimo = max(1, cfg.ventana_funding_horas // 2)
    if len(ultimas) < minimo or nocional <= 0:
        return {"abre": False, "motivo": "sin_historial", "horas": len(ultimas)}
    # No reabre mientras siga vigente la condición de salida (evita cerrar y reabrir en el mismo tick).
    recientes = fh.ultimas(historial, activo, cfg.horas_negativo_cierre, fh.ms(now))
    if recientes and (fh.media(recientes) or CERO) < 0:
        return {"abre": False, "motivo": "funding_negativo_reciente", "horas": len(ultimas)}
    esperado = fh.anualizar(fh.media(ultimas) or CERO)
    costos = costos_ida_vuelta(nocional, cfg, sched_spot, sched_perp, perp_spread_bps)
    costo_anual = costos.total / nocional * Decimal("365") / Decimal(cfg.horizonte_dias)
    neto = esperado - costo_anual
    abre = neto > cfg.umbral_neto_anual
    return {
        "abre": abre,
        "motivo": "pasa_umbral" if abre else "bajo_umbral",
        "horas": len(ultimas),
        "funding_anual_esperado": s(esperado),
        "costo_ida_vuelta_usd": s(costos.total),
        "costo_anual": s(costo_anual),
        "neto_anual": s(neto),
        "umbral": s(cfg.umbral_neto_anual),
    }


def debe_cerrar(
    historial: dict | None, pos: PosicionCarry, spot: Decimal, perp: Decimal, cfg: CarryConfig, now: datetime
) -> str | None:
    ultimas = fh.ultimas(historial, pos.activo, cfg.horas_negativo_cierre, fh.ms(now))
    if len(ultimas) >= cfg.horas_negativo_cierre:
        promedio = fh.media(ultimas)
        if promedio is not None and promedio < 0:
            return "funding_negativo_sostenido"
    if abs(base(spot, perp) - pos.base_entrada) > cfg.limite_base:
        return "base_desviada"
    return None


def cobrar_funding(historial: dict | None, pos: PosicionCarry, spot: Decimal, now: datetime) -> Decimal:
    """Anota las horas nuevas. Corto en el perp: cobra si la tasa es positiva."""
    cobrado = CERO
    for t, tasa in fh.tasas(historial, pos.activo, desde_ms=pos.ultimo_funding_ms + 1, hasta_ms=fh.ms(now)):
        pago = pos.qty * spot * tasa
        cobrado += pago
        pos.horas_funding += 1
        if tasa > 0:
            pos.horas_positivas += 1
        pos.ultimo_funding_ms = t
    pos.funding_usd += cobrado
    return cobrado


def abrir(
    estado: EstadoCarry,
    activo: str,
    nocional: Decimal,
    spot: Decimal,
    perp_bid: Decimal,
    perp_ask: Decimal,
    cfg: CarryConfig,
    sched_spot: FeeSchedule,
    sched_perp: FeeSchedule,
    esperado: Decimal,
    now: datetime,
) -> PosicionCarry:
    perp = (perp_bid + perp_ask) / 2
    qty = nocional / spot
    costos = costos_una_ida(nocional, cfg, sched_spot, sched_perp, spread_bps(perp_bid, perp_ask))
    pos = PosicionCarry(
        activo=activo,
        abierta_en=iso(now),
        qty=qty,
        nocional_usd=nocional,
        spot_entrada=spot,
        perp_entrada=perp,
        base_entrada=base(spot, perp),
        # Solo cobra las horas que empiecen después de abrir.
        ultimo_funding_ms=fh.ms(now),
        costos=costos,
        funding_anual_esperado=esperado,
    )
    estado.posiciones[activo] = pos
    return pos


def cerrar(
    estado: EstadoCarry,
    activo: str,
    spot: Decimal,
    perp_bid: Decimal,
    perp_ask: Decimal,
    cfg: CarryConfig,
    sched_spot: FeeSchedule,
    sched_perp: FeeSchedule,
    motivo: str,
    now: datetime,
) -> dict:
    pos = estado.posiciones.pop(activo)
    perp = (perp_bid + perp_ask) / 2
    salida = costos_una_ida(pos.qty * spot, cfg, sched_spot, sched_perp, spread_bps(perp_bid, perp_ask))
    costos = pos.costos.sumar(salida)
    bruto = bruto_abierto(pos, spot, perp)
    estado.realizado.sumar(bruto, pos.funding_usd, costos)
    estado.operaciones_cerradas += 1
    neto = bruto + pos.funding_usd - costos.total
    cierre = {
        "activo": activo,
        "abierta_en": pos.abierta_en,
        "cerrada_en": iso(now),
        "motivo": motivo,
        "nocional_usd": s(pos.nocional_usd),
        "bruto_base_usd": s(bruto),
        "funding_usd": s(pos.funding_usd),
        "comisiones_usd": s(costos.comisiones_usd),
        "deslizamiento_usd": s(costos.deslizamiento_usd),
        "gas_usd": s(costos.gas_usd),
        "costos_usd": s(costos.total),
        "neto_usd": s(neto),
        "horas_funding": pos.horas_funding,
        "horas_positivas": pos.horas_positivas,
    }
    estado.cierres = guardar_cierre(estado.cierres, cierre)
    return cierre


def abierto(estado: EstadoCarry, precios: dict[str, dict]) -> Totales:
    """Bruto, funding y costos ya pagados de las posiciones abiertas, a mercado."""
    tot = Totales()
    for activo, pos in estado.posiciones.items():
        px = precios.get(activo)
        if px:
            bruto = bruto_abierto(pos, px["spot"], (px["bid"] + px["ask"]) / 2)
        else:
            bruto = CERO
        tot.sumar(bruto, pos.funding_usd, pos.costos)
    return tot


def equity(estado: EstadoCarry, precios: dict[str, dict]) -> Decimal:
    return estado.capital_usd + estado.realizado.neto + abierto(estado, precios).neto


def regla_descarte(estado: EstadoCarry, historial: dict | None, cfg: CarryConfig, precios: dict[str, dict], now: datetime) -> dict:
    """Progreso y veredicto de la regla de descarte del carry."""
    dias = dias_entre(estado.iniciado_en, now)
    positivas, total = fh.pct_positivo_desde(historial, cfg.activos, as_utc(estado.iniciado_en), now)
    pct = (Decimal(positivas) / Decimal(total)) if total else None
    neto = equity(estado, precios) - estado.capital_usd
    cumplido = dias >= cfg.descarte_dias
    motivos = []
    if neto <= 0:
        motivos.append("neto_no_positivo")
    if pct is None or pct < cfg.descarte_min_pct_positivo:
        motivos.append("funding_positivo_bajo_minimo")
    if estado.descartada:
        estado_regla = "descartada"
    elif not cumplido:
        estado_regla = "en_prueba"
    elif motivos:
        estado_regla = "descartada"
    else:
        estado_regla = "pasa"
    return {
        "estado": estado_regla,
        "regla": (
            f"tras {cfg.descarte_dias} días: descartada si neto ≤ 0 o funding positivo "
            f"< {s(cfg.descarte_min_pct_positivo * 100, 0)}% de las horas"
        ),
        "dias": s(dias, 2),
        "dias_requeridos": cfg.descarte_dias,
        "progreso_pct": s(min(Decimal("1"), dias / Decimal(cfg.descarte_dias)) * 100, 1),
        "neto_usd": s(neto),
        "funding_horas_positivas": positivas,
        "funding_horas_total": total,
        "funding_pct_positivo": s(pct, 4) if pct is not None else None,
        "min_pct_positivo": s(cfg.descarte_min_pct_positivo, 4),
        "motivos_si_hoy": motivos,
        "motivo_descarte": estado.motivo_descarte,
    }


def paso(
    estado: EstadoCarry,
    historial: dict | None,
    precios: dict[str, dict],
    cfg: CarryConfig,
    fees: dict[str, FeeSchedule],
    max_position_pct: Decimal,
    now: datetime,
) -> EstadoCarry:
    """Un tick del carry. `precios[activo] = {"spot", "bid", "ask"}` en Decimal."""
    sched_perp = fees[cfg.fee_perp]
    for activo, px in precios.items():
        estado.ultimos_precios[activo] = {k: format(v, "f") for k, v in px.items()}

    # 1) Funding de las horas nuevas y salidas.
    for activo in list(estado.posiciones):
        px = precios.get(activo)
        if not px:
            continue
        pos = estado.posiciones[activo]
        cobrar_funding(historial, pos, px["spot"], now)
        perp_mid = (px["bid"] + px["ask"]) / 2
        motivo = debe_cerrar(historial, pos, px["spot"], perp_mid, cfg, now)
        if motivo:
            cerrar(estado, activo, px["spot"], px["bid"], px["ask"], cfg, fees[cfg.fee_spot[activo]], sched_perp, motivo, now)

    # 2) Regla de descarte. Si descarta, cierra todo y no abre más.
    regla = regla_descarte(estado, historial, cfg, precios, now)
    if regla["estado"] == "descartada" and not estado.descartada:
        estado.descartada = True
        estado.motivo_descarte = ",".join(regla["motivos_si_hoy"]) or "regla"
        for activo in list(estado.posiciones):
            px = precios.get(activo)
            if px:
                cerrar(estado, activo, px["spot"], px["bid"], px["ask"], cfg, fees[cfg.fee_spot[activo]], sched_perp, "descarte", now)

    # 3) Entradas.
    if not estado.descartada:
        for activo in cfg.activos:
            px = precios.get(activo)
            if activo in estado.posiciones or not px:
                continue
            eq = equity(estado, precios)
            pct = min(cfg.nocional_pct, max_position_pct)
            nocional = eq * pct
            sched_spot = fees[cfg.fee_spot[activo]]
            ev = evaluar_entrada(historial, activo, nocional, cfg, sched_spot, sched_perp, spread_bps(px["bid"], px["ask"]), now)
            ev["evaluado_en"] = iso(now)
            estado.ultima_evaluacion[activo] = ev
            if not ev["abre"]:
                estado.vetos[ev["motivo"]] = int(estado.vetos.get(ev["motivo"], 0)) + 1
                continue
            abrir(
                estado, activo, nocional, px["spot"], px["bid"], px["ask"], cfg, sched_spot, sched_perp,
                Decimal(ev["funding_anual_esperado"]), now,
            )

    estado.drawdown.anotar(equity(estado, precios))
    estado.actualizado_en = iso(now)
    return estado


def resumen(estado: EstadoCarry, historial: dict | None, cfg: CarryConfig, now: datetime) -> dict:
    precios = _precios_guardados(estado)
    ab = abierto(estado, precios)
    total = Totales(
        bruto_usd=estado.realizado.bruto_usd + ab.bruto_usd,
        funding_usd=estado.realizado.funding_usd + ab.funding_usd,
        costos=estado.realizado.costos.sumar(ab.costos),
    )
    eq = equity(estado, precios)
    return {
        "estrategia": "carry",
        "capital_teorico_usd": s(estado.capital_usd, 2),
        "iniciado_en": estado.iniciado_en,
        "actualizado_en": estado.actualizado_en,
        "equity_usd": s(eq),
        "realizado": totales_dict(estado.realizado),
        "abierto": totales_dict(ab),
        "total": totales_dict(total),
        "operaciones_cerradas": estado.operaciones_cerradas,
        "posiciones_abiertas": {
            a: {
                "abierta_en": p.abierta_en,
                "nocional_usd": s(p.nocional_usd, 2),
                "base_entrada": s(p.base_entrada, 6),
                "funding_usd": s(p.funding_usd),
                "horas_funding": p.horas_funding,
                "funding_anual_esperado": s(p.funding_anual_esperado),
            }
            for a, p in estado.posiciones.items()
        },
        "max_drawdown_usd": s(estado.drawdown.max_drawdown_usd),
        "max_drawdown_pct": s(estado.drawdown.max_drawdown_pct, 4),
        "descarte": regla_descarte(estado, historial, cfg, precios, now),
        "vetos": dict(estado.vetos),
        "ultima_evaluacion": estado.ultima_evaluacion,
        "ultimos_cierres": estado.cierres[-5:],
    }


def _precios_guardados(estado: EstadoCarry) -> dict[str, dict]:
    return {a: {k: Decimal(v) for k, v in px.items()} for a, px in estado.ultimos_precios.items()}
