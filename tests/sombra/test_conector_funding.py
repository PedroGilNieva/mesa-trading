import json
from datetime import timedelta
from decimal import Decimal

from mesa_trading.connectors.live import HyperliquidInfoConnector
from mesa_trading.http_allow import HttpClient


VELA_T = {"4h": 1_700_006_400_000, "5m": 1_700_000_300_000}


class Transporte:
    def __init__(self):
        self.cuerpos = []

    def __call__(self, method, url, headers, body, timeout):
        assert (method, url) == ("POST", "https://api.hyperliquid.xyz/info")
        req = json.loads(body)
        self.cuerpos.append(req)
        if req["type"] == "fundingHistory":
            return 200, json.dumps([
                {"coin": "ETH", "fundingRate": "0.0000125", "premium": "0", "time": 1_700_003_600_000},
                {"coin": "ETH", "fundingRate": "-0.00001", "premium": "0", "time": 1_700_000_000_000},
            ]).encode()
        if req["type"] == "metaAndAssetCtxs":
            return 200, json.dumps([{"universe": [{"name": "ETH"}]}, [{"markPx": "2001", "oraclePx": "2000", "funding": "0.00001"}]]).encode()
        if req["type"] == "candleSnapshot":
            # Un `t` distinto por intervalo: el test no pasa por compartir el inicio.
            t = VELA_T[req["req"]["interval"]]
            return 200, json.dumps([{"t": t, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "3"}]).encode()
        raise AssertionError(req)


def test_funding_history_y_contexto_por_info_sin_llaves():
    t = Transporte()
    hyper = HyperliquidInfoConnector(HttpClient(transport=t))
    filas = hyper.funding_history("ETH", 1, 2)
    assert filas == [(1_700_000_000_000, Decimal("-0.00001")), (1_700_003_600_000, Decimal("0.0000125"))]
    assert t.cuerpos[0] == {"type": "fundingHistory", "coin": "ETH", "startTime": 1, "endTime": 2}
    ctx = hyper.asset_context("ETH")
    assert (ctx["mark"], ctx["oracle"]) == ("2001", "2000")
    assert hyper.asset_context("DOGE") is None


def test_velas_de_4h_cierran_a_las_4h():
    hyper = HyperliquidInfoConnector(HttpClient(transport=Transporte()))
    vela = hyper.candles("ETH", "4h")[0]
    assert vela["close_ts"] - vela["ts"] == timedelta(hours=4)
    vela_5m = hyper.candles("ETH", "5m")[0]
    assert vela_5m["ts"] != vela["ts"]
    assert vela_5m["close_ts"] - vela_5m["ts"] == timedelta(minutes=5)
