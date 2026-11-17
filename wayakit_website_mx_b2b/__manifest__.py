{
    'name': 'Wayakit - B2B MX Website',
    'version': '17.0.1.1.3',
    'summary': 'Pages, header and footer of the "B2B MX" website (approved design), '
               'quote/DEMO forms sent by e-mail',
    'description': """
Ports the approved static design of the B2B Mexico site to the existing Odoo website
"B2B MX": one website.page per page (/inicio as home, industries, products, science,
sustainability, about, press, quote), the design's header and footer, and the quote /
DEMO forms, sent by e-mail (mail.mail) to the addresses of the system parameter
wayakit_website_mx_b2b.form_email_to (never in the page).

Everything is bound to "B2B MX" by website._wk_b2b_mx_bind (looked up by name, no ids):
other websites of the database are not affected.
""",
    'author': 'Wayakit',
    'website': 'https://wayakit.com',
    'category': 'Website/Website',
    'license': 'LGPL-3',
    'depends': ['website'],
    'data': [
        'views/layout.xml',
        'views/page_inicio.xml',
        'views/page_aviacion.xml',
        'views/page_transporte.xml',
        'views/page_hospitales.xml',
        'views/page_restaurantes.xml',
        'views/page_productos.xml',
        'views/page_ciencia.xml',
        'views/page_nosotros.xml',
        'views/page_sustentabilidad.xml',
        'views/page_cotizar.xml',
        'views/page_prensa.xml',
        # Last: binds everything above to the B2B MX website.
        'data/website_bind.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'wayakit_website_mx_b2b/static/src/css/base.css',
            'wayakit_website_mx_b2b/static/src/css/design.css',
            'wayakit_website_mx_b2b/static/src/js/wk_b2b_mx.js',
        ],
    },
    'uninstall_hook': 'uninstall_hook',
    'installable': True,
    'application': False,
}
