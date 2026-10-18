import logging

from markupsafe import Markup
from psycopg2 import errors as pg_errors

from odoo import api, fields, models, _
from odoo.tools import html_escape

from . import mapping

_logger = logging.getLogger(__name__)

# Postgres names an _sql_constraints entry "<table>_<key>". Only a violation of THIS
# constraint means "the webhook/cron race already imported it"; anything else is a bug.
SALLA_ORDER_CONSTRAINT = "sale_order_salla_order_uniq"


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
        string="Status Alerted in Chatter", readonly=True, copy=False,
        help="Last cancellation-type status announced in the chatter. Keeps the notice "
             "idempotent when Salla redelivers or reorders webhooks.",
    )
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

    @api.model
    def _salla_partner(self, backend, customer):
        """F2a customer: found or created ONLY by salla_customer_id, name + country SA.
        Phone/email/address dedup is F2b. Archived contacts are reused rather than
        duplicated. No customer id -> the shared guest contact (never one per order)."""
        if not customer.get("ref"):
            return self.env.ref("salla_connector.partner_salla_guest")
        Partner = self.env["res.partner"].with_context(active_test=False)
        country = self.env.ref("base.sa", raise_if_not_found=False)
        partner = Partner.search([("salla_customer_id", "=", customer["ref"])],
                                 order="active desc, id", limit=1)
        if partner:
            # Only fill blanks: whatever ops typed wins over the API.
            if country and not partner.country_id:
                partner.country_id = country
            return partner
        # ponytail: no phone/email on purpose in F2a. whatsapp_sale/website automations
        # send off partner_id.phone on every confirmation (Trendyol §11); F2b adds the
        # phone together with the dedup and the WhatsApp review.
        return Partner.create({
            "name": customer.get("name") or _("Salla customer %s") % customer["ref"],
            "type": "contact",
            "customer_rank": 1,
            "company_id": backend.company_id.id,
            "country_id": country.id if country else False,
            "salla_customer_id": customer["ref"],
        })

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
    def _create_from_salla(self, backend, order, plan, products):
        """Create the sale.order for one normalized Salla order. Returns the order, or
        False when the webhook/cron race already created it (caller re-reads it).

        `plan` = mapping.build_lines(); `products` = {sku: product.product} covering
        every SKU (the caller skipped the order otherwise)."""
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
                partner = self._salla_partner(backend, order["customer"])
                vals.update(partner_id=partner.id, partner_invoice_id=partner.id,
                            partner_shipping_id=partner.id)
                so = self.create(vals)
        except pg_errors.UniqueViolation as e:
            if e.diag.constraint_name == SALLA_ORDER_CONSTRAINT:
                _logger.info("Salla order %s: imported concurrently by another worker",
                             order["id"])
                return False
            raise
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
                    "s": plan["vat_rate"], "o": odoo_vat, "ot": self.amount_total,
                    "st": plan["salla_total"]})
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
                        "diff": adjust_diff, "total": plan["salla_total"]},
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
                "odoo": self.amount_total, "salla": plan["salla_total"], "diff": diff}
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
    def _salla_alert(self, body):
        """Internal note that also lands in the notify users' Discuss inbox. A note, not
        a comment: from F2b the customer is a follower with an email, and none of these
        notices is meant for them."""
        self.ensure_one()
        partners = self.salla_backend_id.notify_user_ids.partner_id
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
                self._salla_alert(_(
                    "Salla: status \"%(s)s\" with payment method \"%(m)s\": not confirmed "
                    "until paid. The order was left as a quotation; confirm it by hand once "
                    "the payment is verified.") % {
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
                            "(%s -> %s)", self.salla_order_id, self.name, total_before,
                            self.amount_total)
            self._salla_alert(_(
                "Salla: the total changed during confirmation (%(a)s -> %(b)s), most likely "
                "an automatic promotion added a line. The customer paid %(c)s on Salla: "
                "remove the extra line.") % {"a": total_before, "b": self.amount_total,
                                              "c": self.salla_total})
        return True

    def _salla_apply_status(self, status):
        """Status of an order we already have (queue or reconcile cron). Only forward:
        a confirmable status retries a confirmation that never happened (only when the
        status actually changed, or a failing confirm would post every 30 min), and a
        cancellation is announced once and handed to a human."""
        self.ensure_one()
        previous = self.salla_status
        if status and status != previous:
            self.salla_status = status
        if mapping.is_cancel_status(status):
            if self.salla_alerted_status != status:
                self.salla_alerted_status = status
                self._salla_alert(_(
                    "Salla: the order is now \"%s\". It needs manual handling in Odoo "
                    "(cancel it, or process the return).") % status)
            return
        if status and status != previous and self.state in ("draft", "sent") \
                and mapping.should_confirm(status, self.salla_payment_method) \
                and self.salla_backend_id.auto_confirm \
                and not self.salla_total_mismatch:
            self._salla_confirm()


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    salla_line_id = fields.Char(
        string="Salla Item ID", readonly=True, copy=False,
        help="Salla's order item id (F3 needs it to push per-item updates). Empty on "
             "shipping/COD/discount lines.",
    )
