import json
import logging
import re

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

FORM_PAGES = (
    'page_inicio',
    'page_aviacion',
    'page_transporte',
    'page_hospitales',
    'page_restaurantes',
    'page_cotizar',
)
PRIVACY_URL = 'https://wayakit.mx/pages/aviso-de-privacidad'
# The "Leer aviso" link of the forms, still pointing to "#".
LINK = re.compile(r'<a\b([^>]*?)\bhref="#"([^>]*)>(\s*Leer aviso\s*)</a>')


def _fix(match):
    before, after, text = match.groups()
    attrs = before + 'href="%s"' % PRIVACY_URL + after
    if 'target=' not in attrs:
        attrs += ' target="_blank"'
    if 'rel=' not in attrs:
        attrs += ' rel="noopener"'
    return '<a%s>%s</a>' % (attrs, text)


def migrate(cr, version):
    """1.1.2: the forms' "Leer aviso" link points to the privacy notice, patched in place.

    The pages are noupdate and may hold website-editor edits: only that link changes, in every
    stored language. Idempotent: a patched link no longer has href="#". The footer link lives
    in views/layout.xml (not noupdate) and is updated by the upgrade itself.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    ids = [v.id for v in (env.ref('wayakit_website_mx_b2b.%s' % n, raise_if_not_found=False) for n in FORM_PAGES) if v]
    if not ids:
        return
    cr.execute("SELECT id, arch_db FROM ir_ui_view WHERE id IN %s", [tuple(ids)])
    patched = 0
    for view_id, arch_db in cr.fetchall():
        new = {lang: LINK.sub(_fix, arch) if isinstance(arch, str) else arch for lang, arch in (arch_db or {}).items()}
        if new != arch_db:
            cr.execute("UPDATE ir_ui_view SET arch_db = %s::jsonb WHERE id = %s", [json.dumps(new), view_id])
            patched += 1
    env['ir.ui.view'].invalidate_model(['arch_db'])
    env.registry.clear_cache('templates')
    _logger.info("wayakit_website_mx_b2b: privacy link patched on %s of %s page views", patched, len(ids))
