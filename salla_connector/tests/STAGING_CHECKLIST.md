# salla_connector: staging checklist (odoo.sh `mainTest` build): F1 (A-I) and F2a (J-L)

Manual run by a human on the odoo.sh staging build. There is no local Odoo.
Run the standalone suite first: `python3 salla_connector/tests/test_mapping.py` must end in `all passed`.

Never paste real tokens, secrets or customer data into the ticket, this file or a chat.
Everything below uses shell placeholders; export them in your own terminal only.

```sh
export WEBHOOK_URL="https://<maintest-build-host>/sl/webhook"   # changes on EVERY rebuild, re-check it
export WEBHOOK_SECRET="<webhook secret of the Salla app, from the Partners Portal>"
export STORE_ID="<Salla Store ID shown on the backend after step 7>"
export UA="Mozilla/5.0 (X11; Linux x86_64) SallaStagingCheck"    # curl's own UA is blocked by Cloudflare on the prod zone
sig() { printf '%s' "$1" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | sed 's/^.* //'; }
post() {  # post '<body>' [signature]  -> prints the answer body + HTTP code
  # Explicit array, not ${2:+-H "..."}: zsh (macOS default) does not word-split that,
  # curl gets one mangled argument and every signed request falsely answers 401.
  local h=(-H 'Content-Type: application/json')
  [ -n "$2" ] && h+=(-H "X-Salla-Signature: $2")
  curl -sS -w ' | HTTP %{http_code} in %{time_total}s\n' -A "$UA" -X POST "$WEBHOOK_URL" \
    "${h[@]}" --data-binary "$1"
}
```
Always `--data-binary` (plain `-d` strips newlines and changes the bytes you signed).

Odoo shell helpers (odoo.sh web shell -> `odoo-bin shell`). They print fingerprints, never token values:
```python
import hashlib, threading
from odoo import api, SUPERUSER_ID
from odoo.modules.registry import Registry
BID = 1          # <- the salla.backend id
reg = Registry(env.cr.dbname)
fp = lambda v: hashlib.sha256((v or "").encode()).hexdigest()[:8] if v else "EMPTY"
def current():   # committed DB state, read on a fresh cursor
    with reg.cursor() as cr:
        b = api.Environment(cr, SUPERUSER_ID, {})["salla.backend"].browse(BID)
        return b.access_token, b.refresh_token, b.token_expires_at
```

Reading the answers: **403 + HTML ("Attention Required") = Cloudflare**, the request never
reached Odoo. **401 / 400 / 405 / 200 / 500 with a short text body = Odoo** (405 = right path,
GET instead of POST). 404 = wrong path or the module is not installed on that build.

## A. Install / upgrade

1. The odoo.sh build of `mainTest` is green and `salla_connector` **installs** (fresh) and then **upgrades** (next build) with no errors in the build log. The install itself validates the form view that declares `store_id` and `client_id` twice (see step 29).
2. Settings > Technical > Scheduled Actions: **"Salla: Refresh Tokens"** exists (xmlid `salla_connector.ir_cron_salla_refresh_tokens`), active, every **1 day**, code `model.cron_refresh_tokens()`.
3. **Access rights.** Use a user who is a sale manager but **not** an admin (Settings > Administration):
   - In the form: **Salla Store ID**, **Client ID** and **Access Token Expires** are shown **read-only**, once each. **Base URL**, **Token URL**, Client Secret, Webhook Secret, access/refresh token and **Log Full Webhook Payloads** are **not shown**. **Refresh Token Now** is not shown.
   - The UI is not the guard, the server is. In the shell (rolls back, changes nothing):
     ```python
     from odoo.exceptions import AccessError, ValidationError
     mgr = env["res.users"].browse(UID)   # UID = the non-admin sale manager
     B = env["salla.backend"].with_user(mgr).browse(BID)
     for vals in ({"store_id": "1"}, {"client_id": "FAKE_CLIENT"},
                  {"token_expires_at": "2000-01-01 00:00:00"},
                  {"base_url": "https://evil.example/admin/v2"},
                  {"token_url": "https://evil.example/oauth2/token"},
                  {"log_payloads": True}):
         try:
             B.write(vals); print("NOT BLOCKED", vals)
         except AccessError as e:
             print("AccessError OK", list(vals))
     try:
         B.action_refresh_token(); print("NOT BLOCKED refresh")
     except AccessError:
         print("AccessError OK refresh")
     env.cr.rollback()
     ```
     Expected: every line `AccessError OK`. Any `NOT BLOCKED` is a bug.
   - As **admin**, a non-Salla URL is rejected by the constraint (shell, then rollback):
     ```python
     A = env["salla.backend"].browse(BID)
     for vals in ({"token_url": "https://evil.example/oauth2/token"},
                  {"base_url": "http://api.salla.dev/admin/v2"},
                  {"base_url": "https://api.salla.dev:8443/admin/v2"},
                  {"token_url": "https://user:pw@accounts.salla.sa/oauth2/token"}):
         try:
             A.write(vals); A.flush_recordset(); print("NOT BLOCKED", vals)
         except ValidationError:
             print("ValidationError OK", vals)
         env.cr.rollback()
     ```
     Same in the UI: editing Token URL to `https://evil.example/oauth2/token` and saving shows the red *must be an https URL on a Salla host*.
   - A plain salesman: can open the backend, cannot save anything.

## B. Configure the backend and authorize (Easy Mode)

4. Create a backend as admin (company KSA, id 1): Client ID, Client Secret and Webhook Secret of the **demo store** Private App. **Type the demo store's Salla Store ID by hand now, before installing the app** (Partners Portal > demo store). Only an `app.store.authorize` can bind an empty Store ID, and it takes the *first* unbound backend; typing it removes the guesswork. (Left empty, step 7 still binds it, which is the fallback, not the plan.) Base URL / Token URL keep their defaults. `Webhook URL` shows `web.base.url` + `/sl/webhook`; correct it to this build if `web.base.url` points elsewhere.
5. **Log Full Webhook Payloads** is **off** by default. Leave it off for now.
6. In the Partners Portal, set the app's webhook URL to `$WEBHOOK_URL` of **this** build. The build hostname changes on every rebuild; prefer the stable branch domain if there is one.
7. Install the app on the Salla **demo store**. Within seconds `app.store.authorize` must arrive and the backend must show: **Salla Store ID** filled, **Access Token Expires** ~14 days ahead (UTC), **Granted Scopes** filled (readonly), and (as admin) access/refresh token set (masked).
   - Log: `Salla webhook app.store.authorize (store …), payload keys: [...]` (keys only, **no token values**, confirm it) and `authorized for store …`.
   - Nothing arrived: look for the controller line in the log. No line at all = URL/Cloudflare; 401 = the Webhook Secret on the backend differs from the Portal's.
8. **Test Connection** -> green `Connection OK: store <name> (<id>)`. A warning `the token belongs to store X … bound to store Y` means the Store ID on the backend is wrong. Red 401 = token; 403 + HTML = Cloudflare.

## C. Check Product SKUs (demo store, 4 products)

Prepare in the demo store:
| Salla product | SKU | Odoo state |
|---|---|---|
| P1 | an existing `default_code`, e.g. `FP-TEST-00001` | active product |
| P2 | `FP-TEST-00002` | product **archived** in Odoo |
| P3 | `FP-TEST-99999` | does **not** exist in Odoo |
| P4 with 2 variants | variant SKUs `FP-TEST-00003` (exists) and `FP-TEST-99998` (does not) | |

9. **Check Product SKUs** -> warning `N of M Salla SKUs need attention`: `FP-TEST-00002 (…) — ARCHIVED in Odoo`, `FP-TEST-99999 (…) — not in Odoo`, `FP-TEST-99998 (…) — not in Odoo`. P1 and `FP-TEST-00003` must **not** be listed. If `FP-TEST-99998` is missing, variants are not read (log: `variants not readable`).
10. M (total) matches the SKUs actually in the demo store, and the log has the full list (`Salla SKU check: … issues of …`). A product with no SKU at all shows as `— no SKU on Salla`. No `stopped at the 200-page cap` warning and no `asked for products page X, got Y` warning.

## D. Webhook trust boundary (curl)

11. No signature -> **401** `unauthorized`:
    `post '{"event":"order.created","merchant":1,"data":{}}'`
12. Bad signature -> **401**:
    `post '{"event":"order.created","merchant":1,"data":{}}' 0000000000000000000000000000000000000000000000000000000000000000`
13. Signed **empty** body -> **200** `ok:0`, fast (well under Salla's ~30 s):
    `post '' "$(sig '')"`
14. Signed **broken JSON** -> **400** `bad json`; the log says `unparseable body (N bytes)` and does NOT print the body:
    `B='{"event":"order.created",'; post "$B" "$(sig "$B")"`
15. Signed JSON that is **not an object** -> **400**:
    `B='[1,2,3]'; post "$B" "$(sig "$B")"`
16. Signed **valid** event for this store -> **200** `ok:ignored:app.settings.updated`; log line is `payload keys: [...]` only (log_payloads off):
    `B='{"event":"app.settings.updated","merchant":'"$STORE_ID"',"created_at":"2026-10-15 10:00:00","data":{"customer":{"first_name":"Fake","email":"fake@example.invalid"},"status":"ok"}}'; post "$B" "$(sig "$B")"`
17. **log_payloads on** (admin), resend step 16 -> the log now shows the full body with `"first_name": "***"`, `"email": "***"` and `"status": "ok"` readable. Resend a signed authorize (step 21's body) -> still **keys only**, never the body. Turn log_payloads **off** again.
18. Event for a store **no backend is bound to** (every backend already bound) -> **200** `ok:0` + WARNING `matches no backend allowed to take it; ignored`:
    `B='{"event":"app.settings.updated","merchant":1999999999,"data":{}}'; post "$B" "$(sig "$B")"`
19. Signature is over the **raw bytes**: step 16-style signature on the same JSON re-spaced -> **401**:
    ```sh
    B='{"event":"app.settings.updated","merchant":'"$STORE_ID"',"data":{}}'; S=$(sig "$B")
    B2='{"event": "app.settings.updated", "merchant": '"$STORE_ID"', "data": {}}'
    post "$B2" "$S"
    ```
20. Forged `app.store.authorize` (fake tokens, signed with a **wrong** secret) -> **401**, tokens and expiry unchanged (`current()` fingerprints identical before/after):
    ```sh
    B='{"event":"app.store.authorize","merchant":'"$STORE_ID"',"data":{"access_token":"FAKE_ACCESS_TOKEN","refresh_token":"FAKE_REFRESH_TOKEN","expires":1209600}}'
    post "$B" "$(printf '%s' "$B" | openssl dgst -sha256 -hmac 'not-the-secret' | sed 's/^.* //')"
    ```

## E. Token refresh

21. **Authorize without refresh_token must NOT blank the stored one.** Note `fp()` of the refresh token from `current()`, then:
    ```sh
    B='{"event":"app.store.authorize","merchant":'"$STORE_ID"',"data":{"access_token":"FAKE_ACCESS_TOKEN_NO_REFRESH"}}'
    post "$B" "$(sig "$B")"      # -> 200 ok:authorized
    ```
    Expected: refresh token **still set, same fingerprint**; Granted Scopes and Store ID unchanged; Access Token Expires ~**14 days ahead** (assumed); log WARNINGs `authorize carried no refresh token` and `authorize gave no readable expiry; assuming 14 days`.
    Now the access token is fake with a future expiry, so the **proactive** refresh does NOT fire (expiry is 14 days out): **Test Connection** -> Salla answers 401 -> exactly **one** refresh with the stored refresh token -> retry -> **green**. Log: exactly one `access token refreshed` line. With the expiry 14 days out the proactive path cannot fire (margin is 1 day), so that line can only come from the 401 -> refresh -> retry path. (There is no dedicated log line for the 401 retry; if you see two refresh lines, or none, report it.) If it ends red with `no refresh token`, the stored one was blanked: bug.
    Also: signed authorize with `"data":{}` -> **200** `ok:authorize:unparsed`, nothing changed.
22. Forced expiry (**admin only**: the field is read-only for everyone else and a non-admin write raises AccessError, step 3): set **Access Token Expires** to a past date (or < 1 day ahead), save, click **Test Connection** -> green; expiry ~14 days ahead; refresh fingerprint **changed** (single-use); one log line `access token refreshed, expires … UTC`.
23. Cron: as admin, put the expiry inside 1 day again, Scheduled Actions > "Salla: Refresh Tokens" > **Run Manually** -> same result as 22. Run it again at once -> **no** second refresh line. **Refresh Token Now** (admin) asks for confirmation, then shows `Token valid until … UTC`.
24. **Two simultaneous refreshes** (the single-use refresh token must never be sent twice). With the helpers loaded:
    ```python
    def go(n, stale, out):
        with reg.cursor() as cr:
            b = api.Environment(cr, SUPERUSER_ID, {})["salla.backend"].browse(BID)
            try:
                r = b._refresh_token(stale_access_token=stale, force=True)
                out[n] = ("ok", fp(r["refresh_token"]), r["token_expires_at"])
            except Exception as e:
                out[n] = ("raised", type(e).__name__, str(e)[:200])

    def race(use_stale):
        acc, ref, exp = current()
        print("before: refresh", fp(ref), "expires", exp)
        out = {}
        ts = [threading.Thread(target=go, args=(i, acc if use_stale else None, out)) for i in range(2)]
        [t.start() for t in ts]; [t.join() for t in ts]
        print("threads:", out)
        acc, ref, exp = current()
        print("after:  refresh", fp(ref), "expires", exp)

    race(use_stale=True)    # A: what two "Refresh Token Now" clicks do
    race(use_stale=False)   # B: two blind forced refreshes
    ```
    Expected:
    - **A**: exactly **one** `access token refreshed` log line. The other thread hits the lock (NOWAIT, retries every 3 s), sees the access token already changed and returns the new tokens **without** calling Salla. Both threads `ok`, same fingerprint.
    - **B**: **two** refresh lines, at least ~3 s apart (serialized by the lock), the second using the refresh token the first one just saved. Both `ok`; final fingerprint differs from "before". Neither `raised`.
    - Afterwards **Test Connection** is green. A red 401 / "Reinstall the app" here means a spent refresh token was resent: **bug, report it** (and the app must be reinstalled on the demo store).
    - `raised UserError … kept the token refresh lock for over 18 s` is acceptable only if Salla's token endpoint was slower than ~15 s; note it.
25. A second 401 right after a refresh must show the red `Salla answered 401 on <path> even with a freshly refreshed token. Either the app lacks the scope this endpoint needs ... or the store revoked the app (reinstall it).`, not loop (step 72 reproduces it on purpose with a missing scope) (only observable if Salla really rejects the fresh token; do not force it).

## F. Rate limiting (429)

26. Not forceable on the demo store without hammering it; do not try. On any real 429 in the log, check the wait honoured `Retry-After` (seconds or HTTP-date) or `X-RateLimit-Reset`, never exceeded 30 s, and after 3 attempts raised `Salla request … failed after 3 attempts: HTTP 429`. The arithmetic is covered by `test_retry_delay_*`.

## G. Odoo 17 assumptions nobody has executed yet

These four are API calls that only a real Odoo 17 can confirm. A wrong one shows up as a
`TypeError` / `AttributeError` / `ProgrammingError` in the log, usually as a 500 on the webhook.

27. **`cr.execute(..., log_exceptions=False)`** (token UPDATE, the lock SELECTs). Steps 7 and 22 already run it on success: no `unexpected keyword argument 'log_exceptions'` anywhere in the log. The point of the flag is checked in step 30: the failed query must **not** be logged with its parameters.
28. **`env.cache.set(record, field, value)`** (`_set_token_cache`): the caller's transaction must see the new tokens without re-reading its old snapshot. In the shell:
    ```python
    b = env["salla.backend"].browse(BID)
    before = fp(b.refresh_token)
    r = b._refresh_token(force=True)
    print("cache:", fp(b.refresh_token), "returned:", fp(r["refresh_token"]),
          "db:", fp(current()[1]), "before:", before)
    env.cr.rollback()
    print("db after rollback:", fp(current()[1]))
    ```
    Expected: cache == returned == db != before, and **db after rollback is still the new one** (the refresh committed on its own cursor; the rollback of the caller does not undo it, and nothing was written back from the cache).
29. **Duplicated `store_id` / `client_id` with `groups="!base.group_system"`** in the form: the install/upgrade passes view validation (step 1); as admin each field appears **once, editable**; as the non-admin sale manager each appears **once, read-only** (step 3). Showing twice, or not at all, is a bug.
30. **`SET LOCAL lock_timeout`** (an authorize/revoke write waits at most 12 s per attempt, 2 attempts on fresh cursors). Hold the row lock from the shell for **longer than 25 s**, then send a signed authorize with a fake token:
    ```python
    env.cr.execute("SELECT id FROM salla_backend WHERE id = %s FOR UPDATE", (BID,))
    # leave the shell open, do NOT commit; run the curl below now, roll back after it answers
    ```
    ```sh
    B='{"event":"app.store.authorize","merchant":'"$STORE_ID"',"data":{"access_token":"FAKE_ACCESS_TOKEN_LOCKTEST","refresh_token":"FAKE_REFRESH_TOKEN_LOCKTEST","expires":1209600}}'
    post "$B" "$(sig "$B")"
    ```
    Then `env.cr.rollback()` in the shell. Expected: one INFO `token row busy, retrying the write on a fresh cursor`, then the curl answers **500** `error` after **~24 s** (2 x 12 s; not instantly, not hanging until the rollback). The log has `processing failed` with a lock-timeout error, **`grep LOCKTEST` on the log finds nothing** (step 27: no query parameters logged), and `current()` fingerprints are unchanged. If the curl instead hangs until the rollback and then answers `ok:authorized`, `lock_timeout` was ignored: bug, and the stored refresh token is now the fake one; reinstall the app on the demo store.
    Variant: release the lock (`env.cr.rollback()`) **within ~10 s** of the curl -> **200** `ok:authorized` (the waiting write simply gets the lock, or retries once on a fresh cursor).
31. **Authorize colliding with a refresh** must be retried, **not** answered 500. Start a forced refresh and send a signed authorize while it is in flight (the refresh holds the row lock for the token POST):
    ```python
    out = {}
    t = threading.Thread(target=go, args=(0, None, out)); t.start()   # go() from step 24
    # within ~1 s, run the curl below in your terminal; then:
    t.join(); print(out, "refresh now:", fp(current()[1]))
    ```
    ```sh
    B='{"event":"app.store.authorize","merchant":'"$STORE_ID"',"data":{"access_token":"FAKE_ACCESS_TOKEN_COLLIDE","expires":1209600}}'
    post "$B" "$(sig "$B")"
    ```
    Expected: the curl answers **200** `ok:authorized` (possibly after waiting for the refresh), log may show `token row busy, retrying the write on a fresh cursor`, **no 500**. The refresh thread is `ok`. The refresh token is the one the refresh just obtained (the authorize carried none, so it did not blank it). The access token is now the fake one: **Test Connection** -> 401 -> one refresh -> **green** (same as step 21).

## H. Optional, destructive (last)

32. A signed `app.uninstalled` for `$STORE_ID` (or uninstalling the app from the demo store) -> **200** `ok:revoked`, access/refresh token and expiry cleared, WARNING `tokens cleared`. The app must then be reinstalled (back to step 7).
    Also: a signed `app.uninstalled` with **no** `merchant`, or with a merchant no backend is bound to -> **200** `ok:0`, nothing cleared.

## I. Before production (not F1 sign-off, but in this order)

33. Cloudflare Skip rule for `/sl/webhook` on `ksasystem.wayakit.com` exists **before** the prod URL is set in the app, **without** a method filter (see CLAUDE.md §11, Trendyol).
34. Create the prod backend and **type the prod Store ID by hand BEFORE installing the app on the real store and BEFORE pointing the app's webhook URL at prod**. The app's webhook secret is shared by the demo and real stores, and an `app.store.authorize` may bind the first **unbound** backend: if the prod backend is still unbound when the demo store re-authorizes, the demo tokens land on it. Once both backends are bound, an authorize can only reach the backend whose Store ID matches exactly (`test_pick_backend`).
35. Prod gets its own app installation and a fresh secret where possible; stage tokens never go to prod. An uninstall/revoke from the **demo** store must never touch a backend bound to another store: revoke only acts on an **exact** store match, never falls back to an unbound backend and never acts without a merchant (`pick_backend`, covered by `test_pick_backend_revoke`). Repeat step 32's "foreign merchant" curl against prod once both backends exist: prod tokens unchanged.

---

# salla_connector F2a: orders Salla -> Odoo (no payments, minimal customer)

Version `17.0.2.0.0`. Out of scope: payment registration, and the full customer
(phone/email dedup, national short address) = F2b. Run the standalone suite first, then
sections A-B above on the **new** build. F1 steps C-I only need repeating if F1 code changed.

How F2a works, in one line per piece (names from the code; check them in the UI):
- Webhook `order.created` / `order.updated` / `order.status.updated` -> one row in
  **`salla.order.queue`** (append-only: one row **per delivery**, no unique key, so
  duplicate rows for one order are expected and harmless) + `_trigger()` of the queue
  cron **"Salla: Process Order Queue"** (`ir_cron_salla_order_queue`), answer `ok:queued`.
  The webhook never calls Salla's API. Backend buttons: **Process Order Queue** and the
  **Queued Orders** smart button (the queue list = the to-do list).
- Queue cron: fetches the order + its items (Order Items endpoint), creates the
  `sale.order`, **deletes** the rows it read for that order (grouped by order, each order
  once per run). A missing SKU keeps one row `pending`; it is due again only **25 min**
  after its last try (button **Retry** on the row makes it due at once); repeated errors
  turn it `failed`. A row inserted while the cron works on that order survives and is
  processed on the next run.
- **"Salla: Reconcile Orders"** (every 30 min, button **Import Orders Now**): lists orders from `Orders Synced Until`
  (`last_order_sync`, UTC) minus a 6 h overlap, imports what is missing, and **holds** the
  cursor at the oldest order it could not import.
- `sale.order.salla_order_id` is SQL-unique (`sale_order_salla_order_uniq`).

## J. Rebuild preparation (every push to `mainTest` = a new build with a clean prod DB copy)

36. Build green; `salla_connector` installs/upgrades to **17.0.2.0.0** with no errors in the build log.
37. Redo F1 steps 4-8 on this build: create the backend (company **KSA, id 1**, warehouse **WH**), type the demo Store ID by hand, **update the Webhook URL in the Partners Portal** to this build's `$WEBHOOK_URL` (the build number changed), **reinstall the app on the demo store**, Test Connection green.
38. Backend, F2a fields: **Website** = the KSA website (id **2**), **Pricelist** in SAR, **Shipping Product**, **COD Fee Product** and **Discount Product** set, each of **Product Type = Service** with Customer Taxes = the **15 % sales tax, price-excluded** (Included in Price unchecked on the tax; a price-included tax would make every extra line 15 % short), **Notify on New Order** = your own user (empty = silent, only a WARNING), **Auto Confirm** on. Leave **Log Full Webhook Payloads** on only for steps 40-41, then off. A missing Shipping/COD/Discount product or pricelist does not import a wrong order: the queue row keeps `last_error` *backend has no shipping_product_id* (etc.) until it is set; check that once by leaving the COD product empty for step 45, then set it and press **Retry** on the queue row (Process Order Queue alone does not retry it before 25 min).
39. Scheduled Actions: **"Salla: Process Order Queue"** and **"Salla: Reconcile Orders"** exist, active, every **30 minutes**, code `model.cron_process_order_queue()` / `model.cron_reconcile_orders()`, next to "Salla: Refresh Tokens". The backend form shows the **Queued Orders** smart button. Sales > Orders: filters **Salla Orders** and **Salla Total Mismatch** exist.

Extra shell helpers (after the F1 ones). They print ids, states and amounts only:
```python
def orders(salla_id):   # every Odoo order for one Salla order id: must be 0 or 1
    with reg.cursor() as cr:
        e = api.Environment(cr, SUPERUSER_ID, {})
        rs = e["sale.order"].with_context(active_test=False).search(
            [("salla_order_id", "=", str(salla_id))])
        return [(o.id, o.name, o.state, o.amount_untaxed, o.amount_tax, o.amount_total,
                 o.salla_total, o.salla_total_mismatch, o.salla_status) for o in rs]
def lines(salla_id):
    o = env["sale.order"].search([("salla_order_id", "=", str(salla_id))])
    return [(l.product_id.default_code, l.product_uom_qty, l.price_unit, l.discount,
             l.tax_id.mapped("amount"), l.price_subtotal, l.price_total, l.salla_line_id)
            for l in o.order_line]
def queue():
    with reg.cursor() as cr:
        e = api.Environment(cr, SUPERUSER_ID, {})
        return [(q.salla_order_id, q.event, q.state, q.attempts, q.last_error)
                for q in e["salla.order.queue"].search([])]
def cursor():
    return current_backend_field("last_order_sync")
def current_backend_field(name):
    with reg.cursor() as cr:
        return api.Environment(cr, SUPERUSER_ID, {})["salla.backend"].browse(BID)[name]
```
Never paste the customer name, phone or address of a demo order into the ticket: only Salla order ids, amounts and SKUs.

**Demo store preparation** (fake data only, demo store `461067869`): products whose SKU equals an existing Odoo `default_code` (P1, P2, both active), one whose SKU matches an **archived** Odoo product (P4), one SKU that does **not** exist in Odoo (P3, e.g. `FP-TEST-99999`), a **20 %** coupon (`TEST20`), a paid shipping option with a known fee, free shipping (option or threshold), and Cash on Delivery enabled **with a COD fee**. Write down whether the demo store shows prices **including** VAT 15 % (Settings > Taxes): it decides what "unit price" means in step 41. **Never test with quantity 1** and never a coupon of 0.

For every order below, write down from the Salla dashboard: order id, reference, status, each line's qty and unit price, discount, shipping, COD fee, VAT and **total**. The Odoo `amount_total` must equal that Salla total to the halala (0.01 SAR), possibly through a "Rounding adjustment (Salla total)" line.

## K. Import, amounts and lines (the 7 mandatory cases)

40. **Webhook path, fast answer.** Place an order in the demo store. The `order.created` webhook answers **200 `ok:queued` in well under 1 s** (controller log line; the Portal's webhook log shows the delivery). The controller does **not** call Salla's API: the log shows the order queued, and the import lines come from the queue cron a few seconds later (`queue()` is empty again afterwards). With log_payloads on, the body is logged with `"first_name": "***"`, `"email": "***"`, `"mobile": "***"` and any `notes`/`note`/`gift_message` as `"***"`. Compare the real field names with `mapping.py` (`normalize_order`, `normalize_items`, `webhook_order_id`, `order_status`, `salla_date`) and write every mismatch in the ticket: this is the "validate against a real payload" step. Turn log_payloads off after the first 2-3 orders.
41. **Case 1 - 3 units of one product.** Order **3 x P1**. `lines()`: one product line, qty **3**, `price_unit` = Salla **unit** price net of VAT (unit / 1.15 if the store shows VAT-inclusive prices), **not** the line total and not 3x it; `discount` 0; tax 15. `amount_total` == Salla total. Also: `salla_order_id`, `salla_reference`, `salla_status`, `salla_total` filled; company **KSA (1)**; warehouse **WH**; website **id 2**; `x_studio_channel` = `B2C` if that Studio field exists on this DB (if it does not, no error). Customer: a partner with the Salla customer's name, country **Saudi Arabia**, `salla_customer_id` set, **no phone, no email, no address**.
42. **Case 2 - coupon 20 %.** Order **2 x P1 + 3 x P2** with coupon `TEST20`. Product lines at their full unit price (net, `discount` 0), plus **one separate discount line** (Discount Product, negative, = 20 % of the eligible goods net of VAT, taxed 15 %). `amount_total` == Salla total. The coupon is not folded into `price_unit` and not counted twice (order-level and item-level discount are the same money).
43. **Case 3a - paid shipping.** Order **3 x P1** with the paid option. A shipping line (Shipping Product) whose VAT-inclusive total equals Salla's shipping fee; `amount_total` == Salla total. Note in the ticket which reading the log says won (`Extras read as gross/net` appears only on a mismatch; otherwise compare the line with the Salla VAT breakdown).
44. **Case 3b - free shipping.** Order **3 x P1** with free shipping: **no** shipping line; `amount_total` == Salla total.
45. **Case 4 - COD.** Order **4 x P2** with Cash on Delivery: a **COD line** (COD Fee Product) whose VAT-inclusive total equals Salla's COD fee, plus the shipping line if any; `amount_total` == Salla total (a 0.01 "Rounding adjustment (Salla total)" line is acceptable and expected for some fees: `mapping.py` predicts a 10.00 COD fee as 8.6956522 net -> subtotal 8.70 + VAT 1.31 = **10.01**; write down what Odoo really shows on the COD line, 10.01 or 10.00, because it decides whether that rounding line is right). No payment registered (out of scope). A COD order in **under_review** is **confirmed** (COD accepted = prepare it); see step 63 for the bank-transfer counterpart.
46. **Total mismatch policy.** If Odoo cannot reach Salla's total within rounding: the order **exists** (never lost), stays a **quotation**, `salla_total_mismatch` is ticked, the chatter has an internal note *"Salla: the total does not match — Odoo total X vs Salla total Y (difference Z) ..."* that reaches the notify users, and the log a WARNING. To see it on purpose: give the Shipping Product **no tax** (staging only), import an order with paid shipping, then restore the tax. Fix the lines, then confirm by hand: confirmation works. **Abandoned cart check:** the flagged quotation carries website 2 and a customer, so while it sits as a draft it must not be picked up as an abandoned cart: it must not appear under Website > eCommerce > Abandoned Carts, and no "you left items in your cart" email is queued for it (Settings > Technical > Emails). Report it to the lead if it does.
47. **Case 5 - resend the same signed webhook: no second order.** Use the Salla order id of step 41 (`ORDER_ID`). Resend `order.created` twice with the same bytes and signature, then an `order.updated`, then an `order.status.updated` carrying an **older** timestamp and an older status (out of order):
    ```sh
    export ORDER_ID="<salla order id from step 41>"
    B='{"event":"order.created","merchant":'"$STORE_ID"',"created_at":"2026-10-18 10:00:00","data":{"id":'"$ORDER_ID"'}}'
    S=$(sig "$B"); post "$B" "$S"; post "$B" "$S"          # same bytes, same signature, twice
    B2='{"event":"order.updated","merchant":'"$STORE_ID"',"created_at":"2026-10-18 10:00:05","data":{"id":'"$ORDER_ID"'}}'
    post "$B2" "$(sig "$B2")"
    B3='{"event":"order.status.updated","merchant":'"$STORE_ID"',"created_at":"2026-10-18 09:00:00","data":{"id":1,"order":{"id":'"$ORDER_ID"'},"status":{"slug":"payment_pending"}}}'
    post "$B3" "$(sig "$B3")"                              # older, arrives last; data.id is NOT the order id
    ```
    Each answers **200** fast. After the queue cron ran: `orders(ORDER_ID)` returns **exactly one** order (same name as in step 41), no second "New Salla order" notice, `salla_status` is the **current** Salla status (re-read from the API, not the stale `payment_pending` of B3), the order is **not** cancelled or un-confirmed, and after the run no **pending** row is left for `ORDER_ID` in `queue()` (several rows may exist **while** waiting: one per delivery, by design). Log: already imported, no traceback.
48. **Concurrent identical webhooks: no 500.** Fire the same signed body 5 times **in parallel** (plain INSERTs, one row each, nothing to conflict on):
    ```sh
    for i in 1 2 3 4 5; do post "$B" "$S" & done; wait
    ```
    Every answer **200**, none 500, no `IntegrityError`/`duplicate key` traceback in the log. Up to 5 queue rows for that order while it waits (expected); after the queue run they are **all** gone and there is still exactly **one** order.
49. **Race webhook vs cron, and the SQL constraint itself.** Place a new order (**3 x P2**) and, within a few seconds of the webhook, run the reconcile cron manually (Run Manually). Exactly **one** order. If both workers collided, the log of the loser has an Odoo **ERROR `bad query`** line for the `sale_order` INSERT (`duplicate key value violates unique constraint "sale_order_salla_order_uniq"`) followed by the INFO `imported concurrently by another worker`: that ERROR is **expected** (Odoo logs the failed query before the savepoint catches it). Check that this line carries **no PII** (no customer name, phone or email in the logged parameters) and that nothing else failed: no 500, no `processing failed`, no second order. Then in the shell (rolls back, creates nothing):
    ```python
    import psycopg2
    o = env["sale.order"].search([("salla_order_id", "!=", False)], limit=1)
    try:
        with env.cr.savepoint():
            o.copy({"salla_order_id": o.salla_order_id}); env.cr.flush()
        print("NOT BLOCKED: duplicate salla_order_id accepted")
    except psycopg2.IntegrityError as e:
        print("UNIQUE OK", e.diag.constraint_name)    # sale_order_salla_order_uniq
    env.cr.rollback()
    ```
50. **Case 6 - lost webhook: the cron imports it.** Simulate Salla giving up after its 3 retries: in the Partners Portal set the webhook URL to a dead path for a moment (e.g. `$WEBHOOK_URL-off`), place an order of **3 x P1 + 2 x P2**, put the real `$WEBHOOK_URL` back. No Odoo order and no queue row (`orders(<id>)` = `[]`, `queue()` = `[]`). Press **Import Orders Now** on the backend (or Run Manually on "Salla: Reconcile Orders", or wait <= 30 min): the order is imported with the checks of step 41; the log has the run breakdown and the new cursor. Run it again at once: **nothing** created (all already imported). Variant without the Portal: place the order, then delete its queue row in the shell **before** the queue cron runs (`env["salla.order.queue"].search([("salla_order_id","=","<id>")]).unlink(); env.cr.commit()`), then run the reconcile cron.
51. **Case 7 - unknown SKU: whole order skipped, cursor held, imported later.** Note `cursor()`. Order **2 x P1 + 3 x P3** (`FP-TEST-99999`, not in Odoo). After the queue cron: **no** Odoo order, not even a partial one (`orders(<id>)` = `[]`); the queue row stays **`pending`** with a `last_error` naming `FP-TEST-99999`; log WARNING. Press **Import Orders Now**: the notification is a **warning** listing `FP-TEST-99999`, its *Cursor:* is that order's creation time (UTC), not now; still no order; after the commit `cursor()` is **not** past that order's creation time (held). Now create a product with `default_code` `FP-TEST-99999` (Sales tax 15 %) **on staging only** (it disappears with the next build) and press **Retry** on its queue row (Process Order Queue alone waits the 25 min): the order is imported with **both** lines, the queue row is gone, and the next reconcile run moves `cursor()` forward.
52. **Archived product still matches.** Order **3 x P4** (SKU of an **archived** Odoo product): imported, the line uses the archived product, no "missing SKU". If an active and an archived product share the same `default_code`, the **active** one is used.
53. **Archived partner reused, not duplicated.** Archive the partner created in step 41 (Contacts > Archive), then place a second order with the **same** Salla customer (**3 x P1**): the new order uses the **same, archived** partner (same id; `env["res.partner"].with_context(active_test=False).search_count([("salla_customer_id","=","<ref>")])` == **1**). Any name/country typed by hand on it is not overwritten. Unarchive it afterwards. An order with **no** Salla customer id goes to the shared guest contact (`salla_connector.partner_salla_guest`), never one contact per order.
54. **Order Items endpoint.** In the log of steps 41/42 the import fetched the items for the order (one extra GET per order), not an `expanded` order. Line count and quantities in Odoo == the Salla dashboard.
55. **Status filter.** An order left in **payment_pending** (bank transfer, not paid): not imported (no order), and it holds the cursor for up to 7 days. Mark it paid in Salla: the status webhook (or the next reconcile run) imports it and confirms it. A **cancelled** order is never imported; cancelling an **already imported** order posts one internal note *"Salla: the order is now "canceled"..."* and changes nothing else (resend its status webhook: no second note).

## L. Confirmation, notice and side effects

56. **New-order notice.** For each imported order, the notify users get one Inbox message *New Salla order ...* with **product - ordered - available**; a line ordering more than P1's free stock in WH shows *must be manufactured* and the order says *stock is short*. Shipping/COD/discount lines are not listed.
57. **Safe confirmation.** Paid statuses end in state **sale**. Force a confirm failure on staging only (e.g. a temporary automated action on `sale.order` that raises on state = sale, or as the dev suggests), import an order: it **exists**, stays a **quotation** (the savepoint rolled the confirmation back), and the chatter says *automatic confirmation failed ... (nothing was lost)*. The note must match the **real** state: never "left as quotation" on a confirmed order. Remove the forcing rule. **Auto Confirm** off: everything lands as a quotation.
58. **WhatsApp / automations on confirm (Mafer, UI).** Before the first import on each build: Settings > Technical > WhatsApp Templates, template **«Sale Order»**, and Settings > Technical > Automation Rules, **automation 1**: write down the model, trigger and **domain** (does it filter on website, team, partner phone, state?). Then, for every confirmed order of section K, check the chatter and the WhatsApp channel list: what was sent and to whom. Expected: **nothing sent and no error**, because the F2a partner has no phone. Anything sent, queued or failing goes to the lead **before** anything is disabled (do not switch off templates or rules yourself). Also check no customer email went out (Settings > Technical > Emails, filter on the order).
59. **Logs are clean.** Search the build log for the demo customer's name, phone and email: they appear only inside a log_payloads dump, masked as `***`. No token and no secret anywhere.
60. Turn **Log Full Webhook Payloads** off again.

## M. F2a round 2 (review findings)

61. **Missing-SKU pile does not loop the cron.** Create **at least 31** demo orders (each **3 x P3**, unknown SKU), or insert fake queue rows in the shell on staging only:
    ```python
    B = env["salla.backend"].browse(BID)
    for i in range(31):
        B._enqueue_order("FAKE-%05d" % i, "manual")
    env.cr.commit()
    ```
    (fake ids: each one ends as `error` *order not readable from the API*, which also retries; real P3 orders end as `missing_sku`.) Press **Process Order Queue** once, then watch Scheduled Actions > "Salla: Process Order Queue" (*Last Execution Date*) and the log for 5 minutes: at most **one** extra triggered run (the batch of 30 was full), then **quiet**; no run every few seconds, no line `order queue -> ...` repeating for the same rows. Each row's `last_try` is set, and the rows are retried again only **~25 min** later (next cron run after that). The fake rows end `failed` after 10 attempts; delete them afterwards (`env["salla.order.queue"].search([("salla_order_id","=like","FAKE-%")]).unlink(); env.cr.commit()`).
62. **Webhook in the middle of processing.** Order **3 x P1** with a slow import (e.g. right after step 61's batch, so the queue cron is busy), and change its status in Salla while the queue cron runs. The status webhook answers **200** at once (no waiting on the cron), adds a **new** row, and that row is processed on the **next** run (triggered automatically): the order ends with the **latest** Salla status, one order, no pending row left.
63. **under_review: bank transfer vs COD.** Two orders of **3 x P2**, both left in **under_review**: one paid by **bank transfer** (receipt uploaded, not yet approved by the merchant) and one **COD**. Bank transfer: imported as a **quotation**, no delivery created, with the internal note *Salla: status "under_review" with payment method "<method>": not confirmed until paid. The order was left as a quotation; confirm it by hand once the payment is verified.* Check that `<method>` is the **real** method of the payload (e.g. `bank`, as shown in the log_payloads dump of that order), not `unknown`: that confirms the payment-method field name in `mapping.order_payment_method`. `unknown` here means the field is misread, and then COD orders would not confirm either: report it to the dev. COD: **confirmed**, delivery created. Approve the bank transfer in Salla (status moves to in_progress / processing): the order is then confirmed by the status webhook or the next reconcile run.
64. **Loyalty / coupons on the customer and guest partner (Mafer, UI).** After the confirmed orders of section K, open the Salla customer partner and the **Salla Guest Customer**: no Odoo loyalty points, eWallet credit or gift-card/coupon programs were credited or consumed by these imports (Sales > Products > Discount & Loyalty: the program histories show no lines for these orders), and no reward line was added to any Salla order on confirmation (otherwise step 57's "total changed during confirmation" note appears). The Salla coupon `TEST20` must exist only as the discount line, never as an Odoo coupon code. Anything credited goes to the lead.
65. **VAT rate in the payload is not 15 %.** In the demo store, temporarily set the VAT to **0 %** (Settings > Taxes, or a tax-exempt product/customer) and place an order of **3 x P1**; restore 15 % right after. With log_payloads on, confirm the order's `tax.percent` (if Salla still sends 15, note it and skip). Expected, by case:
    - **A real rate other than 15 (`0`, `5`)**: the order is **imported** as a **quotation**, `salla_total_mismatch` ticked, log WARNING *Salla VAT 0% vs Odoo 15%, left as quotation*, and the internal note to the notify users *Salla: the order carries VAT 0% but Odoo applies 15% (Odoo total X vs Salla total Y). The order was NOT confirmed: check the tax settings of the Salla store and correct the order before confirming it.* No rounding line is added. It must **not** stay in the queue as an `error` row, not be retried every 25 min, and not end `failed`.
    - **A fraction (`0.15`) or an out-of-range value (`45`)**: `vat_rate()` reads it as 15 %, the log has only the WARNING *unexpected tax percent in the payload, using 15%*, and the order is **imported normally** (totals reconcile, confirmed if its status allows). These two cannot be produced from the store settings; they are covered by `test_vat_rate` / `test_normalize_order_vat_suspect_and_default`. Only check it if a real payload ever shows such a value.
    Delete the test quotation afterwards.

## N. F2a fix round 1 (staging findings 2026-10-18)

66. **Status webhook BEFORE created + missing SKU: the queue run does not crash, the rows stay pending, the other orders of the batch are imported.** This is the exact staging crash (`MissingError: salla.order.queue(<id>,) deleted` in `_queue_bookkeeping`, which rolled back the whole batch and left the queue undrained). Needs: one Salla order with an SKU not in Odoo (`MISS_ID`, e.g. **3 x P3**, case 7) and one normal order not yet in Odoo (`GOOD_ID`, **3 x P1**, placed with the webhook URL dead as in step 50, or its queue row deleted before the cron ran). Then in the shell, two rows for `MISS_ID` with the **status** row first (the real order of arrival), plus `GOOD_ID`:
    ```python
    MISS_ID, GOOD_ID = "<missing-sku order id>", "<normal order id>"
    B = env["salla.backend"].browse(BID)
    B._enqueue_order(MISS_ID, "order.status.updated")
    B._enqueue_order(MISS_ID, "order.created")
    B._enqueue_order(GOOD_ID, "order.created")
    env.cr.commit()
    queue()                                   # 2 rows MISS_ID + 1 row GOOD_ID, all pending
    B.with_user(1)._process_order_queue()     # must return counters, NO traceback
    env.cr.commit()
    queue()
    orders(GOOD_ID), orders(MISS_ID)
    ```
    Expected: the call returns e.g. `{'missing_sku': 1, 'created': 1}` with no exception; `queue()` shows **exactly one** row for `MISS_ID`, state `pending`, `attempts` **not** increased (a missing SKU never counts as an attempt), `last_error` naming the missing SKU, `last_try` set; **no** row left for `GOOD_ID`; `orders(GOOD_ID)` has **one** order, `orders(MISS_ID)` is `[]`. Run `B.with_user(1)._process_order_queue()` again at once: `MISS_ID` is **not** retried (not due for 25 min), nothing changes. Then press **Retry** on the `MISS_ID` row and run Process Order Queue: still exactly one row, still pending, no traceback.
67. **Same with an error outcome (attempts read before the duplicate is deleted).** Two fake rows for one id that the API cannot read, the **newer** one carrying the higher count (so reading only the kept row after the unlink would lose it):
    ```python
    B._enqueue_order("FAKE-DUP-1", "order.status.updated")
    B._enqueue_order("FAKE-DUP-1", "order.created")
    Q = env["salla.order.queue"].search([("salla_order_id", "=", "FAKE-DUP-1")], order="id")
    Q[-1].attempts = 3
    env.cr.commit()
    keep_id = Q[0].id
    B.with_user(1)._process_order_queue(); env.cr.commit()
    [(q.id, q.state, q.attempts) for q in env["salla.order.queue"].search(
        [("salla_order_id", "=", "FAKE-DUP-1")])]
    ```
    No traceback; exactly **one** row left, id == `keep_id` (the lowest), state `pending`, `attempts` = **4** (max of the group, 3, + 1). `attempts` = 1 means the count was read from the kept row only: report it to the dev. Delete it afterwards (`env["salla.order.queue"].search([("salla_order_id","=","FAKE-DUP-1")]).unlink(); env.cr.commit()`).
68. **A bookkeeping failure costs only its own order.** Staging shell only (the patch lives in this shell process and is gone when it exits). Queue `MISS_ID` (or a `FAKE-` id) and `GOOD_ID`-style normal order again (a new **3 x P2** order with its queue row in place), then make the bookkeeping raise for one order:
    ```python
    cls = type(env["salla.backend"])
    orig = cls._queue_bookkeeping
    def boom(self, order_id, group, outcome, detail):
        if order_id == MISS_ID:
            raise RuntimeError("forced bookkeeping failure (test)")
        return orig(self, order_id, group, outcome, detail)
    cls._queue_bookkeeping = boom
    B._enqueue_order(MISS_ID, "manual"); B._enqueue_order("<new 3 x P2 order id>", "manual")
    env.cr.commit()
    B.with_user(1)._process_order_queue(); env.cr.commit()
    cls._queue_bookkeeping = orig
    ```
    Expected: no traceback out of `_process_order_queue`; the log has a **WARNING** *Salla order `MISS_ID`: queue bookkeeping failed (outcome missing_sku), rows left as they were; retried next run* with the `RuntimeError: forced bookkeeping failure (test)` traceback under it; the 3 x P2 order **is imported** and its queue row is gone; `MISS_ID`'s rows are unchanged (still pending). Then run Process Order Queue normally: back to step 66's end state.
69. **COD fee present but not charged by Salla (real case: API-created order).** `create_order([("FP-ACC-00004", 3)], payment="cod", cod_fee=10, branch_id=BR)` with `tests/staging_api_orders.py`. Write down Salla's `sub_total`, `cash_on_delivery`, `tax` and `total`: in the staging finding the total was `sub_total x 1.15` (COD **not** included). Expected in Odoo: **no** COD line, `amount_total` == Salla total (no `salla_total_mismatch`, confirmed since COD + under_review), and an internal note in the chatter saying the COD fee was **dropped** because Salla did not charge it (with the fee amount, 2 decimals). Counterpart: a **dashboard** order with a COD fee that Salla **does** include in the total (step 45) still gets its COD line, and **no** "dropped" note.
70. **Customer IP and user agent masked.** With **Log Full Webhook Payloads** on, place one order and look at the dump: `source_details.ip` and `source_details["user-agent"]` (and `user_agent`, if present) are `"***"`; `amounts.shipping_cost`, `shipping` keys that are not PII, `description` and the item `sku`/`quantity` stay readable. Turn log_payloads off again.
71. **Amounts in notes have 2 decimals.** In every Salla chatter note/log line with money from steps 46, 57, 65 and 69 (total mismatch, rounding adjustment, COD dropped, VAT), amounts read like `370.09`, never `370.09000000000003`.
72. **401 for a missing scope: the message names it.** `show_branches()` from `tests/staging_api_orders.py` (the app has no `branches.read` scope). Expected: one forced refresh (log), then the error *Salla answered 401 on /branches even with a freshly refreshed token. Either the app lacks the scope this endpoint needs (add it to the app in the Salla Partners Portal, then reinstall the app on the store), or the store revoked the app (reinstall it).* Exactly **one** refresh for the call (no loop, no second refresh in the log). Test Connection right after is still **green** (the refresh did not break the token).
