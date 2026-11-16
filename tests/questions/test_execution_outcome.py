"""La etiqueta de ejecución degradada mira el spread. Sin medición no inventa 'no'."""

from decimal import Decimal

from mesa_trading.questions.catalog import realized_outcome


def _future():
    return [{"close": "100"}]


def test_execution_outcome_uses_the_spread_and_does_not_invent_no():
    assert realized_outcome("ejecucion_degradada", _future(), Decimal("100")) is None
    assert (
        realized_outcome(
            "ejecucion_degradada",
            _future(),
            Decimal("100"),
            spread_bps="10",
            liquidity_usd="5000000",
            max_spread_bps="25",
            min_liquidity_usd="10000",
        )
        == "no"
    )
    assert (
        realized_outcome(
            "ejecucion_degradada",
            _future(),
            Decimal("100"),
            spread_bps="40",
            max_spread_bps="25",
        )
        == "si"
    )
    assert (
        realized_outcome(
            "ejecucion_degradada",
            _future(),
            Decimal("100"),
            liquidity_usd="100",
            min_liquidity_usd="10000",
        )
        == "si"
    )
