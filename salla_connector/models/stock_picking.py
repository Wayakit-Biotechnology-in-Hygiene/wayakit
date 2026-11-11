import logging

from odoo import models

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = "stock.picking"

    def _action_done(self):
        # _action_done, not button_validate: it also runs for the backorder wizard and
        # immediate transfers.
        res = super()._action_done()
        self._salla_queue_status_push()
        return res

    def action_cancel(self):
        # Cancelling the last open backorder can make the order fully shipped too.
        res = super().action_cancel()
        self._salla_queue_status_push()
        return res

    def _salla_queue_status_push(self):
        """Mark the Salla orders of these deliveries for the push cron. sudo: whoever
        validates is often a stock user who cannot read sale orders or the backend. Never
        raises: the validation is the business fact, the Salla status follows it."""
        outgoing = self.sudo().filtered(lambda p: p.picking_type_code == "outgoing")
        for order in outgoing.sale_id.filtered(lambda o: o.salla_order_id):
            try:
                with self.env.cr.savepoint():
                    order._salla_mark_push()
            except Exception:
                _logger.warning("Salla order %s: status push not queued (pickings %s); use "
                                "Push Status to Salla on the order", order.name, self.ids,
                                exc_info=True)
