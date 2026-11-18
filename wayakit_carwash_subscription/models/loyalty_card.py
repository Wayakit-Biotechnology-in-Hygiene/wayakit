from odoo import fields, models

BOOKING_ANCHOR = '/car-wash-subscriptions#book'
# KSA WhatsApp Business number (digits only). Odoo's WhatsApp account stores Meta ids, not
# the number, so it is kept here; a system parameter overrides it without a code change.
WHATSAPP_PARAM = 'wayakit_carwash_subscription.whatsapp_number'
WHATSAPP_DEFAULT = '966545754908'


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

    def _cw_whatsapp_digits(self):
        value = self.env['ir.config_parameter'].sudo().get_param(WHATSAPP_PARAM) or WHATSAPP_DEFAULT
        return ''.join(ch for ch in value if ch.isdigit())

    def _cw_whatsapp_url(self):
        """wa.me link of the e-mail's "message us on WhatsApp" line."""
        return 'https://wa.me/' + self._cw_whatsapp_digits()

    def _cw_whatsapp_display(self):
        """'+966 54 575 4908' for a Saudi mobile, '+<digits>' otherwise."""
        d = self._cw_whatsapp_digits()
        if len(d) == 12 and d.startswith('966'):
            return '+966 %s %s %s' % (d[3:5], d[5:8], d[8:])
        return '+' + d

    # Car Wash cards get their e-mail WITHOUT Odoo's notification layout: core wraps it in
    # mail.mail_notification_light, which adds "Your Loyalty Coupon", the card name (the
    # internal "CW Sub – …" program) and the Odoo footer. Our template is the whole e-mail.
    # _send_points_reach_communication is left alone: CW coupons only lose points (one per
    # wash) and their programs have no "When reaching" plan, so it never sends for them.

    def _cw_is_carwash_card(self):
        """Issued by a Car Wash period, or on a program whose creation e-mail is ours.

        The second case covers cards made by hand on a "CW Sub – …" program (e.g. Generate
        coupons): our template is a full e-mail and must never be wrapped either.
        """
        self.ensure_one()
        if self.cw_period_id:
            return True
        template = self.env.ref('wayakit_carwash_subscription.mail_template_cw_coupon', raise_if_not_found=False)
        create_comms = self.program_id.communication_plan_ids.filtered(lambda c: c.trigger == 'create')
        return bool(template) and template in create_comms.mail_template_id

    def _send_creation_communication(self, force_send=False):
        cw_cards = self.filtered(lambda c: c._cw_is_carwash_card())
        super(LoyaltyCard, self - cw_cards)._send_creation_communication(force_send=force_send)
        if not cw_cards or self.env.context.get('loyalty_no_mail') or self.env.context.get('action_no_send_mail'):
            return
        # Same loop as core, minus email_layout_xmlid. Every 'create' line goes out bare, so a
        # second hand-picked template next to ours would too; no CW program has one.
        for card in cw_cards:
            if not card._get_mail_partner():
                continue
            for comm in card.program_id.communication_plan_ids.filtered(lambda c: c.trigger == 'create'):
                template = comm.mail_template_id
                email_values = {}
                if not template.email_from:
                    author = card._get_mail_author()
                    email_values.update(author_id=author.id, email_from=author.email_formatted)
                template.send_mail(res_id=card.id, force_send=force_send, email_values=email_values)

    def action_coupon_send(self):
        action = super().action_coupon_send()
        if self._cw_is_carwash_card():
            # Dropping the layout is not enough in 'comment' mode: message_post falls back to
            # the full mail.mail_notification_layout. 'mass_mail' sends the rendered template
            # as-is (and still logs the e-mail on the card). mass_mail also drops blacklisted
            # addresses (newsletter unsubscribes), but this is the paid coupon, not marketing.
            action['context'].update(default_composition_mode='mass_mail', default_email_layout_xmlid=False,
                                     default_use_exclusion_list=False)
        return action
