"""Una cotización límite no se tira: espera la vela del siguiente tick."""

from datetime import timedelta
from decimal import Decimal

from mesa_trading.loop.cycle import run_cycle
from mesa_trading.models import Position, RestingOrder
from tests.helpers import T0, ScriptedJudge, bullish, connector_for, make_env


def _env(tmp_path):
    connector = connector_for(T0, price="1000")
    connector._candles["WETH-USDC"] = list(connector._candles["BTC-PERP"])
    connector._books["WETH-USDC"] = dict(connector._books["BTC-PERP"])
    judge = ScriptedJudge(bullish())
    env = make_env(tmp_path, judge, connector, mode="simulated", markets=["BTC-PERP"])
    env.config.bots["gambler"].enabled = False
    env.config.bots["house"].enabled = True
    env.config.bots["house"].mode = "simulated"
    env.config.bots["house"].markets = ["WETH-USDC"]
    return env, connector, judge


def _events(env, kind: str) -> list[dict]:
    return [event for event in env.logs.all_events() if event["event_type"] == kind]


def test_house_quote_rests_and_fills_on_the_next_tick(tmp_path):
    env, connector, judge = _env(tmp_path)
    run_cycle(env, "q1", T0)
    state, _gen = env.book.read()
    assert state.position is None
    assert state.resting_order is not None
    assert state.resting_order.action == "quote"
    assert _events(env, "OrderResting")
    assert not _events(env, "PositionOpened")
    resting_id = state.resting_order.client_order_id

    ts = T0 + timedelta(minutes=5)
    price = Decimal("1000")
    connector._candles["WETH-USDC"].append(
        {
            "ts": ts,
            "close_ts": ts + timedelta(minutes=5),
            "open": price,
            "high": price + 1,
            "low": price - Decimal("0.2"),
            "close": price,
            "volume": Decimal("30"),
        }
    )
    run_cycle(env, "q2", ts)
    state, _gen = env.book.read()
    opened = _events(env, "PositionOpened")
    assert len(opened) == 1
    assert state.position is not None
    assert state.position.bot == "house"
    assert state.resting_order is None
    fills = [event for event in _events(env, "OrderFilled") if event["payload"]["client_order_id"] == resting_id]
    assert len(fills) == 1
    assert fills[0]["payload"]["price_source"] == "cruce_limite"
    # El mismo tick, ya con la posición, solo la administra. No abre otra.
    assert judge.calls == ["house", "house"]
    assert len(_events(env, "PositionOpened")) == 1


def test_a_resting_quote_blocks_another_entry_until_it_resolves(tmp_path):
    env, _connector, judge = _env(tmp_path)
    run_cycle(env, "q1", T0)
    state, _gen = env.book.read()
    resting_id = state.resting_order.client_order_id
    env.config.bots["gambler"].enabled = True
    env.config.bots["gambler"].mode = "simulated"
    env.config.bots["gambler"].markets = ["BTC-PERP"]
    run_cycle(env, "q2", T0 + timedelta(minutes=1))
    state, _gen = env.book.read()
    assert state.position is None
    assert state.resting_order is not None
    assert state.resting_order.client_order_id == resting_id
    assert "gambler" not in judge.calls
    assert not _events(env, "PositionOpened")


def test_house_quote_expires_when_the_next_candle_does_not_cross(tmp_path):
    env, connector, _judge = _env(tmp_path)
    run_cycle(env, "q1", T0)
    state, _gen = env.book.read()
    resting_id = state.resting_order.client_order_id
    ts = T0 + timedelta(minutes=5)
    price = Decimal("1000")
    connector._candles["WETH-USDC"].append(
        {
            "ts": ts,
            "close_ts": ts + timedelta(minutes=5),
            "open": price,
            "high": price + Decimal("0.2"),
            "low": price,
            "close": price,
            "volume": Decimal("30"),
        }
    )
    env.config.bots["house"].enabled = False
    run_cycle(env, "q2", ts)
    cancelled = [
        event
        for event in _events(env, "OrderCancelled")
        if event["payload"]["client_order_id"] == resting_id and event["payload"]["reason"] == "expirada"
    ]
    assert len(cancelled) == 1
    assert not _events(env, "PositionOpened")
    state, _gen = env.book.read()
    assert state.position is None
    assert state.resting_order is None


def test_a_resting_quote_is_cancelled_when_a_position_is_already_open(tmp_path):
    env, _connector, _judge = _env(tmp_path)
    state, generation = env.book.read()
    state.position = Position(
        market="WETH-USDC",
        bot="house",
        side="long",
        qty=Decimal("0.2"),
        entry_price=Decimal("1000"),
        notional_usd=Decimal("200"),
        opened_at=T0,
        kind="spot",
    )
    state.resting_order = RestingOrder(
        client_order_id="vieja",
        cycle_id="q0",
        bot="house",
        market="WETH-USDC",
        side="buy",
        action="quote",
        qty=Decimal("0.25"),
        notional_usd=Decimal("250"),
        limit_price=Decimal("999"),
        decision_time=T0,
        expires_at=T0 + timedelta(minutes=5),
        kind="spot",
    )
    assert env.book.write(state, generation)
    run_cycle(env, "q1", T0)
    cancelled = _events(env, "OrderCancelled")
    assert any(event["payload"]["reason"] == "ya_hay_posicion" for event in cancelled)
    assert not _events(env, "OrderFilled")
    state, _gen = env.book.read()
    assert state.resting_order is None
    assert state.position is not None
    assert state.position.notional_usd == Decimal("200")
