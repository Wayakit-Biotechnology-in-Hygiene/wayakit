from lxml import etree

from odoo import SUPERUSER_ID, api
from odoo.tools import file_open
from odoo.tools.convert import xml_import

MODULE = 'wayakit_carwash_subscription'
TEMPLATE_FILE = 'data/mail_template_data.xml'
TEMPLATE_ID = 'mail_template_cw_coupon'


def migrate(cr, version):
    """1.2.1: reload the redesigned Car Wash coupon e-mail from the XML.

    The template is noupdate, so a plain -u never rewrites it, and flipping
    ir_model_data.noupdate does not help either: convert skips existing records of a
    noupdate block before that flag is ever read. So only this <record> is re-imported in
    'init' mode, which writes it; the <function> of the same file is not re-run. Any UI edit
    of the template is overwritten (none exist as of 1.2.0). Idempotent.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    with file_open(f'{MODULE}/{TEMPLATE_FILE}', 'rb') as fp:
        root = etree.parse(fp).getroot()
    record = root.xpath(f"//record[@id='{TEMPLATE_ID}']")[0]
    data = etree.Element('data', noupdate='1')
    data.append(record)
    xml_import(env, MODULE, {}, mode='init', noupdate=True, xml_filename=TEMPLATE_FILE).parse(data)

    # body_html is translated: the load only writes en_US, so a stored copy for another
    # language (ar_001 on KSA) would keep sending the old body to those customers.
    template = env.ref(f'{MODULE}.{TEMPLATE_ID}')
    cr.execute("""
        UPDATE mail_template SET body_html = jsonb_build_object('en_US', body_html->'en_US') WHERE id = %s
    """, [template.id])
    template.invalidate_recordset(['body_html'])
