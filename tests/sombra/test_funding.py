from datetime import timedelta
from decimal import Decimal

from mesa_trading.sombra import funding as fh

from tests.sombra.fakes import T0, FuenteFalsa, historial, horas


def test_historial_pagina_recorta_y_no_duplica():
    now = T0 + timedelta(days=10)
    filas = horas(now - timedelta(days=12), 12 * 24 + 1, "0.00001")  # la última hora es `now`
    fuente = FuenteFalsa(funding={"ETH": filas}, pagina=100)
    store = fh.actualizar_historial(None, fuente, ["ETH"], now, dias=5)
    serie = store["series"]["ETH"]
    assert len(serie) == 5 * 24 + 1  # solo la ventana de 5 días, con `now`
    assert serie[0][0] >= fh.ms(now) - 5 * 24 * fh.HORA_MS
    # Varias páginas de 100.
    assert sum(1 for c in fuente.llamadas if c[0] == "funding") >= 2
    # Menos de una hora después: no vuelve a pedir.
    fuente.llamadas.clear()
    again = fh.actualizar_historial(store, fuente, ["ETH"], now + timedelta(minutes=30), dias=5)
    assert fuente.llamadas == []
    assert again["series"]["ETH"] == serie


def test_historial_error_no_borra_lo_guardado():
    class Rota(FuenteFalsa):
        def funding_history(self, coin, start_ms, end_ms):
            raise ConnectionError("x")

    now = T0 + timedelta(days=2)
    prev = historial({"ETH": horas(T0, 24, "0.0001")})
    store = fh.actualizar_historial(prev, Rota(), ["ETH"], now, dias=30)
    assert len(store["series"]["ETH"]) == 24
    assert store["errores"] == {"ETH": "ConnectionError"}


def test_persistencia_30_y_90_dias():
    now = T0 + timedelta(days=100)
    # Últimos 30 días: 75% positivo a 0.0001/h. Días 30-90 atrás: negativo.
    filas = horas(now - timedelta(days=90), 60 * 24, "-0.00002")
    reciente = []
    for i, (t, _r) in enumerate(horas(now - timedelta(days=30), 30 * 24, "0")):
        reciente.append((t, Decimal("0.0001") if i % 4 else Decimal("-0.0001")))
    store = historial({"ETH": filas + reciente})
    p30 = fh.persistencia(store, "ETH", now, 30)
    assert p30["periodos"] == 720
    assert Decimal(p30["pct_positivo"]) == Decimal("0.75")
    # media = 0.0001 × (0.75 - 0.25) = 0.00005 por hora → × 8760.
    assert Decimal(p30["funding_anual_medio"]) == Decimal("0.438000")
    p90 = fh.persistencia(store, "ETH", now, 90)
    assert p90["periodos"] == 2160
    assert Decimal(p90["pct_positivo"]) == Decimal("0.2500")
    vacio = fh.persistencia(store, "BTC", now, 30)
    assert vacio["pct_positivo"] is None and vacio["periodos"] == 0


def test_relleno_inicial_incremental_guarda_el_avance():
    now = T0 + timedelta(days=130)
    filas = horas(now - timedelta(days=120), 120 * 24 + 1, "0.00001")  # 2881 h, la última es `now`
    fuente = FuenteFalsa(funding={"ETH": filas}, pagina=500)
    store = None
    largos = []
    for tick in range(3):
        fuente.llamadas.clear()
        store = fh.actualizar_historial(store, fuente, ["ETH"], now, dias=120, paginas=2)
        pedidas = [c for c in fuente.llamadas if c[0] == "funding"]
        assert len(pedidas) <= 2  # pocas páginas por tick
        if tick:
            # Sigue desde la última fila guardada, no desde el piso.
            assert pedidas[0][2] == largos[-1] + 1
        largos.append(int(store["series"]["ETH"][-1][0]))
        assert store["al_dia"]["ETH"] is (tick == 2)
    assert len(store["series"]["ETH"]) == 2881
    assert fh.al_dia(store, "ETH", now) and not fh.al_dia(store, "BTC", now)


def test_historial_corta_por_deadline_sin_perder_lo_bajado():
    now = T0 + timedelta(days=30)
    filas = horas(now - timedelta(days=30), 30 * 24 + 1, "0.00001")
    fuente = FuenteFalsa(funding={"ETH": filas, "BTC": filas}, pagina=500)
    restantes = iter([False, True])  # deja una llamada y corta

    store = fh.actualizar_historial(None, fuente, ["ETH", "BTC"], now, dias=30, paginas=2, agotado=lambda: next(restantes, True))
    assert [c[1] for c in fuente.llamadas] == ["ETH"]
    assert len(store["series"]["ETH"]) == 500
    assert store["series"]["BTC"] == []
    assert store["errores"] == {"ETH": "deadline", "BTC": "deadline"}
    # El próximo tick sin apuro termina el relleno desde donde quedó.
    store = fh.actualizar_historial(store, fuente, ["ETH", "BTC"], now, dias=30, paginas=2)
    assert len(store["series"]["ETH"]) == len(store["series"]["BTC"]) == 30 * 24 + 1
    assert store["errores"] == {} and store["al_dia"] == {"ETH": True, "BTC": True}
