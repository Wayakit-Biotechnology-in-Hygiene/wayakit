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
- [x] 1.5 Actualización a **17.0.1.1.2** (el módulo ahora depende de `website_sale_loyalty`). Antes de actualizar, en *Apps* revisa si **eCommerce Loyalty** (`website_sale_loyalty`) ya está instalado y anótalo. Luego *Apps* › Wayakit - Car Wash Subscriptions (KSA) › *Upgrade*. **Esperado:** termina sin error y el módulo queda en **17.0.1.1.2**. No se instala ningún módulo nuevo, porque `website_sale_loyalty` ya estaba instalado. Si el Upgrade lo instaló, anótalo: en producción también se instalará.
  - **Resultado 2026-11-18:** Staging quedó en 17.0.1.1.2 sin error; `website_sale_loyalty` ya estaba instalado y no se reinstaló.

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

- [x] 7.1 Backend: en un SO nuevo del cliente de prueba › botón **Coupon Code** › pega el código del cupón Full Wash. **Esperado:** se agrega la línea del servicio gratis (`FP-CWS-00101`) a 0. Confirma el SO. **Esperado:** el cupón baja de 8 a **7** y el *Included services left* del periodo baja igual.
  - **Resultado 2026-11-18:** FALLA: al canjear el código, Odoo agrega 8 lavados gratis en una sola orden (qty 8, points_cost 8) y deja el cupón en 0. Es el estándar de Odoo 17 para free product: canjea todos los puntos. Requiere fix en código.
  - **Re-test con el fix (v17.0.1.1.2):** en *Apps* confirma que el módulo está en **17.0.1.1.2** (paso 1.5). Usa un cupón Full Wash de un periodo con **varios** lavados (no uno de 1 punto, porque ahí no se nota el bug). Repite 7.1. **Esperado:** la línea del servicio gratis tiene **cantidad 1**, no 8. Antes de confirmar, cambia la cantidad de otra línea del SO y guarda: la línea gratis sigue en 1. Confirma: el cupón baja **solo 1** y el *Included services left* del periodo baja 1.
  - **Resultado 2026-11-18:** Re-prueba en 17.0.1.1.2: cupón Full Wash de 8 → línea gratis cantidad 1, costo 1; sigue en 1 al cambiar otra línea a 5; al confirmar 8 → 7 y services_left 7. (Antes de este fix: 8 → 0).
  - [x] 7.1b En **otro** SO nuevo, aplica el **mismo** código. **Esperado:** se acepta de nuevo, otra vez cantidad 1, y al confirmar el cupón baja 1 más.
    - **Resultado 2026-11-18:** Otro SO con el mismo código: cantidad 1, 7 → 6.
  - [ ] 7.1c Control (otros cupones sin cambio): si hay en staging un cupón de otro programa (no "CW Sub – …") con recompensa de producto gratis y varios puntos, aplícalo en un SO. **Esperado:** se comporta como antes del fix (canjea todos sus puntos). Si no hay ninguno, marca este paso como *no corrido*; está cubierto por `test_20`.
    - **Resultado 2026-11-18:** No corrido: en staging no hay cupones de otros programas con más de 1 punto. Lo cubre test_20.
  - [x] 7.1d Segundo lavado en el mismo SO desde el backend: en un SO nuevo, aplica el código Full Wash con **Coupon Code** (línea gratis cantidad 1). Luego, en el mismo SO, botón **Promotions**. Si es la única recompensa disponible, se agrega directo. Si se abre el asistente, elige la misma recompensa del cupón. **Esperado:** se agrega una **segunda** línea gratis de cantidad **1** (no 7 ni el resto del cupón). Anota los usos del cupón y confirma el SO. **Esperado:** el cupón baja **2**. Esto prueba que ocultar el botón Claim solo afecta al website.
    - **Resultado 2026-11-18:** Promotions abrió el asistente (había otras recompensas del cliente): elegida la de Full Wash → segunda línea de cantidad 1; al confirmar 5 → 3.
- [ ] 7.2 Website, **sin pagar** (en staging no hay proveedor de pago): como **invitado** (sin iniciar sesión), agrega cualquier producto al carrito y aplica el código del **Streak reward** o del **Monthly reward**. Es transferible: funciona con cualquier cuenta o sin cuenta. **Esperado:** el carrito agrega la línea del servicio gratis con cantidad **1**. Anota los usos del cupón en el backend: no cambian, porque los puntos se descuentan solo cuando se confirma la orden. Deja el carrito sin pagar.
  - **Resultado 2026-11-18:** No corrido (código de Streak/Monthly como invitado); el mecanismo es el mismo que 7.2b.
  - [x] 7.2b Website con un cupón **incluido** de varios lavados (p. ej. Full Wash con 4 u 8), como invitado y sin pagar: aplica el código en el carrito. **Esperado:**
    - El carrito muestra **1** servicio gratis, no 4 ni 8.
    - Después de aplicarlo, el carrito **no** muestra el recuadro/botón **"Claim"** de ese mismo cupón. Antes de v17.0.1.1.2 sí aparecía, y al pulsarlo salía "This program is already applied to this order.".
    - **Resultado 2026-11-18:** Con la sesión de Luisa (no invitado), website KSA: línea "Free Product - Car Wash Sedan" cantidad 1 a 0.00 SR y sin aviso Claim. Cupón sin cambio (3) mientras no se paga; la línea se quitó después.
  - [x] 7.2c Negativo: en el mismo carrito de 7.2b, escribe **otra vez el mismo código**. **Esperado:** Odoo responde que ya está aplicado ("already applied"), no agrega una segunda línea gratis y la que había sigue en cantidad 1.
    - **Resultado 2026-11-18:** "This program is already applied to this order." y sin segunda línea.
  - [ ] 7.2d Cupón propio sin código, en el website: **solo si el cliente de prueba ya tiene acceso al portal**. No crees usuarios para esto; si no tiene acceso, marca el paso como *no corrido* (lo cubre `test_21`). Inicia sesión como ese cliente, que debe ser dueño de un cupón Full Wash con varios usos. Agrega un producto al carrito **sin** escribir ningún código. **Esperado:**
    - El carrito muestra el recuadro **Claim** de ese cupón.
    - Un clic agrega **1** lavado gratis.
    - Después del clic, el recuadro **Claim** de ese cupón desaparece.
    - No pagues.
    - **Resultado 2026-11-18:** No corrido: el cliente de prueba no tiene portal y no se crean usuarios. Lo cubre test_21.
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


## 10. Página `/car-wash-subscriptions` y cita "Car Wash Subscription" (v17.0.1.2.0)

> La cita es **dato**: el código no la crea. Se hace a mano, una vez en staging y otra en producción.

- [x] 10.0 Antes de actualizar, en *Apps* anota si **Online Appointment** (`website_appointment`) ya está instalado. El módulo ahora depende de él. Luego *Upgrade* del módulo. **Esperado:** termina sin error y queda en **17.0.1.2.0**. Si el Upgrade instaló `website_appointment` u otro módulo, anótalo: en producción pasará lo mismo.
  - **Resultado 2026-11-18:** Staging quedó en 17.0.1.2.0 sin error; `website_appointment` ya estaba instalado.
- [x] 10.1 **Paso a mano (Mafer):** *Appointments* › abre **"Car Wash Care"** (id 1) › *Acción › Duplicar*. En la copia:
  - Nombre exacto **`Car Wash Subscription`**, porque el módulo la busca por ese nombre.
  - Mismo personal/calendario que "Car Wash Care", para que los horarios no se encimen.
  - Quita la pregunta **"Payment Method"**.
  - Agrega la pregunta **"Subscription code"** (texto de una línea) y márcala **obligatoria**.
  - Mismo website (KSA) y **Publicada**.
  - **Resultado 2026-11-18:** Hecho por Claude en staging: cita "Car Wash Subscription" (/appointment/16), copia de "Car Wash Care" con el mismo personal y horarios, sin "Payment Method", con "Subscription code" obligatorio y publicada en KSA Website. Además se agregó "Car Wash Subscriptions" al menú de KSA (debajo de "Car wash", dentro de "KAUST services booking").

  **Esperado:** la cita se guarda y su URL pública abre el formulario. Anota la URL (p. ej. `/appointment/<id>`).
- [x] 10.2 Abre `https://<dominio de staging>/car-wash-subscriptions` en una ventana privada (sin sesión). **Esperado:**
  - Carga sin error, en el **KSA Website**. Si da 404, busca en el log del Upgrade el aviso `no single website named 'KSA Website', page /car-wash-subscriptions left unpublished`. Ese aviso sale si no hay un website con nombre **exacto** "KSA Website", o si hay dos. En ese caso la página queda despublicada a propósito, para que no aparezca en otros websites como el B2B MX. Anota los nombres exactos de los websites y avísale al dev. Cuando se corrija el nombre y se vuelva a hacer *Upgrade*, la página se asigna al website, pero **no** se vuelve a publicar sola: publícala a mano.
  - Las secciones aparecen en este orden: Hero, Planes, Beneficios/lealtad, "Already subscribed? Book your wash", FAQ.
  - Se ve como la página `/wayakit-car-wash-service` (mismas fuentes, colores y fotos reales). La marca aparece como **WAYAKIT** en mayúsculas.
  - Las **4 fotos** de la página cargan, sin imágenes rotas. Son adjuntos de producción (`/web/image/…`), que existen en staging porque su BD es copia de producción. En la BD local de pruebas no existen, por eso los tests no las revisan. Si alguna sale rota, anota cuál.
  - **Resultado 2026-11-18:** Con sesión iniciada, la página carga y las 4 fotos responden 200. Sin sesión da 404, igual que /wayakit-car-wash-service y /appointment/1: en staging los websites no tienen dominio, así que el visitante anónimo cae en "My Website". En prod KSA Website sí tiene dominio.
- [x] 10.3 Precios con IVA: **180 / 220 / 260 / 300** (Regular Sedan/SUV × 4 Exterior / 4 Full) y **435 / 570** (Premium Sedan / SUV), cada uno con su valor normal y su ahorro. **Esperado:** coinciden con la propuesta de Dalal. La página **no** muestra rentabilidad, márgenes ni costos ("Indicative profitability", "gross margin").
  - **Resultado 2026-11-18:** Precios correctos y sin márgenes ni rentabilidad.
- [x] 10.4 Botones **Subscribe**: haz clic en cada uno. **Esperado:** cada uno abre la página del producto correcto (`FP-CWS-00103`, `00104`, `00203`, `00204`, `00105`, `00205`).
  - Prueba negativa: despublica un producto (p. ej. `FP-CWS-00205`) y recarga la página. **Esperado:** su botón ya no aparece y el resto de la página sigue igual. Vuelve a publicarlo.
  - **Resultado 2026-11-18:** Los 6 Subscribe apuntan a /shop/fp-cws-00103…00205.
- [x] 10.5 Sección **Book your wash**: abre `/car-wash-subscriptions#book`. **Esperado:** la página baja a esa sección. Su botón abre la cita **"Car Wash Subscription"** del paso 10.1, no `/appointment/1`.
  - En el formulario de la cita, intenta reservar con "Subscription code" vacío. **Esperado:** no deja enviar.
  - Si ya probaste el envío, cancela la cita de prueba para liberar el horario.
  - **Resultado 2026-11-18:** "Book your wash" → /appointment/16 (la cita nueva); el formulario abre con "Subscription code" obligatorio.
- [ ] 10.6 Fallback: despublica temporalmente la cita "Car Wash Subscription" y recarga la página. **Esperado:** el botón de reserva lleva a **`/appointment/1`** ("Car Wash Care") y la página no da error. Vuelve a publicar la cita.
- [ ] 10.7 Otro website (si existe en staging, p. ej. el de México): abre `/car-wash-subscriptions` en su dominio. **Esperado:** 404, porque la página es solo del KSA Website.
- [ ] 10.8 Árabe: en el KSA Website cambia el idioma a **AR** y abre la página. **Esperado:** carga sin error. Mientras no se traduzca, el texto sale en inglés.
  - **Resultado 2026-11-18:** No aplica: no habrá versión en árabe (decisión de Mafer, 2026-11-18).
  - [ ] 10.8b En `/car-wash-subscriptions` (AR) › *Website › Traducir*, haz clic en el texto de un botón **"Subscribe"** y en el de **"Book your wash"**. Estos botones no se pueden editar a propósito (`o_not_editable`), pero su **texto** sí debe poder traducirse. **Esperado:** el texto se marca como traducible y acepta un texto de prueba. Descártalo sin guardar.
    - Si el modo Traducir **no** deja seleccionar esos textos, prueba la alternativa: modo desarrollador › *Ajustes › Técnico › Vistas* › busca la vista de la página (`page_cw_subscriptions`) › icono de traducción del campo *Arch* › busca "Subscribe" / "Book your wash". **Esperado:** aparecen y se pueden traducir. Anota cuál de las dos vías funcionó.
    - **Resultado 2026-11-18:** No aplica: sin traducción al árabe.
  - [ ] 10.8c En modo *Editar* (EN), comprueba que los botones **Subscribe** y **Book your wash** no se pueden mover ni borrar, y que sí se pueden agregar bloques en las zonas vacías: al inicio de la página y antes del FAQ. Descarta los cambios.

## 11. Correo del cupón Car Wash con "Book your wash" (v17.0.1.2.0)

- [x] 11.1 *Discount & Loyalty* › abre los 6 programas **"CW Sub – …"** (KSA) › pestaña **Communications**. **Esperado:** la línea **"At creation"** usa la plantilla nueva del módulo, no "Coupon: Coupon Information". Hay **una sola** línea "At creation" por programa.
  - **Resultado 2026-11-18:** Los 6 programas "CW Sub" usan "Car Wash Subscription: your washes" en "At creation".
- [ ] 11.2 Control: abre un programa que **no** sea "CW Sub – …" (p. ej. el cupón de reseña o un eWallet). **Esperado:** su pestaña Communications no cambió.
- [x] 11.3 Emite cupones nuevos con un pago de backend, como en 5.1 (suscripción del cliente de prueba › factura › *Register Payment*). Abre el correo del cupón en *Mails* de odoo.sh (o en *Ajustes › Técnico › Correos*). **Esperado:** el correo muestra:
  - El nombre del cliente de prueba.
  - El servicio (p. ej. Sedan Full Wash).
  - El **código**.
  - El número de lavados (p. ej. 4).
  - La fecha de vencimiento.
  - El tipo: Included, o *Monthly/Streak reward* con la nota de que es transferible.
  - Que el código es **obligatorio** en la reserva y se aplica en el lavado.
  - **WAYAKIT** en mayúsculas, con el estilo de los otros correos de WAYAKIT.
  - Sin código de barras roto.
  - **Resultado 2026-11-18:** Suscripción Premium Sedan (S… id 2597) pagada desde el backend: 3 cupones (Full Wash 3, Deep Clean 1, Exterior monthly reward 1), cada uno con su correo: código, lavados, vencimiento, tipo, pasos para agendar, nota de deep clean solo en el de Deep Clean, sin código de barras. Detalle: arriba sale el encabezado de Odoo "Your Loyalty Coupon / CW Sub – Sedan Deep Clean: código" con el nombre interno del programa.
- [x] 11.4 Botón **"Book your wash"** del correo. **Esperado:** apunta a `https://<dominio del KSA Website en staging>/car-wash-subscriptions#book`, no al dominio de producción `ksasystem.wayakit.com`. Al abrirlo, llega a la sección de reserva.
  - **Resultado 2026-11-18:** El botón apunta a https://wayakit-staging.odoo.com/car-wash-subscriptions#book: en staging el KSA Website no tiene dominio y se usa web.base.url. En prod será https://ksasystem.wayakit.com.
- [ ] 11.5 El cambio de plantilla de 11.1 lo hace la migración automática del Upgrade a 17.0.1.2.0 (`migrations/17.0.1.2.0/post-migrate.py`). Para comprobar que se puede repetir: en el *Shell* de odoo.sh corre `odoo-bin shell < ~/src/user/wayakit_carwash_subscription/scripts/setup_coupon_programs.py`. **Esperado:** no crea programas duplicados y cada programa "CW Sub – …" sigue con **una sola** línea "At creation" con la plantilla nueva. Si algún programa tenía otra plantilla personalizada, se deja como estaba y queda un aviso en el log: anótalo.

### 11b. Correo sin el encabezado/pie de Odoo y con el diseño WAYAKIT (v17.0.1.2.1)

> Corrige lo visto en 11.3: arriba salía el encabezado de Odoo ("Your Loyalty Coupon / CW Sub – …: código") y abajo el pie de Odoo (dirección, "Powered by Odoo | Unfollow"). Desde 1.2.1 se envían sin ese marco los cupones **emitidos por un periodo Car Wash** y los de cualquier programa cuya comunicación "At creation" usa la plantilla "Car Wash Subscription: your washes" (p. ej. cupones "CW Sub" generados a mano). La plantilla del módulo es el correo completo, con su propio encabezado y pie. Los demás cupones (reseña, eWallet, o un programa "CW Sub" que siga con la plantilla estándar de Odoo) siguen igual que en Odoo estándar.
>
> Staging no envía correos a buzones reales: odoo.sh los guarda en la pestaña **Mails** del build. Ahí se revisan.

- [ ] 11.6 *Apps* › *Upgrade* del módulo. **Esperado:** termina sin error y queda en **17.0.1.2.1**.
  - Esta actualización **reescribe** la plantilla "Car Wash Subscription: your washes" con la versión del código (la migración `migrations/17.0.1.2.1/post-migrate.py`). Cualquier cambio hecho a mano en esa plantilla desde la interfaz se pierde. Hasta 1.2.0 no había ninguno; si alguien la editó, anótalo antes de actualizar.
  - En producción pasará lo mismo al actualizar.
- [ ] 11.7 *Ajustes › Técnico › Plantillas de correo* › abre **"Car Wash Subscription: your washes"**. **Esperado:** la vista previa ya muestra el diseño nuevo (logo oficial de WAYAKIT arriba, pie azul marino). Si sigue el diseño viejo (logo grande con "GreenTech · KAUST · Saudi Arabia" debajo), la migración no corrió: avísale al dev con el log del Upgrade.
- [ ] 11.8 Emite cupones nuevos con un pago de backend, como en 5.1 / 11.3 (suscripción del cliente de prueba › factura › *Register Payment*). Si puedes, usa un plan **Premium** (salen 3 correos: Full Wash, Deep Clean y la recompensa mensual). Abre cada correo en la pestaña **Mails** de odoo.sh. **Esperado:**
  - **No** sale el encabezado de Odoo: ni "Your Loyalty Coupon", ni el nombre interno **"CW Sub – …"**, ni el logo de la compañía a la derecha.
  - **No** sale el pie de Odoo: ni "Powered by Odoo", ni "Unfollow".
  - Arriba, el **logo oficial de WAYAKIT** (no deformado ni recortado).
  - Todo lo de 11.3 sigue igual: nombre del cliente, servicio, **código** destacado, número de lavados, vencimiento, tipo (Included / reward transferible), los 3 pasos, código **obligatorio** en la reserva, nota de deep clean solo en el de Deep Clean, sin código de barras.
  - El botón **"Book your wash"** apunta a `https://<dominio de staging>/car-wash-subscriptions#book` (como en 11.4).
  - El pie lleva: **WAYAKIT**, **Advanced Solutions for Detergents LLC**, el **correo de la compañía KSA** (el de *Ajustes › Compañías › Advanced Solutions for Detergents LLC*; compáralo), el enlace **WhatsApp +966 54 575 4908** (abre `https://wa.me/966545754908`) y un enlace al sitio web.
  - No aparecen márgenes, costos ni nombres internos.
  - **WAYAKIT** siempre en mayúsculas.
- [ ] 11.9 Imagen del logo: en el correo de 11.8, clic derecho en el logo › abrir imagen en otra pestaña. **Esperado:** carga (la URL es `https://<dominio de staging>/wayakit_carwash_subscription/static/src/img/…`). Si sale rota, anota la URL.
- [ ] 11.10 Reenvío manual de un cupón Car Wash: *Discount & Loyalty* › programa "CW Sub – …" › abre uno de los cupones de 11.8 › botón **Send**. En la ventana que se abre el cuerpo puede verse como **código de plantilla** (textos con `object.…`, `t-if`): es normal, se reemplaza al enviar. **No edites el cuerpo**; pulsa **Send**. **Esperado:**
  - Se envía sin error y queda registrado en el chatter del cupón.
  - Llega aunque el correo del cliente de prueba se haya dado de baja de newsletters (lista negra): es el cupón pagado, no marketing.
  - El correo nuevo (pestaña **Mails**) es igual al de 11.8: **sin** encabezado ni pie de Odoo.
- [ ] 11.11 Control, cupones que no son Car Wash: en un programa que **no** sea "CW Sub – …" y que tenga una comunicación "At creation" (p. ej. el cupón de reseña), genera **un** cupón para el **cliente de prueba** (*Generate Coupons*, o desde el flujo normal del programa). **Esperado:** su correo **sí** conserva el marco estándar de Odoo (encabezado "Your Loyalty Coupon" y pie con "Powered by Odoo"), como antes de esta versión. Su botón **Send** también abre la ventana estándar. No lo generes para clientes reales.
- [ ] 11.11b Cupón "CW Sub" hecho a mano: en un programa "CW Sub – …" (ya con la plantilla Car Wash, ver 11.1) genera **un** cupón para el cliente de prueba (*Generate Coupons*). **Esperado:** su correo sale **sin** el marco de Odoo, igual que en 11.8, aunque no tenga periodo. Bórralo o déjalo vencer después; no lo generes para clientes reales.
- [ ] 11.12 Opcional, después de la salida a producción: reenvía (11.10) un cupón de prueba a un correo interno de WAYAKIT y revísalo en Gmail (web y celular) y, si se puede, en Outlook. **Esperado:** se ve igual que en la pestaña Mails, los textos se leen en modo oscuro y, con las imágenes bloqueadas, el texto alternativo dice "WAYAKIT" y el diseño no se rompe.

---

**Después de staging:** repite en **producción**, a mano, el plan Monthly, los productos con su configuración, el script de programas (incluida la revisión de "At creation"), Automatic Invoice y la cita **"Car Wash Subscription"** (10.1). Lo que hiciste en staging se pierde en el próximo build.
