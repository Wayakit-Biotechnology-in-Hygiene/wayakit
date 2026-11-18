# Checklist de staging: `wayakit_carwash_subscription`

Build de odoo.sh, rama `mainTest`, compañía **KSA** (Advanced Solutions for Detergents LLC, id 1).
Para cada paso, marca la casilla y anota el resultado. Si algo falla, anota la entrada, lo esperado y lo obtenido.

> **Antes de empezar**
> - **Staging = código de la rama + copia de la BD de producción.** Todo lo que configures aquí (plan, productos, programas, Studio, acciones de servidor) **se borra en el próximo build**. Después hay que repetirlo en producción (regla de oro 1).
> - **El conector Odoo MCP apunta a PRODUCCIÓN**, no a staging. Todas las verificaciones de esta lista son **manuales en la UI de staging**.
> - En staging, odoo.sh **no envía correos reales**: aparecen en la pestaña *Mails* del build (o en *Ajustes › Técnico › Correos*, modo desarrollador).
> - Usa solo un **cliente de prueba** (p. ej. "CW Test Customer", correo `cw.test@example.com`). No uses clientes reales.
> - **En staging no se instala ni se activa ningún proveedor de pago, y no se hacen compras web** (decisión de Mafer, 2026-11-18). La emisión al pagar se prueba con el pago manual del backend (sección 5). En el website solo se prueba el carrito, sin pagar (7.2).

---

## 0. Precondiciones (README, "Pendientes fuera del código")

- [x] 0.1 **Sales › Configuración › Ajustes › Automatic Invoice** activado. Sin la factura pagada no se emite nada, así que la sección 5 fallaría sin error visible.
- [x] 0.2 **Tokenización** del proveedor de pago real ("Guardar método de pago"). Es un dato, no una prueba de staging. Si no la soporta, la renovación no se cobra sola y el cliente paga desde el portal.
  - **Resultado 2026-11-18:** MyFatoorah (proveedor activo del KSA Website en prod) NO soporta tokenización: las renovaciones no se cobran solas; el cliente paga desde el portal. Stripe sí la soporta.
- [x] 0.3 Producto **Desinfectante 100 mL** (no existe en KSA). Si todavía no se creó en prod, crea uno temporal en staging solo para probar.
  - **Resultado 2026-11-18:** No existe en staging ni en prod; se creó uno temporal TEST-DESINF-100 solo en staging.
- [x] 0.4 Existen en KSA los productos de servicio `FP-CWS-00100`, `00101`, `00102`, `00200`, `00201` y `00202`. El script del paso 2 los busca por referencia interna.
- [x] 0.5 Decisión **6 productos vs variantes** (Dalal/Sandra). Esta lista usa productos separados. Si se usan variantes, llena "Only for variant" en las entitlements.
- [ ] 0.6 Deep clean: anota si la plantilla del correo del cupón ya incluye el link de la cita **"Car Wash Care"**. Si no lo incluye, es un pendiente; no bloquea esta prueba.
  - **Resultado 2026-11-18:** PENDIENTE: la plantilla "Coupon: Coupon Information" no trae el link de la cita.

## 1. Instalación

- [x] 1.1 *Apps* › buscar **Subscriptions** (`sale_subscription`) › Instalar (si no está instalado).
- [x] 1.2 *Apps* › Actualizar lista de apps › buscar **Wayakit - Car Wash Subscriptions (KSA)** › Instalar. **Esperado:** instala sin error.
- [x] 1.3 *Subscriptions › Configuración › Recurring Plans* › Nuevo: nombre **Monthly**, periodo **1 Month** › Guardar.
  - **Resultado 2026-11-18:** El plan Monthly ya existía (1 mes).
- [x] 1.4 Verifica que aparezcan:
  - *Ventas › Pedidos › Car Wash Periods*, con el filtro "Active" puesto por defecto.
  - La pestaña **Car Wash Subscription** en el formulario del producto.
  - *Acción › Issue Car Wash entitlements* en facturas (lista y formulario, solo para Sales Manager).
- [ ] 1.5 Actualización a **17.0.1.1.2** (el módulo ahora depende de `website_sale_loyalty`). Antes de actualizar, en *Apps* revisa si **eCommerce Loyalty** (`website_sale_loyalty`) ya está instalado y anótalo. Luego *Apps* › Wayakit - Car Wash Subscriptions (KSA) › *Upgrade*. **Esperado:** termina sin error y el módulo queda en **17.0.1.1.2**. No se instala ningún módulo nuevo, porque `website_sale_loyalty` ya estaba instalado. Si el Upgrade lo instaló, anótalo: en producción también se instalará.

## 2. Programas de cupones (script)

- [x] 2.1 En odoo.sh › rama › *Shell*:
  ```
  odoo-bin shell < ~/src/user/wayakit_carwash_subscription/scripts/setup_coupon_programs.py
  ```
  **Esperado:** 6 líneas `+ created: CW Sub – …`, sin `!! product not found`.
  - **Resultado 2026-11-18:** Se crearon por RPC con los mismos valores que el script (programas 14–19), con "At creation" incluido.
- [ ] 2.2 Corre el script otra vez. **Esperado:** 6 líneas `= exists: …` y ningún duplicado (idempotente).
- [x] 2.3 *Ventas › Productos › Discount & Loyalty*: hay exactamente 6 programas "CW Sub – …" (Sedan/SUV × Exterior/Full Wash/Deep Clean). Cada uno debe tener:
  - Tipo **Coupons**, compañía KSA y ninguna regla.
  - Una recompensa **Free product** con el producto `FP-CWS-…` correcto, cantidad 1 y 1 punto requerido.
  - En la pestaña **Communications**, una línea **"At creation"** con la plantilla del cupón. **Revisa los 6.** Odoo no la agrega cuando el programa se crea por código. Si falta en alguno, el cliente no recibe el correo del cupón: anótalo y agrégala a mano.
  - **Resultado 2026-11-18:** Los 6: coupons, KSA, 0 reglas, Free product correcto ×1 / 1 punto, "At creation" con la plantilla del cupón.

## 3. Configuración de productos

- [x] 3.1 **Regular Sedan 4 Full**: tipo Servicio, *Subscription Product*, precio del plan Monthly **191.30** (sin IVA, más 15% de IVA = 220). En la pestaña *Car Wash Subscription*:
  - Marca *Car Wash Subscription*, Tier **Regular Care**.
  - Streak 3, grace 7, regalo **Desinfectante 100 mL** ×1.
  - Entitlements: `included` / CW Sub – Sedan Full Wash / **4**, y `streak_reward` / CW Sub – Sedan Exterior / **1**.
  - Publica el producto en el website.
  - **Resultado 2026-11-18:** La BD de staging es de ~21 oct: los 6 productos no existían y se crearon en staging (7191–7196), con pricing Monthly, categorías Regular/Premium Care y publicados. Regalo solo en Regular Sedan 4 Full.
- [x] 3.2 **Premium SUV**: precio **495.65** (sin IVA, 570 con IVA). Tier **Premium Care**. Entitlements:
  - `included` / SUV Full Wash / **3**
  - `included` / SUV Deep Clean / **1**
  - `monthly_reward` / SUV Exterior / **1**

  **Esperado:** los campos Streak/grace están ocultos.
- [x] 3.3 En Premium SUV agrega una línea `streak_reward` › Guardar. **Esperado:** ValidationError *"Premium Care cannot have streak rewards (it already gets a monthly reward)."* Descarta los cambios.
- [x] 3.4 Variante del caso: en un producto Regular con `streak_reward`, cambia el Tier a Premium › Guardar. **Esperado:** el mismo ValidationError.

## 4. Compra web pagada en línea: no se prueba en staging

En staging no hay proveedor de pago ni compras web (ver "Antes de empezar"). Para el módulo, el pago web y el *Register Payment* del backend llegan al mismo `_invoice_paid_hook` en cuanto la factura queda pagada. Por eso este camino lo cubre la **sección 5** (SO de suscripción en el backend › factura › *Register Payment*), y los detalles del periodo se revisan en 5.2c. Que el resumen del chatter tenga como autor a OdooBot cuando el usuario actual es el público del website lo cubren los tests automáticos, no staging.

## 5. Backend: pago manual de una factura recurrente (cantidad 2)

- [x] 5.1 *Subscriptions › Nuevo*: cliente de prueba, plan **Monthly**, **Regular Sedan 4 Full, cantidad 2** › Confirmar › *Create Invoice* › Confirmar factura › *Register Payment* (fecha de pago = hoy).
  - **Resultado 2026-11-18:** S02519. Total 439.99, no 440: 191.30 ×1.15 ×2 redondea a .99.
- [x] 5.2 **Esperado** en el periodo nuevo (smart button Car Wash):
  - Cupón Full Wash con **Uses left 8** (4 × 2), no 4.
  - Regalo **qty 2**.
  - Streak 1.
  - Resumen en el chatter del SO.
  - **Resultado 2026-11-18:** Cupón Full Wash 8, regalo ×2, streak 1, resumen en el chatter y correo del cupón generado.
- [ ] 5.2c Detalle del periodo de 5.1 (antes se revisaba en la compra web, sección 4). **Esperado:**
  - Inicio y fin = fechas de servicio de la línea de factura (mes calendario). Payment date = la fecha de *Register Payment*.
  - El cupón `Included` Full Wash vence en la fecha **fin del periodo**.
  - **No** hay cupón de Streak reward (streak 1).
  - El resumen **"Car Wash period … → … (streak 1)"** del chatter del SO trae programa, código, usos, vencimiento y la línea "Gift: …", con **OdooBot** como autor.
  - **No** hay nota ⚠️ de correo fallido en el periodo, ni nota "⚠️ Car Wash: entitlements were NOT issued…" en la factura.
- [x] 5.2b Cantidad facturada ≠ cantidad del pedido: en la misma suscripción › *Create Invoice* › **antes de confirmar**, cambia la cantidad de la línea de factura de 2 a **1** › Confirmar › Register Payment. **Esperado:** Full Wash **4** y regalo **1**. Se usa lo facturado, no los 2 del SO. Si esto rompe la secuencia de la racha del paso 6, usa otra suscripción.
  - **Resultado 2026-11-18:** Otra suscripción: Full Wash 4, regalo 1.
- [x] 5.3 Repite con **Premium SUV cantidad 2** en otra suscripción. **Esperado:**
  - Full Wash **6**, Deep Clean **2**: ambos `included`, vencen al fin del periodo.
  - Exterior `Monthly reward` **2**, vence en **fecha de pago + 30 días**.
  - Streak no genera nada extra.
  - **Resultado 2026-11-18:** Full Wash 6 y Deep 2 vencen el 2026-12-17; Exterior monthly_reward 2 vence el 2026-12-18 (pago + 30).

## 6. Racha (Regular, sin viajar en el tiempo)

La racha se calcula con la **fecha de pago** (la que pones en *Register Payment*) contra el **fin del periodo anterior + 7 días**. Usa la suscripción del paso 5.1 (Regular, cantidad 2).

- [x] 6.1 Periodo 2: en el SO › *Create Invoice* (factura del mes siguiente) › Confirmar › Register Payment con fecha = hoy. **Esperado:** periodo nuevo con fechas del mes siguiente y **streak 2**, sin cupón de Streak reward.
- [x] 6.2 Periodo 3: *Create Invoice* › Confirmar › Register Payment con fecha = **fin del periodo 2 + 7 días** (límite exacto, debe contar). **Esperado:** **streak 3** y cupón **Streak reward** (CW Sub – Sedan Exterior) con **Uses left 2**, que vence en fecha de pago + 30 días. También el cupón Full Wash de 8.
  - **Resultado 2026-11-18:** Pago el 2027-01-24 (fin + 7): streak 3 y Streak reward 2 usos que vence el 2027-02-23.
- [x] 6.3 Periodo 4: *Create Invoice* › Confirmar › Register Payment con fecha = **fin del periodo 3 + 8 días**. **Esperado:** **streak 1** (la racha reinicia) y ningún Streak reward.
  - **Resultado 2026-11-18:** Pago el 2027-02-25 (fin + 8): streak 1.
- [x] 6.4 En *Car Wash Periods*, filtra por el SO. Los 4 periodos deben tener streak 1, 2, 3, 1, sin huecos ni duplicados.
  - **Resultado 2026-11-18:** 1, 2, 3, 1.
- [x] 6.4b Dos líneas iguales en un mismo SO: crea una suscripción con **dos líneas** de Regular Sedan 4 Full (cantidad 2 cada una) › factura › pago. **Esperado:** **2 periodos**, ambos con **streak 1**; uno no alimenta la racha del otro. En la siguiente factura pagada a tiempo, cada línea pasa a **streak 2**.
  - **Resultado 2026-11-18:** 2 periodos con streak 1 y luego 2/2.
- [ ] 6.5 (Opcional) Renovación: en el SO › *Renew* › pagar la factura de la orden renovada dentro de la gracia. **Esperado:** *First contract* = SO original y la racha continúa (no vuelve a 1).

> Si la fecha de factura queda **después** de la fecha de pago, Odoo toma la fecha más tardía para la conciliación. Deja la fecha de factura = hoy y mueve solo la fecha de pago.

## 7. Canje de cupones

- [ ] 7.1 Backend: en un SO nuevo del cliente de prueba › botón **Coupon Code** › pega el código del cupón Full Wash. **Esperado:** se agrega la línea del servicio gratis (`FP-CWS-00101`) a 0. Confirma el SO. **Esperado:** el cupón baja de 8 a **7** y el *Included services left* del periodo baja igual.
  - **Resultado 2026-11-18:** FALLA: al canjear el código, Odoo agrega 8 lavados gratis en una sola orden (qty 8, points_cost 8) y deja el cupón en 0. Es el estándar de Odoo 17 para free product: canjea todos los puntos. Requiere fix en código.
  - **Re-test con el fix (v17.0.1.1.2):** en *Apps* confirma que el módulo está en **17.0.1.1.2** (paso 1.5). Usa un cupón Full Wash de un periodo con **varios** lavados (no uno de 1 punto, porque ahí no se nota el bug). Repite 7.1. **Esperado:** la línea del servicio gratis tiene **cantidad 1**, no 8. Antes de confirmar, cambia la cantidad de otra línea del SO y guarda: la línea gratis sigue en 1. Confirma: el cupón baja **solo 1** y el *Included services left* del periodo baja 1.
  - [ ] 7.1b En **otro** SO nuevo, aplica el **mismo** código. **Esperado:** se acepta de nuevo, otra vez cantidad 1, y al confirmar el cupón baja 1 más.
  - [ ] 7.1c Control (otros cupones sin cambio): si hay en staging un cupón de otro programa (no "CW Sub – …") con recompensa de producto gratis y varios puntos, aplícalo en un SO. **Esperado:** se comporta como antes del fix (canjea todos sus puntos). Si no hay ninguno, marca este paso como *no corrido*; está cubierto por `test_20`.
  - [ ] 7.1d Segundo lavado en el mismo SO desde el backend: en un SO nuevo, aplica el código Full Wash con **Coupon Code** (línea gratis cantidad 1). Luego, en el mismo SO, botón **Promotions**. Si es la única recompensa disponible, se agrega directo. Si se abre el asistente, elige la misma recompensa del cupón. **Esperado:** se agrega una **segunda** línea gratis de cantidad **1** (no 7 ni el resto del cupón). Anota los usos del cupón y confirma el SO. **Esperado:** el cupón baja **2**. Esto prueba que ocultar el botón Claim solo afecta al website.
- [ ] 7.2 Website, **sin pagar** (en staging no hay proveedor de pago): como **invitado** (sin iniciar sesión), agrega cualquier producto al carrito y aplica el código del **Streak reward** o del **Monthly reward**. Es transferible: funciona con cualquier cuenta o sin cuenta. **Esperado:** el carrito agrega la línea del servicio gratis con cantidad **1**. Anota los usos del cupón en el backend: no cambian, porque los puntos se descuentan solo cuando se confirma la orden. Deja el carrito sin pagar.
  - [ ] 7.2b Website con un cupón **incluido** de varios lavados (p. ej. Full Wash con 4 u 8), como invitado y sin pagar: aplica el código en el carrito. **Esperado:**
    - El carrito muestra **1** servicio gratis, no 4 ni 8.
    - Después de aplicarlo, el carrito **no** muestra el recuadro/botón **"Claim"** de ese mismo cupón. Antes de v17.0.1.1.2 sí aparecía, y al pulsarlo salía "This program is already applied to this order.".
  - [ ] 7.2c Negativo: en el mismo carrito de 7.2b, escribe **otra vez el mismo código**. **Esperado:** Odoo responde que ya está aplicado ("already applied"), no agrega una segunda línea gratis y la que había sigue en cantidad 1.
  - [ ] 7.2d Cupón propio sin código, en el website: **solo si el cliente de prueba ya tiene acceso al portal**. No crees usuarios para esto; si no tiene acceso, marca el paso como *no corrido* (lo cubre `test_21`). Inicia sesión como ese cliente, que debe ser dueño de un cupón Full Wash con varios usos. Agrega un producto al carrito **sin** escribir ningún código. **Esperado:**
    - El carrito muestra el recuadro **Claim** de ese cupón.
    - Un clic agrega **1** lavado gratis.
    - Después del clic, el recuadro **Claim** de ese cupón desaparece.
    - No pagues.
- [x] 7.3 Cupón vencido: en *Discount & Loyalty* › programa › *Coupons*, cambia el **Expiration date** de un cupón de prueba a ayer. Aplica su código en un SO. **Esperado:** Odoo lo rechaza ("expired") y no agrega ninguna línea.
  - **Resultado 2026-11-18:** "This coupon is expired."
- [x] 7.4 Un cupón con 0 usos tampoco se puede aplicar.
  - **Resultado 2026-11-18:** "This coupon has already been used."

## 8. Regalo pendiente

- [x] 8.1 *Ventas › Pedidos › Car Wash Periods* › filtro **Gift pending**. **Esperado:** aparecen los periodos de los pasos 5–6 que tienen regalo. El Premium SUV no tiene regalo, salvo que se lo hayas configurado.
- [x] 8.2 Abre uno › **Mark gift delivered**. **Esperado:**
  - El botón desaparece.
  - Se llenan *Delivered date* y *Delivered by* (tu usuario).
  - El chatter registra el cambio.
  - El periodo sale del filtro Gift pending.
  - **Resultado 2026-11-18:** Delivered date/by y tracking OK (verificado por datos, no visual).

## 9. Reintento idempotente

- [x] 9.1 Abre una factura **pagada** de los pasos 5–6 › *Acción › Issue Car Wash entitlements*, **dos veces**. **Esperado:** no se crean periodos, cupones ni correos nuevos. El smart button Car Wash del SO mantiene el mismo conteo.
  - **Resultado 2026-11-18:** Periodos, cupones y correos sin cambios.
- [x] 9.2 Recuperación real:
  1. Desmarca *Car Wash Subscription* en un producto de prueba.
  2. Vende y paga una suscripción de ese producto. **Esperado:** no se crea ningún periodo.
  3. Vuelve a marcar el producto.
  4. En la factura, ejecuta *Acción › Issue Car Wash entitlements*. **Esperado:** se crea **un** periodo con sus cupones y su correo.
  5. Ejecuta la acción otra vez. **Esperado:** no hay duplicados.
  - **Resultado 2026-11-18:** 0 periodos sin la casilla → 1 al re-emitir → 1 al repetir.
- [x] 9.3 Ejecuta la acción en una factura **no pagada**. **Esperado:** no pasa nada.
- [x] 9.4 La acción solo es visible para **Sales Manager**: un usuario de ventas básico no la ve. Por RPC, ese usuario recibe AccessError (cubierto en código; no hace falta probarlo en la UI).
  - **Resultado 2026-11-18:** Grupo de la acción: Sales / Administrator.
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
