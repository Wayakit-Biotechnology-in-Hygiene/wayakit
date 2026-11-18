import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

MODULE = 'wayakit_carwash_subscription'
# Looked up by exact name, never by id: company 1 owns three websites in prod
# (My Website, KSA Website, Kit Pets KSA Website).
KSA_WEBSITE_NAME = 'KSA Website'
KSA_COMPANY_ID = 1
PAGE_XMLID = MODULE + '.page_cw_subscriptions_page'
# Subscription products behind the page's "Subscribe" buttons.
CW_PLAN_CODES = (
    'FP-CWS-00103', 'FP-CWS-00104', 'FP-CWS-00203', 'FP-CWS-00204',
    'FP-CWS-00105', 'FP-CWS-00205',
)
# Created by hand in the UI (duplicate of "Car Wash Care", required subscription code).
BOOKING_APPOINTMENT_NAME = 'Car Wash Subscription'
BOOKING_FALLBACK_URL = '/appointment/1'


class Website(models.Model):
    _inherit = 'website'

    @api.model
    def _cw_ksa_website(self):
        """The KSA website, or an empty recordset if it can't be told apart.

        The website the page is already bound to wins: renaming or duplicating the
        website later must not take the page down or move the e-mail links.
        """
        page = self.env.ref(PAGE_XMLID, raise_if_not_found=False)
        if page and page.sudo().website_id:
            return page.sudo().website_id
        websites = self.sudo().search([('name', '=', KSA_WEBSITE_NAME)])
        if len(websites) == 1:
            return websites
        # A fresh DB only has the default website. Anywhere else a guess could publish
        # the page on the wrong website.
        if not websites and self.sudo().search_count([]) <= 1:
            return self.sudo().search([], order='id', limit=1)
        return self.browse()

    def _cw_plan_urls(self):
        """{default_code: website_url or ''} for the subscription plans on this website.

        Rendered for public visitors, hence sudo; only published, sellable products of KSA
        on this website get a URL, so the page never links to a 404 or a page without cart.
        """
        self.ensure_one()
        products = self.env['product.product'].sudo().search([
            ('default_code', 'in', CW_PLAN_CODES),
            ('company_id', 'in', [KSA_COMPANY_ID, False]),
        ])
        urls = dict.fromkeys(CW_PLAN_CODES, '')
        for product in products:
            tmpl = product.product_tmpl_id
            sellable_here = not tmpl.website_id or tmpl.website_id == self
            if tmpl.is_published and tmpl.sale_ok and sellable_here and not urls[product.default_code]:
                urls[product.default_code] = tmpl.website_url
        return urls

    def _cw_booking_url(self):
        """URL of the "Car Wash Subscription" appointment, else the Car Wash Care one."""
        self.ensure_one()
        # en_US: an Arabic translation of the name must not break the lookup.
        appointment = self.env['appointment.type'].sudo().with_context(lang='en_US').search([
            ('name', '=', BOOKING_APPOINTMENT_NAME),
            ('is_published', '=', True),
            ('website_id', 'in', [False, self.id]),
        ], order='id', limit=1)
        return appointment.website_url or BOOKING_FALLBACK_URL

    @api.model
    def _cw_bind_page(self):
        """Bind the subscriptions page to the KSA website (data/website_bind.xml).

        Runs on every install and upgrade, so it stays idempotent. Once bound the page
        stays where it is (see _cw_ksa_website). Without a KSA website the page is
        unpublished instead of left generic, which would show it on every website
        (B2B MX included).
        """
        page = self.env.ref(PAGE_XMLID, raise_if_not_found=False)
        if not page:
            return False
        page = page.sudo()
        website = self._cw_ksa_website()
        if not website:
            if page.is_published:
                page.is_published = False
            _logger.warning(
                "%s: no single website named %r, page %s left unpublished",
                MODULE, KSA_WEBSITE_NAME, page.url)
            return False
        if page.view_id.website_id != website:
            page.view_id.with_context(no_cow=True).website_id = website
        if page.website_id != website:
            page.website_id = website
        clash = self.env['website.page'].sudo().search([
            ('website_id', 'in', [False, website.id]),
            ('url', '=', page.url),
            ('id', '!=', page.id),
        ])
        if clash:
            _logger.warning("%s: website %s already has another page on %s", MODULE, website.id, page.url)
        return True
