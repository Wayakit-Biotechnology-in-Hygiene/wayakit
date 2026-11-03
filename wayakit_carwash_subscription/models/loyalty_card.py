from odoo import fields, models


class LoyaltyCard(models.Model):
    _inherit = 'loyalty.card'

    cw_period_id = fields.Many2one('carwash.subscription.period', 'Car Wash period', index=True, ondelete='set null')
    cw_kind = fields.Selection([
        ('included', 'Included'),
        ('monthly_reward', 'Monthly reward'),
        ('streak_reward', 'Streak reward'),
    ], string='Car Wash type')
