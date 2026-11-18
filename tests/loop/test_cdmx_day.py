"""El PnL del día y el halt cambian a medianoche de Ciudad de México, no a la de UTC."""

from datetime import datetime, timezone
from decimal import Decimal

from mesa_trading.loop.cycle import _maybe_halt, _next_midnight, _roll_day
from tests.helpers import T0, ScriptedJudge, bullish, connector_for, make_env


def test_halt_resets_at_cdmx_midnight(tmp_path):
    env = make_env(tmp_path, ScriptedJudge(bullish()), connector_for(T0), mode="simulated", markets=["BTC-PERP"])
    state, _gen = env.book.read()
    state.day_realized_pnl_usd = Decimal("-3")
    state.halted_until = datetime(2026, 11, 7, 0, 0, tzinfo=timezone.utc)
    state.halt_rule = "perdida_diaria"
    still_today = datetime(2026, 11, 7, 5, 30, tzinfo=timezone.utc)
    assert _roll_day(env, state, "c", still_today) is False
    assert state.day_realized_pnl_usd == Decimal("-3")
    assert state.halted_until is not None
    midnight = datetime(2026, 11, 7, 6, 0, tzinfo=timezone.utc)
    assert _roll_day(env, state, "c", midnight) is True
    assert state.day_start_utc == "2026-11-07"
    assert state.day_realized_pnl_usd == Decimal("0")
    assert state.halted_until is None
    assert state.halt_rule is None


def test_new_halt_lasts_until_cdmx_midnight(tmp_path):
    env = make_env(tmp_path, ScriptedJudge(bullish()), connector_for(T0), mode="simulated", markets=["BTC-PERP"])
    state, _gen = env.book.read()
    state.cash_usd = Decimal("940")
    evening = datetime(2026, 11, 6, 23, 30, tzinfo=timezone.utc)
    _maybe_halt(env, state, "c", evening)
    assert state.halt_rule == "perdida_diaria"
    assert state.halted_until == _next_midnight(evening)
    assert state.halted_until == datetime(2026, 11, 7, 6, 0, tzinfo=timezone.utc)
