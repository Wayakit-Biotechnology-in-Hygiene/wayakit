import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

# The six pages that contain a form.
FORM_PAGES = (
    'page_inicio',
    'page_aviacion',
    'page_transporte',
    'page_hospitales',
    'page_restaurantes',
    'page_cotizar',
)


def migrate(cr, version):
    """1.1.0: the forms are sent by e-mail (mail.mail) instead of creating crm.lead records.

    The pages are noupdate, so -u leaves their old forms in place: re-read these six views
    from their XML files. This DISCARDS every website-editor edit made on these six pages
    (in every language); the other five pages are not touched.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    views = env['ir.ui.view']
    for name in FORM_PAGES:
        view = env.ref('wayakit_website_mx_b2b.%s' % name, raise_if_not_found=False)
        if view:
            views |= view
    if not views:
        return
    views.reset_arch('hard')
    # reset_arch writes en_US and maps the old translations onto the new arch; editor edits made
    # in es_MX (the website's language) would survive that way. Keep only the file's arch.
    cr.execute(
        "UPDATE ir_ui_view SET arch_db = jsonb_build_object('en_US', arch_db->'en_US') WHERE id IN %s",
        [tuple(views.ids)],
    )
    views.invalidate_recordset(['arch_db'])
    env.registry.clear_cache('templates')
    _logger.info("wayakit_website_mx_b2b: forms reset on %s page views", len(views))
