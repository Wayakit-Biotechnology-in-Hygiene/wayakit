# Car Wash Subscriptions (KSA)

**Flujo:** cuando se paga una factura de suscripción (web o backend), `_invoice_paid_hook` crea un **periodo** con lo siguiente. Las fechas del periodo son las que factura Subscriptions (`deferred_start_date`/`deferred_end_date` de la línea; con plan mensual son meses calendario). `cw_period_days` (30) solo se usa si la línea no trae esas fechas:
- **Servicios incluidos**: un cupón (`loyalty.card`) por tipo de servicio, cuyos puntos son el número de lavadas. Expira al final del periodo, así que no hay rollover.
- **Recompensas**: un cupón válido 30 días. Como funciona con el código, es transferible.
  - Premium: el exterior gratis de cada mes (`monthly_reward`).
  - Regular: un exterior cada 3 periodos pagados seguidos (`streak_reward`). Para mantener la racha hay que pagar a más tardar 7 días después de que termina el periodo anterior. La racha sigue al contrato original (`origin_order_id`).
- **Regalo**: queda registrado en el periodo. En *Ventas › Car Wash Periods* hay un filtro "Gift pending" y un botón para marcarlo como entregado.

Si el hook falla, no bloquea el pago: deja una nota en la factura. En ese caso, para reintentar usa *Acción › Issue Car Wash entitlements*, que es idempotente.
**Ojo con reintentar tarde:** hay que re-emitir antes de que se pague el periodo siguiente. Si el siguiente ya se emitió, su racha no se recalcula: revisa la racha de los periodos posteriores y, si correspondía, emite a mano el cupón de exterior que faltó.
Si solo falla el **correo** del cupón, los cupones sí se emiten y queda una nota en el periodo para reenviarlo a mano.

## Instalación (primero en staging)
1. Instalar **Subscriptions** (`sale_subscription`). Crear el plan **Monthly**: cada 1 mes, o 30 días si el plan lo permite.
2. Instalar este módulo.
3. Correr `odoo-bin shell -d <db> < scripts/setup_coupon_programs.py`. Esto crea los 6 programas "CW Sub – …".
4. Crear los productos de suscripción: tipo servicio, "Subscription Product", con precio de plan **sin IVA** (el 15% se agrega aparte):

| Plan | Con IVA | Sin IVA |
|---|---|---|
| Regular Sedan 4 Exterior | 180 | 156.52 |
| Regular Sedan 4 Full | 220 | 191.30 |
| Regular SUV 4 Exterior | 260 | 226.09 |
| Regular SUV 4 Full | 300 | 260.87 |
| Premium Sedan | 435 | 378.26 |
| Premium SUV | 570 | 495.65 |

5. En la pestaña **Car Wash Subscription** de cada producto, configurar el tier, el regalo y las entitlements. Por ejemplo, para Premium Sedan: `included` Full ×3, `included` Deep ×1 y `monthly_reward` Exterior ×1. Para Regular Sedan 4 Full: `included` Full ×4 y `streak_reward` Exterior ×1.
   - Si se usan variantes, usar "Only for variant" para separar los programas de Sedan y SUV.
6. Publicarlos en el website.

> El correo del cupón sale del plan de comunicación "At creation" del programa. Al crear un programa *Coupons* por código (como hace el script) Odoo **no** lo agrega solo, porque eso pasa por un onchange de la UI. El script lo agrega; si creas programas a mano, revisa la pestaña Communications.

## Tests
`tests/test_carwash_subscription.py`: compra Regular Sedan, 3 periodos seguidos → exterior, pago dentro de los 7 días de gracia, pago con más de 7 días de atraso → la racha reinicia, Premium SUV → exterior mensual sin racha, cupones incluidos vencen al final del periodo, re-emisión idempotente, falla del hook o del correo sin bloquear el pago.
```
odoo-bin -c ../odoo.conf -d <db_de_prueba> -i wayakit_carwash_subscription --test-enable --test-tags /wayakit_carwash_subscription --stop-after-init
```

## Decisiones de negocio pendientes (comportamiento actual)
- **Nota de crédito o reembolso de una factura pagada:** el periodo y sus cupones siguen vigentes, y el periodo cuenta para la racha.
- **Upsell a mitad de periodo** (por ejemplo, un segundo auto): no emite cupones hasta la siguiente renovación.
- **Cambio de plan al renovar** (por ejemplo, de Sedan a SUV o de Exterior a Full): la racha se reinicia, porque se cuenta por contrato original + producto.

## Pendientes fuera del código
- Decidido (2026-11-03): **6 productos** (sin variantes), agrupados en 2 categorías de eCommerce: *Regular Care* (4) y *Premium Care* (2).
  Creados en producción (2026-11-04, KSA, servicio, 15% VAT, sin publicar):
  `FP-CWS-00103` Regular Care Sedan (4 Exterior Washes) · `FP-CWS-00104` Regular Care Sedan (4 Full Washes) ·
  `FP-CWS-00203` Regular Care SUV (4 Exterior Washes) · `FP-CWS-00204` Regular Care SUV (4 Full Washes) ·
  `FP-CWS-00105` Premium Care Sedan · `FP-CWS-00205` Premium Care SUV.
  Los cupones canjean los servicios sueltos `FP-CWS-00100/101/102` (Sedan) y `00200/201/202` (SUV).
  Falta: categorías de eCommerce y la casilla de suscripción.
- Crear el producto **Desinfectante 100 mL**, que no existe en KSA.
- Confirmar que el proveedor de pago del website soporta **tokenización**. Si no la soporta, la renovación no se cobra sola y el cliente paga desde el portal.
- Activar *Sales › Automatic Invoice*, porque sin factura pagada no se emite nada.
- Deep clean: el correo del cupón debe incluir el link de la cita "Car Wash Care".
