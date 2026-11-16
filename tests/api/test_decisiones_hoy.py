"""GET /decisiones/hoy cuenta el día y no devuelve respuestas ni secretos."""

import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from mesa_trading.api.app import create_app
from mesa_trading.policy.decide import gambler_blockers, load_policy
from mesa_trading.reports.decisions import (
    CACHE_TTL_S,
    MAX_LOG_OBJECTS,
    _project_log,
    cached_decision_report,
    clear_decision_cache,
    decision_report,
    summarize,
)
from mesa_trading.storage import LocalRoot
from tests.policy.test_simulated_entries import WEAK


def test_weak_recorded_answer_names_the_failed_conditions():
    blockers = gambler_blockers(WEAK, load_policy()["gambler"])
    assert blockers is not None
    assert "regimen" in blockers
    assert "setup" in blockers
    assert "accion" in blockers
    assert "ejecucion_degradada" not in blockers
    rules = load_policy()["gambler"]
    assert gambler_blockers({}, rules) is None
    assert gambler_blockers({"regimen": WEAK["regimen"]}, rules) is None
    no_trend = {**WEAK, "regimen": {"probs": {"reversion": "1"}}}
    assert gambler_blockers(no_trend, rules) is None
    empty_action = {**WEAK, "accion": {"probs": {}}}
    assert gambler_blockers(empty_action, rules) is None
    degraded = {**WEAK, "ejecucion_degradada": {"probs": {"si": "0.9", "no": "0.1"}}}
    assert "ejecucion_degradada" in (gambler_blockers(degraded, rules) or [])


def test_decisiones_hoy_counts_without_dumping_answers(tmp_path):
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    day = now.strftime("%Y-%m-%d")
    rows = [
        {
            "event_type": "PolicyDecided",
            "occurred_at": stamp,
            "bot": "gambler",
            "correlation_id": "c1",
            "payload": {"action": "hold", "reason": "umbral", "condiciones": ["regimen", "setup"]},
        },
        {
            "event_type": "PolicyDecided",
            "occurred_at": stamp,
            "bot": "gambler",
            "correlation_id": "c2",
            "payload": {"action": "long", "reason": "entrada", "condiciones": []},
        },
        {
            "event_type": "PolicyDecided",
            "occurred_at": stamp,
            "bot": "house",
            "correlation_id": "c3",
            "payload": {"action": "quote", "reason": "cotizar", "condiciones": []},
        },
        {
            "event_type": "BatteryAnswered",
            "occurred_at": stamp,
            "bot": "gambler",
            "correlation_id": "c4",
            "payload": {
                "answers": [
                    {"question_id": "regimen", "probs": {"tendencia": 0.1, "reversion": 0.9}},
                    {"question_id": "setup", "probs": {"0": 0.1, "1": 0.1, "2": 0.4, "3": 0.4}},
                    {"question_id": "accion", "probs": {"largo": 0.8, "corto": 0.1, "hold": 0.1}},
                    {"question_id": "ejecucion_degradada", "probs": {"si": 0.1, "no": 0.9}},
                ],
                "model_version": "secreto-no-sale",
            },
        },
        {
            "event_type": "PolicyDecided",
            "occurred_at": stamp,
            "bot": "gambler",
            "correlation_id": "c4",
            "payload": {"action": "hold", "reason": "umbral"},
        },
    ]
    root = LocalRoot(tmp_path)
    body = "".join(json.dumps(row) + "\n" for row in rows)
    root.write_text(f"logs/dt={day}/{day}T000000Z.jsonl", body)
    client = TestClient(create_app(root))
    response = client.get("/decisiones/hoy")
    assert response.status_code == 200
    report = response.json()
    assert report["por_bot"]["gambler"] == 3
    assert report["por_bot"]["house"] == 1
    assert report["por_accion"]["hold"] == 2
    assert report["por_accion"]["quote"] == 1
    assert report["por_razon"]["umbral"] == 2
    assert report["gambler"]["evaluadas"] == 3
    assert report["gambler"]["sin_desglose"] == 0
    assert report["gambler"]["fallos_por_condicion"]["regimen"] == 2
    assert report["gambler"]["tasa_paso"]["ejecucion_degradada"] == 1.0
    text = response.text
    assert "secreto-no-sale" not in text
    assert "tendencia" not in text
    estado = client.get("/estado")
    assert estado.status_code == 200
    assert "decisiones" not in estado.json()


def test_cached_report_does_not_touch_storage_again(tmp_path):
    clear_decision_cache()
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    day = now.strftime("%Y-%m-%d")

    class Counting(LocalRoot):
        def __init__(self, root):
            super().__init__(root)
            self.lists = 0
            self.reads = 0

        def list_prefix(self, prefix: str, max_results: int | None = None):
            self.lists += 1
            return super().list_prefix(prefix, max_results=max_results)

        def read_text(self, key: str):
            self.reads += 1
            return super().read_text(key)

    root = Counting(tmp_path)
    root.write_text(
        f"logs/dt={day}/c.jsonl",
        json.dumps(
            {
                "event_type": "PolicyDecided",
                "occurred_at": stamp,
                "bot": "house",
                "payload": {"action": "hold", "reason": "mantener", "condiciones": []},
            }
        )
        + "\n",
    )
    client = TestClient(create_app(root))
    first = client.get("/decisiones/hoy")
    assert first.status_code == 200
    assert first.json()["decisiones"] == 1
    listed, read = root.lists, root.reads
    assert listed > 0 and read > 0
    second = client.get("/decisiones/hoy")
    assert second.json() == first.json()
    assert root.lists == listed
    assert root.reads == read
    cached_decision_report(root, now, load_policy(), now_mono=10**9)
    assert root.lists > listed


def test_a_truncated_listing_is_flagged_and_stops():
    class Cap:
        def __init__(self):
            self.limits: list[int | None] = []

        def list_prefix(self, prefix: str, max_results: int | None = None):
            self.limits.append(max_results)
            return [f"{prefix}uno.jsonl"], True

        def read_text(self, key: str):
            return ""

    store = Cap()
    report = decision_report(store, datetime.now(timezone.utc), load_policy())
    assert report["truncado"] is True
    assert store.limits == [MAX_LOG_OBJECTS]


def test_unused_judge_answers_are_not_kept():
    text = "\n".join(
        [
            json.dumps(
                {
                    "event_type": "PolicyDecided",
                    "bot": "gambler",
                    "correlation_id": "c",
                    "occurred_at": "2026-11-16T18:00:00.000000Z",
                    "payload": {
                        "action": "hold",
                        "reason": "umbral",
                        "condiciones": ["regimen"],
                        "snapshot": {"secreto": "NO-CARGAR-SNAPSHOT"},
                    },
                }
            ),
            json.dumps(
                {
                    "event_type": "BatteryAnswered",
                    "bot": "gambler",
                    "correlation_id": "c",
                    "occurred_at": "2026-11-16T18:00:00.000000Z",
                    "payload": {
                        "model_version": "NO-CARGAR-MODELO",
                        "answers": [{"question_id": "regimen", "probs": {"tendencia": 0.1}}],
                    },
                }
            ),
        ]
    )
    blob = json.dumps(_project_log(text))
    assert "NO-CARGAR-SNAPSHOT" not in blob
    assert "NO-CARGAR-MODELO" not in blob
    assert "tendencia" not in blob


def test_old_decision_without_a_battery_stays_unscored():
    summary = summarize(
        [
            {
                "event_type": "PolicyDecided",
                "occurred_at": "2026-11-16T18:00:00.000000Z",
                "bot": "gambler",
                "correlation_id": "c",
                "payload": {"action": "hold", "reason": "umbral"},
            }
        ],
        load_policy()["gambler"],
    )
    assert summary["gambler"]["sin_desglose"] == 1
    assert summary["gambler"]["evaluadas"] == 0
