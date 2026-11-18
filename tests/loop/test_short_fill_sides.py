"""El fill de un short dice long/short y buy/sell, y el cierre resta las comisiones."""

from datetime import timedelta
from decimal import Decimal

from mesa_trading.constants import PINNED_MODEL
from mesa_trading.loop.cycle import run_cycle
from tests.helpers import T0, ScriptedJudge, connector_for, make_env


def _answers(accion: dict, trend: str = "0.9") -> dict:
    return {
        "status": "answered",
        "model_version": PINNED_MODEL,
        "input_tokens": 120,
        "latency_ms": 15,
        "answers": [
            {"question_id": "regimen", "probs": {"tendencia": trend, "reversion": "0.04", "caotico": "0.03", "otro": "0.03"}},
            {"question_id": "presion_compra", "probs": {"si": "0.2", "no": "0.8"}},
            {"question_id": "setup", "probs": {"0": "0", "1": "0", "2": "0.2", "3": "0.8"}, "score": "2.8"},
            {"question_id": "accion", "probs": accion},
            {"question_id": "ejecucion_degradada", "probs": {"si": "0.05", "no": "0.95"}},
        ],
    }


def test_short_open_and_close_keep_both_sides(tmp_path):
    judge = ScriptedJudge(_answers({"largo": "0.1", "corto": "0.85", "hold": "0.05"}))
    env = make_env(tmp_path, judge, connector_for(T0, price="100"), mode="simulated", markets=["BTC-PERP"])
    run_cycle(env, "20261106T120000Z", T0)
    state, _gen = env.book.read()
    opened = state.recent_fills[-1]
    assert opened["side"] == "short"
    assert opened["order_side"] == "sell"
    assert state.position is not None
    entry = state.position.entry_price
    open_fee = state.position.fees_usd
    open_slip = state.position.slippage_usd
    qty = state.position.qty

    judge.response = _answers({"largo": "0.1", "corto": "0.1", "hold": "0.8"}, trend="0.1")
    later = T0 + timedelta(minutes=5)
    run_cycle(env, "20261106T120500Z", later)
    state, _gen = env.book.read()
    closed = state.recent_fills[-1]
    assert closed["action"] == "close"
    assert closed["side"] == "short"
    assert closed["order_side"] == "buy"
    assert state.position is None
    fill = [event for event in env.logs.all_events() if event["event_type"] == "OrderFilled"][-1]["payload"]
    assert fill["side"] == "short"
    assert fill["order_side"] == "buy"
    gross = (entry - Decimal(fill["price"])) * qty
    expected = gross - Decimal(fill["fee_usd"]) - Decimal(fill["slippage_usd"]) - open_fee - open_slip
    assert Decimal(fill["pnl_usd"]) == expected
    assert Decimal(closed["pnl_usd"]) == expected
    assert state.realized_pnl_usd == expected
