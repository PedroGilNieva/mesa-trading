"""Issue #29: deslizamiento una sola vez, tier real del pool, gas por pata y costos por mercado.

Todo con dinero simulado: SimBroker, libro de papel y bitácora local.
"""

import json
from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from mesa_trading.api.app import create_app
from mesa_trading.book.store import close_position, fresh_book, open_position
from mesa_trading.broker.fees import FeeSchedule, commission_usd, load_fees
from mesa_trading.broker.sim import SimBroker
from mesa_trading.cli import report as report_cli
from mesa_trading.loop.cycle import run_cycle
from mesa_trading.models import BookState, Order, Position
from mesa_trading.reports.build import build_daily, render_daily_md
from mesa_trading.reports.costos import (
    book_cost_view,
    by_market_type,
    recalc_current_table,
    size_scenarios,
    summarize,
    trades_from_events,
    update_cost_store,
)
from mesa_trading.storage import LocalRoot
from tests.helpers import T0, ScriptedJudge, connector_for, desk, make_env
from tests.reports.test_costos import _answers, _ev, _round_trip, _trade

Q = Decimal("0.0001")


def _position(**overrides) -> Position:
    body = dict(
        market="WETH-USDC",
        bot="house",
        side="long",
        qty=Decimal("0.1"),
        entry_price=Decimal("2500.25"),
        notional_usd=Decimal("250"),
        opened_at=T0,
        fees_usd=Decimal("0.125"),
        slippage_usd=Decimal("0.025"),
        kind="spot",
    )
    body.update(overrides)
    return Position(**body)


# --- 1. Deslizamiento una sola vez ----------------------------------------------------------


def test_book_charges_slippage_once_and_cash_matches_realized_pnl():
    book = fresh_book(Decimal("1000"), T0)
    # El precio de entrada (2500.25) ya trae 0.025 USD de deslizamiento sobre 2500.
    open_position(book, _position(), Decimal("0.125"), Decimal("0.025"))
    # Salida a 2509.75: 2510 menos 0.025 USD de deslizamiento (0.25 por unidad × 0.1).
    pnl = close_position(book, Decimal("2509.75"), Decimal("0.1255"), Decimal("0.025"), T0)
    move = (Decimal("2509.75") - Decimal("2500.25")) * Decimal("0.1")
    assert pnl == move - Decimal("0.125") - Decimal("0.1255")  # sin restar deslizamiento otra vez
    assert book.cash_usd - Decimal("1000") == pnl
    assert book.slippage_usd == Decimal("0.05")  # solo desglose
    assert book.slippage_double_counted_legacy_usd == Decimal("0")


def test_cycle_book_pnl_equals_corrected_net(tmp_path):
    env, state = _round_trip(tmp_path)
    trades, _ = trades_from_events(env.logs.all_events())
    [trade] = trades
    assert Decimal(trade["deslizamiento_usd"]) > 0
    assert trade["pnl_libro_usd"] == trade["neto_usd"]
    view = book_cost_view(json.loads(state.model_dump_json()), desk())
    assert view["deslizamiento_doble_contado_usd"] == "0.0000"
    assert view["pnl_neto_usd"] == view["pnl_libro_usd"] == trade["neto_usd"]
    # Sin posición abierta, todo el cambio de cash es el realizado.
    assert state.position is None
    assert state.cash_usd - Decimal("1000") == state.realized_pnl_usd


def test_sim_broker_cash_does_not_charge_slippage_twice():
    schedule = FeeSchedule(taker_bps=Decimal("10"), min_fee_usd=Decimal("0"), gas_usd=Decimal("0.05"))
    broker = SimBroker(fee_schedule=schedule, spread_bps=Decimal("0"), extra_bps=Decimal("10"),
                       liquidity_usd=Decimal("1000000000"), ask=Decimal("100"), bid=Decimal("100"),
                       cash_usd=Decimal("1000"))
    order = Order(client_order_id="b1", cycle_id="c", bot="gambler", market="X", side="buy", action="long",
                  order_type="market", qty=Decimal("1"), notional_usd=Decimal("100"), decision_time=T0)
    broker.submit(order)
    [fill] = broker.fills()
    assert fill.price > Decimal("100")  # deslizamiento dentro del precio
    assert broker.cash_usd == Decimal("1000") - fill.price * fill.qty - fill.fee_usd - fill.gas_usd


def test_legacy_book_keeps_its_history_and_reports_the_double_count():
    # Libro escrito antes de #29: 2 cierres con 0.3 de deslizamiento, sin los campos nuevos.
    legacy = BookState.model_validate(
        {
            "cash_usd": "990",
            "day_start_equity_usd": "1000",
            "day_start_cdmx": "2026-11-18",
            "peak_equity_usd": "1000",
            "realized_pnl_usd": "-10",
            "fees_usd": "1",
            "slippage_usd": "0.3",
            "closed_trades": 2,
        }
    )
    assert legacy.slippage_double_counted_legacy_usd is None
    before = book_cost_view(json.loads(legacy.model_dump_json()))
    assert before["deslizamiento_doble_contado_usd"] == "0.3000"
    assert before["pnl_neto_usd"] == "-9.7000"

    open_position(legacy, _position(), Decimal("0.125"), Decimal("0.025"))
    close_position(legacy, Decimal("2509.75"), Decimal("0.125"), Decimal("0.025"), T0)
    # El realizado viejo no se reescribe; el monto doble queda congelado aparte.
    assert legacy.slippage_double_counted_legacy_usd == Decimal("0.3")
    assert legacy.realized_pnl_usd == Decimal("-10") + Decimal("0.95") - Decimal("0.25")
    after = book_cost_view(json.loads(legacy.model_dump_json()))
    assert after["deslizamiento_doble_contado_usd"] == "0.3000"
    assert Decimal(after["pnl_neto_usd"]) == legacy.realized_pnl_usd + Decimal("0.3")


def test_legacy_close_event_is_still_read_with_the_old_identity():
    events = [
        _ev("OrderFilled", "2026-11-19T10:05:00Z", market="BTC-PERP", action="close", side="long",
            notional_usd="250", price="101", fee_usd="0.1125", slippage_usd="0.025", pnl_usd="2.275",
            entry_fee_usd="0.1125", entry_slippage_usd="0.025", client_order_id="x1"),
    ]
    [trade], _ = trades_from_events(events)
    assert trade["contabilidad"] == 1
    assert Decimal(trade["neto_usd"]) - Decimal(trade["pnl_libro_usd"]) == Decimal("0.05")


# --- 2. Tier real del pool --------------------------------------------------------------------


def test_weth_usdc_uses_the_5_bps_pool_tier():
    fees = load_fees()
    config = desk()
    spec = config.markets["WETH-USDC"]
    schedule = fees[spec.fee_schedule]
    assert spec.pool == "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640"
    assert schedule.taker_bps == Decimal("5")
    assert spec.pool in schedule.fuente
    assert commission_usd(Decimal("250"), schedule) == Decimal("0.125")
    # La tabla genérica de 30 bps sigue en el archivo, pero ningún mercado la usa.
    assert fees["dex_pool"].taker_bps == Decimal("30")
    assert all(market.fee_schedule != "dex_pool" for market in config.markets.values())
    # Los perps no cambian.
    assert fees[config.markets["BTC-PERP"].fee_schedule].taker_bps == Decimal("4.5")


def test_every_schedule_cites_its_source():
    assert all(schedule.fuente for schedule in load_fees().values())


# --- 3. Gas por pata ------------------------------------------------------------------------


def test_gas_is_charged_per_leg_in_the_book():
    book = fresh_book(Decimal("1000"), T0)
    open_position(book, _position(), Decimal("0.125"), Decimal("0.025"), Decimal("0.05"))
    assert book.position.gas_usd == Decimal("0.05")
    assert book.cash_usd == Decimal("1000") - Decimal("250.025") - Decimal("0.125") - Decimal("0.05")
    pnl = close_position(book, Decimal("2509.75"), Decimal("0.125"), Decimal("0.025"), T0, gas=Decimal("0.05"))
    assert pnl == Decimal("0.95") - Decimal("0.25") - Decimal("0.10")
    assert book.gas_usd == Decimal("0.10")
    assert book.cash_usd - Decimal("1000") == pnl
    assert book.costs_by_market["WETH-USDC"] == {
        "fees_usd": "0.250", "slippage_usd": "0.050", "gas_usd": "0.10", "legs": "2",
    }


def test_cycle_charges_gas_from_the_fee_table(tmp_path):
    judge = ScriptedJudge(_answers({"largo": "0.1", "corto": "0.85", "hold": "0.05"}))
    env = make_env(tmp_path, judge, connector_for(T0, price="100"), mode="simulated", markets=["BTC-PERP"])
    # Solo en la prueba: la tabla del perp con gas, para ver que el ciclo lo cobra.
    base = env.fees["hyperliquid_perp"]
    env.fees["hyperliquid_perp"] = base.model_copy(update={"gas_usd": Decimal("0.05")})
    run_cycle(env, "20261106T120000Z", T0)
    judge.response = _answers({"largo": "0.1", "corto": "0.1", "hold": "0.8"}, trend="0.1")
    run_cycle(env, "20261106T120500Z", T0 + timedelta(minutes=5))
    state, _ = env.book.read()
    fills = [e["payload"] for e in env.logs.all_events() if e["event_type"] == "OrderFilled"]
    assert [fill["gas_usd"] for fill in fills] == ["0.05", "0.05"]
    assert fills[-1]["entry_gas_usd"] == "0.05"
    assert state.gas_usd == Decimal("0.10")
    [trade], _ = trades_from_events(env.logs.all_events())
    assert trade["gas_usd"] == "0.1000"
    assert trade["pnl_libro_usd"] == trade["neto_usd"]
    assert Decimal(trade["bruto_usd"]) - Decimal(trade["neto_usd"]) == (
        Decimal(trade["comisiones_usd"]) + Decimal(trade["deslizamiento_usd"]) + Decimal("0.1")
    )
    assert summarize([trade])["total"]["gas_usd"] == "0.1000"


def test_gas_does_not_grow_with_order_size():
    trades = [_trade("house", bruto="2", fees="0.25", slip="0.05", nocional="250", market="WETH-USDC", gas_usd="0.1")]
    rows = {row["multiplicador"]: row for row in size_scenarios(trades, load_fees(), desk())}
    assert rows["1"]["gas_usd"] == rows["4"]["gas_usd"] == "0.1000"
    assert rows["4"]["comisiones_usd"] == "1.0000"


# --- Costos por mercado (DEX vs. perps) ----------------------------------------------------------


def test_costs_split_by_market_type():
    trades = [
        _trade("house", bruto="2", fees="0.25", slip="0.05", nocional="250", market="WETH-USDC", gas_usd="0.1", id="h"),
        _trade("gambler", bruto="1", fees="0.225", slip="0.05", nocional="250", market="BTC-PERP", id="g1"),
        _trade("gambler", bruto="1", fees="0.225", slip="0.05", nocional="250", market="ETH-PERP", id="g2"),
    ]
    split = by_market_type(trades, desk())
    assert set(split) == {"dex", "perps"}
    assert split["dex"]["total"]["comisiones_usd"] == "0.2500"
    assert split["dex"]["total"]["gas_usd"] == "0.1000"
    assert split["perps"]["total"]["comisiones_usd"] == "0.4500"
    assert set(split["perps"]["por_mercado"]) == {"BTC-PERP", "ETH-PERP"}


def test_estado_shows_fees_by_market_type(tmp_path):
    env, state = _round_trip(tmp_path)
    now = T0 + timedelta(hours=1)
    daily = build_daily([], desk(), now)
    report_cli._add_costs(str(tmp_path), daily, env.logs, state, desk(), now, [])
    assert "Comisiones por tipo de mercado" in render_daily_md(daily)

    root = LocalRoot(tmp_path)
    root.write_text("book/state.json", json.dumps({"state": json.loads(state.model_dump_json()), "generation": 3}))
    body = TestClient(create_app(root)).get("/estado").json()
    costos = body["costos"]
    reporte = costos["por_tipo_mercado"]["acumulado"]["perps"]
    assert reporte["por_mercado"]["BTC-PERP"]["operaciones"] == 1
    assert reporte["total"]["comisiones_usd"] == format(state.fees_usd.quantize(Q), "f")
    vivo = costos["acumulado_libro"]["por_tipo_mercado"]["perps"]
    assert vivo["patas"] == 2
    assert vivo["comisiones_usd"] == format(state.fees_usd.quantize(Q), "f")
    assert "dex" not in costos["acumulado_libro"]["por_tipo_mercado"]
    assert body["gas_usd"] == "0"


def test_old_cost_store_rebuilds_market_totals_from_detail():
    trades = [_trade("house", market="WETH-USDC", id="h"), _trade("gambler", market="BTC-PERP", id="g")]
    old = {"version": 1, "desde": None, "abiertas": {}, "operaciones": trades, "dias": {}}
    store, _ = update_cost_store(old, [])
    assert set(store["dias_mercado"]["2026-11-19"]) == {"WETH-USDC", "BTC-PERP"}
    assert store["version"] == 2


# --- Recalculo histórico (reporte aparte) -----------------------------------------------------


def test_recalc_separates_each_correction_without_touching_the_book():
    # Operación vieja en el pool: 30 bps por pata (1.50 ida y vuelta), sin gas, deslizamiento doble.
    old = _trade("house", bruto="2", fees="1.50", slip="0.05", nocional="250", market="WETH-USDC", id="h")
    perp = _trade("gambler", bruto="1", fees="0.225", slip="0.05", nocional="250", market="BTC-PERP", id="g")
    recalc = recalc_current_table([old, perp], load_fees(), desk())
    dex = recalc["por_tipo_mercado"]["dex"]
    assert dex["pnl_libro_usd"] == "0.4000"  # 2 - 1.50 - 0.05 - 0.05
    assert dex["efecto_deslizamiento_doble_usd"] == "0.0500"
    assert dex["efecto_comisiones_usd"] == "1.2500"  # 1.50 -> 0.25
    assert dex["efecto_gas_usd"] == "-0.1000"
    assert dex["neto_tabla_actual_usd"] == "1.6000"  # 2 - 0.25 - 0.05 - 0.10
    perps = recalc["por_tipo_mercado"]["perps"]
    assert perps["efecto_comisiones_usd"] == "0.0000"
    assert perps["efecto_gas_usd"] == "0.0000"
    assert perps["diferencia_vs_libro_usd"] == "0.0500"
    assert recalc["total"]["operaciones"] == 2
    # El detalle de entrada no cambia.
    assert Decimal(old["pnl_libro_usd"]) == Decimal("0.4")
