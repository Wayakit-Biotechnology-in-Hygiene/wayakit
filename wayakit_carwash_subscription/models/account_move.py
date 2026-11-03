import logging

from psycopg2 import errors as pg_errors

from odoo import _, models
from odoo.exceptions import AccessError

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = 'account.move'

    def _invoice_paid_hook(self):
        res = super()._invoice_paid_hook()
        Period = self.env['carwash.subscription.period'].sudo()
        odoobot = self.env.ref('base.partner_root').id
        for move in self.filtered(lambda m: m.move_type == 'out_invoice'):
            try:
                # Never block the payment reconciliation because of this module.
                with self.env.cr.savepoint():
                    Period._cw_issue_from_invoice(move)
            except Exception as e:
                # A unique violation means another transaction issued this invoice first
                # (the DB waited for it to commit); a same-transaction re-run shows up in the count.
                if isinstance(e, pg_errors.UniqueViolation) or \
                        Period.search_count([('invoice_id', '=', move.id)]):
                    _logger.info('Car Wash subscription: entitlements for %s already issued by another run', move.name)
                    continue
                _logger.exception('Car Wash subscription: could not issue entitlements for %s', move.name)
                move.sudo().message_post(
                    body='⚠️ Car Wash: entitlements were NOT issued automatically. '
                         'Use Action > "Issue Car Wash entitlements" after fixing the issue.',
                    author_id=odoobot)
        return res

    def action_cw_issue_entitlements(self):
        """Manual / re-run (idempotent). Only for paid invoices."""
        # The server action is already limited to sales managers; checked again here because
        # the method is public (RPC) and everything below runs as sudo.
        if not (self.env.is_superuser() or self.env.user.has_group('sales_team.group_sale_manager')):
            raise AccessError(_('Only Sales managers can issue Car Wash entitlements.'))
        self.check_access_rights('read')
        self.check_access_rule('read')
        Period = self.env['carwash.subscription.period'].sudo()
        for move in self.sudo().filtered(lambda m: m.move_type == 'out_invoice' and m.payment_state in ('paid', 'in_payment')):
            Period._cw_issue_from_invoice(move)
        return True
