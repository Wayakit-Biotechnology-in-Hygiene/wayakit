import logging
import threading
from datetime import timedelta

from odoo import api, fields, models, SUPERUSER_ID, _
from odoo.exceptions import AccessError, UserError

from . import mapping

_logger = logging.getLogger(__name__)

UPLOAD_BATCH = 30             # moves per upload cron run; it re-triggers itself when full
INVOICE_BATCH = 30            # orders invoiced / invoices paid per run, same
# A move waiting for ZATCA (or for its invoice, for a credit note) is re-checked this
# often: no API call, but it must not crowd out due uploads in the batch.
UPLOAD_WAIT_RECHECK = timedelta(minutes=10)
# Payments are only registered by the cron for invoices posted this recently: an old
# open invoice of a Salla order is Accounting's, not something to pay automatically.
PAYMENT_SWEEP_WINDOW = timedelta(days=14)


class AccountMove(models.Model):
    _inherit = "account.move"

    salla_sale_order_id = fields.Many2one(
        "sale.order", string="Salla Order", readonly=True, copy=False, index="btree_not_null",
        help="Set when an invoice / credit note of a Salla order is posted: it is then "
             "uploaded to Salla.")
    salla_upload_state = fields.Selection(
        [("pending", "Pending"), ("done", "Uploaded"), ("failed", "Failed"),
         ("skipped", "Not Uploaded")],
        string="Salla Upload", readonly=True, copy=False,
        help="Pending: waits for ZATCA acceptance (or, for a credit note, for its invoice) "
             "or for a retry. Failed: retries exhausted, use Upload Invoice to Salla. Not "
             "uploaded: nothing Salla can take (e.g. a credit note of an invoice that "
             "never reached Salla).")
    salla_invoice_id = fields.Char(
        string="Salla Invoice ID", readonly=True, copy=False,
        help="Id Salla gave the uploaded document. Set = never uploaded again: Salla "
             "accepts a second tax invoice for the same order.")
    salla_invoice_url = fields.Char(string="Salla Invoice File", readonly=True, copy=False)
    salla_upload_posted = fields.Boolean(
        string="Salla Upload Attempted", readonly=True, copy=False,
        help="Committed BEFORE the first POST to Salla. Only a move that attempted a POST "
             "may adopt an unrecorded document found on Salla (its answer was lost).")
    salla_upload_attempts = fields.Integer(string="Salla Upload Attempts", readonly=True,
                                           copy=False)
    salla_upload_next_try = fields.Datetime(string="Salla Upload Next Try", readonly=True,
                                            copy=False)
    salla_upload_error = fields.Char(string="Salla Upload Error", readonly=True, copy=False)
    salla_upload_alerted = fields.Boolean(
        string="Salla Upload Alerted", readonly=True, copy=False,
        help="Accounting already got the activity for this upload problem; reset by "
             "Upload Invoice to Salla.")
    salla_payment_alerted = fields.Boolean(
        string="Salla Payment Alerted", readonly=True, copy=False,
        help="The payment could not be registered automatically and Accounting was "
             "alerted; the cron no longer tries. Register it by hand.")

    # ------------------------------------------------------------- posting hook
    def _post(self, soft=True):
        posted = super()._post(soft=soft)
        posted._salla_mark_upload()
        return posted

    def _salla_order(self):
        """The Salla sale order of an invoice (its lines) or of a credit note (its lines,
        else the invoice it reverses)."""
        self.ensure_one()
        move = self.sudo()
        order = move.line_ids.sale_line_ids.order_id.filtered("salla_order_id")[:1]
        return order or move.reversed_entry_id.salla_sale_order_id

    def _salla_mark_upload(self, trigger=True):
        """Link posted invoices / credit notes of Salla orders and queue their upload.
        Runs inside the posting (Accounting's click, or our cron), so it never raises
        and makes no API call: the upload cron does that later. The access token the
        public PDF URL needs is created here, committed with the posting, so the
        controller can see it when Salla fetches the file."""
        marked = False
        for move in self.filtered(lambda m: m.move_type in ("out_invoice", "out_refund")):
            try:
                # Read-only lookup first: every invoice of the database passes here, a
                # savepoint is only worth it for the Salla ones.
                order = move._salla_order()
                if not order or not order.salla_backend_id:
                    continue
                with self.env.cr.savepoint():
                    vals = {"salla_sale_order_id": order.id}
                    if order.salla_backend_id.upload_invoices and not move.salla_invoice_id:
                        vals.update(salla_upload_state="pending", salla_upload_attempts=0,
                                    salla_upload_next_try=fields.Datetime.now(),
                                    salla_upload_error=False, salla_upload_alerted=False)
                        marked = True
                    move.sudo().write(vals)
                    move.sudo()._portal_ensure_token()
            except Exception:
                _logger.warning("Salla: move %s not queued for upload; use Upload Invoice to "
                                "Salla", move.id, exc_info=True)
        cron = self.env.ref("salla_connector.ir_cron_salla_invoices",
                            raise_if_not_found=False)
        if marked and trigger and cron:
            cron.sudo()._trigger()

    # ------------------------------------------------------------- payment
    def _salla_register_payment(self):
        """Register the Salla payment on a posted invoice, in the journal of the provider
        that collected it (mapping.payment_journal_key). -> "paid" | "failed" | "skip"

        Memo = the Salla reference, so Accounting can match the provider's payout
        report. An unknown method, a missing journal, or an amount that is not what
        Salla collected registers nothing and alerts Accounting once."""
        self.ensure_one()
        move = self.sudo()
        order = move.salla_sale_order_id
        if move.move_type != "out_invoice" or move.state != "posted" or not order \
                or move.payment_state != "not_paid" or move.salla_payment_alerted:
            return "skip"
        backend = order.salla_backend_id
        overrides, _bad = mapping.parse_method_overrides(backend.payment_method_overrides)
        key = mapping.payment_journal_key(order.salla_payment_method, overrides)
        journal = backend._payment_journal(key)
        problem = ""
        if not key:
            problem = _("payment method \"%s\" is not mapped to Salla Payments, Tabby or "
                        "Tamara (add it to the backend's Payment Method Mapping)") % (
                order.salla_payment_method or _("unknown"))
        elif not journal:
            problem = _("no journal set for \"%s\" on the Salla backend") % key
        elif journal.company_id != move.company_id:
            problem = _("journal %s belongs to another company") % journal.display_name
        elif not journal._get_available_payment_method_lines("inbound"):
            problem = _("journal %s has no incoming payment method") % journal.display_name
        elif mapping.total_check(move.amount_total, order.salla_total, 0)[0] != "ok":
            problem = _("the invoice total %(i)s is not what Salla collected (%(s)s)") % {
                "i": mapping.fmt_money(move.amount_total),
                "s": mapping.fmt_money(order.salla_total)}
        if not problem:
            try:
                with self.env.cr.savepoint():
                    wizard = self.env["account.payment.register"].sudo().with_context(
                        active_model="account.move", active_ids=move.ids).create({
                            "journal_id": journal.id,
                            "payment_date": move.invoice_date or fields.Date.context_today(move),
                            "communication": order.salla_reference or move.name,
                        })
                    payments = wizard._create_payments()
            except Exception as e:
                _logger.warning("Salla invoice %s: payment not registered", move.id,
                                exc_info=True)
                problem = str(e)[:300]
        if problem:
            move.salla_payment_alerted = True
            order._salla_accounting_alert(_("Salla: payment not registered"), _(
                "Salla: the payment of this invoice was not registered automatically: "
                "%s. Register it by hand in the journal of the provider that collected "
                "it.") % problem, record=move)
            return "failed"
        _logger.info("Salla invoice %s: payment %s registered in %s", move.name,
                     payments.mapped("name"), journal.name)
        return "paid"

    # ------------------------------------------------------------- upload
    def _salla_upload_origin_state(self):
        """Upload state of the invoice a credit note follows: the one it reverses, else
        the best state among the order's invoices ("done" > "pending"/"failed")."""
        self.ensure_one()
        move = self.sudo()
        if move.reversed_entry_id:
            return move.reversed_entry_id.salla_upload_state or ""
        states = set(move.salla_sale_order_id.invoice_ids.filtered(
            lambda m: m.move_type == "out_invoice").mapped("salla_upload_state"))
        for state in ("done", "pending", "failed"):
            if state in states:
                return state
        return ""

    def _salla_cancelled_before_upload(self):
        """An invoice not on Salla yet, whose order Salla cancelled and that a posted
        credit note already reverses (the F5 cancel path): nothing to give the customer."""
        self.ensure_one()
        move = self.sudo()
        # A move that attempted a POST may already be on Salla (answer lost): it goes
        # through the adopt path instead, and its credit note follows.
        if move.move_type != "out_invoice" or move.salla_invoice_id or move.salla_upload_posted \
                or not mapping.is_cancel_status(move.salla_sale_order_id.salla_status):
            return False
        return bool(self.search_count([("reversed_entry_id", "=", move.id),
                                       ("move_type", "=", "out_refund"),
                                       ("state", "=", "posted")]))

    def _salla_upload_decision(self):
        self.ensure_one()
        move = self.sudo()
        backend = move.salla_sale_order_id.salla_backend_id
        edi_error = "edi_document_ids" in move._fields and bool(
            move.edi_document_ids.filtered(lambda d: d.blocking_level == "error"))
        return mapping.upload_decision(
            move.move_type, move.state, move["edi_state"] if "edi_state" in move._fields
            else False, backend.require_zatca_before_upload, move.salla_invoice_id,
            edi_error=edi_error, origin_state=move._salla_upload_origin_state(),
            cancelled_before_upload=move._salla_cancelled_before_upload())

    def _salla_upload_now(self, commit=None):
        """Upload ONE move to Salla. -> (outcome, detail); outcome "uploaded" |
        "adopted" | "wait" | "skip" | "alert" | "failed".

        Only the upload cron calls it (the button queues and triggers the cron): the cron
        never runs twice at once, so two POSTs of the same move cannot race. A POST that
        succeeded but whose commit was lost is caught on the next attempt by listing the
        order's documents on Salla (mapping.adopt_uploaded_invoice) — only for a move
        whose salla_upload_posted marker was committed (`commit`, the cron's) before its
        first POST, so a move that never posted cannot take another one's document."""
        self.ensure_one()
        move = self.sudo()
        order = move.salla_sale_order_id
        backend = order.salla_backend_id
        now = fields.Datetime.now()
        decision = move._salla_upload_decision()
        if decision == "skip":
            move.write({"salla_upload_state": "done" if move.salla_invoice_id else "skipped",
                        "salla_upload_next_try": False})
            if not move.salla_invoice_id:
                move.message_post(body=_(
                    "Salla: %s is not uploaded to Salla: nothing the customer needs there "
                    "(order cancelled before its invoice was uploaded, or a credit note of "
                    "an invoice that never reached Salla).") % move.name,
                    subtype_xmlid="mail.mt_note")
            return "skip", ""
        if decision == "wait":
            move.salla_upload_next_try = now + UPLOAD_WAIT_RECHECK
            return "wait", ""
        if decision in ("no_zatca", "zatca_error", "origin_failed"):
            # Stays pending and re-checked: once ZATCA accepts (Accounting resends it), or
            # the invoice a credit note follows is uploaded (its button), the upload goes
            # on its own. Alerted once.
            move.salla_upload_next_try = now + timedelta(minutes=mapping.PUSH_RETRY_MINUTES[1])
            if not move.salla_upload_alerted:
                move.salla_upload_alerted = True
                reason = {
                    "no_zatca": _("its journal %s does not report to ZATCA")
                    % move.journal_id.name,
                    "zatca_error": _("ZATCA has not accepted it (see the electronic "
                                     "invoicing error on the invoice)"),
                    "origin_failed": _("the invoice it credits could not be uploaded to "
                                       "Salla (see the alert on that invoice; once it is "
                                       "uploaded, this credit note follows)"),
                }[decision]
                order._salla_accounting_alert(_("Salla: upload on hold"), _(
                    "Salla: %(n)s is not uploaded to Salla yet because %(r)s. It is "
                    "uploaded on its own once that is solved.") % {"n": move.name, "r": reason}, record=move)
            return "alert", decision
        kind = mapping.upload_kind(move.move_type)
        # No savepoint around the calls: the marker below must be COMMITTED before the
        # POST, which a savepoint would forbid. A DB error aborts the transaction and the
        # cron rolls the move back (still pending, retried next run).
        salla_id, salla_url, outcome = "", "", "adopted"
        try:
            url = mapping.invoice_pdf_url(backend._public_base_url(), move.id,
                                          move.access_token, move.name)
            if not url:
                if not move.access_token:
                    # Normally created at posting; visible to the controller (Salla's
                    # fetch runs on another cursor) only after this commit.
                    move._portal_ensure_token()
                    move.salla_upload_next_try = now
                    return "wait", "token"
                raise UserError(_("no public https base URL: set the backend's Webhook "
                                  "URL to the public https domain"))
            if move.salla_upload_posted:
                listed = backend._request("GET", "/orders/invoices",
                                          params={"order_id": order.salla_order_id})
                listed = listed.get("data") if isinstance(listed, dict) else None
                known = self.search([("salla_invoice_id", "!=", False),
                                     ("salla_sale_order_id", "=", order.id)]).mapped(
                                         "salla_invoice_id")
                salla_id = mapping.adopt_uploaded_invoice(
                    listed if isinstance(listed, list) else [], kind, known)
            if not salla_id:
                raw = backend._fetch_order(order.salla_order_id)
                target = mapping.status_before_upload(
                    kind, mapping.order_status(raw) if raw else order.salla_status)
                if target:
                    backend._request("POST", "/orders/%s/status" % order.salla_order_id,
                                     payload=mapping.status_payload(
                                         target, backend.send_status_sms))
                    order.message_post(body=_(
                        "Salla: order set to \"%s\" in Salla so the invoice can be "
                        "uploaded.") % target, subtype_xmlid="mail.mt_note")
                if not move.salla_upload_posted:
                    # Committed right before the first POST: proves, on a later attempt,
                    # that an unrecorded document on Salla may be this move's lost upload.
                    move.salla_upload_posted = True
                    if commit:
                        commit()
                # Never resent inside one call: a timeout may hide an invoice Salla
                # created; the next attempt lists the order's documents first.
                res = backend._request("POST", "/orders/invoices",
                                       payload=mapping.invoice_upload_payload(
                                           order.salla_order_id, kind, url),
                                       idempotent=False)
                salla_id = mapping.uploaded_invoice_id(res)
                if not salla_id:
                    # Accepted without a readable id: the next attempt adopts it from
                    # the listing instead of posting it again.
                    raise UserError(_("Salla answered without an invoice id"))
                data = res.get("data") if isinstance(res, dict) else None
                salla_url = mapping.text(data.get("url"))[:500] if isinstance(data, dict) \
                    else ""
                outcome = "uploaded"
        except Exception as e:
            return move._salla_upload_failed(e)
        move.write({"salla_invoice_id": salla_id, "salla_invoice_url": salla_url or False,
                    "salla_upload_state": "done", "salla_upload_attempts": 0,
                    "salla_upload_next_try": False, "salla_upload_error": False})
        body = _("Salla: %(n)s uploaded to Salla as %(k)s (Salla id %(i)s).") % {
            "n": move.name, "k": kind, "i": salla_id}
        if outcome == "adopted":
            body = _("Salla: %(n)s was already on Salla (Salla id %(i)s, an earlier upload "
                     "whose answer was lost); not uploaded again.") % {
                "n": move.name, "i": salla_id}
        move.message_post(body=body, subtype_xmlid="mail.mt_note")
        _logger.info("Salla: %s %s -> Salla invoice %s (%s)", kind, move.name, salla_id,
                     outcome)
        return outcome, salla_id

    def _salla_upload_failed(self, error):
        """Count the failure, schedule the retry (5 / 30 / 120 min, a permanent 4xx gives
        up at once), alert Accounting once when retries run out."""
        self.ensure_one()
        move = self.sudo()
        http_status = getattr(error, "http_status", None)
        attempts = move.salla_upload_attempts + 1
        next_try = mapping.push_next_try(attempts, fields.Datetime.now(),
                                         mapping.is_permanent_error(http_status))
        # UserErrors carry Salla's error body (truncated by _request), never a token.
        detail = (error.args[0] if isinstance(error, UserError) and error.args
                  else type(error).__name__)
        detail = str(detail)
        if move.access_token:
            # Salla's 422 may echo the submitted URL, which carries the token.
            detail = detail.replace(move.access_token, "***")
        detail = detail[:300]
        _logger.warning("Salla: upload of move %s failed (attempt %s, %s): %s", move.id,
                        attempts, "retry %s UTC" % next_try if next_try else "giving up",
                        detail, exc_info=not isinstance(error, UserError))
        move.write({"salla_upload_state": "pending" if next_try else "failed",
                    "salla_upload_attempts": attempts,
                    "salla_upload_next_try": next_try or False,
                    "salla_upload_error": detail})
        if not next_try and not move.salla_upload_alerted:
            move.salla_upload_alerted = True
            move.salla_sale_order_id._salla_accounting_alert(
                _("Salla: invoice upload failed"), _(
                    "Salla: %(n)s could not be uploaded to Salla after %(a)s attempt(s), so "
                    "the customer does not have it there. Fix the cause and click \"Upload "
                    "Invoice to Salla\" on it. Error: %(e)s") % {
                        "n": move.name, "a": attempts, "e": detail}, record=move)
        return "failed", detail

    # ------------------------------------------------------------- cron
    @api.model
    def cron_salla_invoices(self, limit=UPLOAD_BATCH):
        """F5 cron, one transaction per record: (1) invoice orders confirmed by hand,
        (2) register payments of Salla invoices posted by hand, (3) upload what is due.
        A commit per record keeps what Salla accepted whatever happens to the next one."""
        testing = getattr(threading.current_thread(), "testing", False)
        now = fields.Datetime.now()
        SaleOrder = self.env["sale.order"]
        counters = {}
        full = False

        def _each(records, label, fn):
            nonlocal full
            full = full or len(records) >= limit
            for rec_id in records.ids:
                rec = records.browse(rec_id)
                try:
                    outcome = fn(rec)
                    if not testing:
                        self.env.cr.commit()
                except Exception:
                    _logger.warning("Salla %s: record %s failed, retried next run", label,
                                    rec_id, exc_info=True)
                    if testing:
                        raise
                    self.env.cr.rollback()
                    outcome = "rolled_back"
                key = "%s:%s" % (label, outcome)
                counters[key] = counters.get(key, 0) + 1

        _each(SaleOrder._salla_invoice_sweep(now, limit), "invoice",
              lambda o: o._salla_auto_invoice_safe())
        to_pay = self.search([
            ("salla_sale_order_id", "!=", False), ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"), ("payment_state", "=", "not_paid"),
            ("salla_payment_alerted", "=", False),
            ("salla_sale_order_id.salla_backend_id.auto_invoice", "=", True),
            ("create_date", ">=", now - PAYMENT_SWEEP_WINDOW)], order="id", limit=limit)
        _each(to_pay, "payment", lambda m: m._salla_register_payment())
        due = self.search([
            ("salla_upload_state", "=", "pending"), ("salla_upload_next_try", "!=", False),
            ("salla_upload_next_try", "<=", now),
            ("salla_sale_order_id.salla_backend_id.active", "=", True),
            ("salla_sale_order_id.salla_backend_id.upload_invoices", "=", True)],
            order="salla_upload_next_try, id", limit=limit)
        _each(due, "upload", lambda m: m._salla_upload_now(
            commit=None if testing else self.env.cr.commit)[0])
        if full:
            self.env.ref("salla_connector.ir_cron_salla_invoices").sudo()._trigger()
        if counters:
            _logger.info("Salla invoices cron: %s", counters)
        return counters

    def action_salla_upload_invoice(self):
        """Queue (again) the upload of this invoice / credit note and wake the cron. The
        cron is the only uploader (see _salla_upload_now), so this never races it."""
        self.ensure_one()
        if not self.env.user.has_group("account.group_account_invoice"):
            raise AccessError(_("Only Accounting can upload an invoice to Salla."))
        self.check_access_rights("write")
        self.check_access_rule("write")
        move = self.sudo()
        if move.salla_invoice_id:
            raise UserError(_("Already uploaded to Salla (Salla id %s).") % move.salla_invoice_id)
        if move.state != "posted" or move.move_type not in ("out_invoice", "out_refund"):
            raise UserError(_("Only a posted customer invoice or credit note can be uploaded."))
        order = move.salla_sale_order_id or move._salla_order()
        if not order:
            raise UserError(_("This is not the invoice of a Salla order."))
        move.write({"salla_sale_order_id": order.id, "salla_upload_state": "pending",
                    "salla_upload_attempts": 0, "salla_upload_next_try": fields.Datetime.now(),
                    "salla_upload_error": False, "salla_upload_alerted": False})
        move._portal_ensure_token()
        self.env.ref("salla_connector.ir_cron_salla_invoices").sudo()._trigger()
        return order.salla_backend_id._notify(_(
            "Queued: the upload runs within a minute or two (after ZATCA acceptance when "
            "the backend requires it). Reload the invoice to see the result."))

    # ------------------------------------------------------------- PDF
    def _salla_invoice_pdf(self):
        """PDF bytes Salla downloads: the legal PDF stored by Send & Print if there is
        one, else the standard invoice report (with the ZATCA QR), never a proforma."""
        self.ensure_one()
        move = self.sudo()
        if move.invoice_pdf_report_id:
            return move.invoice_pdf_report_id.raw
        report = self.env["ir.actions.report"].sudo().with_company(move.company_id)
        return report._render_qweb_pdf("account.account_invoices", move.ids)[0]
