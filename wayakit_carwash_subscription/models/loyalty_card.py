from odoo import fields, models

BOOKING_ANCHOR = '/car-wash-subscriptions#book'


class LoyaltyCard(models.Model):
    _inherit = 'loyalty.card'

    cw_period_id = fields.Many2one('carwash.subscription.period', 'Car Wash period', index=True, ondelete='set null')
    cw_kind = fields.Selection([
        ('included', 'Included'),
        ('monthly_reward', 'Monthly reward'),
        ('streak_reward', 'Streak reward'),
    ], string='Car Wash type')

    def _cw_base_url(self):
        """Root URL for the coupon e-mail's links and images.

        The domain of the website the page lives on (the KSA one) in prod. Staging copies
        have their website domains wiped, so there it is web.base.url and the e-mail points
        at staging.
        """
        self.ensure_one()
        website = self.env['website']._cw_ksa_website()
        return (website.get_base_url() if website else self.get_base_url()).rstrip('/')

    def _cw_booking_url(self):
        """Absolute link of the coupon e-mail's "Book your wash" button."""
        self.ensure_one()
        return self._cw_base_url() + BOOKING_ANCHOR
