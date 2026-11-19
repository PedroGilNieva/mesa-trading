# Estrategias en sombra: `carry` y `tendencia` (#31)

Dos estrategias en **papel**, con libro y reporte aparte de house/gambler. No hay
órdenes, broker ni llaves: el código vive en `src/mesa_trading/sombra/` y no
importa `broker/factory` ni escribe `book/state.json`. Corren al final de cada
tick (`mesa-tick`), después del ciclo de la mesa; si fallan, el ciclo ya terminó y
solo queda un aviso en stderr.

Configuración: `sombra:` en `config/desk.yaml`. Capital teórico: 10,000 USD cada
una (`capital_usd`). No cambia house/gambler, `promotion.yaml`, los umbrales GO ni
los topes de `risk` (se leen, no se tocan).

## Datos

- Funding: Hyperliquid `POST /info` con `{"type": "fundingHistory"}` (mismo host
  y ruta de la lista blanca). Hyperliquid paga funding cada hora; anualizado =
  tasa horaria × 8760. Se guarda en `sombra/funding/historial.json` (120 días),
  se pide solo cuando ya debe existir una hora nueva.
- Precio spot: `oraclePx` de `metaAndAssetCtxs` (índice spot de Hyperliquid).
  Precio perp: medio del `l2Book`.
- Velas 4h y 1D: `candleSnapshot`, solo cuando cerró una vela de 4h nueva.

## Contabilidad (las dos)

- **bruto**: movimiento de precio a precios medios.
- **costos**: comisión (`config/fees.yaml`) + deslizamiento (medio spread +
  impacto + extra) + gas, por pata. El deslizamiento se cobra una vez, como costo.
- **funding**: cobrado (+) o pagado (−) por la pata perp.
- **neto** = bruto + funding − costos.
- Drawdown máximo: peor pico-a-valle del equity a mercado, en USD y %.

## `carry`: spot largo + perp corto

- Nocional por pata: `nocional_pct` (25%) del equity del libro de carry; no puede
  pasar de `risk.max_position_pct`.
- Pata spot: ETH usa el pool `uniswap_v3_weth_usdc_005` (5 bps + 0.05 USD de gas);
  BTC no tiene pool en la mesa y usa la tabla genérica `dex_pool` (30 bps + gas),
  un supuesto conservador. Spread spot supuesto: 10 bps. Pata perp: `hyperliquid_perp`.
- **Abre** si `funding anualizado esperado − costos de ida y vuelta anualizados >
  umbral_neto_anual` (5%). Esperado = media de las últimas 72 h × 8760. Los costos
  de las dos patas, entrada y salida, se reparten en `horizonte_dias` (30). No
  abre si el funding medio de las últimas 24 h es negativo.
- **Cierra** si el funding medio de las últimas 24 h es negativo, o si la base
  `(perp − spot)/spot` se aleja más de `limite_base` (1%) de la de entrada.
- Funding: por cada hora nueva, `qty × spot × tasa` (spot del tick en que se anota).
- **Descarte**: tras 30 días, si el neto (realizado + abierto) ≤ 0 o el funding fue
  positivo menos del 60% de las horas (ETH y BTC juntos), se marca descartada,
  cierra lo abierto y no abre más.

## `tendencia`: ETH y BTC, 4h y 1D

Señal estándar, sin ajustar a los datos:

- Régimen (1D): último cierre diario vs. SMA de 50 cierres. Arriba = solo largos;
  abajo = solo cortos.
- Entrada (4h): el cierre de la última vela de 4h rompe el máximo (largo) o el
  mínimo (corto) de las 20 velas anteriores (~3.3 días).
- Stop inicial: 2 × ATR(14) diario. Se revisa en cada tick con el medio del perp.
- Salida (4h): el cierre rompe el mínimo/máximo de las 10 velas anteriores, o el
  régimen deja de acompañar.
- **Tamaño por riesgo**: `nocional = 0.5% × equity / (distancia al stop / precio)`,
  con tope `risk.max_position_pct` (25%) por posición y `risk.max_exposure_pct`
  (25%) en total.
- **Veto de economía**: `expected_edge_bps` (100, supuesto como los de
  `policy.yaml`) tiene que superar comisión + deslizamiento + gas de ida y vuelta.
- **Descarte**: tras 20 operaciones cerradas o 60 días (lo primero), descartada si
  la esperanza neta por operación ≤ 0 (o si no hubo operaciones).

## Dónde se ve

- `GET /estado` → `sombra`: lee un solo objeto (`sombra/resumen.json`): por
  estrategia, capital, equity, bruto/funding/costos/neto (total y realizado),
  operaciones, posiciones abiertas, drawdown máximo y progreso de la regla de
  descarte; y `funding_persistencia` (% de horas con funding > 0 y anualizado
  medio, 30 y 90 días, ETH y BTC).
- Reporte diario: `sombra` en el JSON y la sección "Estrategias en sombra" en el
  Markdown, con los cierres de las últimas 24 h.
- Estado completo: `sombra/carry/state.json`, `sombra/tendencia/state.json`
  (últimos 200 cierres con su desglose).
