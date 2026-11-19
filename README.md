# mesa-trading

Mesa de trading 24/7 con **dinero simulado**. No hay broker real, ni llaves de exchange, ni llaves privadas.

**Gambler** y **House** corren en **simulado**: `SimBroker` abre y cierra una sola posición con dinero ficticio, después de comisiones y slippage. En vivo no hay una vela que abra después de la decisión, así que la orden de mercado cruza el libro de ese ciclo (compra al ask, vende al bid) y encima paga impacto y comisión. Si el backtest sí trae esa vela, el fill sigue siendo su apertura. **Spy** está apagado (`bots.spy.enabled=false`) y en sombra. No hay modo real. El reporte recomienda un veredicto; no cambia el modo. El no-go por drawdown usa el peor pico-a-valle, no el hueco que quede si el capital ya rebotó.

## Cómo se mueve un ciclo

1. El reloj dispara el job `mesa-trading-tick` cada 5 minutos.
2. `state/` arma una foto chica del mercado (sin ver el futuro).
3. `filter/` tira lo que no vale la pena.
4. `mesa-trading-judge` pregunta a Jev `jev-1.14.0` (una llamada por batería). Si no hay llave, si se vence el tiempo o si se pasa el tope de US$5 al mes, la mesa hace hold.
5. `policy/` aplica umbrales. `risk/` puede vetar. Nada de eso lo salta el modelo.
6. En sombra no hay orden. En simulado, `SimBroker` es la única implementación de `Broker`.
7. Todo queda en la bitácora. A las 07:00 hora de la Ciudad de México, `mesa-trading-report` publica el resumen y la calibración.

## API de solo lectura

- `GET /health` → 200, `"broker": "simulado"`
- `GET /estado`
- `GET /reportes`
- `GET /reportes/ultimo.json`
- `GET /reportes/diario/{fecha}.json`
- `GET /reportes/calibracion/{fecha}.json`

## PnL bruto vs. neto y costos

Medición en `src/mesa_trading/reports/costos.py`. No cambia umbrales ni tamaño.

- El reporte diario trae `costos` en el JSON y una sección en el Markdown: últimas 24 h, por día (CDMX), acumulado, por estrategia (house, gambler) y por tipo de mercado (DEX vs. perps). Para cada uno muestra el PnL bruto, las comisiones, el deslizamiento estimado, el gas y el PnL neto.
- Por estrategia y por señal (estrategia/mercado/lado) muestra el número de operaciones, el tamaño promedio, el bruto promedio y el costo promedio, en USD y en bps. También dice si la señal cubre sus costos.
- `GET /estado` → `costos.acumulado_libro` sale en vivo de `book/state.json`; su `por_tipo_mercado` parte comisiones, deslizamiento y gas entre `dex` y `perps` (cuenta desde #29, por pata). `costos.por_tipo_mercado` (24 h y acumulado) y lo demás vienen del último reporte (`reportes/costos/resumen.json`).
- El estado acumulado vive en `reportes/costos/estado.json`. La primera corrida sin ese archivo relee la bitácora desde el inicio del libro, con un máximo de 60 días.
- Modelo de comisiones: `comision = max(nocional × taker_bps / 10000, min_fee_usd)` por pata, más `gas_usd` fijo por pata, con la tabla de `config/fees.yaml` que cada mercado elige con `fee_schedule` en `config/desk.yaml`. Cada tabla cita su `fuente`.
  - Perps de Hyperliquid (`hyperliquid_perp`): 4.5 bps por pata, sin gas.
  - Pool WETH/USDC (`uniswap_v3_weth_usdc_005`): 5 bps por pata, el tier del pool `0x88e6…5640` (Uniswap v3, 0.05%), y 0.05 USD de gas por pata. La tabla genérica `dex_pool` (30 bps) sigue en el archivo, pero ningún mercado la usa.
- El deslizamiento se cobra una sola vez: va dentro del precio del fill (compra más caro, vende más barato). El libro lo anota en `slippage_usd` como desglose, pero no lo resta otra vez. Desde #29, `pnl_libro` = neto en cada cierre.
- `bruto` se mide desde el precio del fill sin deslizamiento. Si el fill cruzó el libro, ese precio es el bid/ask, así que el medio spread queda dentro del bruto.
- Historial: el libro **no se reescribe**. Los cierres de antes de #29 restaron el deslizamiento dos veces y no cobraron gas. Ese monto queda congelado en `slippage_double_counted_legacy_usd` (en la primera escritura con la contabilidad nueva) y el reporte lo muestra como `deslizamiento_doble_contado_usd`: neto = `pnl_libro` + ese monto. Los cierres nuevos llevan `contabilidad: 2` en la bitácora.
- `recalculo_tabla_actual` es un reporte aparte: vuelve a calcular las operaciones guardadas con el deslizamiento una vez, la comisión de la tabla de hoy (p. ej. 5 bps en el pool) y el gas, y separa el efecto de cada corrección. No toca el libro.
- La sección "qué habría pasado" es solo un reporte. Vuelve a filtrar las operaciones pasadas con umbrales de entrada más altos y reescala el tamaño ×2 y ×4 (el gas por pata no escala).

## Desarrollo

```bash
pip install -e ".[dev]"
pytest
```

La configuración está en `config/desk.yaml` y `config/promotion.yaml`. Las fuentes y sus límites están en `docs/fuentes.md`.

## Carga de staging y veredicto

Universo: BTC, ETH, SOL, DOGE y XRP perp en Hyperliquid, más el pool WETH/USDC en GeckoTerminal. Coinbase sigue apagado.

Filtros en `filters` (los libros reales quedan muy por debajo):

| Parámetro | Valor | Por qué |
| --- | --- | --- |
| `max_spread_bps` | 25 | BTC en Hyperliquid ~0.1 bp; el pool sintético ~10 bp. 25 deja pasar ambos y corta un libro roto. |
| `min_liquidity_usd` | 10000 | Esos libros andan en cientos de millones. 10000 solo tira polvo. |
| `max_vol` | 0.05 | La vol realizada de 5 min anda en ~0.002. 0.05 es un libro disparado. |
| `max_fee_fraction` | 0.02 | La comisión mínima no puede comerse más del 2% del tamaño. |

`/estado` lee solo `book/state.json` (el ciclo anota ahí `last_cycle_id`). No lista la bitácora.

El resumen diario trae el embudo (vistos → en banca → rechazados por paso y por motivo → juez → decisiones → operaciones simuladas), el gasto real de Jev y un techo estimado.

Tope de Jev: **0.15 USD/día** y **5 USD/mes**. Techo si los 6 mercados llegan al juez en cada ciclo de 5 min, a 800 tokens de entrada (un snapshot real de BTC midió ~450): **~0.06 USD/día**, ~1.80 USD/mes. Si el prompt real es más caro, el tope diario corta antes.

Veredicto (`veredicto.estado`), solo con dinero simulado:

- **insuficiente** hasta 200 cierres y 14 días.
- **go_tentativo** si, con esa muestra, el PnL después de comisiones y slippage es > 0 y el skill de Brier de las preguntas de política es ≥ 0.02. No autoriza dinero real.
- **no_go** si, ya con la muestra, el PnL no es positivo o el skill es ≤ 0. También si el drawdown pasa el 15% del capital antes.
- Con una sola posición, ~10 cierres al día juntan la muestra en ~20 días; ~2 al día, en ~100. El reporte calcula el ritmo observado.

## Staging

Proyecto `tangerine-staging-510423`, región `northamerica-south1`. El despliegue es `scripts/staging-apply.sh` con Workload Identity (cuenta `sa-forge-staging`), sin llaves JSON. Terraform no carga el valor de `mesa-trading-jev-key`: si el secreto está vacío, `/health` del juez responde `"jev_key": "missing"` y la mesa hace hold.
