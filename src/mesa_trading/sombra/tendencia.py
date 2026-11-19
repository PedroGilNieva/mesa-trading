"""Tendencia diaria/4h en papel (#31). Perps de Hyperliquid, largo o corto.

Señal (estándar, sin ajustar a los datos; cada parámetro está en config/desk.yaml):

- Régimen (1D): último cierre diario contra la SMA de `sma_1d` cierres diarios.
  Arriba = alcista (solo largos); abajo = bajista (solo cortos).
- Entrada (4h): el cierre de la última vela de 4h cerrada rompe el máximo (largo)
  o el mínimo (corto) de las `donchian_entrada_4h` velas anteriores, a favor del régimen.
- Stop inicial: `atr_mult_stop` × ATR(`atr_1d`) diario desde el precio de entrada.
  Se revisa en cada tick con el precio medio del perp.
- Salida (4h): el cierre rompe el mínimo (largo) o el máximo (corto) de las
  `donchian_salida_4h` velas anteriores, o el régimen deja de acompañar.

Tamaño por riesgo: nocional = `riesgo_pct` × equity / (distancia al stop / precio),
con tope `risk.max_position_pct` del equity por posición y `risk.max_exposure_pct`
en total (los mismos topes de la mesa, 25%).

Veto de economía: como en la mesa, la ventaja supuesta (`expected_edge_bps`) tiene
que superar comisión + deslizamiento + gas; aquí de ida y vuelta (dos patas).

Descarte: tras `descarte_operaciones` cierres o `descarte_dias` días (lo primero
que llegue), descartada si la esperanza neta por operación es ≤ 0 (o si no hubo
ninguna operación).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from mesa_trading.broker.fees import FeeSchedule
from mesa_trading.config import TendenciaConfig
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

CUATRO_HORAS = timedelta(hours=4)


class PosicionTendencia(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activo: str
    lado: Literal["long", "short"]
    abierta_en: str
    qty: Decimal
    nocional_usd: Decimal
    entrada: Decimal
    stop: Decimal
    riesgo_usd: Decimal
    ultimo_funding_ms: int
    funding_usd: Decimal = CERO
    costos: Costos = Field(default_factory=Costos)


class EstadoTendencia(BaseModel):
    model_config = ConfigDict(extra="forbid")

    estrategia: str = "tendencia"
    capital_usd: Decimal
    iniciado_en: str
    actualizado_en: str | None = None
    posiciones: dict[str, PosicionTendencia] = Field(default_factory=dict)
    realizado: Totales = Field(default_factory=Totales)
    operaciones_cerradas: int = 0
    ganadoras: int = 0
    cierres: list[dict] = Field(default_factory=list)
    drawdown: Drawdown
    descartada: bool = False
    motivo_descarte: str | None = None
    vetos: dict[str, int] = Field(default_factory=dict)
    ultima_vela_4h: dict[str, str] = Field(default_factory=dict)
    ultima_senal: dict[str, dict] = Field(default_factory=dict)
    ultimos_precios: dict[str, str] = Field(default_factory=dict)


def nuevo(cfg: TendenciaConfig, now: datetime) -> EstadoTendencia:
    return EstadoTendencia(
        capital_usd=cfg.capital_usd,
        iniciado_en=iso(now),
        drawdown=Drawdown(pico_equity_usd=cfg.capital_usd),
    )


# --- señal -----------------------------------------------------------------


def cerradas(velas: list[dict], now: datetime) -> list[dict]:
    return [v for v in velas if as_utc(v["close_ts"]) <= as_utc(now)]


def sma(valores: list[Decimal], n: int) -> Decimal | None:
    if n <= 0 or len(valores) < n:
        return None
    return sum(valores[-n:], CERO) / Decimal(n)


def atr(velas: list[dict], n: int) -> Decimal | None:
    """Media simple del rango verdadero de las últimas `n` velas."""
    if n <= 0 or len(velas) < n + 1:
        return None
    rangos = []
    for prev, cur in zip(velas[-n - 1 : -1], velas[-n:]):
        alto, bajo, previo = Decimal(cur["high"]), Decimal(cur["low"]), Decimal(prev["close"])
        rangos.append(max(alto - bajo, abs(alto - previo), abs(bajo - previo)))
    return sum(rangos, CERO) / Decimal(n)


def senal(velas_4h: list[dict], velas_1d: list[dict], cfg: TendenciaConfig) -> dict:
    """Solo velas cerradas. Devuelve régimen, entrada y salidas de la última vela de 4h."""
    necesita_4h = max(cfg.donchian_entrada_4h, cfg.donchian_salida_4h) + 1
    if len(velas_4h) < necesita_4h or len(velas_1d) < max(cfg.sma_1d, cfg.atr_1d + 1):
        return {"lista": False, "motivo": "pocas_velas", "velas_4h": len(velas_4h), "velas_1d": len(velas_1d)}
    cierres_1d = [Decimal(v["close"]) for v in velas_1d]
    media = sma(cierres_1d, cfg.sma_1d)
    ultimo_1d = cierres_1d[-1]
    regimen = "alcista" if ultimo_1d > media else "bajista" if ultimo_1d < media else "neutral"
    ultima = velas_4h[-1]
    cierre = Decimal(ultima["close"])
    previas_e = velas_4h[-1 - cfg.donchian_entrada_4h : -1]
    previas_s = velas_4h[-1 - cfg.donchian_salida_4h : -1]
    techo = max(Decimal(v["high"]) for v in previas_e)
    piso = min(Decimal(v["low"]) for v in previas_e)
    techo_s = max(Decimal(v["high"]) for v in previas_s)
    piso_s = min(Decimal(v["low"]) for v in previas_s)
    entrada = None
    if regimen == "alcista" and cierre > techo:
        entrada = "long"
    elif regimen == "bajista" and cierre < piso:
        entrada = "short"
    return {
        "lista": True,
        "vela_4h": iso(as_utc(ultima["ts"])),
        "cierre_4h": cierre,
        "regimen": regimen,
        "sma_1d": media,
        "atr_1d": atr(velas_1d, cfg.atr_1d),
        "entrada": entrada,
        "salir_long": cierre < piso_s or regimen != "alcista",
        "salir_short": cierre > techo_s or regimen != "bajista",
    }


# --- tamaño y economía ------------------------------------------------------


def tamano_por_riesgo(
    equity: Decimal,
    precio: Decimal,
    distancia_stop: Decimal,
    riesgo_pct: Decimal,
    max_position_pct: Decimal,
    max_exposure_pct: Decimal,
    exposicion_abierta: Decimal = CERO,
) -> tuple[Decimal, str]:
    """nocional = riesgo / (distancia al stop / precio), con los topes de la mesa."""
    if equity <= 0 or precio <= 0 or distancia_stop <= 0:
        return CERO, "sin_tamano"
    riesgo = equity * riesgo_pct
    nocional = riesgo * precio / distancia_stop
    limite = "riesgo"
    tope = equity * max_position_pct
    if nocional > tope:
        nocional, limite = tope, "tope_posicion"
    libre = equity * max_exposure_pct - exposicion_abierta
    if nocional > libre:
        nocional, limite = max(libre, CERO), "tope_exposicion"
    return nocional, limite


def veto_economia(
    nocional: Decimal, schedule: FeeSchedule, spread: Decimal, liquidez: Decimal, extra_bps: Decimal, edge_bps: Decimal
) -> tuple[bool, Decimal, Costos]:
    """True si la ventaja supuesta cubre comisión + deslizamiento + gas de ida y vuelta."""
    una = costo_pata(nocional, schedule, spread, liquidez, extra_bps)
    ida_vuelta = una.sumar(una)
    edge = nocional * edge_bps / Decimal("10000")
    return edge > ida_vuelta.total, edge, ida_vuelta


# --- libro -----------------------------------------------------------------


def bruto_abierto(pos: PosicionTendencia, mid: Decimal) -> Decimal:
    if pos.lado == "long":
        return (mid - pos.entrada) * pos.qty
    return (pos.entrada - mid) * pos.qty


def cobrar_funding(historial: dict | None, pos: PosicionTendencia, mid: Decimal, now: datetime) -> Decimal:
    """Largo paga funding positivo; corto lo cobra."""
    signo = Decimal("-1") if pos.lado == "long" else Decimal("1")
    neto = CERO
    for t, tasa in fh.tasas(historial, pos.activo, desde_ms=pos.ultimo_funding_ms + 1, hasta_ms=fh.ms(now)):
        neto += signo * pos.qty * mid * tasa
        pos.ultimo_funding_ms = t
    pos.funding_usd += neto
    return neto


def abrir(
    estado: EstadoTendencia,
    activo: str,
    lado: str,
    nocional: Decimal,
    bid: Decimal,
    ask: Decimal,
    distancia_stop: Decimal,
    cfg: TendenciaConfig,
    schedule: FeeSchedule,
    now: datetime,
) -> PosicionTendencia:
    mid = (bid + ask) / 2
    stop = mid - distancia_stop if lado == "long" else mid + distancia_stop
    qty = nocional / mid
    pos = PosicionTendencia(
        activo=activo,
        lado=lado,  # type: ignore[arg-type]
        abierta_en=iso(now),
        qty=qty,
        nocional_usd=nocional,
        entrada=mid,
        stop=stop,
        riesgo_usd=qty * distancia_stop,
        ultimo_funding_ms=fh.ms(now),
        costos=costo_pata(nocional, schedule, spread_bps(bid, ask), cfg.liquidez_supuesta_usd, cfg.extra_bps),
    )
    estado.posiciones[activo] = pos
    return pos


def cerrar(
    estado: EstadoTendencia,
    activo: str,
    bid: Decimal,
    ask: Decimal,
    cfg: TendenciaConfig,
    schedule: FeeSchedule,
    motivo: str,
    now: datetime,
) -> dict:
    pos = estado.posiciones.pop(activo)
    mid = (bid + ask) / 2
    salida = costo_pata(pos.qty * mid, schedule, spread_bps(bid, ask), cfg.liquidez_supuesta_usd, cfg.extra_bps)
    costos = pos.costos.sumar(salida)
    bruto = bruto_abierto(pos, mid)
    neto = bruto + pos.funding_usd - costos.total
    estado.realizado.sumar(bruto, pos.funding_usd, costos)
    estado.operaciones_cerradas += 1
    if neto > 0:
        estado.ganadoras += 1
    cierre = {
        "activo": activo,
        "lado": pos.lado,
        "abierta_en": pos.abierta_en,
        "cerrada_en": iso(now),
        "motivo": motivo,
        "entrada": s(pos.entrada),
        "salida": s(mid),
        "stop": s(pos.stop),
        "nocional_usd": s(pos.nocional_usd),
        "riesgo_usd": s(pos.riesgo_usd),
        "bruto_usd": s(bruto),
        "funding_usd": s(pos.funding_usd),
        "comisiones_usd": s(costos.comisiones_usd),
        "deslizamiento_usd": s(costos.deslizamiento_usd),
        "gas_usd": s(costos.gas_usd),
        "costos_usd": s(costos.total),
        "neto_usd": s(neto),
        "r_multiplo": s(neto / pos.riesgo_usd, 3) if pos.riesgo_usd > 0 else None,
    }
    estado.cierres = guardar_cierre(estado.cierres, cierre)
    return cierre


def abierto(estado: EstadoTendencia, mids: dict[str, Decimal]) -> Totales:
    tot = Totales()
    for activo, pos in estado.posiciones.items():
        mid = mids.get(activo)
        tot.sumar(bruto_abierto(pos, mid) if mid is not None else CERO, pos.funding_usd, pos.costos)
    return tot


def equity(estado: EstadoTendencia, mids: dict[str, Decimal]) -> Decimal:
    return estado.capital_usd + estado.realizado.neto + abierto(estado, mids).neto


def esperanza(estado: EstadoTendencia) -> Decimal | None:
    if estado.operaciones_cerradas == 0:
        return None
    return estado.realizado.neto / Decimal(estado.operaciones_cerradas)


def regla_descarte(estado: EstadoTendencia, cfg: TendenciaConfig, now: datetime) -> dict:
    dias = dias_entre(estado.iniciado_en, now)
    ops = estado.operaciones_cerradas
    exp = esperanza(estado)
    cumplido = ops >= cfg.descarte_operaciones or dias >= cfg.descarte_dias
    motivos = []
    if exp is None:
        motivos.append("sin_operaciones")
    elif exp <= 0:
        motivos.append("esperanza_neta_no_positiva")
    if estado.descartada:
        estado_regla = "descartada"
    elif not cumplido:
        estado_regla = "en_prueba"
    elif motivos:
        estado_regla = "descartada"
    else:
        estado_regla = "pasa"
    progreso = max(Decimal(ops) / Decimal(cfg.descarte_operaciones), dias / Decimal(cfg.descarte_dias))
    return {
        "estado": estado_regla,
        "regla": (
            f"tras {cfg.descarte_operaciones} operaciones cerradas o {cfg.descarte_dias} días: "
            "descartada si la esperanza neta por operación es ≤ 0"
        ),
        "operaciones_cerradas": ops,
        "operaciones_requeridas": cfg.descarte_operaciones,
        "dias": s(dias, 2),
        "dias_requeridos": cfg.descarte_dias,
        "progreso_pct": s(min(Decimal("1"), progreso) * 100, 1),
        "esperanza_neta_por_operacion_usd": s(exp) if exp is not None else None,
        "motivos_si_hoy": motivos,
        "motivo_descarte": estado.motivo_descarte,
    }


def paso(
    estado: EstadoTendencia,
    datos: dict[str, dict],
    historial: dict | None,
    cfg: TendenciaConfig,
    fees: dict[str, FeeSchedule],
    max_position_pct: Decimal,
    max_exposure_pct: Decimal,
    now: datetime,
) -> EstadoTendencia:
    """Un tick. `datos[activo] = {"bid", "ask", "velas_4h"?, "velas_1d"?}`.

    Las velas solo vienen cuando ya cerró una vela de 4h nueva; el stop se revisa
    en cada tick con el precio medio.
    """
    schedule = fees[cfg.fee_schedule]
    mids = {a: (d["bid"] + d["ask"]) / 2 for a, d in datos.items()}
    for activo, mid in mids.items():
        estado.ultimos_precios[activo] = format(mid, "f")

    # 1) Funding y stops.
    for activo in list(estado.posiciones):
        d = datos.get(activo)
        if not d:
            continue
        pos = estado.posiciones[activo]
        cobrar_funding(historial, pos, mids[activo], now)
        tocado = mids[activo] <= pos.stop if pos.lado == "long" else mids[activo] >= pos.stop
        if tocado:
            cerrar(estado, activo, d["bid"], d["ask"], cfg, schedule, "stop", now)

    # 2) Señal en cada vela de 4h nueva.
    senales: dict[str, dict] = {}
    for activo, d in datos.items():
        v4 = cerradas(d.get("velas_4h") or [], now)
        v1 = cerradas(d.get("velas_1d") or [], now)
        if not v4:
            continue
        ultima = iso(as_utc(v4[-1]["ts"]))
        if estado.ultima_vela_4h.get(activo) == ultima:
            continue
        sig = senal(v4, v1, cfg)
        estado.ultima_vela_4h[activo] = ultima
        estado.ultima_senal[activo] = {k: (s(v) if isinstance(v, Decimal) else v) for k, v in sig.items()}
        estado.ultima_senal[activo]["evaluada_en"] = iso(now)
        senales[activo] = sig
        pos = estado.posiciones.get(activo)
        if pos is not None and sig.get("lista"):
            if (pos.lado == "long" and sig["salir_long"]) or (pos.lado == "short" and sig["salir_short"]):
                cerrar(estado, activo, d["bid"], d["ask"], cfg, schedule, "salida_senal", now)

    # 3) Regla de descarte. Si descarta, cierra y no abre más.
    regla = regla_descarte(estado, cfg, now)
    if regla["estado"] == "descartada" and not estado.descartada:
        estado.descartada = True
        estado.motivo_descarte = ",".join(regla["motivos_si_hoy"]) or "regla"
        for activo in list(estado.posiciones):
            d = datos.get(activo)
            if d:
                cerrar(estado, activo, d["bid"], d["ask"], cfg, schedule, "descarte", now)

    # 4) Entradas.
    if not estado.descartada:
        for activo, sig in senales.items():
            if activo in estado.posiciones or not sig.get("lista") or sig.get("entrada") is None:
                continue
            d = datos[activo]
            if sig.get("atr_1d") is None or sig["atr_1d"] <= 0:
                _veto(estado, "sin_atr")
                continue
            distancia = sig["atr_1d"] * cfg.atr_mult_stop
            eq = equity(estado, mids)
            expuesto = sum((p.nocional_usd for p in estado.posiciones.values()), CERO)
            nocional, limite = tamano_por_riesgo(
                eq, mids[activo], distancia, cfg.riesgo_pct, max_position_pct, max_exposure_pct, expuesto
            )
            if nocional <= 0:
                _veto(estado, "exposicion_total")
                continue
            ok, _edge, _costos = veto_economia(
                nocional, schedule, spread_bps(d["bid"], d["ask"]), cfg.liquidez_supuesta_usd, cfg.extra_bps,
                cfg.expected_edge_bps,
            )
            if not ok:
                _veto(estado, "economia")
                continue
            abrir(estado, activo, sig["entrada"], nocional, d["bid"], d["ask"], distancia, cfg, schedule, now)
            estado.ultima_senal[activo]["tamano_limitado_por"] = limite

    estado.drawdown.anotar(equity(estado, mids))
    estado.actualizado_en = iso(now)
    return estado


def necesita_velas(estado: EstadoTendencia, activo: str, now: datetime) -> bool:
    """True si ya debió cerrar una vela de 4h posterior a la última evaluada."""
    ultima = estado.ultima_vela_4h.get(activo)
    if ultima is None:
        return True
    return as_utc(now) >= as_utc(ultima) + 2 * CUATRO_HORAS


def resumen(estado: EstadoTendencia, cfg: TendenciaConfig, now: datetime) -> dict:
    mids = {a: Decimal(v) for a, v in estado.ultimos_precios.items()}
    ab = abierto(estado, mids)
    total = Totales(
        bruto_usd=estado.realizado.bruto_usd + ab.bruto_usd,
        funding_usd=estado.realizado.funding_usd + ab.funding_usd,
        costos=estado.realizado.costos.sumar(ab.costos),
    )
    return {
        "estrategia": "tendencia",
        "capital_teorico_usd": s(estado.capital_usd, 2),
        "iniciado_en": estado.iniciado_en,
        "actualizado_en": estado.actualizado_en,
        "equity_usd": s(equity(estado, mids)),
        "realizado": totales_dict(estado.realizado),
        "abierto": totales_dict(ab),
        "total": totales_dict(total),
        "operaciones_cerradas": estado.operaciones_cerradas,
        "ganadoras": estado.ganadoras,
        "posiciones_abiertas": {
            a: {
                "lado": p.lado,
                "abierta_en": p.abierta_en,
                "entrada": s(p.entrada),
                "stop": s(p.stop),
                "nocional_usd": s(p.nocional_usd, 2),
                "riesgo_usd": s(p.riesgo_usd, 2),
                "funding_usd": s(p.funding_usd),
            }
            for a, p in estado.posiciones.items()
        },
        "max_drawdown_usd": s(estado.drawdown.max_drawdown_usd),
        "max_drawdown_pct": s(estado.drawdown.max_drawdown_pct, 4),
        "descarte": regla_descarte(estado, cfg, now),
        "vetos": dict(estado.vetos),
        "ultima_senal": estado.ultima_senal,
        "ultimos_cierres": estado.cierres[-5:],
    }


def _veto(estado: EstadoTendencia, regla: str) -> None:
    estado.vetos[regla] = int(estado.vetos.get(regla, 0)) + 1
