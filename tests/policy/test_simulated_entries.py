"""Con la curva identidad de staging, unas respuestas como las del 16 nov sí operan.

Sin curva, el simulado se queda en hold (sin_calibracion_aprobada) y el reporte
cuenta la decisión pero no hay orden. Las probabilidades de abajo son los centros
de las cubetas publicadas en /reportes/calibracion/2026-11-16.json.
"""

from decimal import Decimal

from mesa_trading.constants import PINNED_MODEL
from mesa_trading.loop.cycle import run_cycle
from mesa_trading.policy.decide import curves_from_policy, decide, load_policy
from tests.helpers import T0, connector_for, make_env


# Cubeta 0.6-0.7 de accion, n=1309 de 6173. regimen 0.6-0.7, n=673.
# ejecucion_degradada 0.1-0.2, n=5883. setup arma un score 1.8 (umbral 1.5).
STRONG = {
    "regimen": {"probs": {"tendencia": "0.65", "reversion": "0.15", "caotico": "0.10", "otro": "0.10"}},
    "setup": {"probs": {"0": "0.10", "1": "0.25", "2": "0.40", "3": "0.25"}},
    "accion": {"probs": {"largo": "0.64", "corto": "0.20", "hold": "0.16"}},
    "ejecucion_degradada": {"probs": {"si": "0.15", "no": "0.85"}},
    "presion_compra": {"probs": {"si": "0.55", "no": "0.45"}},
}
# Cubeta grande de regimen 0.4-0.5 (n=2427) y accion cuyo tope es hold.
WEAK = {
    "regimen": {"probs": {"tendencia": "0.44", "reversion": "0.30", "caotico": "0.16", "otro": "0.10"}},
    "setup": {"probs": {"0": "0.20", "1": "0.45", "2": "0.25", "3": "0.10"}},
    "accion": {"probs": {"largo": "0.36", "corto": "0.20", "hold": "0.44"}},
    "ejecucion_degradada": {"probs": {"si": "0.15", "no": "0.85"}},
    "presion_compra": {"probs": {"si": "0.48", "no": "0.52"}},
}


def _battery(answers: dict) -> dict:
    return {
        "status": "answered",
        "model_version": PINNED_MODEL,
        "input_tokens": 450,
        "latency_ms": 20,
        "answers": [
            {"question_id": qid, "probs": {key: float(value) for key, value in body["probs"].items()}}
            for qid, body in answers.items()
        ],
    }


class QueueJudge:
    def __init__(self, answers_for_cycle: list[dict]) -> None:
        self._items = [_battery(item) for item in answers_for_cycle]
        self.calls = 0

    def ask(self, cycle_id: str, bot: str, snapshot: dict) -> dict:
        _ = (cycle_id, bot, snapshot)
        item = self._items[self.calls]
        self.calls += 1
        return item


def test_simulated_without_a_curve_holds():
    policy = load_policy()
    held = decide(
        "gambler",
        "BTC-PERP",
        "perp",
        STRONG,
        capital_usd=Decimal("1000"),
        mode="simulated",
        policy=policy,
        curves={},
    )
    assert held.action == "hold"
    assert held.reason == "sin_calibracion_aprobada"
    assert held.is_entry is False


def test_recorded_strong_answer_enters_and_weak_one_does_not():
    policy = load_policy()
    curves = curves_from_policy(policy)
    assert "gambler.presion_compra" in curves
    entered = decide(
        "gambler",
        "BTC-PERP",
        "perp",
        STRONG,
        capital_usd=Decimal("1000"),
        mode="simulated",
        policy=policy,
        curves=curves,
    )
    assert entered.is_entry is True
    assert entered.action == "long"
    assert entered.notional_usd == Decimal("250.00")
    weak = decide(
        "gambler",
        "BTC-PERP",
        "perp",
        WEAK,
        capital_usd=Decimal("1000"),
        mode="simulated",
        policy=policy,
        curves=curves,
    )
    assert weak.action == "hold"


def test_a_day_of_recorded_answers_opens_five_to_ten_round_trips(tmp_path):
    """288 ciclos, una sola posición. Una respuesta fuerte cada 40 ciclos cierra al siguiente."""
    cycles = 288
    # Cada entrada fuerte va seguida de un ciclo débil que cierra. La última
    # no puede caer en el ciclo 287, porque no habría con qué cerrarla.
    schedule = [STRONG if index % 40 == 0 and index + 1 < cycles else WEAK for index in range(cycles)]
    judge = QueueJudge(schedule)
    env = make_env(tmp_path, judge, connector_for(T0), mode="simulated", markets=["BTC-PERP"])
    env.policy = load_policy()
    env.curves = curves_from_policy(env.policy)
    for index in range(cycles):
        # Mismo instante que las velas grabadas. Si el reloj avanza, el filtro
        # marca datos_viejos y el resto del día no vuelve a evaluar.
        run_cycle(env, f"c{index:04d}", T0)
    opened = [event for event in env.logs.all_events() if event["event_type"] == "PositionOpened"]
    closed = [event for event in env.logs.all_events() if event["event_type"] == "PositionClosed"]
    vetoes = [event for event in env.logs.all_events() if event["event_type"] == "RiskVetoed"]
    assert 5 <= len(opened) <= 10
    assert len(closed) == len(opened)
    assert vetoes == []
    assert all(event["payload"]["price_source"] == "libro" for event in env.logs.all_events() if event["event_type"] == "OrderFilled")
