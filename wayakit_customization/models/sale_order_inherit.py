# -*- coding: utf-8 -*-
import logging

from odoo import fields, models
from odoo.exceptions import UserError

from .review_text import DEFAULT_BASE_URL, REVIEW_ANCHOR, build_review_text

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    x_national_short_code = fields.Char(
        related='partner_id.x_national_short_code',
        string="National Address Short Code",
        readonly=False,
        store=False,
        help="Saudi National Address short code of the customer (captured via WhatsApp). "
             "Editing it here writes back to the customer record and is reused across orders.",
    )

    def _get_confirmation_template(self):
        """Send the B2B confirmation template when the channel is B2B.

        "Channel" on the SO form is the Studio field x_studio_channel ('B2B'/'B2C'),
        NOT the UTM medium_id: 1047 orders carry x_studio_channel, only 6 carry
        medium_id. Studio fields live in the DB and never reach git, hence the
        _fields guard.

        Also covers the manual "Send by Email" button on a confirmed order,
        which routes here through _find_mail_template().
        """
        self.ensure_one()
        template = self.env.ref(
            'wayakit_customization.mail_template_sale_confirmation_b2b',
            raise_if_not_found=False,
        )
        if not template or 'x_studio_channel' not in self._fields:
            # Data file never loaded, or the Studio field was renamed/dropped.
            # Without this log the B2C fallback below is silent.
            _logger.warning(
                "B2B confirmation template disabled on %s: template=%s, has x_studio_channel=%s",
                self.name, template, 'x_studio_channel' in self._fields,
            )
        elif self.x_studio_channel == 'B2B':
            return template
        else:
            _logger.info(
                "%s: channel %r is not B2B, using default confirmation template",
                self.name, self.x_studio_channel,
            )
        # ponytail: a missing/deleted record degrades to B2C, never blocks confirmation.
        return super()._get_confirmation_template()

    def _wayakit_review_text(self):
        """One-line 'Product: review link' list for this order, '' if nothing to review.

        Filter agreed on the ticket: skip delivery lines and keep only products
        whose template has a website_url (an unpublished product has no review
        page). Deduplicated by product.template so two variants of the same
        product give one link.
        """
        self.ensure_one()
        base_url = (self.website_id.get_base_url() if self.website_id else DEFAULT_BASE_URL).rstrip('/')
        items, seen = [], set()
        for line in self.order_line:
            # wayakit_customization does not depend on `delivery`, so is_delivery
            # may not exist on this database's sale.order.line.
            if line.display_type or getattr(line, 'is_delivery', False):
                continue
            tmpl = line.product_id.product_tmpl_id
            if not tmpl or tmpl.id in seen or not tmpl.website_url:
                continue
            seen.add(tmpl.id)
            items.append((tmpl.name, '%s%s%s' % (base_url, tmpl.website_url, REVIEW_ANCHOR)))
        return build_review_text(items)

    def wayakit_send_review_whatsapp(self):
        """Server Action entry point: post-purchase review request over WhatsApp.

        Called from ir.actions.server `action_send_review_whatsapp` (marketing
        campaign "Whatsapp Feedback + Coupon", ~14 days after the order). Runs in
        parallel with the existing review email, it does not replace it.
        """
        template = self.env.ref(
            'wayakit_customization.whatsapp_template_review_feedback',
            raise_if_not_found=False,
        )
        if not template:
            _logger.warning(
                "Review WhatsApp skipped: xmlid whatsapp_template_review_feedback not found")
            return
        for order in self:
            text = order._wayakit_review_text()
            if not text:
                _logger.info("%s: no reviewable product, no WhatsApp sent", order.name)
                continue
            # Template 38 applies to sale.order: send on the order itself, it is
            # what resolves phone_field 'partner_id.mobile' and {{1}} 'partner_id.name'.
            order._wayakit_send_whatsapp(template, [text])

    def _wayakit_carwash_note(self, text):
        """Chatter note with a fixed prefix so sales can filter them."""
        self.message_post(body="Car Wash Loyalty: %s" % text)

    def _wayakit_apply_carwash_reward(self, card):
        """Claim the free Exterior wash of `card` on this draft booking SO.

        Called by the booking controller when the customer came through the
        WhatsApp link /carwash/reward/<code>. Same result as the manual process:
        apply the product reward, then remove the booked wash so the customer
        does not pay it twice. Every exit leaves a "Car Wash Loyalty:" note.
        Raises UserError if the reward does not survive the recompute; the
        caller runs this in a savepoint so the booking itself is never lost.
        """
        self.ensure_one()
        if not card:
            return False
        if card.partner_id.commercial_partner_id != self.partner_id.commercial_partner_id:
            self._wayakit_carwash_note(
                "WhatsApp reward link of card %s (%s) used by another customer, "
                "reward not applied." % (card.code, card.partner_id.display_name))
            return False

        rewards = card.program_id.reward_ids.filtered(lambda r: r.reward_type == 'product')
        booked_line = self.order_line.filtered(
            lambda l: not l.reward_id and l.product_id in rewards.reward_product_id)[:1]
        if not booked_line:
            self._wayakit_carwash_note(
                "customer came from the WhatsApp reward link but booked a "
                "non-eligible wash, check manually.")
            return False
        reward = rewards.filtered(lambda r: r.reward_product_id == booked_line.product_id)[:1]

        # card.points, not _get_real_points_for_coupon: the latter also counts
        # the point this booking would earn, and that point disappears with
        # the booked line we remove below.
        if card.points < reward.required_points:
            self._wayakit_carwash_note(
                "card %s has %s points, %s needed; reward not applied."
                % (card.code, card.points, reward.required_points))
            return False

        self._update_programs_and_rewards()
        claimable = self._get_claimable_rewards(forced_coupons=card)
        if reward not in claimable.get(card, self.env['loyalty.reward']):
            self._wayakit_carwash_note(
                "%s is not claimable with card %s (%s points), reward not applied."
                % (reward.description, card.code, card.points))
            return False

        # Core only auto-loads cards whose partner_id is exactly the SO
        # partner, and action_confirm recomputes rewards dropping lines whose
        # card is not on the order (sale_loyalty/models/sale_order.py:868).
        self.applied_coupon_ids |= card
        result = self._apply_program_reward(reward, card)
        if result.get('error'):
            self._wayakit_carwash_note("reward not applied: %s" % result['error'])
            return False

        if booked_line.product_uom_qty > 1:
            booked_line.product_uom_qty -= 1
        else:
            booked_line.unlink()
        # The free wash earns no point: refresh this order's point entry
        self._update_programs_and_rewards()
        if reward not in self.order_line.reward_id:
            raise UserError("Car Wash Loyalty: reward line dropped after removing the booked wash on %s" % self.name)

        self._wayakit_carwash_note(
            "Free %s applied from WhatsApp reward link." % reward.reward_product_id.display_name)
        return True
