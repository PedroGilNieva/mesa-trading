import json
from datetime import timedelta
from pathlib import Path
from decimal import Decimal

from fastapi.testclient import TestClient

from mesa_trading.api.app import create_app
from mesa_trading.broker.fees import load_fees
from mesa_trading.reports.build import render_daily_md
from mesa_trading.sombra import funding as fh
from mesa_trading.sombra.reporte import render_md, seccion_diaria
from mesa_trading.sombra.runner import CARRY_KEY, RESUMEN_KEY, TENDENCIA_KEY, correr
from mesa_trading.storage import LocalRoot

from tests.helpers import desk
from tests.sombra.fakes import T0, FuenteFalsa, horas, velas


def _desk(**sombra):
    cfg = desk()
    return cfg.model_copy(update={"sombra": cfg.sombra.model_copy(update=sombra)})


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
    # Relleno incremental: 100 días (~5 páginas de 500 h) a 2 páginas por tick.
    # Mientras no esté al día, el carry no evalúa (medias viejas).
    resumen = correr(root, fuente, desk(), load_fees(), now)
    assert resumen["errores"] == {"funding:ETH": "relleno_pendiente", "funding:BTC": "relleno_pendiente"}
    assert resumen["estrategias"]["carry"]["posiciones_abiertas"] == {}
    assert resumen["plazo"] == {"deadline_segundos": 20.0, "cortado": False}
    correr(root, fuente, desk(), load_fees(), now)
    resumen = correr(root, fuente, desk(), load_fees(), now)
    assert resumen["errores"] == {}
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
    resumen = correr(LocalRoot(tmp_path), fuente, _desk(paginas_funding_por_tick=10), load_fees(), now)
    assert resumen["errores"] == {"precio:BTC": "sin_dato"}
    assert set(resumen["estrategias"]["carry"]["posiciones_abiertas"]) == {"ETH"}


class Reloj:
    """Reloj falso: cada llamada a la fuente tarda `por_llamada` segundos."""

    def __init__(self, por_llamada: float):
        self.t = 0.0
        self.por_llamada = por_llamada

    def __call__(self) -> float:
        return self.t


class FuenteLenta(FuenteFalsa):
    def __init__(self, reloj: Reloj, **kw):
        super().__init__(**kw)
        self.reloj = reloj

    def _tarda(self):
        self.reloj.t += self.reloj.por_llamada

    def funding_history(self, coin, start_ms, end_ms):
        self._tarda()
        return super().funding_history(coin, start_ms, end_ms)

    def asset_context(self, coin):
        self._tarda()
        return super().asset_context(coin)

    def order_book(self, coin, depth=5):
        self._tarda()
        return super().order_book(coin, depth)

    def candles(self, coin, interval, since=None):
        self._tarda()
        return super().candles(coin, interval, since)


def test_deadline_corta_limpio_y_sigue_en_el_proximo_tick(tmp_path):
    now = T0 + timedelta(days=100)
    root = LocalRoot(tmp_path)
    base = _fuente(now)
    reloj = Reloj(por_llamada=8.0)  # el timeout HTTP
    fuente = FuenteLenta(reloj, funding=base.funding, ctx=base.ctx, books=base.books, velas4h=base.v4, velas1d=base.v1)
    cfg = _desk(deadline_segundos=20)

    resumen = correr(root, fuente, cfg, load_fees(), now, reloj=reloj)
    # Revisa antes de cada llamada: 0, 8 y 16 s pasan; a los 24 s ya no llama.
    assert len(fuente.llamadas) == 3
    assert reloj.t <= 20 + 8  # a lo más una llamada de más
    assert resumen["plazo"] == {"deadline_segundos": 20.0, "cortado": True}
    errores = resumen["errores"]
    assert errores["funding:BTC"] == "deadline"
    assert errores["precio:ETH"] == errores["precio:BTC"] == "deadline"
    assert not any(c[0] == "velas" for c in fuente.llamadas)
    # Cortó limpio: guardó el avance y los libros, sin posiciones.
    for key in (CARRY_KEY, TENDENCIA_KEY, RESUMEN_KEY, "sombra/funding/historial.json"):
        assert root.read_text(key)
    guardado = json.loads(root.read_text("sombra/funding/historial.json"))["series"]
    assert len(guardado["ETH"]) == 1000 and len(guardado["BTC"]) == 500
    assert resumen["estrategias"]["carry"]["posiciones_abiertas"] == {}

    # Ticks siguientes con su propio plazo: continúa desde lo guardado y termina.
    for _ in range(4):
        fuente.llamadas.clear()
        reloj.t = 0.0
        resumen = correr(root, fuente, _desk(deadline_segundos=1000), load_fees(), now, reloj=reloj)
        primeras = [c for c in fuente.llamadas if c[0] == "funding" and c[1] == "ETH"]
        if primeras:
            assert primeras[0][2] > fh.ms(now - timedelta(days=100))  # no vuelve al inicio
    assert resumen["errores"] == {} and resumen["plazo"]["cortado"] is False
    assert set(resumen["estrategias"]["carry"]["posiciones_abiertas"]) == {"ETH", "BTC"}


def test_expected_edge_bps_de_tendencia_viene_de_desk_yaml():
    texto = (Path(__file__).resolve().parents[2] / "config" / "desk.yaml").read_text()
    assert "expected_edge_bps: 100" in texto
    assert desk().sombra.tendencia.expected_edge_bps == Decimal("100")
    assert _desk().sombra.deadline_segundos == 20 and _desk().sombra.paginas_funding_por_tick == 2
