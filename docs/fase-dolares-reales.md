# Borrador: fase con pocos dólares reales

Este documento no enciende nada. `real_money.enabled` está en false y el
código rechaza ponerlo en true. Hace falta un PR posterior y el OK explícito
de Pedro. No cambia `promotion.yaml` ni la regla de GO.

## Venue

Hyperliquid, el mismo que ya alimenta los perps simulados (BTC, ETH, SOL,
DOGE, XRP). El precio, el spread y el fill de la simulación salen de ahí, así
que la evidencia de simulado se puede comparar con la ejecución real.

Coinbase queda fuera: sus términos no permiten este uso y el esquema ya
rechaza ese conector y esos mercados.

No se abre cuenta, no se crea una API wallet y no se manda ninguna orden en
este PR.

## Topes duros

| Tope | Valor |
| --- | --- |
| Capital total en la cuenta de la fase | ≤ 100 USD |
| Nocional por operación | ≤ 25 USD |
| Pérdida realizada del día | ≤ 10 USD |
| Apalancamiento | 1x, una sola posición, como hoy |

Esos tres números están en `real_money` y el validador no acepta subirlos.
El halt diario que ya existe en simulado (5% del capital) se queda. En real
se le suma el tope absoluto de 10 USD: al cruzarlo, la mesa cancela la
orden viva, cierra la posición y no vuelve a entrar hasta el día siguiente
en Ciudad de México. Un kill switch aparte, también apagado, hace lo mismo
en el acto si Pedro lo pide.

## Llaves

Solo en Secret Manager. Este PR no crea el secreto, no lo lee y no toca IAM.
Los pasos, para cuando Pedro lo decida:

1. En el proyecto de staging, crear un secreto (por ejemplo
   `mesa-hyperliquid-key`) con la clave de una API wallet de Hyperliquid
   dedicada a esta fase. La cuenta de esa wallet no debe tener más de los
   100 USD de arriba.
2. Dar `roles/secretmanager.secretAccessor` sobre ese secreto únicamente a
   la cuenta de servicio que ya corre el tick. Ningún otro rol, y nada a
   nivel de organización.
3. El proceso la leería en el momento de la orden, con el mismo patrón que
   `GcpSecretKey`, y no la escribiría en el env del repo, en la imagen ni
   en los logs.
4. Rotarla o borrarla es borrar la versión del secreto. No hace falta
   redeploy para dejar de poder firmar si se deshabilita la versión.

## Flag

`real_money.enabled: false`. Si alguien lo pone en true, `load_desk` falla
antes de arrancar. Encenderlo es otro PR, después del OK de Pedro, que
además tendría que implementar el broker. Este borrador no lo trae.

## Evidencia antes de encender

Independiente del veredicto GO de `promotion.yaml`, que no autoriza dinero
real y no se modifica aquí:

- Varios días de simulado con la version 3, con fills de verdad (mercado y
  límite que descansa hasta el tick siguiente) visibles en `/estado`
  (`ultimos_fills`, PnL realizado y del día).
- El kill switch y el halt de 10 USD probados en simulado: cierran y no
  reabren el mismo día.
- Drawdown y pérdida diaria dentro de los topes de arriba durante esa
  muestra. Una racha roja no se “compensa” cambiando el modo.
- OK explícito de Pedro por escrito, en el PR que baje el flag. Sin eso,
  el flag sigue en false.
