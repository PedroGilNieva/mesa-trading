from datetime import timedelta
from decimal import Decimal

from mesa_trading.broker.fees import load_fees
from mesa_trading.sombra import carry
from mesa_trading.sombra.funding import HORA_MS, ms
from mesa_trading.sombra.libro import costo_pata, spread_bps

from tests.helpers import desk
from tests.sombra.fakes import T0, historial, horas

FEES = load_fees()
CFG = desk().sombra.carry
PCT = desk().risk.max_position_pct


def px(spot="2000", bid="2000.9", ask="2001.1"):
    return {"spot": Decimal(spot), "bid": Decimal(bid), "ask": Decimal(ask)}


def _costo_ida(nocional, bid, ask):
    spot = costo_pata(nocional, FEES["uniswap_v3_weth_usdc_005"], CFG.spread_spot_bps, CFG.liquidez_supuesta_usd, CFG.extra_bps)
    perp = costo_pata(nocional, FEES["hyperliquid_perp"], spread_bps(Decimal(bid), Decimal(ask)), CFG.liquidez_supuesta_usd, CFG.extra_bps)
    return spot.sumar(perp)


def test_contabilidad_funding_costos_base_y_neto():
    estado = carry.nuevo(CFG, T0)
    abrir_en = T0 + timedelta(minutes=5)
    p = px()
    pos = carry.abrir(
        estado, "ETH", Decimal("2500"), p["spot"], p["bid"], p["ask"], CFG,
        FEES["uniswap_v3_weth_usdc_005"], FEES["hyperliquid_perp"], Decimal("0.1"), abrir_en,
    )
    assert pos.qty == Decimal("1.25")
    assert pos.base_entrada == Decimal("0.0005")
    # La hora de T0 es anterior a la apertura: no se cobra. Luego +, +, +, -.
    filas = [(ms(T0), Decimal("0.001"))] + [
        (ms(T0) + i * HORA_MS, Decimal(r)) for i, r in enumerate(["0.0001", "0.0001", "0.0001", "-0.00005"], start=1)
    ]
    hist = historial({"ETH": filas})
    cobrado = carry.cobrar_funding(hist, pos, Decimal("2000"), T0 + timedelta(hours=5))
    # 1.25 × 2000 × (3 × 0.0001 - 0.00005) = 0.625
    assert cobrado == Decimal("0.625")
    assert pos.horas_funding == 4 and pos.horas_positivas == 3
    # Otra vez en el mismo instante: no cobra dos veces.
    assert carry.cobrar_funding(hist, pos, Decimal("2000"), T0 + timedelta(hours=5)) == 0

    cierre = carry.cerrar(
        estado, "ETH", Decimal("2010"), Decimal("2010.9"), Decimal("2011.1"), CFG,
        FEES["uniswap_v3_weth_usdc_005"], FEES["hyperliquid_perp"], "prueba", T0 + timedelta(hours=6),
    )
    # Spot +10 y perp corto -10: la base no cambió → bruto 0.
    assert Decimal(cierre["bruto_base_usd"]) == 0
    entrada = _costo_ida(Decimal("2500"), "2000.9", "2001.1")
    salida = _costo_ida(Decimal("1.25") * Decimal("2010"), "2010.9", "2011.1")
    costos = entrada.sumar(salida)
    assert estado.realizado.costos == costos
    assert estado.realizado.costos.gas_usd == Decimal("0.10")  # gas del pool por pata spot
    assert estado.realizado.funding_usd == Decimal("0.625")
    assert estado.realizado.neto == Decimal("0.625") - costos.total
    assert Decimal(cierre["neto_usd"]) == (Decimal("0.625") - costos.total).quantize(Decimal("0.000001"))
    assert estado.operaciones_cerradas == 1 and estado.posiciones == {}


def test_entrada_solo_si_funding_menos_costos_supera_umbral():
    now = T0 + timedelta(days=4)
    nocional = Decimal("2500")
    args = (FEES["uniswap_v3_weth_usdc_005"], FEES["hyperliquid_perp"], Decimal("1"), now)
    alto = historial({"ETH": horas(now - timedelta(hours=72), 72, "0.00002")})
    ev = carry.evaluar_entrada(alto, "ETH", nocional, CFG, *args)
    assert ev["abre"] is True
    assert Decimal(ev["funding_anual_esperado"]) == Decimal("0.175200")
    assert Decimal(ev["neto_anual"]) == Decimal(ev["funding_anual_esperado"]) - Decimal(ev["costo_anual"])
    # Funding anual ~4.4% no cubre ~4.4% de costos anualizados + 5% de umbral.
    bajo = historial({"ETH": horas(now - timedelta(hours=72), 72, "0.000005")})
    assert carry.evaluar_entrada(bajo, "ETH", nocional, CFG, *args)["abre"] is False
    assert carry.evaluar_entrada(None, "ETH", nocional, CFG, *args)["motivo"] == "sin_historial"


def test_paso_abre_cierra_por_funding_negativo_sostenido():
    now = T0 + timedelta(days=4)
    filas = horas(now - timedelta(hours=72), 73, "0.00003")
    estado = carry.nuevo(CFG, now)
    carry.paso(estado, historial({"ETH": filas}), {"ETH": px()}, CFG, FEES, PCT, now)
    assert set(estado.posiciones) == {"ETH"}
    # 25% del equity por pata.
    assert estado.posiciones["ETH"].nocional_usd == Decimal("2500")
    later = now + timedelta(hours=25)
    negativas = horas(now + timedelta(hours=1), 25, "-0.00001")
    carry.paso(estado, historial({"ETH": filas + negativas}), {"ETH": px()}, CFG, FEES, PCT, later)
    assert estado.cierres[-1]["motivo"] == "funding_negativo_sostenido"
    assert "ETH" not in estado.posiciones  # y no reabre: la media de 72 h ya no alcanza
    assert estado.drawdown.max_drawdown_usd > 0


def test_cierra_si_la_base_se_desvia():
    estado = carry.nuevo(CFG, T0)
    p = px()
    pos = carry.abrir(
        estado, "ETH", Decimal("2500"), p["spot"], p["bid"], p["ask"], CFG,
        FEES["uniswap_v3_weth_usdc_005"], FEES["hyperliquid_perp"], Decimal("0.1"), T0,
    )
    assert carry.debe_cerrar(None, pos, Decimal("2000"), Decimal("2010"), CFG, T0) is None
    assert carry.debe_cerrar(None, pos, Decimal("2000"), Decimal("2030"), CFG, T0) == "base_desviada"


def test_regla_de_descarte_carry():
    estado = carry.nuevo(CFG, T0)
    positivo = historial({"ETH": horas(T0, 31 * 24, "0.0001"), "BTC": horas(T0, 31 * 24, "0.0001")})
    # Antes de 30 días: en prueba aunque el neto sea 0.
    assert carry.regla_descarte(estado, positivo, CFG, {}, T0 + timedelta(days=10))["estado"] == "en_prueba"
    # A los 30 días con neto 0 (nunca abrió): descartada.
    r = carry.regla_descarte(estado, positivo, CFG, {}, T0 + timedelta(days=30))
    assert r["estado"] == "descartada" and r["motivos_si_hoy"] == ["neto_no_positivo"]
    # Neto positivo y funding positivo 100%: pasa.
    estado.realizado.funding_usd = Decimal("50")
    assert carry.regla_descarte(estado, positivo, CFG, {}, T0 + timedelta(days=30))["estado"] == "pasa"
    # Neto positivo pero funding positivo solo 50% de las horas: descartada.
    mitad = historial({
        "ETH": horas(T0, 31 * 24, "0.0001"),
        "BTC": horas(T0, 31 * 24, "-0.0001"),
    })
    r = carry.regla_descarte(estado, mitad, CFG, {}, T0 + timedelta(days=30))
    assert r["estado"] == "descartada" and r["motivos_si_hoy"] == ["funding_positivo_bajo_minimo"]
    assert r["funding_pct_positivo"] == "0.5000"


def test_paso_descarta_cierra_y_no_vuelve_a_abrir():
    estado = carry.nuevo(CFG, T0)
    now = T0 + timedelta(days=30)
    filas = horas(T0, 30 * 24 + 1, "-0.00001")
    carry.paso(estado, historial({"ETH": filas}), {"ETH": px()}, CFG, FEES, PCT, now)
    assert estado.descartada is True
    assert "funding_positivo_bajo_minimo" in (estado.motivo_descarte or "")
    alto = filas + horas(now + timedelta(hours=1), 80, "0.001")
    carry.paso(estado, historial({"ETH": alto}), {"ETH": px()}, CFG, FEES, PCT, now + timedelta(hours=80))
    assert estado.posiciones == {}
