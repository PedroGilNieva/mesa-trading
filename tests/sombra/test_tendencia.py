from datetime import timedelta
from decimal import Decimal

from mesa_trading.broker.fees import load_fees
from mesa_trading.sombra import tendencia as td
from mesa_trading.sombra.funding import HORA_MS, ms

from tests.helpers import desk
from tests.sombra.fakes import T0, historial, velas

FEES = load_fees()
DESK = desk()
CFG = DESK.sombra.tendencia
MAXP = DESK.risk.max_position_pct
MAXE = DESK.risk.max_exposure_pct


def test_tamano_por_riesgo_y_topes():
    e = Decimal("10000")
    # Stop a 8%: riesgo 0.5% → 50 USD / 0.08 = 625 USD de nocional.
    n, lim = td.tamano_por_riesgo(e, Decimal("2000"), Decimal("160"), CFG.riesgo_pct, MAXP, MAXE)
    assert (n, lim) == (Decimal("625"), "riesgo")
    # Stop a 1%: 5000 USD → tope del 25% del equity.
    n, lim = td.tamano_por_riesgo(e, Decimal("2000"), Decimal("20"), CFG.riesgo_pct, MAXP, MAXE)
    assert (n, lim) == (Decimal("2500"), "tope_posicion")
    # Ya hay 2000 USD expuestos: solo queda 500 dentro del 25% total.
    n, lim = td.tamano_por_riesgo(e, Decimal("2000"), Decimal("160"), CFG.riesgo_pct, MAXP, MAXE, Decimal("2000"))
    assert (n, lim) == (Decimal("500"), "tope_exposicion")
    assert td.tamano_por_riesgo(e, Decimal("2000"), Decimal("0"), CFG.riesgo_pct, MAXP, MAXE)[0] == 0


def test_veto_de_economia_sigue_aplicando():
    sched = FEES["hyperliquid_perp"]
    ok, edge, costos = td.veto_economia(Decimal("1000"), sched, Decimal("1"), CFG.liquidez_supuesta_usd, CFG.extra_bps, Decimal("100"))
    assert ok and edge == Decimal("10") and costos.total < edge
    ok, _e, _c = td.veto_economia(Decimal("1000"), sched, Decimal("1"), CFG.liquidez_supuesta_usd, CFG.extra_bps, Decimal("10"))
    assert not ok  # 10 bps no cubren ~13 bps de ida y vuelta


def _datos(ultimo_4h="2100", ultimo_1d_sube=True, bid="2099", ask="2101"):
    inicio_1d = T0 - timedelta(days=70)
    cierres_1d = [str(1500 + 10 * i) if ultimo_1d_sube else str(3000 - 10 * i) for i in range(70)]
    v1 = velas(inicio_1d, timedelta(days=1), cierres_1d, rango="40")
    inicio_4h = T0 - timedelta(hours=4 * 30)
    v4 = velas(inicio_4h, timedelta(hours=4), ["2000"] * 29 + [ultimo_4h], rango="10")
    return {"bid": Decimal(bid), "ask": Decimal(ask), "velas_4h": v4, "velas_1d": v1}


def test_senal_ruptura_con_regimen():
    d = _datos()
    sig = td.senal(td.cerradas(d["velas_4h"], T0), td.cerradas(d["velas_1d"], T0), CFG)
    assert sig["regimen"] == "alcista" and sig["entrada"] == "long"
    # Misma ruptura al alza con régimen bajista: no entra.
    d = _datos(ultimo_1d_sube=False)
    sig = td.senal(td.cerradas(d["velas_4h"], T0), td.cerradas(d["velas_1d"], T0), CFG)
    assert sig["regimen"] == "bajista" and sig["entrada"] is None
    assert td.senal([], [], CFG)["lista"] is False


def test_paso_abre_por_riesgo_y_cierra_en_stop_cerca_de_menos_1r():
    estado = td.nuevo(CFG, T0)
    td.paso(estado, {"ETH": _datos()}, None, CFG, FEES, MAXP, MAXE, T0)
    pos = estado.posiciones["ETH"]
    assert pos.lado == "long" and pos.entrada == Decimal("2100")
    # ATR diario = 80 (rango 40 a cada lado) → stop a 2 × 80 = 160.
    assert pos.stop == Decimal("1940")
    assert pos.nocional_usd == Decimal("50") * Decimal("2100") / Decimal("160")
    assert pos.riesgo_usd == Decimal("50")
    # La misma vela no se vuelve a evaluar; el stop se revisa con el precio.
    later = T0 + timedelta(minutes=5)
    td.paso(estado, {"ETH": {"bid": Decimal("1939"), "ask": Decimal("1941")}}, None, CFG, FEES, MAXP, MAXE, later)
    assert estado.posiciones == {}
    cierre = estado.cierres[-1]
    assert cierre["motivo"] == "stop"
    # Pierde ~1R (50 USD) más costos de ida y vuelta.
    assert Decimal(cierre["bruto_usd"]) == Decimal("-50.000000")
    assert Decimal(cierre["neto_usd"]) == Decimal("-50") - Decimal(cierre["costos_usd"])
    assert Decimal("-1.05") < Decimal(cierre["r_multiplo"]) < Decimal("-1")
    assert estado.drawdown.max_drawdown_usd >= Decimal("50")


def test_veto_economia_en_paso():
    cfg = CFG.model_copy(update={"expected_edge_bps": Decimal("5")})
    estado = td.nuevo(cfg, T0)
    td.paso(estado, {"ETH": _datos()}, None, cfg, FEES, MAXP, MAXE, T0)
    assert estado.posiciones == {} and estado.vetos == {"economia": 1}


def test_funding_largo_paga_corto_cobra():
    estado = td.nuevo(CFG, T0)
    pos = td.abrir(estado, "ETH", "long", Decimal("2000"), Decimal("1999"), Decimal("2001"), Decimal("100"), CFG, FEES["hyperliquid_perp"], T0)
    hist = historial({"ETH": [(ms(T0) + HORA_MS, Decimal("0.0001")), (ms(T0) + 2 * HORA_MS, Decimal("0.0001"))]})
    assert td.cobrar_funding(hist, pos, Decimal("2000"), T0 + timedelta(hours=3)) == Decimal("-0.4")
    pos.lado = "short"
    pos.ultimo_funding_ms = ms(T0)
    pos.funding_usd = Decimal("0")
    assert td.cobrar_funding(hist, pos, Decimal("2000"), T0 + timedelta(hours=3)) == Decimal("0.4")


def test_regla_de_descarte_tendencia():
    estado = td.nuevo(CFG, T0)
    assert td.regla_descarte(estado, CFG, T0 + timedelta(days=10))["estado"] == "en_prueba"
    # 60 días sin operar: descartada (no hay esperanza que mostrar).
    r = td.regla_descarte(estado, CFG, T0 + timedelta(days=60))
    assert r["estado"] == "descartada" and r["motivos_si_hoy"] == ["sin_operaciones"]
    # 20 cierres con esperanza neta negativa: descartada antes de los 60 días.
    estado.operaciones_cerradas = 20
    estado.realizado.bruto_usd = Decimal("10")
    estado.realizado.costos.comisiones_usd = Decimal("12")
    r = td.regla_descarte(estado, CFG, T0 + timedelta(days=5))
    assert r["estado"] == "descartada" and r["esperanza_neta_por_operacion_usd"] == "-0.100000"
    assert r["progreso_pct"] == "100.0"
    # Esperanza positiva: pasa.
    estado.realizado.bruto_usd = Decimal("100")
    assert td.regla_descarte(estado, CFG, T0 + timedelta(days=5))["estado"] == "pasa"
    # 19 cierres y 10 días: todavía en prueba.
    estado.operaciones_cerradas = 19
    assert td.regla_descarte(estado, CFG, T0 + timedelta(days=10))["estado"] == "en_prueba"


def test_necesita_velas_solo_tras_cierre_de_4h():
    estado = td.nuevo(CFG, T0)
    assert td.necesita_velas(estado, "ETH", T0)
    estado.ultima_vela_4h["ETH"] = "2026-11-01T00:00:00.000000Z"
    assert not td.necesita_velas(estado, "ETH", T0 + timedelta(hours=7, minutes=55))
    assert td.necesita_velas(estado, "ETH", T0 + timedelta(hours=8))
