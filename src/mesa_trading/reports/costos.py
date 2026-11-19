"""PnL bruto vs. neto y costo por operación (issues #26 y #29).

Solo mide. No cambia la estrategia, los umbrales ni el tamaño. Todo el dinero
es simulado.

Definiciones, por operación cerrada (entrada + salida):

- ``P``: diferencia de precio entre los dos fills, por la cantidad. El
  SimBroker mete el deslizamiento en el precio del fill (compra más caro,
  vende más barato), así que ``P`` ya trae el deslizamiento restado una vez.
- bruto = ``P`` + deslizamiento de entrada + deslizamiento de salida. Es el
  resultado antes de comisiones, gas y deslizamiento.
- neto = bruto - comisiones - deslizamiento - gas = ``P`` - comisiones - gas.
- pnl_libro = lo que guarda el libro (``close_position``).
  - Cierres con ``contabilidad`` = 2 (desde #29): pnl_libro = neto. El
    deslizamiento se cobra una sola vez y el gas se cobra por pata.
  - Cierres viejos (sin ``contabilidad``): el libro restaba el deslizamiento
    otra vez sobre ``P`` y no cobraba gas, así que pnl_libro = neto -
    deslizamiento. Esa diferencia sale como ``deslizamiento_doble_contado_usd``.
    No se reescribe el libro.

Si el cierre no trae la cantidad (eventos de antes de #26), ``P`` se
reconstruye con la identidad del libro de ese cierre.

``recalculo_tabla_actual`` es un reporte aparte: vuelve a calcular las
operaciones guardadas con la contabilidad y la tabla de comisiones de hoy. No
toca el libro.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from mesa_trading.broker.fees import FeeSchedule, commission_usd
from mesa_trading.config import DeskConfig
from mesa_trading.util import as_utc, iso

CDMX = ZoneInfo("America/Mexico_City")
STORE_KEY = "reportes/costos/estado.json"
RESUMEN_KEY = "reportes/costos/resumen.json"
# Operaciones con detalle que se guardan para las simulaciones. Los totales por
# día no tienen techo; solo el detalle por operación lo tiene.
TRADE_CAP = 5000
DIAS_EN_REPORTE = 14
# La primera corrida sin estado relee desde el inicio del libro, con este techo.
BACKFILL_MAX_DAYS = 60
ENTRY_ACTIONS = ("long", "short", "quote")
ZERO = Decimal("0")
BPS = Decimal("10000")

# La fuente de cada tarifa vive en config/fees.yaml (campo ``fuente``). Esto es
# solo el respaldo para una tabla que no la trae.
FUENTE_SIN_CITA = "config/fees.yaml (la tabla no cita fuente)"
# Versión de la contabilidad del libro en la que pnl_libro = neto (#29).
CONTABILIDAD_CORREGIDA = 2

# Umbrales de entrada que se pueden endurecer y la medida que comparan.
# (llave del umbral, medida observada, sentido, paso). Sentido ">=" exige
# observado >= umbral; "<" exige observado < umbral. El paso endurece.
UMBRALES_SIMULABLES = {
    "gambler": (
        ("accion_min_p", "accion_p", ">=", Decimal("0.05")),
        ("setup_min", "setup", ">=", Decimal("0.25")),
        ("regimen_tendencia_min_p", "regimen_tendencia", ">=", Decimal("0.05")),
        ("ejecucion_degradada_max_p", "ejecucion_degradada", "<", Decimal("-0.05")),
    ),
    "house": (
        ("vale_cotizar_min", "vale_cotizar", ">=", Decimal("0.25")),
        ("toxico_max_p", "flujo_toxico", "<", Decimal("-0.05")),
    ),
}
MULTIPLICADORES_TAMANO = (Decimal("1"), Decimal("2"), Decimal("4"))


# --------------------------------------------------------------------------
# Reconstrucción de operaciones desde la bitácora


def trades_from_events(
    events: Iterable[dict], carry: dict[str, dict] | None = None
) -> tuple[list[dict], dict[str, dict]]:
    """Empareja entradas y cierres. Devuelve operaciones cerradas y entradas abiertas.

    ``carry`` son entradas abiertas de una corrida anterior, por mercado.
    """
    ordered = sorted(events, key=lambda event: str(event.get("occurred_at") or ""))
    submitted_cycle: dict[str, str] = {}
    decided: dict[tuple[str, str, str], dict] = {}
    open_entries: dict[str, dict] = {key: dict(value) for key, value in (carry or {}).items()}
    trades: list[dict] = []
    for event in ordered:
        kind = event.get("event_type")
        payload = event.get("payload") or {}
        bot = event.get("bot") or payload.get("bot") or "?"
        cycle = str(event.get("correlation_id") or "")
        if kind == "OrderSubmitted":
            coid = payload.get("client_order_id")
            if coid:
                submitted_cycle.setdefault(str(coid), cycle)
        elif kind == "PolicyDecided":
            if payload.get("action") in ENTRY_ACTIONS:
                decided[(cycle, bot, str(payload.get("market") or ""))] = payload
        elif kind == "OrderFilled":
            market = str(payload.get("market") or "")
            if payload.get("action") == "close":
                entry = open_entries.pop(market, None)
                trades.append(_trade(event, payload, bot, entry))
            else:
                coid = str(payload.get("client_order_id") or "")
                origin = submitted_cycle.get(coid, cycle)
                decision = decided.get((origin, bot, market)) or {}
                open_entries[market] = {
                    "client_order_id": coid,
                    "bot": bot,
                    "market": market,
                    "action": payload.get("action"),
                    "side": payload.get("side") or ("short" if payload.get("action") == "short" else "long"),
                    "at": event.get("occurred_at"),
                    "price": payload.get("price"),
                    "qty": payload.get("qty"),
                    "notional_usd": payload.get("notional_usd"),
                    "fee_usd": payload.get("fee_usd"),
                    "slippage_usd": payload.get("slippage_usd"),
                    "gas_usd": payload.get("gas_usd"),
                    "motivo": decision.get("reason") or "",
                    "observados": dict(decision.get("observados") or {}),
                    "umbrales": {k: str(v) for k, v in (decision.get("thresholds") or {}).items()},
                }
    return trades, open_entries


def _trade(event: dict, payload: dict, bot: str, entry: dict | None) -> dict:
    entry = entry or {}
    side = payload.get("side") or entry.get("side") or "long"
    fee_out = _d(payload.get("fee_usd"))
    slip_out = _d(payload.get("slippage_usd"))
    fee_in = _first_d(payload.get("entry_fee_usd"), entry.get("fee_usd"))
    slip_in = _first_d(payload.get("entry_slippage_usd"), entry.get("slippage_usd"))
    pnl_libro = _d(payload.get("pnl_usd"))
    entry_known = fee_in is not None and slip_in is not None
    fee_in = fee_in or ZERO
    slip_in = slip_in or ZERO
    fees = fee_in + fee_out
    slips = slip_in + slip_out
    # Antes de #29 el libro no cobraba gas: en esos cierres el gas cobrado es 0.
    contabilidad = int(_d(payload.get("contabilidad")) or 1)
    corregida = contabilidad >= CONTABILIDAD_CORREGIDA
    gas_in = _first_d(payload.get("entry_gas_usd"), entry.get("gas_usd")) if corregida else None
    gas_out = _d(payload.get("gas_usd")) if corregida else ZERO
    gas = (gas_in or ZERO) + gas_out
    exit_price = _first_d(payload.get("price"))
    entry_price = _first_d(payload.get("entry_price"), entry.get("price"))
    qty = _first_d(payload.get("qty"), payload.get("entry_qty"), entry.get("qty"))
    if exit_price is not None and entry_price is not None and qty is not None:
        move = (exit_price - entry_price) * qty
        price_move = move if side == "long" else -move
        fuente = "precios"
    elif corregida:
        # Identidad de close_position desde #29: pnl = P - comisiones - gas.
        price_move = pnl_libro + fees + gas
        fuente = "identidad_libro"
    else:
        # Identidad vieja de close_position: pnl = P - comisiones - deslizamiento.
        price_move = pnl_libro + fees + slips
        fuente = "identidad_libro"
    bruto = price_move + slips
    neto = bruto - fees - slips - gas
    nocional = _first_d(payload.get("entry_notional_usd"), entry.get("notional_usd")) or ZERO
    closed_at = event.get("occurred_at") or ""
    return {
        "id": str(payload.get("client_order_id") or event.get("event_id") or closed_at),
        "bot": bot,
        "market": str(payload.get("market") or ""),
        "side": side,
        "motivo_entrada": entry.get("motivo") or "",
        "motivo_salida": str(payload.get("reason") or ""),
        "abierta_en": entry.get("at") or "",
        "cerrada_en": closed_at,
        "dia": _cdmx_day(closed_at),
        "nocional_usd": _s(nocional),
        "nocional_salida_usd": _s(_d(payload.get("notional_usd"))),
        "bruto_usd": _s(bruto),
        "comision_entrada_usd": _s(fee_in),
        "comision_salida_usd": _s(fee_out),
        "comisiones_usd": _s(fees),
        "deslizamiento_usd": _s(slips),
        "gas_usd": _s(gas),
        "neto_usd": _s(neto),
        "pnl_libro_usd": _s(pnl_libro),
        "contabilidad": contabilidad,
        "entrada_conocida": entry_known,
        "fuente_bruto": fuente,
        "observados": dict(entry.get("observados") or {}),
        "umbrales": dict(entry.get("umbrales") or {}),
    }


# --------------------------------------------------------------------------
# Agregados


def _blank() -> dict[str, Any]:
    return {
        "operaciones": 0,
        "nocional": ZERO,
        "bruto": ZERO,
        "comisiones": ZERO,
        "deslizamiento": ZERO,
        "gas": ZERO,
        "neto": ZERO,
        "pnl_libro": ZERO,
        "ganadoras_bruto": 0,
        "ganadoras_neto": 0,
    }


def _add(acc: dict, trade: dict) -> None:
    acc["operaciones"] += 1
    acc["nocional"] += _d(trade.get("nocional_usd"))
    bruto = _d(trade.get("bruto_usd"))
    neto = _d(trade.get("neto_usd"))
    acc["bruto"] += bruto
    acc["comisiones"] += _d(trade.get("comisiones_usd"))
    acc["deslizamiento"] += _d(trade.get("deslizamiento_usd"))
    acc["gas"] += _d(trade.get("gas_usd"))
    acc["neto"] += neto
    acc["pnl_libro"] += _d(trade.get("pnl_libro_usd"))
    acc["ganadoras_bruto"] += 1 if bruto > 0 else 0
    acc["ganadoras_neto"] += 1 if neto > 0 else 0


def _merge(acc: dict, other: dict) -> None:
    for key in acc:
        acc[key] += other.get(key, 0) if isinstance(acc[key], int) else _d(other.get(key))


def stats(acc: dict) -> dict:
    """Totales y promedios por operación. Montos en USD, tasas en bps del nocional."""
    count = int(acc["operaciones"])
    gas = acc.get("gas", ZERO)
    costos = acc["comisiones"] + acc["deslizamiento"] + gas
    out = {
        "operaciones": count,
        "pnl_bruto_usd": _s(acc["bruto"]),
        "comisiones_usd": _s(acc["comisiones"]),
        "deslizamiento_usd": _s(acc["deslizamiento"]),
        "gas_usd": _s(gas),
        "costos_usd": _s(costos),
        "pnl_neto_usd": _s(acc["neto"]),
        "pnl_libro_usd": _s(acc["pnl_libro"]),
        "deslizamiento_doble_contado_usd": _s(acc["neto"] - acc["pnl_libro"]),
        "nocional_total_usd": _s(acc["nocional"]),
        "ganadoras_bruto": int(acc["ganadoras_bruto"]),
        "ganadoras_neto": int(acc["ganadoras_neto"]),
    }
    if count:
        n = Decimal(count)
        out.update(
            {
                "tamano_promedio_usd": _s(acc["nocional"] / n),
                "bruto_promedio_usd": _s(acc["bruto"] / n),
                "costo_promedio_usd": _s(costos / n),
                "comision_promedio_usd": _s(acc["comisiones"] / n),
                "neto_promedio_usd": _s(acc["neto"] / n),
            }
        )
    if acc["nocional"] > 0:
        out["bruto_bps"] = _bps(acc["bruto"] / acc["nocional"] * BPS)
        out["costo_bps"] = _bps(costos / acc["nocional"] * BPS)
    out["cubre_costos"] = bool(count) and acc["bruto"] > costos
    return out


def summarize(trades: Iterable[dict], key=lambda trade: trade.get("bot") or "?") -> dict:
    total = _blank()
    groups: dict[str, dict] = defaultdict(_blank)
    for trade in trades:
        _add(total, trade)
        _add(groups[key(trade)], trade)
    return {"total": stats(total), "por_estrategia": {name: stats(acc) for name, acc in sorted(groups.items())}}


# --------------------------------------------------------------------------
# Estado persistente (como pares.json): totales por día y detalle con techo


def update_cost_store(store: dict | None, events: list[dict]) -> tuple[dict, list[dict]]:
    """Suma las operaciones nuevas una sola vez. Devuelve (estado, nuevas)."""
    current = {
        "version": 2,
        "desde": None,
        "abiertas": {},
        "operaciones": [],
        "dias": {},
        "dias_mercado": None,
    }
    if store:
        current.update({key: store[key] for key in current if key in store and key != "version"})
    if current["dias_mercado"] is None:
        # Estado de antes de #29: los totales por mercado salen del detalle guardado
        # (hasta TRADE_CAP operaciones; las más viejas que eso ya no están).
        current["dias_mercado"] = {}
        for trade in current["operaciones"]:
            _bump_day(current["dias_mercado"], trade, trade.get("market") or "?")
    trades, open_entries = trades_from_events(events, current.get("abiertas"))
    known = {trade["id"] for trade in current["operaciones"]}
    fresh = [trade for trade in trades if trade["id"] not in known]
    dias = current["dias"]
    for trade in fresh:
        _bump_day(dias, trade, trade["bot"])
        _bump_day(current["dias_mercado"], trade, trade.get("market") or "?")
        if current["desde"] is None or trade["cerrada_en"] < current["desde"]:
            current["desde"] = trade["cerrada_en"]
    operaciones = current["operaciones"] + fresh
    operaciones.sort(key=lambda trade: trade.get("cerrada_en") or "")
    current["operaciones"] = operaciones[-TRADE_CAP:]
    current["abiertas"] = open_entries
    return current, fresh


def _bump_day(dias: dict, trade: dict, key: str) -> None:
    day = dias.setdefault(trade.get("dia") or "?", {})
    acc = _from_json(day.get(key))
    _add(acc, trade)
    day[key] = _to_json(acc)


def market_type(config: DeskConfig | None, market: str) -> str:
    """dex o perps (o el kind del mercado si no es ninguno de los dos)."""
    spec = config.markets.get(market) if config is not None else None
    if spec is None:
        return "perps" if market.upper().endswith("-PERP") else "?"
    if spec.kind == "perp":
        return "perps"
    if spec.venue == "geckoterminal" or "dex" in spec.fee_schedule or spec.fee_schedule.startswith("uniswap"):
        return "dex"
    return spec.kind


def by_market_type(trades: Iterable[dict], config: DeskConfig | None) -> dict:
    """Por tipo de mercado (dex / perps): total y cada mercado."""
    groups: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(_blank))
    for trade in trades:
        market = trade.get("market") or "?"
        _add(groups[market_type(config, market)][market], trade)
    return _type_view(groups)


def _type_view(groups: dict[str, dict[str, dict]]) -> dict:
    out = {}
    for tipo, markets in sorted(groups.items()):
        total = _blank()
        for acc in markets.values():
            _merge(total, acc)
        out[tipo] = {"total": stats(total), "por_mercado": {m: stats(acc) for m, acc in sorted(markets.items())}}
    return out


def backfill_start(store: dict | None, book_started_at: str | None, now: datetime) -> datetime:
    """Sin estado: desde el inicio del libro (con techo). Con estado: 26 h."""
    if store:
        return as_utc(now) - timedelta(days=1, hours=2)
    floor = as_utc(now) - timedelta(days=BACKFILL_MAX_DAYS)
    if book_started_at:
        try:
            started = as_utc(datetime.fromisoformat(str(book_started_at).replace("Z", "+00:00")))
            return max(started, floor)
        except ValueError:
            pass
    return floor


# --------------------------------------------------------------------------
# Lo que se ve desde el libro (sin bitácora): sirve en /estado en vivo


def book_cost_view(state: dict | None, config: DeskConfig | None = None) -> dict:
    """Bruto, comisiones, deslizamiento, gas y neto acumulados, de todas las estrategias.

    Sale solo de book/state.json. Los costos de la posición abierta se quitan:
    todavía no entran al PnL realizado.

    El realizado del libro no se reescribe. Las operaciones cerradas antes de
    #29 restaron el deslizamiento dos veces; ese monto queda congelado en
    ``slippage_double_counted_legacy_usd`` y se suma de vuelta para el neto. Un
    libro que aún no pasó por la contabilidad nueva (sin ese campo) tiene todo
    su deslizamiento cerrado contado dos veces.
    """
    state = state or {}
    position = state.get("position") or {}
    fees = _d(state.get("fees_usd")) - _d(position.get("fees_usd"))
    slips = _d(state.get("slippage_usd")) - _d(position.get("slippage_usd"))
    gas = _d(state.get("gas_usd")) - _d(position.get("gas_usd"))
    libro = _d(state.get("realized_pnl_usd"))
    legacy = state.get("slippage_double_counted_legacy_usd")
    doble = slips if legacy is None else _d(legacy)
    neto = libro + doble
    bruto = neto + fees + slips + gas
    return {
        "operaciones_cerradas": int(state.get("closed_trades") or 0),
        "pnl_bruto_usd": _s(bruto),
        "comisiones_usd": _s(fees),
        "deslizamiento_usd": _s(slips),
        "gas_usd": _s(gas),
        "pnl_neto_usd": _s(neto),
        "pnl_libro_usd": _s(libro),
        "deslizamiento_doble_contado_usd": _s(doble),
        "costos_posicion_abierta_usd": {
            "comisiones": _s(_d(position.get("fees_usd"))),
            "deslizamiento": _s(_d(position.get("slippage_usd"))),
            "gas": _s(_d(position.get("gas_usd"))),
        },
        "por_tipo_mercado": book_costs_by_type(state, config),
        "nota": (
            "Acumulado del libro, todas las estrategias. neto = bruto - comisiones - deslizamiento - gas. "
            "pnl_libro es el realizado que guarda el libro; desde #29 cobra el deslizamiento una vez y el gas. "
            "deslizamiento_doble_contado_usd es lo que las operaciones de antes de #29 restaron de más: "
            "neto = pnl_libro + ese monto. El libro no se reescribe."
        ),
    }


def book_costs_by_type(state: dict | None, config: DeskConfig | None = None) -> dict:
    """Comisiones, deslizamiento y gas por tipo de mercado (dex / perps), en vivo.

    Sale de ``costs_by_market`` del libro: cuenta desde #29 y cada pata
    (entrada o salida), incluida la entrada de la posición abierta.
    """
    raw = (state or {}).get("costs_by_market") or {}
    groups: dict[str, dict[str, dict]] = defaultdict(dict)
    for market, body in raw.items():
        groups[market_type(config, market)][market] = {
            "patas": int(_d(body.get("legs"))),
            "comisiones_usd": _s(_d(body.get("fees_usd"))),
            "deslizamiento_usd": _s(_d(body.get("slippage_usd"))),
            "gas_usd": _s(_d(body.get("gas_usd"))),
        }
    out: dict[str, Any] = {}
    for tipo, markets in sorted(groups.items()):
        total = {key: ZERO for key in ("comisiones_usd", "deslizamiento_usd", "gas_usd")}
        legs = 0
        for body in markets.values():
            legs += body["patas"]
            for key in total:
                total[key] += _d(body[key])
        out[tipo] = {
            "patas": legs,
            **{key: _s(value) for key, value in total.items()},
            "por_mercado": dict(sorted(markets.items())),
        }
    return out


# --------------------------------------------------------------------------
# Modelo de comisiones


def fee_model(fees: dict[str, FeeSchedule], config: DeskConfig, notional: Decimal | None = None) -> dict:
    """Tarifa por tabla: parte porcentual, parte fija y de dónde sale."""
    size = notional if notional is not None else config.capital_usd * config.risk.max_position_pct
    used: dict[str, list[str]] = defaultdict(list)
    for market, spec in config.markets.items():
        used[spec.fee_schedule].append(market)
    tablas = {}
    for name, schedule in fees.items():
        per_leg = commission_usd(size, schedule)
        proportional = size * schedule.taker_bps / BPS
        tablas[name] = {
            "mercados": sorted(used.get(name, [])),
            "porcentual_bps": _bps(schedule.taker_bps),
            "minimo_por_operacion_usd": _s(schedule.min_fee_usd),
            "gas_fijo_por_operacion_usd": _s(schedule.gas_usd),
            "gas_por_pata_usd": _s(schedule.gas_usd),
            "gas_ida_y_vuelta_usd": _s(schedule.gas_usd * 2),
            "gas_cobrado_en_libro": True,
            "nocional_ejemplo_usd": _s(size),
            "comision_por_pata_usd": _s(per_leg),
            "comision_ida_y_vuelta_usd": _s(per_leg * 2),
            "parte_porcentual_por_pata_usd": _s(proportional),
            "parte_fija_por_pata_usd": _s(per_leg - proportional),
            "fuente": schedule.fuente or FUENTE_SIN_CITA,
        }
    return {
        "formula": (
            "comision = max(nocional × taker_bps / 10000, min_fee_usd) por pata (entrada y salida). "
            "El mínimo no se suma: reemplaza a la parte porcentual cuando ésta es menor. "
            "Además, gas_usd fijo por pata (entrada y salida); hoy solo el pool DEX lo tiene. "
            "Cada mercado elige su tabla con fee_schedule en config/desk.yaml."
        ),
        "codigo": "src/mesa_trading/broker/fees.py (commission_usd) y config/fees.yaml",
        "deslizamiento": (
            "deslizamiento = nocional × (medio spread + impacto + 1 bp extra) / 10000, con impacto = "
            "nocional / liquidez × 10000. Si el fill cruza el libro (bid/ask), el medio spread ya está "
            "en el precio y no se suma otra vez; ese medio spread queda dentro del bruto. Es una estimación "
            "del simulador, no un costo observado."
        ),
        "tablas": tablas,
    }


# --------------------------------------------------------------------------
# Qué habría pasado (solo reporte)


def threshold_scenarios(trades: list[dict]) -> list[dict]:
    """Operaciones que habrían pasado un umbral de entrada más alto.

    Usa las medidas que la política anotó al entrar. Supone que las demás
    operaciones no cambian: no reconstruye entradas que un umbral más alto
    habría dejado libres.
    """
    rows = []
    for bot, rules in UMBRALES_SIMULABLES.items():
        base = [trade for trade in trades if trade.get("bot") == bot and trade.get("observados") and trade.get("umbrales")]
        if not base:
            continue
        rows.append(_scenario_row(bot, "actual", {}, base, base))
        candidates: list[tuple[str, dict[str, Decimal]]] = []
        for steps in (1, 2):
            for key, _obs, _sense, step in rules:
                candidates.append((f"{key}+{steps}", {key: step * steps}))
            candidates.append((f"todos+{steps}", {key: step * steps for key, _o, _s, step in rules}))
        for name, deltas in candidates:
            kept = [trade for trade in base if _passes(trade, rules, deltas)]
            rows.append(_scenario_row(bot, name, deltas, base, kept))
    return rows


def _passes(trade: dict, rules: tuple, deltas: dict[str, Decimal]) -> bool:
    observed = trade.get("observados") or {}
    current = trade.get("umbrales") or {}
    for key, obs, sense, _step in rules:
        if key not in deltas:
            continue
        if key not in current or obs not in observed:
            return False
        try:
            limit = Decimal(str(current[key])) + deltas[key]
            value = Decimal(str(observed[obs]))
        except (InvalidOperation, ValueError):
            return False
        if sense == ">=" and value < limit:
            return False
        if sense == "<" and value >= limit:
            return False
    return True


def _scenario_row(bot: str, name: str, deltas: dict, base: list[dict], kept: list[dict]) -> dict:
    acc = _blank()
    for trade in kept:
        _add(acc, trade)
    body = stats(acc)
    return {
        "estrategia": bot,
        "escenario": name,
        "endurece": {key: _s(value) for key, value in deltas.items()},
        "operaciones_base": len(base),
        "operaciones": body["operaciones"],
        "pnl_bruto_usd": body["pnl_bruto_usd"],
        "costos_usd": body["costos_usd"],
        "pnl_neto_usd": body["pnl_neto_usd"],
        "neto_promedio_usd": body.get("neto_promedio_usd", "0"),
    }


def size_scenarios(trades: list[dict], fees: dict[str, FeeSchedule], config: DeskConfig) -> list[dict]:
    """Mismas operaciones con órdenes k veces más grandes.

    El bruto escala con k. La comisión se recalcula por pata con la tabla
    (así el mínimo fijo, si lo hay, se diluye). El deslizamiento se escala con
    k a bps constantes: con la liquidez de estos libros el impacto es casi
    cero. El tope de riesgo (max_position_pct) no permitiría k > 1 hoy.
    """
    rows = []
    bots = sorted({trade.get("bot") or "?" for trade in trades})
    for bot in bots:
        mine = [trade for trade in trades if (trade.get("bot") or "?") == bot]
        for k in MULTIPLICADORES_TAMANO:
            bruto = fee_total = slip_total = gas_total = nocional = ZERO
            for trade in mine:
                market = trade.get("market") or ""
                spec = config.markets.get(market)
                schedule = fees.get(spec.fee_schedule) if spec else None
                bruto += _d(trade.get("bruto_usd")) * k
                slip_total += _d(trade.get("deslizamiento_usd")) * k
                # El gas es fijo por pata: no crece con el tamaño.
                gas_total += _d(trade.get("gas_usd"))
                nocional += _d(trade.get("nocional_usd")) * k
                if schedule is None:
                    fee_total += _d(trade.get("comisiones_usd")) * k
                    continue
                for leg in (trade.get("nocional_usd"), trade.get("nocional_salida_usd") or trade.get("nocional_usd")):
                    fee_total += commission_usd(_d(leg) * k, schedule)
            costos = fee_total + slip_total + gas_total
            row = {
                "estrategia": bot,
                "multiplicador": format(k, "f"),
                "operaciones": len(mine),
                "pnl_bruto_usd": _s(bruto),
                "comisiones_usd": _s(fee_total),
                "deslizamiento_usd": _s(slip_total),
                "gas_usd": _s(gas_total),
                "pnl_neto_usd": _s(bruto - costos),
            }
            if nocional > 0:
                row["costo_bps"] = _bps(costos / nocional * BPS)
                row["bruto_bps"] = _bps(bruto / nocional * BPS)
            rows.append(row)
    return rows


# --------------------------------------------------------------------------
# Recalculo con la tabla y la contabilidad de hoy (reporte aparte, no toca el libro)


def recalc_current_table(trades: list[dict], fees: dict[str, FeeSchedule], config: DeskConfig) -> dict:
    """Las operaciones guardadas, otra vez, con la contabilidad de #29 y la tabla actual.

    Por operación: neto_tabla_actual = P - comisión(tabla actual, por pata) -
    gas(tabla actual, por pata). ``P`` es el mismo movimiento de precio que ya
    se midió (con el deslizamiento dentro, una sola vez). Se separa cuánto
    cambia por cada corrección:

    - deslizamiento_doble: lo que el libro restó de más (neto - pnl_libro).
    - comisiones: comisión registrada - comisión con la tabla de hoy (p. ej. el
      pool WETH/USDC pasa de 30 a 5 bps).
    - gas: gas registrado - gas con la tabla de hoy (negativo: antes no se cobraba).
    """
    groups: dict[str, dict[str, dict]] = {"total": {}, "por_estrategia": {}, "por_tipo_mercado": {}}
    for trade in trades:
        market = trade.get("market") or ""
        spec = config.markets.get(market)
        schedule = fees.get(spec.fee_schedule) if spec else None
        fees_now = _d(trade.get("comisiones_usd"))
        gas_now = _d(trade.get("gas_usd"))
        if schedule is not None:
            legs = (trade.get("nocional_usd"), trade.get("nocional_salida_usd") or trade.get("nocional_usd"))
            fees_now = sum((commission_usd(_d(leg), schedule) for leg in legs), ZERO)
            gas_now = schedule.gas_usd * 2
        row = {
            "operaciones": 1,
            "pnl_libro": _d(trade.get("pnl_libro_usd")),
            "neto_registrado": _d(trade.get("neto_usd")),
            "efecto_deslizamiento_doble": _d(trade.get("neto_usd")) - _d(trade.get("pnl_libro_usd")),
            "efecto_comisiones": _d(trade.get("comisiones_usd")) - fees_now,
            "efecto_gas": _d(trade.get("gas_usd")) - gas_now,
        }
        row["neto_tabla_actual"] = row["neto_registrado"] + row["efecto_comisiones"] + row["efecto_gas"]
        for bucket, key in (
            ("total", "total"),
            ("por_estrategia", trade.get("bot") or "?"),
            ("por_tipo_mercado", market_type(config, market)),
        ):
            acc = groups[bucket].setdefault(key, {name: (0 if name == "operaciones" else ZERO) for name in row})
            for name, value in row.items():
                acc[name] += value

    def view(acc: dict | None) -> dict:
        acc = acc or {}
        out = {"operaciones": int(acc.get("operaciones", 0))}
        for name in ("pnl_libro", "neto_registrado", "efecto_deslizamiento_doble", "efecto_comisiones", "efecto_gas", "neto_tabla_actual"):
            out[f"{name}_usd"] = _s(acc.get(name, ZERO))
        out["diferencia_vs_libro_usd"] = _s(acc.get("neto_tabla_actual", ZERO) - acc.get("pnl_libro", ZERO))
        return out

    return {
        "nota": (
            "Reporte aparte. No reescribe el libro: pnl_libro es lo que el libro guardó. neto_tabla_actual "
            "recalcula cada operación guardada con el deslizamiento una sola vez, la comisión de la tabla "
            f"de hoy y el gas por pata. Cubre las operaciones con detalle (hasta {TRADE_CAP})."
        ),
        "total": view(groups["total"].get("total")),
        "por_estrategia": {key: view(acc) for key, acc in sorted(groups["por_estrategia"].items())},
        "por_tipo_mercado": {key: view(acc) for key, acc in sorted(groups["por_tipo_mercado"].items())},
    }


# --------------------------------------------------------------------------
# Sección del reporte


def build_cost_section(
    store: dict,
    fees: dict[str, FeeSchedule],
    config: DeskConfig,
    now: datetime,
    book_state: dict | None = None,
) -> dict:
    trades = list(store.get("operaciones") or [])
    end = as_utc(now)
    start = end - timedelta(days=1)
    window = [trade for trade in trades if start <= _parse(trade.get("cerrada_en")) <= end]
    dias = store.get("dias") or {}
    por_dia = {}
    acumulado = _blank()
    acumulado_bot: dict[str, dict] = defaultdict(_blank)
    for day in sorted(dias):
        total = _blank()
        bots = {}
        for bot, raw in sorted(dias[day].items()):
            acc = _from_json(raw)
            _merge(total, acc)
            _merge(acumulado, acc)
            _merge(acumulado_bot[bot], acc)
            bots[bot] = stats(acc)
        por_dia[day] = {"total": stats(total), "por_estrategia": bots}
    recientes = dict(list(por_dia.items())[-DIAS_EN_REPORTE:])
    mercados: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(_blank))
    for _day, markets in sorted((store.get("dias_mercado") or {}).items()):
        for market, raw in markets.items():
            _merge(mercados[market_type(config, market)][market], _from_json(raw))
    detalle = summarize(trades)
    sin_entrada = sum(1 for trade in trades if not trade.get("entrada_conocida"))
    return {
        "ventana_24h": summarize(window),
        "por_dia_cdmx": recientes,
        "acumulado": {
            "desde": store.get("desde"),
            "total": stats(acumulado),
            "por_estrategia": {bot: stats(acc) for bot, acc in sorted(acumulado_bot.items())},
        },
        "acumulado_libro": book_cost_view(book_state, config),
        # Comisiones, deslizamiento y gas partidos por tipo de mercado (#29).
        "por_tipo_mercado": {
            "ventana_24h": by_market_type(window, config),
            "acumulado": _type_view(mercados),
        },
        "recalculo_tabla_actual": recalc_current_table(trades, fees, config),
        "por_senal": summarize(trades, key=lambda t: f"{t.get('bot')}/{t.get('market')}/{t.get('side')}")["por_estrategia"],
        "por_salida": summarize(trades, key=lambda t: f"{t.get('bot')}/{t.get('motivo_salida') or '?'}")["por_estrategia"],
        "operaciones_con_detalle": len(trades),
        "operaciones_sin_entrada_en_bitacora": sin_entrada,
        "por_estrategia_detalle": detalle["por_estrategia"],
        "modelo_comisiones": fee_model(fees, config),
        "simulacion": {
            "nota": (
                "Solo reporte. No cambia umbrales ni tamaño. Usa las operaciones con detalle guardadas "
                f"(hasta {TRADE_CAP})."
            ),
            "umbral_mas_alto": threshold_scenarios(trades),
            "ordenes_mas_grandes": size_scenarios(trades, fees, config),
        },
        "glosario": {
            "pnl_bruto": (
                "resultado de precio antes de comisiones y deslizamiento. Cuando el fill cruza el libro, "
                "se mide desde el bid/ask: el medio spread queda dentro del bruto"
            ),
            "comisiones": "lo que cobra la tabla de comisiones del simulador, entrada + salida",
            "deslizamiento": (
                "estimación del simulador (medio spread + impacto + 1 bp), entrada + salida. Va dentro del "
                "precio del fill; se cobra una vez"
            ),
            "gas": "gas fijo por pata de la tabla (hoy solo el pool DEX), entrada + salida; desde #29",
            "pnl_neto": "bruto - comisiones - deslizamiento - gas",
            "pnl_libro": (
                "realizado que guarda el libro. Desde #29 es igual al neto; los cierres de antes restaban "
                "el deslizamiento dos veces (deslizamiento_doble_contado_usd) y no cobraban gas"
            ),
        },
    }


def summary_for_estado(section: dict, now: datetime) -> dict:
    """Lo que /estado lee: chico, sin detalle por operación."""
    return {
        "actualizado_en": iso(now),
        "ventana_24h": section.get("ventana_24h"),
        "por_dia_cdmx": section.get("por_dia_cdmx"),
        "acumulado": section.get("acumulado"),
        "por_tipo_mercado": section.get("por_tipo_mercado"),
        "recalculo_tabla_actual": section.get("recalculo_tabla_actual"),
        "modelo_comisiones": section.get("modelo_comisiones"),
    }


def render_cost_md(section: dict | None) -> list[str]:
    if not section:
        return []
    lines = ["## PnL bruto vs. neto y costos", ""]
    lines.append("bruto = antes de comisiones, deslizamiento y gas. neto = bruto - comisiones - deslizamiento - gas.")
    lines.append("")

    def row(label: str, body: dict) -> str:
        text = (
            f"- {label}: {body['operaciones']} ops. Bruto {body['pnl_bruto_usd']}, comisiones {body['comisiones_usd']}, "
            f"deslizamiento {body['deslizamiento_usd']}, gas {body.get('gas_usd', '0.0000')}, neto {body['pnl_neto_usd']} USD"
        )
        if body.get("pnl_libro_usd") is not None:
            text += f" (libro {body['pnl_libro_usd']})"
        return text + "."

    ventana = section.get("ventana_24h") or {}
    lines.append("### Últimas 24 h")
    lines.append(row("total", ventana.get("total") or stats(_blank())))
    for bot, body in (ventana.get("por_estrategia") or {}).items():
        lines.append(row(bot, body))
    lines.append("")
    lines.append("### Por día (CDMX)")
    por_dia = section.get("por_dia_cdmx") or {}
    if not por_dia:
        lines.append("- Sin operaciones cerradas")
    for day, body in por_dia.items():
        lines.append(row(day, body["total"]))
        for bot, inner in body["por_estrategia"].items():
            lines.append("  " + row(bot, inner))
    lines.append("")
    acumulado = section.get("acumulado") or {}
    lines.append(f"### Acumulado (desde {acumulado.get('desde') or '—'})")
    lines.append(row("total", acumulado.get("total") or stats(_blank())))
    for bot, body in (acumulado.get("por_estrategia") or {}).items():
        lines.append(row(bot, body))
    libro = section.get("acumulado_libro") or {}
    if libro:
        lines.append(
            f"- Desde el libro (todas, {libro['operaciones_cerradas']} cerradas): bruto {libro['pnl_bruto_usd']}, "
            f"comisiones {libro['comisiones_usd']}, deslizamiento {libro['deslizamiento_usd']}, "
            f"gas {libro.get('gas_usd', '0.0000')}, neto {libro['pnl_neto_usd']}, libro {libro['pnl_libro_usd']} USD "
            f"(deslizamiento contado dos veces antes de #29: {libro['deslizamiento_doble_contado_usd']})."
        )
    lines.append("")
    tipos = section.get("por_tipo_mercado") or {}
    lines.append("### Comisiones por tipo de mercado (DEX vs. perps)")
    for periodo in ("ventana_24h", "acumulado"):
        bloque = tipos.get(periodo) or {}
        if not bloque:
            lines.append(f"- {periodo}: sin operaciones.")
        for tipo, body in bloque.items():
            lines.append(row(f"{periodo} {tipo}", body["total"]))
            for market, inner in body["por_mercado"].items():
                lines.append("  " + row(market, inner))
    lines.append("")
    recalc = section.get("recalculo_tabla_actual") or {}
    if recalc:
        lines.append("### Recalculo con la tabla y la contabilidad de hoy (reporte aparte)")
        lines.append(recalc.get("nota", ""))

        def recalc_row(label: str, body: dict) -> str:
            return (
                f"- {label}: {body['operaciones']} ops. Libro {body['pnl_libro_usd']} → recalculado "
                f"{body['neto_tabla_actual_usd']} USD (deslizamiento doble +{body['efecto_deslizamiento_doble_usd']}, "
                f"comisiones {body['efecto_comisiones_usd']}, gas {body['efecto_gas_usd']})."
            )

        lines.append(recalc_row("total", recalc.get("total") or {}))
        for name, body in (recalc.get("por_tipo_mercado") or {}).items():
            lines.append(recalc_row(name, body))
        for name, body in (recalc.get("por_estrategia") or {}).items():
            lines.append(recalc_row(name, body))
        lines.append("")
    lines.append("### Por estrategia: ¿la señal cubre sus costos?")
    for bot, body in (section.get("por_estrategia_detalle") or {}).items():
        lines.append(_coverage_line(bot, body))
    no_cubren = [name for name, body in (section.get("por_senal") or {}).items() if not body.get("cubre_costos")]
    lines.append("")
    lines.append("Señales (estrategia/mercado/lado) que no cubren sus costos: " + (", ".join(no_cubren) if no_cubren else "ninguna") + ".")
    for name, body in (section.get("por_senal") or {}).items():
        lines.append(_coverage_line(name, body))
    lines.append("")
    lines.append("### Modelo de comisiones del simulador")
    model = section.get("modelo_comisiones") or {}
    lines.append(model.get("formula", ""))
    for name, body in (model.get("tablas") or {}).items():
        if not body["mercados"]:
            continue
        lines.append(
            f"- {name} ({', '.join(body['mercados'])}): {body['porcentual_bps']} bps por pata; mínimo "
            f"{body['minimo_por_operacion_usd']} USD; gas {body['gas_por_pata_usd']} USD por pata. "
            f"Con {body['nocional_ejemplo_usd']} USD: {body['comision_ida_y_vuelta_usd']} USD ida y vuelta "
            f"(fija {body['parte_fija_por_pata_usd']} + porcentual {body['parte_porcentual_por_pata_usd']} por pata). "
            f"Fuente: {body['fuente']}"
        )
    lines.append(model.get("deslizamiento", ""))
    lines.append("")
    sim = section.get("simulacion") or {}
    lines.append("### Qué habría pasado (solo reporte)")
    lines.append(sim.get("nota", ""))
    for item in sim.get("umbral_mas_alto") or []:
        lines.append(
            f"- {item['estrategia']} umbral {item['escenario']}: {item['operaciones']}/{item['operaciones_base']} ops, "
            f"bruto {item['pnl_bruto_usd']}, costos {item['costos_usd']}, neto {item['pnl_neto_usd']} USD."
        )
    for item in sim.get("ordenes_mas_grandes") or []:
        lines.append(
            f"- {item['estrategia']} órdenes ×{item['multiplicador']}: bruto {item['pnl_bruto_usd']}, "
            f"comisiones {item['comisiones_usd']}, deslizamiento {item['deslizamiento_usd']}, gas {item['gas_usd']}, "
            f"neto {item['pnl_neto_usd']} USD "
            f"(costo {item.get('costo_bps', '—')} bps)."
        )
    lines.append("")
    return lines


def _coverage_line(name: str, body: dict) -> str:
    if not body.get("operaciones"):
        return f"- {name}: sin operaciones."
    flag = "sí" if body.get("cubre_costos") else "no"
    return (
        f"- {name}: {body['operaciones']} ops, tamaño promedio {body.get('tamano_promedio_usd')} USD, "
        f"bruto promedio {body.get('bruto_promedio_usd')} USD ({body.get('bruto_bps', '—')} bps), "
        f"costo promedio {body.get('costo_promedio_usd')} USD ({body.get('costo_bps', '—')} bps). Cubre costos: {flag}."
    )


# --------------------------------------------------------------------------
# Utilidades


def _to_json(acc: dict) -> dict:
    return {key: (value if isinstance(value, int) else format(value, "f")) for key, value in acc.items()}


def _from_json(raw: dict | None) -> dict:
    acc = _blank()
    for key, value in (raw or {}).items():
        if key not in acc:
            continue
        acc[key] = int(value) if isinstance(acc[key], int) else _d(value)
    return acc


def _d(value: Any) -> Decimal:
    if value is None or value == "":
        return ZERO
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return ZERO


def _first_d(*values: Any) -> Decimal | None:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
    return None


def _s(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.0001")), "f")


def _bps(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _parse(value: Any) -> datetime:
    try:
        return as_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return datetime.min.replace(tzinfo=CDMX)


def _cdmx_day(value: Any) -> str:
    try:
        return _parse(value).astimezone(CDMX).strftime("%Y-%m-%d")
    except (OverflowError, ValueError):
        return "?"
