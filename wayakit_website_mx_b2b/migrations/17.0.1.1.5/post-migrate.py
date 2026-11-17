import json
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

# page key: (title written by versions <= 1.1.4, new title)
TITLES = {
    'inicio': ('WAYAKIT B2B · Inicio', 'WAYAKIT México · Limpieza y desinfección para empresas'),
    'aviacion': ('WAYAKIT B2B · Industria · Aviación', 'Aviación · WAYAKIT México'),
    'transporte': ('WAYAKIT B2B · Industria · Transporte y flotillas', 'Transporte y flotillas · WAYAKIT México'),
    'hospitales': ('WAYAKIT B2B · Industria · Hospitales', 'Hospitales · WAYAKIT México'),
    'restaurantes': ('WAYAKIT B2B · Industria · Restaurantes', 'Restaurantes · WAYAKIT México'),
    'productos': ('WAYAKIT B2B · Productos', 'Productos · WAYAKIT México'),
    'ciencia': ('WAYAKIT B2B · Ciencia y registros', 'Ciencia y registros · WAYAKIT México'),
    'sustentabilidad': ('WAYAKIT B2B · Sustentabilidad', 'Sustentabilidad · WAYAKIT México'),
    'nosotros': ('WAYAKIT B2B · Nosotros', 'Nosotros · WAYAKIT México'),
    'cotizar': ('WAYAKIT B2B · Cotizar y contacto', 'Cotizar y contacto · WAYAKIT México'),
    'prensa': ('WAYAKIT B2B · Prensa', 'Prensa · WAYAKIT México'),
}


def migrate(cr, version):
    """1.1.5: new browser-tab titles and the WAYAKIT favicon on B2B MX.

    The pages are noupdate: website_meta_title is rewritten here, per stored language, only
    where it still holds the module's old value (a title edited by hand is kept). The favicon
    is set only if the website still has Odoo's default one.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    titles = 0
    for key, (old, new) in TITLES.items():
        page = env.ref('wayakit_website_mx_b2b.page_%s_page' % key, raise_if_not_found=False)
        if not page:
            continue
        # website.page delegates website_meta_title to its ir.ui.view (_inherits).
        cr.execute("SELECT website_meta_title FROM ir_ui_view WHERE id = %s", [page.view_id.id])
        stored = cr.fetchone()[0] or {}
        updated = {lang: (new if value == old else value) for lang, value in stored.items()}
        if updated != stored:
            cr.execute("UPDATE ir_ui_view SET website_meta_title = %s::jsonb WHERE id = %s", [json.dumps(updated), page.view_id.id])
            titles += 1
    env['ir.ui.view'].invalidate_model(['website_meta_title'])
    env['website.page'].invalidate_model(['website_meta_title'])

    favicon = False
    layout = env.ref('wayakit_website_mx_b2b.layout_b2b_mx', raise_if_not_found=False)
    if layout and layout.website_id:
        favicon = layout.website_id._wk_b2b_mx_set_favicon()
    _logger.info("wayakit_website_mx_b2b: new title on %s of %s pages; favicon set: %s", titles, len(TITLES), favicon)
