"""Un tick de las estrategias de sombra. Corre después del ciclo de la mesa.

Solo lecturas públicas (Hyperliquid `POST /info`) y escrituras en `sombra/`. No
toca `book/state.json` ni crea órdenes: no hay broker en este camino.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal

from mesa_trading.broker.fees import FeeSchedule
from mesa_trading.config import DeskConfig
from mesa_trading.sombra import carry, tendencia
from mesa_trading.sombra import funding as fh
from mesa_trading.storage import DataRoot
from mesa_trading.util import iso

CARRY_KEY = "sombra/carry/state.json"
TENDENCIA_KEY = "sombra/tendencia/state.json"
RESUMEN_KEY = "sombra/resumen.json"
NOTA = "Papel: capital teórico propio, sin órdenes ni broker. Libro aparte de house/gambler."


def activos(config: DeskConfig) -> list[str]:
    sc = config.sombra
    out: list[str] = []
    for cfg in (sc.carry, sc.tendencia):
        if cfg.enabled:
            out.extend(a for a in cfg.activos if a not in out)
    return out


def correr(root: DataRoot, fuente, config: DeskConfig, fees: dict[str, FeeSchedule], now: datetime) -> dict:
    sc = config.sombra
    lista = activos(config)
    if not lista:
        return {}
    errores: dict[str, str] = {}

    historial = fh.actualizar_historial(_leer(root, fh.HISTORIAL_KEY), fuente, lista, now, sc.funding_historial_dias)
    _escribir(root, fh.HISTORIAL_KEY, historial)
    for activo, err in (historial.get("errores") or {}).items():
        errores[f"funding:{activo}"] = err

    mercado: dict[str, dict] = {}
    for activo in lista:
        try:
            ctx = fuente.asset_context(activo) or {}
            book = fuente.order_book(activo) or {}
            spot = ctx.get("oracle")
            bid, ask = book.get("bid"), book.get("ask")
            if spot is None or bid is None or ask is None:
                errores[f"precio:{activo}"] = "sin_dato"
                continue
            mercado[activo] = {"spot": Decimal(str(spot)), "bid": Decimal(str(bid)), "ask": Decimal(str(ask))}
        except Exception as exc:  # noqa: BLE001
            errores[f"precio:{activo}"] = exc.__class__.__name__

    estrategias: dict[str, dict] = {}
    if sc.carry.enabled:
        raw = _leer(root, CARRY_KEY)
        estado_c = carry.EstadoCarry.model_validate(raw) if raw else carry.nuevo(sc.carry, now)
        precios = {a: mercado[a] for a in sc.carry.activos if a in mercado}
        carry.paso(estado_c, historial, precios, sc.carry, fees, config.risk.max_position_pct, now)
        _escribir(root, CARRY_KEY, json.loads(estado_c.model_dump_json()))
        estrategias["carry"] = carry.resumen(estado_c, historial, sc.carry, now)

    if sc.tendencia.enabled:
        cfg = sc.tendencia
        raw = _leer(root, TENDENCIA_KEY)
        estado_t = tendencia.EstadoTendencia.model_validate(raw) if raw else tendencia.nuevo(cfg, now)
        datos: dict[str, dict] = {}
        for activo in cfg.activos:
            if activo not in mercado:
                continue
            datos[activo] = {"bid": mercado[activo]["bid"], "ask": mercado[activo]["ask"]}
            if not tendencia.necesita_velas(estado_t, activo, now):
                continue
            try:
                # 4h: las velas de entrada y salida más holgura. 1D: SMA y ATR más holgura.
                horas_4h = (max(cfg.donchian_entrada_4h, cfg.donchian_salida_4h) + 6) * 4
                dias_1d = max(cfg.sma_1d, cfg.atr_1d + 1) + 10
                datos[activo]["velas_4h"] = fuente.candles(activo, "4h", now - timedelta(hours=horas_4h))
                datos[activo]["velas_1d"] = fuente.candles(activo, "1d", now - timedelta(days=dias_1d))
            except Exception as exc:  # noqa: BLE001
                errores[f"velas:{activo}"] = exc.__class__.__name__
                datos[activo].pop("velas_4h", None)
                datos[activo].pop("velas_1d", None)
        tendencia.paso(
            estado_t, datos, historial, cfg, fees, config.risk.max_position_pct, config.risk.max_exposure_pct, now
        )
        _escribir(root, TENDENCIA_KEY, json.loads(estado_t.model_dump_json()))
        estrategias["tendencia"] = tendencia.resumen(estado_t, cfg, now)

    resumen = {
        "actualizado_en": iso(now),
        "nota": NOTA,
        "funding": fh.resumen_persistencia(historial, lista, now),
        "estrategias": estrategias,
        "errores": errores,
    }
    _escribir(root, RESUMEN_KEY, resumen)
    return resumen


def _leer(root: DataRoot, key: str) -> dict | None:
    text = root.read_text(key)
    if not text:
        return None
    return json.loads(text)


def _escribir(root: DataRoot, key: str, payload: dict) -> None:
    root.write_text(key, json.dumps(payload, ensure_ascii=False, sort_keys=True))
