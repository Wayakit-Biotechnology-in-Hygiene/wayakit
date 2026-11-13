# salla_connector: staging checklist (odoo.sh `mainTest` build): F1 (A-I), F2a (J-N), F2b (O), F2a post-meeting (P), F3 (Q), F5 (R), F8 (S), option images spike (T)

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
35a. **Prod backend Website stays EMPTY** (decided 2026-10-18). Since v17.0.2.1.2 a new backend has no default website; do not set one by hand. With website 2 set, `whatsapp_website_sale` + automation 4 sent WhatsApp template 7 twice and Salla orders mixed with web sales. Customer messages come from Salla; Odoo only sends the invoice email.
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
38. Backend, F2a fields: **Website** stays **EMPTY** (since v17.0.2.1.2 a new backend comes with no website, decided 2026-10-18; an existing backend keeps what it has: clear it by hand on staging), **Pricelist** in SAR, **Shipping Product**, **COD Fee Product** and **Discount Product** set, each of **Product Type = Service** with Customer Taxes = the **15 % sales tax, price-excluded** (Included in Price unchecked on the tax; a price-included tax would make every extra line 15 % short), **Notify on New Order** = your own user (empty = silent, only a WARNING), **Auto Confirm** on. Leave **Log Full Webhook Payloads** on only for steps 40-41, then off. A missing Shipping/COD/Discount product or pricelist does not import a wrong order: the queue row keeps `last_error` *backend has no shipping_product_id* (etc.) until it is set; check that once by leaving the COD product empty for step 45, then set it and press **Retry** on the queue row (Process Order Queue alone does not retry it before 25 min).
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
63. **under_review: card/BNPL confirmed, bank transfer held (v17.0.2.1.2).** Two orders of **3 x P2**, both left in **under_review** (Pending review): one paid by **card** (or Apple Pay / Tabby / Tamara, whichever the demo store offers) and, only if the demo store still lets you enable it, one by **bank transfer** (receipt uploaded, not yet approved by the merchant). COD is no longer offered (meeting 2026-10-18): do not test it. Card / BNPL: **confirmed** (state **sale**), delivery created, **no** "not confirmed" note. Bank transfer: imported as a **quotation**, no delivery created, with the internal note *Salla: status "under_review" with payment method "<method>" (bank transfer): not confirmed until the transfer is verified. The order was left as a quotation; confirm it by hand once the payment is checked.* Check that `<method>` is the **real** method of the payload (e.g. `bank`, as shown in the log_payloads dump of that order), not `unknown`; also write down the **exact** payment-method slug Salla sends for the card / Apple Pay / Tabby / Tamara order (e.g. `credit_card`, `apple_pay`, `tabby`, `tamara`, `mada`) and send it to the dev: an unexpected slug is still confirmed, but the bank-transfer guard only knows `bank` / `bank_transfer`, so a bank transfer arriving under another slug would be **confirmed unpaid**: report that as a blocker. Approve the bank transfer in Salla (status moves to in_progress / processing): the order is then confirmed by the status webhook or the next reconcile run. **Orders already imported before this deploy** and sitting as quotations in under_review are **not** re-confirmed by the upgrade (the import only acts when the Salla status **changes**): list them (`env["sale.order"].search([("salla_order_id","!=",False),("state","=","draft")]).mapped("name")`, names only) and either confirm them by hand once ops checked they are paid, or let them confirm when Salla moves them to in_progress.
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

## O. F2b: real customer and national address (v17.0.2.1.0, fix round 1 = v17.0.2.1.1)

**Fix round 1 (staging 2026-10-18, order 819051611 = S02372):** `GET /orders/{id}` is Salla's *light* format since 2026-09-01: **no** `shipping`, **no** `shipments`, `receiver` null, and `GET /shipments` needs the `shipping.read` scope the Partners Portal does not offer our app. The shipping address therefore comes **only** in the webhook body (`order.created` / `order.updated`), at `data.shipments[0].ship_to`. The webhook stores just that address on the queue row (`ship_to_json`, admin-only, deleted with the row after import); an order imported **without** its webhook (reconcile / Import Orders Now) gets no address and, if it is a shipping order, a note. `data.shipments[0].ship_from` is the **merchant's** origin and is never used.

Order of work: O.1 (preparation, then the one dashboard order that validates the field names) **before** anything else, because the shipping-address field names have never been seen in a real payload. Everything here is on the KSA company (id 1) and the demo store `461067869`. **Every amount test still uses quantity >= 2** (most use 3); a coupon is never 0.

PII rule for this section: the only real phone/email used is **Mafer's own**, on the demo test customer, so the WhatsApp step reaches nobody else. Never paste it, the national address or the short code into the ticket or a chat: write "matches / does not match", and use the masked helper below.

Extra shell helpers (after the J ones). They print **masked** contact data (`+96650*****67`, `m***@***`), never the full values:
```python
import re
mask_phone = lambda v: (v[:6] + "*" * max(len(v) - 8, 0) + v[-2:]) if v else "EMPTY"
mask_mail = lambda v: (v[0] + "***@***") if v else "EMPTY"
def P(ids):            # partner facts, active or archived
    rs = env["res.partner"].with_context(active_test=False).browse(ids)
    return [dict(id=p.id, active=p.active, company=p.company_id.id or None,
                 parent=p.parent_id.id or None, type=p.type, salla=p.salla_customer_id or None,
                 phone=mask_phone(p.phone), mobile=mask_phone(p.mobile),
                 phone_e164=bool(re.fullmatch(r"\+9665\d{8}", p.phone or "")),
                 mobile_e164=bool(re.fullmatch(r"\+9665\d{8}", p.mobile or "")),
                 email=mask_mail(p.email), country=p.country_id.code,
                 street=bool(p.street), city=bool(p.city), zip=bool(p.zip),
                 short_code_ok=bool(re.fullmatch(r"[A-Z]{4}\d{4}", p.x_national_short_code or "")),
                 short_code_set=bool(p.x_national_short_code)) for p in rs]
def order_partners(salla_id):   # customer / invoice / shipping partner of the imported order
    o = env["sale.order"].search([("salla_order_id", "=", str(salla_id))])
    return dict(order=o.name, partner=o.partner_id.id, invoice=o.partner_invoice_id.id,
                shipping=o.partner_shipping_id.id, fpos=o.fiscal_position_id.name,
                total=o.amount_total, salla_total=o.salla_total)
def by_ref(ref):       # every partner (archived too) carrying one Salla customer id: must be 1
    return env["res.partner"].with_context(active_test=False).search(
        [("salla_customer_id", "=", str(ref))]).ids
GUEST = env.ref("salla_connector.partner_salla_guest")
```

### O.1 Preparation and the real-payload check

73. Build green; `salla_connector` upgrades to **17.0.2.1.1** with no error in the build log (the new queue field `ship_to_json` needs the module upgrade: Apps > Salla Connector > Upgrade if the build did not do it). Redo step 37 (backend, Webhook URL in the Partners Portal, reinstall the app, Test Connection green). Run `python3 salla_connector/tests/test_mapping.py` locally first: it ends in `all passed`.
74. **Guest contact has no contact data (before any import).** `P(GUEST.ids)`: `phone`, `mobile`, `email` are all `EMPTY`, no street, no short code. Write it down: step 86 checks it is still so at the end.
75. **Mafer, demo store (Salla dashboard):** open the demo test customer and put **your own** mobile and **your own** email on it (nobody else's: step 94 sends a real WhatsApp to that number). Write down only *how* the dashboard shows the mobile (with `+966`? leading `0`? a separate country-code box?), not the number.
76. Backend: turn **Log Full Webhook Payloads** ON.
76b. **Staging is a copy of PROD: is your phone/email already on a contact?** Before step 77, in the shell (prints ids and masked facts only; type the values in your own shell, never in the ticket):
    ```python
    B = env["salla.backend"].browse(BID)
    MY_PHONE, MY_MAIL = "<your mobile in +9665... form>", "<your email, lower case>"
    by_phone = env["sale.order"]._salla_match_ids(B, phone=MY_PHONE)
    by_mail = env["sale.order"]._salla_match_ids(B, email=MY_MAIL)
    internal = env["sale.order"]._salla_internal_partners(
        env["res.partner"].with_context(active_test=False).browse(by_phone + by_mail)).ids
    print(by_phone, by_mail, internal); P(by_phone + by_mail)
    ```
    Ids listed in `internal` (your user's partner, a company partner) are never picked: ignore them. If **any other** id is listed, step 81 will **reuse** that existing contact (filling only its empty fields) instead of creating a new one, and steps 81-85 will not show what they describe. Either use a second email/mobile of yours that matches nothing (re-run until both lists hold only `internal` ids), or write down those ids and read step 81 as "reuse of that contact, only empty fields filled".
76c. **Pause the queue cron** (so step 77's queue row can be seen before it is imported): Settings > Technical > Scheduled Actions > **"Salla: Process Order Queue"** > untick **Active**, save. Webhooks still answer 200 and add rows; nothing imports them until it is active again. Do not leave it paused past step 77b.
77. **One dashboard order WITH shipping and a national address.** In the Salla dashboard, create an order for that customer: **3 x P1**, delivery = a **paid shipping** option (not pickup), a full KSA national address including a valid **short address / short code** (format `ABCD1234`: 4 letters + 4 digits; use a made-up one, e.g. `RRRD2929`, not a real home), building number, additional number, street, district, postal code, city. Write down the Salla order id (`ORD_SHIP`) and total.
77a. **The queue row carries the address while pending (keys only, never values).** With the queue cron still paused, in the shell:
    ```python
    import json
    rows = env["salla.order.queue"].sudo().search([("salla_order_id", "=", "<ORD_SHIP>")], order="id")
    for q in rows:
        d = json.loads(q.ship_to_json) if q.ship_to_json else None
        print(q.id, q.event, q.state, "ship_to_json:", sorted(d) if isinstance(d, dict) else d)
    ```
    Expected: at least one `order.created` (and maybe `order.updated`) row whose `ship_to_json` key list is **only** address keys (a subset of `name, address_line, address_line_two, street_number, building_number, additional_number, district, block, subblock, city, postal_code, short_address, ...` as the dev documented): **no** `customer`, `amounts`, `items`, `total`, `payment_method`, `ship_from` or anything else of the order body. A `order.status.updated` row has `ship_to_json` empty/None. Do not print `q.ship_to_json` itself.
    **Field access (server side, rolls back):** with the non-admin sale manager of step 3,
    ```python
    from odoo.exceptions import AccessError
    mgr = env["res.users"].browse(UID)
    Q = env["salla.order.queue"].with_user(mgr)
    print("ship_to_json" in Q.fields_get())              # False
    try:
        Q.search([("salla_order_id", "=", "<ORD_SHIP>")]).read(["ship_to_json"]); print("NOT BLOCKED")
    except AccessError:
        print("AccessError OK")
    env.cr.rollback()
    ```
    Expected: `False`, then `AccessError OK` (or no row visible at all to that user). The queue's form/list view as that user does not show the field. `NOT BLOCKED` -> PII exposed, report to the dev.
77b. **Re-activate the queue cron** (tick Active), then press **Process Order Queue** on the backend. After it ran: `env["salla.order.queue"].sudo().search([("salla_order_id", "=", "<ORD_SHIP>")])` is **empty** (the rows, and with them the stored address, are deleted after the import), and `orders(<ORD_SHIP>)` has one order. Continue with step 81 for the contact checks.
78. **Validate the field names in the log (the "validate against real payload" step for F2b).** In the build log find the masked dump of `ORD_SHIP`'s **webhook body** (the `GET /orders/{id}` detail has no address since Salla's light format, so do not expect it there). Known real paths (staging 2026-10-18): `data.shipments[0].ship_to.{type, name, email, phone, country, country_id, country_code, region.{id, name, code}, city, city_id, district, address_line, address_line_two, street_number, block, subblock, short_address, building_number, additional_number, postal_code, latitude, longitude, branch_id}`; `data.shipments[0].ship_from.{same keys}` (merchant origin); `data.shipping.{id, company, receiver.{name, email, phone}, shipper, pickup_address, address, branch_id}`; `data.customer.{id, full_name, first_name, last_name, mobile, mobile_code, email, city, country, country_code, location}`; `data.delivery_method` = `shipping`. Write in the ticket, by **key path only** (values are `***`):
    - any key of `ship_to` that is **new** or **missing** compared with the list above, and whether `shipments` holds more than one entry on this order.
    - `ship_to.type` (this one is not PII: write its value, e.g. `address`/`customer`) and `ship_from.type`.
    - `customer.mobile` and `customer.mobile_code`: present? Does the masked value show `***` for both (the format is still unknown from the log, see step 79).
    Compare every path with `mapping.py` (`shipping_address()` and the webhook address-extraction helper) and write each mismatch to the dev. **Masking check:** in that same dump, under **both** `ship_to` and `ship_from` and `shipping.receiver`, every one of `name`, `email`, `phone`, `city`, `district`, `address_line`, `address_line_two`, `street_number`, `block`, `subblock`, `short_address`, `building_number`, `additional_number`, `postal_code`, `latitude`, `longitude`, `region.name`, and `shipping.address` / `shipping.pickup_address` (whole or each key), plus `customer.{full_name, first_name, last_name, mobile, mobile_code, email, city, location}` shows `***`. Any value in clear -> stop, report it to the lead (PII leak), do not paste it.
79. **Phone format, without logging it.** In the shell, read the order detail once and print only the **shape** of the phone (digits replaced by `9`):
    ```python
    d = (B._request("GET", "/orders/%s" % "<ORD_SHIP>") or {}).get("data") or {}
    c = d.get("customer") or {}
    shape = lambda v: re.sub(r"\d", "9", str(v)) if v not in (None, "") else v
    print(shape(c.get("mobile_code")), shape(c.get("mobile")))
    ```
    (`B = env["salla.backend"].browse(BID)`.) Write the two shapes in the ticket, e.g. `+999 999999999` or `None 0599999999`. That is the real Salla format: tell the dev if `test_e164_*` does not cover it.
80. Backend: turn **Log Full Webhook Payloads** OFF again. Look at the log for 1 min after one more webhook: no new dump.

### O.2 First order and reuse

81. **First order creates a full contact.** After the queue cron imported `ORD_SHIP`: `order_partners(ORD_SHIP)` and `P([<partner>])`. Expected: a partner with the Salla name, `salla` = the Salla customer id, country **SA** (even though the payload `customer.country_code` may say something else: a real test payload said `MX`), the **same** E.164 number on **both** `phone` and `mobile` (`phone_e164` and `mobile_e164` True: template 7 reads `partner_id.phone`, the other sale.order templates `partner_id.mobile`), email set (trimmed, lower case), `short_code_ok` True. A **new** contact carries the order's address **on itself** (street, street2 = district/block, city, zip, short code), read from the **webhook's** `shipments[0].ship_to` (staging before the fix: contact reused and email filled, but **no** street/zip/short code: that is the bug this round fixes; `street`, `city`, `zip`, `short_code_ok` must all be True now). The address is the **receiver's** (`ship_to`), not Wayakit's warehouse (`ship_from`): open the contact and check the street/city are the ones typed in step 77, not the demo store's origin address: `shipping` == `partner` in `order_partners()`, no delivery child (`env["res.partner"].search([("parent_id","=",<partner>)])` is empty). `by_ref(<customer id>)` has **exactly one** id. `amount_total` == `salla_total` == Salla's total; fiscal position as on any KSA website order (compare with a recent website order: same name, or both empty).
82. **Second order reuses it (by Salla customer id).** Same customer, dashboard order **3 x P2** with shipping. Same partner id on the new order, `by_ref()` still one id, no delivery child created (same street + city as the contact's own address, compared ignoring case/spaces), totals equal. Then a third order **3 x P1** shipped to a **different** address: a **delivery** child (`type` delivery, `parent` = the customer, country SA) is created and set as `partner_shipping_id`; the customer's own address is unchanged. A fourth order **3 x P2** to that same different address **reuses** that child (no second child).
83. **Reuse by phone and by email (no Salla id match).** On staging only, clear the `salla_customer_id` of the partner from step 81 (`env["res.partner"].browse(<id>).salla_customer_id = False; env.cr.commit()`), then place a **new** order **3 x P1** for the same customer. Expected: the **same** partner is found (by phone), its `salla_customer_id` is written back (it was empty), no new partner. Then clear its `salla_customer_id` **and** its phone/mobile, place another **3 x P2** order: found by **email** (try it with the email typed in a different case / with a trailing space on the Odoo contact, e.g. `Fake.Name@Example.Invalid `: still found). Each time: `by_ref()` == one id, totals equal. Since phone **and** mobile were both empty, the email-matched import writes the number back on **both** (allowed: only empty fields are filled).
84. **Phone spelt differently is the same customer.** On the Odoo partner of step 81, **before each format**: clear `salla_customer_id` (the previous import wrote it back, and a match by Salla id proves nothing here), clear `mobile` (otherwise the E.164 value left there matches and the format in `phone` is never exercised), and type the format into `phone`. Then a new demo order **3 x P1** (or `B._enqueue_order("<new order id>", "manual")` + Process Order Queue: a manual row carries no address, so a **shipping** order queued that way also gets the step-91d "did not send the shipping address" note; expected, not a failure). Do one extra round the other way round: format in `mobile`, `phone` empty. Expected every time: the **same** partner is reused, no new partner, and **no** ambiguity note. Formats: `0 50 123 4567`-style (leading 0, spaces), `00966...`, `+966 5...` with spaces, `5...` (9 digits, no prefix). The Ops-typed spelling is **not** rewritten to E.164 (a found contact with a phone keeps it as typed; check `P()` shows the same masked value as before). Put the original E.164 value back at the end.
85. **A contact typed by hand by Ops keeps its data.** Create a contact by hand (fake data: name `Ops Test Contact`, your own phone as on the Salla customer, street `Ops Street 1`, city `Ops City`, zip `11111`, email `ops.test@example.invalid` **different** from the Salla one, short code `OPST1111`, a note in Internal Notes, country SA). Make sure no partner carries the Salla customer id (clear it on the step-81 partner and archive that partner, or use a second demo customer with your phone). Import a new order **3 x P1** with shipping to a **different** address and short code. Expected: the hand-made contact is reused (by phone); its name, email, phone, street, city, zip, short code and notes are **unchanged**; `salla_customer_id` was empty so it is now filled; `mobile` stays **empty** (the number is only written when phone **and** mobile are both empty, so a contact never carries two different numbers). The order's `partner_shipping_id` is a new **delivery** child with the Salla address and the new short code; the parent's typed address is **not** replaced. Chatter of the order: no error. If you took the clear-and-archive route, the archived step-81 partner still holds your phone/mobile, so **two** contacts match: the **active** Ops contact wins, and the chatter has the note *Salla: 2 contacts match this customer's mobile number; the order uses Ops Test Contact (id ...)*. That note is expected here, not a failure.
86. **Guest customer never matched, never written.** Place an order whose customer has **no** Salla id (if the dashboard can't, skip; the standalone tests cover it) and, separately, create a fake partner with your phone (step 85) while `GUEST` still has none. Expected: `P(GUEST.ids)` still has `phone`, `mobile`, `email` `EMPTY` (same as step 74), no address, no short code; a guest order **with** a shipping address ships to a **delivery child of the guest** (the guest itself stays empty); no order of a real customer ever lands on `GUEST` because of a phone/email match.
86b. **An employee's own contact is never the customer.** With your own phone/email on the demo customer (step 75), check after step 81 that `order_partners(ORD_SHIP)["partner"]` is **not** your user's partner (`env.user.partner_id.id`, and the partner of any other internal user with that phone) and that your user's partner got **no** `salla_customer_id`, address or short code. Expected: a **new** customer contact was created instead (internal users' partners, archived users included, a `res.company` partner, and any contact whose commercial partner is a `res.company` partner, are never candidates). The same holds if `ORD_SHIP`'s email equals your user's email. If it did: stop, report to the dev (dedup matched an internal user), and clear those fields by hand on staging.
87. **Archived and other-company partners.** (a) Archive the step-81 partner (Salla id intact), import **3 x P1**: same archived partner reused, nothing new (as step 53). Unarchive. (b) Clear its Salla id and set its **company** to Mexico (staging only), import **3 x P2**: it is **not** matched (a new KSA/no-company partner is created instead, or another KSA match); a partner with **no** company would be. Put the company back.
88. **Ambiguous phone/email match is deterministic + noted.** Two partners (fake names `Dup A`, `Dup B`), same phone (yours), **no** Salla id on either, both KSA. Import **3 x P1** for that customer. Expected: the partner picked is the one the dev's documented rule names (CLAUDE.md F2b section, e.g. lowest id), the order chatter has an internal note *Salla: 2 contacts match this customer's mobile number; the order uses <name> (id <id>). If they are the same person, merge them.* (no phone in the note). The dev's rule (`mapping.pick_partner`): contacts already linked to **another** Salla id and delivery/invoice/other addresses are never picked; then active first, top-level (no parent) first, **lowest id**. So here: the lower id of `Dup A` / `Dup B`. Import a second order **3 x P2**: the **same** one is picked again (now by Salla id). Archive the two fakes afterwards.

### O.3 National address and the short code

89. **Invalid short code: order imported + note.** Dashboard order **3 x P1** with shipping and a short address that breaks `^[A-Z]{4}\d{4}$` (e.g. `RRR29299`, or 3 letters / 5 digits; whatever the dashboard accepts). Expected: the order **exists** (no queue `error` row, no `ValidationError` in the log), confirmed per its status, the short code is **not** written on any partner (`short_code_set` stays as before), and the order chatter has an internal note *Salla: the National Address short code "<code>" is not valid (4 letters + 4 digits, e.g. RRRD2929), so it was not saved...* that also reaches the notify users (it is an alert). It quotes only the short code (cut to at most 32 characters), never the rest of the address. Totals equal. If the dashboard only accepts valid codes, forge it in the shell instead (creates nothing: everything is rolled back):
    ```python
    from odoo.addons.salla_connector.models import mapping
    b = env["salla.backend"].search([("store_id", "=", "461067869")], limit=1)
    buyer = mapping.buyer({"customer": {"id": "FAKE-F2B-89", "first_name": "Fake", "last_name": "Buyer",
                                        "mobile": "500000001", "mobile_code": "+966"},
                           "shipments": [{"ship_to": {"street_number": "Fake St", "city": "Riyadh", "short_address": "rrr 2929x"}}]})
    print(buyer["address"]["short_code_state"])                       # invalid
    p, ship, notes = env["sale.order"]._salla_partner(b, buyer)
    print(p.id, p.salla_customer_id, p.x_national_short_code, p.phone == p.mobile == "+966500000001", ship, notes)
    env.cr.rollback()
    ```
    Expected: `x_national_short_code` False, `True` for phone == mobile, `ship` empty (`res.partner()`), `notes` holds exactly one `("alert", ...short code...)`, and **no** `ValidationError`.
90. **Lower case / spaces short code is normalized, not rejected.** If the dashboard lets you type `rrrd 2929` (lower case, a space): the partner ends with `RRRD2929` (`short_code_ok` True), no "invalid" note.
91. **Existing short code typed by Ops is kept.** On the step-85 contact (short code `OPST1111`), import an order with a different valid short code: the contact still says `OPST1111` (only empty fields are filled; on a delivery child the child gets the new one, the parent keeps `OPST1111`).
91b. **Pickup first, shipping later: the address goes on the contact.** A **new** demo customer (second email/mobile of yours, or fake values with no WhatsApp expectation): first order **3 x P1** as **pickup**. The contact gets the city only (from `customer.city`), no street, no short code, no delivery child. Then a **shipped** order **3 x P2** for the same customer, in the **same city**, with a valid short code. Expected: street, zip and short code are written **on the contact itself** (a city alone does not count as "has an address"), and **no** delivery child is created. If a delivery child appears instead, report it to the dev (`mapping.address_fillable`: a contact with only the same city, or nothing, takes the address). Counter-check: a shipped order to a **different** city on a city-only contact does create a delivery child and leaves the contact's city alone.
91c. **An archived delivery child is not reused.** On the step-82 customer, archive its delivery child, then a new order **3 x P1** to that **same** different address. Expected: a **new**, active delivery child is created and used as `partner_shipping_id`; the archived child stays archived and unchanged. Report it to the dev if the archived child is reused or unarchived.
91d. **Lost webhook, shipping order: imported without address + note.** Pause the queue cron (step 76c). Dashboard order **3 x P2** with **paid shipping** and a valid short code (`ORD_LOST`). Delete its queue rows **before** anything imports them: `env["salla.order.queue"].sudo().search([("salla_order_id", "=", "<ORD_LOST>")]).unlink(); env.cr.commit()`. Re-activate the queue cron, then press **Import Orders Now**. Expected: the order **is** imported (`orders(<ORD_LOST>)` one order, totals equal, confirmed per its status, no queue `error` row); **no** address was written by this import (on a new contact: `street`, `zip`, `short_code_set` False; on a reused one: unchanged), no delivery child created for it; the order chatter has the internal note *Salla did not send the shipping address for this order (imported without its webhook): add it by hand before shipping.* and it **reaches the notify users** (alert). The note holds no address, phone or email.
91e. **Lost webhook, pickup order: no note.** Same as 91d with a **pickup** order **3 x P1** (`ORD_LOST_PICKUP`). Expected: imported, **no** "did not send the shipping address" note, no address written (only `city` from `customer.city` if it was empty, as before).
91f. **order.updated after the order is in Odoo: nothing changes (accepted in F2b).** On `ORD_LOST` (already imported without address), change something in the Salla dashboard that fires `order.updated` (e.g. the order's internal note or status). Expected: the webhook answers 200, the queue row is processed and deleted, the existing order and its partner stay as they were (no address added, no duplicate order, no traceback). Updating the address of an already-imported order is **not** built in F2b: if the address **does** appear, write it down (not a failure), if anything else changes report it to the dev.
91g. **Newest webhook wins.** Pause the queue cron. Dashboard order **3 x P1** with shipping to address A (`ORD_EDIT`); before re-activating the cron, edit the order's shipping address in Salla to a **different** street/city B (fires `order.updated`). Step 77a's snippet shows two rows with `ship_to_json`. Re-activate the cron, Process Order Queue. Expected: one order; the address used (contact or delivery child) is **B**, the newest row's; no row left. If the dashboard does not allow editing the address, skip and write so. Known limit (write it down if seen, not a failure): "newest" is the newest **row** (`max(id)` with an address), so a late Salla **retry** of the original `order.created` (address A) arriving **after** the `order.updated` (B) would make A win.
91g2. **Missing SKU keeps the address until the SKU exists.** Pause the queue cron. Dashboard order **2 x P1 + 3 x P3** (`FP-TEST-99999`, not in Odoo) with paid shipping and a valid short code (`ORD_SKU`); if possible also fire an `order.updated` on it (e.g. change its internal note) so there are 2 rows. Re-activate the cron, Process Order Queue. Expected: no Odoo order; `queue()` shows **one** `ORD_SKU` row, `pending`, `last_error` naming the SKU; step 77a's snippet shows that one row **still has** `ship_to_json` with the address keys (the newer duplicate's address was moved onto the kept row before deleting it). Create `FP-TEST-99999` on staging (as step 51), **Retry** the row: the order is imported **with** the address on the contact/delivery child, **no** "did not send the shipping address" note, the row is gone. Variant: instead of Retry, press **Import Orders Now** while the row still waits: the reconcile import also uses the queued address (same expectations).
91g3. **payment_pending keeps ONE queue row with the address until it is paid.** Dashboard order **3 x P2** paid by **bank transfer** (no receipt approved yet, so Salla reports it `payment_pending`) with **paid shipping** and a valid short code (`ORD_PEND`). After the queue run (Process Order Queue): `orders(<ORD_PEND>)` is `[]`; `queue()` shows **exactly one** `ORD_PEND` row, state `pending`, `last_error` `payment_pending`, `attempts` not increased; step 77a's snippet (admin shell, keys only) shows that row **has** `ship_to_json` with the address keys. If several webhooks arrived, the kept row is the newest one that carries an address. Run Process Order Queue again at once: nothing changes (the row is re-checked only every **~25 min**). Then mark it **paid** in Salla (approve the transfer). Either let the status webhook arrive, or pause the queue cron for that moment, delete only the **new** status row (`env["salla.order.queue"].sudo().search([("salla_order_id","=","<ORD_PEND>"),("ship_to_json","=",False)]).unlink(); env.cr.commit()`), re-activate it and wait for the ~25 min recheck. Expected: the order is imported **with** street/city/zip/short code on the contact or delivery child, **without** the "did not send the shipping address" alert, and **no** `ORD_PEND` row is left.
    **Negative check:** (a) a second bank-transfer order with shipping (`ORD_PEND_X`), **cancelled** in Salla before it is paid: after the next queue run its rows are **deleted** and none is left (`queue()` has no `ORD_PEND_X`), no order is created. (b) A payment_pending order **older than 7 days** (an old pending demo order, re-queued with `B._enqueue_order("<old id>", "manual")` + Process Order Queue): its rows are deleted, none left. Never leave a held row with an address behind on staging: delete leftovers at the end of the section.
91h. **Log clean of the stored address.** With **Log Full Webhook Payloads** OFF, search the build log for the period of steps 77-91g for the street, district, postal code, building number and short code you typed: they appear **nowhere** (neither from the webhook, the queue, the import nor a traceback). The log may show `ship_to_json` as a field name or a key list, never its values.
92. **Address does not change the money.** For steps 81, 82, 85 and 89: `amount_total` == Salla total to the halala, fiscal position the same as an F2a order of section K, taxes 15 % on every line, and no `salla_total_mismatch`. A delivery child with country SA must not switch the fiscal position.

### O.4 WhatsApp on confirmation (Mafer, UI)

93. **Before any import of this section** (repeat of step 58): Settings > Technical > Automation Rules, the automation that uses WhatsApp template 7 «Sale Order» (automation 4): model, trigger, **domain**, and which phone it uses (`partner_id.phone` for template 7; other sale.order templates use `partner_id.mobile`). Website 2: Settings > Website > *WhatsApp Template* set or empty. Write it down. Since v17.0.2.1.2 Salla orders carry **no** website (the backend Website stays empty), so website 2's template must **not** fire on them: see step 101.
94. **Confirm an order whose customer has a phone: no failure.** Order of step 81 (or 82), paid status: state **sale**, **no** "automatic confirmation failed" note. Chatter / WhatsApp: the confirmation message went to **your** phone (check your WhatsApp) in state sent/delivered, or, if the template/automation is off on this DB, nothing was sent and **no** error. A `whatsapp.message` in error state `phone_invalid` on an order whose partner has a phone is a **bug** (the phone was written in a format WhatsApp rejects): report to the dev with the masked shape from step 79.
95. **Unusable phone is not written.** On staging only, change the demo customer's mobile in Salla to something unusable (e.g. `12345`, or a landline-like `011...` if the dashboard accepts it), and import **3 x P1** for a **new** demo customer with that number (or clear phone/mobile on the step-81 partner first). Expected: the partner's `phone`/`mobile` stay **EMPTY**, the order chatter has an internal note *Salla: the customer's phone number is not a valid Saudi mobile, so it was not saved on the contact...* (a plain note, not an alert), the order **confirms** (state sale), no "confirmation failed" note. Put your mobile back in Salla.
96. **Logs clean.** Build log search for your phone (last 4 digits), your email, the short code, the street and the postal code: they appear **nowhere** except, while log_payloads was on, as `***`. No ambiguous-match / invalid-phone / invalid-short-code log line contains the value itself.
97. Clean-up: remove your own phone and email from the demo test customer in Salla if you don't want future test orders to WhatsApp you; archive `Ops Test Contact`, `Dup A`, `Dup B`; **Log Full Webhook Payloads** off.

## P. F2a post-meeting changes (v17.0.2.1.2, meeting with Salla 2026-10-18)

Decisions: payments **only** Apple Pay, Tabby, Tamara and credit/debit cards (every order arrives fully paid, no COD, no bank transfer); customer messages come from Salla, Odoo only sends the invoice email; shipping is Aramex via Salla's default integration (label printed in Salla). Code changes: `under_review` is confirmed unless the payment method is a bank transfer (`mapping.should_confirm`, covered by `test_status_filter` and `test_should_confirm_under_review_vs_bank_transfer`); a new backend has **no** default website. Run the standalone suite first: it must end in `all passed`.

98. Build green; `salla_connector` upgrades to **17.0.2.1.2** with no error in the build log. Redo step 37 (Webhook URL in the Partners Portal for this build, reinstall the app on the demo store, Test Connection green).
99. **New backend has no website.** Shell or UI on staging: `env["salla.backend"].new({"name": "Fake Default Check"}).website_id` is empty (`website()` / falsy), or open Salla > Backends > New and check the **Website** field is empty and its help says to leave it empty. Discard it (never save a second backend bound to the demo store).
100. **Existing backend not changed by the upgrade.** The staging backend (a copy of prod, or the one re-created in step 37 before the upgrade) keeps whatever **Website** it had before the upgrade: the upgrade does not clear it. Then **clear it by hand** on staging (Website empty) for the steps below.
101. **Order without website.** With Website empty, import one **3 x P1** order paid by **card** in status **completed** (or in_progress), with a **non-zero coupon** (e.g. `TEST20`) and paid shipping. Expected: `env["sale.order"].search([("salla_order_id","=","<id>")]).website_id` is **empty**; the order is **not** in Website > Orders / eCommerce reporting; state **sale**; `amount_total` == Salla total; **no** "abandoned cart" mail, and on confirmation WhatsApp template 7 «Sale Order» is sent **at most once** (by automation 4 if its domain matches; never twice, never by `whatsapp_website_sale`). Check Settings > Technical > Emails / the chatter: no customer email except the invoice email if an invoice is posted.
102. **under_review paid by card / BNPL.** Re-run step 63 on this build (card or Tabby/Tamara under review -> confirmed; bank transfer if still available -> quotation with the bank-transfer note). Write down the payment-method slug of each order.
103. **payment_pending still not imported.** A demo order left in **payment_pending** (e.g. a Tabby/Tamara checkout abandoned before paying, if the demo store can produce one): not imported, held in the queue as in step 91g3. Completing the payment imports and confirms it. A **cancelled** order is never imported (step 55).

# salla_connector F3: order status Odoo -> Salla, and cancellations

Version `17.0.3.0.0`. Decided with Ops 2026-10-21: validating the **WH/OUT by hand** puts the
order in **shipped** in Salla; a failed push never undoes the validation, it alerts Ops (activity);
a Salla cancellation before shipping cancels the order and its WH/OUT, after shipping it only
alerts Ops. No lock on delivery. Out of scope: shipments/tracking, stock (F4), invoices/refunds (F5).
Run the standalone suite first (`python3 salla_connector/tests/test_mapping.py` -> `all passed`;
the F3 decisions are `test_ship_push_*`, `test_cancel_action_*`, `test_push_next_try_bounded`,
`test_is_permanent_error`, `test_order_cancelled_is_queued`).

Pieces (names from the code; check them in the UI):
- `sale.order`: `salla_push_pending` ("shipped" while waiting), `salla_push_attempts`,
  `salla_push_next_try`, `salla_push_error`, `salla_push_alerted` (one-shot), existing
  `salla_pushed_status` (set to `shipped` on 201) and `salla_alerted_status` (first cancel status handled).
- `salla.backend`: **Ops Alerts** (`ops_alert_user_ids`, empty -> falls back to Notify users) and
  **Send Status SMS** (`send_status_sms`, default on).
- Cron **"Salla: Push Order Status"** (`salla_connector.ir_cron_salla_push_status`, 30 min backup,
  `_trigger()`ed when a WH/OUT is validated or cancelled). Retries after 5 / 30 / 120 min, then
  gives up and alerts once. A 4xx (except 408/425/429) gives up at once.
- Button **Push Status to Salla** (`action_salla_push_status`) on the sale order header: hidden when state != sale or `salla_pushed_status == "shipped"`. Writes nothing before the call; on failure keeps `pending`, stores the error, sets attempts = 4 and `next_try` empty, shows a sticky warning, creates **no** activity (the person who clicked sees it).
- Sales filter **Salla Push Failed** (`salla_push_failed`) = `salla_push_pending` set AND `salla_push_next_try` empty (retries exhausted, or the button failed).

Extra shell helpers (after the F1/F2a ones). Ids, states and statuses only, never customer data:
```python
def push(salla_id):   # committed state of the push fields
    with reg.cursor() as cr:
        o = api.Environment(cr, SUPERUSER_ID, {})["sale.order"].search(
            [("salla_order_id", "=", str(salla_id))])
        return dict(name=o.name, state=o.state, salla_status=o.salla_status,
                    pending=o.salla_push_pending, pushed=o.salla_pushed_status,
                    attempts=o.salla_push_attempts, next_try=o.salla_push_next_try,
                    error=o.salla_push_error, alerted=o.salla_push_alerted,
                    alerted_status=o.salla_alerted_status)
def picks(salla_id):  # every picking of the order: (name, type, state)
    o = env["sale.order"].search([("salla_order_id", "=", str(salla_id))])
    return [(p.name, p.picking_type_code, p.state) for p in o.picking_ids]
def acts(salla_id):   # open activities on the order: (summary, user login)
    o = env["sale.order"].search([("salla_order_id", "=", str(salla_id))])
    return [(a.summary, a.user_id.login) for a in o.activity_ids]
def run_push():       # run the push cron now, as the scheduler would
    env.ref("salla_connector.ir_cron_salla_push_status").method_direct_trigger()
    env.cr.commit()
```
Read the Salla side with `GET /orders/{id}` (dashboard or `tests/staging_api_orders.py`): only `status.slug`.

Replay a cancel webhook by hand (zsh and bash; uses `sig`/`post` from the top of this file). The
queue re-reads the order from Salla, so this only re-delivers: the real status comes from Salla.
```sh
export ORDER_ID="<Salla order id of the demo order under test>"
cancel_body() { printf '{"event":"order.cancelled","merchant":%s,"data":{"id":%s}}' "$STORE_ID" "$ORDER_ID"; }
B="$(cancel_body)"; post "$B" "$(sig "$B")"        # expect: ok:queued | HTTP 200
B="$(cancel_body)"; post "$B" "$(sig "$B")"        # same body again: also ok:queued (one more queue row, harmless)
```

## Q. F3: shipped push and cancellations (v17.0.3.0.0)

### Q.1 Build and configuration
104. Build green; `salla_connector` upgrades to **17.0.3.0.0** with no error in the build log. Redo step 37 (Webhook URL in the Partners Portal for this build, reinstall the app on the demo store, Test Connection green).
105. Backend form: **Ops Alerts** and **Send Status SMS** visible with help text. Set **Ops Alerts** = your own user only on staging (never ghanimah / the Ops account on a staging DB: activities would notify real people). **Send Status SMS** = **off** on staging (no SMS to the demo customer). Scheduled Actions: **"Salla: Push Order Status"** exists, active, every 30 min, model sale.order.
106. Leave **Ops Alerts** empty once, and check in step 111 that the activity goes to the **Notify on New Order** users instead (fallback). Then set it back to your user.

### Q.2 WH/OUT validated -> shipped in Salla
107. Import a new demo order **3 x P1** with a **non-zero coupon**, paid (card) in **under_review** or **in_progress**; it confirms (state sale) and has one WH/OUT. `push(ORDER_ID)`: pending empty, pushed empty.
108. Validate the WH/OUT **by hand** (quantities done = 3). The validation finishes normally (no error dialog, no wait on Salla). Right after: `push(ORDER_ID)["pending"] == "shipped"`. Within ~1 min (cron triggered) or after `run_push()`: `pushed == "shipped"`, `pending` empty, `attempts == 0`, `error` empty, **no** activity. Salla: `GET /orders/{id}` -> `status.slug == "shipped"`; the dashboard shows it shipped. The build log line for the push shows the order id and HTTP 201 only (no token, no customer data).
109. Salla answers with `order.status.updated`: `salla_status` becomes `shipped`, **no** second push (`run_push()` again: nothing sent, `pushed` still `shipped`), no new chatter message, no loop of webhooks in the log.
110. **Status already ahead.** In the Salla dashboard set another imported demo order (3 x P2) to **delivering** (or delivered/completed) **before** validating its WH/OUT; wait for the webhook (`salla_status` updated). Validate the WH/OUT: `run_push()` sends **nothing** (Salla stays at delivering, not moved back to shipped), the order is marked as handled (`pending` empty), no activity. If the button is still visible, pressing it shows *nothing to push, Salla already at delivering*.

### Q.3 Push failure -> activity, validation kept, retry works
111. **Break the push on staging only:** backend **Base URL** = `https://api.salla.dev/admin/v2/fake-broken` (same host, so the constraint accepts it; every API call answers 404). Import (before breaking it) and confirm a new **3 x P1** order, then break the URL and validate its WH/OUT. Expected: the WH/OUT stays **done** (never rolled back), the order stays **sale**; after `run_push()`: `attempts >= 1`, `error` shows the HTTP code (no token, no URL query), and since 404 is permanent `alerted == True` and `pending == "shipped"` at once. One activity **"Salla: status push failed"** per Ops Alerts user (or per Notify user with Ops Alerts empty, step 106) + an `_salla_alert` note. The order appears under the **Salla Push Failed** filter (`pending` set, `next_try` empty). While the URL is still broken, press **Push Status to Salla** once: sticky warning *Salla refused or did not answer: ...*, `attempts == 4`, `next_try` empty, still **one** activity per user (the button adds none).
112. **Alert only once.** `run_push()` twice more and wait for the 30 min cron once: still exactly **one** activity per user (`acts(ORDER_ID)`), no new note.
113. **Transient failure retries** (optional, if you can cut the network or get a 5xx): `attempts` grows, `next_try` moves 5 -> 30 -> 120 min, and the alert comes only after the 4th failure. If it cannot be reproduced on staging, write "not run" in the ticket: the timing is covered by `test_push_next_try_bounded`.
114. **Retry.** Restore **Base URL** = `https://api.salla.dev/admin/v2` (exactly; Test Connection green). On the order press **Push Status to Salla**: a notification says it was pushed, `pushed == "shipped"`, `pending` empty, `error` empty, Salla shows **shipped**. The button disappears once pushed. Mark the activity done by hand. Check a **salesman** user can press it and a portal/no-sales user cannot see it.

114a. **Safety net for an unmarked order (`_salla_sweep_unmarked`).** At the start of every run, the push cron marks orders that are in state sale, have an outgoing picking done in the last **2 days**, and have neither `pending` nor `pushed` set. Validating a WH/OUT triggers the cron within seconds. So first **archive** the scheduled action "Salla: Push Order Status" (Active off), then take a new **3 x P1** order, validate its WH/OUT, and clear the mark in the shell: `o = env["sale.order"].search([("salla_order_id","=",ORDER_ID)]); o.write({"salla_push_pending": False}); env.cr.commit()`. Then `run_push()` (it runs even with the action archived). Expected: in that run, `pending` is set again by the sweep and then pushed: `pushed == "shipped"`, Salla shows **shipped**. `run_push()` again: nothing is sent. **Set the scheduled action Active again.**
114b. **First F3 build: older orders get swept.** Right after the upgrade of step 104, before validating anything, run `push(<id>)` on every Salla order whose WH/OUT was validated in the last 2 days (if any). The first cron run pushes **shipped** for those unless Salla is already at/after shipped or cancelled. On staging the backend points to the demo store, so this only touches demo orders. Write down which ones were pushed. **Before production go-live (Mafer):** the same sweep runs on the first production run, and the real customer may get a status SMS if Send Status SMS is on. Check beforehand which Salla orders in production have a WH/OUT done in the last 2 days and are not yet shipped in Salla.

### Q.4 Backorder: no push until the last picking
115. Import a new **3 x P1** order (P1 with stock, or set quantities by hand). On the WH/OUT set done = **2** of 3, validate, **Create Backorder**. Expected: `pending` empty or the decision `wait` (nothing sent), `run_push()` sends nothing, Salla still in its previous status. `picks(ORDER_ID)` shows one `done` + one open WH/OUT. Pressing **Push Status to Salla** here shows *not every delivery validated* and sends nothing.
116. Validate the backorder (the last unit). Now the push runs once: Salla **shipped**, `pushed == "shipped"`.
117. Variant: a backorder that is **cancelled** instead of validated (`done` + `cancel`): the push runs (something did ship). An order whose only WH/OUT is cancelled: never pushed.
118. A **return** (WH/IN or Return of a done WH/OUT) on the order of step 116: nothing is pushed, `pushed` unchanged (returns are ignored).

### Q.5 Cancellation in Salla
119. **Cancel before shipping.** Import + confirm a new **3 x P2** order (WH/OUT ready, not validated, **no invoice**). Cancel it in the Salla dashboard. Within ~1 min (`order.cancelled` and/or `order.status.updated` queued, then processed): the sale order is **cancel**, its WH/OUT is **cancel** (`picks(ORDER_ID)`), `alerted_status` is set, a chatter note pings the Notify users, **no** activity. Variant: on a BB/BC/CC-style order whose internal **pick/pack** picking is already done and that has a manufacturing order, the note also says *Already picked/packed (names): put that stock back* and *Check the related manufacturing orders*; on the plain order of this step neither line appears. The order never asks for a wizard (no "Cancel order?" dialog left pending).
120. **Repeated webhook, nothing duplicated.** Run the two replay `post` lines above with that `ORDER_ID`, then let the queue process (or press **Process Order Queue**). Expected: no new note, no activity, no error on the queue rows, no new order, state still cancel. Replaying the same cancel body hours later (after the order was re-read) also changes nothing.
121. **Cancel after shipping.** Take the order of step 108 (WH/OUT done, Salla shipped). Cancel it in Salla (if the dashboard does not allow cancelling a shipped order, use **restoring**/return, or write "not possible on Salla" in the ticket). Expected: the sale order stays **sale**, the WH/OUT stays **done**, one activity **"Salla: cancelled after shipping"** per Ops Alerts user + alert note. Replay the webhook (step 120 commands): still **one** activity.
121a. **Ops alert user without Sales rights (reviewer BLOCKER 1).** On staging only, create a fake internal user `Fake Stock Only` (login `fake.stock.only@example.invalid`, Inventory = User, Sales = **no access**, no other Sales group), add it to **Ops Alerts** next to your own user, and keep **Notify on New Order** = your own user. Repeat step 121 on a new shipped **3 x P1** order (WH/OUT done, Salla shipped, then cancel/restoring in Salla). Expected:
    - The queue row ends **done**, not in error. The order stays **sale** and the WH/OUT stays **done**.
    - `Fake Stock Only` gets the **inbox ping**: the alert note pings the Notify users **and** the Ops Alerts users. Log in as that user or check Discuss > Inbox.
    - The **"Salla: cancelled after shipping"** activity: dev expects Odoo 17 to create it for `Fake Stock Only` even without read access to sale orders. Write down which case happened:
      - (a) `acts(ORDER_ID)` lists it for `Fake Stock Only` and for your own user. Log in as `Fake Stock Only` and open Activities: note whether the user can open the order or gets an access error.
      - (b) It could not be assigned to `Fake Stock Only`, so it went to the **Notify users**. The order chatter has a note naming `Fake Stock Only` (a name only, no email or phone).
    - Either way: no activity has an empty user, and nothing goes in the queue row's error.
    - Replay the cancel webhook (step 120 commands): still one activity per user, no second note.
    - Archive `Fake Stock Only` afterwards.
121b. **Access of the real Ops accounts (read-only, on production, by Mafer in the UI; no change).** Settings > Users: open **ghanimah** and the **general Operations account**, and write down in the ticket for each one whether it has a Sales right that can read sale orders (Sales > *User: Own Documents Only* or higher). Answer yes/no only, no other user data. If either one has no Sales right, step 121a's fallback is what will happen in production. Mafer and the Ops lead decide whether to grant the right or keep the fallback before go-live. Claude does not change users.
122. **Partial shipment + cancel:** order with one WH/OUT done and a backorder open (step 115 before step 116), cancelled in Salla: treated as shipped (activity only, nothing cancelled automatically, the open backorder stays for Ops).
123. **Posted invoice, not shipped** (staging only, **never** post in the production INV journal; on staging the DB is a copy and ZATCA must be neutralized: check the journal shows no live ZATCA submission before posting, else skip and write "not run"): order with a posted invoice and an open WH/OUT, cancelled in Salla -> nothing cancelled, activity **"Salla: cancelled, invoice posted"**.
124. **Auto-cancel failure** (optional): if any cancel raises (e.g. a WH/OUT locked by another module), the order stays as it was, activity **"Salla: automatic cancellation failed"**, and the queue row is not stuck in error.
125. **Delivered / completed do nothing else.** Set the step-108 order to **delivered** then **completed** in Salla: only `salla_status` changes; the order is **not** locked (lines still editable by a salesman), no push, no activity.
126. Clean-up: backend **Base URL** back to the default (step 114), **Ops Alerts** = what prod will use is set **on production by Mafer**, not here; **Log Full Webhook Payloads** off. Record in the ticket: Salla order ids, statuses and HTTP codes only.

## R. F5: invoices, payments, credit notes, upload to Salla (v17.0.4.0.0)

**STAGING ONLY.** Never post a test invoice or credit note on the production database: every posting in INV is reported to ZATCA and cannot be undone. Never put a move's access token, a full PDF URL, customer data or a journal's bank details in the ticket or a chat. The helpers below print status codes and field values only, never the URL or the token.

Odoo shell helpers (paste after the ones at the top of this file; `BID` = the backend id):
```python
import requests
from odoo.addons.salla_connector.models import mapping
UA = "Mozilla/5.0 (X11; Linux x86_64) SallaStagingCheck"
b = env["salla.backend"].browse(BID)
ORDER_ID = "<Salla order id of the demo order under test>"   # set again for each order
def so(salla_id):
    return env["sale.order"].search([("salla_order_id", "=", str(salla_id))])
def moves(salla_id):  # committed state of the order's invoices / credit notes
    with reg.cursor() as cr:
        o = api.Environment(cr, SUPERUSER_ID, {})["sale.order"].search(
            [("salla_order_id", "=", str(salla_id))])
        ms = o.invoice_ids | o.invoice_ids.reversal_move_id
        return [dict(id=m.id, name=m.name, type=m.move_type, state=m.state,
                     edi=m.edi_state, total=m.amount_total, pay=m.payment_state,
                     up=m.salla_upload_state, sid=m.salla_invoice_id,
                     attempts=m.salla_upload_attempts, next_try=m.salla_upload_next_try,
                     err=m.salla_upload_error, alerted=m.salla_upload_alerted,
                     pay_alerted=m.salla_payment_alerted, posted_marker=m.salla_upload_posted)
                for m in ms.sorted("id")]
def pays(salla_id):   # payments on the order's invoices: (journal, amount, memo)
    inv = so(salla_id).invoice_ids.filtered(lambda m: m.move_type == "out_invoice")
    return [(p.journal_id.name, p.amount, p.ref) for p in inv._get_reconciled_payments()]
def acts_move(move_id):
    m = env["account.move"].browse(move_id)
    return [(a.summary, a.user_id.login) for a in m.activity_ids]
def run_inv():        # run the invoice/upload cron now, as the scheduler would
    env.ref("salla_connector.ir_cron_salla_invoices").method_direct_trigger()
    env.cr.commit()
def salla_docs(salla_id):   # what Salla holds for the order: (id, slug, invoice_number, date)
    res = b._request("GET", "/orders/invoices", params={"order_id": int(salla_id)}) or {}
    return [(d.get("id"), d.get("slug"), d.get("invoice_number"), d.get("date"))
            for d in (res.get("data") or []) if isinstance(d, dict)]
def pdf_check(move_id, token=None, mid=None, name=None):
    """GET the public PDF route from the server itself. Prints HTTP code, content type and
    the first bytes only (never the URL or the token)."""
    m = env["account.move"].browse(move_id)
    url = mapping.invoice_pdf_url(b._public_base_url(), mid or m.id,
                                  m.access_token if token is None else token,
                                  m.name if name is None else name)
    if not url:
        return "no URL (empty base / id / token)"
    r = requests.get(url, timeout=90, headers={"User-Agent": UA}, allow_redirects=False)
    return r.status_code, r.headers.get("Content-Type"), r.content[:5]
```

### R.1 Build, ZATCA safety and configuration
127. Build green; `salla_connector` upgrades to **17.0.4.0.0** with no error in the build log. Redo step 37 (Webhook URL in the Partners Portal for this build, reinstall the app on the demo store, Test Connection green). Check that the backend **Webhook URL** is the public **https** URL of this build: invoice PDF URLs are built from it.
128. **ZATCA safety gate. Do this before ANY posting in this section.** In the shell: `env.company.l10n_sa_api_mode` and `env["res.company"].browse(1).l10n_sa_api_mode` must both be **`sandbox`** (odoo.sh neutralization sets it). If either one is `prod` or `preprod`, **STOP**: post nothing and write "R not run: ZATCA not neutralized" in the ticket.
129. Accounting > Configuration > Journals (staging, by hand): create three **Bank** journals, **Salla Payments**, **Tabby** and **Tamara**, for company 1 (codes e.g. `SLP`, `TBY`, `TMR`). Each one needs an incoming payment method (the default "Manual" is enough).
130. Backend form, new groups **Invoices / Upload to Salla / Payments**. Every field shows its help text. Expected defaults:
    - **Invoice Confirmed Orders** **off** (default since review round 1).
    - **Invoice Journal** = the KSA sales journal **INV**.
    - **Invoice Orders Confirmed From** = about the creation/upgrade time.
    - **Upload Invoices to Salla** on.
    - **Require ZATCA Acceptance Before Upload** on. Its help says it **must stay on in production**.

    Before turning it on, check once that with it **off** nothing is invoiced: import one **3 x P1** order, it confirms, `moves(ORDER_ID)` is empty after `run_inv()`. If you cancel it in Salla now (nothing shipped, no invoice) it is simply cancelled as in F3. Then turn **Invoice Confirmed Orders** **on**: with it off, R.2-R.6 do not apply, and a cancellation with a posted invoice only gives the F3 Ops alert **"Salla: cancelled, invoice posted"**.
    Set the three payment journals and **Accounting Alerts** = your own user only (never Ismail, ghanimah or Hiba on staging). Then turn **Require ZATCA Acceptance Before Upload** **off** (staging cannot get ZATCA acceptance). Leave **Payment Method Mapping** empty.
131. Settings > Technical > Scheduled Actions: **"Salla: Invoices and Upload"** exists, is active, runs every 30 min on model account.move.
132. Salla demo dashboard: invoicing mode is still **Custom** (if it says "Salla", Salla issues its own invoice on shipping and the test is meaningless).

### R.2 Confirmed order -> invoice posted + payment in the right journal
Use **fresh** demo orders in this section: older demo orders may hold test uploads from 2026-10-22.
133. Import a new demo order **3 x P1** with a **non-zero coupon** (e.g. TEST20) and paid shipping. It confirms (state sale). Within ~1 min, `moves(ORDER_ID)` shows exactly **one** `out_invoice`, state **posted**, in **INV**, with `total` == the Salla total to the cent (compare with `GET /orders/{id}` `amounts.total`). The invoice lines are 3 x P1 + discount + shipping, the same as the order. If it is **draft** instead: read the activity (**"Salla: invoice left in draft"** = total differs; **"Salla: invoice not posted"** = posting refused, e.g. ZATCA checks; no invoice at all: **"Salla: invoice not created"** / **"Salla: nothing to invoice"** on the order, which then shows **Salla Auto-Invoice Failed**). Write down which one happened and the order id, then continue with step 134 on another order.
134. **Payment.** API orders on the demo store are COD (`tests/staging_api_orders.py`), and COD never registers a payment: expect an activity **"Salla: payment not registered"** saying the method `cod` is not mapped, `pay_alerted == True`, invoice still `not_paid`. Then simulate a card order on that same invoice, in the shell:
    `o = so(ORDER_ID); o.salla_payment_method = "mada"; inv = o.invoice_ids[:1]; inv.salla_payment_alerted = False; env.cr.commit(); run_inv()`
    Expected:
    - `pays(ORDER_ID)` -> one payment in **Salla Payments** for the full total, memo = the Salla reference (`o.salla_reference`).
    - Invoice `pay` = `in_payment` or `paid`.
    - Run `run_inv()` again: still **one** payment.

    Repeat on two more fresh orders with `"tabby_installment"` -> **Tabby** journal and `"Tamara-Installment"` -> **Tamara** journal. If the demo store can take a real card/Apple Pay test payment, prefer one real one and write down the slug Salla sent (`o.salla_payment_method`) in the ticket.
135. **Unknown method + mapping.** Same simulation with `"stc_pay"`: no payment, one **"Salla: payment not registered"** activity, `run_inv()` twice -> still one activity. Then backend **Payment Method Mapping** = `stc_pay = salla_payments`, reset `inv.salla_payment_alerted = False; env.cr.commit(); run_inv()` -> payment in **Salla Payments**. Last, write `bank_transfer = salla_payments` in the mapping and simulate `"bank_transfer"` on another order: still **no** payment (COD and transfers are never mapped). Clear the mapping afterwards.
136. **No invoice email (reviewer (d)).** On the invoice of step 133:
    - The chatter has no "Invoice sent" / email message.
    - Shell: `env["mail.mail"].search_count([("model","=","account.move"),("res_id","=",<invoice id>)])` == 0.
    - `env["mail.message"].search_count([("model","=","account.move"),("res_id","=",<invoice id>),("message_type","=","email")])` == 0.

    Same 0 for the sale order (`model = "sale.order"`). Write down only the counts.
137. **Total mismatch never invoiced.** An order that came in as a quotation with **Salla Total Mismatch** (any from section K/N; else write "not run"): confirm it by hand, then `run_inv()`. Expected: **no** invoice (`moves()` empty), no activity. Accounting invoices it by hand if wanted: once posted, that invoice is uploaded like any other.
138. **Confirmed by hand later -> invoiced by the cron sweep.** A Salla quotation **without** mismatch (e.g. a bank transfer kept as a quotation, step P; if none can be produced write "not run"): confirm it by hand. Within ~1 min or after `run_inv()`: one posted invoice as in step 133. An order confirmed **before** "Invoice Orders Confirmed From", or more than 14 days ago, is never invoiced by the cron.

### R.3 Upload to Salla ("Require ZATCA" off on staging)
139. **Under review -> in_progress -> upload.** Use the invoice of step 133 (the order still **under_review** in Salla; if it already moved, use a new order and leave it in under_review). After `run_inv()` (or ~1 min after posting):
    - The **order** chatter has the note *order set to "in_progress" in Salla so the invoice can be uploaded*.
    - `GET /orders/{id}` -> `status.slug == "in_progress"`.
    - `moves()` shows the invoice with `up == "done"`, `sid` set, `posted_marker == True`, `attempts == 0`, `err` empty.
    - The invoice chatter has *uploaded to Salla as tax_invoice (Salla id ...)*.
    - `salla_docs(ORDER_ID)` lists exactly **one** entry with slug 1 and that id.
    - In the Salla dashboard, the order shows the invoice. Open the **Salla Invoice File** link on the invoice (Salla's CDN copy): it is Odoo's invoice PDF with the right number and total. Write down whether the **ZATCA QR** is visible (on staging it may be missing or a placeholder: the journal's production CSID is neutralized). A rendering error instead of a PDF is a bug: report it to the developer.
140. **Never moves Salla backwards.** Take an order already **shipped** or **delivering** in Salla (from section Q, with an invoice posted now via step 138, or invoice it by hand). After the upload, Salla's status is unchanged (no in_progress note on the order). Credit notes never change the Salla status.
141. **Repeated cron, never a second upload.** `run_inv()` three times, then wait for one scheduled run. Expected:
    - `salla_docs(ORDER_ID)` still shows **one** tax invoice.
    - `sid` is unchanged and there are no new chatter notes.
    - The **Upload Invoice to Salla** button is hidden on the uploaded invoice. If you call it from the shell (`env["account.move"].browse(<id>).action_salla_upload_invoice()`), it raises *Already uploaded to Salla*.
142. **Button vs cron race (reviewer (c)). Run this after step 148,** on its `failed` invoice with the Webhook URL already restored (instead of step 149's single press): press **Upload Invoice to Salla** and run `run_inv()` in the shell at the same moment. Expected: `salla_docs()` lists **one** tax invoice from Odoo (the button only queues; the cron is the only uploader).
143. **Public PDF URL (`/sl/invoice/...`, reviewer).** In the shell, on the invoice of step 133 (`MID = <its id>`), with `OTHER = <id of another posted Salla invoice>`. All three bad cases must look identical (same 404, nothing about which check failed), and the build log must never contain the token.
    - `pdf_check(MID)` -> `(200, 'application/pdf', b'%PDF-')`.
    - `pdf_check(MID, token="0" * 36)` -> `404` (wrong but well-formed token).
    - `pdf_check(MID, token=env["account.move"].browse(OTHER).access_token)` -> `404` (another move's valid token).
    - `pdf_check(MID, mid=999999999)` -> `404` (unknown move).
    - `pdf_check(MID, name="anything")` -> `200` (the file name is cosmetic).
    - A posted invoice that is **not** from a Salla order (e.g. a website or manual one, staging only): `pdf_check(<its id>)` -> `404`, even with its own token. A **draft** Salla invoice -> `404`.
    - In zsh, only a status check from your own machine: `curl -sS -o /dev/null -w '%{http_code}\n' -A "$UA" "$BUILD_URL/sl/invoice/1/fake-token/x.pdf"` -> `404`, where `export BUILD_URL="https://<maintest-build-host>"`. Do **not** put a real token on the command line.
    - After the 200 call: `env["ir.attachment"].search_count([("res_model","=","account.move"),("res_id","=",MID)])` is the same before and after (a GET must not create attachments; write down both counts).

### R.4 Cancellation and credit notes
144. **Cancel before shipping, invoice already uploaded.** A new **3 x P2** order: imported, confirmed, invoiced and uploaded (as steps 133 and 139), WH/OUT ready but **not** validated. Cancel it in the Salla dashboard. Within ~1 min:
    - `moves()` shows one `out_refund` **posted** that reverses the invoice in full (same total). Its reference/reason says *Order ... cancelled on Salla (canceled)*.
    - The sale order is **cancel** and its WH/OUT is **cancel** (`picks(ORDER_ID)`).
    - One activity **"Salla: refund to make by hand"** for your user, naming the payment method.
    - After `run_inv()`, the credit note has `up == "done"` and `sid` set. `salla_docs()` = one slug 1 + one slug 2.
    - Salla's status stays **canceled** (not moved to in_progress).
145. **Repeated cancel webhook (reviewer (b)).** Replay the cancel twice (commands at the top of section Q, same `ORDER_ID`), process the queue and run `run_inv()`. Expected: still **one** credit note, one "refund to make by hand" activity, `salla_docs()` unchanged, no queue error.
146. **Cancel before the invoice was uploaded (reviewer (a)).** A new **3 x P1** order. Before it is uploaded, archive **"Salla: Invoices and Upload"** (Active off) so the invoice stays `up == "pending"`. Cancel the order in Salla and let the queue process it. Then set the cron **Active again** and `run_inv()`. Expected:
    - Exactly **one** credit note, posted; order and WH/OUT **cancel**.
    - The invoice ends `up == "skipped"` with the note *is not uploaded to Salla: nothing the customer needs there*. The credit note also ends `skipped`.
    - `salla_docs()` is **empty**.
    - No activity **"Salla: upload on hold"**, nothing left `pending`.

    Optionally (shell, write down the HTTP code + Salla's message only), check what Salla answers to an invoice POST on a cancelled order: `b._request("POST", "/orders/invoices", payload={"order_id": int(ORDER_ID), "type": "tax_invoice", "data_type": "url", "url": "https://pdfobject.com/pdf/sample.pdf"})`. If it answers 200, that order now has a sample document: do not reuse it.
146a. **Accounting already has a DRAFT credit note (reviewer NIT 3).** Use a new **3 x P1** order, invoiced and uploaded, WH/OUT not validated. Accounting opens **Reverse** on the invoice and leaves the credit note in **draft**. Then cancel the order in Salla and let the queue process. Expected:
    - Odoo creates **no** second credit note: `moves()` shows the invoice plus only the draft credit note, still **draft** and untouched.
    - The order stays **sale** and its WH/OUT stays open (not cancelled).
    - There is one Ops activity **"Salla: cancelled, invoice posted"**.
    - Replaying the webhook (step 145 commands) adds no second activity.
147. **Cancel after shipping: unchanged F3 behaviour.** An invoiced order with its WH/OUT **done**, cancelled in Salla:
    - The order stays **sale**, there is **no** credit note, and the activity is **"Salla: cancelled after shipping"** (Ops).
    - Then Accounting makes the return's credit note **by hand**: Reverse the invoice with a reason, partial (e.g. 2 of the 3 units) or full, and post it. Expected: it is queued (`up == "pending"`) and uploaded as **credit_note** after `run_inv()` (`salla_docs()` gains a slug 2).
    - Repeat with a **second** partial credit note on the same invoice: both credit notes end with **different** `sid`s.

### R.5 Failure, one alert, button retries
148. **Bad public URL (Salla cannot read the PDF).** Backend **Webhook URL** = `https://example.invalid/sl/webhook` (staging only; Salla's webhooks keep going to the URL registered in the Partners Portal). Post the invoice of a new order (or press the button on a pending one) and `run_inv()`. Expected:
    - Salla answers 422 (`url`), which is permanent, so on the first failure: `up == "failed"`, `attempts == 1`, `err` shows the HTTP code / Salla's field message, `alerted == True`. If `err` or the activity quotes the URL, the token part must show `***`. Check this in the UI; never copy the URL into the ticket.
    - One activity **"Salla: invoice upload failed"** for your user.
    - The invoice appears under the **Salla Upload Failed** filter.
    - `run_inv()` twice more: still **one** activity, no new note.
149. **Retry.** Restore **Webhook URL** = this build's public URL. Press **Upload Invoice to Salla** on the failed invoice: a notification says it was queued. Within ~1-2 min (or `run_inv()`): `up == "done"`, `sid` set, `err` empty, `salla_docs()` lists **one** tax invoice (no duplicate from the failed attempt, which Salla refused). The button disappears. Mark the activity done by hand. Check that a user **without** Accounting > Invoicing rights does not see the button, and that calling it as that user raises *Only Accounting can upload*.
150. **Transient failure** (optional): with backend **Base URL** = `https://api.salla.dev/admin/v2/fake-broken` (404, permanent) the alert comes at once. A real 5xx/network retry (5 -> 30 -> 120 min, alert after the 4th failure) usually cannot be produced on staging: write "not run". Timing is covered by `test_push_next_try_bounded`. Restore the Base URL exactly.
151. **ZATCA required (staging check of the gate only, nothing is uploaded).** Turn **Require ZATCA Acceptance Before Upload** back **on** and post a new order's invoice. Expected: the upload does **not** run. `up == "pending"` and `edi` == `to_send` or with an error. After `run_inv()` there is one activity **"Salla: upload on hold"**, and `salla_docs()` is empty. Turn it **off** again only if more uploads are needed, then leave it **on** at the end of this section.
152. Clean-up and record:
    - Backend Webhook URL and Base URL back to the build defaults.
    - **Accounting Alerts** stays your user on staging; production users (Ismail / Accounting) are set **on production by Mafer**.
    - **Require ZATCA Acceptance Before Upload** is **on**.
    - **Log Full Webhook Payloads** is off.
    - Record in the ticket: Salla order ids, Odoo move names, statuses, HTTP codes, the payment slugs seen, and whether the QR showed in Salla's copy (step 139). No tokens, URLs with tokens, customer data or bank details.
153. **Before production go-live (Mafer, F7, no code):**
    - Extend the Cloudflare Skip rule to `/sl/invoice/` (path starts with) next to `/sl/webhook`, **before** the first upload. Salla's PDF fetch must reach Odoo: a 403 HTML answer means Cloudflare blocked it, and Salla would answer 422 "url does not point to a valid PDF".
    - Create the three journals and set them on the production backend.
    - Keep **Require ZATCA Acceptance Before Upload** on.
    - Check **Invoice Orders Confirmed From** = the cut-over time, so no older order gets invoiced in INV.

### R.6 Reviewer blockers (run after R.1-R.5, staging only)
154. **Lost answer, no duplicate POST (BLOCKER 1).** In the odoo.sh shell (this patch lives only in that shell process; `run_inv()` runs in the same process):
    ```python
    import requests as _rq
    from odoo import fields
    _real = _rq.request
    CALLS = []
    def _flaky(method, url, *a, **kw):
        resp = _real(method, url, *a, **kw)          # Salla really receives it
        if method.upper() == "POST" and url.rstrip("/").endswith("/orders/invoices"):
            CALLS.append(resp.status_code)
            if len(CALLS) == 1:
                raise _rq.ReadTimeout("staging fake timeout after Salla accepted")
        return resp
    _rq.request = _flaky
    ```
    1. Import + confirm a new **3 x P1** order with "Require ZATCA" off. Keep the cron archived until the invoice is posted, then `run_inv()` **once**. Expected:
       - `CALLS` has exactly **one** entry: the invoice POST is never resent within one call.
       - `moves()`: the invoice has `up == "pending"`, `attempts == 1`, `posted_marker == True`, `sid` empty.
       - `salla_docs(ORDER_ID)` already shows **one** slug-1 entry, the one Salla created.
    2. Make it due: `m = env["account.move"].browse(<id>); m.salla_upload_next_try = fields.Datetime.now(); env.cr.commit(); run_inv()`. Expected:
       - `CALLS` still has **one** entry: no new POST.
       - The invoice is `up == "done"` and `sid` = the id from `salla_docs()`.
       - The chatter has *was already on Salla (Salla id ..., an earlier upload whose answer was lost); not uploaded again*.
       - `salla_docs()` lists exactly **one** tax invoice.
    3. Restore: `_rq.request = _real`. Exit the shell afterwards.
155. **Invoicing never undoes the import (BLOCKER 2).**
    - **Variant A** (needs an order whose payment method maps to a journal, e.g. a real demo card payment). Point **Salla Payments Journal** to a staging bank journal whose incoming payment methods were removed. Import the paid order. Expected:
      - The sale order is created and **confirmed**, and the invoice is **posted**.
      - There is one activity **"Salla: payment not registered"** (no incoming payment method).
      - The queue row is **gone**: not failed, no retry, no error.
    - **Variant B** (always possible). Settings > Technical > Automation Rules (staging only; it disappears on the next build anyway): create a rule on **Journal Entry**, trigger **On creation**, filter Journal = INV, action **Execute Code** `raise UserError("staging fake invoice failure")`. Import a new **3 x P1** order with a coupon. Expected:
      - The sale order is created and **confirmed**.
      - There is no invoice, and the order shows **Salla Auto-Invoice Failed**.
      - There is one activity **"Salla: invoice not created"** with the fake message. If the failure escapes that path, the order instead has the note *automatic invoicing failed unexpectedly*: also acceptable, as long as the order stays confirmed. Write down which one happened.
      - The queue row is **gone** and the order is not imported twice.
      - `run_inv()` twice: no new invoice attempt, no second activity.
    - **Delete the automation rule** right after, and restore the Salla Payments Journal.

## S. F8: product upload (v17.0.5.0.0; option/value English v17.0.5.0.2)

**DEMO STORE ONLY (`461067869`).** Nothing in this section touches the real store: the real
run (second backend, `Import Orders` off, spec `claude-docs/14-product-upload.md` §1) comes
only after S.1-S.10 pass and the spike findings are settled, and only through S.11 (backend
created with Import Orders off and checked **before** the app is installed on the real store). The uploader never deletes and never
publishes; every product it creates stays **hidden**. Every handle, SKU and barcode below is
fake (`stg-f8-*` / `STG-F8-*`). Never put marketing copy with claims on hold, ingredient
names, costs, tokens or customer data in the test sheet, the ticket or a chat.

Run the standalone suite first: `python3 salla_connector/tests/test_mapping.py` must end in
`all passed` (F8 tests: `test_read_*`, `test_parse_*`, `test_upload_*`, `test_text_to_html`,
`test_flagged_claims_*`, `test_product_payload_*`, `test_translation_*`,
`test_option_en_writes_lang_ar_translations_only`, `test_option_en_keys_from_spike_run4`,
`test_option_translation_payloads_*`, `test_option_english_names_*`, `test_detail_option_name`,
`test_english_kept`, `test_image_size_*`, `test_norm_label`,
`test_match_variants_*`, `test_variant_payload_*`, `test_images_to_attach`,
`test_detail_helpers`, `test_create_weight_*`). 274 tests on 2026-11-10 (v17.0.5.0.2).

**Rules after the spike of 2026-11-10** (`claude-docs/14-product-upload.md` §7):
- Salla publishes a product **by itself** when a variant gets stock (hidden/out -> `sale`).
  The uploader puts back to hidden only a product it **created** in the run or that was
  **hidden before** the run; any other existing product's status is never written (a change
  Salla made is only reported). The uploader only ever POSTs `hidden`.
- Option / value English names (v17.0.5.0.2, spike run 4 with English **enabled**): written
  ONLY as `PUT /products/options/{id}` and `PUT /products/options/values/{id}` with lang
  **`ar`** and body `{"name": <Salla's current Arabic name>, "translations": {"en":
  {"option_name" | "option_details_name": <EN>}}}`. Never a lang `en` PUT there (it wrote
  over the Arabic), never `values` / price / quantity in that body, never `translations.en.name`
  (Salla ignores it). The first option and the first value of each run are read back in
  Arabic (moved -> restored, error, no more option English in the run) and in English (not
  shown -> *Salla did not keep the English option names; enter them by hand*). Names left
  out (English empty, equal to the Arabic, different English on rows of one value, option or
  value not found / found twice in Salla) get *English option/value names must be entered by
  hand in Salla: <reason>*.
- English product texts are read back with lang `en`; if Salla does not show them, the run
  warns *Salla did not keep the English (check that English is enabled in the store's
  languages)...* once and stops EN writes for the rest of the run.
- Image sizes come from a header parser (PNG / JPEG / WebP), not Pillow: Pillow on odoo.sh
  cannot open WebP. **Convert WebP to JPEG** is refused on a server without Pillow WebP.

**Weight is required on create** (observed on the demo store 2026-11-10: `POST /products`
without `weight` -> 422 `{"message":"alert.invalid_fields","fields":{"weight":[...]}}`). A NEW
handle needs a weight on at least one row; an existing product (update / images) does not.

Odoo shell helpers (paste after the ones at the top of this file; `BID` = the DEMO backend id).
They print ids, statuses and numbers only:
```python
from odoo.addons.salla_connector.models import mapping
b = env["salla.backend"].browse(BID)
assert b.store_id == "461067869", "S runs on the demo backend only"
def sl(pid):        # one Salla product: status, parent sku, images, variants
    d = (b._request("GET", "/products/%s" % pid) or {}).get("data") or {}
    return dict(id=d.get("id"), sku=d.get("sku"), status=mapping.detail_status(d),
                price=mapping.money(d.get("price")), images=len(mapping.detail_images(d)),
                variants=[(v.get("id"), v.get("sku"), mapping.money(v.get("price")),
                           mapping.money(v.get("sale_price")), v.get("stock_quantity"),
                           v.get("weight"), v.get("barcode")) for v in d.get("skus") or []])
def sl_names(pid):  # name as Salla shows it in AR and in EN
    return [((b._request("GET", "/products/%s" % pid, lang=lang) or {}).get("data") or {})
            .get("name") for lang in ("ar", "en")]
def sl_tr(pid):     # the `translations` object (keys + names only), if Salla returns one
    d = (b._request("GET", "/products/%s" % pid, lang="en",
                    params={"with": "translations"}) or {}).get("data") or {}
    tr = d.get("translations")
    return {k: (v.get("name") if isinstance(v, dict) else v) for k, v in tr.items()} \
        if isinstance(tr, dict) else tr
def sl_options(pid):  # option / value names as Salla shows them in Arabic
    d = (b._request("GET", "/products/%s" % pid, lang="ar") or {}).get("data") or {}
    return [(o.get("name"), [v.get("name") for v in o.get("values") or []])
            for o in d.get("options") or []]
def sl_options_en(pid):  # names with lang en + translations.en of each option / value
    d = (b._request("GET", "/products/%s" % pid, lang="ar",
                    params={"with": "translations"}) or {}).get("data") or {}
    e = (b._request("GET", "/products/%s" % pid, lang="en") or {}).get("data") or {}
    en = {str(x.get("id")): x.get("name") for o in e.get("options") or []
          for x in [o] + list(o.get("values") or [])}
    def tr(x, key):
        t = x.get("translations") if isinstance(x.get("translations"), dict) else {}
        return (t.get("en") or {}).get(key) if isinstance(t.get("en"), dict) else None
    return [(o.get("id"), o.get("name"), en.get(str(o.get("id"))), tr(o, "option_name"),
             [(v.get("id"), v.get("name"), en.get(str(v.get("id"))),
               tr(v, "option_details_name")) for v in o.get("values") or []])
            for o in d.get("options") or []]
def sl_spy(w, handles, mode):  # run the wizard, return the Salla calls (method, path, lang)
    B = type(w.backend_id); req = B._request; calls = []
    def spy(self, method, path, *a, **k):
        calls.append((method, path, k.get("lang"))); return req(self, method, path, *a, **k)
    B._request = spy
    try:
        w.handles = handles; w.mode = mode; w.action_run(); env.cr.commit()
    finally:
        B._request = req
    return calls
def opt_calls(calls):  # only the option / value calls
    return [c for c in calls if "/options" in c[1]]
def sl_find(keyword="STG-F8"):   # every Salla product whose SKUs match: duplicates show here
    res = b._request("GET", "/products", params={"keyword": keyword}) or {}
    return [(p.get("id"), mapping.detail_status(p), mapping.product_skus(p))
            for p in res.get("data") or [] if isinstance(p, dict)]
def last_wiz():
    return env["salla.product.upload"].search([], order="id desc", limit=1)
```

### S.1 Build and dependencies
156. Build green; `salla_connector` upgrades to **17.0.5.0.1** with no error or warning about the module in the build log. Redo step 37 (Webhook URL in the Partners Portal for this build, reinstall the app on the demo store, Test Connection green). Expected in the UI:
    - Menu **Sales > Configuration > Salla > Upload Products** (admin and sale manager).
    - Button **Upload Products** on the backend form: it opens the wizard with the backend already set.
    - The wizard shows Master Sheet, Images ZIP, Mode (**Dry Run** by default), Step (**Everything** by default), Only Handles, Blocked Phrases (pre-filled with the 10 SAL-92 phrases, one per line), Convert WebP to JPEG (off), and the buttons **Run**, **Download Template**, Close.
157. **openpyxl is not required.** `__manifest__.py` `external_dependencies` lists only `requests`. In the shell, write down `import importlib.util; importlib.util.find_spec("openpyxl") is not None` (either answer is fine: the reader is stdlib-only, and step 166 must read a real Excel file either way). Also write down Pillow and its WebP support with the same probe the wizard uses: `import PIL; from PIL import Image, WebPImagePlugin; PIL.__version__, "WEBP" in Image.OPEN and "WEBP" in Image.SAVE`. **Do not use `features.check("webp")`**: it only says libwebp is compiled in, and odoo.sh may report True there while `img.save(buf, "WEBP")` fails with `KeyError 'WEBP'` (spike 2026-11-10), because Odoo's `Image.preinit()` skips the WebP plugin; importing `WebPImagePlugin` registers it. If the import itself fails, write down the error: that is WebP False. WebP False: **Convert WebP to JPEG** must be refused (step 169b) and WebP sizes still checked by the header parser (step 169a). WebP True: write it down; 169b then expects a normal run (and the spike's `6 Pillow WebP support on this server` must say True too).

### S.2 Catalog-only guard (`Import Orders`)
158. Backend form: **Import Orders** is visible, **on** by default, with its help text (*Off = catalog-only backend: ...*). As a **sale manager who is not admin**: the field is read-only, and in the shell `b.with_user(<that user id>).write({"import_orders": False})` raises **AccessError**. Nothing changes.
159. As admin, turn **Import Orders off** on the demo backend. Expected:
    - The header buttons **Import Orders Now** and **Process Order Queue** disappear.
    - In the shell, `b.action_import_orders()` and `b.action_process_order_queue()` each raise a **UserError** that says the backend is catalog-only.
    - Backstop: `b._sync_order("<id of any demo order>")` returns `("error", None, "catalog-only backend (Import Orders off): not imported")` and creates no sale order.
160. **Webhook ignored.** Write down `env["salla.order.queue"].search_count([])` first. Then (zsh, helpers from the top of this file):
    ```sh
    B='{"event":"order.created","merchant":'"$STORE_ID"',"data":{"id":1000000001}}'; post "$B" "$(sig "$B")"
    B='{"event":"order.status.updated","merchant":'"$STORE_ID"',"data":{"order":{"id":1000000001},"status":"in_progress"}}'; post "$B" "$(sig "$B")"
    ```
    Expected: both answer **200 `ok:ignored:catalog_only`** in well under 1 s; the queue count is unchanged; the log says *ignored (Import Orders off)*. An unsigned body still gets **401** (step 11 command).
161. **Crons skip it.** Create a demo order (Salla dashboard, or `tests/staging_api_orders.py`), write down its id as `ORDER_ID`, then:
    ```python
    env.ref("salla_connector.ir_cron_salla_order_queue").method_direct_trigger()
    env.ref("salla_connector.ir_cron_salla_reconcile_orders").method_direct_trigger()
    env.cr.commit()
    env["sale.order"].search_count([("salla_order_id", "=", str(ORDER_ID))])   # -> 0
    ```
    **Refresh Tokens still runs for it:** `print(fp(current()[0]))`, then as admin set **Token Expires At** to about 1 hour from now, `env.ref("salla_connector.ir_cron_salla_refresh_tokens").method_direct_trigger()`, `print(fp(current()[0]))`: the fingerprint **changed** and Test Connection is green.
162. **Authorize still works.** With Import Orders still off, reinstall the app on the demo store. The webhook answers `ok:authorized` (build log), the token fingerprints change, Test Connection is green.
163. Turn **Import Orders back on** (the demo backend is used with it on for the rest of S; the uploader does not care). Optional: run the reconcile cron and check that the order of step 161 now imports once (write down the result).

### S.3 Spike: settle the open API questions (one throwaway hidden product)
164. Paste the whole of `tests/staging_products_spike.py` into the shell (round 3: `GET /languages` runs first; the option/value EN experiment is back, on the spike product only and only when English is active; steps 9-11 and an embedded 64x64 WebP, no Pillow needed). **Before pasting, check Settings > Languages in the demo dashboard: English must be ON** (it was OFF on the first run, 2026-11-10, so those English results are not meaningful). Expected: `store verified: demo 461067869`, `English is present and active in this store.`, then the **F8 SPIKE FINDINGS** block. If it prints **ENGLISH NOT ACTIVE IN THIS STORE: English results below are not meaningful**, the step 4/5/10/11 results do not count: enable English and paste again (if the dashboard shows English ON but the reason says `unknown`, set `FORCE_EN_EXPERIMENTS = True` at the top and paste again). If it prints `STOP`, nothing was sent: fix what it says (wrong backend, module not upgraded) and paste again. Copy only the findings block into the ticket. Check in the Salla dashboard that the spike product (`SPIKE-F8-...`) is **hidden**.
165. Act on the findings **before** step 168 (tell the lead/developer; do not work around them by hand):

    | Finding | If it says | Then |
    |---|---|---|
    | `1 create needs weight` | yes (422 `fields.weight` without it) | Expected (demo 2026-11-10): the uploader sends the weight on create and refuses a new handle without one (168, 170, 171a) |
    | `1 parent sku kept` | False | Lookup by parent SKU cannot work: developer |
    | `1 variants created` | not 2, or prices not 10 | Options create shape is wrong: developer, **STOP S** |
    | `2 variant PUT with weight_type` | 422 | Developer drops `weight_type` from `variant_payload` |
    | `2 decimal weight 0.25 kept` / `2 barcode leading zeros kept` | False | Developer + Ops (barcodes as text) |
    | `3 GET /products/sku/<variant sku>` | 404 | Fine only if `3 ...keyword...` finds ours (fallback) |
    | `2 status after variant quantity` | `sale` | Expected (2026-11-10): Salla publishes by itself. The uploader puts it back (171b) |
    | `2 POST status hidden` / `2 status after the POST (GET)` | an HTTP error / not `hidden` | The uploader relies on it right after the stock writes: developer, **STOP S** |
    | `4 AR name survived` | False | Expected handling: the uploader restores AR and says *English must be entered by hand* (check in 170) |
    | `4 AR description survived` | False | The developer's Arabic re-write after a lost EN (`translation_payload(product, "ar")`) is needed: keep it; tell the developer |
    | `4 EN name shown with lang en` | False with `0 English active` = `yes` | English enabled and still not shown: developer (the EN write is wrong, not the store). If `0 English active` is not `yes`, the result does not count |
    | `4 variants kept ...` / `4 price kept ...` | False | A plain PUT wipes data: developer, **STOP S** |
    | `4 status after the EN PUT`, `6 status after the image uploads`, `11 status after ...` | not `hidden` | That write also publishes: the wizard only re-checks after the stock writes and at the end; tell the developer which write |
    | `0 English active (GET /languages)` | `NO` / `unknown` | English results of this run are not meaningful (banner printed): enable English in the store and paste again |
    | `5 option/value EN writes` | SKIPPED: English not active | Expected only when `0 English active` is not `yes`; otherwise a bug in the spike: tell the tester |
    | `5 option/value EN writes` | SKIPPED: spike option not found as created | The spike product's option is not the one step 1 created (wrong option name or value, or no option read back): no EN write was sent. Tell the developer (paste the line: it shows the option and value read) |
    | `5 values kept` / `5 variants kept` | False (either one) | **STOP**: an option/value PUT changed the option's values or the product's variants (the line shows the value ids, or the variant count, before the EN PUTs, after them and after the Arabic restore; `5 values (id, name)` has the names). **Never re-enable option/value EN writes in the uploader**, whatever `5 option EN shown` says. Tell the developer and check the spike product's variants in the dashboard |
    | `5 option AR survived` / `5 value AR survived` | True, and `5 option EN shown` / `5 value EN shown` True | Unexpected (run 4, 2026-11-10: lang `en` overwrote the Arabic): write it down; the uploader keeps the 5b method anyway |
    | `5b option ...` / `5b value ...` | `AR kept` True, `EN shown` True, `values kept` / `variants kept` True | Expected (run 4, 2026-11-10, English enabled): the method the uploader uses since v17.0.5.0.2 (lang ar + `translations.en.option_name` / `option_details_name`). Any of them False: **STOP S.5 option English** (do not Apply an options product), tell the developer; until fixed, option English is entered by hand |
    | `5 option AR survived` / `5 value AR survived` | False | Expected (run 4): lang `en` overwrites the Arabic; the uploader never sends that. `5 AR restored` must be True for both |
    | `5 option EN shown` / `5 value EN shown` | False, AR survived True | Salla ignored the lang `en` write: no change for the uploader (it uses the 5b body) |
    | `5 AR restore FAILED` | any | Put the Arabic option/value names back by hand on the spike product (names in the line) and tell the developer |
    | `5 ?with=translations (lang ...)` | translations shown per option/value | Write it down: tells how Salla stores option translations |
    | `6 Pillow WebP support on this server` | False | Convert WebP is refused (169b). Same probe as step 157 and the wizard: it must agree with step 157; if not, tell the developer |
    | `6 embedded webp header (mapping.image_size)` | not `(64, 64), 110 bytes` | The module on the build is not the tested one: developer |
    | `6 attach webp (embedded 64x64)` | ok | Salla accepts WebP: upload WebP as is, Convert WebP stays off |
    | `6 attach webp (embedded 64x64)` | an HTTP error, and `6 control: 64x64 PNG ...` ok | Salla refuses WebP: convert the images to JPEG **before zipping** (odoo.sh cannot convert) |
    | `6 attach webp (embedded 64x64)` and `6 control ...` both HTTP errors | | Salla refuses the 64 px size, not the format: WebP still unknown; tell the developer |
    | `8 status after images + quantity` | not `hidden` | Images (or a later write) publish too: the wizard's final check puts it back; write it down |
    | `8 POST status hidden` | an HTTP error | Developer (the run relies on it), **STOP S** |
    | `9 GET /languages` | `en` listed and enabled | English should be kept: if `4 EN name shown` is still False, tell the developer (the EN write is wrong, not the store) |
    | `9 GET /languages` | only `ar`, or `en` disabled | Explains the lost English: enable English in the store (demo: Settings > Languages) and paste the spike again; for the real store, decide with Marketing |
    | `9 GET /languages` | HTTP 401/403 | Scope missing (not in the granted scopes): check Settings > Languages by hand and write it down |
    | `10 GET ?with=translations (lang en)` | `en` name = the step 4 EN name | Salla stores the EN PUT but `lang en` does not show it: developer (read back via `?with=translations` in `english_kept`) |
    | `10 ...` | no `translations` key / no `en` | Salla dropped the EN PUT |
    | `11 PUT {translations: ...}` ok and `11 EN name via ...` True | | A `translations` body works: developer may switch the EN write to it |
    | `11 AR name unchanged` | False | Restored by the spike (`11 AR restored` must be True); the `translations` body is not usable |

    Any `... crashed` line: send it to the developer as is.

### S.4 Dry run on the demo store
166. **Download Template.** It downloads an .xlsx with a **Products** sheet (the header row + 2 fake example rows: one simple product, one Size x Scent product) and an **Instructions** sheet. It opens in Excel/Numbers with the Arabic readable.
167. **Test files** (make them from the template, all values fake):
    - `stg-f8-simple`: one row, SKU `STG-F8-001`, price 25, **sale_price 20**, stock 12, weight 0.5 kg, barcode `000123456789`, `image_1 = stg-f8-1.jpg`, AR + EN name/description.
    - `stg-f8-options`: Size (500 مل / 1 لتر) x Scent (لافندر / ليمون), 4 rows `STG-F8-011` ... `STG-F8-014`, prices 30 / 32 / 45 / 47 (different on purpose), stock 4 / 0 / 12 / 3, **weight 0.5 kg on all 4 rows** (Salla requires a weight to create), one variant with sale_price 28 (< its price 30), `image_1..image_4 = stg-f8-1.jpg, stg-f8-2.webp, stg-f8-3.png, stg-f8-4.jpg`, `image_alt` set, `category_ids` = one demo category id (from the dashboard URL).
    - `stg-f8-broken`: one row with price `abc`.
    - `stg-f8-noweight`: one simple row, SKU `STG-F8-090`, price 25, stock 3, AR name/description, **weight and weight_unit empty**.
    - Images ZIP (inside a folder, as a Mac zips it): `stg-f8-1.jpg` (1200 x 1200, < 300 KB), `stg-f8-2.webp` (**1200 x 1200 lossy WebP**, < 300 KB), `stg-f8-3.png` (800 x 600). **`stg-f8-4.jpg` is left out on purpose.**
168. **Dry Run, Step Everything** (as admin). Before: `sl_find()` -> write down (expected empty). Expected result:
    - `stg-f8-simple` and `stg-f8-options`: *would create it hidden with 1 / 4 variant(s)*, *would attach image(s) ...*.
    - `stg-f8-options`: warning *images stop: stg-f8-4.jpg not in the ZIP* (images 1-3 only) and a warning that `stg-f8-3.png` is not square.
    - `stg-f8-options`: *would set English option/value names: 6* (2 options + 4 values, counted from the sheet: the product is new) and **no** *English option/value names must be entered by hand* warning. `stg-f8-simple` (no options) has neither line.
    - `stg-f8-2.webp`: **no** message at all (1200 x 1200 read from its header even though Pillow on odoo.sh cannot open WebP). Never *not a readable image* and never *size not checked*.
    - `stg-f8-broken`: error starting with `Row N:` (N = its sheet row) about the price; it is skipped.
    - `stg-f8-noweight`: action **would not create**, error *Salla requires a weight to create a product: fill weight (and weight_unit) in the sheet*; no "would create" / "would attach" line for it.
    - Warnings *SKU not found in Odoo* for the `STG-F8-*` SKUs (they are fake): expected.
    - **Download the result sheet:** the input rows plus `salla_product_id`, `salla_variant_id`, `upload_action`, `upload_status`, `upload_message`.
    - **No write in dry run:** `sl_find()` is still the same as before, and nothing new in the Salla dashboard.
169. Save the same sheet as **CSV UTF-8** and dry-run it: same result as 168. Also dry-run the .xlsx with **Only Handles** = `stg-f8-simple`: only that handle is listed.
169a. **Image checks without Pillow WebP (Dry Run only, never Apply).** Separate sheet, one simple handle `stg-f8-imgcheck` (SKU `STG-F8-095`, price 25, stock 1, weight 0.5 kg), `image_1..image_5` = `ic-1.webp, ic-2.webp, ic-3.webp, ic-4.webp, ic-5.jpg`. Make the files on a laptop (Python 3, no Pillow needed), zip them, and dry-run:
    ```python
    import base64, zipfile
    full = base64.b64decode("UklGRmYAAABXRUJQVlA4IFoAAADQBACdASpAAEAAPrFInUmnJCKhMegA4BYJQBpGIAaADeAA"
                            "P7BD0SV+jyHoTKG4AAD+4Ll+q8QZmA/ys1sxthqyRlG2r//9zfv7fOA6l6N7S38Vw7NxDQAAAAA=")
    lossless = base64.b64decode("UklGRh4AAABXRUJQVlA4TBEAAAAvJIAFAAdQ5CrUo/+BiOh/AAA=")   # 37 x 23
    unknown = b"RIFF\x14\x00\x00\x00WEBPALPH" + b"\x08\x00\x00\x00" + b"\x00" * 8  # no size chunk
    with zipfile.ZipFile("stg-f8-imgcheck.zip", "w") as z:
        z.writestr("ic/ic-1.webp", full); z.writestr("ic/ic-2.webp", lossless)
        z.writestr("ic/ic-3.webp", unknown); z.writestr("ic/ic-4.webp", b"not an image at all")
        z.writestr("ic/ic-5.jpg", b"not an image at all")
    ```
    Expected for `stg-f8-imgcheck` (Pillow without WebP, step 157):
    - `ic-1.webp`: *is 64 px, not 1200 px* (size read from the header).
    - `ic-2.webp`: *is not square (37x23)*.
    - `ic-3.webp`: *size not checked (format not readable on this server)*, **not** *not a readable image*.
    - `ic-4.webp` (no RIFF header): *not a readable image* (Pillow cannot open it). Write down the exact text.
    - `ic-5.jpg` (not a JPEG): *not a readable image*.
    - No traceback, and the other handles of the run are unaffected. Do not Apply this sheet.
169b. **Convert WebP refused on odoo.sh.** Same S.4 sheet + ZIP, turn **Convert WebP to JPEG** on, Mode **Dry Run**, Run. Expected (Pillow without WebP, step 157): a UserError *This server cannot convert WebP (Pillow without WebP): convert the images to JPEG before zipping, or leave the toggle off.* before anything is read; no result sheet; `sl_find()` unchanged. Same with Mode Apply (still refused, nothing created). The field tooltip says it needs Pillow with WebP support. Turn the toggle **off** again for the rest of S. (If step 157 said WebP True: the dry run runs normally; write it down.)

### S.5 Apply with images
170. **Mode Apply, Step Everything** (as admin), same files. Expected:
    - `stg-f8-simple` and `stg-f8-options` **created**; `stg-f8-broken` skipped with its error.
    - `stg-f8-noweight`: action **not created**, the same *Salla requires a weight...* error, **no POST**: `sl_find("STG-F8-090")` is empty and nothing new in the dashboard (Salla would have answered 422 anyway; the error must be ours, not a `422`/`invalid_fields` text). The run continues with the other handles.
    - The result sheet has `salla_product_id` filled for the two.
    - `sl(<id>)` for each: status **hidden**; parent SKU `STG-F8-001` (simple) and `STG-F8-OPTIONS` (options product: the handle in upper case), if the spike said the parent sku is kept; every variant with its own SKU, price (30 / 32 / 45 / 47, not all the lowest), sale price where set, stock 4 / 0 / 12 / 3 (the 0 is a 0, not empty), weight 0.5, barcode `000123456789` with its zeros.
    - In the dashboard: AR name and description (paragraphs and bullet lists rendered, no raw `<p>` text), options Size x Scent with the 4 combinations, images 1-3 in sheet order with image 1 as the main one and the alt text, the category set.
    - `sl_names(<id>)`: the AR name is **always** intact. The EN name with lang en is one of:
        - shown -> no English warning (English enabled and kept);
        - not shown (the demo on 2026-11-10) -> the **first** product with EN texts has *Salla did not keep the English (check that English is enabled in the store's languages); enter it by hand (no more English writes in this run)*, and every later product with EN texts has *English must be entered by hand in Salla* (no EN PUT for them). Write down `sl_tr(<id>)` for both products.
    - In the dashboard the Arabic description of both products is intact (not the English one): the uploader re-writes the Arabic texts after a lost English. If instead the result has *the Arabic texts could not be written again after the English write ...*: an error, tell the developer.
    - `stg-f8-options` option/value English (needs English **ON** in the demo, as in step 164; v17.0.5.0.2):
        - the result has *English option/value names set: 6* and **no** *English option/value names must be entered by hand*, *Salla wrote the English over the Arabic*, *Salla did not keep the English option names* or *not verified* line. If any of those appears: copy it, **STOP S.5 option English**, developer.
        - `sl_options(<id>)` still shows the **Arabic** names, unchanged: `الحجم` with 500 مل / 1 لتر, `الرائحة` with لافندر / ليمون (never `Size`, `500 ml`...).
        - `sl_options_en(<id>)`: for each option / value, the lang `en` name and the `translations.en` name are the sheet English: `Size` (500 ml / 1 L), `Scent` (Lavender / Lemon). Write down the ids.
        - Values and variants kept: the option value ids in `sl_options_en(<id>)` are 2 + 2 and `sl(<id>)["variants"]` still has the 4 variant ids, SKUs, prices 30 / 32 / 45 / 47 and stock 4 / 0 / 12 / 3.
        - **Salla dashboard, English view** (switch the dashboard language to English, or open the product and pick the English tab of the options): options read `Size` / `Scent` with values `500 ml`, `1 L`, `Lavender`, `Lemon`. Switch back to Arabic: `الحجم` / `الرائحة` with the Arabic values. Write down what each view shows.
        - If English is **OFF** in the demo: the result says *English option/value names must be entered by hand in Salla: English writes stopped earlier in this run* (no options PUT). That is not the expected run: enable English and repeat 170 on a new handle.
    - `stg-f8-2.webp`: attached as image 2 if the spike said Salla accepts WebP (`6 attach webp (embedded 64x64)` ok). If the spike said Salla refuses WebP: the result reports image 2 failed and images stop there (3 is not attached); convert it to JPEG on a laptop, re-zip, and Step Images (175) attaches 2 and 3.
    - `sl_find()` lists exactly **two** products.
    - Build log: one summary line per product (handle, Salla id, action, counts). **No request bodies, no marketing copy, no token.**
171. Status after the run: both **hidden** (`sl(<id>)["status"]`), although both got stock (Salla flipped them to `sale` after the variant PUT; the uploader put them back right after the stock writes and again at the end). The uploader never publishes. If either is not hidden: **STOP S**, developer.
171a. **Options product whose first row has no weight** (own SKU prefix so the `STG-F8` counts below do not change). Separate sheet, handle `stg-w8-options`, option Size (500 مل / 1 لتر), 2 rows `STG-W8-001` (500 مل, price 30, stock 4, **weight empty**) and `STG-W8-002` (1 لتر, price 45, stock 12, **weight 600, weight_unit g**). Dry Run: *would create it hidden with 2 variant(s)* (no weight error: one row has a weight). Apply: **created**, no 422; `sl(<id>)`: status hidden, variant `STG-W8-002` weight 600 g (or 0.6 if Salla shows kg: write down which). Write down the weight Salla shows for `STG-W8-001` (no row weight: the create sent 600 g at product level; this says whether Salla copies it to variants without their own). Leave it hidden.

### S.6 Re-run idempotence
172. **Apply again**, same files (also once with the result sheet of 170 as input, `salla_product_id` filled). Expected:
    - Both products *update #id*; `sl_find()` still lists exactly **two** (no duplicates).
    - Images count unchanged (3): nothing re-attached. Variant values unchanged; no variant PUT reported when nothing changed.
    - **Option English already set:** Dry Run first: `stg-f8-options` says *would set English option/value names: 0 (6 already set)*. Then Apply through the spy (shell, wizard `w` = `last_wiz()` with the same file): `calls = sl_spy(w, "stg-f8-options", "apply"); print(opt_calls(calls))` -> `[]` (**no** `/products/options` PUT) and no *English option/value names set* line in the result. `sl_options(<id>)` and `sl_options_en(<id>)` unchanged from 170.
172b. **Option English: one change, one PUT, always lang ar** (v17.0.5.0.2). In the sheet set `option2_value_en` of **every** ليمون row (`STG-F8-012`, `STG-F8-014`) to `Lime`.
    - **Dry Run:** *would set English option/value names: 1 (5 already set)*.
    - **Apply via the spy:** `calls = sl_spy(w, "stg-f8-options", "apply"); print(opt_calls(calls))` -> exactly **one** call `("PUT", "/products/options/values/<ليمون value id>", "ar")` (plus no other `/options` call; the lang is `ar`, **never** `en`, never a DELETE). Result: *English option/value names set: 1*.
    - `sl_options(<id>)`: Arabic unchanged (`ليمون` still `ليمون`). `sl_options_en(<id>)`: that value shows `Lime` with lang en and in `translations.en`; the other 5 unchanged. `sl(<id>)["variants"]`: same 4 ids, prices and stock as before. Dashboard English view shows `Lime`.
    - Put `Lemon` back on both rows and Apply: one PUT again, `Lemon` shown.
172c. **Option English left out with a reason, nothing sent** (Dry Run and Apply via the spy, `stg-f8-options` only; restore the sheet after each case):
    - Different English on rows of one value: row of `STG-F8-013` `option2_value_en` = `Lavande` (row of `STG-F8-011` stays `Lavender`). Expected: warning *English option/value names must be entered by hand in Salla: option2 value لافندر: different English on rows N, M (Lavender, Lavande)* (N, M = the sheet rows); `opt_calls(calls)` has no PUT for the لافندر value id; Salla still shows `Lavender` for it.
    - Empty English: clear `option1_name_en` (first row). Expected: warning *... option1 الحجم: option1_name_en is empty*; no PUT for the الحجم option id; Salla keeps `Size` (an empty cell never clears Salla's English).
    - English equal to the Arabic: `option2_name_en` = `الرائحة`. Expected: warning *... option2 الرائحة: the English name is the same as the Arabic*; no PUT for that option.
    - In all three: the Arabic names in `sl_options(<id>)` are unchanged and no *Salla wrote the English over the Arabic* line.
172a. **Existing product: no weight needed to update.** Clear `weight` and `weight_unit` of `stg-f8-simple` in the sheet, change its `stock` to 7, Apply. Expected: *update #id* (no *Salla requires a weight* error), stock 7 in `sl(<id>)`, its weight in Salla **unchanged** (0.5: an empty cell never clears it). Restore the sheet (weight 0.5 kg, stock 12) and Apply again.
171b. **Existing hidden product + new stock stays hidden.** Both products are hidden (171). In the sheet change the stock of `STG-F8-012` from 0 to 5 (the variant with no stock) and of `STG-F8-001` from 12 to 9 (simple product: its stock goes in the product PUT). Before, write down `sl(<options id>)["status"]` and `sl(<simple id>)["status"]` (both `hidden`).
    - **Dry Run:** both say *hidden in Salla: kept hidden (Salla publishes a product by itself when a variant gets stock; Apply puts it back to hidden)*; no write (statuses still hidden).
    - **Apply:** both *update #id*; stock 5 and 9 in `sl()`; status **hidden** at the end. For each product Salla flipped, the result has the warning *Salla changed the status to sale after the stock update: put back to hidden* (**once** per product, not twice). Write down for which product Salla flipped (none / options / simple / both). No *Salla changed the status from ... (not touched ...)* line for these two.
    - Restore the sheet (0 and 12) and Apply again: same, hidden at the end.
171c. **Interrupted run: the marker hides it on the next run (v17.0.5.0.1).** Simulates "both hide calls failed" (the same state a worker killed mid-product leaves). Shell, wizard of 171b, sheet with the stock of `STG-F8-012` changed again (5 -> 6), both products hidden before:
    ```python
    Cls = type(w); keep, ens = Cls._keep_hidden, Cls._ensure_hidden
    Cls._keep_hidden = lambda *a, **k: None; Cls._ensure_hidden = lambda *a, **k: False
    w.handles = "stg-f8-options"; w.mode = "apply"; w.action_run(); env.cr.commit()
    Cls._keep_hidden, Cls._ensure_hidden = keep, ens
    print(sl(<options id>)["status"]); print(env["salla.product.hide"].search([]).mapped("ref"))
    ```
    Expected: the status is whatever Salla made it (write it down: `sale` expected) and the marker list has `product:<options id>`. Also visible in Sales -> Configuration -> Salla -> **Products Kept Hidden** (admin only). Then, in the UI:
    - **Dry Run** (Only Handles `stg-f8-options`): warning *a previous upload did not finish on this product (Salla status now: sale): Apply puts it back to hidden*; no write.
    - **Apply**: warning *a previous upload did not finish on this product (Salla status at the start of this run: sale): kept hidden*; status **hidden** at the end; the marker row is **gone**.
    - Apply once more: no such warning, no marker left after it.
    If the status stays published or the marker stays after a hidden status: **STOP S**, developer.
171c2. **Stale marker (older than 24 h) on a published product is only reported (v17.0.5.0.1).** Needs a product that is **published** (`sale`) and has a marker. Repeat the 171c shell block (stock of `STG-F8-012` changed again, 6 -> 7) to leave `stg-f8-options` with a marker and the status Salla gave it. If `sl(<options id>)["status"]` is not `sale` afterwards, skip this step and write down the status (never publish it through the API). Then age the marker past 24 h in the shell:
    ```python
    env.cr.execute("UPDATE salla_product_hide SET write_date = (now() at time zone 'UTC') "
                   "- interval '25 hours' WHERE ref = %s", ("product:<options id>",))
    print(env.cr.rowcount); env.cr.commit()   # 1 expected
    print(env["salla.product.hide"].search([("ref", "=", "product:<options id>")]).write_date)
    ```
    Change the stock of `STG-F8-012` again (7 -> 8) so Apply has something to write.
    - **Dry Run** (Only Handles `stg-f8-options`): warning *a previous upload left this product marked "must stay hidden" more than 24 h ago; Salla status sale: Apply does not touch it (it may have been published on purpose) ...*; **not** *a previous upload did not finish ... Apply puts it back to hidden*; no write.
    - **Apply**, with every Salla call recorded (shell, wizard `w`):
      ```python
      B = type(w.backend_id); req = B._request; calls = []
      def spy(self, method, path, *a, **k):
          calls.append((method, path)); return req(self, method, path, *a, **k)
      B._request = spy
      try:
          w.handles = "stg-f8-options"; w.mode = "apply"; w.action_run(); env.cr.commit()
      finally:
          B._request = req
      print([c for c in calls if "/status" in c[1]])   # [] expected: no status call
      print(sl(<options id>)["status"]); print(env["salla.product.hide"].search([]).mapped("ref"))
      ```
      Expected: warning *... more than 24 h ago; Salla status sale: not touched ...*; **no** `/status` call in the list (the stock PUT is there); the status is still `sale`; the marker `product:<options id>` is **still there** (a stale marker is never cleared by a run). If a `/status` call was sent or the status became `hidden`: **STOP S**, developer.
    - Clean up: hide `stg-f8-options` by hand in the Salla dashboard, delete its row in Sales -> Configuration -> Salla -> **Products Kept Hidden**, and check `sl(<options id>)["status"]` is `hidden` and the marker list is empty (step 175 expects it hidden). The sheet can keep stock 8 for `STG-F8-012` (no later step depends on it).
171d. **Dry run warns before stock publishes a product that is not hidden.** In the dashboard set `stg-f8-simple` to out of stock / unpublished so `sl(<simple id>)["status"]` is neither `hidden` nor `sale` (write down the value, `out` expected; if the dashboard cannot reach such a status, skip and say so). Change its stock in the sheet, **Dry Run**: warning *Salla status out: writing the stock makes Salla publish it (sale) by itself; hide it in Salla first if it must stay hidden*. Hide it by hand again; restore the sheet.
171e. **EN text without its AR column is not sent.** On `stg-f8-simple` set `subtitle_en` = `Fake subtitle` with `subtitle_ar` empty. **Dry Run** and **Apply**: warning *English texts not sent for subtitle_en: their Arabic column is empty ...*; the Arabic subtitle in Salla (empty, or one typed by hand) is unchanged. Clear `subtitle_en`.
173. **Published product untouched.** In the Salla dashboard, publish `stg-f8-simple` by hand (demo only). Change its `price` to 27 in the sheet and Apply. Expected: price 27 in Salla, status still **published** (the uploader never writes the status of an existing product that was not hidden). No *put back to hidden* warning. Keep it published for 173a.
173a. **Published product whose status Salla changes is only reported.** Still published: set the stock of `STG-F8-001` to **0** in the sheet and Apply; write down the status (`out` expected) and whether this run already says *Salla changed the status from sale to out ... (not touched ...)*. Then stock **5**, Apply. Expected: status in Salla is what Salla made it (`sale` expected), **never** put back to hidden, and the result says *Salla changed the status from out to sale during the upload (not touched: the product was not hidden before)* (with the statuses Salla really showed). If Salla did not change it: no such line. Restore the sheet (stock 12, price 25), Apply, and **hide it again by hand** in the dashboard.
174. **New option value is never created by the uploader.** Add a 5th row to `stg-f8-options` with a new scent (e.g. نعناع / Mint, SKU `STG-F8-015`) and Apply. Expected: error *add the option value in Salla by hand; the uploader never edits options* for that row, the 4 existing variants untouched, no new option value in Salla. Remove the row.

### S.7 Images only
175. Add `stg-f8-4.jpg` to the ZIP. Change `name_en` of `stg-f8-options` in the sheet, and add a new handle `stg-f8-new` (one simple row). **Step Images only, Apply.** Expected:
    - `stg-f8-options`: exactly one image attached (sort 4); `sl(<id>)["images"]` = 4. The EN name in Salla is **not** changed (no product PUT in this step).
    - `stg-f8-new`: error *not in Salla yet: run step Everything first*; nothing created (`sl_find()` still two).
    - Run it again: nothing attached (4 already there).
    - Status of `stg-f8-options` after the images step: **hidden** (it was hidden before). If Salla flipped it on the image upload, the result has *Salla changed the status to sale after the image upload: put back to hidden*; write down whether it appeared. The second run (nothing attached) makes **no** status call for it.

### S.8 Claims on hold block apply
176. Put `Baby-Safe formula` in `description_en` of `stg-f8-simple` and `It won’t scratch` (curly apostrophe, as pasted from Word/Notion) in its `subtitle_en`. **Dry Run:** warnings naming the phrases found. **Apply:** error for `stg-f8-simple`, product skipped: its EN description and subtitle in Salla are unchanged (`sl_names()` and the dashboard). Remove the phrases afterwards. Also check that clearing **Blocked Phrases** to a blank line does not flag every product.

### S.9 Time budget
177. Run a Dry Run once in the UI (so the wizard record exists), then in the shell:
    ```python
    from odoo.addons.salla_connector.wizard import salla_product_upload as W
    w = last_wiz(); w = w.with_user(w.create_uid)    # wizard records are owner-only
    W.TIME_BUDGET = 0; W.TIME_BUDGET_MIN = 0   # both: on odoo.sh limit_time_real is set, so the 30 s floor would apply
    w.handles = False; w.mode = "dry_run"; w.action_run(); print(w.result_html)
    W.TIME_BUDGET = 240; W.TIME_BUDGET_MIN = 30
    ```
    Expected: *N product(s) left (time budget): run again, re-runs are idempotent.*, each product marked *time budget reached: run again*, no API write. Restore 240 / 30 (or exit the shell).
177a. **Run continues with the remaining handles.** Stop after the **first** product (not at 0, where "remaining" = "all" and proves nothing). Same shell, same wizard, the S.4 sheet (at least 2 valid handles, e.g. `stg-f8-simple`, `stg-f8-options` in that sheet order):
    ```python
    import time
    Cls = type(w); orig = Cls._run_product
    def slow(self, product, res, state):        # each product now takes > 1 s
        orig(self, product, res, state); time.sleep(1.5)
    Cls._run_product = slow
    W.TIME_BUDGET = 1; W.TIME_BUDGET_MIN = 1
    w.handles = False; w.mode = "dry_run"; w.action_run()
    print(w.handles); print(w.result_html)
    ```
    Expected:
    - The first handle of the sheet ran (its normal dry-run result); every later one is *time budget reached: run again* and the summary says *N product(s) left*.
    - **`w.handles` (Only Handles) lists exactly the handles left**, comma-separated, in sheet order, and **not** the one that already ran. A handle with a row error (`stg-f8-broken`) is listed only if the run stopped before it.
    - Again with a filter: `w.handles = "stg-f8-simple,stg-f8-options"` (2 handles), run: Only Handles becomes `stg-f8-options` only (never a handle outside the original filter).
    - Restore: `Cls._run_product = orig; W.TIME_BUDGET = 240; W.TIME_BUDGET_MIN = 30` (or exit the shell; the patch lives only in this shell process). Then `w.action_run()` with the remaining handles still in Only Handles: it runs **only** those, none is marked *time budget*, no *left* line. Same in the UI: open the wizard result, check Only Handles shows the remaining handles, press **Run**.
    - When a run finishes without stopping, Only Handles is left as it was (a run with it empty stays empty).

### S.10 Access
178. As a **sale manager who is not admin**: the menu and the backend button are visible; **Dry Run** works; **Apply** raises *Only an administrator can apply a product upload to Salla; run a Dry Run instead.* Same through the shell, with a wizard owned by that user (wizard records are owner-only): `src = last_wiz(); w = env["salla.product.upload"].with_user(<uid>).create({"backend_id": BID, "mode": "apply", "file": src.file, "file_name": src.file_name}); w.action_run()` -> **AccessError** with that message, and `sl_find()` unchanged.
179. As a **salesman** (not manager): no menu, no button. Shell: `env["salla.product.upload"].with_user(<uid>).create({"backend_id": BID})` -> **AccessError** (no ACL row).
180. Clean-up and record:
    - The spike product and the `stg-f8-*` / `stg-w8-*` products stay **hidden** on the demo store. Delete them by hand in the Salla dashboard if wanted (the uploader and the spike never delete).
    - Ticket: Salla product ids, statuses, HTTP codes, the spike findings block, which steps passed / failed / were not run. No tokens, no URLs with tokens, no customer data.

### S.11 Real store: catalog-only backend BEFORE the app is installed there
Only after S.1-S.10 passed and the spike findings are settled. **Order matters.** A backend for
the real store with **Import Orders on** makes the "Salla: Reconcile Orders" cron (every 30 min)
import the last `DEFAULT_LOOKBACK_DAYS` (3) days of the real store's orders into staging, the
orders **Tecfy already created in production**, and queues every real order webhook. The Store ID
must be on the backend before the authorize arrives (routing is by exact Store ID). Never tick
Import Orders on this backend, not even for a moment: turning it on later imports everything
since Orders Synced Until.

181. Write down: `env["sale.order"].search_count([("salla_order_id", "!=", False)])` and `env["salla.order.queue"].search_count([])`. The demo backend stays as it is (`store_id` `461067869`).
182. **Create the real-store backend, Import Orders off, then save.** As **admin**: Sales > Configuration > Salla > New. Name e.g. `Salla REAL store (catalog only)`, company **KSA (id 1)**, warehouse **WH**, Website **empty**, **Store ID** = the real store's id, typed by hand from the real store's Salla dashboard. **Untick Import Orders before the first Save.** Save. Do not touch any token field.
183. **Check it reads off** before going further. Reload the page (F5): the **Import Orders** box is **unticked**, and the header buttons **Import Orders Now** / **Process Order Queue** are **not shown**. In the shell (`REAL_STORE_ID` = the id typed in 182):
    ```python
    r = env["salla.backend"].search([("store_id", "=", REAL_STORE_ID)]); r.ensure_one()
    r.import_orders, r.store_id, r.access_token and "SET" or "EMPTY"   # -> False, the real id, "EMPTY"
    env["salla.backend"].search_count([("store_id", "=", "461067869")])  # -> 1 (demo untouched)
    ```
    If `import_orders` is **True**, the Store ID is empty or wrong, or `ensure_one()` fails: **STOP, do not install the app on the real store**; fix it and repeat 183.
184. **Only now** install the app on the real store (Mafer, real store's Salla dashboard; on a new build, update the Webhook URL in the Partners Portal first). Expected: the build log shows `ok:authorized` for this backend; **Test Connection** on the real backend is green; Test Connection on the demo backend is still green.
185. **Nothing imported.** Right after 184 and again ~30 min later (one cron cycle):
    ```python
    env.ref("salla_connector.ir_cron_salla_reconcile_orders").method_direct_trigger()
    env.ref("salla_connector.ir_cron_salla_order_queue").method_direct_trigger()
    env.cr.commit()
    env["salla.order.queue"].search_count([("backend_id", "=", r.id)])   # -> 0
    env["sale.order"].search_count([("salla_backend_id", "=", r.id)])    # -> 0
    ```
    and both counts of step 181 unchanged. Any real-store order event in the build log reads `ok:ignored:catalog_only`. If a single real order appears in staging: **STOP**, uninstall the app from the real store, tell the lead (do not confirm, invoice or delete it by hand).
186. Run the upload on the real store with **this** backend picked in the wizard, Dry Run first. Leave Import Orders off for the whole real run.
187. **Every new build** (each push to `mainTest` = clean prod DB copy, no real backend): repeat 181-185 in the same order (create with Import Orders off, check it reads off) **before** the Webhook URL is updated in the Partners Portal or the app is reinstalled on the real store.

## T. Option images spike (photo per size)

**DEMO STORE ONLY (`461067869`).** Script: `tests/staging_option_images_spike.py`. It settles
whether each **Size** value of a Salla product can show its own gallery photo through the API,
and whether linking the photo damages the variants. It creates **one** throwaway product
`SPIKE-OI-<yymmddhhmmss>` (Size صغير / كبير x Scent رائحة أ / رائحة ب = 4 variants, prices
10 / 11 / 20 / 21, stock 3 / 4 / 5 / 6) and attaches 3 fake PNGs (grey **MAIN**, green **SMALL**,
orange **LARGE**). It sets the Size option to `display_type: image` (A), then sets each Size value's
`display_value` to its image id (B). After that it only reads. It refuses any store but the demo
(checked on the backend and with `GET /store/info`), never deletes and never sends a status other
than `hidden`. It writes only to the ids it created in the run, never sends `values` in an option PUT,
never sends price or quantity in a value PUT, and never creates a value or a variant. Every paste creates
a **new** product, so paste it once per run. No token is printed or typed.

188. **Build and backend.** Build green with `salla_connector` **17.0.5.0.0 or later** (`_request` with `lang=` / `files=`). The demo backend (`store_id` `461067869`) exists and **Test Connection** is green. On a new build, redo step 37 first.
189. **Run it.** odoo.sh > branch `mainTest` > current build > **Shell** tab > `odoo-bin shell`. Paste the **whole** file as it is in the repo (`RUN_SPIKE = True`). Expected: `store verified: demo 461067869`, some raw prints (keys, one value, the option GETs), then the block `===== OPTION IMAGES SPIKE FINDINGS (demo store 461067869, product #<id>, parent SKU SPIKE-OI-...) =====`, which ends with `Product #<id> left HIDDEN on the demo store, never deleted. ...` (or `Product #<id> NOT confirmed hidden (last status read '...'): hide it BY HAND ...`; see 190). If it prints `STOP: ...`, nothing was sent. Fix what it says (no backend bound to the demo store, module not upgraded, or `/store/info` answered another store), then paste again.
190. **Copy only the FINDINGS block into the ticket** (from the `=====` line to the closing `Product #...` line, or to the last finding when no product was created). It holds only ids, statuses, HTTP codes and Salla error messages. If the developer asks for the raw prints above it, paste those to them as well: they contain only spike data and image file names. Read the lines like this:

    | Finding | If it says | Then |
    |---|---|---|
    | `0 product created by this run` | an id | The spike product: check it in 191-192 |
    | `0 product created by this run` | `none`, with `1 create outcome unknown: GET /products/sku/<parent> answered no product id ...: check the demo by hand` or `... not found by parent SKU (HTTP ...)` | The create timed out and the product could not be adopted, but it **may still exist** (created hidden, nothing else written to it). Search `SPIKE-OI-` in the demo dashboard, write down what you find and tell the developer |
    | `1 create outcome unknown` | `adopted #<id> by parent SKU` | The create timed out but the spike found its product and went on: fine |
    | `1 create` | `FAILED ...` | Nothing else ran. Send the line to the developer |
    | `2 image ids` | each file `-> <id> (..., response id)` or `(..., detail, sort N)` | OK. `NOT FOUND` for the small or large file: that size is not linked (its `5 value ...` line says `SKIPPED: no image id`). Tell the developer |
    | `2 attach stopped` | `unknown outcome on <file>: not re-sent` | Expected behaviour after a timeout. The later images were not sent. Paste again later (a new product) |
    | `4 PUT /products/options/<size> {name, display_type: image}` | `ok`, or `HTTP 422 ...` | Write it down. On 422, look at the `5r ...` lines (A is retried once after B linked the images) |
    | `4 Size after: display_type / type / name kept` (or `5r ...`) | `'image' / ... / True` | Salla took the image display type |
    | `5 value #<id> (صغير)` / `(كبير)` | `display_value '<id>' (str): ok`, or `... HTTP 422 ... \| display_value <id> (int): ok` | Write down which type Salla accepted. `HTTP 422` on both: the API link with `display_value` does not work. Do 193 (link by hand + probe) |
    | `5 value #<id> after: display_value / hashed / image_url -> linked to <file>: True/False` | `True` for both sizes | The API link works |
    | `4` / `5` / `5r` / `7 variants/options intact after ...` | `yes` / `NO: ...` / `unknown: product unreadable ...` | Per-write detail for the verdict below |
    | `7 VARIANTS INTACT` | `yes (option/value ids, names, prices, quantities; variant ids, skus, prices, stock, related_option_values identical at every step)` | Expected. Linking the photos does not touch the variants |
    | `7 VARIANTS INTACT` | `NO: changed at step(s) [...]` | **STOP.** That write regenerated or changed the values or variants. Do not use option images in the uploader. Send every `variants/options intact` line to the developer and check the spike product's variants in the dashboard (191) |
    | `7 VARIANTS INTACT` | `UNKNOWN: product unreadable at step(s) [...]` | A read failed (no change was seen in the other reads). Check the variants by hand in 191, or paste again later |
    | `7 VARIANTS INTACT` | `unknown: no step 3 snapshot` | `3 snapshot FAILED`: A and B were not run (`step_a crashed` / `step_b crashed` with *no step 3 snapshot* is expected then). Paste again later |
    | `<n> status after <write>` | `sale`, then `<n> POST status hidden: ok` and `<n> status after the POST (GET): hidden` | Expected: that write published the product and the spike hid it again. Write down which write it was |
    | `<n> POST status hidden` | an HTTP error, or the last `status after ...` line is not `hidden` | **Hide the product by hand now** in the demo dashboard. Tell the developer |
    | `7 product` and the closing `Product #...` line | `#<id> left HIDDEN on the demo store, never deleted.` | The last status the spike **read** was hidden. Confirm it in 191 |
    | `7 product` and the closing `Product #...` line | `#<id> NOT confirmed hidden (last status read '<status>'): hide it BY HAND in the demo dashboard now.` | **Hide it by hand now**, reload the dashboard to confirm **Hidden**, and tell the developer |
    | `7 final POST status hidden` / `7 final hide` | present | A step crashed or was refused, so the spike hid the product once more. If `7 final hide: FAILED ...: hide #<id> by hand` appears, **hide it by hand** (the closing line then says `NOT confirmed hidden (last status read None)`) |
    | `REFUSED` | any | The guard stopped the run before a write it does not allow. Nothing outside the spike product was written. Send the line to the developer |
    | `... crashed` | any | Send the line as it is to the developer |
    | `6 GET ...` / `6 ?with=translations ...` | keys and values | Read-only. Write them down for the developer (where Salla keeps the link) |

191. **Demo dashboard.** Products > search `SPIKE-OI-` > open the product with the id from `0 product created by this run`. Check and write down:
    - Status **Hidden**.
    - **Options:** Size (الحجم) shows as an image option, and each value shows its photo: صغير = green **SMALL**, كبير = orange **LARGE**. The grey **MAIN** image is linked to no size. Scent (الرائحة) is still a text option with no images.
    - **Variants:** 4, SKUs `SPIKE-OI-...-S-A`, `-S-B`, `-L-A`, `-L-B`, prices 10 / 11 / 20 / 21, stock 3 / 4 / 5 / 6.
    - A screenshot in the ticket is fine (only fake spike data).
192. **Optional, storefront (demo only, never on the real store).** In the demo dashboard, make the spike product **visible by hand**. Open its page in the demo storefront, pick صغير, then كبير. Write down whether the main photo switches to the green SMALL / orange LARGE image. Then **hide it again by hand** and reload the dashboard to confirm it shows **Hidden**.
193. **Probe after linking an image by hand** (when 190 shows the API link failing, or to compare with what the dashboard writes):
    - In the demo dashboard, on the spike product (or another throwaway demo product with a size option), link an image to a size value by hand.
    - Shell: in your copy of the file, change `RUN_SPIKE = True` to `RUN_SPIKE = False` **before** pasting (then nothing is created or written), paste it, and call `probe(<product id>)`. In a shell where the spike already ran, call `probe(<product id>)` directly.
    - Expected: `store verified: demo 461067869`, then the product keys, each image (raw), `option #<id> '<name>' display_type ... type ..., keys [...]`, a `value, raw: {...}` line per value, the sku lines, the `?with=translations` lines and the `GET /products/options/...` lines. It sends GETs only (a write would raise `REFUSED: probe() never writes`).
    - Copy for the developer: the Size `option #...` line, and the `value, raw:` lines of the value you linked by hand and of a value without an image. The difference shows which field the dashboard writes (`display_value`, `hashed_display_value`, `image_url` or another one).
    - `STOP: ...` = wrong backend or store. Nothing was read.
194. **Clean-up and record.** The spike product stays **hidden** on the demo store (the script never deletes; delete it by hand if wanted). Ticket: the FINDINGS block, what the dashboard showed (191), the storefront result (192, or *not run*), the probe lines (193, or *not run*), and which steps passed / failed / were not run. No tokens, no URLs with tokens, no customer data.

## U. Photo per size in the upload wizard (17.0.5.1.0)

**DEMO STORE ONLY (`461067869`), never the real store.** New in 17.0.5.1.0: sheet column
`variant_image` = the photo of the row's **option1** value (Size). Apply sets option1 to
`display_type: image` and links each of its values to its gallery photo (`display_value`), after
the gallery attach and after the option English. Rules and messages: `claude-docs/14-product-upload.md`
§3 (`variant_image` row), §5 step 7b and §8. Paste the S helpers first (`BID` = the DEMO backend id),
then this one (ids, sort numbers and names only):
```python
def sl_oi(pid):  # gallery (id, sort, main) + each option's display_type and its values' linked image
    d = (b._request("GET", "/products/%s" % pid, lang="ar") or {}).get("data") or {}
    return dict(status=mapping.detail_status(d),
                images=[(i.get("id"), i.get("sort"), i.get("main"))
                        for i in mapping.detail_images(d)],
                options=[(o.get("id"), o.get("name"), o.get("display_type"),
                          [(v.get("id"), v.get("name"), v.get("display_value"),
                            v.get("hashed_display_value")) for v in o.get("values") or []])
                         for o in d.get("options") or []])
```

195. **Build.** Build green; `salla_connector` upgrades to **17.0.5.1.0** with no error or warning about the module in the build log. On a new build redo step 37; **Test Connection** green on the demo backend. `python3 salla_connector/tests/test_mapping.py` ended `all passed` before the push.
196. **(a) Download Template.** The **Products** sheet has the column `variant_image` (between `image_alt` and `salla_product_id`). The `example-options` rows: `image_1 = example-options-1.jpg`, `image_2 = example-options-500ml.jpg`, `image_3 = example-options-1l.jpg`; `variant_image` = `example-options-500ml.jpg` on the 500 مل row and `example-options-1l.jpg` on the 1 لتر row. `example-simple` has `variant_image` empty. The **Instructions** sheet has a `variant_image` row (level `variant`, required `no`) that says it must be one of image_1 ... image_10, the same file on every row of one option1 value, and not for a product without options.
197. **Test files** (from the template, all values fake):
    - `stg-u-sizes`: Size (500 مل / 1 لتر) x Scent (لافندر / ليمون), 4 rows `STG-U-001` ... `STG-U-004` (500/لافندر, 500/ليمون, 1 لتر/لافندر, 1 لتر/ليمون), prices 30 / 32 / 45 / 47, stock 4 / 0 / 12 / 3, weight 0.5 kg on all 4 rows, AR + EN names (option and values: Size / Scent, 500 ml / 1 L, Lavender / Lemon). `image_1 = stg-u-main.jpg`, `image_2 = stg-u-500.jpg`, `image_3 = stg-u-1l.jpg`. `variant_image` = `stg-u-500.jpg` on both 500 مل rows and `stg-u-1l.jpg` on both 1 لتر rows.
    - Images ZIP (inside a folder, as a Mac zips it): the 3 files, 1200 x 1200 JPEG < 300 KB, easy to tell apart (e.g. grey **MAIN**, green **500**, orange **1L**).
    - Broken copies of `stg-u-sizes` (same handle and SKUs, **Dry Run only, never Apply**), sheet rows 2-5 = `STG-U-001` ... `STG-U-004`:
        - `partial`: `variant_image` of `STG-U-002` (row 3) empty.
        - `mismatch`: `variant_image` of `STG-U-002` (row 3, a 500 مل row) = `stg-u-1l.jpg`.
        - `outside`: `variant_image` of both 1 لتر rows = `stg-u-other.jpg` (in the ZIP, but in no `image_N` column).
        - `simple`: one simple handle `stg-u-simple` (SKU `STG-U-090`, price 25, stock 3, weight 0.5 kg, `image_1 = stg-u-main.jpg`) with `variant_image = stg-u-main.jpg`.
198. **(b) Dry Run, Step Everything** (as admin), good sheet + ZIP. Before: `sl_find("STG-U")` -> `[]`. Expected for `stg-u-sizes`:
    - *would create it hidden with 4 variant(s)*, *would attach image(s) 1=stg-u-main.jpg, 2=stg-u-500.jpg, 3=stg-u-1l.jpg*.
    - *would show الحجم as image swatches* and *would link size photos: 500 مل -> stg-u-500.jpg, 1 لتر -> stg-u-1l.jpg*. No *size photos would not be linked* warning.
    - Same sheet **without the ZIP**: warning *size photos would not be linked: stg-u-500.jpg would not be attached; stg-u-1l.jpg would not be attached*.
    - **No write:** `sl_find("STG-U")` still `[]`.
    - **Broken copies** (each one, Dry Run): `stg-u-sizes` is listed with an error and skipped; no *would link size photos* line. Copy each error into the ticket. Expected texts:
        - `partial`: *Row 3: variant_image is empty: once a row of stg-u-sizes has one, every row needs the photo of its option1 value (an image option cannot have a value without a photo)*.
        - `mismatch`: *Row 3: variant_image stg-u-1l.jpg differs from row 2 (stg-u-500.jpg) for option1 value 500 مل: one photo per option1 value, the same on each of its rows*.
        - `outside`: *Row 4: variant_image stg-u-other.jpg is not one of image_1 ... image_10 of stg-u-sizes: add it to the gallery columns (Salla links a value to a gallery photo)* and the same for row 5.
        - `simple`: *Row 2: variant_image needs option1 (it is the photo of an option1 value); a product without options shows image_1* for `stg-u-simple`.
    - Any broken copy that does **not** show its error, or shows *would link size photos*: **STOP U**, developer.
199. **(c) Apply on a new demo product**, Step Everything, good sheet + ZIP, through the spy (shell, wizard `w` = `last_wiz()` with the same file): `calls = sl_spy(w, "stg-u-sizes", "apply"); print(opt_calls(calls))`. Expected:
    - `stg-u-sizes` **created**. Result lines *size photos linked: 500 مل -> stg-u-500.jpg, 1 لتر -> stg-u-1l.jpg* and *الحجم shown as image swatches*; **no** *size photos not linked*, *not set to image swatches*, *not linked to*, *every size photo was refused*, *time budget reached: size photos not linked* or *Salla undid the size photo link* line. If one appears: copy it, **STOP U**, developer.
    - `opt_calls(calls)`: the English option/value PUTs of S.5 (lang `ar`), then, **after** the 3 `POST /products/<id>/images` in `calls`, exactly one `("PUT", "/products/options/<Size option id>", "ar")` and two `("PUT", "/products/options/values/<Size value id>", "ar")`. Every option call has lang `ar` (never `en`, never a DELETE), and none is on the Scent option or its values.
    - `sl_oi(<id>)`: `images` = 3 with `sort` **exactly 1, 2, 3** as the uploader sent them (sort 1 main = MAIN). If Salla shows other sort numbers (0-based, gaps), write them down and tell the developer: the link of images already in Salla relies on sort == sheet position. Size option `display_type` `image`; the 500 مل value `display_value` (or `hashed_display_value`) = the id of the **sort 2** image, 1 لتر = the id of **sort 3**. Scent `display_type` `text`, no `display_value`. Write down the ids.
    - `sl(<id>)`: 4 variants with SKUs `STG-U-001` ... `004`, prices 30 / 32 / 45 / 47, stock 4 / 0 / 12 / 3 (the link did not touch them). `sl_options(<id>)`: Arabic names unchanged. `sl_options_en(<id>)`: Size / 500 ml / 1 L / Scent / Lavender / Lemon. If the result also says *Salla dropped the English name(s) ... on the size photo link: put back*, write it down (the first sighting; see 201).
    - **Ends hidden:** `sl(<id>)["status"]` = `hidden` and Sales -> Configuration -> Salla -> **Products Kept Hidden** has no row for it. Not hidden: **STOP U**, developer.
    - **Dashboard:** Products -> `stg-u-sizes`: Options -> Size shows **Option type: Image** with each value's thumbnail (500 مل = green, 1 لتر = orange); Scent stays Text; gallery MAIN (primary), 500, 1L in that order.
    - **Storefront (demo only):** make the product **visible by hand**, open its page, pick 500 مل then 1 لتر: the swatches show the photos with the value name under them and the main photo switches to the green / orange image. Then **hide it again by hand** and reload the dashboard: **Hidden**.
200. **(d) Re-run.** First **Dry Run** with the same files: *size photos already linked: 500 مل -> stg-u-500.jpg, 1 لتر -> stg-u-1l.jpg*, no *would show ... as image swatches*, no *would link size photos* (if it says *would link* again, write it down: the dry run reads the lookup answer, which may not carry `display_value`; tell the developer). Then **Apply** through the spy: `calls = sl_spy(w, "stg-u-sizes", "apply"); print(opt_calls(calls))` -> **no** `PUT` in the list. Result: *size photos already linked: ...*, no *size photos linked* line. `sl_oi(<id>)` and `sl(<id>)` identical to 199 (still 3 images, same ids, same variants), status `hidden`. Repeat with **Step Images only**, Apply: same result, no option PUT.
201. **(e) English value names survive the link** (the open question of §8). Demo product `glass-cleaner` (doc §7.2: Size x Scent, English option/value names already shown on the English dashboard, no images).
    - Before: write down `sl_options(<gc id>)`, `sl_options_en(<gc id>)`, `sl_oi(<gc id>)` and `sl(<gc id>)` (status, variants).
    - Sheet: the Wave 1 rows of `glass-cleaner` only. Add `image_1` = a main photo and one `image_N` per size (placeholder pack shots are fine on the demo), and `variant_image` on every row = the photo of its size. ZIP with those files. **Only Handles** = `glass-cleaner`, **Step Images only**.
    - **Dry Run:** *would attach image(s) ...*, *would show الحجم as image swatches*, *would link size photos: ...* (one entry per size).
    - **Apply** through the spy: `calls = sl_spy(w, "glass-cleaner", "apply"); print(opt_calls(calls))`. Expected: *size photos linked: ...* and *الحجم shown as image swatches*; every option call lang `ar`.
    - **English still there:** `sl_options_en(<gc id>)` shows the same English as before for Size and every Size value (`700 mL` ...), with lang en and in `translations.en`; the **English dashboard** shows them too. `sl_options(<gc id>)`: the Arabic unchanged. `sl(<gc id>)`: same variant ids, SKUs and prices; status as before (if it was hidden, still `hidden`).
    - **Write down which line appeared** (this is the finding):
        - no line about English -> Salla keeps the English through the link PUTs (`opt_calls` shows only the link PUTs);
        - *Salla dropped the English name(s) X on the size photo link: put back* -> Salla drops it and the uploader put it back (`opt_calls` shows extra lang `ar` PUTs on the same paths after the link PUTs). Write down X.
        - *Salla changed the Arabic value name X to Y on the size photo link: put back* (warning) -> Salla moved the Arabic and the uploader put it back: check `sl_options(<gc id>)` shows X again, write it down, tell the developer.
        - *Salla did not keep the English name(s) ... put back*, *English names not read before the link*, *not re-checked after the size photo link*, *names put back after the size photo link but not verified*, *the Arabic ... name changed when its English was put back*, *... putting it back failed*, *Salla undid the size photo link when the names were put back*, or *Salla dropped ... English writes are stopped in this run*: copy it, **STOP U**, developer. Fix the English / Arabic / photo link by hand in the dashboard.
    - Run Apply once more: *size photos already linked: ...*, no option PUT.
201a. **Gallery changed by hand is not guessed** (separate throwaway product, so `stg-u-sizes` stays clean). Sheet `stg-u-gap`: Size only (500 مل / 1 لتر), 2 rows `STG-U-101` / `STG-U-102`, price 30 / 45, stock 2 / 5, weight 0.5 kg, `image_1..image_3 = stg-u-main.jpg, stg-u-500.jpg, stg-u-1l.jpg`, `variant_image` = `stg-u-500.jpg` / `stg-u-1l.jpg`. **Apply** (Step Everything, same ZIP): created, *size photos linked: ...*, hidden. Write down `sl_oi(<gap id>)` (3 images, sorts 1, 2, 3).
    - In the demo dashboard **delete the middle image** (`stg-u-500`, sort 2) of `stg-u-gap` by hand. Write down `sl_oi(<gap id>)["images"]` (ids and sorts).
    - **Dry Run** (Only Handles `stg-u-gap`, same sheet and ZIP). **Do not Apply** this state (Apply would attach `stg-u-1l.jpg` again at sort 3).
    - If the sorts read **1, 3** (Salla did not renumber): expected warning *size photos would not be linked: option1 value 500 مل: no Salla image id for stg-u-500.jpg (not attached, or Salla's answer had no id: see the image messages, then re-upload the images with Images Only); Salla's images are not in the uploader's 1..2 order (deleted, added or moved by hand): which photo is which cannot be told; make the gallery in Salla match image_1..image_N*, and **no** *would link size photos* line.
    - If the sorts read **1, 2** (Salla renumbered after the delete): the dry run cannot see the delete (doc §8, "Not detectable") and may say *would link size photos*: write that down, **do not Apply**, and tell the developer (it means a hand delete can make the uploader link the wrong photo).
    - Leave `stg-u-gap` hidden (delete it by hand if wanted).
202. **Clean-up and record.** `stg-u-sizes`, `stg-u-gap` and `glass-cleaner` stay **hidden** on the demo store (the uploader never deletes or publishes). Ticket: the dry-run and error texts (198), the spy lists and `sl_oi` before/after (199-201), the dashboard and storefront result (199), the English line of 201, the sorts and dry-run result of 201a, and which steps passed / failed / were not run. No tokens, no URLs with tokens, no customer data.
