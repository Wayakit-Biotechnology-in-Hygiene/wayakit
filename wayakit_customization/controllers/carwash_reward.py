# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request

CARWASH_APPOINTMENT_URL = '/appointment/1'


class CarwashRewardController(http.Controller):

    @http.route('/carwash/reward/<string:code>', type='http', auth='public',
                website=True, sitemap=False)
    def carwash_reward(self, code, **kw):
        """Landing of the WhatsApp "free wash" button (template 2, {{1}} = card code).

        Only remembers the card in the session; the reward is applied on the
        draft SO that the booking controller creates
        (controllers.py, _handle_appointment_form_submission). Always redirects
        to the booking page, even for an unknown code, so the customer can
        still book and the response never tells whether a code exists.
        """
        card = request.env['loyalty.card'].sudo().search([
            ('code', '=', code),
            ('program_id.program_type', '=', 'loyalty'),
            ('program_id.active', '=', True),
        ], limit=1)
        if card:
            request.session['carwash_reward_card_id'] = card.id
        return request.redirect(CARWASH_APPOINTMENT_URL)
