"""Umbrales en código. La probabilidad no cambia el tamaño de la posición."""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

import yaml

from mesa_trading.config import default_policy_path
from mesa_trading.models import Proposal


# La version 2 es el periodo de staging con curva identidad. No cuenta como
# evidencia de edge aunque un evento la marque cuenta_como_evidencia.
IDENTITY_POLICY_VERSION = 2


def load_policy(path: Path | None = None) -> dict[str, Any]:
    target = path or default_policy_path()
    return yaml.safe_load(target.read_text(encoding="utf-8"))


def policy_counts_as_evidence(policy: dict[str, Any] | None) -> bool:
    """Una version cuenta para el GO solo si no es la identidad y todas sus curvas están aprobadas."""
    if not policy:
        return False
    try:
        version = int(policy.get("version") or 0)
    except (TypeError, ValueError):
        return False
    if version == 0 or version == IDENTITY_POLICY_VERSION:
        return False
    if policy.get("cuenta_como_evidencia") is not True:
        return False
    curves = policy.get("curvas") or {}
    if not isinstance(curves, dict) or not curves:
        return False
    for body in curves.values():
        if not isinstance(body, dict) or "a" not in body or "b" not in body:
            return False
        if _identity_curve(body):
            return False
        if body.get("aprobada") is not True or not str(body.get("fuente") or "").strip():
            return False
    return True


def curves_from_policy(policy: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Curvas declaradas en la política. Vacío = el simulado no opera."""
    raw = (policy or {}).get("curvas") or {}
    out = {}
    for key, body in raw.items():
        if not isinstance(body, dict) or "a" not in body or "b" not in body:
            continue
        item: dict[str, Any] = {"a": str(body["a"]), "b": str(body["b"])}
        if "aprobada" in body:
            item["aprobada"] = bool(body["aprobada"])
        if str(body.get("fuente") or "").strip():
            item["fuente"] = str(body["fuente"]).strip()
        out[str(key)] = item
    return out


def _identity_curve(curve: dict) -> bool:
    return Decimal(str(curve.get("a"))) == Decimal("1") and Decimal(str(curve.get("b"))) == Decimal("0")


def _curve_allowed(curve: dict | None, mode: str) -> bool:
    """La identidad solo vale en simulado. Otro modo exige aprobada y fuente, y no puede ser identidad."""
    if not curve or "a" not in curve or "b" not in curve:
        return False
    identity = _identity_curve(curve)
    if identity:
        return mode == "simulated"
    return curve.get("aprobada") is True and bool(str(curve.get("fuente") or "").strip())


def platt(probability: Decimal, a: Decimal, b: Decimal) -> Decimal:
    clipped = min(Decimal("0.999"), max(Decimal("0.001"), probability))
    logit = (clipped / (Decimal("1") - clipped)).ln()
    z = a * logit + b
    return Decimal("1") / (Decimal("1") + (-z).exp())


def apply_calibration(
    probs: dict[str, Decimal],
    *,
    curve: dict | None,
    mode: str,
) -> dict[str, Decimal] | None:
    """La sombra usa la probabilidad cruda. La identidad solo se aplica en simulado."""
    if mode == "shadow":
        return dict(probs)
    if not _curve_allowed(curve, mode):
        return None
    assert curve is not None
    a = Decimal(str(curve["a"]))
    b = Decimal(str(curve["b"]))
    adjusted = {key: platt(value, a, b) for key, value in probs.items()}
    total = sum(adjusted.values())
    if total <= 0:
        return None
    return {key: value / total for key, value in adjusted.items()}


def decide(
    bot: str,
    market: str,
    kind: str,
    answers: dict[str, dict],
    *,
    capital_usd: Decimal,
    mode: str,
    policy: dict[str, Any] | None = None,
    curves: dict[str, dict] | None = None,
    managing: bool = False,
) -> Proposal:
    rules = (policy or load_policy())[bot]
    version = int((policy or load_policy()).get("version", 1))
    curves = curves or {}
    usable = _usable_answers(bot, answers, mode, curves)
    if usable is None:
        return _hold(bot, market, kind, version, rules, "sin_calibracion_aprobada")
    if bot == "gambler":
        return _gambler(market, kind, usable, capital_usd, rules, version, managing)
    if bot == "house":
        return _house(market, kind, usable, capital_usd, rules, version, managing)
    return _hold(bot, market, kind, version, rules, "bot_sin_politica_activa")


def _usable_answers(bot, answers, mode, curves):
    out = {}
    for qid, answer in answers.items():
        probs = {key: Decimal(str(value)) for key, value in (answer.get("probs") or {}).items()}
        calibrated = apply_calibration(probs, curve=curves.get(f"{bot}.{qid}"), mode=mode)
        if calibrated is None and mode != "shadow":
            return None
        out[qid] = {**answer, "probs": calibrated if calibrated is not None else probs}
    return out


def _p(answer: dict, option: str) -> Decimal | None:
    probs = answer.get("probs") or {}
    if option not in probs:
        return None
    return Decimal(str(probs[option]))


def _score(answer: dict) -> Decimal | None:
    if answer.get("score") is not None:
        return Decimal(str(answer["score"]))
    probs = answer.get("probs") or {}
    if not probs:
        return None
    total = Decimal("0")
    for key, value in probs.items():
        total += Decimal(str(key)) * Decimal(str(value))
    return total


def _size(capital: Decimal, pct: Decimal) -> Decimal:
    # Hacia abajo: redondear al centavo de arriba deja el nocional por encima
    # del 25% y el veto de posicion_maxima bloquea todo lo que sigue a la primera pérdida.
    return (capital * pct).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def _hold(bot, market, kind, version, rules, reason) -> Proposal:
    return Proposal(
        bot=bot,
        market=market,
        action="hold",
        kind=kind,  # type: ignore[arg-type]
        threshold_version=version,
        thresholds=dict(rules),
        reason=reason,
        is_entry=False,
    )


def _gambler(market, kind, answers, capital, rules, version, managing) -> Proposal:
    needed = ("regimen", "setup", "accion", "ejecucion_degradada")
    if any(qid not in answers for qid in needed):
        return _hold("gambler", market, kind, version, rules, "respuesta_incompleta")
    if any(_missing_fields(answers[qid]) for qid in needed):
        return _hold("gambler", market, kind, version, rules, "dato_faltante")
    p_trend = _p(answers["regimen"], "tendencia")
    p_action = answers["accion"]["probs"]
    top = max(p_action, key=p_action.get)
    setup = _score(answers["setup"])
    p_bad = _p(answers["ejecucion_degradada"], "si")
    common = dict(bot="gambler", market=market, kind=kind, threshold_version=version, thresholds=dict(rules))
    if p_trend is None or setup is None or p_bad is None:
        return _hold("gambler", market, kind, version, rules, "dato_faltante")
    failed = gambler_blockers(answers, rules) or []
    exit_now = (
        p_trend < Decimal(str(rules["regimen_tendencia_min_p"]))
        or top == "hold"
        or p_bad >= Decimal(str(rules["ejecucion_degradada_max_p"]))
    )
    if managing:
        action = "close" if exit_now else "hold"
        return Proposal(**common, action=action, reason="salida" if action == "close" else "mantener", is_entry=False)
    if failed:
        return Proposal(**common, action="hold", reason="umbral", condiciones=failed, is_entry=False)
    if top == "corto" and kind == "spot":
        return Proposal(**common, action="hold", reason="corto_en_spot", is_entry=False)
    action = "short" if top == "corto" else "long"
    return Proposal(
        **common,
        action=action,
        notional_usd=_size(capital, Decimal(str(rules["position_size_pct"]))),
        reason="entrada",
        is_entry=True,
    )


def _house(market, kind, answers, capital, rules, version, managing) -> Proposal:
    needed = ("estado_rango", "flujo_toxico", "vale_cotizar", "hay_volumen")
    if any(qid not in answers for qid in needed):
        return _hold("house", market, kind, version, rules, "respuesta_incompleta")
    p_break = _p(answers["estado_rango"], "rompiendo")
    p_toxic = _p(answers["flujo_toxico"], "si")
    worth = _score(answers["vale_cotizar"])
    p_vol = _p(answers["hay_volumen"], "si")
    common = dict(bot="house", market=market, kind=kind, threshold_version=version, thresholds=dict(rules))
    if None in (p_break, p_toxic, worth, p_vol):
        return _hold("house", market, kind, version, rules, "dato_faltante")
    if p_break >= Decimal(str(rules["ruptura_min_p"])):
        return Proposal(**common, action="close" if managing else "pause", reason="ruptura", is_entry=False)
    if p_vol < Decimal(str(rules["volumen_min_p"])):
        return Proposal(**common, action="close" if managing else "pause", reason="sin_volumen", is_entry=False)
    if p_toxic >= Decimal(str(rules["toxico_max_p"])) or worth < Decimal(str(rules["vale_cotizar_min"])):
        return Proposal(**common, action="pause", reason="toxico_o_no_conviene", is_entry=False)
    if managing:
        return Proposal(**common, action="hold", reason="mantener_grid", is_entry=False)
    return Proposal(
        **common,
        action="quote",
        notional_usd=_size(capital, Decimal(str(rules["position_size_pct"]))),
        reason="cotizar",
        is_entry=True,
    )


def gambler_blockers(answers: dict, rules: dict) -> list[str] | None:
    """Condiciones de entrada que no pasan. None si falta un dato para evaluarlas."""
    needed = ("regimen", "setup", "accion", "ejecucion_degradada")
    if any(qid not in answers for qid in needed):
        return None
    p_trend = _p(answers["regimen"], "tendencia")
    probs = (answers.get("accion") or {}).get("probs") or {}
    if not probs:
        return None
    top = max(probs, key=probs.get)
    p_top = Decimal(str(probs[top]))
    setup = _score(answers["setup"])
    p_bad = _p(answers["ejecucion_degradada"], "si")
    if p_trend is None or setup is None or p_bad is None:
        return None
    failed = []
    if p_trend < Decimal(str(rules["regimen_tendencia_min_p"])):
        failed.append("regimen")
    if setup < Decimal(str(rules["setup_min"])):
        failed.append("setup")
    if top == "hold" or p_top < Decimal(str(rules["accion_min_p"])):
        failed.append("accion")
    if p_bad >= Decimal(str(rules["ejecucion_degradada_max_p"])):
        failed.append("ejecucion_degradada")
    return failed


def _missing_fields(answer: dict) -> bool:
    return bool(answer.get("unknown"))
