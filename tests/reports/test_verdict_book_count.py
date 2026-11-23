"""El contador del veredicto sale del libro (el mismo de /estado), no de pares.json.

Issue #25: el contador debe salir del libro. Issue #33: los cierres de la
version 3 cuentan como evidencia; cierre_sin_datos no entra al contador y se
marca aparte.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from mesa_trading.book.store import FileBookStore, close_position, fresh_book, open_position
from mesa_trading.cli import report as report_cli
from mesa_trading.config import load_desk, load_promotion, repo_root
from mesa_trading.models import Position
from mesa_trading.reports.build import (
    apply_verdict,
    book_sample,
    build_calibration,
    build_daily,
    render_daily_md,
    update_pair_store,
)
from mesa_trading.util import iso
from tests.helpers import T0, ScriptedJudge, bullish, make_env

NOW = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)


def _promo():
    return load_promotion(repo_root() / "config" / "promotion.yaml")


def _desk():
    return load_desk(repo_root() / "config" / "desk.yaml")


def _position(when: datetime) -> Position:
    return Position(
        market="BTC-PERP",
        bot="gambler",
        side="long",
        qty=Decimal("1"),
        entry_price=Decimal("100"),
        notional_usd=Decimal("100"),
        opened_at=when,
        kind="perp",
        threshold_version=3,
    )


def test_close_position_counts_by_reason():
    book = fresh_book(Decimal("1000"), NOW)
    for reason in ("take_profit", "cierre_sin_datos", "stop_loss", "cierre_sin_datos"):
        open_position(book, _position(NOW), Decimal("0"), Decimal("0"))
        close_position(book, Decimal("101"), Decimal("0"), Decimal("0"), NOW, reason=reason)
    open_position(book, _position(NOW), Decimal("0"), Decimal("0"))
    close_position(book, Decimal("101"), Decimal("0"), Decimal("0"), NOW)
    assert book.closed_trades == 5
    assert book.closed_by_reason == {"take_profit": 1, "cierre_sin_datos": 2, "stop_loss": 1, "sin_motivo": 1}


def test_book_sample_counts_closes_and_marks_cierre_sin_datos_apart():
    book = {
        "closed_trades": 32,
        "closed_by_reason": {"take_profit": 20, "cierre_sin_datos": 3},
        "started_at": iso(NOW - timedelta(days=3, hours=12)),
    }
    sample = book_sample(book, NOW)
    # cierre_sin_datos no entra al contador de evidencia.
    assert sample["cierres"] == 29
    assert sample["cierres_totales"] == 32
    assert sample["cierres_sin_datos"] == 3
    # Los 9 de antes de llevar la cuenta por motivo no se pierden.
    assert sample["cierres_por_motivo"] == {"take_profit": 20, "cierre_sin_datos": 3, "sin_motivo_registrado": 9}
    assert round(sample["dias"], 2) == 3.5


def test_book_sample_without_start_is_zero_days():
    sample = book_sample({"closed_trades": 0}, NOW)
    assert sample["cierres"] == 0
    assert sample["dias"] == 0.0
    assert sample["dias_de_trading_cdmx"] == 0
    assert sample["cierres_por_motivo"] == {}


def test_trading_day_cuts_at_cdmx_midnight():
    # 05:30 UTC del 19 = 23:30 CDMX del 18 (UTC-6, sin horario de verano).
    started = iso(datetime(2026, 11, 19, 5, 30, tzinfo=timezone.utc))
    before_midnight = datetime(2026, 11, 19, 5, 59, tzinfo=timezone.utc)
    after_midnight = datetime(2026, 11, 19, 6, 0, tzinfo=timezone.utc)
    first = book_sample({"closed_trades": 1, "started_at": started}, before_midnight)
    second = book_sample({"closed_trades": 1, "started_at": started}, after_midnight)
    assert first["inicio_cdmx"] == "2026-11-18"
    assert first["dias_de_trading_cdmx"] == 1
    # Mismo día UTC, pero ya es otro día de trading en Ciudad de México.
    assert second["dias_de_trading_cdmx"] == 2
    assert second["dias"] < 1


def test_verdict_counter_uses_book_when_period_is_simulation():
    """Version 3: sus cierres cuentan; cierre_sin_datos queda aparte."""
    promo = _promo()
    events = [
        {
            "event_id": f"close{index}",
            "event_type": "PositionClosed",
            "occurred_at": iso(NOW - timedelta(hours=1)),
            "bot": "gambler",
            "mode": "simulated",
            "payload": {
                "pnl_usd": "0.5",
                "reason": "cierre_sin_datos" if index == 0 else "take_profit",
                "threshold_version": 3,
                "cuenta_como_evidencia": False,
            },
        }
        for index in range(4)
    ]
    calibration = build_calibration(events, promo, _desk(), NOW)
    assert calibration["evidencia"]["cuenta_como_evidencia"] is True
    assert calibration["evidencia"]["cierres"] == 3
    assert calibration["periodos"]["3"]["cierres"] == 3
    assert calibration["periodos"]["3"]["cierres_sin_datos"] == 1
    daily = build_daily(events, _desk(), NOW, book={"realized_pnl_usd": "2", "drawdown_usd": "0"})
    sample = book_sample(
        {
            "closed_trades": 32,
            "closed_by_reason": {"take_profit": 30, "cierre_sin_datos": 2},
            "started_at": iso(NOW - timedelta(days=4)),
        },
        NOW,
    )
    report = apply_verdict(
        daily,
        calibration,
        promo,
        closed_trades=calibration["evidencia"]["cierres"],
        started_at=calibration["evidencia"]["desde"],
        capital_usd=Decimal("1000"),
        max_drawdown_usd=Decimal("0"),
        now=NOW,
        book=sample,
    )
    verdict = report["veredicto"]
    assert verdict["operaciones_cerradas"] == 30
    assert verdict["cierres_sin_datos"] == 2
    assert verdict["dias_observados"] == 4
    assert verdict["cierres_por_dia"] == 7.5
    assert verdict["dias_estimados_para_muestra"] == 22.7
    # La version 3 cuenta como evidencia; cierre_sin_datos no.
    assert verdict["operaciones_cerradas_evidencia"] == 3
    assert verdict["estado"] == "insuficiente"
    assert "Muestra del libro: 30 cierres en 4.0 días" in verdict["nota"]
    assert "2 cierre_sin_datos" in verdict["nota"]
    assert "Cuentan como evidencia para el GO: 3 cierres" in verdict["nota"]
    assert report["cierres_del_periodo"] == {
        "total": 4,
        "cierre_sin_datos": 1,
        "por_motivo": {"cierre_sin_datos": 1, "take_profit": 3},
    }
    md = render_daily_md(report)
    assert "Contador: 30 de 200 cierres, 4.0 de 14 días." in md
    assert "cierre_sin_datos (aparte): 2." in md
    assert "Cierres en el periodo: 4. De ellos, cierre_sin_datos (último precio conocido): 1." in md


def test_book_sample_never_unlocks_go():
    """Muchos cierres en el libro no bastan si no son evidencia."""
    promo = _promo()
    calibration = build_calibration([], promo, _desk(), NOW)
    sample = book_sample({"closed_trades": 500, "started_at": iso(NOW - timedelta(days=30))}, NOW)
    report = apply_verdict(
        build_daily([], _desk(), NOW),
        calibration,
        promo,
        closed_trades=0,
        started_at=None,
        capital_usd=Decimal("1000"),
        max_drawdown_usd=Decimal("0"),
        now=NOW,
        pnl_usd=Decimal("50"),
        book=sample,
    )
    assert report["veredicto"]["operaciones_cerradas"] == 500
    assert report["veredicto"]["estado"] == "insuficiente"


def test_pair_store_keeps_cierre_sin_datos_across_runs():
    event = {
        "event_id": "c1",
        "event_type": "PositionClosed",
        "occurred_at": iso(NOW),
        "bot": "gambler",
        "payload": {"pnl_usd": "-1", "reason": "cierre_sin_datos", "threshold_version": 3},
    }
    first = update_pair_store(None, [event])
    again = update_pair_store(json.loads(json.dumps(first)), [event])
    assert again["periodos"]["3"]["cierres"] == 0
    assert again["periodos"]["3"]["cierres_sin_datos"] == 1


def test_cycle_records_cierre_sin_datos_in_book(tmp_path):
    from mesa_trading.connectors.fixture import FixtureConnector
    from mesa_trading.loop.cycle import run_cycle

    env = make_env(tmp_path, ScriptedJudge(bullish()), FixtureConnector(fail_times=3), mode="simulated", markets=["BTC-PERP"])
    state, generation = env.book.read()
    state.position = Position(
        market="BTC-PERP",
        bot="gambler",
        side="long",
        qty=Decimal("0.01"),
        entry_price=Decimal("70000"),
        notional_usd=Decimal("250"),
        opened_at=T0,
        kind="perp",
    )
    state.last_prices["BTC-PERP"] = "70000"
    state.cash_usd = Decimal("750")
    env.book.write(state, generation)
    run_cycle(env, "sin-datos", T0)
    closed = env.book.read()[0]
    assert closed.closed_trades == 1
    assert closed.closed_by_reason == {"cierre_sin_datos": 1}
    assert book_sample(closed.model_dump(mode="json"), T0)["cierres_sin_datos"] == 1


def test_report_cli_reads_the_same_book_as_estado(tmp_path, monkeypatch):
    monkeypatch.delenv("MESA_BUCKET", raising=False)
    started = datetime.now(timezone.utc) - timedelta(days=2)
    store = FileBookStore(Path(tmp_path) / "book", capital=Decimal("1000"), now=started)
    state, generation = store.read()
    state.started_at = iso(started)
    state.closed_trades = 32
    state.closed_by_reason = {"take_profit": 31, "cierre_sin_datos": 1}
    assert store.write(state, generation)
    assert report_cli.main(["--data-dir", str(tmp_path)]) == 0
    daily_dir = Path(tmp_path) / "reportes" / "diario"
    [daily_json] = list(daily_dir.glob("*.json"))
    verdict = json.loads(daily_json.read_text(encoding="utf-8"))["veredicto"]
    assert verdict["operaciones_cerradas"] == 31
    assert verdict["cierres_sin_datos"] == 1
    assert verdict["dias_observados"] >= 1.99
    assert verdict["operaciones_cerradas_evidencia"] == 0
