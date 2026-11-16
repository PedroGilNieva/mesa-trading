"""Conteos del día para ver por qué no hay entradas. Solo agregados, sin respuestas."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from mesa_trading.policy.decide import gambler_blockers
from mesa_trading.storage import DataRoot
from mesa_trading.util import as_utc

CDMX = ZoneInfo("America/Mexico_City")
CONDITIONS = ("regimen", "setup", "accion", "ejecucion_degradada")
# Razones en las que el gambler ya tuvo las cuatro respuestas y se puede medir el corte.
ENTRY_REASONS = {"umbral", "entrada", "corto_en_spot"}
MAX_LOG_OBJECTS = 400


def decision_report(store: DataRoot, now: datetime, policy: dict) -> dict:
    try:
        events, truncated = _events_for_cdmx_day(store, now)
        lectura = "ok"
    except Exception:
        events, truncated, lectura = [], False, "no_disponible"
    summary = summarize(events, (policy or {}).get("gambler") or {})
    summary["fecha"] = _cdmx_day(now)
    summary["zona"] = "America/Mexico_City"
    summary["lectura"] = lectura
    summary["truncado"] = truncated
    summary["nota"] = (
        "Solo lectura. Cuenta decisiones del día en Ciudad de México. "
        "No incluye respuestas del juez ni secretos y no cambia el modo."
    )
    return summary


def summarize(events: list[dict], gambler_rules: dict) -> dict:
    batteries: dict[tuple[str, str], dict] = {}
    decisions = []
    for event in events:
        kind = event.get("event_type")
        if kind == "BatteryAnswered":
            batteries[(str(event.get("correlation_id") or ""), str(event.get("bot") or ""))] = event
        elif kind == "PolicyDecided":
            decisions.append(event)
    por_bot: dict[str, int] = {}
    por_accion: dict[str, int] = {}
    por_razon: dict[str, int] = {}
    fallos = {name: 0 for name in CONDITIONS}
    evaluated = 0
    sin_desglose = 0
    for event in decisions:
        payload = event.get("payload") or {}
        bot = str(event.get("bot") or "?")
        por_bot[bot] = por_bot.get(bot, 0) + 1
        action = str(payload.get("action") or "?")
        reason = str(payload.get("reason") or "?")
        por_accion[action] = por_accion.get(action, 0) + 1
        por_razon[reason] = por_razon.get(reason, 0) + 1
        if bot != "gambler" or reason not in ENTRY_REASONS:
            continue
        blockers = _blockers(event, batteries, gambler_rules)
        if blockers is None:
            sin_desglose += 1
            continue
        evaluated += 1
        for name in blockers:
            if name in fallos:
                fallos[name] += 1
    tasas = {}
    if evaluated:
        tasas = {name: round((evaluated - fallos[name]) / evaluated, 4) for name in CONDITIONS}
    return {
        "decisiones": len(decisions),
        "por_bot": por_bot,
        "por_accion": por_accion,
        "por_razon": por_razon,
        "gambler": {
            "evaluadas": evaluated,
            "sin_desglose": sin_desglose,
            "fallos_por_condicion": fallos,
            "tasa_paso": tasas,
        },
    }


def _blockers(event: dict, batteries: dict[tuple[str, str], dict], rules: dict) -> list[str] | None:
    payload = event.get("payload") or {}
    if "condiciones" in payload:
        raw = payload.get("condiciones") or []
        return [str(item) for item in raw]
    battery = batteries.get((str(event.get("correlation_id") or ""), "gambler"))
    if battery is None or not rules:
        return None
    answers = {}
    for item in (battery.get("payload") or {}).get("answers") or []:
        qid = item.get("question_id")
        if qid:
            answers[str(qid)] = item
    return gambler_blockers(answers, rules)


def _events_for_cdmx_day(store: DataRoot, now: datetime) -> tuple[list[dict], bool]:
    start = as_utc(now).astimezone(CDMX).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    names: list[str] = []
    # El día de la Ciudad de México cabe en dos fechas UTC.
    for offset in range(2):
        stamp = (start + timedelta(days=offset)).astimezone(ZoneInfo("UTC")).date().isoformat()
        names.extend(store.list_prefix(f"logs/dt={stamp}/"))
    names = [name for name in names if name.endswith(".jsonl") or name.endswith(".ndjson")]
    truncated = len(names) > MAX_LOG_OBJECTS
    chosen = sorted(names)[-MAX_LOG_OBJECTS:]
    rows: list[dict] = []
    for name in chosen:
        text = store.read_text(name)
        if not text:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event_type") not in {"PolicyDecided", "BatteryAnswered"}:
                continue
            when = _when(event)
            if when is None or when < start or when >= end:
                continue
            rows.append(event)
    return rows, truncated


def _cdmx_day(now: datetime) -> str:
    return as_utc(now).astimezone(CDMX).strftime("%Y-%m-%d")


def _when(event: dict) -> datetime | None:
    raw = event.get("occurred_at")
    if not raw:
        return None
    try:
        return as_utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00"))).astimezone(CDMX)
    except ValueError:
        return None
