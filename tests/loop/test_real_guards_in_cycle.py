"""Issue #23: freno diario, conciliación y tope nocional conectados en run_cycle.

La fase real sigue apagada en config (y `load_desk` no deja encenderla). Aquí
se "ensaya" parcheando `phase_is_on`: todo corre contra SimBroker, sin red,
sin llaves y sin broker real.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import mesa_trading.real_money as real_money
from mesa_trading.broker.sim import SimBroker
from mesa_trading.loop.cycle import _next_midnight, _order_notional, _submit, run_cycle
from mesa_trading.models import Position, Proposal
from tests.helpers import T0, ScriptedJudge, bullish, connector_for, make_env


@pytest.fixture
def armed(monkeypatch):
    """La fase real 'encendida' solo dentro de la prueba."""
    monkeypatch.setattr(real_money, "phase_is_on", lambda _caps: True)


def _env(tmp_path, price="100"):
    return make_env(
        tmp_path,
        ScriptedJudge(bullish()),
        connector_for(T0, price=price),
        mode="simulated",
        markets=["BTC-PERP"],
    )


def _kinds(env) -> list[str]:
    return [event["event_type"] for event in env.logs.all_events()]


def _lose(env, usd: str) -> None:
    """Deja el libro con una pérdida realizada de `usd` en el día CDMX de T0."""
    state, generation = env.book.read()
    state.day_start_cdmx = "2026-11-06"
    state.day_start_equity_usd = Decimal("1000")
    state.cash_usd = Decimal("1000") - Decimal(usd)
    assert env.book.write(state, generation)


# 1. Freno de pérdida diaria ---------------------------------------------------


def test_daily_loss_limit_blocks_new_orders_for_the_rest_of_the_cdmx_day(tmp_path, armed):
    env = _env(tmp_path)
    _lose(env, "10")
    result = run_cycle(env, "20261106T120000Z", T0)
    assert result["halted"] is True
    assert result["orders"] == 0
    assert "OrderSubmitted" not in _kinds(env)
    state, _gen = env.book.read()
    assert state.halt_rule == "perdida_diaria_real"
    # 12:00Z = 06:00 en CDMX; el freno dura hasta la medianoche de CDMX (06:00Z del día siguiente).
    assert state.halted_until == _next_midnight(T0) == datetime(2026, 11, 7, 6, 0, tzinfo=timezone.utc)

    # Más tarde, el mismo día CDMX (23:55 local): sigue sin órdenes.
    late = datetime(2026, 11, 7, 5, 55, tzinfo=timezone.utc)
    again = run_cycle(env, "20261107T055500Z", late)
    assert again["orders"] == 0 and again["halted"] is True
    assert env.submits == []

    # Pasada la medianoche CDMX el día nuevo arranca y la mesa vuelve a operar.
    next_day = datetime(2026, 11, 7, 6, 5, tzinfo=timezone.utc)
    env.connector_for = lambda _m, c=connector_for(next_day, price="100"): c
    resumed = run_cycle(env, "20261107T060500Z", next_day)
    assert resumed.get("halted") is False
    assert resumed["orders"] == 1


def test_a_smaller_loss_does_not_stop_the_desk(tmp_path, armed):
    env = _env(tmp_path)
    _lose(env, "9.99")
    result = run_cycle(env, "20261106T120000Z", T0)
    assert result["orders"] == 1
    state, _gen = env.book.read()
    assert state.halt_rule is None


def test_unrealized_loss_flattens_and_halts(tmp_path, armed):
    env = _env(tmp_path)
    state, generation = env.book.read()
    state.day_start_cdmx = "2026-11-06"
    state.day_start_equity_usd = Decimal("1000")
    state.cash_usd = Decimal("980")
    state.position = Position(
        market="BTC-PERP",
        bot="gambler",
        side="long",
        qty=Decimal("0.2"),
        entry_price=Decimal("100"),
        notional_usd=Decimal("20"),
        opened_at=T0 - timedelta(hours=1),
        kind="perp",
    )
    # Marca a 40: equity 980 + 8 = 988, −12 USD en el día.
    state.last_prices["BTC-PERP"] = "40"
    assert env.book.write(state, generation)
    result = run_cycle(env, "20261106T120000Z", T0)
    assert result["halted"] is True
    state, _gen = env.book.read()
    assert state.position is None
    assert state.halt_rule == "perdida_diaria_real"
    assert "OrderSubmitted" not in _kinds(env)  # el aplanado va por real_money.flatten, no por una entrada


def test_paper_desk_is_unchanged_while_the_phase_is_off(tmp_path):
    env = _env(tmp_path)
    _lose(env, "15")  # sobre 10 USD, pero debajo del 5 % de la mesa de papel
    result = run_cycle(env, "20261106T120000Z", T0)
    assert result["orders"] == 1
    state, _gen = env.book.read()
    assert state.halt_rule is None
    assert state.position is not None
    assert state.position.notional_usd == Decimal("246.25")  # sin el tope de 25 USD


# 2. Conciliación al arrancar ---------------------------------------------------


def _venue(position=None) -> SimBroker:
    broker = SimBroker(last_prices={"BTC-PERP": Decimal("100")}, bid=Decimal("99.99"), ask=Decimal("100.01"))
    if position is not None:
        broker.seed_position("BTC-PERP", *position)
    return broker


def test_reconcile_fixes_a_position_the_book_did_not_know(tmp_path, armed):
    env = _env(tmp_path)
    venue = _venue(("long", Decimal("0.3"), Decimal("100")))
    env.venue_factory = lambda: venue
    result = run_cycle(env, "20261106T120000Z", T0)
    assert result["orders"] == 0
    assert "BookReconciled" in _kinds(env)
    assert venue.positions() == []  # el venue quedó plano
    state, _gen = env.book.read()
    assert state.position is None  # y el libro coincide
    assert state.halt_rule == "reconciliacion"
    assert "OrderSubmitted" not in _kinds(env)


def test_reconcile_drops_a_position_the_venue_does_not_have(tmp_path, armed):
    env = _env(tmp_path)
    state, generation = env.book.read()
    state.position = Position(
        market="BTC-PERP",
        bot="gambler",
        side="long",
        qty=Decimal("0.2"),
        entry_price=Decimal("100"),
        notional_usd=Decimal("20"),
        opened_at=T0,
        kind="perp",
    )
    state.last_prices["BTC-PERP"] = "100"
    assert env.book.write(state, generation)
    env.venue_factory = _venue
    run_cycle(env, "20261106T120000Z", T0)
    state, _gen = env.book.read()
    assert state.position is None
    assert "BookReconciled" in _kinds(env)


def test_matching_venue_is_left_alone(tmp_path, armed):
    env = _env(tmp_path)
    env.venue_factory = _venue
    result = run_cycle(env, "20261106T120000Z", T0)
    assert result["orders"] == 1
    assert "BookReconciled" not in _kinds(env)
    state, _gen = env.book.read()
    pos = state.position
    assert pos is not None
    env.venue_factory = lambda: _venue((pos.side, pos.qty, pos.entry_price))
    later = T0 + timedelta(minutes=5)
    run_cycle(env, "20261106T120500Z", later)
    assert "BookReconciled" not in _kinds(env)
    state, _gen = env.book.read()
    assert state.halt_rule is None


def test_unreadable_venue_skips_the_cycle_without_touching_the_book(tmp_path, armed):
    env = _env(tmp_path)

    class Broken:
        def positions(self):
            raise TimeoutError("venue")

    env.venue_factory = Broken
    result = run_cycle(env, "20261106T120000Z", T0)
    assert result["blocked"] == "sin_conciliar"
    assert result["orders"] == 0
    assert "ReconcileFailed" in _kinds(env)
    assert "CycleFinished" in _kinds(env)
    state, _gen = env.book.read()
    assert state.position is None and state.halt_rule is None


# 3. Tope nocional en la orden que sale ----------------------------------------


def test_order_sent_is_capped_not_just_the_proposal(tmp_path, armed):
    env = _env(tmp_path)
    run_cycle(env, "20261106T120000Z", T0)
    events = env.logs.all_events()
    decided = next(e for e in events if e["event_type"] == "PolicyDecided")
    submitted = next(e for e in events if e["event_type"] == "OrderSubmitted")
    assert Decimal(decided["payload"]["notional_usd"]) == Decimal("250.00")
    sent = Decimal(submitted["payload"]["notional_usd"])
    assert sent <= Decimal("25")
    assert "OrderCapped" in _kinds(env)
    state, _gen = env.book.read()
    assert state.position is not None
    # Lo que entró al libro es la orden recortada, con comisión y slippage dentro de 25 USD.
    assert state.position.notional_usd == sent
    assert sent + state.position.fees_usd + state.position.slippage_usd <= Decimal("25")
    assert state.position.qty * Decimal("100") <= Decimal("25")


def test_order_that_does_not_fit_is_rejected(tmp_path, armed):
    env = _env(tmp_path)
    state, _gen = env.book.read()
    state.cash_usd = Decimal("0")
    proposal = Proposal(
        bot="gambler",
        market="BTC-PERP",
        action="long",
        notional_usd=Decimal("100"),
        kind="perp",
        threshold_version=1,
        is_entry=True,
    )
    snapshot = {"last_close": "100", "spread_bps": "1", "liquidity_usd": "1000000"}
    assert _submit(env, state, proposal, snapshot, "c1", T0, "simulated") == 0
    assert env.submits == []
    vetoes = [e for e in env.logs.all_events() if e["event_type"] == "RiskVetoed"]
    assert vetoes and vetoes[-1]["payload"]["regla"] == "tope_nocional_orden"


def test_paper_cap_also_applies_to_the_order(tmp_path):
    env = _env(tmp_path)
    state, _gen = env.book.read()
    big = Proposal(
        bot="gambler",
        market="BTC-PERP",
        action="long",
        notional_usd=Decimal("900"),
        kind="perp",
        threshold_version=1,
        is_entry=True,
    )
    snapshot = {"last_close": "100", "spread_bps": "1"}
    # Mesa de papel: 25 % de 1000.
    assert _order_notional(env, state, big, snapshot, Decimal("100")) == Decimal("250.00")
    small = big.model_copy(update={"notional_usd": Decimal("40")})
    assert _order_notional(env, state, small, snapshot, Decimal("100")) == Decimal("40")
