from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """1.2.0: the "CW Sub – …" programs send the Car Wash coupon e-mail at creation.

    The <function> in data/mail_template_data.xml is noupdate, so it only runs on a fresh
    install; databases upgrading from an older version get the switch here. Idempotent,
    company 1 only, and a template picked by hand is kept.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['loyalty.program']._cw_use_coupon_template()
