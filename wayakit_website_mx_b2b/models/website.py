import logging

from odoo import _, api, models, tools
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

MODULE = 'wayakit_website_mx_b2b'
WEBSITE_NAME = 'B2B MX'
HOME_URL = '/inicio'
LAYOUT_XMLID = MODULE + '.layout_b2b_mx'


class Website(models.Model):
    _inherit = 'website'

    @api.model
    def _wk_b2b_mx_get_website(self):
        websites = self.search([('name', '=', WEBSITE_NAME)])
        if len(websites) == 1:
            return websites
        # Creating the website is only for test / fresh DBs (just the default website).
        # Anywhere else a missing or duplicated name must stop the install/upgrade: binding
        # to a new or wrong website would move Mafer's pages and header off the real one,
        # and not binding would publish the pages on every website.
        if not websites and (tools.config['test_enable'] or self.search_count([]) <= 1):
            vals = {'name': WEBSITE_NAME}
            lang = self.env['res.lang']._lang_get('es_MX')
            if lang and lang.active:
                vals.update(language_ids=[(6, 0, lang.ids)], default_lang_id=lang.id)
            _logger.info("%s: website %r not found on a fresh DB, creating it", MODULE, WEBSITE_NAME)
            return self.create(vals)
        raise UserError(_(
            "%(module)s needs exactly one website named %(name)r, found %(count)s. "
            "Rename the B2B Mexico website back to %(name)r (or the duplicates to something else) "
            "and upgrade again.",
            module=MODULE, name=WEBSITE_NAME, count=len(websites),
        ))

    @api.model
    def _wk_b2b_mx_bind(self):
        """Bind the pages and website views of this module to the "B2B MX" website.

        Called from data/website_bind.xml on every install and upgrade, so it must stay
        idempotent. Ids are never hardcoded: the website is looked up by name.
        """
        website = self._wk_b2b_mx_get_website()
        data = self.env['ir.model.data'].sudo().search([
            ('module', '=', MODULE),
            ('model', 'in', ('ir.ui.view', 'website.page', 'website.menu')),
        ])
        by_model = {}
        for xmlid in data:
            by_model.setdefault(xmlid.model, []).append(xmlid.res_id)
        Views = self.env['ir.ui.view'].sudo().with_context(active_test=False, no_cow=True)
        # Only frontend (qweb) views: a backend view bound to a website would never apply.
        views = Views.browse(by_model.get('ir.ui.view', [])).exists().filtered(lambda v: v.type == 'qweb')
        pages = self.env['website.page'].sudo().browse(by_model.get('website.page', [])).exists()
        menus = self.env['website.menu'].sudo().browse(by_model.get('website.menu', [])).exists()
        layout = self.env.ref(LAYOUT_XMLID).sudo().with_context(active_test=False, no_cow=True)
        # The layout view is generic only right after the XML created it (first install, or
        # re-created after Odoo COW-moved it): that is when it must be switched on. On a plain
        # upgrade a deliberate archive or homepage change is left as it is.
        first_bind = not layout.website_id
        # After a COW of website.layout, Odoo keeps our layout as an xmlid-less copy (with its
        # active flag) and the upgrade recreates the xmlid one: that is not a first install,
        # so the copy's state wins and homepage_url is left alone.
        stale = Views.search([
            ('key', '=', layout.key),
            ('website_id', '=', website.id),
            ('id', '!=', layout.id),
        ])
        recreated = first_bind and bool(stale)
        stale_active = stale[:1].active

        # website_id first, active afterwards: an inherited view is never active while generic.
        views.filtered(lambda v: v.website_id != website).write({'website_id': website.id})
        # website.page.website_id is related to its view's: normally already done above.
        pages.filtered(lambda p: p.website_id != website).write({'website_id': website.id})
        menus.filtered(lambda m: m.website_id != website).write({'website_id': website.id})

        for view in views.filtered('inherit_id'):
            # If this website has its own copy of the parent (COW), the child must hang from
            # that copy, otherwise Odoo never applies it when rendering this website.
            # A module upgrade puts inherit_id back on the generic parent; this fixes it again.
            parents = Views.search([
                ('key', '=', view.inherit_id.key),
                ('website_id', 'in', (False, website.id)),
            ])
            parent = parents.filtered('website_id')[:1] or parents[:1]
            if parent and parent != view.inherit_id:
                view.write({'inherit_id': parent.id})
            # When Odoo COWs the parent for this website it moves our view by copying it
            # (without xmlid) and deleting the original; the upgrade then recreates the
            # xmlid one. Drop such copies or the header would be rendered twice.
            Views.search([
                ('key', '=', view.key),
                ('website_id', '=', website.id),
                ('id', '!=', view.id),
            ]).unlink()
        if first_bind:
            layout.write({'active': stale_active if recreated else True})

        # Two pages with the same URL on one website make Odoo serve either of them.
        clash = self.env['website.page'].sudo().search([
            ('website_id', '=', website.id),
            ('url', 'in', pages.mapped('url')),
            ('id', 'not in', pages.ids),
        ])
        if clash:
            _logger.warning(
                "%s: website %s already has other pages on %s; rename or unpublish them",
                MODULE, website.id, ', '.join(sorted(set(clash.mapped('url')))),
            )
        # Only a warning, nothing is written (team settings are changed by hand in the UI).
        # Form leads get the website's default sales team, and a lead's company follows its
        # team: a team of another company would put Mexican prospects in that company's CRM.
        team_company = website.crm_default_team_id.company_id
        if team_company and team_company != website.company_id:
            _logger.warning(
                "%s: website %s default sales team belongs to %s, not to the website's company %s",
                MODULE, website.id, team_company.name, website.company_id.name,
            )

        # "/" serves our home only when nobody chose another homepage for this website;
        # the existing "/" page is left untouched.
        if first_bind and not recreated and not website.homepage_url:
            website.homepage_url = HOME_URL
        _logger.info(
            "%s: bound %s views, %s pages, %s menus to website %s (first bind: %s, recreated: %s)",
            MODULE, len(views), len(pages), len(menus), website.id, first_bind, recreated,
        )
        return True
