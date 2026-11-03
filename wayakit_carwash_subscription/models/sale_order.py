from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    # Periods are a Sales object: hide them from accountants / API users opening the order.
    cw_period_ids = fields.One2many(
        'carwash.subscription.period', 'order_id', 'Car Wash periods',
        groups='sales_team.group_sale_salesman')
    cw_period_count = fields.Integer(
        compute='_compute_cw_period_count', compute_sudo=True,
        groups='sales_team.group_sale_salesman')

    def _compute_cw_period_count(self):
        for order in self:
            order.cw_period_count = len(order.cw_period_ids)

    def action_view_cw_periods(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'wayakit_carwash_subscription.action_carwash_period')
        action['domain'] = [('order_id', '=', self.id)]
        action['context'] = {'default_order_id': self.id}
        return action
