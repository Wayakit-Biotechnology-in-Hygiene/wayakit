# -*- coding: utf-8 -*-
{
    'name': "wayakit_cutomization",

    'summary': """
        Short (1 phrase/line) summary of the module's purpose, used as
        subtitle on modules listing or apps.openerp.com""",

    'description': """
        Long description of module's purpose
    """,

    'author': "My Company",
    'website': "https://www.yourcompany.com",

    # Categories can be used to filter modules in modules listing
    # Check https://github.com/odoo/odoo/blob/16.0/odoo/addons/base/data/ir_module_category_data.xml
    # for the full list
    'category': 'Uncategorized',
    'version': '1.0.41',

    # any module necessary for this one to work correctly
    'depends': ['base', 'product', 'appointment', 'website_appointment', 'appointment_account_payment', 'price_intelligence', 'website', 'website_sale', 'sale', 'stock', 'whatsapp', 'rating', 'website_sale_loyalty'],

    # always loaded
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/mail_template_data.xml',
        'data/loyalty_review_coupon_data.xml',
        'data/whatsapp_review_data.xml',
        'data/whatsapp_channel_name_data.xml',
        'views/website_sale_hide_fields.xml',
        'views/views.xml',
        'views/templates.xml',
        'views/appointment_kitchen_templates.xml',
        'views/ir_cron.xml',
        'views/website_sale_cdmx_filter.xml',
        'views/kit_pets_no_navbar.xml',
        'views/partner_national_short_code.xml',
        'views/sale_order_financials.xml',
        'views/sale_order_national_short_code.xml',
    ],
    # only loaded in demonstration mode
    'demo': [
        'demo/demo.xml',
    ],
    'assets': {
        'web.assets_backend': [],
        'web.assets_frontend': [
            'wayakit_customization/static/src/js/appointment_form.js',
        ],
        'website.assets_frontend': [
                'wayakit_customization/static/src/js/popup_handler.js',
        ],
    }
}
