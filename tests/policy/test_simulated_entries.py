"""Con la curva identidad de staging, unas respuestas como las del 16 nov sí operan.

Sin curva, el simulado se queda en hold (sin_calibracion_aprobada) y el reporte
cuenta la decisión pero no hay orden. Las probabilidades de abajo son los centros
de las cubetas publicadas en /reportes/calibracion/2026-11-16.json.
"""

from decimal import Decimal

from mesa_trading.constants import PINNED_MODEL
from mesa_trading.loop.cycle import run_cycle
from mesa_trading.policy.decide import apply_calibration, curves_from_policy, decide, load_policy, policy_counts_as_evidence
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


def test_identity_curve_only_applies_in_simulated():
    probs = {"si": Decimal("0.6"), "no": Decimal("0.4")}
    identity = {"a": "1", "b": "0", "aprobada": True, "fuente": "no autoriza dinero real"}
    assert apply_calibration(probs, curve=identity, mode="real") is None
    assert apply_calibration(probs, curve=identity, mode="live") is None
    assert apply_calibration(probs, curve=identity, mode="") is None
    assert policy_counts_as_evidence(load_policy()) is True  # v3 cuenta (issue #33)
    shadowed = apply_calibration(probs, curve={"a": "2", "b": "0", "aprobada": True, "fuente": "x"}, mode="shadow")
    assert shadowed is not None
    assert shadowed["si"] == Decimal("0.6")
    assert apply_calibration(probs, curve={"a": "1", "b": "0"}, mode="simulated") is not None
    assert apply_calibration(probs, curve={"a": "1.2", "b": "-0.1"}, mode="simulated") is None
    approved = {"a": "1.2", "b": "-0.1", "aprobada": True, "fuente": "informe interno"}
    assert apply_calibration(probs, curve=approved, mode="simulated") is not None
    v2_marked = {"version": 2, "cuenta_como_evidencia": True, "curvas": {"gambler.regimen": {"a": "1.2", "b": "-0.1", "aprobada": True, "fuente": "informe"}}}
    assert policy_counts_as_evidence(v2_marked) is False
    assert policy_counts_as_evidence({"version": 1, "cuenta_como_evidencia": True, "curvas": v2_marked["curvas"]}) is False
    approved_policy = {
        "version": 4,
        "cuenta_como_evidencia": True,
        "curvas": {"gambler.regimen": {"a": "1.2", "b": "-0.1", "aprobada": True, "fuente": "informe"}},
    }
    assert policy_counts_as_evidence(approved_policy) is True
    # La v3 cuenta sin depender del flag ni de las curvas; v2 y anteriores nunca.
    assert policy_counts_as_evidence({"version": 3}) is True
    assert policy_counts_as_evidence({"version": 3, "cuenta_como_evidencia": False, "curvas": {}}) is True
    assert policy_counts_as_evidence({"version": 2, "cuenta_como_evidencia": True, "curvas": {}}) is False
    assert policy_counts_as_evidence({"version": 0}) is False
    assert policy_counts_as_evidence({"version": "no"}) is False
    # Las versiones nuevas siguen exigiendo flag y curvas aprobadas.
    assert policy_counts_as_evidence({"version": 4, "curvas": approved_policy["curvas"]}) is False
    assert policy_counts_as_evidence({"version": 4, "cuenta_como_evidencia": True, "curvas": {}}) is False
    assert policy_counts_as_evidence(None) is False


def test_house_quotes_a_limit_at_a_quarter_of_capital(tmp_path):
    """House sigue pudiendo cotizar. Es una límite al 25%; sin vela que cruce, no llena."""
    answers = {
        "estado_rango": {"probs": {"dentro": "0.8", "borde": "0.1", "rompiendo": "0.05", "otro": "0.05"}},
        "flujo_toxico": {"probs": {"si": "0.1", "no": "0.9"}},
        "vale_cotizar": {"probs": {"0": "0", "1": "0", "2": "0.6", "3": "0.4"}},
        "hay_volumen": {"probs": {"si": "0.9", "no": "0.1"}},
    }
    policy = load_policy()
    quote = decide(
        "house",
        "WETH-USDC",
        "spot",
        answers,
        capital_usd=Decimal("1000"),
        mode="simulated",
        policy=policy,
        curves=curves_from_policy(policy),
    )
    assert quote.action == "quote"
    assert quote.is_entry is True
    assert quote.notional_usd == Decimal("250.00")
    connector = connector_for(T0, price="1000")
    connector._candles["WETH-USDC"] = list(connector._candles["BTC-PERP"])
    connector._books["WETH-USDC"] = dict(connector._books["BTC-PERP"])
    env = make_env(tmp_path, QueueJudge([answers]), connector, mode="simulated", markets=["BTC-PERP"])
    env.config.bots["gambler"].enabled = False
    env.config.bots["house"].enabled = True
    env.config.bots["house"].mode = "simulated"
    env.config.bots["house"].markets = ["WETH-USDC"]
    env.policy = policy
    env.curves = curves_from_policy(policy)
    run_cycle(env, "house-1", T0)
    submitted = [event for event in env.logs.all_events() if event["event_type"] == "OrderSubmitted"]
    assert len(submitted) == 1
    payload = submitted[0]["payload"]
    assert payload["action"] == "quote"
    assert payload["order_type"] == "limit"
    assert payload["notional_usd"] == "250.00"
    assert not any(event["event_type"] == "PositionOpened" for event in env.logs.all_events())


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


def test_a_day_of_recorded_answers_opens_eight_round_trips(tmp_path):
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
    assert len(opened) == 8
    assert len(closed) == len(opened)
    assert vetoes == []
    assert all(event["payload"]["price_source"] == "libro" for event in env.logs.all_events() if event["event_type"] == "OrderFilled")
