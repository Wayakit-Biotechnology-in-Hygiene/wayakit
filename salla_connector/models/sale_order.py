import logging
import threading
from datetime import timedelta

from markupsafe import Markup
from psycopg2 import errors as pg_errors

from odoo import api, fields, models, SUPERUSER_ID, _
from odoo.exceptions import AccessError, UserError
from odoo.tools import html_escape

from . import mapping

_logger = logging.getLogger(__name__)

# Postgres names an _sql_constraints entry "<table>_<key>". Only a violation of THIS
# constraint means "the webhook/cron race already imported it"; anything else is a bug.
SALLA_ORDER_CONSTRAINT = "sale_order_salla_order_uniq"
PUSH_BATCH = 30               # orders per push cron run; it re-triggers itself when full
# Safety net for a validation whose push was never marked (the picking hook only logs):
# the cron re-checks orders with a delivery done this recently and nothing pushed.
PUSH_SWEEP_WINDOW = timedelta(days=2)
# F5: orders confirmed by hand (mismatch fixed, bank transfer checked) are invoiced by the
# invoice cron when confirmed this recently. Bounded so a first run never invoices old
# orders in bulk (each posted invoice goes to ZATCA and cannot be undone).
INVOICE_SWEEP_WINDOW = timedelta(days=14)


class SaleOrder(models.Model):
    _inherit = "sale.order"

    salla_backend_id = fields.Many2one("salla.backend", string="Salla Backend",
                                       readonly=True, copy=False)
    # No index=True: the UNIQUE constraint below already creates the index.
    salla_order_id = fields.Char(string="Salla Order ID", readonly=True, copy=False)
    salla_reference = fields.Char(string="Salla Order #", readonly=True, copy=False)
    salla_status = fields.Char(string="Salla Status", readonly=True, copy=False)
    salla_payment_method = fields.Char(
        string="Salla Payment Method", readonly=True, copy=False,
        help="Decides whether \"under review\" may be confirmed (cash on delivery only).")
    salla_pushed_status = fields.Char(
        string="Status Pushed to Salla", readonly=True, copy=False,
        help="Last status Odoo successfully notified to Salla (F3). Remember what was "
             "sent, never compare with the previous value: webhooks arrive repeated and "
             "out of order.",
    )
    salla_alerted_status = fields.Char(
        string="Cancellation Handled", readonly=True, copy=False,
        help="First cancellation-type status from Salla that Odoo acted on (cancelled the "
             "order, or alerted Ops). Once set, later cancel/refund/return statuses do "
             "nothing: Salla redelivers and reorders webhooks.",
    )
    # --- F3: status push Odoo -> Salla. Set inside the picking validation, sent by the
    # push cron in its own transaction: Salla failing never touches the validation.
    salla_push_pending = fields.Char(
        string="Salla Status to Push", readonly=True, copy=False,
        help="Status waiting to be sent to Salla (\"shipped\" once every delivery is "
             "done). Empty = nothing to send.")
    salla_push_attempts = fields.Integer(string="Salla Push Attempts", readonly=True,
                                         copy=False)
    salla_push_next_try = fields.Datetime(
        string="Salla Push Next Try", readonly=True, copy=False,
        help="UTC. Empty while pending = retries exhausted: use Push Status to Salla.")
    salla_push_error = fields.Char(string="Salla Push Error", readonly=True, copy=False)
    salla_push_alerted = fields.Boolean(
        string="Salla Push Failure Alerted", readonly=True, copy=False,
        help="Ops already got the activity for this failure; reset on the next attempt "
             "cycle (a new validation or the button).")
    salla_invoice_failed = fields.Boolean(
        string="Salla Auto-Invoice Failed", readonly=True, copy=False,
        help="Odoo could not create the invoice automatically and Accounting was alerted. "
             "The invoice cron no longer retries it: invoice the order by hand (it is "
             "then uploaded to Salla as usual).")
    salla_total = fields.Monetary(string="Salla Total", readonly=True, copy=False,
                                  currency_field="currency_id")
    salla_total_mismatch = fields.Boolean(
        string="Salla Total Mismatch", readonly=True, copy=False,
        help="Odoo's total (VAT included) differs from what the customer paid on Salla "
             "beyond rounding. The order is left as a quotation: fix the lines, then "
             "confirm by hand.",
    )

    _sql_constraints = [
        ("salla_order_uniq", "unique(salla_order_id)",
         "This Salla order is already imported."),
    ]

    # ------------------------------------------------------------- import
    @api.model
    def _salla_find(self, salla_order_id):
        if not salla_order_id:
            return self.browse()
        return self.with_context(active_test=False).search(
            [("salla_order_id", "=", salla_order_id)], limit=1)

    @api.model
    def _salla_match_products(self, backend, skus):
        """{default_code: product.product}. Archived products match too (a paid order is
        never dropped for that) but an active one wins; only products the KSA company
        can sell (company_id False or the backend's)."""
        skus = [s for s in set(skus) if s]
        if not skus:
            return {}
        Product = self.env["product.product"].with_context(active_test=False)
        found = {}
        for product in Product.search([
                ("default_code", "in", skus),
                ("company_id", "in", [False, backend.company_id.id])],
                order="active desc, id"):
            found.setdefault(product.default_code, product)
        return found

    # ------------------------------------------------------------- customer (F2b)
    @api.model
    def _salla_usable_phone(self, phone):
        """`phone` (already a KSA mobile per mapping.e164_ksa) if Odoo's own validator,
        the one WhatsApp calls inside action_confirm, accepts it too; else "". A number
        that is not written cannot break the confirmation (Trendyol §11)."""
        if not phone:
            return ""
        try:
            from odoo.addons.phone_validation.tools import phone_validation
        except ImportError:
            return phone
        try:
            formatted = phone_validation.phone_format(phone, "SA", 966, force_format="E164",
                                                      raise_exception=True)
        except Exception:
            return ""
        return formatted if formatted == phone else ""

    @api.model
    def _salla_match_ids(self, backend, phone="", email=""):
        """Contact ids (archived included, company 1 or none) whose phone or mobile is
        `phone`, or whose email is `email`. SQL on purpose: contacts Ops typed by hand
        hold "050 123 4567" or "+966 50..." and an exact domain misses them, while
        phone_mobile_search only exists with the sms module and misses the "0" form.
        Candidates by the last 9 digits, then confirmed in Python with the same
        normalizer. Email: exact after trim + lower, no ilike wildcards."""
        Partner = self.env["res.partner"]
        # A contact created earlier in this same cron run must be found.
        Partner.flush_model(["phone", "mobile", "email", "company_id"])
        company = backend.company_id.id
        if phone:
            tail = "%" + phone[-9:]
            self.env.cr.execute("""
                SELECT id, phone, mobile FROM res_partner
                 WHERE (company_id IS NULL OR company_id = %s)
                   AND (regexp_replace(coalesce(phone, ''), '[^0-9]', '', 'g') LIKE %s
                        OR regexp_replace(coalesce(mobile, ''), '[^0-9]', '', 'g') LIKE %s)
            """, (company, tail, tail))
            return [pid for pid, p1, p2 in self.env.cr.fetchall()
                    if phone in (mapping.e164_ksa(p1), mapping.e164_ksa(p2))]
        if email:
            self.env.cr.execute("""
                SELECT id FROM res_partner
                 WHERE (company_id IS NULL OR company_id = %s) AND lower(trim(email)) = %s
            """, (company, email))
            return [r[0] for r in self.env.cr.fetchall()]
        return []

    @api.model
    def _salla_internal_partners(self, partners):
        """The subset of `partners` that are Wayakit's own: partner of an internal user
        (archived users too), a company's partner or any contact under it. Never a Salla
        buyer (mapping.pick_partner)."""
        if not partners:
            return partners
        users = self.env["res.users"].sudo().with_context(active_test=False).search(
            [("partner_id", "in", partners.ids), ("share", "=", False)])
        # Every company partner, not only those in `partners`: a contact under the
        # company (an employee without a user, sharing the office phone) is ours too.
        company_partners = self.env["res.company"].sudo().search([]).partner_id
        return users.partner_id | partners.filtered(
            lambda p: p.commercial_partner_id in company_partners)

    @api.model
    def _salla_pick(self, ids, ref, guest, what, notes):
        """mapping.pick_partner over `ids` (the guest never counts). Ambiguity -> note,
        without the phone/email itself."""
        Partner = self.env["res.partner"].with_context(active_test=False)
        partners = Partner.browse(ids).exists() - guest
        internal = self._salla_internal_partners(partners)
        chosen, eligible = mapping.pick_partner([{
            "id": p.id, "salla_customer_id": p.salla_customer_id, "active": p.active,
            "type": p.type, "parent_id": p.parent_id.id, "internal": p in internal}
            for p in partners], ref)
        partner = Partner.browse(chosen) if chosen else Partner
        if eligible > 1:
            notes.append(("note", _(
                "Salla: %(n)s contacts match this customer's %(what)s; the order uses "
                "%(name)s (id %(id)s). If they are the same person, merge them.") % {
                    "n": eligible, "what": what, "name": partner.display_name,
                    "id": partner.id}))
        return partner

    @api.model
    def _salla_partner(self, backend, buyer):
        """Customer and delivery address of a Salla order. -> (partner, shipping, notes)

        `buyer` = mapping.buyer(order). `notes` = [(kind, text)] for the order's chatter
        once it exists ("alert" pings the notify users, "note" does not).

        Dedup: salla_customer_id, then the E.164 mobile (phone or mobile field), then
        the email; archived contacts too, only company 1 or none, never the shared
        guest and never a contact linked to ANOTHER Salla id (mapping.pick_partner).
        A found contact only gets EMPTY fields filled: Ops data always wins, and the
        phone is written only when both phone and mobile are empty (otherwise the
        contact would carry two inconsistent numbers). Country is always SA, whatever
        the payload says: the fiscal position, hence the VAT, hence the total, hang on
        it. Nothing usable (no id, phone or email) -> the shared guest, never written.

        Address: like website_sale. A new contact, or one without any address, gets the
        order's address on itself. A contact whose address is a different one keeps it
        and the order ships to a delivery child (reused when the same street/city comes
        again), country SA too because sale.order computes the fiscal position from
        partner_shipping_id."""
        notes = []
        Partner = self.env["res.partner"].with_context(active_test=False)
        guest = self.env.ref("salla_connector.partner_salla_guest", raise_if_not_found=False) \
            or Partner
        country = self.env.ref("base.sa", raise_if_not_found=False)
        has_code_field = "x_national_short_code" in Partner._fields
        ref = buyer.get("ref") or ""
        phone = self._salla_usable_phone(buyer.get("phone"))
        email = buyer.get("email") or ""
        addr = buyer.get("address") or {}
        if buyer.get("phone_state") == "invalid" or (buyer.get("phone") and not phone):
            notes.append(("note", _(
                "Salla: the customer's phone number is not a valid Saudi mobile, so it was "
                "not saved on the contact (WhatsApp confirmations need one). Add it by hand "
                "if needed.")))
        if buyer.get("address_missing"):
            notes.append(("alert", _(
                "Salla did not send the shipping address for this order (imported without "
                "its webhook): add it by hand before shipping.")))
        code = addr.get("short_code") if has_code_field else ""
        if addr.get("short_code_state") == "invalid":
            notes.append(("alert", _(
                "Salla: the National Address short code \"%s\" is not valid (4 letters + "
                "4 digits, e.g. RRRD2929), so it was not saved. Check it with the customer "
                "before shipping.") % (addr.get("short_code_raw") or "")[:32]))

        partner = Partner
        if ref:
            found = Partner.search([("salla_customer_id", "=", ref), ("id", "!=", guest.id),
                                    ("company_id", "in", [False, backend.company_id.id])],
                                   order="active desc, id")
            # Someone typing a Salla id on an employee/company contact must not make it
            # the buyer either.
            found -= self._salla_internal_partners(found)
            partner = found[:1]
            if len(found) > 1:
                notes.append(("note", _(
                    "Salla: %(n)s contacts carry Salla customer id %(ref)s; the order uses "
                    "%(name)s (id %(id)s). Merge them.") % {
                        "n": len(found), "ref": ref, "name": partner.display_name,
                        "id": partner.id}))
        if not partner and phone:
            partner = self._salla_pick(self._salla_match_ids(backend, phone=phone), ref,
                                       guest, _("mobile number"), notes)
        if not partner and email:
            partner = self._salla_pick(self._salla_match_ids(backend, email=email), ref,
                                       guest, _("email"), notes)
        if not partner and not (ref or phone or email):
            return guest, self._salla_delivery(guest, addr, code, country), notes

        addr_vals = {k: addr.get(k) or False for k in ("street", "street2", "city", "zip")}
        if not partner:
            vals = {
                "name": buyer.get("name") or (_("Salla customer %s") % ref if ref
                                              else _("Salla customer")),
                "type": "contact",
                "customer_rank": 1,
                "company_id": backend.company_id.id,
                "country_id": country.id if country else False,
                "salla_customer_id": ref or False,
                "email": email or False,
                # Same number on both: template 7 reads phone, the others read mobile.
                "phone": phone or False,
                "mobile": phone or False,
            }
            vals.update(addr_vals if addr else {"city": buyer.get("city") or False})
            if code:
                vals["x_national_short_code"] = code
            return Partner.create(vals), Partner.browse(), notes

        incoming = {"country_id": country.id if country else False,
                    "salla_customer_id": ref or False, "email": email or False}
        current = {"country_id": partner.country_id.id,
                   "salla_customer_id": partner.salla_customer_id, "email": partner.email}
        if phone and not partner.phone and not partner.mobile:
            incoming.update(phone=phone, mobile=phone)
        shipping = Partner.browse()
        own = {"street": partner.street, "street2": partner.street2, "city": partner.city,
               "zip": partner.zip}
        if not addr:
            incoming["city"] = buyer.get("city") or False
            current["city"] = partner.city
        elif mapping.address_fillable(own, addr):
            incoming.update(addr_vals)
            current.update(own)
            if code:
                incoming["x_national_short_code"] = code
                current["x_national_short_code"] = partner.x_national_short_code
        else:
            shipping = self._salla_delivery(partner, addr, code, country)
        vals = mapping.fill_empty(current, incoming)
        if vals:
            partner.write(vals)
        return partner, shipping, notes

    @api.model
    def _salla_delivery(self, partner, addr, code, country):
        """Delivery child of `partner` for `addr` (empty recordset when there is no
        address): an existing child with the same street/city is reused and only gets
        empty fields filled, else one is created."""
        Partner = self.env["res.partner"].with_context(active_test=False)
        if not addr:
            return Partner.browse()
        vals = {k: addr.get(k) or False for k in ("street", "street2", "city", "zip")}
        vals["country_id"] = country.id if country else False
        if code:
            vals["x_national_short_code"] = code
        # Active children only: an address Ops archived was wrong or obsolete.
        for child in self.env["res.partner"].search([("parent_id", "=", partner.id),
                                                     ("type", "=", "delivery")], order="id"):
            if mapping.same_address({"street": child.street, "city": child.city}, addr):
                current = {k: child[k] for k in vals if k != "country_id"}
                current["country_id"] = child.country_id.id
                fill = mapping.fill_empty(current, vals)
                if fill:
                    child.write(fill)
                return child
        vals.update(type="delivery", parent_id=partner.id, name=addr.get("name") or False,
                    company_id=partner.company_id.id)
        return Partner.create(vals)

    def _salla_line_vals(self, product, name, qty, price_unit, line_id=False, delivery=False):
        vals = {
            "product_id": product.id,
            "name": name,
            "product_uom_qty": qty,
            "price_unit": price_unit,
            # Explicit 0: Odoo 17 computes discount from the pricelist, which would
            # discount Salla's already-final price a second time.
            "discount": 0.0,
            "salla_line_id": line_id or False,
        }
        if delivery and "is_delivery" in self.env["sale.order.line"]._fields:
            vals["is_delivery"] = True
        return vals

    @api.model
    def _create_from_salla(self, backend, order, plan, products, buyer=None):
        """Create the sale.order for one normalized Salla order. Returns the order, or
        False when the webhook/cron race already created it (caller re-reads it).

        `plan` = mapping.build_lines(); `products` = {sku: product.product} covering
        every SKU (the caller skipped the order otherwise); `buyer` = mapping.buyer() of
        the raw order (None: id and name only, as in F2a)."""
        extra_products = {"shipping": backend.shipping_product_id,
                          "cod": backend.cod_product_id,
                          "discount": backend.discount_product_id}
        lines = [self._salla_line_vals(products[p["sku"]], products[p["sku"]].display_name,
                                       p["quantity"], p["price_unit"], p["line_id"])
                 for p in plan["products"]]
        for extra in plan["extras"]:
            product = extra_products[extra["kind"]]
            label = extra["name"] if extra["kind"] != "discount" else \
                _("Discount: %s") % extra["name"]
            lines.append(self._salla_line_vals(product, label, 1, extra["price_unit"],
                                               delivery=extra["kind"] == "shipping"))
        vals = {
            "company_id": backend.company_id.id,
            "warehouse_id": backend.warehouse_id.id,
            "website_id": backend.website_id.id or False,
            # Explicit: the partner's default pricelist could be in another currency or
            # carry discounts. Prices come from Salla, the pricelist only fixes SAR.
            "pricelist_id": backend.pricelist_id.id,
            # Explicit even when empty, so the default never stamps whoever clicked.
            "user_id": backend.salesperson_id.id or False,
            "origin": "Salla %s" % order["reference"],
            "client_order_ref": order["reference"],
            "date_order": order["created_at"] or fields.Datetime.now(),
            "salla_backend_id": backend.id,
            "salla_order_id": order["id"],
            "salla_reference": order["reference"],
            "salla_status": order["status"] or False,
            "salla_payment_method": order.get("payment_method") or False,
            "salla_total": order["total"] or 0.0,
            "order_line": [(0, 0, l) for l in lines],
            # website_sale mails "abandoned cart" reminders for website quotations; a
            # Salla order left as a quotation (mismatch, unknown status) must never get one.
            "cart_recovery_email_sent": True,
        }
        # Studio field: lives in the DB, not in git. Write it only where it exists.
        if "x_studio_channel" in self._fields:
            vals["x_studio_channel"] = "B2C"
        try:
            # Partner and order in ONE savepoint: a race loser must not leave behind a
            # duplicate contact for the customer.
            with self.env.cr.savepoint():
                # Cheap pre-check; the unique constraint is the real guard.
                if self._salla_find(order["id"]):
                    return False
                partner, shipping, partner_notes = self._salla_partner(
                    backend, buyer or dict(order["customer"], phone="", email="",
                                           phone_state="missing", address={}))
                vals.update(partner_id=partner.id, partner_invoice_id=partner.id,
                            partner_shipping_id=(shipping or partner).id)
                so = self.create(vals)
        except pg_errors.UniqueViolation as e:
            if e.diag.constraint_name == SALLA_ORDER_CONSTRAINT:
                _logger.info("Salla order %s: imported concurrently by another worker",
                             order["id"])
                return False
            raise
        for kind, body in partner_notes:
            if kind == "alert":
                so._salla_alert(body)
            else:
                so.message_post(body=body, subtype_xmlid="mail.mt_note")
        if plan.get("cod_not_charged"):
            # Not an alert: the total reconciled, nothing to fix. Visible for the audit
            # of why a COD order carries no COD line.
            _logger.info("Salla order %s: COD fee %s not in Salla's total, no COD line",
                         order["id"], mapping.fmt_money(plan["cod_not_charged"]))
            so.message_post(body=_(
                "Salla: the order carries a cash-on-delivery fee of %s, but Salla's total "
                "does not include it, so no COD line was added. Check with the courier "
                "whether the fee is collected on delivery.") % mapping.fmt_money(
                    plan["cod_not_charged"]),
                subtype_xmlid="mail.mt_note")
        so._salla_reconcile_total(plan)
        # Before the confirmation on purpose: confirming reserves stock, and then every
        # order would read as short of its own quantity (Trendyol Phase 5).
        so._salla_notify_new_order(plan)
        so._salla_confirm_if_paid()
        return so

    # ------------------------------------------------------------- totals
    def _salla_reconcile_total(self, plan):
        """Golden rule: Odoo total incl. VAT == Salla total.

        Policy (F2a): exact -> nothing. Within rounding tolerance (mapping.total_tolerance:
        per-line rounding, or Salla sending a net already rounded to 2 decimals) -> one or
        two "rounding adjustment" lines plus an internal note, so the invoice matches what
        the customer paid. Beyond it ->
        the order is KEPT (never lose a paid order) but flagged, not confirmed, with a
        chatter note and a WARNING: confirming would create a delivery and later a ZATCA
        invoice for the wrong amount, while a quotation costs one click to fix."""
        self.ensure_one()
        odoo_vat = plan.get("odoo_vat_rate", plan["vat_rate"])
        verdict, diff = mapping.total_check(self.amount_total, plan["salla_total"],
                                            plan["tolerance"])
        if abs(plan["vat_rate"] - odoo_vat) > 0.001:
            # Salla charged another VAT rate than Odoo's tax applies: no rounding line can
            # (or should) paper over that. A human decides.
            self.salla_total_mismatch = True
            _logger.warning("Salla order %s (%s): Salla VAT %s%% vs Odoo %s%%, left as "
                            "quotation", self.salla_order_id, self.name, plan["vat_rate"],
                            odoo_vat)
            self._salla_alert(_(
                "Salla: the order carries VAT %(s)s%% but Odoo applies %(o)s%% (Odoo total "
                "%(ot)s vs Salla total %(st)s). The order was NOT confirmed: check the tax "
                "settings of the Salla store and correct the order before confirming it.") % {
                    "s": plan["vat_rate"], "o": odoo_vat,
                    "ot": mapping.fmt_money(self.amount_total),
                    "st": mapping.fmt_money(plan["salla_total"])})
            return False
        if verdict == "adjust":
            adjust_diff = diff
            # Sized with Odoo's rate: the rounding line is taxed by Odoo's product tax.
            nets = mapping.adjustment_nets(diff, odoo_vat)
            if nets:
                product = self.salla_backend_id.discount_product_id
                self.write({"order_line": [(0, 0, self._salla_line_vals(
                    product, _("Rounding adjustment (Salla total)"), 1, net)) for net in nets]})
                verdict, diff = mapping.total_check(self.amount_total, plan["salla_total"], 0)
            if verdict == "ok":
                _logger.info("Salla order %s: %s rounding adjustment line(s) added",
                             self.salla_order_id, len(nets))
                # Internal note, no ping: visible on the order for whoever audits it.
                self.message_post(body=_(
                    "Salla: rounding adjustment of %(diff)s added so the total equals what "
                    "the customer paid on Salla (%(total)s).") % {
                        "diff": mapping.fmt_money(adjust_diff),
                        "total": mapping.fmt_money(plan["salla_total"])},
                    subtype_xmlid="mail.mt_note")
                return True
            verdict = "mismatch"
        if verdict == "ok":
            return True
        self.salla_total_mismatch = True
        if verdict == "unknown":
            reason = _("Salla's total could not be read from the order")
        else:
            reason = _("Odoo total %(odoo)s vs Salla total %(salla)s (difference %(diff)s)") % {
                "odoo": mapping.fmt_money(self.amount_total),
                "salla": mapping.fmt_money(plan["salla_total"]),
                "diff": mapping.fmt_money(diff)}
        _logger.warning("Salla order %s (%s): total does not match, left as quotation. %s. "
                        "Extras read as %s.", self.salla_order_id, self.name, reason,
                        plan["extras_basis"])
        self._salla_alert(_(
            "Salla: the total does not match — %(reason)s. Shipping/COD/discount amounts "
            "were read as %(basis)s. The order was NOT confirmed: correct the lines so the "
            "total equals what the customer paid, then confirm it by hand.") % {
                "reason": reason, "basis": plan["extras_basis"]})
        return False

    # ------------------------------------------------------------- notices
    def _salla_alert(self, body, extra_partners=None):
        """Internal note that also lands in the notify users' Discuss inbox. A note, not
        a comment: from F2b the customer is a follower with an email, and none of these
        notices is meant for them. `extra_partners`: also pinged (F3 Ops alert users)."""
        self.ensure_one()
        partners = self.salla_backend_id.notify_user_ids.partner_id | (
            extra_partners or self.env["res.partner"])
        self.message_post(body=body, partner_ids=partners.ids, subtype_xmlid="mail.mt_note")

    def _salla_notify_new_order(self, plan):
        """Ordered vs available per product, flagging what must be manufactured (same
        reason as Trendyol Phase 5: KSA often sits at zero free stock and a bare "new
        order" ping hides that). Idempotent by construction: runs once per creation,
        and creation happens once per salla_order_id."""
        self.ensure_one()
        backend = self.salla_backend_id
        partners = backend.notify_user_ids.partner_id
        if not partners:
            _logger.warning("Salla order %s: no Notify on New Order users on backend %s, "
                            "nobody was notified", self.salla_reference, backend.name)
            return False
        warehouse = backend.warehouse_id
        stock = []
        for line in self.order_line.filtered(
                lambda l: not l.display_type and l.product_id and l.salla_line_id):
            product = line.product_id.with_context(warehouse=warehouse.id) if warehouse \
                else line.product_id
            stock.append((line.product_id.display_name, line.product_uom_qty,
                          product.free_qty, line.product_id.type == "product"))
        rows, short = mapping.stock_rows(stock)
        # %g: UoM conversion leaves 25.000000000000004, which reads as broken.
        items = "".join("<li>%s — %g ordered, %g available%s</li>" % (
            html_escape(r["name"]), r["ordered"], r["available"],
            " <b>&#9888; must be manufactured</b>" if r["short"] else "") for r in rows)
        body = Markup("<p>New Salla order <b>%s</b> (%s)%s.</p><ul>%s</ul>%s" % (
            html_escape(self.salla_reference or ""), html_escape(self.name),
            " — <b>stock is short</b>" if short else "", items,
            "<p>Manufacturing has to produce the missing quantity before the delivery "
            "can be picked and shipped.</p>" if short else ""))
        # Subscribed too, so later notices on this order reach them.
        self.message_subscribe(partner_ids=partners.ids)
        self._salla_alert(body)
        return True

    # ------------------------------------------------------------- confirmation
    def _salla_confirm_if_paid(self):
        self.ensure_one()
        status = self.salla_status
        if not self.salla_backend_id.auto_confirm or self.salla_total_mismatch:
            return False
        if not mapping.should_confirm(status, self.salla_payment_method):
            if status in mapping.REVIEW_STATUSES:
                # Only a bank transfer lands here: its receipt is not checked by Salla.
                self._salla_alert(_(
                    "Salla: status \"%(s)s\" with payment method \"%(m)s\" (bank transfer): "
                    "not confirmed until the transfer is verified. The order was left as a "
                    "quotation; confirm it by hand once the payment is checked.") % {
                        "s": status, "m": self.salla_payment_method or _("unknown")})
            elif mapping.should_import(status):
                # Unknown/empty status: a custom status, or the field misread. Visible.
                self._salla_alert(_(
                    "Salla: status \"%s\" is not known as paid, the order was left as a "
                    "quotation. Confirm it by hand once checked.") % (status or "?"))
            return False
        return self._salla_confirm()

    def _salla_confirm(self):
        """Confirm, but never lose the order: action_confirm() runs in a savepoint, so a
        failure in any confirm hook (automation rule, WhatsApp template, stock rule,
        loyalty) rolls back the WHOLE confirmation and the order stays a quotation with
        a note. The state is checked afterwards, not the absence of an exception: Odoo
        writes state='sale' early and fails late (Trendyol §11).
        mail_notify_force_send=False: mails generated inside the savepoint go to the
        queue instead of a post-commit send of records the rollback may have removed."""
        self.ensure_one()
        if self.state not in ("draft", "sent"):
            return self.state == "sale"
        total_before = self.amount_total
        try:
            with self.env.cr.savepoint():
                self.with_context(mail_notify_force_send=False).action_confirm()
        except Exception as e:
            _logger.exception("Salla order %s (%s): confirmation failed, left as quotation",
                              self.salla_order_id, self.name)
            self._salla_alert(_(
                "Salla: automatic confirmation failed, the order was left as a quotation "
                "(nothing was lost). Confirm it by hand. Error: %s") % str(e)[:300])
            return False
        if self.state != "sale":
            self._salla_alert(_("Salla: confirmation did not move the order to Sales Order "
                                "(state %s). Check it by hand.") % self.state)
            return False
        # sale_loyalty's action_confirm applies automatic promotions, which can add a
        # reward line AFTER our total check.
        if mapping.total_check(self.amount_total, total_before, 0)[0] != "ok":
            self.salla_total_mismatch = True
            _logger.warning("Salla order %s (%s): total changed during confirmation "
                            "(%s -> %s)", self.salla_order_id, self.name,
                            mapping.fmt_money(total_before), mapping.fmt_money(self.amount_total))
            self._salla_alert(_(
                "Salla: the total changed during confirmation (%(a)s -> %(b)s), most likely "
                "an automatic promotion added a line. The customer paid %(c)s on Salla: "
                "remove the extra line.") % {"a": mapping.fmt_money(total_before),
                                              "b": mapping.fmt_money(self.amount_total),
                                              "c": mapping.fmt_money(self.salla_total)})
            return True
        # F5: right after the confirmation, never inside it.
        self._salla_auto_invoice_safe()
        return True

    def _salla_auto_invoice_safe(self):
        """_salla_auto_invoice in a savepoint with a catch-all: it runs inside the import
        (anything escaping would roll back the paid order) and in the cron sweep. A crash
        marks the order salla_invoice_failed (no endless retries) and leaves a note."""
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                return self._salla_auto_invoice()
        except Exception as e:
            _logger.warning("Salla order %s (%s): automatic invoicing crashed (%s)",
                            self.salla_order_id, self.name, type(e).__name__, exc_info=True)
            self.salla_invoice_failed = True
            self.message_post(body=_(
                "Salla: automatic invoicing failed unexpectedly (%s). The order is confirmed; "
                "invoice it by hand.") % type(e).__name__, subtype_xmlid="mail.mt_note")
            return "failed"

    def _salla_apply_status(self, status):
        """Status of an order we already have (queue, reconcile or push cron). Only
        forward: a confirmable status retries a confirmation that never happened (only
        when the status actually changed, or a failing confirm would post every 30 min),
        and a cancellation is handled once (_salla_handle_cancel). delivered/completed
        only refresh salla_status: the order is never locked (Ops, 2026-10-21)."""
        self.ensure_one()
        previous = self.salla_status
        if status and status != previous:
            self.salla_status = status
        if mapping.is_cancel_status(status):
            self._salla_handle_cancel(status)
            return
        if status and status != previous and self.state in ("draft", "sent") \
                and mapping.should_confirm(status, self.salla_payment_method) \
                and self.salla_backend_id.auto_confirm \
                and not self.salla_total_mismatch:
            self._salla_confirm()

    # ------------------------------------------------------------- F3: cancellation
    def _salla_out_pickings(self):
        """The order's deliveries to the customer: outgoing type, returns excluded (a
        return of a WH/OUT is not a shipment, whatever its picking type)."""
        self.ensure_one()
        has_return = "return_id" in self.env["stock.picking"]._fields
        return self.sudo().picking_ids.filtered(
            lambda p: p.picking_type_code == "outgoing"
            and not (has_return and p.return_id)
            and not p.move_ids.origin_returned_move_id)

    def _salla_handle_cancel(self, status):
        """Salla cancelled / refunded / returned the order (Ops, 2026-10-21): nothing
        shipped -> cancel the order and its open deliveries here too, after a full credit
        note when the invoice is posted (F5, backend auto-invoicing on); shipped (or the
        cancel / credit note fails) -> change nothing, Ops gets an activity. Once per
        order (salla_alerted_status): "canceled" then "restored", or the same webhook
        three times, act once. Refunds are made by hand in the payment gateway."""
        self.ensure_one()
        to_credit, drafted = self._salla_open_invoices()
        action = mapping.cancel_action(
            self.state, self._salla_out_pickings().mapped("state"), bool(to_credit),
            already_handled=mapping.is_cancel_status(self.salla_alerted_status),
            auto_credit_note=self.salla_backend_id.auto_invoice and not drafted)
        if action == "none":
            if not self.salla_alerted_status:
                self.salla_alerted_status = status
            return action
        self.salla_alerted_status = status
        if action == "credit_note":
            refunds = self._salla_credit_note(to_credit, status)
            if not refunds:
                return "failed"
            return action if self._salla_cancel_now(status, credited=refunds) else "failed"
        if action == "cancel":
            return action if self._salla_cancel_now(status) else "failed"
        if action == "alert_shipped":
            self._salla_ops_alert(_("Salla: cancelled after shipping"), _(
                "Salla: the order is now \"%s\" but it was already shipped from Odoo. "
                "Nothing was changed in Odoo: handle the return with the courier (and the "
                "refund in the payment gateway).") % status)
        else:
            self._salla_ops_alert(_("Salla: cancelled, invoice posted"), _(
                "Salla: the order is now \"%s\". Nothing was shipped, but it has a posted "
                "invoice, so Odoo did not cancel it: cancel it by hand with a credit "
                "note. Any refund is made in the payment gateway.") % status)
        return action

    def _salla_cancel_now(self, status, credited=None):
        """Cancel the order and its open deliveries (F3). -> True when cancelled.
        `credited` = the credit notes just posted (F5), named in the note."""
        self.ensure_one()
        # Stock picked/packed by an internal step stays where it was put: a cancel
        # only cancels the moves that are not done yet.
        picked = self.sudo().picking_ids.filtered(
            lambda p: p.state == "done" and p.picking_type_code != "outgoing")
        try:
            with self.env.cr.savepoint():
                # _action_cancel, not action_cancel: the latter can return a wizard
                # (draft invoices) or refuse a locked order, neither of which a cron
                # can answer. It cancels the open pickings and draft invoices; posted
                # invoices and credit notes are left as they are.
                self.with_context(mail_notify_force_send=False)._action_cancel()
                if self.state != "cancel":
                    raise UserError(_("the order is still in state %s") % self.state)
        except Exception as e:
            _logger.warning("Salla order %s (%s): automatic cancellation failed",
                            self.salla_order_id, self.name, exc_info=True)
            self._salla_ops_alert(_("Salla: automatic cancellation failed"), _(
                "Salla: the order is now \"%(s)s\" and nothing was shipped, but Odoo "
                "could not cancel it automatically (%(e)s). Cancel the order and its "
                "delivery by hand. Any refund is made in the payment gateway.") % {
                    "s": status, "e": str(e)[:300]})
            return False
        _logger.info("Salla order %s (%s): cancelled in Odoo after Salla status %s",
                     self.salla_order_id, self.name, status)
        extra = ""
        if credited:
            extra += _(" Credit note %s posted for the whole invoice.") % ", ".join(
                credited.mapped("name"))
        if picked:
            extra += _(" Already picked/packed (%s): put that stock back.") % \
                ", ".join(picked.mapped("name"))
        if "mrp_production_count" in self._fields and self.sudo().mrp_production_count:
            extra += _(" Check the related manufacturing orders.")
        self._salla_alert(_(
            "Salla: the order is now \"%s\" and nothing was shipped, so Odoo cancelled "
            "it and its open deliveries. Any refund is made in the payment gateway.")
            % status + extra)
        return True

    def _salla_ops_alert(self, summary, body):
        """The chatter alert (notify users) plus a To-Do activity for each Ops alert
        user (notify users when none is set). One user who cannot be assigned (no
        access to the order) must not cost the others their activity, nor the caller
        its transaction."""
        self.ensure_one()
        backend = self.salla_backend_id
        ops = backend.ops_alert_user_ids
        # Ops partners are pinged in the chatter too: an inbox notification reaches them
        # even if their activity cannot be created.
        self._salla_alert(body, extra_partners=ops.partner_id)
        failed = self.env["res.users"]
        if ops:
            failed = self._salla_schedule_activities(ops, summary, body)
            if failed == ops and backend.notify_user_ids:
                # Nobody on the Ops side got the To-Do: it must not vanish.
                self._salla_schedule_activities(backend.notify_user_ids, summary, body)
        elif backend.notify_user_ids:
            self._salla_schedule_activities(backend.notify_user_ids, summary, body)
        else:
            _logger.warning("Salla order %s: no Ops Alerts / Notify users on backend %s, "
                            "no activity created: %s", self.name, backend.name, summary)
        if failed:
            self.message_post(body=_(
                "Salla: the activity could not be assigned to %s. Check their access on "
                "the Salla backend's Ops Alerts.") % ", ".join(failed.mapped("name")),
                subtype_xmlid="mail.mt_note")

    def _salla_schedule_activities(self, users, summary, body, record=None):
        """A To-Do per user on `record` (default: the order), each in its own savepoint:
        one user who cannot be assigned must not cost the others theirs, nor the caller
        its transaction. -> users that failed."""
        record = record or self
        failed = self.env["res.users"]
        for user in users:
            try:
                with self.env.cr.savepoint():
                    record.sudo().activity_schedule(
                        "mail.mail_activity_data_todo", user_id=user.id,
                        summary=summary, note=body)
            except Exception:
                failed |= user
                _logger.warning("Salla order %s: activity for user %s not created",
                                self.name, user.id, exc_info=True)
        return failed

    # ------------------------------------------------------------- F5: invoices
    def _salla_accounting_alert(self, summary, body, record=None):
        """Accounting's version of _salla_ops_alert: a note on `record` (the invoice, or
        the order by default) pinging the Accounting Alerts users and a To-Do for each
        (Notify on New Order users when none is set, or when none could be assigned).
        Also a plain note on the order when the record is an invoice, so whoever looks
        at the order sees it. Never raises past a savepoint for the activities."""
        self.ensure_one()
        record = (record or self).sudo()
        backend = self.salla_backend_id
        users = backend.accounting_alert_user_ids or backend.notify_user_ids
        try:
            # The alert must not cost the caller its transaction (it often runs inside
            # an order import); the activities below still try.
            with self.env.cr.savepoint():
                record.message_post(body=body, partner_ids=users.partner_id.ids,
                                    subtype_xmlid="mail.mt_note")
                if record._name != self._name:
                    self.message_post(body=body, subtype_xmlid="mail.mt_note")
        except Exception:
            _logger.warning("Salla order %s: accounting alert note not posted: %s",
                            self.name, summary, exc_info=True)
        if not users:
            _logger.warning("Salla order %s: no Accounting Alerts / Notify users on backend "
                            "%s, no activity created: %s", self.name, backend.name, summary)
            return
        failed = self._salla_schedule_activities(users, summary, body, record=record)
        if failed == users and backend.accounting_alert_user_ids and backend.notify_user_ids:
            self._salla_schedule_activities(backend.notify_user_ids, summary, body,
                                            record=record)
        if failed:
            record.message_post(body=_(
                "Salla: the activity could not be assigned to %s. Check their access on "
                "the Salla backend's Accounting Alerts.") % ", ".join(failed.mapped("name")),
                subtype_xmlid="mail.mt_note")

    def _salla_open_invoices(self):
        """Posted customer invoices of the order that no POSTED credit note reverses yet:
        what a cancellation must credit. -> (invoices, drafted): `drafted` = those of
        them Accounting already has a draft credit note for (the cancel then only
        alerts: _action_cancel would cancel that draft, and a second note must not be
        made)."""
        self.ensure_one()
        Move = self.env["account.move"].sudo()
        invoices = self.sudo().invoice_ids.filtered(
            lambda m: m.move_type == "out_invoice" and m.state == "posted")
        if not invoices:
            return invoices, Move
        refunds = Move.search([("reversed_entry_id", "in", invoices.ids),
                               ("move_type", "=", "out_refund"), ("state", "!=", "cancel")])
        open_ = invoices - refunds.filtered(lambda m: m.state == "posted").reversed_entry_id
        return open_, open_ & refunds.filtered(lambda m: m.state == "draft").reversed_entry_id

    def _salla_auto_invoice(self):
        """Invoice a confirmed Salla order (proposal C): create the invoice in the
        backend's ZATCA journal, check it against what the customer paid, post it and
        register the payment. -> "posted" | "draft" | "failed" | "skip"

        Each step in its own savepoint, after the confirmation: a failure never undoes
        the confirmation, it leaves the order (or a draft invoice) for Accounting with an
        alert. Posting raises when ZATCA's checks fail (account_edi _post): the invoice is
        then kept as a draft. Never emails the invoice: the customer gets it on Salla."""
        self.ensure_one()
        backend = self.salla_backend_id
        has_invoice = bool(self.sudo().invoice_ids.filtered(lambda m: m.state != "cancel"))
        if not mapping.should_auto_invoice(self.state, self.salla_total_mismatch,
                                           self.invoice_status, has_invoice,
                                           backend.auto_invoice, self.salla_invoice_failed):
            if mapping.should_auto_invoice(self.state, self.salla_total_mismatch,
                                           "to invoice", has_invoice, backend.auto_invoice,
                                           self.salla_invoice_failed):
                # Everything allows it but Odoo sees nothing to invoice: a product
                # invoiced on delivered quantities. Said once, never silent.
                self.salla_invoice_failed = True
                self._salla_accounting_alert(_("Salla: nothing to invoice"), _(
                    "Salla: the order is confirmed and paid on Salla, but Odoo has nothing "
                    "to invoice yet (invoice status \"%s\": a product is probably invoiced "
                    "on delivered quantities). Invoice it by hand; it is then uploaded to "
                    "Salla.") % self.invoice_status)
            return "skip"
        journal = backend.invoice_journal_id
        problem = backend._invoice_config_problem()
        invoice = self.env["account.move"]
        if not problem:
            try:
                with self.env.cr.savepoint():
                    if self.journal_id != journal:
                        self.journal_id = journal
                    moves = self.with_context(mail_notify_force_send=False)._create_invoices()
                    if len(moves) != 1 or moves.move_type != "out_invoice":
                        raise UserError(_("expected one invoice, got %s") % len(moves))
                    invoice = moves
            except Exception as e:
                _logger.warning("Salla order %s (%s): invoice not created",
                                self.salla_order_id, self.name, exc_info=True)
                problem = str(e)[:300]
        if problem:
            self.salla_invoice_failed = True
            self._salla_accounting_alert(_("Salla: invoice not created"), _(
                "Salla: Odoo could not create the invoice of this order (%s). Nothing was "
                "posted. Create the invoice by hand: once posted it is uploaded to Salla "
                "as usual.") % problem)
            return "failed"
        verdict, diff = mapping.total_check(invoice.amount_total, self.salla_total, 0)
        if verdict != "ok":
            _logger.warning("Salla order %s (%s): invoice %s total %s vs Salla %s, left draft",
                            self.salla_order_id, self.name, invoice.id,
                            mapping.fmt_money(invoice.amount_total),
                            mapping.fmt_money(self.salla_total))
            self._salla_accounting_alert(_("Salla: invoice left in draft"), _(
                "Salla: the invoice total %(i)s differs from what the customer paid on "
                "Salla (%(s)s), so it was NOT posted. Correct it and post it by hand (it "
                "is then uploaded to Salla).") % {
                    "i": mapping.fmt_money(invoice.amount_total),
                    "s": mapping.fmt_money(self.salla_total)}, record=invoice)
            return "draft"
        try:
            with self.env.cr.savepoint():
                invoice.with_context(mail_notify_force_send=False).action_post()
                if invoice.state != "posted":
                    raise UserError(_("the invoice is still in state %s") % invoice.state)
        except Exception as e:
            _logger.warning("Salla order %s (%s): invoice %s not posted",
                            self.salla_order_id, self.name, invoice.id, exc_info=True)
            self._salla_accounting_alert(_("Salla: invoice not posted"), _(
                "Salla: the invoice was created but could not be posted, it stays in "
                "draft. Fix the cause and post it by hand (it is then uploaded to Salla "
                "and its payment registered). Error: %s") % str(e)[:300], record=invoice)
            return "draft"
        _logger.info("Salla order %s (%s): invoice %s posted", self.salla_order_id,
                     self.name, invoice.name)
        try:
            # Own savepoint: a payment problem must never take the posted invoice back.
            with self.env.cr.savepoint():
                invoice._salla_register_payment()
        except Exception:
            _logger.warning("Salla order %s (%s): payment registration crashed; the cron "
                            "retries it", self.salla_order_id, self.name, exc_info=True)
        return "posted"

    @api.model
    def _salla_invoice_sweep(self, now, limit):
        """Invoice the Salla orders confirmed outside the import (by hand after a total
        mismatch or a transfer check, or a confirmation retried by a status webhook that
        failed to invoice inline). Orders that already have an invoice, failed before or
        are not to invoice never match. -> orders processed."""
        orders = self.search([
            ("salla_order_id", "!=", False), ("salla_backend_id.active", "=", True),
            ("salla_backend_id.auto_invoice", "=", True), ("state", "=", "sale"),
            ("salla_total_mismatch", "=", False), ("salla_invoice_failed", "=", False),
            ("invoice_status", "=", "to invoice"),
            ("date_order", ">=", now - INVOICE_SWEEP_WINDOW)], order="id", limit=limit)
        # date_order is the confirmation time once confirmed (sale's action_confirm).
        return orders.filtered(lambda o: o.date_order >= (
            o.salla_backend_id.invoice_from or now) and not o.invoice_ids.filtered(
            lambda m: m.state != "cancel"))

    def _salla_credit_note(self, invoices, status):
        """Full credit note of each of `invoices` (posted, not yet reversed), posted.
        Through Odoo's reversal wizard, as Accounting would do it: it carries the
        reason that ZATCA requires on a Saudi credit note (l10n_sa_edi refuses a refund
        without reason/origin) and keeps the sale order link. All or nothing in one
        savepoint. -> the credit notes, or an empty recordset after alerting (Ops and
        Accounting: the order is then NOT cancelled, nothing must ship)."""
        self.ensure_one()
        Move = self.env["account.move"].sudo()
        refunds = Move
        reason = _("Order %(ref)s cancelled on Salla (%(s)s)") % {
            "ref": self.salla_reference or self.name, "s": status}
        try:
            with self.env.cr.savepoint():
                for invoice in invoices.sudo():
                    wizard = self.env["account.move.reversal"].sudo().with_context(
                        active_model="account.move", active_ids=invoice.ids).create({
                            "reason": reason,
                            "journal_id": invoice.journal_id.id,
                        })
                    wizard.reverse_moves()
                    refund = wizard.new_move_ids
                    if len(refund) != 1:
                        raise UserError(_("the reversal produced %s moves") % len(refund))
                    refund.with_context(mail_notify_force_send=False).action_post()
                    if refund.state != "posted":
                        raise UserError(_("credit note still in state %s") % refund.state)
                    refunds |= refund
        except Exception as e:
            _logger.warning("Salla order %s (%s): credit note failed", self.salla_order_id,
                            self.name, exc_info=True)
            body = _(
                "Salla: the order is now \"%(s)s\" and nothing was shipped, but Odoo could "
                "not create the credit note of its posted invoice (%(e)s). The order was NOT "
                "cancelled: do not ship it. Accounting: issue the credit note by hand, then "
                "cancel the order. Any refund is made in the payment gateway.") % {
                    "s": status, "e": str(e)[:300]}
            self._salla_ops_alert(_("Salla: cancelled, credit note failed"), body)
            self._salla_accounting_alert(_("Salla: credit note failed"), body)
            return Move
        _logger.info("Salla order %s (%s): credit note(s) %s posted after Salla status %s",
                     self.salla_order_id, self.name, refunds.mapped("name"), status)
        methods = self.salla_payment_method or _("unknown")
        self._salla_accounting_alert(_("Salla: refund to make by hand"), _(
            "Salla: the order was cancelled on Salla before shipping. Credit note %(cn)s "
            "was posted for the whole invoice and goes to Salla once ZATCA accepts it. "
            "The refund itself must be made by hand in the payment gateway that collected "
            "it (payment method \"%(m)s\"), then registered on the credit note.") % {
                "cn": ", ".join(refunds.mapped("name")), "m": methods}, record=refunds[:1])
        return refunds

    # ------------------------------------------------------------- F3: push status
    def _salla_ship_decision(self):
        self.ensure_one()
        return mapping.ship_push_decision(self.state, self.salla_status,
                                          self.salla_pushed_status,
                                          self._salla_out_pickings().mapped("state"))

    def _salla_mark_push(self, trigger=True):
        """Called from the picking validation/cancellation (sudo). Only marks the order
        and wakes the push cron: no API call here, the validation must never wait on or
        fail because of Salla (golden rule 5)."""
        for order in self.filtered(lambda o: o.salla_order_id and o.salla_backend_id):
            if order._salla_ship_decision() != "push":
                continue
            order.write({"salla_push_pending": mapping.SHIPPED_STATUS,
                         "salla_push_attempts": 0,
                         "salla_push_next_try": fields.Datetime.now(),
                         "salla_push_error": False, "salla_push_alerted": False})
            if trigger:
                self.env.ref("salla_connector.ir_cron_salla_push_status").sudo()._trigger()

    def _salla_push_now(self, manual=False):
        """Send salla_push_pending to Salla for ONE order. -> (outcome, detail)
        outcome: "pushed" | "wait" | "skip" | "failed". `manual`: the button; a failure
        is reported to whoever clicked instead of retried and alerted.

        The order is re-read from Salla first: a cancellation whose webhook was lost (or
        is still in the queue) must not be overwritten by "shipped", which could revive
        it. The push fields are written only after the POST. A fresh status that differs
        is applied before it (salla_status written, row locked during the POST): a
        status webhook the queue cron processes meanwhile then fails on this row and is
        retried ~25 min later, which is harmless.
        Not serialised with the button: both may POST "shipped" (two SMS with
        send_status_sms on). Accepted: it needs a click inside the cron's seconds."""
        self.ensure_one()
        backend = self.salla_backend_id
        target = self.salla_push_pending or mapping.SHIPPED_STATUS
        clear = {"salla_push_pending": False, "salla_push_attempts": 0,
                 "salla_push_next_try": False, "salla_push_error": False}
        try:
            with self.env.cr.savepoint():
                raw = backend._fetch_order(self.salla_order_id)
                fresh = mapping.order_status(raw) if raw else ""
                if fresh and fresh != self.salla_status:
                    self._salla_apply_status(fresh)
                decision = self._salla_ship_decision()
                if decision != "push":
                    self.write(clear)
                    _logger.info("Salla order %s (%s): no status push (%s, Salla status %s)",
                                 self.salla_order_id, self.name, decision, self.salla_status)
                    return decision, self.salla_status or ""
                backend._request("POST", "/orders/%s/status" % self.salla_order_id,
                                 payload=mapping.status_payload(target,
                                                                backend.send_status_sms))
                self.write(dict(clear, salla_pushed_status=target))
                self.message_post(body=_(
                    "Salla: order set to \"%s\" in Salla (every delivery validated).")
                    % target, subtype_xmlid="mail.mt_note")
        except Exception as e:
            return self._salla_push_failed(e, target=target, manual=manual)
        _logger.info("Salla order %s (%s): status %s pushed", self.salla_order_id,
                     self.name, target)
        return "pushed", target

    def _salla_push_failed(self, error, target=None, manual=False):
        """Count the failure, schedule the retry, alert Ops once when retries run out.
        salla_push_pending stays set: the button (or a new validation) can retry.
        Manual: no automatic retry and no activity, the person who clicked sees it."""
        self.ensure_one()
        http_status = getattr(error, "http_status", None)
        attempts = mapping.PUSH_MAX_ATTEMPTS if manual else self.salla_push_attempts + 1
        next_try = None if manual else mapping.push_next_try(
            attempts, fields.Datetime.now(), mapping.is_permanent_error(http_status))
        # UserErrors from _request carry Salla's error body (truncated there), never a
        # token. Anything else is a bug: its type is enough here, the traceback is logged.
        detail = (error.args[0] if isinstance(error, UserError) and error.args
                  else type(error).__name__)
        detail = str(detail)[:300]
        _logger.warning("Salla order %s (%s): status push failed (attempt %s, %s): %s",
                        self.salla_order_id, self.name, attempts,
                        "retry %s UTC" % next_try if next_try else "giving up", detail,
                        exc_info=not isinstance(error, UserError))
        self.write({"salla_push_pending": target or self.salla_push_pending
                    or mapping.SHIPPED_STATUS,
                    "salla_push_attempts": attempts, "salla_push_next_try": next_try or False,
                    "salla_push_error": detail})
        if next_try is None and not manual and not self.salla_push_alerted:
            self.salla_push_alerted = True
            self._salla_ops_alert(_("Salla: status push failed"), _(
                "Salla: Odoo could not set the order to \"%(s)s\" in Salla after %(n)s "
                "attempt(s). The delivery in Odoo stays validated. Set the status in the "
                "Salla dashboard by hand, or fix the cause and click \"Push Status to "
                "Salla\". Error: %(e)s") % {
                    "s": self.salla_push_pending or mapping.SHIPPED_STATUS, "n": attempts,
                    "e": detail})
        return "failed", detail

    @api.model
    def cron_salla_push_status(self, limit=PUSH_BATCH):
        """Push the pending statuses that are due. One transaction per order: a push
        Salla accepted stays recorded whatever happens to the next order, and a
        SerializationFailure (the queue cron writing the same order for the status
        webhook our push caused) costs only that order a retry, its pending intact."""
        now = fields.Datetime.now()
        testing = getattr(threading.current_thread(), "testing", False)
        self._salla_sweep_unmarked(now, limit)
        if not testing:
            # Kept apart from the per-order rollbacks below.
            self.env.cr.commit()
        orders = self.search([("salla_push_pending", "!=", False),
                              ("salla_push_next_try", "!=", False),
                              ("salla_push_next_try", "<=", now),
                              ("salla_push_attempts", "<", mapping.PUSH_MAX_ATTEMPTS),
                              ("salla_backend_id.active", "=", True)],
                             order="salla_push_next_try, id", limit=limit)
        counters = {}
        for order_id in orders.ids:
            order = self.browse(order_id)
            try:
                outcome = order._salla_push_now()[0]
                if not testing:
                    self.env.cr.commit()
            except Exception:
                # Bookkeeping itself failed (e.g. SerializationFailure): nothing of this
                # order is kept, it is still pending and due, so the next run retries.
                _logger.warning("Salla order %s: status push bookkeeping failed, retried "
                                "next run", order_id, exc_info=True)
                if testing:
                    raise
                self.env.cr.rollback()
                outcome = "rolled_back"
            counters[outcome] = counters.get(outcome, 0) + 1
        if len(orders) == limit:
            self.env.ref("salla_connector.ir_cron_salla_push_status").sudo()._trigger()
        if counters:
            _logger.info("Salla status push: %s", counters)
        return counters

    @api.model
    def _salla_sweep_unmarked(self, now, limit):
        """Mark orders a picking hook failed to mark: a delivery done in the last
        PUSH_SWEEP_WINDOW, nothing pending, nothing pushed. The domain is coarse (any
        picking matches); _salla_mark_push applies the real rule, and once the push
        re-reads the order from Salla a stale "skip" stops matching the local decision."""
        candidates = self.search([
            ("salla_order_id", "!=", False), ("salla_backend_id.active", "=", True),
            ("state", "=", "sale"), ("salla_push_pending", "=", False),
            ("salla_pushed_status", "=", False),
            ("picking_ids.picking_type_code", "=", "outgoing"),
            ("picking_ids.date_done", ">=", now - PUSH_SWEEP_WINDOW)],
            # Newest first: orders that legitimately wait (open backorder) match every
            # run and must not starve a fresh one out of the batch.
            order="id desc", limit=limit)
        for order in candidates:
            try:
                with self.env.cr.savepoint():
                    order._salla_mark_push(trigger=False)
            except Exception:
                _logger.warning("Salla order %s: push sweep could not mark it", order.name,
                                exc_info=True)

    def action_salla_push_status(self):
        """Manual retry for Ops: reset the attempts and push now, reporting the outcome.
        Same path as the cron (fresh GET, same decision), run as OdooBot like the other
        Salla buttons; rights are checked here since any method is callable over RPC."""
        self.ensure_one()
        if not self.env.user.has_group("sales_team.group_sale_salesman"):
            raise AccessError(_("Only the sales team can push a status to Salla."))
        self.check_access_rights("write")
        self.check_access_rule("write")
        if not (self.salla_order_id and self.salla_backend_id):
            raise UserError(_("This is not a Salla order."))
        # No write before the call: the order row would stay locked during the API call
        # and the status webhook our POST causes would collide with it.
        outcome, detail = self.with_user(SUPERUSER_ID)._salla_push_now(manual=True)
        messages = {
            "pushed": (_("Salla status set to \"%s\".") % detail, False),
            "wait": (_("Not pushed: not every delivery of this order is validated yet "
                       "(or none is)."), True),
            "skip": (_("Nothing to push: already sent, or Salla is already at \"%s\".")
                     % (detail or "?"), False),
            "failed": (_("Salla refused or did not answer: %s") % detail, True),
        }
        message, warning = messages[outcome]
        return self.salla_backend_id._notify(message, warning=warning)


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    salla_line_id = fields.Char(
        string="Salla Item ID", readonly=True, copy=False,
        help="Salla's order item id (F3 needs it to push per-item updates). Empty on "
             "shipping/COD/discount lines.",
    )
