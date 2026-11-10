{
    "name": "Salla Connector",
    "summary": "Direct Salla (KSA) integration for Wayakit, no middleware: OAuth app "
               "authorization, signed webhooks, SKU reconciliation and order import "
               "(webhook queue + reconcile cron), \"shipped\" pushed back to Salla and "
               "Salla cancellations applied, ZATCA invoices/credit notes created, paid and "
               "uploaded to Salla, bulk product upload from a master sheet + images ZIP "
               "(hidden, upsert by SKU, dry-run first).",
    "category": "Sales",
    "version": "17.0.5.0.1",
    "author": "Wayakit",
    "website": "https://wayakit.com",
    "license": "LGPL-3",
    # sale_stock (not plain stock): from F2 on a confirmed Salla order must produce its
    # delivery and carry warehouse_id. Deliberately independent of the Tecfy modules
    # (odoo_salla_*, models tecfysalla.*): this replaces them and must survive their
    # uninstall. website_sale: backend.website_id (empty by default since
    # 2.1.2, can still be set) and the website fields on imported orders.
    # account_edi (F5): edi_state = ZATCA acceptance, required before an upload to Salla
    # (installed in prod through l10n_sa_edi).
    "depends": ["sale_management", "sale_stock", "website_sale", "account_edi"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/ir.model.access.csv",
        "data/salla_data.xml",
        # Before the backend views: their "Upload Products" menu uses its action.
        "views/salla_product_upload_views.xml",
        "views/salla_backend_views.xml",
        "views/salla_order_queue_views.xml",
        "views/sale_order_views.xml",
        "views/account_move_views.xml",
        "data/salla_cron.xml",
    ],
    "application": False,
    "installable": True,
}
