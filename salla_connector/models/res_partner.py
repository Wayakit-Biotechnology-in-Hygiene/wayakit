from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    # Indexed, NOT unique: res.partner is written by every app in the DB, and a unique
    # index would turn a connector bug into a hard error on unrelated contact creation
    # (same call as trendyol_customer_ref).
    salla_customer_id = fields.Char(string="Salla Customer ID", copy=False, index=True)
