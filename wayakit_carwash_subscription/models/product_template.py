from odoo import api, fields, models
from odoo.exceptions import ValidationError


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    cw_is_subscription = fields.Boolean(
        'Car Wash Subscription',
        help='Paid invoices of this subscription product issue Car Wash coupons and the monthly gift.')
    cw_tier = fields.Selection(
        [('regular', 'Regular Care'), ('premium', 'Premium Care')], string='Tier')
    cw_period_days = fields.Integer('Period length (days)', default=30)
    cw_reward_validity_days = fields.Integer(
        'Reward validity (days)', default=30,
        help='Validity of monthly and streak rewards (not of included services).')
    cw_streak_periods = fields.Integer(
        'Streak: consecutive paid periods', default=3,
        help='Regular Care: a streak reward is issued every N consecutive paid periods.')
    cw_grace_days = fields.Integer(
        'Streak: renewal grace (days)', default=7,
        help='Payment must happen at most this many days after the previous period ends to keep the streak.')
    cw_gift_product_id = fields.Many2one(
        'product.product', 'Monthly gift', help='e.g. Disinfectant 100 mL, recorded on every period.')
    cw_gift_qty = fields.Float('Gift qty per period', default=1.0)
    cw_entitlement_ids = fields.One2many(
        'carwash.subscription.entitlement', 'product_tmpl_id', 'Entitlements',
        groups='sales_team.group_sale_salesman')

    @api.constrains('cw_tier', 'cw_entitlement_ids')
    def _check_cw_premium_no_streak(self):
        for tmpl in self:
            if tmpl.cw_tier == 'premium' and tmpl.sudo().cw_entitlement_ids.filtered(lambda e: e.kind == 'streak_reward'):
                raise ValidationError('Premium Care cannot have streak rewards (it already gets a monthly reward).')


class CarwashSubscriptionEntitlement(models.Model):
    _name = 'carwash.subscription.entitlement'
    _description = 'Car Wash subscription entitlement'
    _order = 'product_tmpl_id, sequence, id'

    sequence = fields.Integer(default=10)
    product_tmpl_id = fields.Many2one('product.template', required=True, ondelete='cascade')
    product_id = fields.Many2one(
        'product.product', 'Only for variant',
        domain="[('product_tmpl_id', '=', product_tmpl_id)]",
        help='Leave empty to apply to every variant (e.g. set Sedan vs SUV programs when using variants).')
    kind = fields.Selection([
        ('included', 'Included service (expires at period end)'),
        ('monthly_reward', 'Monthly reward (valid N days)'),
        ('streak_reward', 'Streak reward (every N consecutive periods)'),
    ], required=True, default='included')
    program_id = fields.Many2one(
        'loyalty.program', 'Coupon program', required=True,
        domain="[('program_type', '=', 'coupons')]",
        help='Coupons program whose reward is the free service.')
    quantity = fields.Integer('Services', default=1, required=True)

    def _applies_to(self, product):
        return self.filtered(lambda e: not e.product_id or e.product_id == product)
