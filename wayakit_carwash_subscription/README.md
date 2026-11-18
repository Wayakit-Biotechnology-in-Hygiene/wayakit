# Car Wash Subscriptions (KSA)

**Flujo:** cuando se paga una factura de suscripción (web o backend), `_invoice_paid_hook` crea un **periodo** con lo siguiente. Las fechas del periodo son las que factura Subscriptions (`deferred_start_date`/`deferred_end_date` de la línea; con plan mensual son meses calendario). `cw_period_days` (30) solo se usa si la línea no trae esas fechas:
- **Servicios incluidos**: un cupón (`loyalty.card`) por tipo de servicio, cuyos puntos son el número de lavadas. Expira al final del periodo, así que no hay rollover.
  - Cada vez que se aplica el código en una orden (backend o website) se canjea **una** lavada: la línea de recompensa lleva cantidad 1 y descuenta 1 punto (8 → 7 al confirmar). Para una segunda lavada en la misma orden no sirve volver a poner el código (Odoo responde "already applied"): en backend se reclama otra vez con el botón *Promotions*. En el website es una lavada por orden: el carrito ya no muestra el aviso "Claim" de un cupón de Car Wash que ya está aplicado.
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

> El correo del cupón (plantilla `mail_template_cw_coupon`) sale del plan de comunicación "At creation" del programa. Al crear un programa *Coupons* por código (como hace el script) Odoo **no** lo agrega solo, porque eso pasa por un onchange de la UI. El script lo agrega; si creas programas a mano, revisa la pestaña Communications.

## Página y correo (v17.0.1.2.0, correo rediseñado en 1.2.1)
- **Página** `/car-wash-subscriptions`, solo en la web **"KSA Website"** (se busca por nombre exacto, nunca por id; la amarra `website._cw_bind_page` en cada instalación/actualización). Si al instalar no hay exactamente una web con ese nombre, la página queda **sin publicar** y sale un warning en el log. Es `noupdate`: después de instalarla se edita y se traduce al árabe en el editor del website (*Website › Translate*), y un `-u` no pisa esos cambios. Para mandar contenido nuevo desde git hace falta una migración.
  - Botones **Subscribe**: van al `website_url` de los productos `FP-CWS-00103/00104/00203/00204/00105/00205` (compañía 1). Si el producto no existe, no está publicado o está en otra web, el botón no se muestra.
  - **Book your wash** (`#book`): va a la cita publicada que se llame exactamente **"Car Wash Subscription"** (web KSA o sin web). Si no existe, va a `/appointment/1` (Car Wash Care).
  - Los botones Subscribe y Book your wash son "vivos" y están bloqueados en el editor (`o_not_editable`): si se editaran, Odoo guardaría el link fijo y dejaría de ocultarlos. El resto del texto sí se edita. Para agregar bloques nuevos están las dos zonas vacías (después del hero y antes del FAQ).
  - Las fotos son adjuntos de prod de la página `/wayakit-car-wash-service` (`/web/image/<id>-…`): si se borran de la biblioteca de medios, aquí se rompen. En una BD de prueba no existen.
  - El texto de la página y del correo dice "3 meses seguidos" fijo. Si cambia `cw_streak_periods` de un producto, hay que cambiar ese texto a mano.
  - Si se renombra la web KSA después de instalar, la página se queda donde está; el nombre solo se usa para la primera vez.
- **Correo del cupón**: plantilla `mail_template_cw_coupon` (`noupdate`, se puede editar en *Ajustes › Técnico › Plantillas de correo*). Muestra nombre, servicio, código, lavadas, vencimiento y tipo (las recompensas son transferibles), y lleva el botón **Book your wash** → `<dominio de la web KSA>/car-wash-subscriptions#book`. En staging la web no tiene dominio, así que el link apunta a staging. No lleva código de barras.
  - **Sin el layout de Odoo (v17.0.1.2.1):** los cupones de Car Wash (con periodo `cw_period_id`, o de un programa cuyo correo "At creation" es `mail_template_cw_coupon`, como los creados a mano con *Generate coupons*) mandan la plantilla tal cual, sin el encabezado "Your Loyalty Coupon" ni el pie "Powered by Odoo"; la plantilla trae su propio encabezado (logo oficial `static/src/img/WAYAKIT-AR_B-horizontal_white.png` sobre banda navy) y pie (nombre, ciudad y correo tomados de la compañía del programa). El contacto por teléfono es un enlace directo a WhatsApp Business KSA (`https://wa.me/966545754908`); el número se cambia sin tocar código con el parámetro de sistema `wayakit_carwash_subscription.whatsapp_number`. Aplica al correo de creación y al botón *Send* del cupón (que abre el compositor en modo `mass_mail`). Los demás programas de lealtad no cambian.
  - En *Send* el cuerpo se ve como código de la plantilla (se rellena al enviar): enviarlo sin editarlo. Se envía aunque el correo del cliente esté en la lista negra de newsletters.
  - Al actualizar a 1.2.1, `migrations/17.0.1.2.1/post-migrate.py` recarga **solo** esta plantilla desde el XML (y borra sus traducciones del cuerpo): cualquier edición hecha en la UI a la plantilla se pierde (a la 1.2.0 no había ninguna).
  - Los programas "CW Sub – …" de la compañía 1 que todavía usan el correo genérico "Coupon: Coupon Information" se cambian al nuevo solos: en una instalación nueva, al instalar; al actualizar desde ≤ 1.1.2, en `migrations/17.0.1.2.0/post-migrate.py`. Si alguien eligió a mano otra plantilla, se respeta. El script `setup_coupon_programs.py` usa la nueva plantilla y hace el mismo cambio.

**A mano (Mafer, en la UI, primero en staging y después en prod):**
1. Crear la cita **"Car Wash Subscription"**: duplicar "Car Wash Care" (id 1) con el mismo personal y calendario, para que los horarios no se encimen. Quitar la pregunta "Payment Method", agregar la pregunta obligatoria "Subscription code", renombrarla exactamente `Car Wash Subscription` y publicarla en la web KSA.
2. Publicar los 6 productos de suscripción en la web KSA. Mientras no estén publicados, la página no muestra sus botones Subscribe.
3. Revisar la página en el editor y traducirla al árabe.
4. Opcional: agregar `/car-wash-subscriptions` al menú de la web KSA (el módulo no crea menú).

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
- Deep clean: el link de reserva en el correo del cupón ya está desde la 1.2.0 (botón "Book your wash" → cita "Car Wash Subscription").
