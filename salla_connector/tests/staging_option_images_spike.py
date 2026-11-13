# Option-image spike: can each SIZE of a Salla product show its own photo? Settles, with
# ONE throwaway HIDDEN product on the Salla DEMO store, how a gallery image is linked to
# an option value through the API, and whether doing so damages the variants.
#
# Run it in the odoo.sh shell of the maintest build (Shell tab -> `odoo-bin shell`),
# pasting the whole file. Needs salla_connector >= 17.0.5.0.0 (_request with lang=,
# files=, data=). Everything goes through backend._request, so the token is never
# printed or typed. NEVER run it against the real store: it refuses any store id other
# than the demo one, checked on the backend AND against GET /store/info.
#
# It never calls DELETE and never sets a status other than "hidden". It writes ONLY to
# the product it creates and to that product's options, values and variants as read
# right after the create: any other id is refused by the guard. Option PUTs never carry
# "values" (Salla's spec: new values remove the old ones and regenerate the variants);
# value PUTs carry only name + display_value (no price / quantity); POST
# /products/options/{id} (= create a value, i.e. a new variant) is never sent. The
# product is hidden again after every write that can flip it and once more in
# `finally`: it stays HIDDEN on the demo store. Delete it by hand if wanted.
#
# Salla's spec (apidog, relayed 2026-11-13; field names still unverified, hence the raw
# keys printed at every read): option `display_type` text / image / color (the read
# model also has `type`, incl. image / thumbnail); value `display_value` (string) = the
# image id answered by POST /products/{id}/images; value details carry `image_url` and
# `hashed_display_value`. One image per value is the only API link: variants have no
# image field and the image attach has no option/variant field.
#
# Steps: 1 create (Size x Scent = 4 variants, prices 10/11/20/21, stock 3..6) -> 2 attach
# 3 PNGs (main, small, large) -> 3 snapshot -> 4 (A) Size display_type image -> 5 (B)
# each Size value display_value = its image id -> 6 (C) read-only GETs of the option,
# its values and ?with=translations -> 7 "variants intact: yes/no", gallery, status.
#
# probe(product_id), NOT run by default: GETs only, prints the raw keys of an existing
# demo product, its options and values. Link an image to a size BY HAND in the demo
# dashboard, then run probe() to see which field the dashboard writes. Without creating
# a new spike product: set RUN_SPIKE = False below, paste the file, call
# probe(<product id>). Same demo-store guard, and every write is refused while it runs.
#
# Copy only the FINDINGS block into the ticket (ids, statuses, HTTP codes, Salla error
# messages). No tokens, no customer data.

import inspect
import io
import re
import time
from urllib.parse import urlparse
from odoo.exceptions import UserError
from odoo.addons.salla_connector.models import mapping

DEMO_STORE_ID = "461067869"
RUN_SPIKE = True                # False: paste, then call probe(<product id>) by hand
STAMP = time.strftime("%y%m%d%H%M%S")
PARENT_SKU = "SPIKE-OI-%s" % STAMP
AR_NAME = "منتج اختبار صور المقاسات لا يُنشر %s" % STAMP
AR_DESC = "<p>وصف تجريبي للاختبار فقط.</p>"
SIZE_AR = "الحجم"
SIZE_VALUES_AR = ["صغير", "كبير"]
SIZE_CODES = ["S", "L"]
SCENT_AR = "الرائحة"
SCENT_VALUES_AR = ["رائحة أ", "رائحة ب"]
SCENT_CODES = ["A", "B"]
# (file name, colour, label, Size index or None); sort = position + 1, the first is main.
# The main image belongs to no size: shows whether a non-main image can be linked.
IMAGES = [
    ("spike-oi-main-no-size.png", (120, 120, 120), "MAIN", None),
    ("spike-oi-size-small.png", (40, 160, 80), "SMALL", 0),
    ("spike-oi-size-large.png", (230, 120, 30), "LARGE", 1),
]


def _variant_values(si, ci):
    """Price and stock of variant (size si, scent ci): 10/11/20/21 and 3/4/5/6, all
    distinct so a regenerated or reshuffled variant shows in the diff."""
    return 10 + 10 * si + ci, 3 + 2 * si + ci


b = env["salla.backend"].search([("store_id", "=", DEMO_STORE_ID)], limit=1)
STATE = {"verified": False, "read_only": False, "created": False, "base": None,
         "last_status": None,
         "size_option": "", "size_values": [], "images": [], "a_ok": False}
# Ids this run created (filled from the product's own detail right after the create).
# Every write is checked against them: nothing else on the demo store is ever written.
ALLOWED = {"product": "", "options": set(), "values": set(), "variants": set()}
F = {}          # findings, printed at the end
CHANGES = {}    # step -> differences vs the step 3 snapshot (ids, skus, prices, stock)

# (path, verb, kind of id, body keys allowed or None for multipart). Anything else that
# writes is refused, POST /products/options/{id} (creates a value + variant) included.
_WRITES = (
    (r"^/products/options/values/([^/]+)$", "PUT", "values", {"name", "display_value"}),
    (r"^/products/options/([^/]+)$", "PUT", "options", {"name", "display_type"}),
    (r"^/products/variants/([^/]+)$", "PUT", "variants", {"sku", "price", "stock_quantity"}),
    (r"^/products/([^/]+)/images$", "POST", "product", None),
    (r"^/products/([^/]+)/status$", "POST", "product", {"status"}),
)


def _guard(path):
    if not b or b.store_id != DEMO_STORE_ID:
        raise RuntimeError("REFUSED: the backend is not bound to the demo store")
    if not STATE["verified"] and path != "/store/info":
        raise RuntimeError("REFUSED: store not verified against GET /store/info yet")


def _guard_write(method, path, body):
    if STATE["read_only"]:
        raise RuntimeError("REFUSED: probe() never writes (%s %s)" % (method, path))
    if method == "POST" and path == "/products":
        # Set before the POST: an unknown outcome is looked up, never re-sent.
        if STATE["created"]:
            raise RuntimeError("REFUSED: one product per run")
        STATE["created"] = True
        return
    for pattern, verb, kind, keys in _WRITES:
        m = re.match(pattern, path)
        if not m:
            continue
        if method != verb:
            raise RuntimeError("REFUSED: the spike never sends %s %s" % (method, path))
        allowed = ALLOWED[kind] if kind != "product" else {ALLOWED["product"]} - {""}
        if m.group(1) not in allowed:
            raise RuntimeError("REFUSED: %s %s: id %s was not created by this run" % (
                method, path, m.group(1)))
        if keys is not None and isinstance(body, dict) and not set(body) <= keys:
            raise RuntimeError("REFUSED: %s %s with keys %s (allowed %s)" % (
                method, path, sorted(body), sorted(keys)))
        return
    raise RuntimeError("REFUSED: the spike never writes to %s" % path)


def call(method, path, **kw):
    """(ok, data_or_error, http_status). GET/POST/PUT only, never a status but hidden,
    writes only on ids this run created."""
    _guard(path)
    method = method.upper()
    if method not in ("GET", "POST", "PUT"):
        raise RuntimeError("REFUSED: the spike never sends %s" % method)
    for body in (kw.get("payload"), kw.get("data")):
        if isinstance(body, dict) and "status" in body and body["status"] != "hidden":
            raise RuntimeError("REFUSED: the spike never sets status %r" % body["status"])
        # An option PUT never carries "values": that is what rewrites them (and the variants).
        if isinstance(body, dict) and "values" in body and \
                path.startswith("/products/options/") and method == "PUT":
            raise RuntimeError("REFUSED: the spike never sends values in an option PUT")
    if method != "GET":
        _guard_write(method, path, kw["payload"] if "payload" in kw else kw.get("data"))
        # Any write may flip the status (stock, images...): unknown until read again, so
        # a crash before the next read never leaves "left HIDDEN" in the findings.
        STATE["last_status"] = None
    try:
        res = b._request(method, path, **kw)
    except UserError as e:
        msg = e.args[0] if e.args else str(e)
        return False, str(msg)[:400], getattr(e, "http_status", "?")
    data = res.get("data") if isinstance(res, dict) else res
    return True, data, 200


def _short(value, n=60):
    value = mapping.text(value) if not isinstance(value, (dict, list)) else repr(value)
    value = re.sub(r"\s+", " ", value)
    return value if len(value) <= n else value[:n] + "..."


def _id(value):
    """An id as a string ("" when absent); a {"id": ...} object gives its id."""
    if isinstance(value, dict):
        value = value.get("id")
    return mapping.clean_id(value)


def _name(value, lang="ar"):
    """A name of unknown shape: a string, or {ar, en} on a multilingual answer."""
    if isinstance(value, dict):
        value = value.get(lang)
    return mapping.text(value)


def _status(d):
    return mapping.detail_status(d) if isinstance(d, dict) else "?"


def _err(ok, d, st, n=300):
    return "ok" if ok else "HTTP %s: %s" % (st, _short(d, n))


def get(pid, lang="ar", params=None):
    ok, d, st = call("GET", "/products/%s" % pid, lang=lang, params=params)
    return d if ok and isinstance(d, dict) else {}


def _url(value):
    """Host + file of an image URL (the whole URL is long and says nothing more)."""
    value = mapping.text(value)
    if not value.startswith("http"):
        return _short(value, 80) or None
    return "%s/...%s" % (urlparse(value).hostname, value.rsplit("/", 1)[-1][-40:])


def verify_store():
    params = inspect.signature(b._request).parameters
    if "lang" not in params or "files" not in params:
        print("STOP: _request has no lang= / files= (needs salla_connector 17.0.5.0.0)")
        return False
    ok, d, st = call("GET", "/store/info")
    store = mapping.text((d or {}).get("id")) if ok and isinstance(d, dict) else ""
    if store != DEMO_STORE_ID:
        print("STOP: /store/info answered store %r (HTTP %s), not the demo store" % (store, st))
        return False
    STATE["verified"] = True
    print("store verified: demo", store)
    return True


def hide(pid, n):
    ok, d, st = call("POST", "/products/%s/status" % pid, payload={"status": "hidden"})
    F["%s POST status hidden" % n] = _err(ok, d, st)
    STATE["last_status"] = _status(get(pid))
    F["%s status after the POST (GET)" % n] = STATE["last_status"]


def keep_hidden(pid, n, label):
    """Status after a write; hidden again at once if Salla flipped it."""
    status = STATE["last_status"] = _status(get(pid))
    F["%s status after %s" % (n, label)] = status
    if status != "hidden":
        hide(pid, n)


def left_as(pid):
    """What the run really left: only a status READ as hidden is called hidden (a
    status POST can answer an HTTP error without raising)."""
    status = STATE["last_status"]
    if status == "hidden":
        return "#%s left HIDDEN on the demo store, never deleted." % pid
    return "#%s NOT confirmed hidden (last status read %r): hide it BY HAND in the demo " \
        "dashboard now. Never deleted." % (pid, status)


# ------------------------------------------------------------------ snapshot + diff
def _option_read(o):
    """What one option says about its display: `display_type` (request field) and
    `type` (read model, incl. image / thumbnail) are both printed."""
    return {"id": _id(o.get("id")), "name": _name(o.get("name")),
            "display_type": o.get("display_type", "<absent>"),
            "type": o.get("type", "<absent>"),
            # price / quantity: a display_value PUT must not reset them (compared in diff).
            "values": [{"id": _id(v.get("id")), "name": _name(v.get("name")),
                        "price": mapping.money(v.get("price")),
                        "quantity": v.get("quantity", "<absent>"),
                        "display_value": v.get("display_value", "<absent>"),
                        "hashed_display_value": v.get("hashed_display_value", "<absent>"),
                        "image_url": _url(v.get("image_url")) if v.get("image_url")
                        else v.get("image_url", "<absent>")}
                       for v in o.get("values") or [] if isinstance(v, dict)]}


def snap(d):
    skus = {}
    for s in (d or {}).get("skus") or []:
        if isinstance(s, dict):
            skus[_id(s.get("id"))] = (
                s.get("sku"), mapping.money(s.get("price")), s.get("stock_quantity"),
                tuple(sorted(_id(x) for x in s.get("related_option_values") or [])))
    return {"options": [_option_read(o) for o in (d or {}).get("options") or []
                        if isinstance(o, dict)],
            "skus": skus,
            "images": [(_id(i.get("id")), i.get("sort"), i.get("default", i.get("main")))
                       for i in mapping.detail_images(d)],
            "status": _status(d)}


def _size(s):
    """The Size option of a snapshot, by the id step 1 recorded ({} when gone)."""
    return next((o for o in s["options"] if o["id"] == STATE["size_option"]), {})


def diff(before, after):
    """What changed in options / values (id, name, price, quantity) / variants (the
    display fields may change on purpose and are not compared). [] = intact."""
    out = []
    ob, oa = [o["id"] for o in before["options"]], [o["id"] for o in after["options"]]
    if ob != oa:
        out.append("option ids %s -> %s" % (ob, oa))
    by_id = {o["id"]: o for o in after["options"]}
    for o in before["options"]:
        a = by_id.get(o["id"])
        if not a:
            continue
        vb = [(v["id"], v["name"], v["price"], v["quantity"]) for v in o["values"]]
        va = [(v["id"], v["name"], v["price"], v["quantity"]) for v in a["values"]]
        if vb != va:
            out.append("option #%s values %s -> %s" % (o["id"], vb, va))
        if o["name"] != a["name"]:
            out.append("option #%s name %r -> %r" % (o["id"], o["name"], a["name"]))
    sb, sa = before["skus"], after["skus"]
    if set(sb) != set(sa):
        out.append("variant ids %s -> %s" % (sorted(sb), sorted(sa)))
    for vid in sorted(set(sb) & set(sa)):
        if sb[vid] != sa[vid]:
            out.append("variant #%s %s -> %s" % (vid, sb[vid], sa[vid]))
    return out


def _unreadable(s):
    """A failed GET (get() answers {}) reads as a product with nothing: not a change."""
    return not s["skus"] and not s["options"]


def record_diff(n, label, after):
    key = "%s variants/options intact after %s" % (n, label)
    if _unreadable(after):
        CHANGES[n] = None                  # unknown, never counted as "changed"
        F[key] = "unknown: product unreadable (GET failed or empty)"
        return
    changes = diff(STATE["base"], after)
    CHANGES[n] = changes
    F[key] = "yes" if not changes else "NO: %s" % _short("; ".join(changes), 600)


# ------------------------------------------------------------------ steps
def _allow(d):
    """Record the option / value / variant ids of OUR product (the create answer or
    its detail): the only ones the guard lets the spike write."""
    for o in (d or {}).get("options") or []:
        if isinstance(o, dict) and _id(o.get("id")):
            ALLOWED["options"].add(_id(o.get("id")))
            ALLOWED["values"].update(_id(v.get("id")) for v in o.get("values") or []
                                     if isinstance(v, dict) and _id(v.get("id")))
    ALLOWED["variants"].update(_id(s.get("id")) for s in (d or {}).get("skus") or []
                               if isinstance(s, dict) and _id(s.get("id")))


def step_create():
    body = {"name": AR_NAME, "price": 10, "product_type": "product", "status": "hidden",
            "sku": PARENT_SKU, "description": AR_DESC, "require_shipping": True,
            # Salla requires weight on create: without it, 422 fields.weight (demo 2026-11-10).
            "weight": 0.5, "weight_type": "kg",
            "options": [{"name": SIZE_AR, "display_type": "text",
                         "values": [{"name": v} for v in SIZE_VALUES_AR]},
                        {"name": SCENT_AR, "display_type": "text",
                         "values": [{"name": v} for v in SCENT_VALUES_AR]}]}
    ok, d, st = call("POST", "/products", payload=body, idempotent=False, lang="ar")
    pid = _id(d.get("id")) if ok and isinstance(d, dict) else ""
    if not ok and st is None:
        print("create: unknown outcome, looking it up by parent SKU")
        ok, d, st = call("GET", "/products/sku/%s" % PARENT_SKU)
        # As the wizard does: a variant answer without its parent id gives "".
        pid = mapping.sku_answer_product_id(d) if ok else ""
        if pid:
            F["1 create outcome unknown"] = "adopted #%s by parent SKU" % pid
        elif ok:
            F["1 create outcome unknown"] = "GET /products/sku/<parent> answered no " \
                "product id (variant without parent?), keys %s: check the demo by hand" % (
                    sorted(d)[:12] if isinstance(d, dict) else type(d).__name__)
        else:
            F["1 create outcome unknown"] = "not found by parent SKU (%s)" % _err(ok, d, st, 200)
        d = {}              # the SKU answer may be a variant: ids only from the detail
    if not pid:
        F["1 create"] = "FAILED %s" % (_err(ok, d, st, 400) if not ok else
                                       "no product id in the answer")
        return None
    ALLOWED["product"] = pid
    _allow(d)
    detail = get(pid)
    _allow(detail)
    s = snap(detail)
    size = next((o for o in s["options"] if o["name"] == SIZE_AR), None)
    if size is None and s["options"]:
        size = s["options"][0]
        F["1 Size option found by"] = "position (no option named %r)" % SIZE_AR
    STATE["size_option"] = size["id"] if size else ""
    STATE["size_values"] = [v["id"] for v in size["values"]] if size else []
    F["1 create"] = "ok #%s, %s option(s), %s variant(s), status %r" % (
        pid, len(s["options"]), len(s["skus"]), s["status"])
    F["1 options (id, name, display_type, type, values)"] = _short(
        [(o["id"], o["name"], o["display_type"], o["type"],
          [(v["id"], v["name"]) for v in o["values"]]) for o in s["options"]], 400)
    F["1 ids allowed for writes"] = "product %s, options %s, values %s, variants %s" % (
        pid, sorted(ALLOWED["options"]), sorted(ALLOWED["values"]), sorted(ALLOWED["variants"]))
    return pid


def step_variants(pid):
    s = snap(get(pid))
    size = _size(s)
    scent = next((o for o in s["options"] if o["id"] != STATE["size_option"]), {})
    size_ids = [v["id"] for v in size.get("values", [])]
    scent_ids = [v["id"] for v in scent.get("values", [])]
    results = []
    for vid, (sku, price, stock, rov) in sorted(s["skus"].items(), key=repr):
        si = next((i for i, x in enumerate(size_ids) if x in rov), None)
        ci = next((i for i, x in enumerate(scent_ids) if x in rov), None)
        if si is None or ci is None or si > 1 or ci > 1:
            results.append("#%s not mapped (related_option_values %s)" % (vid, rov))
            continue
        price, stock = _variant_values(si, ci)
        body = {"sku": "%s-%s-%s" % (PARENT_SKU, SIZE_CODES[si], SCENT_CODES[ci]),
                "price": price, "stock_quantity": stock}
        ok, d, st = call("PUT", "/products/variants/%s" % vid, payload=body)
        results.append("#%s %s %s" % (vid, body["sku"], _err(ok, d, st, 150)))
    F["1 variant PUTs (sku, price, stock)"] = "; ".join(results) or "no variants"
    after = snap(get(pid))
    # key=repr: a null sku or price would make tuple comparison raise TypeError.
    print("variants after PUT:", sorted(after["skus"].items(), key=repr))
    F["1 variants set (sku, price, stock)"] = _short(
        sorted((v[:3] for v in after["skus"].values()), key=repr), 400)
    F["1 status after the stock writes"] = after["status"]
    # Stock flips a hidden product to sale (2026-11-10): hidden again before going on.
    hide(pid, "1")


def _png(colour, label):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (600, 600), colour)
    draw = ImageDraw.Draw(img)
    draw.rectangle([100, 220, 500, 380], fill=(250, 250, 250))
    try:
        font = ImageFont.load_default(size=72)    # Pillow >= 10.1
    except TypeError:
        font = ImageFont.load_default()
    draw.text((130, 255), label, fill=(0, 0, 0), font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def step_images(pid):
    before = {_id(i.get("id")) for i in mapping.detail_images(get(pid))}
    for n, (fname, colour, label, size) in enumerate(IMAGES, 1):
        entry = {"file": fname, "size": size, "sort": n, "raw_id": None, "how": ""}
        STATE["images"].append(entry)
        ok, d, st = call("POST", "/products/%s/images" % pid, idempotent=False,
                         data={"sort": str(n), "main": "1" if n == 1 else "0",
                               "alt": "option image spike %s" % label},
                         files={"photo": (fname, _png(colour, label), "image/png")})
        if ok and isinstance(d, dict):
            print("attach %s response: keys %s, raw %s" % (fname, sorted(d), _short(d, 400)))
            F["2 attach response keys"] = sorted(d)
            if _id(d.get("id")):
                entry["raw_id"], entry["how"] = d.get("id"), "response id"
        F["2 attach %s" % fname] = _err(ok, d, st)
        if not ok and st is None:
            F["2 attach stopped"] = "unknown outcome on %s: not re-sent" % fname
            break
    images = mapping.detail_images(get(pid))
    new = [i for i in images if _id(i.get("id")) not in before]
    new_ids = {_id(i.get("id")) for i in new}
    for entry in STATE["images"]:
        if entry["raw_id"] is not None and _id(entry["raw_id"]) not in new_ids:
            # The answer's `id` may be something else (e.g. a media id): only an id the
            # gallery shows as new is linked to a size.
            entry["how"] = "response id %r not in the gallery" % entry["raw_id"]
            entry["raw_id"] = None
        if entry["raw_id"] is None:
            # Fallback: the new image with this sort in the product read.
            hit = [i for i in new if mapping.text(i.get("sort")) == str(entry["sort"])]
            if len(hit) == 1:
                entry["raw_id"] = hit[0].get("id")
                entry["how"] = "; ".join(x for x in (entry["how"], "detail, sort %s" %
                                                     entry["sort"]) if x)
            elif entry["how"]:
                entry["how"] += "; NOT FOUND in the product read"
    if images:
        print("one image in the detail, raw:", _short(images[-1], 600))
        F["2 image raw keys (detail)"] = sorted(images[-1])
    F["2 image ids"] = "; ".join("%s -> %r (%s, %s)" % (
        e["file"], e["raw_id"], type(e["raw_id"]).__name__, e["how"] or "NOT FOUND")
        for e in STATE["images"])
    F["2 gallery count after attach"] = "%s (%s new)" % (len(images), len(new))
    # An image upload also flips a hidden product with stock to sale (2026-11-10).
    keep_hidden(pid, "2", "the image uploads")


def step_snapshot(pid):
    d = get(pid)
    s = snap(d)
    if _unreadable(s):
        # An empty base would make every later read look "intact" or "changed" for
        # nothing: no snapshot, so experiments A and B do not run.
        F["3 snapshot"] = "FAILED: product unreadable; experiments A and B not run"
        return
    STATE["base"] = s
    print("product top-level keys:", sorted(d))
    options = [o for o in d.get("options") or [] if isinstance(o, dict)]
    if options:
        F["3 option raw keys"] = sorted(options[0])
        values = [v for v in options[0].get("values") or [] if isinstance(v, dict)]
        if values:
            F["3 value raw keys"] = sorted(values[0])
            print("one value, raw:", values[0])
    skus = [x for x in d.get("skus") or [] if isinstance(x, dict)]
    if skus:
        F["3 sku raw keys"] = sorted(skus[0])
    size = _size(s)
    F["3 Size option before"] = "#%s %r display_type %r type %r" % (
        size.get("id"), size.get("name"), size.get("display_type"), size.get("type"))
    F["3 Size values before (id, name, display_value, hashed, image_url)"] = _short(
        [(v["id"], v["name"], v["display_value"], v["hashed_display_value"], v["image_url"])
         for v in size.get("values", [])], 400)
    F["3 variants before (sku, price, stock, related_option_values)"] = _short(
        sorted(s["skus"].values(), key=repr), 500)
    F["3 images (id, sort, default)"] = s["images"]
    F["3 status"] = s["status"]


def _put_display_type(pid, n):
    """PUT {name, display_type: image} on the Size option (never "values")."""
    if not STATE["base"]:
        raise Exception("no step 3 snapshot: experiment not run")
    size = _size(snap(get(pid)))
    if not size or size["name"] != SIZE_AR:
        F["%s option PUT" % n] = "SKIPPED: Size option #%s not as created (%r)" % (
            STATE["size_option"], size.get("name"))
        return
    ok, d, st = call("PUT", "/products/options/%s" % size["id"], lang="ar",
                     payload={"name": size["name"], "display_type": "image"})
    F["%s PUT /products/options/<size> {name, display_type: image}" % n] = _err(ok, d, st, 400)
    if ok and isinstance(d, dict):
        F["%s option PUT response keys" % n] = sorted(d)
    after = snap(get(pid))
    size = _size(after)
    F["%s Size after: display_type / type / name kept" % n] = "%r / %r / %s" % (
        size.get("display_type"), size.get("type"), size.get("name") == SIZE_AR)
    STATE["a_ok"] = size.get("display_type") == "image" or size.get("type") == "image"
    return after


def step_a(pid):
    after = _put_display_type(pid, "4")
    if after:
        record_diff("4", "A (display_type)", after)
    keep_hidden(pid, "4", "A")


def _alt_type(raw):
    """The image id in the other type (the spec says string; the attach answers what
    it answers): str <-> int, None when there is no other form."""
    if isinstance(raw, int) and not isinstance(raw, bool):
        return str(raw)
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


def step_b(pid):
    if not STATE["base"]:
        raise Exception("no step 3 snapshot: experiment not run")
    wrote = False
    for idx, vid in enumerate(STATE["size_values"][:2]):
        img = next((e for e in STATE["images"] if e["size"] == idx), {})
        key = "5 value #%s (%s)" % (vid, SIZE_VALUES_AR[idx])
        if img.get("raw_id") is None:
            F[key] = "SKIPPED: no image id for %s" % img.get("file", "this size (step 2)")
            continue
        # Re-read before each PUT: Salla may replace the values after the first one.
        current = {v["id"]: v for v in _size(snap(get(pid))).get("values", [])}
        value = current.get(vid)
        if not value:
            F[key] = "SKIPPED: value gone from the product (regenerated?); new ids are " \
                "never written"
            continue
        if value["name"] != SIZE_VALUES_AR[idx]:
            F[key] = "SKIPPED: name %r not as created" % value["name"]
            continue
        # String first (the spec types display_value as string), the other type on 422.
        raw = img["raw_id"]
        tries = [str(raw).strip() if not isinstance(raw, str) else raw]
        alt = _alt_type(tries[0])
        if alt is not None:
            tries.append(alt)
        done = []
        for dv in tries:
            ok, d, st = call("PUT", "/products/options/values/%s" % vid, lang="ar",
                             payload={"name": value["name"], "display_value": dv})
            done.append("display_value %r (%s): %s" % (dv, type(dv).__name__,
                                                        _err(ok, d, st, 300)))
            if ok:
                wrote = True
                if isinstance(d, dict):
                    F["5 value PUT response keys"] = sorted(d)
                break
            if st != 422:
                break
        F[key] = " | ".join(done)
    after = snap(get(pid))
    size = _size(after)
    for v in size.get("values", []):
        img = next((e for e in STATE["images"]
                    if e["size"] is not None and e["size"] < len(STATE["size_values"])
                    and STATE["size_values"][e["size"]] == v["id"]), {})
        want = mapping.text(img.get("raw_id"))
        F["5 value #%s after: display_value / hashed / image_url" % v["id"]] = \
            "%r / %r / %r -> linked to %s: %s" % (
                v["display_value"], v["hashed_display_value"], v["image_url"],
                img.get("file", "?"),
                bool(want) and want in (mapping.text(v["display_value"]),
                                        mapping.text(v["hashed_display_value"])))
    F["5 Size after B: display_type / type"] = "%r / %r" % (size.get("display_type"),
                                                            size.get("type"))
    record_diff("5", "B (display_value)", after)
    keep_hidden(pid, "5", "B")
    if wrote and not STATE["a_ok"]:
        # A may have been refused because no value had an image yet: once more now.
        after = _put_display_type(pid, "5r")
        if after:
            record_diff("5r", "A retried after B", after)
        keep_hidden(pid, "5r", "A retried")


def step_c(pid):
    """Read-only: where does the link live?"""
    oid = STATE["size_option"]
    ok, d, st = call("GET", "/products/options/%s" % oid, lang="ar")
    if ok and isinstance(d, dict):
        print("GET /products/options/<size>, raw:", _short(d, 1500))
        F["6 GET /products/options/<size> keys"] = sorted(d)
        F["6 GET /products/options/<size> display_type / type"] = "%r / %r" % (
            d.get("display_type", "<absent>"), d.get("type", "<absent>"))
        vals = [v for v in d.get("values") or [] if isinstance(v, dict)]
        if vals:
            F["6 GET /products/options/<size> value keys"] = sorted(vals[0])
    else:
        F["6 GET /products/options/<size>"] = _err(ok, d, st)
    for vid in STATE["size_values"][:2]:
        ok, d, st = call("GET", "/products/options/values/%s" % vid, lang="ar")
        if ok and isinstance(d, dict):
            print("GET /products/options/values/%s, raw:" % vid, _short(d, 1500))
            F["6 GET values/#%s keys" % vid] = sorted(d)
            F["6 GET values/#%s display_value / hashed / image_url / skus" % vid] = \
                "%r / %r / %r / %s" % (d.get("display_value", "<absent>"),
                                       d.get("hashed_display_value", "<absent>"),
                                       _url(d.get("image_url")) or d.get("image_url", "<absent>"),
                                       _short(d.get("skus", "<absent>"), 200))
        else:
            F["6 GET values/#%s" % vid] = _err(ok, d, st)
    ok, d, st = call("GET", "/products/%s" % pid, lang="ar", params={"with": "translations"})
    if not ok or not isinstance(d, dict):
        F["6 GET ?with=translations"] = _err(ok, d, st)
        return
    o = next((x for x in d.get("options") or [] if isinstance(x, dict)
              and _id(x.get("id")) == oid), {})
    print("?with=translations Size option, raw:", _short(o, 1500))
    F["6 ?with=translations Size option keys"] = sorted(o)
    vals = [v for v in o.get("values") or [] if isinstance(v, dict)]
    if vals:
        F["6 ?with=translations Size value keys"] = sorted(vals[0])
        F["6 ?with=translations Size value translations"] = _short(
            vals[0].get("translations", "<absent>"), 200)


def step_final(pid):
    d = get(pid)
    s = snap(d)
    if STATE["base"]:
        record_diff("7", "everything", s)
        broken = sorted(n for n, c in CHANGES.items() if c)
        unread = sorted(n for n, c in CHANGES.items() if c is None)
        if broken:
            F["7 VARIANTS INTACT"] = "NO: changed at step(s) %s; see 'variants/options " \
                "intact' above%s" % (broken, " (unreadable at %s)" % unread if unread else "")
        elif unread:
            F["7 VARIANTS INTACT"] = "UNKNOWN: product unreadable at step(s) %s, no " \
                "change seen in the other reads" % unread
        else:
            F["7 VARIANTS INTACT"] = "yes (option/value ids, names, prices, quantities; " \
                "variant ids, skus, prices, stock, related_option_values identical at " \
                "every step)"
    else:
        F["7 VARIANTS INTACT"] = "unknown: no step 3 snapshot"
    size = _size(s)
    F["7 Size option display_type / type"] = "%r / %r" % (size.get("display_type"),
                                                          size.get("type"))
    F["7 Size values (id, display_value, hashed, image_url)"] = _short(
        [(v["id"], v["display_value"], v["hashed_display_value"], v["image_url"])
         for v in size.get("values", [])], 400)
    F["7 gallery count"] = len(s["images"])
    keep_hidden(pid, "7", "the run")
    F["7 product"] = "%s Mafer: open it in the demo dashboard (Products -> this product " \
        "-> Options) to see whether the size values show their linked image; optionally " \
        "make it visible BY HAND on the demo store to see the storefront, then hide it " \
        "again." % left_as(pid)


def run_spike():
    pid = None
    if not b:
        print("STOP: no backend bound to the demo store")
        return F
    if not verify_store():
        return F
    try:
        pid = step_create()
    except RuntimeError as e:
        F["REFUSED"] = str(e)
    except Exception as e:
        # After the POST the product may exist: the finally below still hides it.
        F["step_create crashed"] = "%s: %s" % (type(e).__name__, str(e)[:200])
    pid = pid or ALLOWED["product"] or None
    if pid:
        done = set()
        steps = () if "step_create crashed" in F or "REFUSED" in F else (
            step_variants, step_images, step_snapshot, step_a, step_b, step_c, step_final)
        try:
            for step in steps:
                try:
                    step(pid)
                    done.add(step.__name__)
                except RuntimeError as e:          # a guard refused: stop everything
                    F["REFUSED"] = str(e)
                    break
                except Exception as e:             # report and keep settling the rest
                    F["%s crashed" % step.__name__] = "%s: %s" % (type(e).__name__,
                                                                  str(e)[:200])
        finally:
            # A refusal (or a crash) after a write that flipped the status must not
            # leave the demo product published: hide it once more (the guard applies).
            if STATE["verified"] and "step_final" not in done:
                try:
                    hide(pid, "7 final")
                except Exception as e:
                    F["7 final hide"] = "FAILED %s: %s: hide #%s by hand" % (
                        type(e).__name__, str(e)[:200], pid)
    F["0 product created by this run"] = ALLOWED["product"] or "none"
    print("\n===== OPTION IMAGES SPIKE FINDINGS (demo store %s, product #%s, parent SKU %s) "
          "=====" % (DEMO_STORE_ID, pid, PARENT_SKU))
    seen = list(F)

    def _order(key):    # by step number ("5r" after "5"), then as found
        m = re.match(r"^(\d+)([a-z]?)$", key.split(" ", 1)[0])
        return (int(m.group(1)) if m else 999, m.group(2) if m else "", seen.index(key))

    for key in sorted(F, key=_order):
        print("  %s: %s" % (key, F[key]))
    if pid:
        print("Product %s Delete it by hand in the Salla dashboard if wanted." % left_as(pid))
    return F


def probe(product_id):
    """GETs only: raw keys of an existing demo product, its options and values (e.g.
    after linking an image to a size BY HAND in the dashboard). Returns the detail."""
    pid = _id(product_id)
    STATE["read_only"] = True
    try:
        if not b:
            print("STOP: no backend bound to the demo store")
            return None
        if not STATE["verified"] and not verify_store():
            return None
        d = get(pid)
        if not d:
            print("probe: GET /products/%s answered nothing readable" % pid)
            return None
        print("product #%s keys: %s" % (pid, sorted(d)))
        print("status %r, images %s" % (_status(d), len(mapping.detail_images(d))))
        for i in mapping.detail_images(d):
            print("  image, raw:", _short(i, 400))
        options = [o for o in d.get("options") or [] if isinstance(o, dict)]
        for o in options:
            print("option #%s %r display_type %r type %r, keys %s" % (
                _id(o.get("id")), _name(o.get("name")), o.get("display_type", "<absent>"),
                o.get("type", "<absent>"), sorted(o)))
            for v in o.get("values") or []:
                if isinstance(v, dict):
                    print("  value, raw:", v)
        for s in d.get("skus") or []:
            if isinstance(s, dict):
                print("sku #%s %r related_option_values %s, keys %s" % (
                    _id(s.get("id")), s.get("sku"), s.get("related_option_values"), sorted(s)))
        tr = get(pid, params={"with": "translations"})
        for o in tr.get("options") or []:
            if isinstance(o, dict):
                print("?with=translations option #%s keys %s" % (_id(o.get("id")), sorted(o)))
                for v in o.get("values") or []:
                    if isinstance(v, dict):
                        print("  value, raw:", v)
        for o in options:
            oid = _id(o.get("id"))
            ok, od, st = call("GET", "/products/options/%s" % oid, lang="ar")
            print("GET /products/options/%s: %s" % (
                oid, _short(od, 1500) if ok else _err(ok, od, st)))
            for v in o.get("values") or []:
                vid = _id(v) if isinstance(v, dict) else ""
                if vid:
                    ok, vd, st = call("GET", "/products/options/values/%s" % vid, lang="ar")
                    print("GET /products/options/values/%s: %s" % (
                        vid, _short(vd, 1500) if ok else _err(ok, vd, st)))
        return d
    finally:
        STATE["read_only"] = False


FINDINGS = run_spike() if RUN_SPIKE else None
