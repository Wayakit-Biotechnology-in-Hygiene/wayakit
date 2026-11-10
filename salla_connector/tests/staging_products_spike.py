# F8 spike: settle the open questions of claude-docs/14-product-upload.md with ONE
# throwaway HIDDEN product on the Salla DEMO store, before the real upload.
#
# Run it in the odoo.sh shell of the maintest build (Shell tab -> `odoo-bin shell`),
# pasting the whole file. Needs salla_connector >= 17.0.5.0.0 (_request with lang=,
# files=, data=). Everything goes through backend._request, so the token is never
# printed or typed. NEVER run it against the real store: it refuses any store id other
# than the demo one, checked on the backend AND against GET /store/info.
#
# It never calls DELETE and never sets status "sale". The product stays HIDDEN on the
# demo store: delete it by hand in the Salla dashboard if wanted.
#
# Copy only the FINDINGS block into the ticket (ids, statuses, HTTP codes, Salla error
# messages). No tokens, no customer data.

import inspect
import io
import time
from urllib.parse import urlparse
from odoo.exceptions import UserError
from odoo.addons.salla_connector.models import mapping

DEMO_STORE_ID = "461067869"
STAMP = time.strftime("%y%m%d%H%M")
PARENT_SKU = "SPIKE-F8-%s" % STAMP
VARIANT_SKUS = ["SPIKE-F8-%s-S" % STAMP, "SPIKE-F8-%s-L" % STAMP]
FAKE_BARCODE = "00%s" % STAMP                    # fake, leading zeros on purpose
AR_NAME = "منتج اختبار F8 لا يُنشر %s" % STAMP
EN_NAME = "F8 spike test product, do not publish %s" % STAMP
AR_DESC = "<p>وصف تجريبي للاختبار فقط.</p>"
EN_DESC = "<p>Test description, spike only.</p>"
OPTION_AR, OPTION_EN = "الحجم", "Size"
VALUES_AR, VALUE_EN = ["صغير", "كبير"], "Small"

b = env["salla.backend"].search([("store_id", "=", DEMO_STORE_ID)], limit=1)
STATE = {"verified": False}
F = {}          # findings, printed at the end


def _guard(path):
    if not b or b.store_id != DEMO_STORE_ID:
        raise RuntimeError("REFUSED: the backend is not bound to the demo store")
    if not STATE["verified"] and path != "/store/info":
        raise RuntimeError("REFUSED: store not verified against GET /store/info yet")


def call(method, path, **kw):
    """(ok, data_or_error, http_status). GET/POST/PUT only, never status sale."""
    _guard(path)
    method = method.upper()
    if method not in ("GET", "POST", "PUT"):
        raise RuntimeError("REFUSED: the spike never sends %s" % method)
    for body in (kw.get("payload"), kw.get("data")):
        if isinstance(body, dict) and "status" in body and body["status"] != "hidden":
            raise RuntimeError("REFUSED: the spike never sets status %r" % body["status"])
    try:
        res = b._request(method, path, **kw)
    except UserError as e:
        msg = e.args[0] if e.args else str(e)
        return False, str(msg)[:400], getattr(e, "http_status", "?")
    data = res.get("data") if isinstance(res, dict) else res
    return True, data, 200


def _skus(d):
    return [(s.get("id"), s.get("sku"), mapping.money(s.get("price")), s.get("stock_quantity"),
             s.get("related_option_values"), s.get("barcode"), s.get("weight"),
             s.get("weight_type")) for s in (d or {}).get("skus") or [] if isinstance(s, dict)]


def _options(d):
    return [(o.get("id"), o.get("name"), [(v.get("id"), v.get("name")) for v in
                                          o.get("values") or [] if isinstance(v, dict)])
            for o in (d or {}).get("options") or [] if isinstance(o, dict)]


def _status(d):
    return mapping.detail_status(d) if isinstance(d, dict) else "?"


def get(pid, lang=None):
    ok, d, st = call("GET", "/products/%s" % pid, lang=lang)
    return d if ok and isinstance(d, dict) else {}


def verify_store():
    if "lang" not in inspect.signature(b._request).parameters:
        print("STOP: _request has no lang= (needs salla_connector 17.0.5.0.0)")
        return False
    ok, d, st = call("GET", "/store/info")
    store = mapping.text((d or {}).get("id")) if ok and isinstance(d, dict) else ""
    if store != DEMO_STORE_ID:
        print("STOP: /store/info answered store %r (HTTP %s), not the demo store" % (store, st))
        return False
    STATE["verified"] = True
    print("store verified: demo", store)
    return True


def step_create():
    body = {"name": AR_NAME, "price": 10, "product_type": "product", "status": "hidden",
            "sku": PARENT_SKU, "description": AR_DESC, "require_shipping": True,
            "options": [{"name": OPTION_AR, "display_type": "text",
                         "values": [{"name": v} for v in VALUES_AR]}]}
    ok, d, st = call("POST", "/products", payload=body, idempotent=False)
    if not ok and st is None:
        print("create: unknown outcome, looking it up by parent SKU")
        ok, d, st = call("GET", "/products/sku/%s" % PARENT_SKU)
    if not ok or not isinstance(d, dict) or not d.get("id"):
        F["1 create"] = "FAILED HTTP %s: %s" % (st, d)
        return None
    print("create response keys:", sorted(d))
    print("create skus[]:", _skus(d))
    print("create options[]:", _options(d))
    F["1 create"] = "ok #%s, status in response %r" % (d["id"], _status(d))
    F["1 create response has skus[] / options[]"] = "%s / %s" % (bool(d.get("skus")),
                                                               bool(d.get("options")))
    detail = get(d["id"])
    F["1 parent sku kept"] = "%s (detail sku %r)" % (detail.get("sku") == PARENT_SKU,
                                                     detail.get("sku"))
    F["1 variants created"] = "%s, prices %s" % (len(_skus(detail)),
                                                 [s[2] for s in _skus(detail)])
    F["1 status after create (GET)"] = _status(detail)
    return d["id"]


def step_variants(pid):
    detail = get(pid)
    skus = [s for s in (detail.get("skus") or []) if isinstance(s, dict)]
    if len(skus) < 2:
        F["2 variant PUT"] = "skipped: %s variant(s) in Salla" % len(skus)
        return
    body = {"sku": VARIANT_SKUS[0], "price": 10, "stock_quantity": 3, "weight": 0.25,
            "weight_type": "kg", "barcode": FAKE_BARCODE}
    ok, d, st = call("PUT", "/products/variants/%s" % skus[0]["id"], payload=body)
    if not ok and st == 422:
        F["2 variant PUT with weight_type"] = "422: %s" % d
        body.pop("weight_type")
        ok, d, st = call("PUT", "/products/variants/%s" % skus[0]["id"], payload=body)
    else:
        F["2 variant PUT with weight_type"] = "accepted" if ok else "HTTP %s: %s" % (st, d)
    F["2 variant PUT"] = "ok" if ok else "HTTP %s: %s" % (st, d)
    ok2, d2, st2 = call("PUT", "/products/variants/%s" % skus[1]["id"],
                        payload={"sku": VARIANT_SKUS[1], "price": 12, "stock_quantity": 2})
    F["2 second variant PUT (price 12, qty 2)"] = "ok" if ok2 else "HTTP %s: %s" % (st2, d2)
    after = {s[0]: s for s in _skus(get(pid))}
    v = after.get(skus[0]["id"])
    print("variants after PUT:", list(after.values()))
    if v:
        F["2 decimal weight 0.25 kept"] = "%s (weight %r, weight_type %r)" % (
            mapping.money(v[6]) == 0.25, v[6], v[7])
        F["2 barcode leading zeros kept"] = "%s (barcode %r)" % (v[5] == FAKE_BARCODE, v[5])
        F["2 variant sku set"] = "%s (%r)" % (v[1] == VARIANT_SKUS[0], v[1])
    F["2 status after variant quantity"] = _status(get(pid))


def step_lookups(pid):
    for label, sku in (("parent", PARENT_SKU), ("variant", VARIANT_SKUS[0])):
        ok, d, st = call("GET", "/products/sku/%s" % sku)
        if ok and isinstance(d, dict):
            F["3 GET /products/sku/<%s sku>" % label] = "found id %s (product id is %s), " \
                "keys %s" % (d.get("id"), pid, sorted(d)[:12])
        else:
            F["3 GET /products/sku/<%s sku>" % label] = "HTTP %s: %s" % (st, str(d)[:150])
    ok, d, st = call("GET", "/products", params={"keyword": VARIANT_SKUS[0]})
    items = d if ok and isinstance(d, list) else []
    hit = [i.get("id") for i in items if isinstance(i, dict)
           and VARIANT_SKUS[0] in mapping.product_skus(i)]
    F["3 GET /products?keyword=<variant sku>"] = "%s item(s), ours carries the variant sku " \
        "in the list payload: %s" % (len(items), pid in hit)


def step_english(pid):
    before = get(pid)
    n_skus = len(_skus(before))
    ok, d, st = call("PUT", "/products/%s" % pid, lang="en",
                     payload={"name": EN_NAME, "description": EN_DESC})
    F["4 PUT /products/<id> lang en {name, description}"] = "ok" if ok else \
        "HTTP %s: %s" % (st, d)
    if not ok:
        return
    ar, en = get(pid, "ar"), get(pid, "en")
    print("after EN PUT: GET ar name=%r | GET en name=%r" % (ar.get("name"), en.get("name")))
    F["4 AR name survived"] = "%s (%r)" % (ar.get("name") == AR_NAME, ar.get("name"))
    F["4 EN name shown with lang en"] = "%s (%r)" % (en.get("name") == EN_NAME, en.get("name"))
    F["4 variants kept after a PUT without options"] = "%s (%s -> %s)" % (
        len(_skus(ar)) == n_skus, n_skus, len(_skus(ar)))
    F["4 price kept after a PUT without price"] = "%s (%s -> %s)" % (
        mapping.money(ar.get("price")) == mapping.money(before.get("price")),
        mapping.money(before.get("price")), mapping.money(ar.get("price")))
    if ar.get("name") != AR_NAME:
        ok, d, st = call("PUT", "/products/%s" % pid, lang="ar",
                         payload={"name": AR_NAME, "description": AR_DESC})
        F["4 AR restored"] = "%s (%r)" % (get(pid, "ar").get("name") == AR_NAME,
                                          "ok" if ok else "HTTP %s: %s" % (st, d))


def _find_option(d, oid):
    return next((o for o in _options(d) if str(o[0]) == str(oid)), None)


def step_option_english(pid):
    opts = _options(get(pid))
    if not opts or not opts[0][2]:
        F["5 option EN"] = "skipped: no option/value in the detail"
        return
    oid, vid = opts[0][0], opts[0][2][0][0]
    n_values, n_skus = len(opts[0][2]), len(_skus(get(pid)))
    ok, d, st = call("PUT", "/products/options/%s" % oid, lang="en", payload={"name": OPTION_EN})
    if not ok and st == 422:
        F["5 option PUT {name} only"] = "422: %s" % d
        ok, d, st = call("PUT", "/products/options/%s" % oid, lang="en",
                         payload={"name": OPTION_EN, "display_type": "text"})
    F["5 option PUT lang en (never values)"] = "ok" if ok else "HTTP %s: %s" % (st, d)
    ar, en = get(pid, "ar"), get(pid, "en")
    o_ar, o_en = _find_option(ar, oid), _find_option(en, oid)
    F["5 option AR survived"] = "%s (%r)" % (bool(o_ar) and o_ar[1] == OPTION_AR,
                                             o_ar and o_ar[1])
    F["5 option EN shown"] = "%s (%r)" % (bool(o_en) and o_en[1] == OPTION_EN, o_en and o_en[1])
    F["5 option values and variants kept"] = "%s (values %s -> %s, variants %s -> %s)" % (
        bool(o_ar) and len(o_ar[2]) == n_values and len(_skus(ar)) == n_skus, n_values,
        o_ar and len(o_ar[2]), n_skus, len(_skus(ar)))
    if o_ar and o_ar[1] != OPTION_AR:
        call("PUT", "/products/options/%s" % oid, lang="ar", payload={"name": OPTION_AR})
        F["5 option AR restored"] = (_find_option(get(pid, "ar"), oid) or [None, None])[1]
    ok, d, st = call("PUT", "/products/options/values/%s" % vid, lang="en",
                     payload={"name": VALUE_EN})
    F["5 value PUT lang en"] = "ok" if ok else "HTTP %s: %s" % (st, d)
    ar, en = get(pid, "ar"), get(pid, "en")
    v_ar = next((v for v in (_find_option(ar, oid) or [0, 0, []])[2] if str(v[0]) == str(vid)), None)
    v_en = next((v for v in (_find_option(en, oid) or [0, 0, []])[2] if str(v[0]) == str(vid)), None)
    F["5 value AR survived"] = "%s (%r)" % (bool(v_ar) and v_ar[1] == VALUES_AR[0],
                                            v_ar and v_ar[1])
    F["5 value EN shown"] = "%s (%r)" % (bool(v_en) and v_en[1] == VALUE_EN, v_en and v_en[1])
    if v_ar and v_ar[1] != VALUES_AR[0]:
        call("PUT", "/products/options/values/%s" % vid, lang="ar", payload={"name": VALUES_AR[0]})


def make_images():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (600, 600), (40, 120, 200))
    ImageDraw.Draw(img).rectangle([150, 150, 450, 450], fill=(250, 250, 250))
    out = {}
    buf = io.BytesIO()
    img.save(buf, "PNG")
    out["png"] = buf.getvalue()
    try:
        buf = io.BytesIO()
        img.save(buf, "WEBP", quality=80)
        out["webp"] = buf.getvalue()
    except Exception as e:      # Pillow built without libwebp
        out["webp_error"] = "%s: %s" % (type(e).__name__, e)
    return out


def _image_url(d):
    if not isinstance(d, dict):
        return ""
    for key in ("original", "url", "image", "photo", "thumbnail"):
        v = d.get(key)
        if isinstance(v, str) and v.startswith("https://"):
            return v
        if isinstance(v, dict):
            inner = _image_url(v)
            if inner:
                return inner
    return ""


def step_images(pid):
    imgs = make_images()
    url = ""
    for sort, ext, mime in ((1, "png", "image/png"), (2, "webp", "image/webp")):
        if ext not in imgs:
            F["6 attach %s" % ext] = "not generated: %s" % imgs.get("webp_error")
            continue
        ok, d, st = call("POST", "/products/%s/images" % pid, idempotent=False,
                         data={"sort": str(sort), "main": "1" if sort == 1 else "0",
                               "alt": "F8 spike"},
                         files={"photo": ("spike-f8-%d.%s" % (sort, ext), imgs[ext], mime)})
        F["6 attach %s multipart photo (%s bytes)" % (ext, len(imgs[ext]))] = \
            ("ok, response keys %s" % sorted(d)[:10]) if ok and isinstance(d, dict) else \
            ("ok" if ok else "HTTP %s: %s" % (st, d))
        if ok and not url:
            url = _image_url(d)
    detail = get(pid)
    if not url:
        url = next((_image_url(i) for i in mapping.detail_images(detail) if _image_url(i)), "")
    print("images in detail:", [{k: i.get(k) for k in ("id", "sort", "main", "default", "alt")
                                 if k in i} for i in mapping.detail_images(detail)])
    F["6 images in detail after attach"] = len(mapping.detail_images(detail))
    if not url:
        F["7 attach by original URL"] = "skipped: no image URL readable in the answers"
        return
    print("re-attaching the uploaded PNG by URL (host %s)" % urlparse(url).hostname)
    tried = []
    for label, kw in (("json original", {"payload": {"original": url, "sort": 3}}),
                      ("form original", {"data": {"original": url, "sort": "3"}}),
                      ("json photo", {"payload": {"photo": url, "sort": 3}})):
        ok, d, st = call("POST", "/products/%s/images" % pid, idempotent=False, **kw)
        tried.append("%s: %s" % (label, "ok" if ok else "HTTP %s %s" % (st, str(d)[:120])))
        if ok:
            break
    F["7 attach by original URL"] = " | ".join(tried)


def step_status(pid):
    detail = get(pid)
    F["8 status after images + quantity"] = "%s (images %s, variant qty %s)" % (
        _status(detail), len(mapping.detail_images(detail)), [s[3] for s in _skus(detail)])
    ok, d, st = call("POST", "/products/%s/status" % pid, payload={"status": "hidden"})
    F["8 POST /products/<id>/status hidden"] = "ok" if ok else "HTTP %s: %s" % (st, d)
    F["8 status after the POST (GET)"] = _status(get(pid))


def run_spike():
    if not b:
        print("STOP: no backend bound to the demo store")
        return F
    if not verify_store():
        return F
    pid = step_create()
    if pid:
        for step in (step_variants, step_lookups, step_english, step_option_english,
                     step_images, step_status):
            try:
                step(pid)
            except RuntimeError as e:          # a guard refused: stop everything
                F["REFUSED"] = str(e)
                break
            except Exception as e:             # report and keep settling the rest
                F["%s crashed" % step.__name__] = "%s: %s" % (type(e).__name__, str(e)[:200])
    print("\n===== F8 SPIKE FINDINGS (demo store %s, product #%s, parent SKU %s) =====" % (
        DEMO_STORE_ID, pid, PARENT_SKU))
    for key in sorted(F):
        print("  %s: %s" % (key, F[key]))
    print("Product #%s is left HIDDEN on the demo store; this script never deletes. "
          "Delete it by hand in the Salla dashboard if wanted." % pid)
    return F


FINDINGS = run_spike()
