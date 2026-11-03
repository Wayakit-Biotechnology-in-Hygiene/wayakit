import logging
from datetime import timedelta

from markupsafe import Markup, escape

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class CarwashSubscriptionPeriod(models.Model):
    _name = 'carwash.subscription.period'
    _description = 'Car Wash subscription period'
    _inherit = ['mail.thread']
    _order = 'period_start desc, id desc'
    order_id = fields.Many2one('sale.order', 'Subscription', required=True, index=True, ondelete='cascade')
    root_order_id = fields.Many2one(
        'sale.order', 'First contract', index=True,
        help='Original subscription (follows renewals) — used to keep the streak.')
    sale_line_id = fields.Many2one('sale.order.line', required=True, ondelete='cascade')
    invoice_id = fields.Many2one('account.move', 'Invoice', required=True, index=True, ondelete='cascade')
    company_id = fields.Many2one(related='order_id.company_id', store=True)
    partner_id = fields.Many2one('res.partner', 'Customer', required=True, index=True)
    product_id = fields.Many2one('product.product', 'Plan', required=True)
    tier = fields.Selection(related='product_id.product_tmpl_id.cw_tier', store=True)
    period_start = fields.Date(required=True)
    period_end = fields.Date(required=True)
    payment_date = fields.Date(required=True)
    streak = fields.Integer('Consecutive paid periods', readonly=True)
    state = fields.Selection(
        [('active', 'Active'), ('expired', 'Expired')], compute='_compute_state', search='_search_state')

    card_ids = fields.One2many('loyalty.card', 'cw_period_id', 'Coupons')
    services_left = fields.Float(compute='_compute_services_left', string='Included services left')

    gift_product_id = fields.Many2one('product.product', 'Gift')
    gift_qty = fields.Float('Gift qty')
    gift_delivered = fields.Boolean(tracking=True)
    gift_delivered_date = fields.Datetime(readonly=True)
    gift_delivered_by = fields.Many2one('res.users', readonly=True)

    _sql_constraints = [
        ('invoice_line_uniq', 'unique(invoice_id, sale_line_id)',
         'Entitlements were already issued for this invoice and subscription line.'),
    ]

    @api.depends('order_id', 'period_start', 'period_end')
    def _compute_display_name(self):
        for p in self:
            p.display_name = '%s · %s → %s' % (p.order_id.name or '', p.period_start or '', p.period_end or '')

    @api.depends('period_end')
    def _compute_state(self):
        today = fields.Date.context_today(self)
        for p in self:
            p.state = 'active' if p.period_end and p.period_end >= today else 'expired'

    def _search_state(self, operator, value):
        if operator not in ('=', '!='):
            raise NotImplementedError('Unsupported operator %r on state' % operator)
        today = fields.Date.context_today(self)
        active = (value == 'active') == (operator == '=')
        return [('period_end', '>=' if active else '<', today)]

    @api.depends('card_ids.points', 'card_ids.cw_kind')
    def _compute_services_left(self):
        for p in self:
            p.services_left = sum(p.card_ids.filtered(lambda c: c.cw_kind == 'included').mapped('points'))

    def action_mark_gift_delivered(self):
        self.write({
            'gift_delivered': True,
            'gift_delivered_date': fields.Datetime.now(),
            'gift_delivered_by': self.env.user.id,
        })

    # ------------------------------------------------------------------
    # Issuing
    # ------------------------------------------------------------------
    @api.model
    def _cw_issue_from_invoice(self, invoice):
        """Create one period (+ coupons + gift) per Car Wash subscription line of a paid invoice.
        Idempotent: skips (invoice, line) pairs already issued."""
        # The payer may be an accountant or the website Public user, who cannot read sale
        # lines / entitlements: everything below runs as sudo (callers do their own checks).
        self = self.sudo()
        invoice = invoice.sudo()
        sale_lines = invoice.invoice_line_ids.sale_line_ids.filtered(
            lambda l: l.product_id.product_tmpl_id.cw_is_subscription and l.order_id.is_subscription)
        periods = self.browse()
        for sol in sale_lines:
            if self.search_count([('invoice_id', '=', invoice.id), ('sale_line_id', '=', sol.id)]):
                continue
            periods |= self._cw_create_period(invoice, sol)
        return periods

    def _cw_create_period(self, invoice, sol):
        tmpl = sol.product_id.product_tmpl_id
        order = sol.order_id
        # Real payment date (works for manual re-runs too); fallback to today.
        today = max(invoice.line_ids.matched_credit_ids.mapped('max_date'),
                    default=fields.Date.context_today(self))
        # Use the period Odoo Subscriptions invoiced (deferred dates on the invoice line, so a
        # "1 month" plan gives real calendar months); fallback to invoice date + cw_period_days.
        inv_line = invoice.invoice_line_ids.filtered(lambda l: sol in l.sale_line_ids)[:1]
        if inv_line.deferred_start_date and inv_line.deferred_end_date:
            start, end = inv_line.deferred_start_date, inv_line.deferred_end_date
        else:
            start = invoice.invoice_date or today
            end = start + timedelta(days=max(tmpl.cw_period_days, 1) - 1)
        root = (order.origin_order_id if 'origin_order_id' in order._fields else False) or order

        # Previous period = same contract (root) and plan, strictly earlier, from another invoice.
        # Within the same order it must also be the same line, so two identical lines don't feed
        # each other's streak; across orders (renewal) lines differ, so root + plan is enough.
        prev = self.search([
            ('root_order_id', '=', root.id),
            ('product_id', '=', sol.product_id.id),
            ('invoice_id', '!=', invoice.id),
            ('period_start', '<', start),
            '|', ('order_id', '!=', order.id), ('sale_line_id', '=', sol.id),
        ], order='period_start desc, id desc', limit=1)
        if prev and today <= prev.period_end + timedelta(days=tmpl.cw_grace_days):
            streak = prev.streak + 1
        else:
            streak = 1

        # Quantity actually invoiced (and paid) in this invoice, not the order quantity.
        qty = inv_line.quantity if inv_line else 0
        if inv_line and inv_line.product_uom_id and sol.product_uom and inv_line.product_uom_id != sol.product_uom:
            qty = inv_line.product_uom_id._compute_quantity(qty, sol.product_uom)
        qty = int(qty) or 1
        period = self.create({
            'order_id': order.id,
            'root_order_id': root.id,
            'sale_line_id': sol.id,
            'invoice_id': invoice.id,
            'partner_id': order.partner_id.id,
            'product_id': sol.product_id.id,
            'period_start': start,
            'period_end': end,
            'payment_date': today,
            'streak': streak,
            'gift_product_id': tmpl.cw_gift_product_id.id,
            'gift_qty': tmpl.cw_gift_qty * qty if tmpl.cw_gift_product_id else 0.0,
        })

        reward_expiry = today + timedelta(days=tmpl.cw_reward_validity_days)
        ents = tmpl.cw_entitlement_ids._applies_to(sol.product_id)
        issue = ents.filtered(lambda e: e.kind == 'included')
        issue |= ents.filtered(lambda e: e.kind == 'monthly_reward')
        if tmpl.cw_tier == 'regular' and tmpl.cw_streak_periods > 0 and streak % tmpl.cw_streak_periods == 0:
            issue |= ents.filtered(lambda e: e.kind == 'streak_reward')

        # Coupons are created without e-mail; the e-mail goes out afterwards in its own savepoint
        # so a mail/template error never cancels the coupons the customer already paid for.
        cards = self.env['loyalty.card'].with_context(action_no_send_mail=True).create([{
            'program_id': ent.program_id.id,
            'partner_id': order.partner_id.id,
            'points': ent.quantity * qty,
            'expiration_date': end if ent.kind == 'included' else reward_expiry,
            'cw_period_id': period.id,
            'cw_kind': ent.kind,
        } for ent in issue])

        period._cw_post_summary()
        period._cw_send_coupon_emails(cards)
        return period

    def _cw_send_coupon_emails(self, cards):
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                cards.with_context(action_no_send_mail=False)._send_creation_communication()
        except Exception:
            _logger.exception('Car Wash: coupon e-mail failed for period %s', self.id)
            self.message_post(body='⚠️ Car Wash: the coupons were issued but the coupon e-mail could not be sent. '
                                   'Send it from each coupon (Loyalty › Coupons).',
                              author_id=self.env.ref('base.partner_root').id)

    def _cw_post_summary(self):
        # Posted as OdooBot: in website flows the current user is Public.
        odoobot = self.env.ref('base.partner_root').id
        for p in self:
            rows = Markup('').join(
                Markup('<li>%s — <b>%s</b> × %s (expires %s)</li>') % (
                    escape(c.program_id.name), escape(c.code), int(c.points), c.expiration_date or '')
                for c in p.card_ids)
            gift = (Markup('<br/>Gift: %s × %s') % (escape(p.gift_product_id.display_name), p.gift_qty)
                    if p.gift_product_id else Markup(''))
            body = Markup('<b>Car Wash period %s → %s</b> (streak %s)<ul>%s</ul>%s') % (
                p.period_start, p.period_end, p.streak, rows, gift)
            p.order_id.message_post(body=body, author_id=odoobot)
