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

    def _get_reward_values_product(self, reward, coupon, product=None, **kwargs):
        """One wash per code application on Car Wash subscription coupons.

        Core claims every multiple of required_points at once
        (sale_loyalty/models/sale_order.py:145): a card with 8 washes gave one
        line of 8 and spent the whole period. Cap it so the rest stays on the
        card. Website cart and backend wizard both land here, and so does the
        recompute in _update_programs_and_rewards, so the cap sticks.
        """
        vals_list = super()._get_reward_values_product(reward, coupon, product=product, **kwargs)
        if coupon.cw_period_id and not reward.clear_wallet:
            for vals in vals_list:
                vals['product_uom_qty'] = reward.reward_product_qty
                vals['points_cost'] = reward.required_points
        return vals_list

    def _get_claimable_and_showable_rewards(self):
        """Hide the cart "Claim" box of a Car Wash coupon already on the order.

        The leftover points keep it claimable, but the box posts the coupon
        code back to /shop/pricelist, which answers "already applied". The
        backend Promotions button reads _get_claimable_rewards, left as core,
        so sales can still add a second wash there.
        """
        res = super()._get_claimable_and_showable_rewards()
        applied = self.order_line.filtered('is_reward_line').coupon_id
        for coupon in [c for c in res if c.cw_period_id and c in applied]:
            del res[coupon]
        return res
