"""Issue #23: /decisiones/hoy con ~300 ciclos y GCS lento responde en menos de 2 s."""

import json
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import mesa_trading.reports.decisions as decisions
from mesa_trading.api.app import create_app
from mesa_trading.policy.decide import load_policy
from mesa_trading.reports.decisions import cached_decision_report, clear_decision_cache
from mesa_trading.storage import GcsRoot, LocalRoot

# Latencia de un GET a GCS en staging. En serie: 300 × 0.1 s = 30 s, más el token por objeto.
GCS_LATENCY_S = 0.1


class SlowGcs(LocalRoot):
    """Disco local que tarda como GCS en cada lectura y cuenta las llamadas."""

    def __init__(self, root, latency=GCS_LATENCY_S):
        super().__init__(root)
        self.latency = latency
        self.reads = 0
        self._mu = threading.Lock()

    def read_text(self, key):
        with self._mu:
            self.reads += 1
        time.sleep(self.latency)
        return super().read_text(key)


def _seed_day(root, now: datetime, cycles: int = 300) -> None:
    """Un objeto por ciclo de 5 min, como escribe el tick, todo dentro del día CDMX de `now`."""
    start = now - timedelta(minutes=5 * (cycles - 1))
    for index in range(cycles):
        when = start + timedelta(minutes=5 * index)
        stamp = when.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        cid = when.strftime("%Y%m%dT%H%M%SZ")
        row = {
            "event_type": "PolicyDecided",
            "occurred_at": stamp,
            "bot": "gambler",
            "correlation_id": cid,
            "payload": {"action": "hold", "reason": "umbral", "condiciones": ["setup"]},
        }
        root.write_text(f"logs/dt={when.strftime('%Y-%m-%d')}/{cid}.jsonl", json.dumps(row) + "\n")


# 23:55 en CDMX: el día ya tiene ~288 ciclos. Se usan 300 para cubrir el objetivo.
NOW = datetime(2026, 11, 19, 5, 55, tzinfo=timezone.utc)


def test_three_hundred_cycles_answer_in_under_two_seconds(tmp_path):
    clear_decision_cache()
    root = SlowGcs(tmp_path)
    _seed_day(root, NOW, cycles=288)
    began = time.perf_counter()
    report = cached_decision_report(root, NOW, load_policy(), now_mono=0)
    elapsed = time.perf_counter() - began
    assert elapsed < 2.0, elapsed
    assert report["decisiones"] == 288
    assert report["parcial"] is False
    assert report["truncado"] is False


def test_endpoint_is_fast_and_only_reads_new_cycles_after_the_cache_expires(tmp_path, monkeypatch):
    clear_decision_cache()
    root = SlowGcs(tmp_path)
    _seed_day(root, datetime.now(timezone.utc), cycles=300)
    client = TestClient(create_app(root))
    began = time.perf_counter()
    response = client.get("/decisiones/hoy")
    elapsed = time.perf_counter() - began
    assert response.status_code == 200
    assert elapsed < 2.0, elapsed
    first_reads = root.reads
    assert first_reads > 0
    # Vence el TTL: se vuelve a listar, pero los ciclos ya leídos no se bajan otra vez.
    monkeypatch.setattr(decisions, "CACHE_TTL_S", 0)
    again = client.get("/decisiones/hoy")
    assert again.json()["decisiones"] == response.json()["decisiones"]
    assert root.reads == first_reads


def test_a_slow_bucket_returns_a_partial_report_on_time_and_finishes_later(tmp_path, monkeypatch):
    clear_decision_cache()
    monkeypatch.setattr(decisions, "READ_DEADLINE_S", 0.3)
    monkeypatch.setattr(decisions, "READ_WORKERS", 4)
    root = SlowGcs(tmp_path, latency=0.1)
    _seed_day(root, NOW, cycles=60)
    policy = load_policy()
    began = time.perf_counter()
    first = cached_decision_report(root, NOW, policy, now_mono=0)
    assert time.perf_counter() - began < 1.0
    assert first["parcial"] is True
    assert 0 < first["decisiones"] < 60
    # Un parcial no se guarda: las siguientes peticiones siguen desde lo ya leído.
    report = first
    for step in range(1, 20):
        report = cached_decision_report(root, NOW, policy, now_mono=step)
        if not report["parcial"]:
            break
    assert report["parcial"] is False
    assert report["decisiones"] == 60


def test_gcs_root_reuses_the_metadata_token():
    calls = []

    def provider():
        calls.append(1)
        return "tok"

    clock = [0.0]
    root = GcsRoot("bucket", token_provider=provider, clock=lambda: clock[0])
    assert root._bearer() == "tok"
    assert root._bearer() == "tok"
    assert len(calls) == 1
    clock[0] = GcsRoot.TOKEN_TTL_S + 1
    root._bearer()
    assert len(calls) == 2
