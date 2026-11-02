# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .channel_prefix import is_valid_prefix, prefixed_name

_logger = logging.getLogger(__name__)


class WhatsAppAccount(models.Model):
    _inherit = "whatsapp.account"

    channel_prefix = fields.Char(
        string="Channel Prefix",
        help="Shown as \"[PREFIX] \" in front of the name of this account's "
             "conversations in Discuss, e.g. KSA or MX. Saving it renames the "
             "existing conversations too.")

    @api.constrains("channel_prefix")
    def _check_channel_prefix(self):
        for account in self:
            if not is_valid_prefix(account.channel_prefix):
                raise ValidationError(_(
                    "The channel prefix can only have 1 to 6 letters or digits, e.g. KSA or MX."))

    def write(self, vals):
        if "channel_prefix" not in vals:
            return super().write(vals)
        old_prefixes = {account.id: account.channel_prefix for account in self}
        res = super().write(vals)
        for account in self:
            account._wayakit_apply_channel_prefix(old_prefix=old_prefixes[account.id])
        return res

    def _wayakit_apply_channel_prefix(self, old_prefix=None):
        """Apply each account's prefix to its existing WhatsApp channels.

        Called when channel_prefix is saved and by the server action in
        data/server_action.xml. Idempotent, so running it N times equals running
        it once. Only 'name' is written: no channel is created, deleted or has
        its members touched.
        """
        renamed = total = 0
        for account in self:
            channels = self.env["discuss.channel"].sudo().search([
                ("channel_type", "=", "whatsapp"),
                ("wa_account_id", "=", account.id),
            ])
            total += len(channels)
            for channel in channels:
                new_name = prefixed_name(account.channel_prefix, channel.name, old_prefix)
                if new_name and new_name != channel.name:
                    # Already prefixed here: skip the discuss.channel write override.
                    channel.with_context(wayakit_wa_prefix_done=True).name = new_name
                    renamed += 1
        _logger.info(
            "WhatsApp channel prefix: %s renamed out of %s channels", renamed, total)
        return renamed
