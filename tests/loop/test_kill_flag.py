"""book/kill aplana en simulado. Un 412 no da el ciclo por terminado."""

from datetime import timedelta

from mesa_trading.loop.cycle import run_cycle
from tests.helpers import T0, ScriptedJudge, bullish, connector_for, make_env


def test_kill_file_flattens_on_the_next_cycle(tmp_path):
    env = make_env(tmp_path, ScriptedJudge(bullish()), connector_for(T0, price="100"), mode="simulated", markets=["BTC-PERP"])
    run_cycle(env, "20261106T120000Z", T0)
    state, _gen = env.book.read()
    assert state.position is not None
    (tmp_path / "book" / "kill").write_text("1\n", encoding="utf-8")
    later = T0 + timedelta(minutes=5)
    result = run_cycle(env, "20261106T120500Z", later)
    state, _gen = env.book.read()
    assert result["halted"] is True
    assert state.position is None
    assert state.resting_order is None
    assert state.kill_latched is True
    assert state.halt_rule == "kill_switch"
    assert state.sent_orders


def test_rejected_write_skips_cycle_finished(tmp_path):
    env = make_env(tmp_path, ScriptedJudge(bullish()), connector_for(T0, price="100"), mode="simulated", markets=["BTC-PERP"])
    env.book.write = lambda *_args, **_kwargs: False  # type: ignore[method-assign]
    result = run_cycle(env, "20261106T120000Z", T0)
    assert result["book_rejected"] is True
    assert not any(event["event_type"] == "CycleFinished" for event in env.logs.all_events())
