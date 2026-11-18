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
`RealMoneyConfig` está congelada: no se puede reasignar `enabled` después
de cargar. El capital que usaría la fase es `min(capital_usd, 100)`. La
mesa de papel de staging sigue en 1000 USD; no se le aplica este tope.

El nocional que se mandaría ya descuenta comisión y slippage. Tiene que
caber en 25 USD, en los 100 USD de capital y en el saldo. Si no cabe, se
recorta; no se manda un tamaño que el costo empuje por encima del tope.

El halt del 5% del simulado no cambia. La fase suma un tope de 10 USD
que junta el realizado del día y el no realizado de la posición abierta.
Al cruzarlo se cancela la orden viva —solo si el venue confirma— y se
aplana con `reduce_only`. No se reabre hasta la medianoche de Ciudad de
México, la misma que calcula el ciclo. Cada aplanado lleva cloid propio
(fecha y nonce). Repetir el mismo intento no vuelve a cruzar; un segundo
aplanado no cierra con el fill del primero.

Si al arrancar el venue tiene posición, el libro se pone a la par y se
aplana. Si el venue ya está plano, no se manda una orden ni se inventa un
fill. El cloid y el latch del kill se guardan en el libro, con la misma
escritura condicionada: si GCS responde 412, no se publica el ciclo como
terminado y no sale la orden.

## Kill sin esperar un tick de decisión

Objeto `book/kill` en el mismo bucket que `book/state.json`. Cualquier
contenido no vacío lo prende. El proceso lo lee al empezar cada operación,
antes de cotizar. No hace falta que el juez esté a mitad de una decisión:
basta con que el proceso arranque. El latch queda en el libro hasta la
medianoche de Ciudad de México. Borrar el objeto no reabre ese día.

En simulado, ese camino aplana con `SimBroker` y una llave ficticia que
no es un secreto. No llama a Secret Manager ni a un broker real. El flag
`real_money.enabled` sigue imposible de encender, así que este proceso no
firma con la wallet real.

## Llaves

Solo en Secret Manager. Este PR no crea el secreto, no lo lee y no toca IAM.
Los pasos, para cuando Pedro lo decida:

1. En el proyecto de staging, crear un secreto (por ejemplo
   `mesa-hyperliquid-key`) con la clave de una API wallet de Hyperliquid
   dedicada a esta fase. La cuenta de esa wallet no debe tener más de los
   100 USD de arriba.
2. Dar `roles/secretmanager.secretAccessor` sobre ese secreto únicamente a
   la cuenta de servicio del tick. No a la cuenta de la API pública, no a
   ninguna otra cuenta, y nada a nivel de organización. La API solo lee
   reportes; no debe poder leer la llave.
3. Esa wallet es de la fase real. No es la del servicio simulado de
   staging y no se carga en ese proceso. El simulado no firma, no tiene
   saldo real y no comparte secreto con esta wallet.
4. El proceso la leería en el momento de la orden, con el mismo patrón que
   `GcpSecretKey`, y no la escribiría en el env del repo, en la imagen ni
   en los logs. Si Secret Manager falla o no hay versión, no se manda la
   orden: se queda la posición como está. No hay camino que opere sin llave.
5. Rotarla o borrarla es borrar la versión del secreto. No hace falta
   redeploy para dejar de poder firmar si se deshabilita la versión.

## Idempotencia

El `client_order_id` se convierte en el `cloid` de Hyperliquid (0x y 16
bytes). Un reintento manda el mismo cloid y no cruza otra vez. El segundo
intento devuelve el ack del primero.

## Flag

`real_money.enabled: false`. Si alguien lo pone en true, `load_desk` falla
antes de arrancar, y asignarlo después también falla porque el modelo está
congelado. Encenderlo es otro PR, después del OK de Pedro, que además
tendría que conectar el broker. Este borrador no lo conecta.

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
