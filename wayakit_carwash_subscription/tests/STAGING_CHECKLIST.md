# Checklist de staging: `wayakit_carwash_subscription`

Build de odoo.sh, rama `mainTest`, compañía **KSA** (Advanced Solutions for Detergents LLC, id 1).
Para cada paso, marca la casilla y anota el resultado. Si algo falla, anota la entrada, lo esperado y lo obtenido.

> **Antes de empezar**
> - **Staging = código de la rama + copia de la BD de producción.** Todo lo que configures aquí (plan, productos, programas, Studio, acciones de servidor) **se borra en el próximo build**. Después hay que repetirlo en producción (regla de oro 1).
> - **El conector Odoo MCP apunta a PRODUCCIÓN**, no a staging. Todas las verificaciones de esta lista son **manuales en la UI de staging**.
> - En staging, odoo.sh **no envía correos reales**: aparecen en la pestaña *Mails* del build (o en *Ajustes › Técnico › Correos*, modo desarrollador).
> - Usa solo un **cliente de prueba** (p. ej. "CW Test Customer", correo `cw.test@example.com`). No uses clientes reales.
> - La neutralización de staging desactiva los proveedores de pago. Para la compra web, activa un proveedor en **modo Test** (p. ej. *Demo*, módulo `payment_demo`). No uses credenciales reales.

---

## 0. Precondiciones (README, "Pendientes fuera del código")

- [ ] 0.1 **Sales › Configuración › Ajustes › Automatic Invoice** activado. Sin la factura pagada no se emite nada, así que el paso 4 fallaría sin error visible.
- [ ] 0.2 Proveedor de pago en modo Test que soporte **tokenización** ("Guardar método de pago"). Si el proveedor real no la soporta, la renovación no se cobra sola y el cliente paga desde el portal. Anota cuál de los dos casos aplica.
- [ ] 0.3 Producto **Desinfectante 100 mL** (no existe en KSA). Si todavía no se creó en prod, crea uno temporal en staging solo para probar.
- [ ] 0.4 Existen en KSA los productos de servicio `FP-CWS-00100`, `00101`, `00102`, `00200`, `00201` y `00202`. El script del paso 2 los busca por referencia interna.
- [ ] 0.5 Decisión **6 productos vs variantes** (Dalal/Sandra). Esta lista usa productos separados. Si se usan variantes, llena "Only for variant" en las entitlements.
- [ ] 0.6 Deep clean: anota si la plantilla del correo del cupón ya incluye el link de la cita **"Car Wash Care"**. Si no lo incluye, es un pendiente; no bloquea esta prueba.

## 1. Instalación

- [ ] 1.1 *Apps* › buscar **Subscriptions** (`sale_subscription`) › Instalar (si no está instalado).
- [ ] 1.2 *Apps* › Actualizar lista de apps › buscar **Wayakit - Car Wash Subscriptions (KSA)** › Instalar. **Esperado:** instala sin error.
- [ ] 1.3 *Subscriptions › Configuración › Recurring Plans* › Nuevo: nombre **Monthly**, periodo **1 Month** › Guardar.
- [ ] 1.4 Verifica que aparezcan:
  - *Ventas › Pedidos › Car Wash Periods*, con el filtro "Active" puesto por defecto.
  - La pestaña **Car Wash Subscription** en el formulario del producto.
  - *Acción › Issue Car Wash entitlements* en facturas (lista y formulario, solo para Sales Manager).

## 2. Programas de cupones (script)

- [ ] 2.1 En odoo.sh › rama › *Shell*:
  ```
  odoo-bin shell < ~/src/user/wayakit_carwash_subscription/scripts/setup_coupon_programs.py
  ```
  **Esperado:** 6 líneas `+ created: CW Sub – …`, sin `!! product not found`.
- [ ] 2.2 Corre el script otra vez. **Esperado:** 6 líneas `= exists: …` y ningún duplicado (idempotente).
- [ ] 2.3 *Ventas › Productos › Discount & Loyalty*: hay exactamente 6 programas "CW Sub – …" (Sedan/SUV × Exterior/Full Wash/Deep Clean). Cada uno debe tener:
  - Tipo **Coupons**, compañía KSA y ninguna regla.
  - Una recompensa **Free product** con el producto `FP-CWS-…` correcto, cantidad 1 y 1 punto requerido.
  - En la pestaña **Communications**, una línea **"At creation"** con la plantilla del cupón. **Revisa los 6.** Odoo no la agrega cuando el programa se crea por código. Si falta en alguno, el cliente no recibe el correo del cupón: anótalo y agrégala a mano.

## 3. Configuración de productos

- [ ] 3.1 **Regular Sedan 4 Full**: tipo Servicio, *Subscription Product*, precio del plan Monthly **191.30** (sin IVA, más 15% de IVA = 220). En la pestaña *Car Wash Subscription*:
  - Marca *Car Wash Subscription*, Tier **Regular Care**.
  - Streak 3, grace 7, regalo **Desinfectante 100 mL** ×1.
  - Entitlements: `included` / CW Sub – Sedan Full Wash / **4**, y `streak_reward` / CW Sub – Sedan Exterior / **1**.
  - Publica el producto en el website.
- [ ] 3.2 **Premium SUV**: precio **495.65** (sin IVA, 570 con IVA). Tier **Premium Care**. Entitlements:
  - `included` / SUV Full Wash / **3**
  - `included` / SUV Deep Clean / **1**
  - `monthly_reward` / SUV Exterior / **1**

  **Esperado:** los campos Streak/grace están ocultos.
- [ ] 3.3 En Premium SUV agrega una línea `streak_reward` › Guardar. **Esperado:** ValidationError *"Premium Care cannot have streak rewards (it already gets a monthly reward)."* Descarta los cambios.
- [ ] 3.4 Variante del caso: en un producto Regular con `streak_reward`, cambia el Tier a Premium › Guardar. **Esperado:** el mismo ValidationError.

## 4. Compra web pagada en línea (camino web de `_invoice_paid_hook`)

- [ ] 4.1 Con una sesión de portal del cliente de prueba, compra **Regular Sedan 4 Full** (plan Monthly, cantidad 1). Paga con el proveedor Test y marca "guardar método de pago" si aparece.
- [ ] 4.2 En el backend, en el SO: estado de suscripción *In Progress*, factura **Paid / In Payment**.
- [ ] 4.3 Smart button **Car Wash (1)** en el SO › abre el periodo. Revisa:
  - Inicio y fin = fechas de servicio de la línea de factura (mes calendario). Payment date = fecha del pago. **Streak 1**.
  - Cupón `Included` de Sedan Full Wash con **Uses left 4**, que expira en la fecha **fin del periodo**.
  - **No** hay cupón de Streak reward.
  - Regalo Desinfectante ×1, sin entregar.
- [ ] 4.4 En el chatter del SO aparece el resumen **"Car Wash period … → … (streak 1)"**, con programa, código, usos y vencimiento, más la línea "Gift: …". **Autor del mensaje: OdooBot**, no el usuario público del website ni el cliente.
- [ ] 4.5 Correo del cupón: aparece en *Mails* de odoo.sh (o en *Ajustes › Técnico › Correos*), dirigido al cliente de prueba y con el código. **No** debe haber nota ⚠️ de correo fallido en el periodo.
- [ ] 4.6 La factura **no** tiene nota "⚠️ Car Wash: entitlements were NOT issued…".

## 5. Backend: pago manual de una factura recurrente (cantidad 2)

- [ ] 5.1 *Subscriptions › Nuevo*: cliente de prueba, plan **Monthly**, **Regular Sedan 4 Full, cantidad 2** › Confirmar › *Create Invoice* › Confirmar factura › *Register Payment* (fecha de pago = hoy).
- [ ] 5.2 **Esperado** en el periodo nuevo (smart button Car Wash):
  - Cupón Full Wash con **Uses left 8** (4 × 2), no 4.
  - Regalo **qty 2**.
  - Streak 1.
  - Resumen en el chatter del SO.
- [ ] 5.2b Cantidad facturada ≠ cantidad del pedido: en la misma suscripción › *Create Invoice* › **antes de confirmar**, cambia la cantidad de la línea de factura de 2 a **1** › Confirmar › Register Payment. **Esperado:** Full Wash **4** y regalo **1**. Se usa lo facturado, no los 2 del SO. Si esto rompe la secuencia de la racha del paso 6, usa otra suscripción.
- [ ] 5.3 Repite con **Premium SUV cantidad 2** en otra suscripción. **Esperado:**
  - Full Wash **6**, Deep Clean **2**: ambos `included`, vencen al fin del periodo.
  - Exterior `Monthly reward` **2**, vence en **fecha de pago + 30 días**.
  - Streak no genera nada extra.

## 6. Racha (Regular, sin viajar en el tiempo)

La racha se calcula con la **fecha de pago** (la que pones en *Register Payment*) contra el **fin del periodo anterior + 7 días**. Usa la suscripción del paso 5.1 (Regular, cantidad 2).

- [ ] 6.1 Periodo 2: en el SO › *Create Invoice* (factura del mes siguiente) › Confirmar › Register Payment con fecha = hoy. **Esperado:** periodo nuevo con fechas del mes siguiente y **streak 2**, sin cupón de Streak reward.
- [ ] 6.2 Periodo 3: *Create Invoice* › Confirmar › Register Payment con fecha = **fin del periodo 2 + 7 días** (límite exacto, debe contar). **Esperado:** **streak 3** y cupón **Streak reward** (CW Sub – Sedan Exterior) con **Uses left 2**, que vence en fecha de pago + 30 días. También el cupón Full Wash de 8.
- [ ] 6.3 Periodo 4: *Create Invoice* › Confirmar › Register Payment con fecha = **fin del periodo 3 + 8 días**. **Esperado:** **streak 1** (la racha reinicia) y ningún Streak reward.
- [ ] 6.4 En *Car Wash Periods*, filtra por el SO. Los 4 periodos deben tener streak 1, 2, 3, 1, sin huecos ni duplicados.
- [ ] 6.4b Dos líneas iguales en un mismo SO: crea una suscripción con **dos líneas** de Regular Sedan 4 Full (cantidad 2 cada una) › factura › pago. **Esperado:** **2 periodos**, ambos con **streak 1**; uno no alimenta la racha del otro. En la siguiente factura pagada a tiempo, cada línea pasa a **streak 2**.
- [ ] 6.5 (Opcional) Renovación: en el SO › *Renew* › pagar la factura de la orden renovada dentro de la gracia. **Esperado:** *First contract* = SO original y la racha continúa (no vuelve a 1).

> Si la fecha de factura queda **después** de la fecha de pago, Odoo toma la fecha más tardía para la conciliación. Deja la fecha de factura = hoy y mueve solo la fecha de pago.

## 7. Canje de cupones

- [ ] 7.1 Backend: en un SO nuevo del cliente de prueba › botón **Coupon Code** › pega el código del cupón Full Wash. **Esperado:** se agrega la línea del servicio gratis (`FP-CWS-00101`) a 0. Confirma el SO. **Esperado:** el cupón baja de 8 a **7** y el *Included services left* del periodo baja igual.
- [ ] 7.2 Website: en el carrito, aplica el código del **Streak reward** o del **Monthly reward**, ya sea con otra cuenta o como invitado (es transferible). **Esperado:** se aplica la recompensa y, al pagar, los usos bajan en 1.
- [ ] 7.3 Cupón vencido: en *Discount & Loyalty* › programa › *Coupons*, cambia el **Expiration date** de un cupón de prueba a ayer. Aplica su código en un SO. **Esperado:** Odoo lo rechaza ("expired") y no agrega ninguna línea.
- [ ] 7.4 Un cupón con 0 usos tampoco se puede aplicar.

## 8. Regalo pendiente

- [ ] 8.1 *Ventas › Pedidos › Car Wash Periods* › filtro **Gift pending**. **Esperado:** aparecen los periodos de los pasos 4–6 que tienen regalo. El Premium SUV no tiene regalo, salvo que se lo hayas configurado.
- [ ] 8.2 Abre uno › **Mark gift delivered**. **Esperado:**
  - El botón desaparece.
  - Se llenan *Delivered date* y *Delivered by* (tu usuario).
  - El chatter registra el cambio.
  - El periodo sale del filtro Gift pending.

## 9. Reintento idempotente

- [ ] 9.1 Abre una factura **pagada** de los pasos 4–6 › *Acción › Issue Car Wash entitlements*, **dos veces**. **Esperado:** no se crean periodos, cupones ni correos nuevos. El smart button Car Wash del SO mantiene el mismo conteo.
- [ ] 9.2 Recuperación real:
  1. Desmarca *Car Wash Subscription* en un producto de prueba.
  2. Vende y paga una suscripción de ese producto. **Esperado:** no se crea ningún periodo.
  3. Vuelve a marcar el producto.
  4. En la factura, ejecuta *Acción › Issue Car Wash entitlements*. **Esperado:** se crea **un** periodo con sus cupones y su correo.
  5. Ejecuta la acción otra vez. **Esperado:** no hay duplicados.
- [ ] 9.3 Ejecuta la acción en una factura **no pagada**. **Esperado:** no pasa nada.
- [ ] 9.4 La acción solo es visible para **Sales Manager**: un usuario de ventas básico no la ve. Por RPC, ese usuario recibe AccessError (cubierto en código; no hace falta probarlo en la UI).
- [ ] 9.5 **Usuario solo de contabilidad** (de prueba: Invoicing/Accounting, **sin** ningún grupo de Ventas):
  1. Con ese usuario, haz Register Payment de una factura recurrente Car Wash. **Esperado:** el pago se registra sin error y, visto como admin, se crean el periodo y los cupones.
  2. Abre el SO desde la factura. **Esperado:** se abre **sin AccessError** y **no** aparece el smart button Car Wash.
- [ ] 9.6 Multi-compañía: con un usuario de prueba que solo tenga la compañía **México**, abre *Car Wash Periods* (o búscalos). **Esperado:** no ve ningún periodo de KSA. Con KSA activa vuelve a verlos.

> Estos casos no se reproducen en staging; los cubren (o deben cubrir) los tests de Odoo en `tests/test_carwash_subscription.py`:
> - Excepción forzada: el hook o el correo fallan sin bloquear el pago y queda la nota ⚠️.
> - Si la factura ya tiene periodos o el error es una UniqueViolation (doble ejecución concurrente), **no** se publica la nota "NOT issued".
> - Una búsqueda por `state` con un operador distinto de `=`/`!=` lanza NotImplementedError.

---

**Después de staging:** repite en **producción**, a mano, el plan Monthly, los productos con su configuración, el script de programas (incluida la revisión de "At creation") y Automatic Invoice. Lo que hiciste en staging se pierde en el próximo build.
