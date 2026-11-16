"""Conteos del día para ver por qué no hay entradas. Solo agregados, sin respuestas."""

from __future__ import annotations

import json
import threading
import time
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
CACHE_TTL_S = 300
# Un fallo de lectura no se queda cinco minutos: se reintenta pronto.
ERROR_TTL_S = 30
# (store, fecha) -> (monotonic, reporte). Un golpe público no vuelve a listar el bucket.
_CACHE: dict[tuple, tuple[float, dict]] = {}
_LOCKS: dict[tuple, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def clear_decision_cache() -> None:
    _CACHE.clear()
    with _LOCKS_GUARD:
        _LOCKS.clear()


def _lock_for(key: tuple) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _ttl_for(found: tuple[float, dict] | None) -> float:
    if found is not None and found[1].get("lectura") == "no_disponible":
        return ERROR_TTL_S
    return CACHE_TTL_S


def _fresh(found: tuple[float, dict] | None, clock: float) -> bool:
    return found is not None and clock - found[0] < _ttl_for(found)


def _purge_other_days(today: str, keep: tuple) -> None:
    for key in list(_CACHE):
        if key == keep or key[1] == today:
            continue
        _CACHE.pop(key, None)
        with _LOCKS_GUARD:
            _LOCKS.pop(key, None)


def cached_decision_report(store: DataRoot, now: datetime, policy: dict, *, now_mono: float | None = None) -> dict:
    """Repite el JSON en memoria unos minutos. El acierto no lista ni lee objetos.

    Si el TTL venció, un solo hilo lee; los demás esperan ese resultado.
    """
    clock = time.monotonic() if now_mono is None else now_mono
    day = _cdmx_day(now)
    key = (id(store), day)
    found = _CACHE.get(key)
    if _fresh(found, clock):
        return found[1]
    with _lock_for(key):
        found = _CACHE.get(key)
        if _fresh(found, clock):
            return found[1]
        report = decision_report(store, now, policy)
        _CACHE[key] = (clock, report)
        _purge_other_days(day, key)
        return report


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
    truncated = False
    remaining = MAX_LOG_OBJECTS
    # El día de la Ciudad de México cabe en dos fechas UTC. Primero la más nueva.
    stamps = [
        (start + timedelta(days=offset)).astimezone(ZoneInfo("UTC")).date().isoformat()
        for offset in range(2)
    ]
    for stamp in reversed(stamps):
        if remaining <= 0:
            truncated = True
            break
        batch, cut = store.list_prefix(f"logs/dt={stamp}/", max_results=remaining)
        logs = [name for name in batch if name.endswith(".jsonl") or name.endswith(".ndjson")]
        names.extend(logs)
        remaining -= len(batch)
        if cut:
            truncated = True
            break
    rows: list[dict] = []
    for name in names[:MAX_LOG_OBJECTS]:
        text = store.read_text(name)
        if not text:
            continue
        for event in _project_log(text):
            when = _when(event)
            if when is None or when < start or when >= end:
                continue
            rows.append(event)
    return rows, truncated


def _project_log(text: str) -> list[dict]:
    """Se queda con los campos del conteo. Las respuestas solo si hacen falta para un hold viejo."""
    needed: set[str] = set()
    for event in _parse_lines(text):
        if event.get("event_type") != "PolicyDecided" or event.get("bot") != "gambler":
            continue
        payload = event.get("payload") or {}
        if str(payload.get("reason") or "") in ENTRY_REASONS and "condiciones" not in payload:
            needed.add(str(event.get("correlation_id") or ""))
    slim = []
    for event in _parse_lines(text):
        kind = event.get("event_type")
        if kind not in {"PolicyDecided", "BatteryAnswered"}:
            continue
        payload = event.get("payload") or {}
        item = {
            "event_type": kind,
            "bot": event.get("bot"),
            "correlation_id": event.get("correlation_id"),
            "occurred_at": event.get("occurred_at"),
            "payload": {},
        }
        if kind == "PolicyDecided":
            kept = {"action": payload.get("action"), "reason": payload.get("reason")}
            if "condiciones" in payload:
                kept["condiciones"] = [str(value) for value in (payload.get("condiciones") or [])]
            item["payload"] = kept
        elif str(event.get("correlation_id") or "") in needed and event.get("bot") == "gambler":
            answers = []
            for answer in payload.get("answers") or []:
                qid = str(answer.get("question_id") or "")
                if qid not in CONDITIONS:
                    continue
                answers.append({"question_id": qid, "probs": answer.get("probs"), "score": answer.get("score")})
            item["payload"] = {"answers": answers}
        slim.append(item)
    return slim


def _parse_lines(text: str):
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


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
