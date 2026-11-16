import logging

from odoo import models, tools

from .website import MODULE

_logger = logging.getLogger(__name__)

RECIPIENTS_PARAM = MODULE + '.form_email_to'
# The subjects our forms send (hidden "subject" inputs of views/page_*.xml). Anything else
# posted on B2B MX gets DEFAULT_SUBJECT: the e-mail goes out From the company address, its
# subject must not be text chosen by an anonymous visitor.
ALLOWED_SUBJECTS = {
    'B2B MX web — Cotización',
    'B2B MX web — Cotización Aviación',
    'B2B MX web — Cotización Transporte y flotillas',
    'B2B MX web — Cotización Hospitales',
    'B2B MX web — Cotización Restaurantes',
    'B2B MX web — DEMO',
}
DEFAULT_SUBJECT = 'B2B MX web — Formulario'


class MailMail(models.Model):
    _inherit = 'mail.mail'

    def website_form_input_filter(self, request, values):
        """Recipients and headers of the B2B MX website forms ("send an e-mail" forms).

        The addresses never appear in the page (the HTML is public and would be scraped):
        they come from the system parameter wayakit_website_mx_b2b.form_email_to, set by hand.
        On B2B MX any recipient sent by the browser is overwritten, so no form of that website
        can be used to send mail to arbitrary addresses. Other websites are left untouched.
        """
        parent = getattr(super(), 'website_form_input_filter', None)
        if parent:
            values = parent(request, values)
        website = request.website
        if not website or not website._wk_b2b_mx_is_this_website():
            return values
        param = self.env['ir.config_parameter'].sudo().get_param(RECIPIENTS_PARAM, '').strip()
        email_to = tools.email_split_and_format(param)
        if not email_to:
            # Empty or without any valid address (typo): the company inbox rather than nobody.
            # The value itself is never logged.
            email_to = tools.email_split_and_format(website.company_id.email or '')
            _logger.warning(
                "%s: system parameter %s is %s, form of website %s sent to the company address",
                MODULE, RECIPIENTS_PARAM, 'invalid' if param else 'empty', website.id,
            )
        values['email_to'] = ','.join(email_to)
        if not values['email_to']:
            # The visitor still sees the thank-you message: make the lost submission visible.
            _logger.error(
                "%s: no recipient for the forms of website %s (set %s or the company e-mail)",
                MODULE, website.id, RECIPIENTS_PARAM,
            )
        values.pop('email_cc', None)
        values.pop('email_bcc', None)
        if values.get('subject') not in ALLOWED_SUBJECTS:
            values['subject'] = DEFAULT_SUBJECT
        # Reply-To: one well-formed address or none (the raw value stays in the body, where
        # the form controller lists it as "email").
        if 'email_from' in values:
            values['email_from'] = tools.email_normalize(values['email_from'] or '') or False
        # Our forms never send a body: the body is the list of fields, escaped by the controller.
        values.pop('body_html', None)
        return values
