import json
from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from mesa_trading.api.app import create_app
from mesa_trading.broker.fees import load_fees
from mesa_trading.reports.build import render_daily_md
from mesa_trading.sombra.reporte import render_md, seccion_diaria
from mesa_trading.sombra.runner import CARRY_KEY, RESUMEN_KEY, TENDENCIA_KEY, correr
from mesa_trading.storage import LocalRoot

from tests.helpers import desk
from tests.sombra.fakes import T0, FuenteFalsa, horas, velas


def _fuente(now):
    funding = {a: horas(now - timedelta(days=100), 100 * 24 + 1, "0.00003") for a in ("ETH", "BTC")}
    ctx = {"ETH": {"oracle": "2000"}, "BTC": {"oracle": "70000"}}
    books = {"ETH": {"bid": "2000.9", "ask": "2001.1"}, "BTC": {"bid": "70003", "ask": "70005"}}
    v1 = {a: velas(now - timedelta(days=70), timedelta(days=1), [str(1000 + i) for i in range(70)]) for a in ("ETH", "BTC")}
    v4 = {a: velas(now - timedelta(hours=120), timedelta(hours=4), ["1000"] * 30) for a in ("ETH", "BTC")}
    return FuenteFalsa(funding=funding, ctx=ctx, books=books, velas4h=v4, velas1d=v1)


def test_correr_escribe_libros_aparte_y_estado_los_muestra(tmp_path):
    now = T0 + timedelta(days=100)
    root = LocalRoot(tmp_path)
    fuente = _fuente(now)
    resumen = correr(root, fuente, desk(), load_fees(), now)
    # No toca el libro de house/gambler.
    assert not (tmp_path / "book").exists()
    for key in (CARRY_KEY, TENDENCIA_KEY, RESUMEN_KEY, "sombra/funding/historial.json"):
        assert root.read_text(key)
    carry = resumen["estrategias"]["carry"]
    assert carry["capital_teorico_usd"] == "10000.00"
    assert set(carry["posiciones_abiertas"]) == {"ETH", "BTC"}  # 26% anual > costos + 5%
    assert Decimal(carry["total"]["costos_usd"]) > 0
    assert carry["descarte"]["estado"] == "en_prueba"
    tend = resumen["estrategias"]["tendencia"]
    assert tend["operaciones_cerradas"] == 0 and tend["descarte"]["estado"] == "en_prueba"
    p30 = resumen["funding"]["activos"]["ETH"]["30d"]
    assert p30["pct_positivo"] == "1.0000" and p30["periodos"] == 721

    # Segundo tick en 2 h: cobra funding y no vuelve a pedir velas de 4h.
    fuente.llamadas.clear()
    later = now + timedelta(hours=2)
    fuente.funding = {a: horas(now - timedelta(days=100), 100 * 24 + 3, "0.00003") for a in ("ETH", "BTC")}
    resumen = correr(root, fuente, desk(), load_fees(), later)
    assert not any(c[0] == "velas" for c in fuente.llamadas)
    assert Decimal(resumen["estrategias"]["carry"]["total"]["funding_usd"]) > 0

    body = TestClient(create_app(root)).get("/estado").json()
    sombra = body["sombra"]
    assert set(sombra["estrategias"]) == {"carry", "tendencia"}
    for item in sombra["estrategias"].values():
        assert {"total", "operaciones_cerradas", "max_drawdown_usd", "descarte"} <= set(item)
    assert sombra["funding_persistencia"]["activos"]["BTC"]["90d"]["pct_positivo"] == "1.0000"

    seccion = seccion_diaria(root, later)
    md = "\n".join(render_md(seccion))
    assert "### carry" in md and "### tendencia" in md and "Persistencia del funding" in md
    assert "Regla de descarte" in md


def test_estado_sin_sombra_es_null_y_reporte_dice_sin_datos(tmp_path):
    root = LocalRoot(tmp_path)
    body = TestClient(create_app(root)).get("/estado").json()
    assert body["sombra"] is None
    assert seccion_diaria(root, T0) is None
    assert render_md(None)[1] == "- Sin datos todavía."


def test_render_daily_md_incluye_sombra():
    report = json.loads((json.dumps({
        "fecha": "2026-11-19", "hora_cdmx": "07:00", "ciclos": {"corridos": 1, "esperados": 1, "perdidos": 0},
        "pnl_dia_usd": "0", "pnl_acumulado_usd": "0", "drawdown_maximo_usd": "0", "gasto_jev_usd": 0.0,
        "decisiones_por_bot": {}, "vetos_por_regla": {}, "rechazos_por_paso": {}, "alertas": [], "sombra": None,
    })))
    assert "Estrategias en sombra" in render_daily_md(report)


def test_un_fallo_de_precio_no_tumba_el_tick(tmp_path):
    now = T0 + timedelta(days=100)
    fuente = _fuente(now)
    fuente.books = {"ETH": fuente.books["ETH"]}  # BTC sin libro
    resumen = correr(LocalRoot(tmp_path), fuente, desk(), load_fees(), now)
    assert resumen["errores"] == {"precio:BTC": "sin_dato"}
    assert set(resumen["estrategias"]["carry"]["posiciones_abiertas"]) == {"ETH"}
