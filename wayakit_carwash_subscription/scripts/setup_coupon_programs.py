"""Create the 6 Car Wash coupon programs in KSA (company_id=1). Idempotent.

Run on staging first, from the odoo.sh shell:
    odoo-bin shell -d <db> < wayakit_carwash_subscription/scripts/setup_coupon_programs.py

Each program = "coupons" type, reward = the free service. Coupons are created by the module
(one per period), so no rules are needed. Coupon email is sent on creation.
"""
COMPANY_ID = 1  # Advanced Solutions for Detergents LLC (KSA)

SERVICES = {
    'CW Sub – Sedan Exterior': 'FP-CWS-00100',
    'CW Sub – Sedan Full Wash': 'FP-CWS-00101',
    'CW Sub – Sedan Deep Clean': 'FP-CWS-00102',
    'CW Sub – SUV Exterior': 'FP-CWS-00200',
    'CW Sub – SUV Full Wash': 'FP-CWS-00201',
    'CW Sub – SUV Deep Clean': 'FP-CWS-00202',
}

Program = env['loyalty.program'].with_company(COMPANY_ID)
mail_tmpl = env.ref('loyalty.mail_template_loyalty_card', raise_if_not_found=False)

for name, code in SERVICES.items():
    product = env['product.product'].search(
        [('default_code', '=', code), ('company_id', 'in', [COMPANY_ID, False])], limit=1)
    if not product:
        print('!! product not found:', code)
        continue
    if Program.search([('name', '=', name), ('company_id', '=', COMPANY_ID)], limit=1):
        print('= exists:', name)
        continue
    vals = {
        'name': name,
        'program_type': 'coupons',
        'trigger': 'with_code',
        'applies_on': 'current',
        'company_id': COMPANY_ID,
        'rule_ids': [(5, 0, 0)],
        'reward_ids': [(5, 0, 0), (0, 0, {
            'reward_type': 'product',
            'reward_product_id': product.id,
            'reward_product_qty': 1,
            'required_points': 1,
        })],
    }
    if mail_tmpl:
        vals['communication_plan_ids'] = [(5, 0, 0), (0, 0, {
            'trigger': 'create', 'mail_template_id': mail_tmpl.id})]
    prog = Program.create(vals)
    print('+ created:', name, '->', prog.id)

env.cr.commit()
