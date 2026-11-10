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
# Round 2 (after the 2026-11-10 results): store languages, ?with=translations, a
# `translations` body experiment, status after each write, and an embedded WebP (Pillow
# on odoo.sh cannot write WebP).
#
# Round 3 (2026-11-10): the demo store did NOT have English enabled during the first run,
# so "product EN not shown" and "option/value EN overwrote the Arabic" may come from that.
# GET /languages now runs FIRST and says whether `en` is present and active (banner if
# not). The option/value EN experiment is back, on THIS spike product only and only when
# English is active: lang en PUT {"name"} (never "values"), read back ar / en /
# ?with=translations, and the Arabic names are ALWAYS put back if they changed.
#
# Copy only the FINDINGS block into the ticket (ids, statuses, HTTP codes, Salla error
# messages). No tokens, no customer data.

import base64
import inspect
import io
import re
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
EN_NAME_TR = "F8 spike translations-field name %s" % STAMP   # step 4b, told apart from 4
AR_DESC = "<p>وصف تجريبي للاختبار فقط.</p>"
EN_DESC = "<p>Test description, spike only.</p>"
OPTION_AR = "الحجم"
VALUES_AR = ["صغير", "كبير"]
OPTION_EN = "Size"          # step 5, lang en, spike product only
VALUE_EN = "Small"
# Step 5 runs only when GET /languages shows `en` active. If /languages cannot tell (HTTP
# error, no status field) and Settings > Languages in the dashboard shows English ON, set
# this to True and paste again (a new spike product is created each run).
FORCE_EN_EXPERIMENTS = False
EN_BANNER = "ENGLISH NOT ACTIVE IN THIS STORE: English results below are not meaningful"
# Complete 64x64 lossy WebP (110 bytes, a white square on blue), made with Pillow 10.2 and
# checked with Pillow + libwebp dwebp: no Pillow WebP support is needed to upload it.
# (The base64 handed over on 2026-11-10 decodes to 95 of the 286 bytes its RIFF header
# declares: the header reads 64x64 but no decoder opens it, so it is not used here.)
WEBP_64_B64 = ("UklGRmYAAABXRUJQVlA4IFoAAADQBACdASpAAEAAPrFInUmnJCKhMegA4BYJQBpGIAaADeAA"
               "P7BD0SV+jyHoTKG4AAD+4Ll+q8QZmA/ys1sxthqyRlG2r//9zfv7fOA6l6N7S38Vw7NxDQAA"
               "AAA=")

b = env["salla.backend"].search([("store_id", "=", DEMO_STORE_ID)], limit=1)
STATE = {"verified": False, "en_active": None, "en_why": "GET /languages not run"}
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
        # An option PUT never carries "values": that is what could rewrite them.
        if isinstance(body, dict) and "values" in body and \
                path.startswith("/products/options/") and method == "PUT":
            raise RuntimeError("REFUSED: the spike never sends values in an option PUT")
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
            # Salla requires weight on create: without it, 422 fields.weight (demo 2026-11-10).
            "weight": 0.5, "weight_type": "kg",
            "options": [{"name": OPTION_AR, "display_type": "text",
                         "values": [{"name": v} for v in VALUES_AR]}]}
    F["1 create needs weight"] = "yes: 422 fields.weight without it (demo 2026-11-10)"
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
    # The wizard hides again right after the stock writes (_keep_hidden): same here, so
    # the next steps show whether THEY flip it too.
    hide(pid, "2")


def hide(pid, n):
    ok, d, st = call("POST", "/products/%s/status" % pid, payload={"status": "hidden"})
    F["%s POST status hidden" % n] = "ok" if ok else "HTTP %s: %s" % (st, d)
    F["%s status after the POST (GET)" % n] = _status(get(pid))


def track(pid, n, label):
    """Status after a write: does this kind of write flip a hidden product?"""
    F["%s status after %s" % (n, label)] = _status(get(pid))


def _short(value, n=60):
    value = mapping.text(value) if not isinstance(value, (dict, list)) else repr(value)
    value = re.sub(r"\s+", " ", value)
    return value if len(value) <= n else value[:n] + "..."


def _plain(html):
    return " ".join(re.sub(r"<[^>]*>", " ", mapping.text(html)).split())


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
    # The wizard re-writes the Arabic texts when English is not kept, because only the
    # name was proven to survive: settle the description too.
    F["4 AR description survived"] = "%s (%r)" % (_plain(ar.get("description")) ==
                                                  _plain(AR_DESC),
                                                  _short(ar.get("description")))
    F["4 EN description shown with lang en"] = "%s (%r)" % (
        _plain(en.get("description")) == _plain(EN_DESC), _short(en.get("description")))
    F["4 EN name shown with lang en"] = "%s (%r)" % (en.get("name") == EN_NAME, en.get("name"))
    F["4 variants kept after a PUT without options"] = "%s (%s -> %s)" % (
        len(_skus(ar)) == n_skus, n_skus, len(_skus(ar)))
    F["4 price kept after a PUT without price"] = "%s (%s -> %s)" % (
        mapping.money(ar.get("price")) == mapping.money(before.get("price")),
        mapping.money(before.get("price")), mapping.money(ar.get("price")))
    # Name OR description lost: both are put back (the description alone is not caught
    # by the wizard's name check, which is why it re-writes the Arabic texts).
    if ar.get("name") != AR_NAME or _plain(ar.get("description")) != _plain(AR_DESC):
        ok, d, st = call("PUT", "/products/%s" % pid, lang="ar",
                         payload={"name": AR_NAME, "description": AR_DESC})
        again = get(pid, "ar")
        F["4 AR restored (name, description)"] = "%s, %s (%s)" % (
            again.get("name") == AR_NAME,
            _plain(again.get("description")) == _plain(AR_DESC),
            "ok" if ok else "HTTP %s: %s" % (st, d))
    track(pid, "4", "the EN PUT")


def _option_names(d):
    """(option_id, option_name, value_id, value_name) of OUR option, from a detail."""
    for oid, oname, values in _options(d):
        if values:
            return oid, oname, values[0][0], values[0][1]
    return None, None, None, None


def _names_by_id(d, oid, vid):
    """Option and value names for these ids in a detail (None when missing)."""
    for o_id, o_name, values in _options(d):
        if o_id == oid:
            return o_name, next((n for v_id, n in values if v_id == vid), None)
    return None, None


def _option_values(d, oid):
    """[(value_id, value_name)] of the option `oid` in a detail ([] when missing)."""
    return next((values for o_id, o_name, values in _options(d) if o_id == oid), [])


def _kept(reads):
    """Finding text for '5 values kept' / '5 variants kept': True only when every read
    (before the EN PUTs, after them, after the Arabic restore) answered the same."""
    same = all(r == reads[0][1] for _, r in reads) and bool(reads[0][1])
    return "%s (%s)" % (same, "; ".join("%s %s" % (when, _short(r, 160)) for when, r in reads))


def _translated_options(d, oid, vid):
    """What ?with=translations says about our option / value (shape unknown)."""
    for o in (d or {}).get("options") or []:
        if not isinstance(o, dict) or o.get("id") != oid:
            continue
        value = next((v for v in o.get("values") or [] if isinstance(v, dict)
                      and v.get("id") == vid), {})
        return "option name %r translations %s | value name %r translations %s" % (
            _short(o.get("name")), _short(o.get("translations", "<none>"), 120),
            _short(value.get("name")), _short(value.get("translations", "<none>"), 120))
    return "option #%s not in the answer (keys %s)" % (oid, sorted(d or {})[:12])


def step_option_english(pid):
    # Experiment on THIS spike product only (created by this run): option/value EN writes,
    # re-tested with English enabled. The Arabic names are always put back if they moved.
    if not (STATE["en_active"] is True or FORCE_EN_EXPERIMENTS):
        F["5 option/value EN writes"] = "SKIPPED: English not active in this store " \
            "(%s); set FORCE_EN_EXPERIMENTS only if the dashboard shows English ON" % \
            STATE["en_why"]
        return
    if STATE["en_active"] is not True:
        F["5 forced"] = "FORCE_EN_EXPERIMENTS = True (GET /languages said: %s)" % STATE["en_why"]
    before = get(pid, "ar")
    oid, o_ar, vid, v_ar = _option_names(before)
    if not oid or not vid or o_ar != OPTION_AR or v_ar not in VALUES_AR:
        F["5 option/value EN writes"] = "SKIPPED: spike option not found as created " \
            "(option #%s %r, value #%s %r)" % (oid, o_ar, vid, v_ar)
        return
    F["5 Arabic before"] = "option #%s %r, value #%s %r" % (oid, o_ar, vid, v_ar)
    # An option PUT might replace the option's values (and so the variants): record the
    # value ids/names and the variant count before, after the EN PUTs, after the restore.
    values_seen = [("before", _option_values(before, oid))]
    variants_seen = [("before", len(_skus(before)))]
    ok, d, st = call("PUT", "/products/options/%s" % oid, lang="en",
                     payload={"name": OPTION_EN})
    F["5 PUT /products/options/<id> lang en {name}"] = "ok" if ok else \
        "HTTP %s: %s" % (st, _short(d, 200))
    ok, d, st = call("PUT", "/products/options/values/%s" % vid, lang="en",
                     payload={"name": VALUE_EN})
    F["5 PUT /products/options/values/<id> lang en {name}"] = "ok" if ok else \
        "HTTP %s: %s" % (st, _short(d, 200))
    after_en = get(pid, "ar")
    o_ar2, v_ar2 = _names_by_id(after_en, oid, vid)
    values_seen.append(("after EN PUTs", _option_values(after_en, oid)))
    variants_seen.append(("after EN PUTs", len(_skus(after_en))))
    o_en, v_en = _names_by_id(get(pid, "en"), oid, vid)
    F["5 option AR survived"] = "%s (lang ar %r)" % (o_ar2 == o_ar, o_ar2)
    F["5 option EN shown"] = "%s (lang en %r)" % (o_en == OPTION_EN, o_en)
    F["5 value AR survived"] = "%s (lang ar %r)" % (v_ar2 == v_ar, v_ar2)
    F["5 value EN shown"] = "%s (lang en %r)" % (v_en == VALUE_EN, v_en)
    for lang in ("ar", "en"):
        ok, d, st = call("GET", "/products/%s" % pid, lang=lang,
                         params={"with": "translations"})
        F["5 ?with=translations (lang %s)" % lang] = _translated_options(d, oid, vid) \
            if ok and isinstance(d, dict) else "HTTP %s: %s" % (st, _short(d, 200))
    # Restore: Arabic names back, lang ar, name only (never "values").
    restored = []
    if o_ar2 != o_ar:
        ok, d, st = call("PUT", "/products/options/%s" % oid, lang="ar",
                         payload={"name": o_ar})
        restored.append("option PUT %s" % ("ok" if ok else "HTTP %s: %s" % (st, _short(d))))
    if v_ar2 != v_ar:
        ok, d, st = call("PUT", "/products/options/values/%s" % vid, lang="ar",
                         payload={"name": v_ar})
        restored.append("value PUT %s" % ("ok" if ok else "HTTP %s: %s" % (st, _short(d))))
    after_restore = get(pid, "ar")
    values_seen.append(("after AR restore", _option_values(after_restore, oid)))
    variants_seen.append(("after AR restore", len(_skus(after_restore))))
    # Compared by value id (the names move on purpose here; "5 AR restored" covers them).
    F["5 values kept"] = _kept([(w, [v_id for v_id, n in vals])
                                for w, vals in values_seen])
    F["5 values (id, name)"] = "; ".join("%s %s" % (w, _short(vals, 160))
                                         for w, vals in values_seen)
    F["5 variants kept"] = _kept(variants_seen)
    if restored:
        o_ar3, v_ar3 = _names_by_id(after_restore, oid, vid)
        o_en3, v_en3 = _names_by_id(get(pid, "en"), oid, vid)
        F["5 AR restored"] = "option %s (%r), value %s (%r) [%s]" % (
            o_ar3 == o_ar, o_ar3, v_ar3 == v_ar, v_ar3, "; ".join(restored))
        F["5 EN after the AR restore"] = "option %r, value %r" % (o_en3, v_en3)
        if o_ar3 != o_ar or v_ar3 != v_ar:
            F["5 AR restore FAILED"] = "fix by hand in the dashboard: product #%s option " \
                "%r value %r" % (pid, o_ar, v_ar)
    else:
        F["5 AR restored"] = "not needed (Arabic names unchanged)"
    track(pid, "5", "the option/value EN PUTs")


def _is_on(value):
    """True / False / None (unknown) for a language status of unknown shape."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    value = mapping.text(value).strip().lower()
    if value in ("1", "true", "yes", "on", "active", "enabled", "published"):
        return True
    if value in ("0", "false", "no", "off", "inactive", "disabled", "hidden", "draft"):
        return False
    return None


def _english_off(why):
    STATE["en_active"], STATE["en_why"] = (False if why.startswith("not") else None), why
    print("\n%s (%s)\n" % (EN_BANNER, why))


def step_languages(pid):
    """Runs FIRST: settles whether the English results of this run mean anything."""
    ok, d, st = call("GET", "/languages")
    if not ok:
        F["9 GET /languages"] = "HTTP %s: %s" % (st, _short(d, 200))
        _english_off("unknown: GET /languages HTTP %s" % st)
        F["0 English active (GET /languages)"] = "unknown (%s)" % STATE["en_why"]
        return
    items = d if isinstance(d, list) else (d.get("languages") if isinstance(d, dict) else None)
    if not isinstance(items, list):
        F["9 GET /languages"] = "ok, unexpected shape: %s" % _short(d, 200)
        _english_off("unknown: unexpected /languages shape")
        F["0 English active (GET /languages)"] = "unknown (%s)" % STATE["en_why"]
        return
    rows, en = [], []
    for lang in items:
        if not isinstance(lang, dict):
            continue
        code = next((lang.get(k) for k in ("iso_code", "code", "iso", "locale", "lang")
                     if lang.get(k)), "?")
        default = next((lang.get(k) for k in ("is_default", "default") if k in lang), "?")
        status = next((lang.get(k) for k in ("status", "is_active", "active", "enabled",
                                             "is_enabled") if k in lang), "?")
        rows.append("%s (default %s, status %s)" % (code, default, status))
        name = mapping.text(lang.get("name")).strip().lower()
        if re.match(r"^en([-_].*)?$", mapping.text(code).strip().lower()) or name == "english":
            on = _is_on(status) if status != "?" else None
            en.append(True if _is_on(default) else on)
    F["9 GET /languages"] = "%s language(s): %s" % (len(rows), "; ".join(rows) or "-")
    if items and isinstance(items[0], dict):
        F["9 language keys"] = sorted(items[0])[:15]
    if not en:
        _english_off("not present: no 'en' in GET /languages")
    elif True in en:
        STATE["en_active"], STATE["en_why"] = True, "present and active"
        print("\nEnglish is present and active in this store.\n")
    elif False in en:
        _english_off("not active: 'en' present but disabled")
    else:
        _english_off("unknown: 'en' present, no status in the answer")
    F["0 English active (GET /languages)"] = "%s (%s)" % (
        {True: "yes", False: "NO", None: "unknown"}[STATE["en_active"]], STATE["en_why"])


def _translations(pid, lang="en"):
    ok, d, st = call("GET", "/products/%s" % pid, lang=lang, params={"with": "translations"})
    if not ok or not isinstance(d, dict):
        return None, "HTTP %s: %s" % (st, _short(d, 200))
    return d, d.get("translations", "<no 'translations' key>")


def _translation_names(tr):
    """{lang: name} from a `translations` object of unknown shape."""
    out = {}
    if isinstance(tr, dict):
        if isinstance(tr.get("name"), dict):           # field first: {name: {ar, en}}
            return {k: _short(v) for k, v in tr["name"].items()}
        for key, value in tr.items():                  # language first: {en: {name}}
            if isinstance(value, dict):
                out[key] = _short(value.get("name"))
    elif isinstance(tr, list):
        for item in tr:
            if isinstance(item, dict):
                key = next((item.get(k) for k in ("locale", "lang", "language", "iso_code",
                                                  "code") if item.get(k)), "?")
                out[key] = _short(item.get("name"))
    return out


def step_with_translations(pid):
    d, tr = _translations(pid, "en")
    if d is None:
        F["10 GET ?with=translations (lang en)"] = tr
        return
    shape = type(tr).__name__
    keys = sorted(tr)[:10] if isinstance(tr, dict) else (len(tr) if isinstance(tr, list) else "-")
    F["10 GET ?with=translations (lang en)"] = "translations is %s, keys/len %s, name per " \
        "lang %s, top-level name %r" % (shape, keys, _translation_names(tr) or _short(tr, 120),
                                         _short(d.get("name")))
    if isinstance(tr, dict):
        for key in sorted(tr)[:4]:
            value = tr[key]
            if isinstance(value, dict):
                F["10 translations[%s] keys" % key] = sorted(value)[:12]


def step_translations_body(pid):
    # Experiment on the spike product only: an undocumented `translations` field, sent
    # with the ARABIC headers. The Arabic name must stay; restored if not.
    ok, d, st = call("PUT", "/products/%s" % pid, lang="ar",
                     payload={"translations": {"en": {"name": EN_NAME_TR}}})
    F["11 PUT {translations: {en: {name}}} lang ar"] = "ok" if ok else \
        "HTTP %s: %s" % (st, _short(d, 200))
    ar = get(pid, "ar")
    kept = ar.get("name") == AR_NAME
    F["11 AR name unchanged"] = "%s (%r)" % (kept, _short(ar.get("name")))
    if not kept:
        ok2, d2, st2 = call("PUT", "/products/%s" % pid, lang="ar",
                            payload={"name": AR_NAME, "description": AR_DESC})
        F["11 AR restored"] = "%s (%s)" % (get(pid, "ar").get("name") == AR_NAME,
                                           "ok" if ok2 else "HTTP %s: %s" % (st2, d2))
    if not ok:
        return
    en = get(pid, "en")
    F["11 EN name via lang en"] = "%s (%r)" % (en.get("name") == EN_NAME_TR,
                                               _short(en.get("name")))
    d, tr = _translations(pid, "en")
    F["11 EN name via ?with=translations"] = (
        "%s (%s)" % (_translation_names(tr).get("en") == _short(EN_NAME_TR),
                     _translation_names(tr) or _short(tr, 120))) if d is not None else tr
    track(pid, "11", "the translations PUT")


def _pillow_webp():
    # Registered plugin, not features.check("webp"): Odoo calls Image.preinit(), which
    # skips the WebP plugin, so features.check (libwebp compiled in) can say True while
    # img.save(..., "WEBP") fails with KeyError 'WEBP'. Importing the plugin registers it.
    try:
        from PIL import Image, WebPImagePlugin  # noqa: F401
        return "WEBP" in Image.OPEN and "WEBP" in Image.SAVE
    except Exception as e:
        return "%s: %s" % (type(e).__name__, e)


def make_images():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (600, 600), (40, 120, 200))
    ImageDraw.Draw(img).rectangle([150, 150, 450, 450], fill=(250, 250, 250))
    out = {}
    buf = io.BytesIO()
    img.save(buf, "PNG")
    out["png"] = buf.getvalue()
    support = _pillow_webp()
    F["6 Pillow WebP support on this server"] = support
    try:
        out["webp"] = base64.b64decode(WEBP_64_B64)
        F["6 embedded webp header (mapping.image_size)"] = "%s, %s bytes" % (
            mapping.image_size(out["webp"]), len(out["webp"]))
    except Exception as e:
        out["webp_error"] = "embedded: %s: %s" % (type(e).__name__, e)
    if "webp" not in out and support is True:
        # Fallback only: on odoo.sh img.save(..., "WEBP") raised KeyError 'WEBP'
        # (2026-11-10; Odoo's Image.preinit leaves the plugin out). `support` is True
        # only when _pillow_webp() registered the plugin, so this save can work.
        try:
            buf = io.BytesIO()
            img.save(buf, "WEBP", quality=80)
            out["webp"] = buf.getvalue()
            out["webp_label"] = "Pillow 600x600"
        except Exception as e:
            out["webp_error"] += " | Pillow: %s: %s" % (type(e).__name__, e)
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


def _attach(pid, sort, filename, content, mime):
    ok, d, st = call("POST", "/products/%s/images" % pid, idempotent=False,
                     data={"sort": str(sort), "main": "1" if sort == 1 else "0",
                           "alt": "F8 spike"},
                     files={"photo": (filename, content, mime)})
    text = ("ok, response keys %s" % sorted(d)[:10]) if ok and isinstance(d, dict) else \
        ("ok" if ok else "HTTP %s: %s" % (st, _short(d, 200)))
    return ok, d, text


def step_images(pid):
    imgs = make_images()
    url = ""
    ok, d, text = _attach(pid, 1, "spike-f8-1.png", imgs["png"], "image/png")
    F["6 attach png multipart photo (%s bytes)" % len(imgs["png"])] = text
    if ok:
        url = _image_url(d)
    if "webp" not in imgs:
        F["6 attach webp (embedded 64x64)"] = "not available: %s" % imgs.get("webp_error")
    else:
        label = imgs.get("webp_label", "embedded 64x64")
        ok, d, text = _attach(pid, 2, "spike-f8-2.webp", imgs["webp"], "image/webp")
        F["6 attach webp (%s)" % label] = "%s (%s bytes)" % (text, len(imgs["webp"]))
        if ok:
            stored = _image_url(d)
            F["6 webp stored as"] = "%s (host %s)" % (
                stored.rsplit(".", 1)[-1][:10] if stored else "?",
                urlparse(stored).hostname if stored else "?")
        elif "webp_label" not in imgs:
            # Control: tells "WebP refused" from "64 px refused".
            from PIL import Image
            small = io.BytesIO()
            Image.new("RGB", (64, 64), (40, 120, 200)).save(small, "PNG")
            ok2, d2, text2 = _attach(pid, 2, "spike-f8-2.png", small.getvalue(), "image/png")
            F["6 control: 64x64 PNG after the webp failed"] = text2
    track(pid, "6", "the image uploads")
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
    hide(pid, "8")


def run_spike():
    if not b:
        print("STOP: no backend bound to the demo store")
        return F
    if not verify_store():
        return F
    pid = step_create()
    if pid:
        done = set()
        try:
            # step_languages FIRST: every English result depends on it (step 5 needs it).
            for step in (step_languages, step_variants, step_lookups, step_english,
                         step_option_english, step_with_translations,
                         step_translations_body, step_images, step_status):
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
            # A refusal (or a crash) after a step that flipped the status must not leave
            # the demo product published: hide it once more (the guard still applies).
            if STATE["verified"] and "step_status" not in done:
                try:
                    hide(pid, "12 final")
                except Exception as e:
                    F["12 final hide"] = "FAILED %s: %s: hide #%s by hand" % (
                        type(e).__name__, str(e)[:200], pid)
    print("\n===== F8 SPIKE FINDINGS (demo store %s, product #%s, parent SKU %s) =====" % (
        DEMO_STORE_ID, pid, PARENT_SKU))
    if pid and STATE["en_active"] is not True:
        print("  %s (%s)" % (EN_BANNER, STATE["en_why"]))
    seen = list(F)

    def _order(key):    # by step number ("10" after "9"), then in the order found
        head = key.split(" ", 1)[0]
        return (int(head) if head.isdigit() else 999, seen.index(key))

    for key in sorted(F, key=_order):
        print("  %s: %s" % (key, F[key]))
    print("Product #%s is left HIDDEN on the demo store; this script never deletes. "
          "Delete it by hand in the Salla dashboard if wanted." % pid)
    return F


FINDINGS = run_spike()
