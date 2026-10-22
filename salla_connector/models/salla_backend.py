import json
import logging
import time
from datetime import timedelta

import requests
from psycopg2 import errors as pg_errors

from odoo import api, fields, models, SUPERUSER_ID, _
from odoo.exceptions import AccessError, UserError, ValidationError

from . import mapping

_logger = logging.getLogger(__name__)

MAX_RETRIES = 3
REQUEST_TIMEOUT = 30
# (connect, read): requests applies each one separately, so a bare 15 meant up to
# ~30 s holding the refresh lock. 5 + 10 keeps the worst case at 15 s.
TOKEN_TIMEOUT = (5, 10)
PER_PAGE = 60                 # Salla max page size
MAX_PAGES = 200               # hard cap: 12 000 products, far above the catalog
# Refresh while the access token (14 days) still has a day left. The daily cron then
# always refreshes before expiry, which also renews the 1-month refresh token.
TOKEN_REFRESH_MARGIN = timedelta(days=1)
# Documented access-token lifetime. Assumed when Salla answers without a readable
# expiry: leaving token_expires_at empty would make EVERY _request refresh again.
DEFAULT_TOKEN_LIFETIME = timedelta(days=14)
# Waiting for another worker that holds the refresh lock: its token POST is bounded
# by TOKEN_TIMEOUT (15 s worst case), so 6 x 3 s covers it without hanging a request
# for long.
LOCK_ATTEMPTS = 6
LOCK_WAIT = 3
# The authorize/revoke write waits for the lock instead of NOWAIT, bounded per attempt;
# 2 x 12 s outlasts a refresh (15 s worst case) and stays under Salla's ~30 s webhook
# timeout. Past that the webhook answers 500 and Salla retries it.
WRITE_LOCK_TIMEOUT = "12s"
WRITE_ATTEMPTS = 2

# Secret fields are restricted IN PYTHON, not only in the view: field groups are what
# blocks a salesman reading them over RPC (read/search_read/export).
SECRET_GROUP = "base.group_system"
# Readable by the sales team, writable only by system: changing them rebinds the
# backend to another store / app, or (expiry) forces/suppresses token refreshes.
SYSTEM_WRITE_FIELDS = ("store_id", "client_id", "token_expires_at")
# Columns the dedicated token cursor may write. Column names are interpolated into the
# UPDATE from THIS tuple only, never from caller input.
TOKEN_COLUMNS = ("access_token", "refresh_token", "token_expires_at", "token_scope", "store_id")

# --- F2a orders ---
# First reconcile run (empty cursor) looks this far back.
DEFAULT_LOOKBACK_DAYS = 3
# The list filter is a calendar date in Riyadh time and Salla's clock/zone are
# unverified: re-read well before the cursor. Re-reading is free (salla_order_id unique).
WINDOW_OVERLAP = timedelta(hours=6)
MAX_ORDER_PAGES = 50          # 3 000 orders per window, far above Wayakit's volume
MAX_ITEM_PAGES = 10
QUEUE_BATCH = 30              # per queue run; the cron re-triggers itself when full
QUEUE_MAX_ATTEMPTS = 10       # errors only; a missing SKU never fails the row
# A row that did not import is retried after this, not on every trigger. Just under
# the 30 min cron interval so the interval run picks it up.
QUEUE_RETRY_AFTER = timedelta(minutes=25)
CURSOR_STALE_AFTER = timedelta(days=3)


class SallaApiError(UserError):
    """A Salla API error answer, carrying its HTTP status so a caller can tell a
    permanent refusal (404, 422) from a blip (mapping.is_permanent_error). Still a
    UserError: every existing caller and message stays as it was."""

    def __init__(self, message, http_status=None):
        super().__init__(message)
        self.http_status = http_status


class SallaBackend(models.Model):
    _name = "salla.backend"
    _description = "Salla Store Backend"

    name = fields.Char(required=True, default="Salla KSA")
    active = fields.Boolean(default=True)

    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company,
    )
    warehouse_id = fields.Many2one(
        "stock.warehouse", string="Warehouse",
        domain="[('company_id', '=', company_id)]",
        default=lambda self: self.env["stock.warehouse"].search(
            [("company_id", "=", self.env.company.id)], limit=1),
        help="Warehouse Salla orders ship from and whose stock is pushed (F2+).",
    )

    # --- Salla app (Partners Portal -> App -> App Keys) ---
    store_id = fields.Char(
        string="Salla Store ID", copy=False,
        help="Salla merchant id. Filled by the app.store.authorize webhook; editable by "
             "an administrator as the recovery lever if that webhook was missed or the "
             "backend has to be re-bound. Webhooks for another store never touch it.",
    )
    client_id = fields.Char(string="Client ID", copy=False)
    client_secret = fields.Char(string="Client Secret", copy=False, groups=SECRET_GROUP)
    webhook_secret = fields.Char(
        string="Webhook Secret", copy=False, groups=SECRET_GROUP,
        help="Salla signs every webhook with this (HMAC-SHA256 of the raw body, header "
             "X-Salla-Signature). It belongs to the Salla app, so a demo store and the "
             "real store installed on the same app share it.",
    )

    # --- OAuth tokens (Easy Mode: delivered by the app.store.authorize webhook) ---
    access_token = fields.Char(copy=False, groups=SECRET_GROUP)
    refresh_token = fields.Char(
        copy=False, groups=SECRET_GROUP,
        help="SINGLE-USE. Reusing a spent one can force reinstalling the app on the "
             "store, which is why the refresh runs under a row lock.",
    )
    token_expires_at = fields.Datetime(
        string="Access Token Expires", copy=False,
        help="UTC. The daily cron refreshes the token a day before this.",
    )
    token_scope = fields.Char(
        string="Granted Scopes", copy=False, readonly=True,
        help="Scopes the store owner granted. A 403 from Salla usually means one is missing.",
    )

    # System-only: whatever host these name receives the client secret, the refresh
    # token and the Bearer token. A sale manager could otherwise point them at a server
    # of their own and collect the credentials on the next refresh.
    base_url = fields.Char(required=True, default="https://api.salla.dev/admin/v2",
                           groups=SECRET_GROUP)
    token_url = fields.Char(required=True, default="https://accounts.salla.sa/oauth2/token",
                            groups=SECRET_GROUP)
    webhook_url = fields.Char(
        string="Webhook URL", copy=False,
        default=lambda self: self._default_webhook_url(),
        help="Endpoint to paste in the Salla app's webhook settings. Defaults to "
             "web.base.url + /sl/webhook, editable because on odoo.sh web.base.url often "
             "points at another build. Use the domain, and on a Cloudflare-fronted domain "
             "add the Skip rule for /sl/webhook first (same trap as Trendyol). Its https "
             "origin is also where Salla downloads the uploaded invoice PDFs "
             "(/sl/invoice/...): the Skip rule must cover that path too.",
    )
    log_payloads = fields.Boolean(
        string="Log Full Webhook Payloads", groups=SECRET_GROUP,
        help="Off: webhooks log only their key structure. On: the full body, with "
             "credentials and customer data masked. Turn on only to check field names "
             "against the first real payloads, then off again.",
    )

    # --- F2a: orders ---
    website_id = fields.Many2one(
        "website", string="Website",
        domain="[('company_id', '=', company_id)]",
        help="Leave EMPTY (decided 2026-10-18). If set, every imported order carries "
             "this website: its WhatsApp confirmation template is sent on top of the "
             "Salla automation (customer gets it twice) and Salla orders mix with web "
             "sales in reporting. Customer messages come from Salla.")
    pricelist_id = fields.Many2one(
        "product.pricelist", string="Pricelist",
        default=lambda self: self._default_pricelist(),
        domain="[('company_id', 'in', [company_id, False])]",
        help="Only fixes the order currency (must be SAR): line prices come from Salla "
             "and every line carries discount 0.")
    salesperson_id = fields.Many2one("res.users", string="Salesperson",
                                     help="Empty = imported orders have no salesperson.")
    notify_user_ids = fields.Many2many(
        "res.users", "salla_backend_notify_user_rel", "backend_id", "user_id",
        string="Notify on New Order",
        help="Pinged (Discuss inbox) on every imported order with ordered vs available "
             "stock, and on every Salla problem with it (total mismatch, failed "
             "confirmation, cancellation). Empty = silent.")
    # --- F3: order status Odoo -> Salla, cancellations ---
    ops_alert_user_ids = fields.Many2many(
        "res.users", "salla_backend_ops_alert_user_rel", "backend_id", "user_id",
        string="Ops Alerts",
        help="Get a To-Do activity on the sale order when Odoo could not set it to "
             "\"shipped\" in Salla, or when Salla cancels an order Odoo must not cancel "
             "by itself (already shipped, invoice posted). Ops decided: the operations "
             "lead and the general Operations account. Empty = Notify on New Order users.",
        domain=[("share", "=", False)])
    send_status_sms = fields.Boolean(
        string="Send Status SMS", default=True,
        help="Sent with every status Odoo pushes (send_status_sms): Salla notifies the "
             "customer of the new status. On by default: customer messages come from "
             "Salla, not Odoo. Off = the status changes silently.")
    # --- F5: invoices (proposal C: Odoo is the only ZATCA issuer), payments ---
    # Opt-in: every invoice posted in INV goes to ZATCA for good, so a module upgrade
    # must never start posting on its own. Switched on deliberately at go-live.
    auto_invoice = fields.Boolean(
        string="Invoice Confirmed Orders", default=False,
        help="Create and post the customer invoice of every confirmed Salla order whose "
             "total matches Salla's, register its payment, and turn a cancellation "
             "before shipping into a credit note. Off = Accounting invoices by hand "
             "(posted invoices of Salla orders are still uploaded to Salla).")
    invoice_from = fields.Datetime(
        string="Invoice Orders Confirmed From", default=fields.Datetime.now, copy=False,
        help="UTC. The invoice cron only invoices hand-confirmed orders confirmed after "
             "this (set when the backend, or this field, was created): an upgrade never "
             "invoices older orders in bulk, each posted invoice goes to ZATCA for good.")
    invoice_journal_id = fields.Many2one(
        "account.journal", string="Invoice Journal",
        domain="[('type', '=', 'sale'), ('company_id', '=', company_id)]",
        default=lambda self: self._default_invoice_journal(),
        help="ZATCA-enabled sales journal the invoices are posted in (KSA: INV). Every "
             "invoice posted here is reported to ZATCA and cannot be undone: never test "
             "in the production database.")
    salla_payments_journal_id = fields.Many2one(
        "account.journal", string="Salla Payments Journal",
        domain="[('type', 'in', ('bank', 'cash')), ('company_id', '=', company_id)]",
        help="Payments by mada, credit card and Apple Pay (settled by Salla Payments).")
    tabby_journal_id = fields.Many2one(
        "account.journal", string="Tabby Journal",
        domain="[('type', 'in', ('bank', 'cash')), ('company_id', '=', company_id)]")
    tamara_journal_id = fields.Many2one(
        "account.journal", string="Tamara Journal",
        domain="[('type', 'in', ('bank', 'cash')), ('company_id', '=', company_id)]")
    payment_method_overrides = fields.Text(
        string="Payment Method Mapping",
        help="Extra Salla payment method slugs, one \"slug = journal\" per line, journal "
             "being salla_payments, tabby or tamara (e.g. \"stc_pay = salla_payments\"). "
             "Known slugs (mada, credit_card, apple_pay, tabby_*, tamara_*) need no "
             "line. An unknown method registers no payment and alerts Accounting. Cash on "
             "delivery and bank transfers are never mapped, whatever this says.")
    upload_invoices = fields.Boolean(
        string="Upload Invoices to Salla", default=True,
        help="Upload every posted invoice / credit note of a Salla order to Salla (the "
             "customer gets it there; Odoo never emails it).")
    require_zatca_before_upload = fields.Boolean(
        string="Require ZATCA Acceptance Before Upload", default=True,
        help="MUST stay on in production: only an invoice ZATCA accepted reaches the "
             "customer. Turn it off only on staging, which cannot get ZATCA acceptance.")
    accounting_alert_user_ids = fields.Many2many(
        "res.users", "salla_backend_accounting_alert_user_rel", "backend_id", "user_id",
        string="Accounting Alerts",
        help="Get a To-Do activity when an invoice, payment, credit note or upload to "
             "Salla needs a human. Empty = Notify on New Order users.",
        domain=[("share", "=", False)])
    shipping_product_id = fields.Many2one(
        "product.product", domain="[('company_id', 'in', [company_id, False])]",
        string="Shipping Product",
        help="Line for Salla's shipping cost. Must carry the 15% sales tax.")
    cod_product_id = fields.Many2one(
        "product.product", domain="[('company_id', 'in', [company_id, False])]",
        string="COD Fee Product",
        help="Line for Salla's cash-on-delivery fee. Must carry the 15% sales tax.")
    discount_product_id = fields.Many2one(
        "product.product", domain="[('company_id', 'in', [company_id, False])]",
        string="Discount Product",
        help="Line for coupons/offers (negative price) and for the rounding adjustment. "
             "Must carry the 15% sales tax.")
    auto_confirm = fields.Boolean(
        default=True,
        help="Confirm imported orders whose Salla status means paid. Off = everything "
             "lands as a quotation (the kill switch if confirmation misbehaves).")
    last_order_sync = fields.Datetime(
        string="Orders Synced Until", copy=False,
        help="UTC reconcile cursor. Held at the oldest order that could not be imported "
             "(missing SKU, error, recent payment_pending). Editable: it is the recovery "
             "lever when orders fall behind the window.")
    order_queue_count = fields.Integer(compute="_compute_order_queue_count")

    def _compute_order_queue_count(self):
        Queue = self.env["salla.order.queue"]
        for backend in self:
            backend.order_queue_count = Queue.search_count([("backend_id", "=", backend.id)])

    @api.model
    def _default_pricelist(self):
        company = self.env.company
        return self.env["product.pricelist"].search(
            [("currency_id", "=", company.currency_id.id),
             ("company_id", "in", [company.id, False])], limit=1)

    @api.model
    def _default_invoice_journal(self):
        Journal = self.env["account.journal"]
        domain = [("type", "=", "sale"), ("company_id", "=", self.env.company.id)]
        return Journal.search(domain + [("code", "=", "INV")], limit=1) \
            or Journal.search(domain, limit=1)

    def _payment_journal(self, key):
        """Journal for a mapping.payment_journal_key() key, empty if not configured."""
        self.ensure_one()
        field = {"salla_payments": "salla_payments_journal_id", "tabby": "tabby_journal_id",
                 "tamara": "tamara_journal_id"}.get(key)
        return self[field] if field else self.env["account.journal"]

    def _invoice_config_problem(self):
        """Why this backend cannot invoice, or ""."""
        self.ensure_one()
        journal = self.invoice_journal_id
        if not journal:
            return _("no Invoice Journal on the Salla backend")
        if journal.type != "sale" or journal.company_id != self.company_id:
            return _("the Invoice Journal %s is not a sales journal of %s") % (
                journal.display_name, self.company_id.name)
        return ""

    def _public_base_url(self):
        """Origin Salla downloads the invoice PDFs from (mapping.public_base_url)."""
        self.ensure_one()
        return mapping.public_base_url(
            self.webhook_url,
            self.env["ir.config_parameter"].sudo().get_param("web.base.url", ""))

    @api.model
    def _default_webhook_url(self):
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        return base.rstrip("/") + "/sl/webhook"

    @api.constrains("base_url", "token_url")
    def _check_salla_urls(self):
        for backend in self.sudo():
            for fname in ("base_url", "token_url"):
                if not mapping.is_allowed_salla_url(backend[fname]):
                    raise ValidationError(_(
                        "%s must be an https URL on a Salla host (api.salla.dev or "
                        "accounts.salla.sa).") % self._fields[fname].string)

    def _check_system_write(self, vals):
        if self.env.is_superuser() or self.env.user.has_group(SECRET_GROUP):
            return
        touched = [f for f in SYSTEM_WRITE_FIELDS if f in vals]
        if touched:
            raise AccessError(_("Only an administrator can change %s on a Salla backend.")
                              % ", ".join(touched))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._check_system_write(vals)
        return super().create(vals_list)

    def write(self, vals):
        self._check_system_write(vals)
        return super().write(vals)

    # ------------------------------------------------------------------ tokens
    # Transaction rules for every caller (F2+ included):
    # - Never write the salla_backend row in a transaction that may also refresh, nor
    #   before calling _request: the row lock that write holds makes the refresh's
    #   NOWAIT fail until that transaction commits. Cursors/counters that sync code needs
    #   (last_sync_date & co.) go on their own cursor or after the API calls.
    # - Token fields are written ONLY through _write_token_row on a dedicated cursor.
    # - Webhook handlers must not call the Salla API inline (Salla waits ~30 s, a 429
    #   alone may sleep 30 s): queue the work, e.g. ir.cron._trigger() on a sync cron.
    def _set_token_cache(self, values):
        """Make the caller's environment see token values committed by another cursor.

        Not invalidate(): the caller's transaction runs in REPEATABLE READ, so a re-read
        after invalidation would return its OLD snapshot — the spent refresh token. The
        values go straight into the cache instead (not marked dirty, so nothing is ever
        written back from this transaction)."""
        rec = self.sudo()
        for fname, value in values.items():
            if fname in TOKEN_COLUMNS:
                self.env.cache.set(rec, self._fields[fname], value or False)

    def _update_token_row(self, cr, values):
        """UPDATE the token columns on `cr`. log_exceptions=False: on a failed execute
        Odoo logs the query WITH its parameters filled in, i.e. the tokens."""
        cols = [c for c in TOKEN_COLUMNS if c in values]
        if not cols:
            return
        assignments = ", ".join("%s = %%s" % c for c in cols)   # names from TOKEN_COLUMNS only
        cr.execute(
            "UPDATE salla_backend SET %s, write_date = (now() at time zone 'UTC') "
            "WHERE id = %%s" % assignments,
            [values[c] or None for c in cols] + [self.id],
            log_exceptions=False)

    def _write_token_row(self, values):
        """Write token columns on a dedicated cursor and commit at once.

        Same reasons as the refresh: the tokens must survive a rollback of the caller's
        transaction, and the caller must not end up holding the row lock (which would
        make every later NOWAIT refresh fail).

        What actually happens on a collision with a refresh in flight: FOR UPDATE waits
        (bounded by WRITE_LOCK_TIMEOUT). When the refresh commits, Postgres does NOT hand
        us the row — under Odoo's REPEATABLE READ a row changed after our snapshot raises
        SerializationFailure. If the refresh outlasts the timeout we get LockNotAvailable.
        Either way the cursor is closed and the write retried on a FRESH cursor (new
        snapshot), which then takes the lock at once. The authorize values win over what
        the refresh wrote: they are the newer grant."""
        self.ensure_one()
        for attempt in range(WRITE_ATTEMPTS):
            cr = self.env.registry.cursor()
            try:
                cr.execute("SET LOCAL lock_timeout = %s", (WRITE_LOCK_TIMEOUT,))
                cr.execute("SELECT id FROM salla_backend WHERE id = %s FOR UPDATE",
                           (self.id,), log_exceptions=False)
                self._update_token_row(cr, values)
                cr.commit()
            except (pg_errors.SerializationFailure, pg_errors.LockNotAvailable):
                cr.rollback()
                if attempt == WRITE_ATTEMPTS - 1:
                    raise
                _logger.info("Salla backend %s: token row busy, retrying the write on a "
                             "fresh cursor.", self.name)
                continue
            finally:
                cr.close()
            self._set_token_cache(values)
            return

    def _refresh_token(self, stale_access_token=None, force=False):
        """Refresh the OAuth token pair safely and return the current tokens dict
        {access_token, refresh_token, token_expires_at}.

        Salla's refresh token is single-use, so two workers refreshing at once would burn
        it. Hence: a SEPARATE cursor, SELECT ... FOR UPDATE NOWAIT on the row, re-check
        inside the lock, and an immediate COMMIT on that cursor — the new refresh token
        must survive a rollback of the caller's transaction.

        `stale_access_token` = the token the caller just saw fail/expire. If the row
        already holds a different one, another worker refreshed in the meantime: use it.
        """
        self.ensure_one()
        for attempt in range(LOCK_ATTEMPTS):
            busy = False
            cr = self.env.registry.cursor()
            try:
                try:
                    cr.execute(
                        "SELECT client_id, client_secret, token_url, access_token, "
                        "refresh_token, token_expires_at FROM salla_backend "
                        "WHERE id = %s FOR UPDATE NOWAIT", (self.id,), log_exceptions=False)
                except (pg_errors.LockNotAvailable, pg_errors.SerializationFailure):
                    # Locked by another refresher, or changed under our snapshot: either
                    # way the next attempt, on a fresh cursor, sees the outcome.
                    cr.rollback()
                    busy = True
                if not busy:
                    return self._refresh_locked(cr, stale_access_token, force)
            finally:
                cr.close()
            # Sleep only after closing: no idle connection held while waiting.
            if attempt < LOCK_ATTEMPTS - 1:
                time.sleep(LOCK_WAIT)
        raise UserError(_(
            "Salla backend %s: another process kept the token refresh lock for over %s s. "
            "Try again in a minute.") % (self.name, LOCK_ATTEMPTS * LOCK_WAIT))

    def _refresh_locked(self, cr, stale_access_token, force):
        """Body of _refresh_token, run while `cr` holds the row lock."""
        row = cr.fetchone()
        if not row:
            raise UserError(_("Salla backend %s no longer exists.") % self.id)
        client_id, client_secret, token_url, access, refresh, expires = row
        current = {"access_token": access, "refresh_token": refresh,
                   "token_expires_at": expires}
        now = fields.Datetime.now()
        someone_refreshed = bool(stale_access_token) and access != stale_access_token
        if someone_refreshed or (
                not force and not mapping.token_needs_refresh(expires, now, TOKEN_REFRESH_MARGIN)):
            cr.rollback()
            self._set_token_cache(current)
            return current
        if not refresh:
            raise UserError(_(
                "Salla backend %s has no refresh token. Install (or reinstall) the app on "
                "the store so Salla sends app.store.authorize.") % self.name)

        # The point of no return: once Salla answers, the old refresh token is spent.
        # Deliberately no retry here — resending a spent token is exactly what can
        # revoke the app.
        tokens = self._post_token_refresh(token_url, client_id, client_secret, refresh, now)
        if not tokens["refresh_token"]:
            _logger.warning("Salla backend %s: token refresh answered without a refresh "
                            "token; keeping the previous one, which may now be spent.",
                            self.name)
        if not tokens["expires_at"]:
            tokens["expires_at"] = now + DEFAULT_TOKEN_LIFETIME
            _logger.warning("Salla backend %s: token refresh gave no readable expiry; "
                            "assuming %s days.", self.name, DEFAULT_TOKEN_LIFETIME.days)
        values = {"access_token": tokens["access_token"],
                  "refresh_token": tokens["refresh_token"] or refresh,
                  "token_expires_at": tokens["expires_at"]}
        if tokens["scope"]:
            values["token_scope"] = tokens["scope"]
        try:
            self._update_token_row(cr, values)
            cr.commit()
        except Exception:
            _logger.critical(
                "Salla backend %s (id %s): token refresh SUCCEEDED at Salla but the new "
                "tokens could NOT be saved. The old refresh token is spent; the app must be "
                "reinstalled on the store to get new tokens.",
                self.name, self.id, exc_info=True)
            raise
        _logger.info("Salla backend %s: access token refreshed, expires %s UTC",
                     self.name, values["token_expires_at"])
        self._set_token_cache(values)
        return {k: values[k] for k in ("access_token", "refresh_token", "token_expires_at")}

    @api.model
    def _post_token_refresh(self, token_url, client_id, client_secret, refresh, now):
        """POST grant_type=refresh_token. Returns mapping.parse_tokens() output.

        Errors carry only Salla's `error`/`error_description`, never the response body:
        a token endpoint response is the one body that can hold live credentials."""
        if not mapping.is_allowed_salla_url(token_url):
            # The ORM constraint covers the form; this covers a row edited by SQL.
            raise UserError(_("Salla token URL is not a Salla host; refusing to send credentials."))
        try:
            resp = requests.post(token_url, data={
                "grant_type": "refresh_token",
                "client_id": client_id or "",
                "client_secret": client_secret or "",
                "refresh_token": refresh,
            }, headers={"Accept": "application/json"}, timeout=TOKEN_TIMEOUT,
                # A 307/308 would resend client_secret + refresh_token in the body to
                # whatever host the Location names. Salla's token URL never redirects.
                allow_redirects=False)
        except requests.ConnectTimeout:
            # Never connected: the refresh token was not sent, still valid.
            raise UserError(_("Salla token endpoint unreachable (connect timeout)."))
        except requests.RequestException as e:
            # Sent, but no answer read: Salla may have rotated the token already.
            _logger.error("Salla token refresh: no answer (%s); the refresh token may "
                          "already be spent.", type(e).__name__)
            raise UserError(_(
                "Salla did not answer the token refresh (%s). The refresh token MAY already "
                "be spent: if the next refresh fails with invalid_grant, reinstall the app "
                "on the store.") % type(e).__name__)
        # allow_redirects=False makes a 3xx come back as the response itself, and
        # resp.ok is True below 400: without this it would read as "no access token".
        # Fixed label, no URL or Location: nothing about where it pointed is echoed.
        if 300 <= resp.status_code < 400:
            _logger.error("Salla token refresh: unexpected redirect (%s); refresh token "
                          "not followed elsewhere.", resp.status_code)
            raise UserError(_("Salla answered an unexpected redirect (%s) on the token "
                              "refresh.") % resp.status_code)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if not resp.ok:
            reason = ""
            if isinstance(data, dict):
                reason = " ".join(mapping.text(data.get(k)) for k in ("error", "error_description")
                                  if mapping.text(data.get(k)))[:200]
            _logger.error("Salla token refresh rejected (%s): %s", resp.status_code, reason)
            raise UserError(_(
                "Salla rejected the token refresh (%s) %s. If it says invalid_grant the "
                "refresh token is dead: reinstall the app on the store.") % (resp.status_code, reason))
        tokens = mapping.parse_tokens(data, now)
        if not tokens:
            _logger.error("Salla token refresh: 2xx without access_token; response keys: %s",
                          mapping.payload_keys(data))
            raise UserError(_("Salla token refresh returned no access token."))
        return tokens

    @api.model
    def cron_refresh_tokens(self):
        """Daily: refresh every token nearing expiry, so the 1-month refresh token never
        lapses on a store that happens to have no API traffic."""
        for backend in self.search([("active", "=", True)]):
            try:
                if not backend.sudo().refresh_token:
                    continue
                backend._refresh_token()
            except Exception as e:  # never let one backend kill the cron
                _logger.exception("Salla token refresh failed for backend %s: %s",
                                  backend.name, e)

    def action_refresh_token(self):
        self.ensure_one()
        # The view hides the button, but any object method is callable over RPC.
        if not self.env.user.has_group(SECRET_GROUP):
            raise AccessError(_("Only an administrator can force a Salla token refresh."))
        tokens = self._refresh_token(stale_access_token=self.sudo().access_token, force=True)
        return self._notify(_("Token valid until %s UTC.") % tokens["token_expires_at"])

    # ------------------------------------------------------------------ HTTP
    def _request(self, method, path, params=None, payload=None, idempotent=True):
        """Bearer request. Retries 429 (Retry-After / X-RateLimit-Reset) and 5xx/network
        with backoff; refreshes the token proactively near expiry and once on a 401.
        See the transaction rules above _set_token_cache before calling it.

        idempotent=False (a POST that creates something, e.g. an invoice upload): only
        what Salla certainly did not process is retried (401 refresh, 429, connect
        timeout). A read timeout, any other network error or a 5xx raises at once
        (SallaApiError, http_status None = transient): Salla may have created it, and
        the caller must check before sending it again."""
        self.ensure_one()
        backend = self.sudo()  # tokens/URLs are system-only fields; never exposed here
        if not mapping.is_allowed_salla_url(backend.base_url):
            raise UserError(_("Salla API URL is not a Salla host; refusing to send the token."))
        if not backend.access_token:
            raise UserError(_(
                "Salla backend %s is not authorized yet: install the app on the store "
                "(Salla sends the tokens through app.store.authorize).") % self.name)
        token = backend.access_token
        if backend.refresh_token and mapping.token_needs_refresh(
                backend.token_expires_at, fields.Datetime.now(), TOKEN_REFRESH_MARGIN):
            try:
                token = self._refresh_token(stale_access_token=token)["access_token"] or token
            except UserError as e:
                # The current token may still have hours left: try it rather than fail.
                _logger.warning("Salla backend %s: proactive refresh failed: %s", self.name, e)

        url = backend.base_url.rstrip("/") + path
        refreshed, attempt, last_err = False, 0, None
        while attempt < MAX_RETRIES:
            headers = {
                "Authorization": "Bearer %s" % token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            try:
                # No redirects: the Bearer token must not follow a Location header off
                # the allowlisted host. The 3xx is turned into an error right below.
                resp = requests.request(method, url, headers=headers, params=params,
                                        json=payload, timeout=REQUEST_TIMEOUT,
                                        allow_redirects=False)
            except requests.RequestException as e:
                last_err = type(e).__name__
                if not idempotent and not isinstance(e, requests.ConnectTimeout):
                    raise SallaApiError(_(
                        "Salla did not answer %s (%s); it may have processed it.") % (
                        path, last_err), None)
                attempt += 1
                if attempt < MAX_RETRIES:
                    time.sleep(mapping.retry_delay(None, {}, attempt - 1))
                continue
            # resp.ok is True for a 3xx: without this a redirect would read as a success
            # with an empty body. No Location in the message.
            if 300 <= resp.status_code < 400:
                raise UserError(_("Salla answered an unexpected redirect (%s) on %s.") % (
                    resp.status_code, path))

            if resp.status_code == 401:
                if refreshed:
                    # Salla also answers 401 (not 403) for an endpoint outside the app's
                    # scopes (GET /branches without branches.read, staging 2026-10-18).
                    raise SallaApiError(_(
                        "Salla answered 401 on %s even with a freshly refreshed token. "
                        "Either the app lacks the scope this endpoint needs (add it to the "
                        "app in the Salla Partners Portal, then reinstall the app on the "
                        "store), or the store revoked the app (reinstall it).") % path, 401)
                # One refresh per call, then the loop retries; no recursion into _request.
                token = self._refresh_token(stale_access_token=token, force=True)["access_token"]
                refreshed = True
                continue
            delay = mapping.retry_delay(resp.status_code, resp.headers, attempt)
            if delay is not None and not idempotent and resp.status_code != 429:
                raise SallaApiError(_("Salla answered %s on %s; it may have processed "
                                      "it.") % (resp.status_code, path), None)
            if delay is not None:
                last_err = "HTTP %s" % resp.status_code
                attempt += 1
                if attempt < MAX_RETRIES:
                    time.sleep(delay)
                continue
            if not resp.ok:
                # Admin API error bodies carry the error message, not credentials; still
                # truncated so a huge HTML error page doesn't flood the notification.
                raise SallaApiError(_("Salla API error %s on %s: %s") % (
                    resp.status_code, path, resp.text[:500]), resp.status_code)
            try:
                return resp.json()
            except ValueError:
                return resp.text
        raise UserError(_("Salla request %s failed after %s attempts: %s") % (
            path, MAX_RETRIES, last_err))

    # ------------------------------------------------------------------ webhook
    def _pick_for_event(self, merchant, event):
        """Among the backends whose secret verified a webhook, the one it belongs to
        (rules in mapping.pick_backend)."""
        key = mapping.pick_backend([(b.id, b.store_id) for b in self], merchant, event)
        return self.browse(key) if key else self.browse()

    def _process_webhook(self, payload):
        """Handle one verified webhook. Returns a short status for the HTTP answer.
        F1 only handles the app lifecycle; order events are logged and ignored (F2)."""
        self.ensure_one()
        event = mapping.event_name(payload)
        if event == mapping.AUTHORIZE_EVENT:
            tokens = mapping.parse_authorize(payload)
            if not tokens:
                _logger.error("Salla backend %s: %s without a readable access token; "
                              "payload keys: %s", self.name, event, mapping.payload_keys(payload))
                return "authorize:unparsed"
            expires = tokens["expires_at"]
            if not expires:
                expires = fields.Datetime.now() + DEFAULT_TOKEN_LIFETIME
                _logger.warning("Salla backend %s: authorize gave no readable expiry; "
                                "assuming %s days.", self.name, DEFAULT_TOKEN_LIFETIME.days)
            # A missing refresh token / scope never blanks the stored one: losing the
            # refresh token means reinstalling the app.
            values = {"access_token": tokens["access_token"], "token_expires_at": expires}
            if tokens["refresh_token"]:
                values["refresh_token"] = tokens["refresh_token"]
            else:
                _logger.warning("Salla backend %s: authorize carried no refresh token.", self.name)
            if tokens["scope"]:
                values["token_scope"] = tokens["scope"]
            if tokens["store_id"] and not self.store_id:
                values["store_id"] = tokens["store_id"]
            self._write_token_row(values)
            _logger.info("Salla backend %s: authorized for store %s, token expires %s UTC",
                         self.name, tokens["store_id"] or self.store_id, expires)
            return "authorized"
        if event in mapping.REVOKE_EVENTS:
            self._write_token_row({"access_token": False, "refresh_token": False,
                                   "token_expires_at": False})
            _logger.warning("Salla backend %s: %s received, tokens cleared. The app has to "
                            "be reinstalled on store %s.", self.name, event, self.store_id)
            return "revoked"
        if event in mapping.ORDER_EVENTS:
            order_id = mapping.webhook_order_id(payload)
            if not order_id:
                _logger.warning("Salla backend %s: %s without a readable order id; payload "
                                "keys: %s", self.name, event, mapping.payload_keys(payload))
                return "ignored:no-order-id"
            # Only the address leaves the body (whitelisted keys): the API detail the
            # import reads has none since 2026-09-01.
            self._enqueue_order(order_id, event, ship_to=mapping.webhook_ship_to(payload))
            return "queued"
        return "ignored:%s" % (event or "?")

    def _enqueue_order(self, order_id, event, ship_to=None):
        """Queue a Salla order id for the import cron and wake it up. No API call and no
        write on salla_backend here (webhook rules above _set_token_cache).

        Append-only: a plain INSERT, one row per delivery, no unique key. It can never
        conflict with, or wait on, the cron working on the same order — the cron only
        deletes the row ids it read, so a webhook arriving mid-processing survives as a
        new due row and is processed on the next run (which this call triggers).

        `ship_to` = mapping.webhook_ship_to(body): stored on the row (PII, see the field)
        for _sync_order; None/{} for manual rows and events without an address."""
        self.ensure_one()
        order_id = mapping.clean_id(order_id)
        if not order_id:
            return False
        self.env.cr.execute("""
            INSERT INTO salla_order_queue
                (backend_id, salla_order_id, event, state, attempts, ship_to_json,
                 create_uid, write_uid, create_date, write_date)
            VALUES (%s, %s, %s, 'pending', 0, %s, %s, %s,
                    now() at time zone 'UTC', now() at time zone 'UTC')
        """, (self.id, order_id, event or "manual", mapping.dump_ship_to(ship_to),
              self.env.uid, self.env.uid))
        self.env.ref("salla_connector.ir_cron_salla_order_queue").sudo()._trigger()
        return True

    # ------------------------------------------------------------------ actions
    def _notify(self, message, warning=False, title=None):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "warning" if warning else "success",
                "title": title or _("Salla"),
                "message": message,
                "sticky": warning,
            },
        }

    def action_test_connection(self):
        self.ensure_one()
        res = self._request("GET", "/store/info")
        data = res.get("data") if isinstance(res, dict) else None
        data = data if isinstance(data, dict) else {}
        store = mapping.text(data.get("id"))
        name = mapping.text(data.get("name")) or "?"
        if self.store_id and store and store != self.store_id:
            return self._notify(_(
                "Connected, but the token belongs to store %s (%s) while this backend is "
                "bound to store %s.") % (store, name, self.store_id), warning=True)
        return self._notify(_("Connection OK: store %s (%s).") % (name, store or "?"))

    def _iter_products(self):
        """Yield every Salla product, paging /products. Stops on the last page, an empty
        page, a page that isn't the one asked for, or MAX_PAGES."""
        self.ensure_one()
        page = 1
        for _i in range(MAX_PAGES):
            res = self._request("GET", "/products", params={"page": page, "per_page": PER_PAGE})
            items = res.get("data") if isinstance(res, dict) else None
            if not isinstance(items, list) or not items:
                return
            pagination = res.get("pagination")
            served = mapping.current_page(pagination)
            if served is not None and served != page:
                # An API that ignores `page` would hand back page 1 forever.
                _logger.warning("Salla backend %s: asked for products page %s, got %s; "
                                "listing stopped.", self.name, page, served)
                return
            for product in items:
                if isinstance(product, dict):
                    yield product
            page = mapping.next_page(pagination)
            if not page:
                return
        _logger.warning("Salla backend %s: product listing stopped at the %s-page cap",
                        self.name, MAX_PAGES)

    def _product_skus(self, product):
        """SKUs of a product and its variants. When the list payload only declares
        options, read the variants from the product detail (field names unverified)."""
        skus = mapping.product_skus(product)
        if mapping.needs_variant_fetch(product) and product.get("id"):
            try:
                detail = self._request("GET", "/products/%s" % product["id"])
                detail = detail.get("data") if isinstance(detail, dict) else None
                skus += [s for s in mapping.product_skus(detail) if s not in skus]
            except UserError as e:
                _logger.warning("Salla product %s: variants not readable: %s", product.get("id"), e)
        return skus

    def action_check_products(self):
        """Reconcile Salla product/variant SKUs against product.product.default_code.
        Reports SKUs missing from Odoo and SKUs that only match an archived product."""
        self.ensure_one()
        # active_test=False so archived products are diagnosed as "archived", not "missing".
        Product = self.env["product.product"].with_context(active_test=False)
        rows, no_sku = [], []
        for product in self._iter_products():
            title = mapping.text(product.get("name"))[:40]
            skus = self._product_skus(product)
            if not skus:
                no_sku.append("%s (%s) — no SKU on Salla" % (product.get("id") or "?", title))
            rows += [(sku, title) for sku in skus]

        codes = list({sku for sku, _t in rows})
        # order="active desc" -> an active match wins over an archived duplicate
        found = {}
        for prod in Product.search([("default_code", "in", codes)], order="active desc") if codes else []:
            found.setdefault(prod.default_code, prod)
        missing = list(no_sku)
        for sku, title in rows:
            match = found.get(sku)
            if not match:
                missing.append("%s (%s) — not in Odoo" % (sku, title))
            elif not match.active:
                missing.append("%s (%s) — ARCHIVED in Odoo" % (sku, title))
        total = len(rows) + len(no_sku)
        if missing:
            _logger.warning("Salla SKU check: %s issues of %s: %s", len(missing), total, missing)
        msg = (_("%s of %s Salla SKUs need attention:\n%s") % (
            len(missing), total, "\n".join(missing[:20]) + ("\n…" if len(missing) > 20 else ""))
            if missing else _("All %s Salla SKUs match an active Odoo product.") % total)
        return self._notify(msg, warning=bool(missing), title=_("Salla Product Check"))

    # ------------------------------------------------------------------ orders (F2a)
    # Transaction rules (see above _set_token_cache): nothing here writes the
    # salla_backend row through the ORM. The reconcile cursor is written by
    # _save_order_cursor AFTER the orders commit, on its own cursor.
    def _log_payload(self, kind, ref, data):
        """Full body only with log_payloads on (credentials and PII masked), else the key
        structure: the first real orders are the only way to validate mapping.py."""
        if self.sudo().log_payloads:
            _logger.info("Salla %s %s: %s", kind, ref, json.dumps(mapping.redact(data)))
        else:
            _logger.debug("Salla %s %s, keys: %s", kind, ref, mapping.payload_keys(data))

    def _list_pages(self, path, params, max_pages):
        """All `data` items of a paginated GET -> (items, complete). complete=False when
        the listing stopped early (page cap, an API ignoring `page`): the caller must not
        advance a cursor past what it did not see."""
        items, page = [], 1
        for _i in range(max_pages):
            res = self._request("GET", path, params=dict(params, page=page, per_page=PER_PAGE))
            batch = res.get("data") if isinstance(res, dict) else None
            if not isinstance(batch, list) or not batch:
                return items, True
            pagination = res.get("pagination")
            served = mapping.current_page(pagination)
            if served is not None and served != page:
                _logger.warning("Salla backend %s: asked %s page %s, got %s; stopped.",
                                self.name, path, page, served)
                return items, False
            items += [i for i in batch if isinstance(i, dict)]
            page = mapping.next_page(pagination)
            if not page:
                return items, True
        _logger.warning("Salla backend %s: %s stopped at the %s-page cap", self.name, path,
                        max_pages)
        return items, False

    def _fetch_order(self, order_id):
        res = self._request("GET", "/orders/%s" % order_id)
        data = res.get("data") if isinstance(res, dict) else None
        if isinstance(data, dict):
            self._log_payload("order", order_id, data)
        return data if isinstance(data, dict) else None

    def _fetch_order_items(self, order_id):
        """Order Items endpoint (the "expanded" order format is gone since 2026-09-01).
        -> (items, complete)."""
        items, complete = self._list_pages(mapping.ORDER_ITEMS_PATH, {"order_id": order_id},
                                           MAX_ITEM_PAGES)
        self._log_payload("order items", order_id, items)
        return items, complete

    def _order_config_problem(self, order, plan=None):
        """Why this backend cannot build the order, or "" — checked before anything is
        created, so a misconfiguration holds the order instead of importing it wrong."""
        if not self.warehouse_id or self.warehouse_id.company_id != self.company_id:
            return "warehouse missing or not in the backend's company"
        if not self.pricelist_id:
            return "no pricelist on the backend"
        if self.website_id and self.website_id.company_id != self.company_id:
            return "website belongs to another company"
        currency = order.get("currency")
        if currency and currency != self.pricelist_id.currency_id.name:
            return "order currency %s but pricelist currency %s" % (
                currency, self.pricelist_id.currency_id.name)
        if plan:
            needed = {e["kind"] for e in plan["extras"]}
            if plan["extras"] or plan["salla_total"] is not None:
                needed.add("discount")   # also carries the rounding adjustment
            names = {"shipping": "shipping_product_id", "cod": "cod_product_id",
                     "discount": "discount_product_id"}
            missing = [names[k] for k in sorted(needed) if not self[names[k]]]
            if missing:
                return "backend has no %s" % ", ".join(missing)
            foreign = [names[k] for k in sorted(needed) if self[names[k]].company_id
                       and self[names[k]].company_id != self.company_id]
            if foreign:
                return "%s belongs to another company" % ", ".join(foreign)
            for key in sorted(needed):
                # Odoo's rate, never the payload's: a payload saying 0 % (VAT off on the
                # demo store, exempt, misread field) must not turn every order into a
                # config error. That disagreement goes down the total-mismatch path.
                problem = self._extra_product_problem(self[names[key]], plan["odoo_vat_rate"])
                if problem:
                    return "%s: %s" % (names[key], problem)
        return ""

    def _extra_product_problem(self, product, vat_rate):
        """Shipping/COD/discount/rounding lines are built on these products. A storable
        one would create stock moves for a fee; one without the price-excluded VAT of the
        order would break "Odoo total == Salla total" on every order that uses it."""
        if product.type != "service":
            return "must be a service product"
        taxes = product.taxes_id.filtered(lambda t: t.company_id == self.company_id)
        if not taxes.filtered(lambda t: t.amount_type == "percent" and not t.price_include
                              and abs(t.amount - vat_rate) < 0.001):
            return "needs a %g%% sales tax (not included in price) for %s" % (
                vat_rate, self.company_id.name)
        return ""

    def _sync_order(self, order_id, summary=None):
        """Import or refresh ONE Salla order. -> (outcome, created_at, detail)

        outcome: "created" | "known" (already in Odoo, status refreshed) |
        "not_importable" | "missing_sku" | "error" (API/config/data problem, retried).
        `summary` = the order as the list endpoint returned it: enough to refresh a known
        order without one more API call. Creation always reads the full order."""
        self.ensure_one()
        SaleOrder = self.env["sale.order"].with_company(self.company_id)
        existing = SaleOrder._salla_find(order_id)
        if summary is not None:
            norm = mapping.normalize_order(summary) or {}
            if existing:
                existing._salla_apply_status(norm.get("status"))
                return "known", norm.get("created_at"), ""
            # payment_pending/cancelled in the listing: no detail call every 30 min.
            if norm.get("status") and not mapping.should_import(norm["status"]):
                return "not_importable", norm.get("created_at"), norm["status"]
        raw = self._fetch_order(order_id)
        order = mapping.normalize_order(raw)
        if not order:
            return "error", None, "order %s not readable from the API" % order_id
        if existing:
            existing._salla_apply_status(order["status"])
            return "known", order["created_at"], ""
        if not mapping.should_import(order["status"]):
            return "not_importable", order["created_at"], order["status"]
        problem = self._order_config_problem(order)
        if problem:
            return "error", order["created_at"], problem
        raw_items, complete = self._fetch_order_items(order_id)
        lines, problems = mapping.normalize_items(raw_items)
        if not complete:
            problems.append("item listing incomplete")
        if not lines and not problems:
            problems.append("no items")
        if problems:
            return "error", order["created_at"], "; ".join(problems)[:300]
        products = SaleOrder._salla_match_products(self, [l["sku"] for l in lines])
        missing = mapping.missing_skus(lines, products.keys())
        if missing:
            # Whole order skipped, never half an order. Retried until the SKU exists.
            _logger.warning("Salla order %s (%s) skipped: SKU(s) not in Odoo %s",
                            order["reference"], order_id, missing)
            return "missing_sku", order["created_at"], ", ".join(missing)
        for sku, product in products.items():
            if not product.active:
                _logger.warning("Salla order %s: product %s is archived in Odoo",
                                order["reference"], sku)
        if order["vat_suspect"]:
            _logger.warning("Salla order %s: unexpected tax percent in the payload, using "
                            "%s%%", order["reference"], order["vat_rate"]
                            if order["vat_rate"] is not None else mapping.DEFAULT_VAT_RATE)
        # Odoo stores unit prices with this precision (7 on prod): predict with the same.
        digits = self.env["decimal.precision"].precision_get("Product Price")
        plan = mapping.build_lines(order, lines, price_digits=digits)
        # The rate Odoo's KSA sales tax applies (15 %); _salla_reconcile_total flags an
        # order whose payload rate disagrees, and rounding lines are sized with it.
        plan["odoo_vat_rate"] = mapping.DEFAULT_VAT_RATE
        problem = self._order_config_problem(order, plan)
        if problem:
            return "error", order["created_at"], problem
        # Read from the raw order, not normalize_order(): that one stays PII-free (name
        # only) because its output reaches log lines; buyer() goes straight to the partner.
        # The API detail has no address: it comes from the webhook, via the queue.
        detail = mapping.with_ship_to(raw, self._queued_ship_to(order_id))
        so = SaleOrder._create_from_salla(self, order, plan, products,
                                          buyer=mapping.buyer(detail))
        if not so:
            # Lost the race against the other path (webhook queue vs reconcile). Under
            # REPEATABLE READ the winner's row may be invisible to this snapshot: then it
            # owns the order and its status, nothing to do here.
            winner = SaleOrder._salla_find(order_id)
            if winner:
                winner._salla_apply_status(order["status"])
            return "known", order["created_at"], ""
        _logger.info("Salla order %s (%s) imported as %s, state %s", order["reference"],
                     order_id, so.name, so.state)
        return "created", order["created_at"], ""

    def _queued_ship_to(self, order_id):
        """The address the NEWEST queue row of this order carries (any state), {} if none.

        Read here, whoever imports (queue or reconcile): the reconcile cron can import an
        order whose webhook row still waits in the queue (missing SKU retried every
        ~25 min) and must not drop the address that row holds. Newest wins: an
        order.updated after the customer edited the address beats the order.created."""
        row = self.env["salla.order.queue"].sudo().search(
            [("backend_id", "=", self.id), ("salla_order_id", "=", order_id),
             ("ship_to_json", "!=", False)], order="id desc", limit=1)
        return mapping.load_ship_to(row.ship_to_json) if row else {}

    def _sync_order_safe(self, order_id, summary=None):
        """_sync_order in a savepoint: one bad order never takes the batch down with it."""
        try:
            with self.env.cr.savepoint():
                result = self._sync_order(order_id, summary=summary)
        except Exception as e:
            _logger.exception("Salla backend %s: order %s failed", self.name, order_id)
            return "error", None, ("%s: %s" % (type(e).__name__, e))[:300]
        if result[0] == "error":
            _logger.warning("Salla backend %s: order %s not imported: %s", self.name,
                            order_id, result[2])
        return result

    # --- queue (webhook path) ---
    @api.model
    def cron_process_order_queue(self):
        for backend in self.search([("active", "=", True)]):
            try:
                with self.env.cr.savepoint():
                    backend._process_order_queue()
            except Exception as e:  # never let one backend kill the cron
                _logger.exception("Salla order queue failed for backend %s: %s",
                                  backend.name, e)

    def _process_order_queue(self, limit=QUEUE_BATCH):
        """Process up to `limit` ORDERS (not rows) that are due, each once.

        Due = never tried, or last tried more than QUEUE_RETRY_AFTER ago: a pile of
        missing-SKU rows is retried at most every ~25 min instead of looping the cron.
        Only the row ids read here are deleted or updated; rows inserted meanwhile (a
        status webhook during processing) stay due and are processed next run."""
        self.ensure_one()
        Queue = self.env["salla.order.queue"]
        now = fields.Datetime.now()
        rows = Queue.search([
            ("backend_id", "=", self.id), ("state", "=", "pending"),
            "|", ("last_try", "=", False), ("last_try", "<", now - QUEUE_RETRY_AFTER)],
            # ponytail: the 5x read cap can split an order's rows across runs; the rest
            # are simply processed next run (imports are idempotent).
            order="last_try asc nulls first, id", limit=limit * 5)
        groups = {}
        for row in rows:
            if row.salla_order_id in groups or len(groups) < limit:
                groups.setdefault(row.salla_order_id, Queue)
                groups[row.salla_order_id] |= row
        counters = {}
        for order_id, group in groups.items():
            outcome, created, detail = self._sync_order_safe(order_id)
            counters[outcome] = counters.get(outcome, 0) + 1
            try:
                # Own savepoint, any exception: a failure here (a Retry clicked on this
                # order's row mid-run -> SerializationFailure, a row deleted meanwhile ->
                # MissingError, a bug) must cost only this order's bookkeeping, redone
                # next run. Letting it reach the cron's savepoint rolled back EVERY order
                # of the batch and the queue never drained (staging 2026-10-18).
                with self.env.cr.savepoint():
                    self._queue_bookkeeping(order_id, group, outcome, detail,
                                            created=created)
            except Exception:
                _logger.warning("Salla order %s: queue bookkeeping failed (outcome %s), "
                                "rows left as they were; retried next run", order_id,
                                outcome, exc_info=True)
        if len(groups) == limit:
            # Batch full of DUE orders: more may be waiting. Processed ones are no longer
            # due, so this cannot loop on the same rows.
            self.env.ref("salla_connector.ir_cron_salla_order_queue").sudo()._trigger()
        if groups:
            _logger.info("Salla backend %s: order queue -> %s", self.name, counters)
        return counters

    def _queue_bookkeeping(self, order_id, group, outcome, detail, created=None):
        """Delete or update the queue rows `group` (read for `order_id`) after a sync."""
        Queue = self.env["salla.order.queue"]
        now = fields.Datetime.now()
        if outcome == "not_importable" and mapping.holds_cursor(detail, created, now):
            # payment_pending and recent: ONE row stays, re-checked every ~25 min (one GET,
            # no items call). It holds the only copy of the address: the status webhook
            # that follows the payment carries none, and it may be lost anyway.
            group = group.exists()
            if group:
                keep = next((r for r in group.sudo().sorted("id", reverse=True)
                             if r.ship_to_json), None) or group.sorted("id")[0]
                # Never kept past the hold, even when the order date is unreadable
                # (holds_cursor is True for created=None): the PII must leave.
                if keep.create_date >= now - timedelta(days=mapping.PENDING_HOLD_DAYS):
                    (group - keep).unlink()
                    keep.sudo().write({"last_try": now, "last_error": detail or outcome})
                    return
        if outcome in ("created", "known", "not_importable"):
            # not_importable (payment_pending/cancelled): a later status webhook queues
            # it again, and the reconcile cursor holds recent pending ones. Old failed
            # rows of the order are moot now too. exists(): a row deleted meanwhile
            # (another run, a manual delete) is skipped instead of failing here.
            (group.exists() | Queue.search([("backend_id", "=", self.id),
                                            ("salla_order_id", "=", order_id),
                                            ("state", "=", "failed")])).unlink()
            return
        # One row carries the order's retry state; duplicates read with it go. Read
        # everything BEFORE the unlink: reading a deleted record raises MissingError.
        group = group.exists()
        if not group:
            return
        attempts = max(group.mapped("attempts") or [0])
        keep = group.sorted("id")[0]
        # The kept row is the OLDEST; the address of a newer duplicate moves onto it
        # before that one is deleted, or the order would import without it next run.
        ship_to = next((r.ship_to_json for r in group.sudo().sorted("id", reverse=True)
                        if r.ship_to_json), False)
        (group - keep).unlink()
        vals = {"last_try": now, "last_error": detail or outcome}
        if ship_to:
            vals["ship_to_json"] = ship_to
        if outcome == "error":
            # A missing SKU never fails the row: it waits until the product exists.
            vals["attempts"] = attempts + 1
            if vals["attempts"] >= QUEUE_MAX_ATTEMPTS:
                vals["state"] = "failed"
                _logger.warning("Salla order %s: gave up after %s attempts: %s",
                                order_id, vals["attempts"], detail)
        keep.sudo().write(vals)

    def _check_order_manager(self):
        # The view hides the buttons, but any object method is callable over RPC.
        if not self.env.user.has_group("sales_team.group_sale_manager"):
            raise AccessError(_("Only a sales manager can run the Salla order import."))

    def action_process_order_queue(self):
        self.ensure_one()
        self._check_order_manager()
        # OdooBot, like the cron: same authorship whoever clicks.
        counters = self.with_user(SUPERUSER_ID)._process_order_queue()
        return self._notify(_("Order queue processed: %s") % (counters or _("nothing due")))

    def action_view_order_queue(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Salla Order Queue"),
            "res_model": "salla.order.queue",
            "view_mode": "tree,form",
            "domain": [("backend_id", "=", self.id)],
            "context": {"default_backend_id": self.id},
        }

    # --- reconcile (cron path: catches every webhook Salla gave up on) ---
    @api.model
    def cron_reconcile_orders(self):
        for backend in self.search([("active", "=", True)]):
            try:
                with self.env.cr.savepoint():
                    backend._reconcile_orders()
            except Exception as e:  # never let one backend kill the cron
                _logger.exception("Salla order reconcile failed for backend %s: %s",
                                  backend.name, e)

    def _reconcile_orders(self):
        """List the orders of [cursor - overlap, now] and import what Odoo lacks.

        The cursor is HELD at the oldest order that still needs work (missing SKU, any
        error, payment_pending younger than PENDING_HOLD_DAYS): advancing past an order
        Salla never touches again loses it for good (Trendyol lesson). It is written only
        after this transaction commits, so a rollback never skips orders."""
        self.ensure_one()
        now = fields.Datetime.now()
        start = self.last_order_sync or now - timedelta(days=DEFAULT_LOOKBACK_DAYS)
        params = {mapping.ORDER_LIST_FROM_PARAM: mapping.to_salla_date(start - WINDOW_OVERLAP),
                  # +1 day: if Salla's to_date is exclusive, today's orders would never
                  # be listed while the cursor moves to now.
                  mapping.ORDER_LIST_TO_PARAM: mapping.to_salla_date(now + timedelta(days=1))}
        summaries, complete = self._list_pages("/orders", params, MAX_ORDER_PAGES)
        counters, holds, skus = {}, [], set()
        for summary in summaries:
            order_id = mapping.clean_id(summary.get("id"))
            if not order_id:
                continue
            outcome, created, detail = self._sync_order_safe(order_id, summary=summary)
            counters[outcome] = counters.get(outcome, 0) + 1
            created = created or (mapping.normalize_order(summary) or {}).get("created_at")
            if outcome in ("missing_sku", "error"):
                holds.append(created)
                if outcome == "missing_sku":
                    skus.update(detail.split(", "))
            elif outcome == "not_importable" and mapping.holds_cursor(detail, created, now):
                holds.append(created)
        cursor = mapping.next_cursor(now, holds) if complete else None
        if cursor:
            self._save_order_cursor(cursor)
        effective = cursor or start
        if effective < now - CURSOR_STALE_AFTER:
            _logger.warning("Salla backend %s: order cursor held at %s, more than %s days "
                            "behind (missing SKU? error? see the order queue and log)",
                            self.name, effective, CURSOR_STALE_AFTER.days)
        _logger.info("Salla backend %s: reconcile %s -> %s (%s listed); cursor %s",
                     self.name, params, counters, len(summaries),
                     "-> %s after commit" % cursor if cursor else "held at %s" % start)
        return {"counters": counters, "listed": len(summaries), "cursor": cursor,
                "window": "%s → %s" % (start, now), "missing_skus": sorted(skus)}

    def _save_order_cursor(self, value):
        """Write last_order_sync on a fresh cursor once the current transaction has
        committed (postcommit), i.e. once the orders it covers are durable. Never through
        the ORM in the sync transaction: the row lock would make a token refresh's NOWAIT
        fail (or a SerializationFailure) for the rest of the run."""
        self.ensure_one()
        backend_id, registry, name = self.id, self.env.registry, self.name

        @self.env.cr.postcommit.add
        def _write_cursor():
            try:
                with registry.cursor() as cr:
                    cr.execute("SET LOCAL lock_timeout = %s", (WRITE_LOCK_TIMEOUT,))
                    cr.execute("UPDATE salla_backend SET last_order_sync = %s WHERE id = %s",
                               (value, backend_id))
            except Exception:
                # Harmless: the next run re-reads the same window (imports are idempotent).
                _logger.warning("Salla backend %s: order cursor not saved, next run "
                                "re-reads the window", name, exc_info=True)

    def action_import_orders(self):
        self.ensure_one()
        self._check_order_manager()
        res = self.with_user(SUPERUSER_ID)._reconcile_orders()
        msg = _("Window %(window)s UTC — %(listed)s order(s) listed: %(c)s. Cursor: %(cur)s.") % {
            "window": res["window"], "listed": res["listed"], "c": res["counters"] or "-",
            "cur": res["cursor"] or _("held")}
        if res["missing_skus"]:
            msg += _(" No product.product has default_code: %s") % ", ".join(res["missing_skus"])
        return self._notify(msg, warning=bool(res["missing_skus"]) or not res["cursor"])
