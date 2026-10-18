# Staging helper: create test orders in the Salla DEMO store through the API, so the
# F2a checklist doesn't need manual orders in the Salla dashboard.
#
# Run it in the odoo.sh shell of the maintest build (Shell tab -> `odoo-bin shell`),
# pasting the whole file. It goes through backend._request, so the token is never
# printed or typed. NEVER run it against the real store: it refuses any store id other
# than the demo one.
#
# Field names follow docs.salla.dev "Create Order" (POST /orders). Not validated yet
# against a real response: if Salla answers 422, the error body says which field fails.

DEMO_STORE_ID = "461067869"

b = env["salla.backend"].search([("store_id", "=", DEMO_STORE_ID)], limit=1)
assert b, "No backend for the demo store"


def show_branches():
    """Branch ids for delivery_method 'pickup' (avoids the long ship_to address)."""
    for br in (b._request("GET", "/branches") or {}).get("data", []):
        print(br.get("id"), br.get("name"), "default" if br.get("is_default") else "")


def create_order(products, payment="cod", cod_fee=0, coupon=None, branch_id=None,
                 customer_id=None):
    """products: [("FP-ACC-00004", 3), ...] by SKU.
    payment: only "cod" works through the API (Salla refuses a "paid" credit_card/mada
    order: those must go through its gateway). customer_id: an existing demo-store
    customer; a new customer is refused when its mobile is already registered.
    Note: via the API, cash_on_delivery.amount is NOT added to Salla's total (seen
    2026-10-18), so the COD-fee case must be tested with a dashboard order."""
    body = {
        "products": [{"identifier_type": "sku", "identifier": sku, "quantity": qty}
                     for sku, qty in products],
        "delivery_method": "pickup",
        "branch_id": branch_id,
    }
    if customer_id:
        body["customer"] = {"id": customer_id}
    else:
        # Obviously fake test customer, no real data.
        body["customer"] = {"name": "Salla Test", "mobile": "+966500000000",
                            "email": "salla.test@example.com"}
    if payment == "cod":
        body["payment"] = {"status": "paid", "method": "cod",
                           "cash_on_delivery": {"amount": cod_fee, "currency": "SAR"}}
    else:
        body["payment"] = {"status": "paid", "method": payment}
    if coupon:
        body["coupon_code"] = coupon
    res = b._request("POST", "/orders", payload=body)
    data = (res or {}).get("data", {}) if isinstance(res, dict) else {}
    print("created id=%s reference=%s status=%s total=%s" % (
        data.get("id"), data.get("reference_id"),
        (data.get("status") or {}).get("slug") if isinstance(data.get("status"), dict)
        else data.get("status"),
        ((data.get("amounts") or {}).get("total") or {})))
    return data


# --- Usage (one line at a time) ---------------------------------------------------
# show_branches() needs the branches.read scope, which the app doesn't have: take the
# id from the dashboard URL instead (Settings -> Branches -> edit: /branches/<id>).
# BR = <id>
# CUST = <salla customer id>      # e.g. the test customer of an earlier order
# create_order([("FP-ACC-00004", 3)], branch_id=BR, customer_id=CUST)                  # case 1: qty 3
# create_order([("FP-ACC-00004", 3)], coupon="TEST20", branch_id=BR, customer_id=CUST)  # case 2: coupon
# create_order([("FP-TEST-99999", 3)], branch_id=BR, customer_id=CUST)                 # case 7: SKU not in Odoo
# Salla also refuses a quantity above the demo product's stock: raise it in the dashboard.
#   (case 7 may be refused by Salla if the SKU doesn't exist in the demo store either:
#    use a demo-store product whose SKU is NOT in Odoo)
