{
    'name': 'Wayakit - Car Wash Subscriptions (KSA)',
    'version': '17.0.1.1.0',
    'summary': 'Monthly Car Wash plans: included washes as coupons, streak rewards, monthly gift',
    'description': """
Builds on Odoo Subscriptions + Loyalty:
- Every paid subscription invoice opens a "period" (default 30 days).
- Included services are issued as loyalty coupons that expire at period end (no rollover).
- Rewards (Premium monthly exterior, Regular 3-consecutive-months exterior) are coupons
  valid N days, transferable (anyone with the code can redeem).
- Monthly gift (e.g. disinfectant 100 mL) is recorded per period.
""",
    'author': 'Wayakit',
    'category': 'Sales/Subscriptions',
    'license': 'LGPL-3',
    'depends': ['sale_subscription', 'sale_loyalty'],
    'data': [
        'security/ir.model.access.csv',
        'security/carwash_security.xml',
        'views/product_template_views.xml',
        'views/carwash_period_views.xml',
        'views/sale_order_views.xml',
        'views/actions.xml',
    ],
    'installable': True,
    'application': False,
}
