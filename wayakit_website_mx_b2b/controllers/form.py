import logging

from odoo.http import request

from odoo.addons.website.controllers.form import WebsiteForm

_logger = logging.getLogger(__name__)


class WkB2bMxWebsiteForm(WebsiteForm):

    def insert_attachment(self, model, id_record, files):
        # None of the B2B MX forms has a file input, and mail.mail sends files right away, From
        # the company address, to the internal recipients: drop anything a visitor posts.
        if files and model.sudo().model == 'mail.mail' and request.website \
                and request.website._wk_b2b_mx_is_this_website():
            _logger.warning(
                "wayakit_website_mx_b2b: %s file(s) posted to a form of website %s ignored",
                len(files), request.website.id,
            )
            return
        return super().insert_attachment(model, id_record, files)
