"""Bruto vs. neto y costo por operación (#26). Solo medición."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from mesa_trading.api.app import create_app
from mesa_trading.broker.fees import FeeSchedule, load_fees
from mesa_trading.cli import report as report_cli
from mesa_trading.constants import PINNED_MODEL
from mesa_trading.loop.cycle import run_cycle
from mesa_trading.reports.build import build_daily, render_daily_md
from mesa_trading.reports.costos import (
    RESUMEN_KEY,
    STORE_KEY,
    book_cost_view,
    build_cost_section,
    fee_model,
    size_scenarios,
    stats,
    summarize,
    threshold_scenarios,
    trades_from_events,
    update_cost_store,
)
from mesa_trading.storage import LocalRoot
from tests.helpers import T0, ScriptedJudge, connector_for, desk, make_env


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


def _round_trip(tmp_path):
    judge = ScriptedJudge(_answers({"largo": "0.1", "corto": "0.85", "hold": "0.05"}))
    env = make_env(tmp_path, judge, connector_for(T0, price="100"), mode="simulated", markets=["BTC-PERP"])
    run_cycle(env, "20261106T120000Z", T0)
    judge.response = _answers({"largo": "0.1", "corto": "0.1", "hold": "0.8"}, trend="0.1")
    run_cycle(env, "20261106T120500Z", T0 + timedelta(minutes=5))
    state, _gen = env.book.read()
    return env, state


def _ev(kind, at, bot="gambler", cycle="c1", **payload):
    return {
        "event_type": kind,
        "occurred_at": at,
        "bot": bot,
        "correlation_id": cycle,
        "event_id": f"{kind}-{at}",
        "payload": payload,
    }


def _trade(bot="gambler", bruto="1", fees="0.2", slip="0.05", nocional="250", market="BTC-PERP", **extra):
    neto = Decimal(bruto) - Decimal(fees) - Decimal(slip)
    row = {
        "id": extra.pop("id", f"{bot}-{bruto}-{nocional}"),
        "bot": bot,
        "market": market,
        "side": "long",
        "cerrada_en": "2026-11-19T15:00:00.000000Z",
        "dia": "2026-11-19",
        "nocional_usd": nocional,
        "nocional_salida_usd": nocional,
        "bruto_usd": bruto,
        "comisiones_usd": fees,
        "deslizamiento_usd": slip,
        "neto_usd": str(neto),
        "pnl_libro_usd": str(neto - Decimal(slip)),
        "entrada_conocida": True,
    }
    row.update(extra)
    return row


# --- reconstrucción desde la bitácora real del ciclo ---------------------------------


def test_round_trip_splits_gross_fees_and_slippage(tmp_path):
    env, state = _round_trip(tmp_path)
    trades, still_open = trades_from_events(env.logs.all_events())
    assert still_open == {}
    assert len(trades) == 1
    trade = trades[0]
    fills = [e["payload"] for e in env.logs.all_events() if e["event_type"] == "OrderFilled"]
    entry, close = fills
    qty = Decimal(close["qty"])
    fees = Decimal(entry["fee_usd"]) + Decimal(close["fee_usd"])
    slips = Decimal(entry["slippage_usd"]) + Decimal(close["slippage_usd"])
    price_move = (Decimal(entry["price"]) - Decimal(close["price"])) * qty  # short
    assert trade["fuente_bruto"] == "precios"
    assert Decimal(trade["bruto_usd"]) == (price_move + slips).quantize(Decimal("0.0001"))
    assert Decimal(trade["comisiones_usd"]) == fees.quantize(Decimal("0.0001"))
    assert Decimal(trade["deslizamiento_usd"]) == slips.quantize(Decimal("0.0001"))
    assert Decimal(trade["neto_usd"]) == (price_move - fees).quantize(Decimal("0.0001"))
    # Desde #29 el libro cobra el deslizamiento una sola vez: pnl_libro = neto.
    assert Decimal(trade["pnl_libro_usd"]) == state.realized_pnl_usd.quantize(Decimal("0.0001"))
    assert trade["pnl_libro_usd"] == trade["neto_usd"]
    assert trade["contabilidad"] == 2
    assert trade["gas_usd"] == "0.0000"  # perps: sin gas
    assert trade["motivo_entrada"] == "entrada"
    assert trade["observados"]["accion_p"]
    assert trade["umbrales"]["accion_min_p"]


def test_book_view_matches_the_trades(tmp_path):
    env, state = _round_trip(tmp_path)
    trades, _ = trades_from_events(env.logs.all_events())
    view = book_cost_view(json.loads(state.model_dump_json()))
    total = summarize(trades)["total"]
    for key in ("pnl_bruto_usd", "comisiones_usd", "deslizamiento_usd", "pnl_neto_usd", "pnl_libro_usd"):
        assert view[key] == total[key], key
    assert view["operaciones_cerradas"] == 1


def test_book_view_leaves_open_position_costs_out():
    state = {
        "realized_pnl_usd": "-1",
        "fees_usd": "0.9",
        "slippage_usd": "0.3",
        "closed_trades": 2,
        "position": {"fees_usd": "0.4", "slippage_usd": "0.1"},
    }
    view = book_cost_view(state)
    assert view["comisiones_usd"] == "0.5000"
    assert view["deslizamiento_usd"] == "0.2000"
    assert view["pnl_neto_usd"] == "-0.8000"
    assert view["pnl_bruto_usd"] == "-0.1000"
    # Libro de antes de #29 (sin slippage_double_counted_legacy_usd): todo el deslizamiento
    # cerrado se contó dos veces.
    assert view["deslizamiento_doble_contado_usd"] == "0.2000"
    assert view["costos_posicion_abierta_usd"] == {"comisiones": "0.4000", "deslizamiento": "0.1000", "gas": "0.0000"}


# --- eventos viejos, sin cantidad ---------------------------------------------------------


def test_legacy_close_uses_the_book_identity():
    events = [
        _ev("OrderFilled", "2026-11-19T10:00:00Z", market="BTC-PERP", action="long", side="long",
            notional_usd="250", price="100", fee_usd="0.1125", slippage_usd="0.025", client_order_id="e1"),
        _ev("OrderFilled", "2026-11-19T10:05:00Z", market="BTC-PERP", action="close", side="long",
            notional_usd="250", price="101", fee_usd="0.1125", slippage_usd="0.025", pnl_usd="2.275",
            client_order_id="x1", reason="salida"),
    ]
    [trade], _ = trades_from_events(events)
    assert trade["fuente_bruto"] == "identidad_libro"
    # P = 2.275 + 0.225 + 0.05 = 2.55; bruto = P + 0.05; neto = P - 0.225.
    assert trade["bruto_usd"] == "2.6000"
    assert trade["neto_usd"] == "2.3250"
    assert trade["comisiones_usd"] == "0.2250"
    assert trade["deslizamiento_usd"] == "0.0500"
    assert trade["entrada_conocida"] is True


def test_close_without_entry_is_flagged():
    events = [
        _ev("OrderFilled", "2026-11-19T10:05:00Z", market="BTC-PERP", action="close", side="long",
            notional_usd="250", price="101", fee_usd="0.1", slippage_usd="0.02", pnl_usd="-0.5", client_order_id="x1"),
    ]
    [trade], _ = trades_from_events(events)
    assert trade["entrada_conocida"] is False
    assert trade["comision_entrada_usd"] == "0.0000"


# --- estado acumulado ----------------------------------------------------------------------


def test_store_counts_each_close_once_and_carries_open_entries():
    entry = _ev("OrderFilled", "2026-11-19T10:00:00Z", market="BTC-PERP", action="long", side="long",
                notional_usd="250", price="100", qty="2.5", fee_usd="0.1", slippage_usd="0.02", client_order_id="e1")
    close = _ev("OrderFilled", "2026-11-19T12:00:00Z", market="BTC-PERP", action="close", side="long",
                notional_usd="250", price="101", qty="2.5", fee_usd="0.1", slippage_usd="0.02", pnl_usd="2.26",
                client_order_id="x1")
    store, fresh = update_cost_store(None, [entry])
    assert fresh == []
    assert "BTC-PERP" in store["abiertas"]
    store, fresh = update_cost_store(store, [close])
    assert len(fresh) == 1
    assert fresh[0]["entrada_conocida"] is True
    assert store["abiertas"] == {}
    again, fresh = update_cost_store(store, [entry, close])
    assert fresh == []
    assert again["dias"] == store["dias"]
    day = store["dias"]["2026-11-19"]["gambler"]
    assert day["operaciones"] == 1
    # P = 2.5; bruto = 2.54; neto = 2.3; libro 2.26.
    assert Decimal(day["bruto"]) == Decimal("2.54")
    assert Decimal(day["neto"]) == Decimal("2.3")


def test_day_is_cut_in_mexico_city():
    close = _ev("OrderFilled", "2026-11-19T05:30:00Z", market="BTC-PERP", action="close", side="long",
                price="1", fee_usd="0", slippage_usd="0", pnl_usd="1", client_order_id="x1")
    store, _ = update_cost_store(None, [close])
    assert list(store["dias"]) == ["2026-11-18"]


# --- estadísticas por estrategia -------------------------------------------------------


def test_stats_per_trade_and_cost_coverage():
    trades = [
        _trade("house", bruto="0.80", fees="1.50", slip="0.06", nocional="250", market="WETH-USDC", id="h1"),
        _trade("house", bruto="1.20", fees="1.50", slip="0.06", nocional="250", market="WETH-USDC", id="h2"),
        _trade("gambler", bruto="0.50", fees="0.225", slip="0.05", nocional="250", id="g1"),
    ]
    body = summarize(trades)
    house = body["por_estrategia"]["house"]
    gambler = body["por_estrategia"]["gambler"]
    assert house["operaciones"] == 2
    assert house["tamano_promedio_usd"] == "250.0000"
    assert house["bruto_promedio_usd"] == "1.0000"
    assert house["costo_promedio_usd"] == "1.5600"
    assert house["costo_bps"] == "62.40"
    assert house["cubre_costos"] is False
    assert gambler["cubre_costos"] is True
    assert body["total"]["operaciones"] == 3
    assert stats({"operaciones": 0, "nocional": Decimal(0), "bruto": Decimal(0), "comisiones": Decimal(0),
                  "deslizamiento": Decimal(0), "neto": Decimal(0), "pnl_libro": Decimal(0),
                  "ganadoras_bruto": 0, "ganadoras_neto": 0})["cubre_costos"] is False


# --- modelo de comisiones -------------------------------------------------------------------


def test_fee_model_splits_fixed_and_percentage_parts():
    model = fee_model(load_fees(), desk(), Decimal("250"))
    perp = model["tablas"]["hyperliquid_perp"]
    assert perp["porcentual_bps"] == "4.50"
    assert perp["comision_por_pata_usd"] == "0.1125"
    assert perp["comision_ida_y_vuelta_usd"] == "0.2250"
    assert perp["parte_fija_por_pata_usd"] == "0.0000"
    assert "BTC-PERP" in perp["mercados"]
    pool = model["tablas"]["uniswap_v3_weth_usdc_005"]
    assert pool["porcentual_bps"] == "5.00"
    assert pool["comision_ida_y_vuelta_usd"] == "0.2500"
    assert pool["gas_por_pata_usd"] == "0.0500"
    assert pool["gas_ida_y_vuelta_usd"] == "0.1000"
    assert pool["gas_cobrado_en_libro"] is True
    assert pool["mercados"] == ["WETH-USDC"]
    assert "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640" in pool["fuente"]
    assert model["tablas"]["dex_pool"]["mercados"] == []
    minimum = model["tablas"]["dex_minimo"]
    assert minimum["parte_fija_por_pata_usd"] == "0.9500"
    assert minimum["parte_porcentual_por_pata_usd"] == "0.0000"
    assert all(body["fuente"] for body in model["tablas"].values())


# --- simulaciones -----------------------------------------------------------------------------


def test_higher_entry_threshold_drops_weaker_trades():
    umbrales = {"accion_min_p": "0.55", "setup_min": "1.0", "regimen_tendencia_min_p": "0.45", "ejecucion_degradada_max_p": "0.40"}
    strong = _trade(id="s", bruto="2", observados={"accion_p": "0.70", "setup": "2", "regimen_tendencia": "0.9", "ejecucion_degradada": "0.1"}, umbrales=umbrales)
    weak = _trade(id="w", bruto="-1", observados={"accion_p": "0.60", "setup": "2", "regimen_tendencia": "0.9", "ejecucion_degradada": "0.1"}, umbrales=umbrales)
    rows = {row["escenario"]: row for row in threshold_scenarios([strong, weak]) if row["estrategia"] == "gambler"}
    assert rows["actual"]["operaciones"] == 2
    assert rows["accion_min_p+1"]["operaciones"] == 2  # 0.60 >= 0.60
    assert rows["accion_min_p+2"]["operaciones"] == 1  # 0.60 < 0.65
    assert rows["accion_min_p+2"]["pnl_bruto_usd"] == "2.0000"
    assert rows["accion_min_p+2"]["endurece"] == {"accion_min_p": "0.1000"}


def test_bigger_orders_scale_costs_one_to_one_without_minimum():
    trades = [_trade("gambler", bruto="0.50", fees="0.225", slip="0.05", nocional="250")]
    rows = {row["multiplicador"]: row for row in size_scenarios(trades, load_fees(), desk())}
    base, double = rows["1"], rows["2"]
    assert base["comisiones_usd"] == "0.2250"
    assert double["comisiones_usd"] == "0.4500"
    assert Decimal(double["pnl_neto_usd"]) == 2 * Decimal(base["pnl_neto_usd"])
    assert double["costo_bps"] == base["costo_bps"]


def test_bigger_orders_dilute_a_fixed_minimum():
    config = desk()
    config.markets["BTC-PERP"].fee_schedule = "fijo"
    fees = {"fijo": FeeSchedule(taker_bps=Decimal("0"), min_fee_usd=Decimal("1"))}
    trades = [_trade("gambler", bruto="1", fees="2", slip="0", nocional="100")]
    rows = {row["multiplicador"]: row for row in size_scenarios(trades, fees, config)}
    assert rows["1"]["comisiones_usd"] == "2.0000"
    assert rows["4"]["comisiones_usd"] == "2.0000"
    assert rows["4"]["pnl_neto_usd"] == "2.0000"


# --- reporte y /estado ---------------------------------------------------------------------


def test_daily_report_and_estado_show_gross_and_net(tmp_path):
    env, state = _round_trip(tmp_path)
    now = T0 + timedelta(hours=1)
    daily = build_daily([], desk(), now)
    report_cli._add_costs(str(tmp_path), daily, env.logs, state, desk(), now, [])
    section = daily["costos"]
    assert section["ventana_24h"]["por_estrategia"]["gambler"]["operaciones"] == 1
    assert section["acumulado"]["por_estrategia"]["gambler"]["operaciones"] == 1
    assert list(section["por_dia_cdmx"]) == ["2026-11-06"]
    assert section["acumulado_libro"]["pnl_neto_usd"] == section["acumulado"]["total"]["pnl_neto_usd"]
    text = render_daily_md(daily)
    assert "## PnL bruto vs. neto y costos" in text
    assert "hyperliquid_perp" in text
    assert (tmp_path / STORE_KEY).is_file()
    assert (tmp_path / RESUMEN_KEY).is_file()

    root = LocalRoot(tmp_path)
    root.write_text("book/state.json", json.dumps({"state": json.loads(state.model_dump_json()), "generation": 3}))
    body = TestClient(create_app(root)).get("/estado").json()
    costos = body["costos"]
    assert costos["acumulado_libro"]["pnl_libro_usd"] == format(state.realized_pnl_usd.quantize(Decimal("0.0001")), "f")
    assert costos["acumulado"]["por_estrategia"]["gambler"]["operaciones"] == 1
    assert costos["ventana_24h"]["total"]["operaciones"] == 1


def test_estado_without_cost_summary_still_answers(tmp_path):
    root = LocalRoot(tmp_path)
    root.write_text(
        "book/state.json",
        json.dumps({"state": {"cash_usd": "1000", "realized_pnl_usd": "-1", "fees_usd": "0.5", "slippage_usd": "0.1"}}),
    )
    body = TestClient(create_app(root)).get("/estado").json()
    assert body["costos"]["acumulado_libro"]["pnl_neto_usd"] == "-0.9000"
    assert body["costos"]["acumulado"] is None


def test_section_without_trades_renders():
    section = build_cost_section({}, load_fees(), desk(), datetime(2026, 11, 19, tzinfo=timezone.utc), None)
    assert section["ventana_24h"]["total"]["operaciones"] == 0
    daily = build_daily([], desk(), datetime(2026, 11, 19, tzinfo=timezone.utc))
    daily["costos"] = section
    assert "Sin operaciones cerradas" in render_daily_md(daily)
