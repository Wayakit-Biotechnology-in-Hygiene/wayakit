from odoo import fields, models, _
from odoo.exceptions import AccessError


class SallaOrderQueue(models.Model):
    """Salla order ids waiting to be fetched and imported.

    Why a table and not the webhook doing the work: Salla waits ~30 s per webhook and an
    import makes 2+ API calls that may each sleep on a 429 or refresh the token. The
    webhook only inserts here and triggers the queue cron.

    Append-only, one row per delivery and deliberately NO unique key: a unique key makes
    the webhook either drop a delivery that arrives while the cron works on that order,
    or wait on the cron's row lock past Salla's 30 s. The cron groups rows by order and
    deletes the ids it read once the order is in Odoo (or is not importable); what stays
    is what still needs work, so the list view is the to-do list."""
    _name = "salla.order.queue"
    _description = "Salla Order Import Queue"
    _order = "id"

    backend_id = fields.Many2one("salla.backend", required=True, ondelete="cascade",
                                 index=True)
    salla_order_id = fields.Char(string="Salla Order ID", required=True, index=True)
    event = fields.Char(readonly=True, help="Webhook event that queued it (or 'manual').")
    state = fields.Selection(
        [("pending", "Pending"), ("failed", "Failed")], default="pending", required=True,
        help="Pending: retried on every queue run (a missing SKU stays here until the "
             "product exists). Failed: gave up after repeated errors; Retry puts it back.")
    attempts = fields.Integer(readonly=True)
    last_try = fields.Datetime(readonly=True)
    last_error = fields.Char(readonly=True)
    # The buyer's shipping address from the webhook body (mapping.webhook_ship_to, JSON):
    # the API's order detail no longer carries it, so this row is the only copy until
    # the import. Customer PII: system-only in Python (salesmen read this model over
    # RPC), in no view, never logged; it leaves the DB with the row once imported.
    ship_to_json = fields.Text(string="Shipping Address (webhook)", readonly=True,
                               groups="base.group_system", copy=False)

    def action_retry(self):
        if not self.env.user.has_group("sales_team.group_sale_manager"):
            raise AccessError(_("Only a sales manager can retry Salla imports."))
        self.write({"state": "pending", "attempts": 0, "last_try": False})
        self.env.ref("salla_connector.ir_cron_salla_order_queue").sudo()._trigger()
        return True
