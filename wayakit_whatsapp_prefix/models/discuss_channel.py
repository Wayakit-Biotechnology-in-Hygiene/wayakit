# -*- coding: utf-8 -*-
"""Put the WhatsApp account's "[PREFIX] " in front of its channel names.

Hooked on create()/write() rather than on a whatsapp method: those are the end
of the chain (after wayakit_customization puts the number in front and after
enterprise appends " (record_name)"), and they do not depend on enterprise
internals whose signatures already changed once (commit 8fe680ab, July 2026).
"""
from odoo import api, models

from .channel_prefix import prefixed_name


class DiscussChannel(models.Model):
    _inherit = "discuss.channel"

    @api.model_create_multi
    def create(self, vals_list):
        accounts = self.env["whatsapp.account"].sudo()
        for vals in vals_list:
            if vals.get("channel_type") == "whatsapp" and vals.get("wa_account_id"):
                prefix = accounts.browse(vals["wa_account_id"]).channel_prefix
                if prefix:
                    vals["name"] = prefixed_name(prefix, vals.get("name"))
        return super().create(vals_list)

    def write(self, vals):
        """Keep the prefix on renames (Discuss channel_rename goes through here).

        sudo() on the account: Discuss users renaming a channel usually cannot
        read whatsapp.account.
        """
        if "name" not in vals or self.env.context.get("wayakit_wa_prefix_done"):
            return super().write(vals)
        prefixed = self.filtered(
            lambda c: c.channel_type == "whatsapp" and c.wa_account_id.sudo().channel_prefix)
        rest = self - prefixed
        res = super(DiscussChannel, rest).write(vals) if rest else True
        for account in prefixed.wa_account_id:
            channels = prefixed.filtered(lambda c: c.wa_account_id == account)
            name = prefixed_name(account.sudo().channel_prefix, vals["name"])
            super(DiscussChannel, channels).write(dict(vals, name=name))
        return res
