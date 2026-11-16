from datetime import datetime, timedelta, timezone
from decimal import Decimal

from mesa_trading.config import load_desk, load_promotion, repo_root
from mesa_trading.reports.build import (
    PAIR_ID_CAP,
    apply_verdict,
    build_calibration,
    build_daily,
    evidence_snapshot,
    update_pair_store,
)

NOW = datetime(2026, 11, 20, 18, tzinfo=timezone.utc)


def _daily():
    events = [
        {
            "event_type": "CycleFinished",
            "occurred_at": "2026-11-20T17:00:00.000000Z",
            "bot": "",
            "mode": "",
            "payload": {
                "funnel": {"vistos": 6, "en_banca": 1, "alcanzados": {"hechos_duros": 5}},
                "funnel_reached": {"hechos_duros": 5},
            },
            "event_id": "f",
            "correlation_id": "c",
        }
    ]
    return build_daily(events, load_desk(repo_root() / "config" / "desk.yaml"), NOW, book={"realized_pnl_usd": "12.5", "drawdown_usd": "1"})


def test_verdict_waits_for_sample_and_estimates_days():
    promo = load_promotion(repo_root() / "config" / "promotion.yaml")
    calibration = build_calibration([], promo, load_desk(repo_root() / "config" / "desk.yaml"), NOW)
    started = (NOW - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    report = apply_verdict(
        _daily(),
        calibration,
        promo,
        closed_trades=40,
        started_at=started,
        capital_usd=Decimal("1000"),
        max_drawdown_usd=Decimal("1"),
        now=NOW,
    )
    verdict = report["veredicto"]
    assert verdict["estado"] == "insuficiente"
    assert verdict["operaciones_cerradas"] == 40
    assert verdict["min_operaciones"] == 200
    assert verdict["cierres_por_dia"] == 10
    assert verdict["dias_estimados_para_muestra"] == 16
    assert "no autoriza dinero real" in verdict["nota"]
    assert report["embudo"]["candidatos_vistos"] == 6
    assert report["embudo"]["en_banca"] == 1


def test_verdict_no_go_on_drawdown_and_go_when_sample_is_good():
    promo = load_promotion(repo_root() / "config" / "promotion.yaml")
    config = load_desk(repo_root() / "config" / "desk.yaml")
    events = []
    for index in range(30):
        events.append(
            {
                "event_id": f"b{index}",
                "event_type": "BatteryAnswered",
                "occurred_at": "2026-11-20T17:00:00.000000Z",
                "bot": "gambler",
                "mode": "simulated",
                "payload": {"answers": [{"question_id": "presion_compra", "probs": {"si": 0.8, "no": 0.2}}]},
            }
        )
        events.append(
            {
                "event_id": f"o{index}",
                "event_type": "OutcomeResolved",
                "occurred_at": "2026-11-20T17:30:00.000000Z",
                "bot": "gambler",
                "mode": "simulated",
                "payload": {"question_id": "presion_compra", "outcome": "si", "source_event": f"b{index}"},
            }
        )
    store = update_pair_store(None, events)
    again = update_pair_store(store, events)
    assert len(again["periodos"]["sin_version"]["pares"]["gambler"]["presion_compra"]) == 30
    calibration = build_calibration(events, promo, config, NOW, pair_store=store)
    started = (NOW - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    blocked = apply_verdict(
        _daily(),
        calibration,
        promo,
        closed_trades=200,
        started_at=started,
        capital_usd=Decimal("1000"),
        max_drawdown_usd=Decimal("200"),
        now=NOW,
    )
    assert blocked["veredicto"]["estado"] == "no_go"
    # presion_compra no es pregunta de política; sin skill de política el go no sale.
    # Forzamos skill alto en una pregunta de política para el caso go.
    calibration["preguntas"]["gambler"]["regimen"]["skill"] = 0.05
    calibration["preguntas"]["gambler"]["setup"]["skill"] = 0.05
    calibration["preguntas"]["gambler"]["accion"]["skill"] = 0.05
    calibration["preguntas"]["gambler"]["ejecucion_degradada"]["skill"] = 0.05
    opened = apply_verdict(
        _daily(),
        calibration,
        promo,
        closed_trades=200,
        started_at=started,
        capital_usd=Decimal("1000"),
        max_drawdown_usd=Decimal("10"),
        now=NOW,
        pnl_usd=Decimal("12.5"),
    )
    assert opened["veredicto"]["estado"] == "go_tentativo"


def test_pair_ids_are_capped():
    store = {"ids": [f"old-{index}" for index in range(PAIR_ID_CAP)], "pares": {}}
    events = [
        {
            "event_id": "nuevo",
            "event_type": "BatteryAnswered",
            "occurred_at": "2026-11-20T17:00:00.000000Z",
            "bot": "gambler",
            "mode": "simulated",
            "payload": {"answers": [{"question_id": "presion_compra", "probs": {"si": 0.8, "no": 0.2}}]},
        },
        {
            "event_id": "out",
            "event_type": "OutcomeResolved",
            "occurred_at": "2026-11-20T17:30:00.000000Z",
            "bot": "gambler",
            "mode": "simulated",
            "payload": {"question_id": "presion_compra", "outcome": "si", "source_event": "nuevo"},
        },
    ]
    merged = update_pair_store(store, events)
    assert len(merged["ids"]) == PAIR_ID_CAP
    assert "nuevo:presion_compra" in merged["ids"]
    assert "old-0" not in merged["ids"]


def test_flat_pnl_after_the_sample_is_no_go():
    promo = load_promotion(repo_root() / "config" / "promotion.yaml")
    config = load_desk(repo_root() / "config" / "desk.yaml")
    calibration = build_calibration([], promo, config, NOW)
    for qid in ("regimen", "setup", "accion", "ejecucion_degradada"):
        calibration["preguntas"]["gambler"][qid]["skill"] = 0.0
    started = (NOW - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    daily = _daily()
    daily["pnl_acumulado_usd"] = "0"
    report = apply_verdict(
        daily,
        calibration,
        promo,
        closed_trades=220,
        started_at=started,
        capital_usd=Decimal("1000"),
        max_drawdown_usd=Decimal("0"),
        now=NOW,
        pnl_usd=Decimal("0"),
    )
    assert report["veredicto"]["estado"] == "no_go"


def _resolved(index: str, version: int, flag: bool, prob: float, outcome: str, when: str) -> list[dict]:
    return [
        {
            "event_id": f"b{index}",
            "event_type": "BatteryAnswered",
            "occurred_at": when,
            "bot": "gambler",
            "mode": "simulated",
            "payload": {
                "threshold_version": version,
                "cuenta_como_evidencia": flag,
                "answers": [{"question_id": "regimen", "probs": {"si": prob, "no": 1 - prob}}],
            },
        },
        {
            "event_id": f"o{index}",
            "event_type": "OutcomeResolved",
            "occurred_at": when,
            "bot": "gambler",
            "mode": "simulated",
            "payload": {"question_id": "regimen", "outcome": outcome, "source_event": f"b{index}"},
        },
    ]


def test_identity_period_does_not_count_toward_a_real_money_go():
    promo = load_promotion(repo_root() / "config" / "promotion.yaml")
    config = load_desk(repo_root() / "config" / "desk.yaml")
    when_v2 = "2026-11-01T17:00:00.000000Z"
    events = [
        {
            "event_id": "loss",
            "event_type": "PositionClosed",
            "occurred_at": when_v2,
            "bot": "gambler",
            "mode": "simulated",
            "payload": {"pnl_usd": "-500", "threshold_version": 2, "cuenta_como_evidencia": True},
        }
    ]
    for index, (prob, outcome) in enumerate([(0.8, "si"), (0.8, "si"), (0.2, "no"), (0.2, "no")]):
        events.extend(_resolved(f"v2-{index}", 2, True, prob, outcome, when_v2))
    only_identity = build_calibration(events, promo, config, NOW)
    blocked = evidence_snapshot(only_identity)
    assert only_identity["periodos"]["2"]["cuenta_como_evidencia"] is False
    tampered = only_identity["pair_store"]
    tampered["periodos"]["2"]["cuenta_como_evidencia"] = True
    reloaded = update_pair_store(tampered, [])
    assert reloaded["periodos"]["2"]["cuenta_como_evidencia"] is False
    assert only_identity["periodos"]["2"]["pnl_usd"] == "-500"
    assert Decimal(only_identity["periodos"]["2"]["drawdown_usd"]) == Decimal("500")
    assert only_identity["periodos"]["2"]["preguntas"]["gambler"]["regimen"]["n"] == 4
    assert only_identity["preguntas"]["gambler"]["regimen"]["n"] == 0
    assert blocked["cierres"] == 0
    assert blocked["pnl_usd"] == "0"
    assert blocked["drawdown_usd"] == "0"
    held = apply_verdict(
        _daily(),
        only_identity,
        promo,
        closed_trades=blocked["cierres"],
        started_at=blocked["desde"],
        capital_usd=Decimal("1000"),
        max_drawdown_usd=Decimal(blocked["drawdown_usd"]),
        now=NOW,
        pnl_usd=Decimal(blocked["pnl_usd"]),
    )
    assert held["veredicto"]["estado"] == "insuficiente"
    assert "version 2" in held["veredicto"]["nota"]

    started = (NOW - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    for index in range(200):
        events.append(
            {
                "event_id": f"c{index}",
                "event_type": "PositionClosed",
                "occurred_at": started,
                "bot": "gambler",
                "mode": "simulated",
                "payload": {"pnl_usd": "0.10", "threshold_version": 3, "cuenta_como_evidencia": True},
            }
        )
    for index, (prob, outcome) in enumerate([(0.8, "si"), (0.8, "si"), (0.2, "no"), (0.2, "no")]):
        events.extend(_resolved(f"v3-{index}", 3, True, prob, outcome, started))
    calibration = build_calibration(events, promo, config, NOW)
    snap = evidence_snapshot(calibration)
    assert snap["threshold_version"] == 3
    assert snap["cierres"] == 200
    assert Decimal(snap["pnl_usd"]) == Decimal("20.00")
    assert Decimal(snap["drawdown_usd"]) == Decimal("0")
    assert calibration["preguntas"]["gambler"]["regimen"]["n"] == 4
    opened = apply_verdict(
        _daily(),
        calibration,
        promo,
        closed_trades=snap["cierres"],
        started_at=snap["desde"],
        capital_usd=Decimal("1000"),
        max_drawdown_usd=Decimal(snap["drawdown_usd"]),
        now=NOW,
        pnl_usd=Decimal(snap["pnl_usd"]),
    )
    assert opened["veredicto"]["estado"] == "go_tentativo"
    assert opened["veredicto"]["pnl_acumulado_usd"] == "20.00"
    assert "version 3" in opened["veredicto"]["nota"]


def test_a_newer_period_that_does_not_count_blocks_the_previous_one():
    """v3 podía ser GO. v4 posterior, sin evidencia, deja el veredicto en insuficiente."""
    promo = load_promotion(repo_root() / "config" / "promotion.yaml")
    config = load_desk(repo_root() / "config" / "desk.yaml")
    started = (NOW - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    events = []
    for index in range(200):
        events.append(
            {
                "event_id": f"c{index}",
                "event_type": "PositionClosed",
                "occurred_at": started,
                "bot": "gambler",
                "mode": "simulated",
                "payload": {"pnl_usd": "0.10", "threshold_version": 3, "cuenta_como_evidencia": True},
            }
        )
    for index, (prob, outcome) in enumerate([(0.8, "si"), (0.8, "si"), (0.2, "no"), (0.2, "no")]):
        events.extend(_resolved(f"v3-{index}", 3, True, prob, outcome, started))
    events.append(
        {
            "event_id": "v4",
            "event_type": "PositionClosed",
            "occurred_at": "2026-11-20T17:00:00.000000Z",
            "bot": "gambler",
            "mode": "simulated",
            "payload": {"pnl_usd": "999", "threshold_version": 4, "cuenta_como_evidencia": False},
        }
    )
    calibration = build_calibration(events, promo, config, NOW)
    snap = evidence_snapshot(calibration)
    assert snap["threshold_version"] == 4
    assert snap["cuenta_como_evidencia"] is False
    assert snap["cierres"] == 0
    assert snap["pnl_usd"] == "0"
    assert calibration["preguntas"]["gambler"]["regimen"]["n"] == 0
    assert calibration["periodos"]["3"]["cierres"] == 200
    daily = _daily()
    daily["pnl_acumulado_usd"] = "999"
    report = apply_verdict(
        daily,
        calibration,
        promo,
        closed_trades=snap["cierres"],
        started_at=snap["desde"],
        capital_usd=Decimal("1000"),
        max_drawdown_usd=Decimal(snap["drawdown_usd"]),
        now=NOW,
    )
    assert report["veredicto"]["estado"] == "insuficiente"
    assert report["veredicto"]["operaciones_cerradas"] == 0
    assert report["veredicto"]["pnl_acumulado_usd"] == "0"
    assert report["veredicto"]["brier_skill_min"] is None
    assert "no vuelve" in report["veredicto"]["nota"]
    # La version 2 marcada a mano como evidencia tampoco se hereda de un periodo anterior.
    forced = evidence_snapshot(
        {
            "periodos": {
                "1": {
                    "threshold_version": 1,
                    "cuenta_como_evidencia": True,
                    "cierres": 200,
                    "pnl_usd": "20",
                    "desde": started,
                    "drawdown_usd": "0",
                },
                "2": {
                    "threshold_version": 2,
                    "cuenta_como_evidencia": True,
                    "cierres": 200,
                    "pnl_usd": "20",
                    "desde": started,
                    "drawdown_usd": "0",
                },
            }
        }
    )
    assert forced["threshold_version"] == 2
    assert forced["cuenta_como_evidencia"] is False
    assert forced["cierres"] == 0
