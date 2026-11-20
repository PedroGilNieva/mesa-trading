"""Sección de sombra (carry y tendencia) para el reporte diario y `/estado`.

Lee lo que dejó el último tick en `sombra/`. No llama a la red.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal

from mesa_trading.sombra.runner import CARRY_KEY, RESUMEN_KEY, TENDENCIA_KEY
from mesa_trading.storage import DataRoot
from mesa_trading.util import as_utc

CAMPOS_ESTADO = (
    "capital_teorico_usd",
    "equity_usd",
    "total",
    "realizado",
    "operaciones_cerradas",
    "posiciones_abiertas",
    "max_drawdown_usd",
    "max_drawdown_pct",
    "descarte",
    "actualizado_en",
)


def vista_estado(root: DataRoot) -> dict | None:
    """Lo que `/estado` muestra: un solo objeto (`sombra/resumen.json`)."""
    resumen = _leer(root, RESUMEN_KEY)
    if not resumen:
        return None
    return {
        "actualizado_en": resumen.get("actualizado_en"),
        "nota": resumen.get("nota"),
        "funding_persistencia": resumen.get("funding"),
        "estrategias": {
            nombre: {k: body.get(k) for k in CAMPOS_ESTADO}
            for nombre, body in (resumen.get("estrategias") or {}).items()
        },
        "errores": resumen.get("errores") or {},
    }


def seccion_diaria(root: DataRoot, now: datetime) -> dict | None:
    resumen = _leer(root, RESUMEN_KEY)
    if not resumen:
        return None
    desde = as_utc(now) - timedelta(hours=24)
    ventana = {}
    for nombre, key in (("carry", CARRY_KEY), ("tendencia", TENDENCIA_KEY)):
        estado = _leer(root, key)
        if not estado:
            continue
        ventana[nombre] = _cierres_ventana(estado.get("cierres") or [], desde)
    return {
        "actualizado_en": resumen.get("actualizado_en"),
        "nota": resumen.get("nota"),
        "funding_persistencia": resumen.get("funding"),
        "estrategias": resumen.get("estrategias") or {},
        "ultimas_24h": ventana,
        "errores": resumen.get("errores") or {},
    }


def render_md(seccion: dict | None) -> list[str]:
    lines = ["## Estrategias en sombra (papel, libro aparte)"]
    if not seccion:
        lines.extend(["- Sin datos todavía.", ""])
        return lines
    lines.append(f"{seccion.get('nota') or ''} Último tick: {seccion.get('actualizado_en')}.")
    lines.append("")
    for nombre, body in (seccion.get("estrategias") or {}).items():
        total = body.get("total") or {}
        regla = body.get("descarte") or {}
        lines.append(f"### {nombre}")
        lines.append(
            f"Capital teórico: {body.get('capital_teorico_usd')} USD. Equity: {body.get('equity_usd')} USD. "
            f"Operaciones cerradas: {body.get('operaciones_cerradas')}. Abiertas: {len(body.get('posiciones_abiertas') or {})}."
        )
        lines.append(
            f"Bruto {total.get('bruto_usd')} + funding {total.get('funding_usd')} - costos {total.get('costos_usd')} "
            f"(comisiones {total.get('comisiones_usd')}, deslizamiento {total.get('deslizamiento_usd')}, "
            f"gas {total.get('gas_usd')}) = neto {total.get('neto_usd')} USD (incluye lo abierto a mercado)."
        )
        lines.append(f"Drawdown máximo: {body.get('max_drawdown_usd')} USD ({_pct(body.get('max_drawdown_pct'))}).")
        lines.append(f"Regla de descarte: {regla.get('regla')}. Estado: {regla.get('estado')}. Progreso: {regla.get('progreso_pct')}%.")
        if nombre == "tendencia":
            lines.append(f"Esperanza neta por operación: {regla.get('esperanza_neta_por_operacion_usd')} USD.")
        if nombre == "carry":
            partes = [
                f"{a} {_pct(v.get('pct_positivo'))} ({v.get('horas_positivas')}/{v.get('horas_total')} h)"
                for a, v in (regla.get("funding_por_activo") or {}).items()
            ]
            if partes:
                lines.append("Funding positivo con posición abierta, por activo: " + "; ".join(partes) + ".")
        vent = (seccion.get("ultimas_24h") or {}).get(nombre)
        if vent:
            lines.append(f"Últimas 24 h: {vent['cierres']} cierres, neto {vent['neto_usd']} USD.")
        if body.get("vetos"):
            lines.append("Vetos: " + ", ".join(f"{k} {v}" for k, v in sorted(body["vetos"].items())) + ".")
        lines.append("")
    funding = (seccion.get("funding_persistencia") or {}).get("activos") or {}
    if funding:
        lines.append("### Persistencia del funding (Hyperliquid, cada hora)")
        for activo, ventanas in funding.items():
            for clave in ("30d", "90d"):
                v = ventanas.get(clave) or {}
                lines.append(
                    f"- {activo} {clave}: positivo {_pct(v.get('pct_positivo'))} de {v.get('periodos')} horas; "
                    f"anualizado medio {_pct(v.get('funding_anual_medio'))}."
                )
        lines.append("")
    if seccion.get("errores"):
        lines.append("Errores del último tick: " + ", ".join(f"{k}={v}" for k, v in seccion["errores"].items()) + ".")
        lines.append("")
    return lines


def _cierres_ventana(cierres: list[dict], desde: datetime) -> dict:
    n = 0
    neto = Decimal("0")
    for c in cierres:
        cuando = c.get("cerrada_en")
        if cuando and as_utc(cuando) >= desde:
            n += 1
            neto += Decimal(str(c.get("neto_usd") or "0"))
    return {"cierres": n, "neto_usd": format(neto, "f")}


def _pct(value) -> str:
    if value is None:
        return "n/d"
    return f"{(Decimal(str(value)) * 100).quantize(Decimal('0.01'))}%"


def _leer(root: DataRoot, key: str) -> dict | None:
    text = root.read_text(key)
    if not text:
        return None
    return json.loads(text)
