"""El PnL del día y el halt cambian a medianoche de Ciudad de México, no a la de UTC."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from mesa_trading.book.store import FileBookStore
from mesa_trading.loop.cycle import _cdmx_day, _maybe_halt, _next_midnight, _roll_day, run_cycle
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
    assert state.day_start_cdmx == "2026-11-07"
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


def test_legacy_utc_day_does_not_roll_backward(tmp_path):
    env = make_env(tmp_path, ScriptedJudge(bullish()), connector_for(T0), mode="simulated", markets=["BTC-PERP"])
    state, _gen = env.book.read()
    state.day_start_cdmx = "2026-11-18"
    state.day_start_equity_usd = Decimal("1000")
    state.day_realized_pnl_usd = Decimal("-4.50")
    state.halted_until = datetime(2026, 11, 18, 0, 0, tzinfo=timezone.utc)
    state.halt_rule = "perdida_diaria"
    evening = datetime(2026, 11, 18, 0, 40, tzinfo=timezone.utc)
    assert _cdmx_day(evening) == "2026-11-17"
    assert _roll_day(env, state, "c", evening) is False
    assert state.day_start_cdmx == "2026-11-18"
    assert state.day_start_equity_usd == Decimal("1000")
    assert state.day_realized_pnl_usd == Decimal("-4.50")
    assert state.halt_rule == "perdida_diaria"
    assert state.halted_until == datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)
    caught_up = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)
    assert _roll_day(env, state, "c", caught_up) is False
    assert state.day_realized_pnl_usd == Decimal("-4.50")
    nxt = datetime(2026, 11, 19, 6, 0, tzinfo=timezone.utc)
    assert _roll_day(env, state, "c", nxt) is True
    assert state.day_start_cdmx == "2026-11-19"
    assert state.day_realized_pnl_usd == Decimal("0")
    assert state.halted_until is None


def test_old_state_json_reads_day_start_utc(tmp_path):
    env = make_env(tmp_path, ScriptedJudge(bullish()), connector_for(T0), mode="simulated", markets=["BTC-PERP"])
    path = tmp_path / "book" / "state.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["state"]["day_start_utc"] = payload["state"].pop("day_start_cdmx")
    path.write_text(json.dumps(payload), encoding="utf-8")
    state, generation = FileBookStore(tmp_path / "book").read()
    assert state.day_start_cdmx == "2026-11-06"
    assert generation == 0
    env.book.write(state, generation)
    saved = json.loads(path.read_text(encoding="utf-8"))["state"]
    assert saved["day_start_cdmx"] == "2026-11-06"
    assert "day_start_utc" not in saved


def test_midnight_follows_zoneinfo_across_dst():
    zone = ZoneInfo("America/Mexico_City")
    for stamp in (
        datetime(2022, 7, 15, 4, 30, tzinfo=timezone.utc),
        datetime(2026, 1, 15, 5, 30, tzinfo=timezone.utc),
    ):
        local = stamp.astimezone(zone)
        assert _cdmx_day(stamp) == local.date().isoformat()
        nxt = _next_midnight(stamp).astimezone(zone)
        assert (nxt.hour, nxt.minute, nxt.second) == (0, 0, 0)
        assert nxt.date() == local.date() + timedelta(days=1)


def test_open_position_marks_to_the_tick_not_only_the_fill(tmp_path):
    connector = connector_for(T0, price="100")
    env = make_env(tmp_path, ScriptedJudge(bullish()), connector, mode="simulated", markets=["BTC-PERP"])
    run_cycle(env, "20261106T120000Z", T0)
    state, _gen = env.book.read()
    assert state.position is not None
    entry = state.position.entry_price
    later = T0 + timedelta(minutes=5)
    for symbol in ("BTC-PERP", "BTC"):
        book = connector._books[symbol]
        book["bid"] = "109"
        book["ask"] = "111"
        book["as_of"] = later
    run_cycle(env, "20261106T120500Z", later)
    state, _gen = env.book.read()
    assert state.position is not None
    assert state.closed_trades == 0
    assert len(state.recent_fills) == 1
    assert Decimal(state.last_prices["BTC-PERP"]) == Decimal("110")
    assert Decimal(state.last_prices["BTC-PERP"]) != entry
