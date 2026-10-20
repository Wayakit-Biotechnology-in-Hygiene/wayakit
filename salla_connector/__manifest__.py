{
    "name": "Salla Connector",
    "summary": "Direct Salla (KSA) integration for Wayakit, no middleware: OAuth app "
               "authorization, signed webhooks, SKU reconciliation and order import "
               "(webhook queue + reconcile cron).",
    "category": "Sales",
    "version": "17.0.2.1.3",
    "author": "Wayakit",
    "website": "https://wayakit.com",
    "license": "LGPL-3",
    # sale_stock (not plain stock): from F2 on a confirmed Salla order must produce its
    # delivery and carry warehouse_id. Deliberately independent of the Tecfy modules
    # (odoo_salla_*, models tecfysalla.*): this replaces them and must survive their
    # uninstall. website_sale: backend.website_id (empty by default since
    # 2.1.2, can still be set) and the website fields on imported orders.
    "depends": ["sale_management", "sale_stock", "website_sale"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/ir.model.access.csv",
        "data/salla_data.xml",
        "views/salla_backend_views.xml",
        "views/salla_order_queue_views.xml",
        "views/sale_order_views.xml",
        "data/salla_cron.xml",
    ],
    "application": False,
    "installable": True,
}
