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
TEL_INPUT = re.compile(r'<input\b[^>]*\btype="tel"[^>]*>')


def _patch(arch):
    # Only the type attribute of phone inputs changes: Odoo's form widget prefills every empty
    # input[type=tel] with "+<geoip country code>". Everything else in the arch is untouched.
    return TEL_INPUT.sub(
        lambda m: m.group(0).replace('type="tel"', 'type="text" inputmode="tel" autocomplete="tel"'),
        arch,
    )


def migrate(cr, version):
    """1.1.1: phone inputs become type=text inputmode=tel, patched in place.

    The pages are noupdate and may hold website-editor edits: unlike the 1.1.0 migration this
    does not reset the arch, it rewrites only that attribute, in every stored language.
    Idempotent: a patched arch has no type="tel" left.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    ids = [v.id for v in (env.ref('wayakit_website_mx_b2b.%s' % n, raise_if_not_found=False) for n in FORM_PAGES) if v]
    if not ids:
        return
    cr.execute("SELECT id, arch_db FROM ir_ui_view WHERE id IN %s", [tuple(ids)])
    patched = 0
    for view_id, arch_db in cr.fetchall():
        new = {lang: _patch(arch) if isinstance(arch, str) else arch for lang, arch in (arch_db or {}).items()}
        if new != arch_db:
            cr.execute("UPDATE ir_ui_view SET arch_db = %s::jsonb WHERE id = %s", [json.dumps(new), view_id])
            patched += 1
    env['ir.ui.view'].invalidate_model(['arch_db'])
    env.registry.clear_cache('templates')
    _logger.info("wayakit_website_mx_b2b: phone inputs patched on %s of %s page views", patched, len(ids))
