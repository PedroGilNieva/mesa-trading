import json

from fastapi.testclient import TestClient

from mesa_trading.api.app import create_app
from mesa_trading.storage import LocalRoot


def test_estado_reads_the_book_and_does_not_list_logs(tmp_path):
    root = LocalRoot(tmp_path)
    root.write_text(
        "book/state.json",
        json.dumps(
            {
                "state": {
                    "cash_usd": "1000",
                    "day_start_equity_usd": "1000",
                    "day_start_utc": "2026-11-12",
                    "peak_equity_usd": "1000",
                    "last_cycle_id": "20261112T171500Z",
                    "last_cycle_at": "2026-11-12T17:15:00.000000Z",
                    "closed_trades": 3,
                    "realized_pnl_usd": "-1.25",
                    "day_realized_pnl_usd": "-1.25",
                    "fees_usd": "0.40",
                    "slippage_usd": "0.05",
                    "recent_fills": [
                        {"bot": "house", "market": "WETH-USDC", "action": "quote", "reason": "cotizacion_llenada"}
                    ],
                    "resting_order": {"client_order_id": "q1", "market": "WETH-USDC"},
                }
            }
        ),
    )

    class Exploding(LocalRoot):
        def list_prefix(self, prefix: str) -> list[str]:
            raise AssertionError(f"no hay que listar {prefix}")

        def read_text(self, key: str) -> str | None:
            if key.startswith("logs/"):
                raise AssertionError(key)
            return super().read_text(key)

    client = TestClient(create_app(Exploding(tmp_path)))
    response = client.get("/estado")
    assert response.status_code == 200
    body = response.json()
    assert body["ultimo_ciclo"] == "20261112T171500Z"
    assert body["ultimo_ciclo_en"] == "2026-11-12T17:15:00.000000Z"
    assert body["operaciones_cerradas"] == 3
    assert body["pnl_realizado_usd"] == "-1.25"
    assert body["pnl_no_realizado_usd"] == "0"
    assert body["equity_usd"] == "1000"
    assert body["pnl_dia_usd"] == "-1.25"
    assert body["ultimos_fills"][0]["bot"] == "house"
    assert body["ultimos_fills"][0]["reason"] == "cotizacion_llenada"
    assert body["broker"] == "simulado"
    assert body["posicion_abierta"] is None
    assert body["resting_order"]["client_order_id"] == "q1"
    assert body["resting_order"]["market"] == "WETH-USDC"


def test_estado_marks_a_short_to_market(tmp_path):
    root = LocalRoot(tmp_path)
    root.write_text(
        "book/state.json",
        json.dumps(
            {
                "state": {
                    "cash_usd": "900",
                    "day_start_equity_usd": "1000",
                    "day_start_utc": "2026-11-17",
                    "peak_equity_usd": "1000",
                    "last_prices": {"BTC-PERP": "90"},
                    "position": {
                        "market": "BTC-PERP",
                        "side": "short",
                        "qty": "1",
                        "entry_price": "100",
                        "notional_usd": "100",
                    },
                }
            }
        ),
    )
    body = TestClient(create_app(root)).get("/estado").json()
    assert body["pnl_no_realizado_usd"] == "10"
    assert body["equity_usd"] == "1010"
    assert body["posicion_abierta"]["side"] == "short"
