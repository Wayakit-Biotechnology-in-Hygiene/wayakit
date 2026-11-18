from datetime import date
from unittest.mock import patch

from freezegun import freeze_time

from odoo.exceptions import ValidationError
from odoo.tests import new_test_user, tagged

from odoo.addons.sale.tests.common import TestSaleCommon


@tagged('post_install', '-at_install')
class TestCarwashSubscription(TestSaleCommon):
    """Ticket cases: Regular Sedan purchase, 3 consecutive periods -> exterior coupon,
    renewal paid > 7 days late -> streak resets, Premium SUV -> monthly exterior without streak,
    included coupons expire at the end of the period."""

    @classmethod
    def setUpClass(cls, chart_template_ref=None):
        super().setUpClass(chart_template_ref=chart_template_ref)
        cls.company = cls.company_data['company']
        cls.env = cls.env(context=dict(cls.env.context, allowed_company_ids=cls.company.ids))
        cls.env.user.company_id = cls.company
        cls.partner = cls.env['res.partner'].create({'name': 'CW Customer', 'email': 'cw.customer@example.com'})
        cls.plan_month = cls.env['sale.subscription.plan'].create({
            'name': 'Monthly', 'billing_period_value': 1, 'billing_period_unit': 'month'})

        def service(name):
            return cls.env['product.product'].create({'name': name, 'type': 'service', 'list_price': 0})

        def program(name, reward_product):
            return cls.env['loyalty.program'].create({
                'name': name,
                'program_type': 'coupons',
                'trigger': 'with_code',
                'applies_on': 'current',
                'company_id': cls.company.id,
                'rule_ids': [(5, 0, 0)],
                'reward_ids': [(5, 0, 0), (0, 0, {
                    'reward_type': 'product',
                    'reward_product_id': reward_product.id,
                    'reward_product_qty': 1,
                    'required_points': 1,
                })],
                # same as scripts/setup_coupon_programs.py: send the coupon e-mail on creation
                'communication_plan_ids': [(5, 0, 0), (0, 0, {
                    'trigger': 'create',
                    'mail_template_id': cls.env.ref('loyalty.mail_template_loyalty_card').id,
                })],
            })

        cls.prog_sedan_ext = program('CW Sub – Sedan Exterior', service('Sedan Exterior'))
        cls.prog_sedan_full = program('CW Sub – Sedan Full Wash', service('Sedan Full'))
        cls.prog_suv_ext = program('CW Sub – SUV Exterior', service('SUV Exterior'))
        cls.prog_suv_full = program('CW Sub – SUV Full Wash', service('SUV Full'))
        cls.prog_suv_deep = program('CW Sub – SUV Deep Clean', service('SUV Deep'))
        cls.gift = service('Disinfectant 100 mL')

        def plan_product(name, tier, price, entitlements):
            return cls.env['product.product'].create({
                'name': name,
                'type': 'service',
                'recurring_invoice': True,
                'list_price': price,
                'taxes_id': [(5, 0, 0)],
                'cw_is_subscription': True,
                'cw_tier': tier,
                'cw_gift_product_id': cls.gift.id,
                'cw_entitlement_ids': [(0, 0, {'kind': k, 'program_id': p.id, 'quantity': q})
                                       for k, p, q in entitlements],
            })

        cls.regular_sedan = plan_product('Regular Sedan 4 Full', 'regular', 191.30, [
            ('included', cls.prog_sedan_full, 4),
            ('streak_reward', cls.prog_sedan_ext, 1),
        ])
        cls.premium_suv = plan_product('Premium SUV', 'premium', 495.65, [
            ('included', cls.prog_suv_full, 3),
            ('included', cls.prog_suv_deep, 1),
            ('monthly_reward', cls.prog_suv_ext, 1),
        ])
        cls.Period = cls.env['carwash.subscription.period']

    def setUp(self):
        super().setUp()
        # The coupon e-mail renders a barcode. Local envs with reportlab 4 but without
        # rl-renderPM can't draw it (odoo.sh can): stub it only in that case.
        try:
            self.env['ir.actions.report'].barcode('QR', 'probe')
        except Exception:
            self.startPatcher(patch.object(type(self.env['ir.actions.report']), 'barcode', return_value=b''))

    # ------------------------------------------------------------------ helpers
    def _subscribe(self, product, start):
        with freeze_time(start):
            order = self.env['sale.order'].create({
                'partner_id': self.partner.id,
                'plan_id': self.plan_month.id,
                'order_line': [(0, 0, {'product_id': product.id, 'product_uom_qty': 1, 'tax_id': False})],
            })
            order.action_confirm()
        self.assertTrue(order.is_subscription)
        return order

    def _invoice(self, order, on):
        with freeze_time(on):
            invoice = order._create_recurring_invoice()
        self.assertEqual(len(invoice), 1, 'one recurring invoice expected on %s' % on)
        self.assertEqual(invoice.state, 'posted')
        return invoice

    def _pay(self, invoice, on):
        with freeze_time(on):
            self.env['account.payment.register'].with_context(
                active_model='account.move', active_ids=invoice.ids,
            ).create({'payment_date': on})._create_payments()
        self.assertIn(invoice.payment_state, ('paid', 'in_payment'))
        return self.Period.search([('invoice_id', '=', invoice.id)])

    def _cycle(self, order, invoice_on, pay_on=None):
        return self._pay(self._invoice(order, invoice_on), pay_on or invoice_on)

    def _user(self, login, groups):
        """Fake internal user restricted to the test company (no real people)."""
        return new_test_user(self.env, login=login, groups=groups,
                             company_id=self.company.id, company_ids=[(6, 0, self.company.ids)])

    def _accountant(self):
        # Billing only: NO sales group at all.
        user = self._user('cw_fake_accountant', 'base.group_user,account.group_account_invoice')
        self.assertFalse(user.has_group('sales_team.group_sale_salesman'))
        return user

    @staticmethod
    def _cards(period, kind, program=None):
        return period.card_ids.filtered(
            lambda c: c.cw_kind == kind and (not program or c.program_id == program))

    # ------------------------------------------------------------------ tests
    def test_01_regular_sedan_purchase(self):
        order = self._subscribe(self.regular_sedan, '2026-01-01')
        period = self._cycle(order, '2026-01-01')

        self.assertEqual(len(period), 1)
        self.assertEqual(period.partner_id, self.partner)
        self.assertEqual(period.root_order_id, order)
        self.assertEqual(period.tier, 'regular')
        self.assertEqual(period.streak, 1)
        self.assertEqual(period.period_start, date(2026, 1, 1))
        self.assertEqual(period.period_end, date(2026, 1, 31))
        self.assertEqual(period.payment_date, date(2026, 1, 1))
        self.assertEqual(period.gift_product_id, self.gift)
        self.assertEqual(period.gift_qty, 1)
        self.assertFalse(period.gift_delivered)

        included = self._cards(period, 'included', self.prog_sedan_full)
        self.assertEqual(len(included), 1)
        self.assertEqual(included.points, 4)
        self.assertEqual(included.partner_id, self.partner)
        self.assertEqual(period.services_left, 4)
        self.assertFalse(self._cards(period, 'streak_reward'), 'no streak reward on the first period')
        self.assertEqual(order.cw_period_count, 1)

        # coupon e-mail queued by the "create" communication plan of the coupons program
        mails = self.env['mail.mail'].search([('model', '=', 'loyalty.card'), ('res_id', 'in', period.card_ids.ids)])
        self.assertEqual(len(mails), len(period.card_ids))
        self.assertIn(self.partner, mails.recipient_ids)
        # 1.2.1: coupons issued by a paid period e-mail without Odoo's layout
        for mail in mails:
            self.assertNotIn('Powered by', str(mail.body_html))
            self.assertNotIn('Your Loyalty Coupon', str(mail.body_html))
            self.assertNotIn('CW Sub', str(mail.body_html))

        period.action_mark_gift_delivered()
        self.assertTrue(period.gift_delivered)
        self.assertEqual(period.gift_delivered_by, self.env.user)

    def test_02_three_consecutive_periods_give_exterior(self):
        order = self._subscribe(self.regular_sedan, '2026-01-01')
        p1 = self._cycle(order, '2026-01-01')
        p2 = self._cycle(order, '2026-02-01')
        p3 = self._cycle(order, '2026-03-01')

        self.assertEqual((p1.streak, p2.streak, p3.streak), (1, 2, 3))
        self.assertFalse(self._cards(p1, 'streak_reward'))
        self.assertFalse(self._cards(p2, 'streak_reward'))
        reward = self._cards(p3, 'streak_reward', self.prog_sedan_ext)
        self.assertEqual(len(reward), 1)
        self.assertEqual(reward.points, 1)
        # reward valid 30 days from payment, independent from the period end
        self.assertEqual(reward.expiration_date, date(2026, 3, 31))

        # next reward only after another 3 consecutive periods
        p4 = self._cycle(order, '2026-04-01')
        self.assertEqual(p4.streak, 4)
        self.assertFalse(self._cards(p4, 'streak_reward'))

    def test_03_payment_within_grace_keeps_streak(self):
        order = self._subscribe(self.regular_sedan, '2026-01-01')
        self._cycle(order, '2026-01-01')
        # period 1 ends 2026-01-31; 7 days of grace -> 2026-02-07 still counts
        p2 = self._cycle(order, '2026-02-01', pay_on='2026-02-07')
        self.assertEqual(p2.streak, 2)

    def test_04_late_renewal_resets_streak(self):
        order = self._subscribe(self.regular_sedan, '2026-01-01')
        self._cycle(order, '2026-01-01')
        self._cycle(order, '2026-02-01')
        # period 2 ends 2026-02-28; paid 2026-03-08 = 8 days late -> streak restarts
        p3 = self._cycle(order, '2026-03-01', pay_on='2026-03-08')
        self.assertEqual(p3.streak, 1)
        self.assertFalse(self._cards(p3, 'streak_reward'), 'no exterior: the streak was broken')
        p4 = self._cycle(order, '2026-04-01')
        p5 = self._cycle(order, '2026-05-01')
        self.assertEqual((p4.streak, p5.streak), (2, 3))
        self.assertTrue(self._cards(p5, 'streak_reward'))

    def test_05_premium_suv_monthly_exterior_without_streak(self):
        order = self._subscribe(self.premium_suv, '2026-01-01')
        periods = [self._cycle(order, d) for d in ('2026-01-01', '2026-02-01', '2026-03-01')]
        for period in periods:
            self.assertEqual(period.tier, 'premium')
            self.assertEqual(len(self._cards(period, 'monthly_reward', self.prog_suv_ext)), 1)
            self.assertFalse(self._cards(period, 'streak_reward'))
            self.assertEqual(self._cards(period, 'included', self.prog_suv_full).points, 3)
            self.assertEqual(self._cards(period, 'included', self.prog_suv_deep).points, 1)

        with self.assertRaises(ValidationError):
            self.premium_suv.product_tmpl_id.write({'cw_entitlement_ids': [(0, 0, {
                'kind': 'streak_reward', 'program_id': self.prog_suv_ext.id, 'quantity': 1})]})

    def test_06_included_coupons_expire_at_period_end(self):
        order = self._subscribe(self.premium_suv, '2026-02-01')
        # paid late in the period: included coupons still end with the period (no rollover)
        period = self._cycle(order, '2026-02-01', pay_on='2026-02-20')
        self.assertEqual(period.period_end, date(2026, 2, 28))
        for card in self._cards(period, 'included'):
            self.assertEqual(card.expiration_date, date(2026, 2, 28))
        # the monthly reward is valid 30 days from the payment instead
        self.assertEqual(self._cards(period, 'monthly_reward').expiration_date, date(2026, 3, 22))

        with freeze_time('2026-03-01'):
            self.assertEqual(period.with_context(tz='UTC').state, 'expired')
            self.assertIn(period, self.Period.search([('state', '=', 'expired')]))

    def test_07_reissue_is_idempotent(self):
        order = self._subscribe(self.regular_sedan, '2026-01-01')
        invoice = self._invoice(order, '2026-01-01')
        self._pay(invoice, '2026-01-01')
        cards_before = self.env['loyalty.card'].search_count([('cw_period_id', '!=', False)])
        invoice.action_cw_issue_entitlements()
        self.assertEqual(len(self.Period.search([('invoice_id', '=', invoice.id)])), 1)
        self.assertEqual(self.env['loyalty.card'].search_count([('cw_period_id', '!=', False)]), cards_before)

    def test_08_failure_never_blocks_payment(self):
        order = self._subscribe(self.regular_sedan, '2026-01-01')
        invoice = self._invoice(order, '2026-01-01')
        Period = type(self.Period)
        with patch.object(Period, '_cw_issue_from_invoice', side_effect=Exception('boom')):
            periods = self._pay(invoice, '2026-01-01')
        self.assertFalse(periods)
        self.assertIn(invoice.payment_state, ('paid', 'in_payment'))
        self.assertTrue(any('NOT issued' in (m.body or '') for m in invoice.message_ids))
        # manual retry from the server action issues them
        invoice.action_cw_issue_entitlements()
        self.assertEqual(len(self.Period.search([('invoice_id', '=', invoice.id)])), 1)

    def test_09_coupon_email_failure_keeps_coupons(self):
        order = self._subscribe(self.regular_sedan, '2026-01-01')
        invoice = self._invoice(order, '2026-01-01')
        MailTemplate = type(self.env['mail.template'])
        with patch.object(MailTemplate, 'send_mail', side_effect=Exception('template error')):
            period = self._pay(invoice, '2026-01-01')
        self.assertEqual(len(period), 1)
        self.assertEqual(self._cards(period, 'included').points, 4)
        self.assertTrue(any('could not be sent' in (m.body or '') for m in period.message_ids))

    # ------------------------------------------------------------------ review fixes (B*, S*, N*)
    def test_10_accountant_only_registers_payment(self):
        """B1: payment registered by a billing-only user still issues period + coupons."""
        acc = self._accountant()
        order = self._subscribe(self.regular_sedan, '2026-01-01')
        invoice = self._invoice(order, '2026-01-01')
        with freeze_time('2026-01-01'):
            self.env['account.payment.register'].with_user(acc).with_context(
                active_model='account.move', active_ids=invoice.ids,
            ).create({'payment_date': '2026-01-01'})._create_payments()
        self.assertIn(invoice.payment_state, ('paid', 'in_payment'))

        period = self.Period.search([('invoice_id', '=', invoice.id)])
        self.assertEqual(len(period), 1, 'period must be issued when an accountant registers the payment')
        self.assertEqual(self._cards(period, 'included', self.prog_sedan_full).points, 4)
        self.assertFalse(any('NOT issued' in (m.body or '') for m in invoice.message_ids),
                         'no failure note expected on the invoice')

    def test_11_accountant_only_reads_subscription_form(self):
        """B2: billing-only user can open the subscription (cw_period_ids / count on the form)."""
        acc = self._accountant()
        order = self._subscribe(self.regular_sedan, '2026-01-01')
        self._cycle(order, '2026-01-01')
        order_acc = order.with_user(acc)
        order_acc.invalidate_recordset()
        # The web client reads only the fields of the form view as processed for the user
        # (group-restricted fields removed). A bare read() of every field is not a valid check:
        # it already fails on standard sale_loyalty fields (sale.order.coupon.points) for this user.
        views = order_acc.get_views([(False, 'form')])
        self.assertIn('form', views['views'])
        form_fields = views['models']['sale.order']
        form_fields = form_fields.get('fields', form_fields) if isinstance(form_fields, dict) else form_fields
        self.assertNotIn('cw_period_count', form_fields)
        order_acc.read(list(form_fields))

    def test_12_basic_user_reads_product_template(self):
        """B3: an internal user without sales rights (e.g. inventory) opens a product form."""
        groups = 'base.group_user'
        if self.env.ref('stock.group_stock_user', raise_if_not_found=False):
            groups += ',stock.group_stock_user'
        user = self._user('cw_fake_basic', groups)
        tmpl = self.regular_sedan.product_tmpl_id.with_user(user)
        tmpl.invalidate_recordset()
        tmpl.read()  # every field, including cw_entitlement_ids: must not raise
        views = tmpl.get_views([(False, 'form')])
        self.assertIn('form', views['views'])

    def test_13_two_lines_same_plan_keep_separate_streaks(self):
        """S1: two lines of the same plan on one order -> each line has its own streak."""
        with freeze_time('2026-01-01'):
            order = self.env['sale.order'].create({
                'partner_id': self.partner.id,
                'plan_id': self.plan_month.id,
                'order_line': [
                    (0, 0, {'product_id': self.regular_sedan.id, 'product_uom_qty': 1, 'tax_id': False}),
                    (0, 0, {'product_id': self.regular_sedan.id, 'product_uom_qty': 1, 'tax_id': False}),
                ],
            })
            order.action_confirm()
        line_a, line_b = order.order_line
        invoices = []
        for day in ('2026-01-01', '2026-02-01', '2026-03-01'):
            periods = self._cycle(order, day)
            self.assertEqual(len(periods), 2, 'one period per subscription line on %s' % day)
            invoices.append(periods.invoice_id)

        for line in (line_a, line_b):
            line_periods = self.Period.search([('sale_line_id', '=', line.id)], order='period_start asc')
            self.assertEqual(line_periods.mapped('streak'), [1, 2, 3],
                             'line %s must count its own streak, not the other line' % line.id)

        rewards = self.Period.search([('order_id', '=', order.id)]).card_ids.filtered(
            lambda c: c.cw_kind == 'streak_reward')
        self.assertEqual(len(rewards), 2, 'exactly one exterior per line after 3 periods')
        self.assertEqual(rewards.cw_period_id.invoice_id, invoices[2], 'both rewards on the 3rd invoice')
        self.assertEqual(rewards.cw_period_id.sale_line_id, line_a | line_b)

    def test_14_out_of_order_reissue_uses_only_previous_periods(self):
        """S2: Feb fails, Mar (and Apr) issue normally, then Feb is re-issued by hand."""
        order = self._subscribe(self.regular_sedan, '2026-01-01')
        Period = type(self.Period)
        original = Period._cw_issue_from_invoice
        failing = {}

        def flaky(self_, invoice):
            if invoice in failing.get('ids', ()):
                raise Exception('fake failure for February')
            return original(self_, invoice)

        jan = self._cycle(order, '2026-01-01')
        feb_inv = self._invoice(order, '2026-02-01')
        failing['ids'] = feb_inv
        with patch.object(Period, '_cw_issue_from_invoice', flaky):
            self.assertFalse(self._pay(feb_inv, '2026-02-01'))
        mar = self._cycle(order, '2026-03-01')
        # April makes the test discriminating: a "latest period wins" lookup would chain Feb
        # onto Apr (streak 2) -> Feb streak 3 + a wrong exterior coupon.
        apr = self._cycle(order, '2026-04-01')
        self.assertEqual(jan.streak, 1)
        # Mar was computed against Jan while Feb did not exist: paid 03-01 > 01-31 + 7 -> reset.
        self.assertEqual(mar.streak, 1)
        self.assertEqual(apr.streak, 2)

        feb_inv.action_cw_issue_entitlements()
        feb = self.Period.search([('invoice_id', '=', feb_inv.id)])
        self.assertEqual(len(feb), 1)
        self.assertEqual(feb.period_start, date(2026, 2, 1))
        self.assertEqual(feb.streak, 2, 'Feb streak only from periods starting before it (Jan)')
        self.assertFalse(self._cards(feb, 'streak_reward'))
        # Documented limitation: a late manual re-issue does NOT recompute later periods.
        self.assertEqual(mar.streak, 1)
        self.assertEqual(apr.streak, 2)
        self.assertFalse(self._cards(mar, 'streak_reward'))

    def test_15_coupons_use_invoiced_quantity(self):
        """S4: qty raised on the sale line after invoicing -> coupons follow the invoice (qty 1)."""
        order = self._subscribe(self.regular_sedan, '2026-01-01')
        invoice = self._invoice(order, '2026-01-01')
        self.assertEqual(invoice.invoice_line_ids.filtered(
            lambda l: l.product_id == self.regular_sedan).quantity, 1)
        order.order_line.filtered(lambda l: l.product_id == self.regular_sedan).product_uom_qty = 2
        period = self._pay(invoice, '2026-01-01')
        self.assertEqual(len(period), 1)
        self.assertEqual(self._cards(period, 'included', self.prog_sedan_full).points, 4)
        self.assertEqual(period.gift_qty, 1)

    def test_16_state_search_rejects_unsupported_operator(self):
        """N4: 'in' on the computed state must fail loudly instead of returning a wrong domain."""
        with self.assertRaises(NotImplementedError):
            self.Period.search([('state', 'in', ['active'])])

    # ------------------------------------------------------------------ redemption: one wash per code
    def _cw_card(self, washes, on='2026-01-01'):
        """Included Full Wash card issued by the module (cw_period_id set) with `washes` points."""
        plan = self.env['product.product'].create({
            'name': 'Fake Plan %s Full' % washes,
            'type': 'service',
            'recurring_invoice': True,
            'list_price': 100,
            'taxes_id': [(5, 0, 0)],
            'cw_is_subscription': True,
            'cw_tier': 'regular',
            'cw_entitlement_ids': [(0, 0, {'kind': 'included', 'program_id': self.prog_sedan_full.id,
                                           'quantity': washes})],
        })
        period = self._cycle(self._subscribe(plan, on), on)
        card = self._cards(period, 'included', self.prog_sedan_full)
        self.assertEqual(len(card), 1)
        self.assertEqual(card.points, washes)
        self.assertTrue(card.cw_period_id)
        return period, card

    def _quotation(self):
        """Plain (non-subscription) quotation with a multi-unit paid line, like a POS-less backend sale."""
        paid = self.env['product.product'].create({'name': 'Fake Air Freshener', 'type': 'consu', 'list_price': 10})
        return self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'order_line': [(0, 0, {'product_id': paid.id, 'product_uom_qty': 3, 'tax_id': False})],
        })

    def _apply_code_backend(self, order, code, reward):
        """Same path as the 'Coupon Code' button: code wizard -> reward wizard."""
        action = self.env['sale.loyalty.coupon.wizard'].create(
            {'order_id': order.id, 'coupon_code': code}).action_apply()
        self.assertIn(reward.id, action['context']['default_reward_ids'])
        self.env['sale.loyalty.reward.wizard'].with_context(active_id=order.id).create(
            {'order_id': order.id, 'selected_reward_id': reward.id}).action_apply()

    @staticmethod
    def _reward_lines(order, coupon):
        return order.order_line.filtered(lambda l: l.is_reward_line and l.coupon_id == coupon)

    def test_17_cw_code_redeems_one_wash_backend(self):
        """Bug 7.1: 8 Full Washes card applied on an order -> 1 wash, card 8 -> 7 (not 8 -> 0)."""
        period, card = self._cw_card(8)
        reward = self.prog_sedan_full.reward_ids
        with freeze_time('2026-01-10'):
            order = self._quotation()
            self._apply_code_backend(order, card.code, reward)
            line = self._reward_lines(order, card)
            self.assertEqual(len(line), 1)
            self.assertEqual(line.product_uom_qty, 1, 'one code application = one wash')
            self.assertEqual(line.points_cost, 1)
            self.assertEqual(line.reward_id, reward)
            self.assertEqual(order._get_real_points_for_coupon(card), 7)

            # the cap must survive Odoo's reward recomputation (called on every order change)
            order._update_programs_and_rewards()
            order.order_line.filtered(lambda l: not l.is_reward_line).product_uom_qty = 5
            order._update_programs_and_rewards()
            line = self._reward_lines(order, card)
            self.assertEqual((len(line), line.product_uom_qty, line.points_cost), (1, 1, 1))

            self.assertEqual(card.points, 8, 'points are only consumed on confirmation')
            order.action_confirm()
        line = self._reward_lines(order, card)
        self.assertEqual((line.product_uom_qty, line.points_cost), (1, 1), 'confirm recomputes rewards too')
        self.assertEqual(card.points, 7)
        self.assertEqual(period.services_left, 7)

        # the same code still works on the next visit: 7 -> 6
        with freeze_time('2026-01-17'):
            order2 = self._quotation()
            self._apply_code_backend(order2, card.code, reward)
            self.assertEqual(self._reward_lines(order2, card).product_uom_qty, 1)
            order2.action_confirm()
        self.assertEqual(card.points, 6)
        self.assertEqual(period.services_left, 6)

        # cancelling a confirmed redemption gives back exactly one wash
        order2._action_cancel()
        self.assertEqual(card.points, 7)

    def test_18_cw_code_redeems_one_wash_website_path(self):
        """website_sale_loyalty claims through the same method: _try_apply_code + _apply_program_reward
        (pending coupon) or _auto_apply_rewards (cart)."""
        _period, card = self._cw_card(8)
        reward = self.prog_sedan_full.reward_ids
        with freeze_time('2026-01-10'):
            # _try_pending_coupon flow (code from the e-mail link)
            order = self._quotation()
            status = order._try_apply_code(card.code)
            self.assertNotIn('error', status)
            self.assertEqual(status[card], reward)
            order._apply_program_reward(reward, card)
            order._update_programs_and_rewards()
            line = self._reward_lines(order, card)
            self.assertEqual((len(line), line.product_uom_qty, line.points_cost), (1, 1, 1))

            if hasattr(type(order), '_auto_apply_rewards'):
                # cart flow: claimable rewards are auto-applied; must not re-claim the same reward
                cart = self._quotation()
                self.assertNotIn('error', cart._try_apply_code(card.code))
                cart._auto_apply_rewards()
                cart._update_programs_and_rewards()
                cart._auto_apply_rewards()
                line = self._reward_lines(cart, card)
                self.assertEqual((len(line), line.product_uom_qty, line.points_cost), (1, 1, 1))

    def test_19_cw_cap_follows_reward_configuration(self):
        """Cap = one reward unit: qty = reward_product_qty, cost = required_points (not hardcoded 1)."""
        _period, card = self._cw_card(8)
        reward = self.prog_sedan_full.reward_ids
        reward.write({'reward_product_qty': 2, 'required_points': 3})
        with freeze_time('2026-01-10'):
            order = self._quotation()
            self._apply_code_backend(order, card.code, reward)
            line = self._reward_lines(order, card)
            self.assertEqual((len(line), line.product_uom_qty, line.points_cost), (1, 2, 3))
            order.action_confirm()
        self.assertEqual(card.points, 5)

    def test_20_non_carwash_coupons_keep_core_behaviour(self):
        """Scope: only cards issued by this module are capped. Other coupons claim everything."""
        free = self.env['product.product'].create({'name': 'Fake Free Sponge', 'type': 'consu', 'list_price': 4})
        other = self.env['loyalty.program'].create({
            'name': 'Fake Other Coupons',
            'program_type': 'coupons',
            'trigger': 'with_code',
            'applies_on': 'current',
            'company_id': self.company.id,
            'rule_ids': [(5, 0, 0)],
            'reward_ids': [(5, 0, 0), (0, 0, {
                'reward_type': 'product',
                'reward_product_id': free.id,
                'reward_product_qty': 3,
                'required_points': 2,
            })],
        })
        other_card = self.env['loyalty.card'].create(
            {'program_id': other.id, 'partner_id': self.partner.id, 'points': 8})
        # a card of a "CW Sub" program created by hand (no period) is NOT issued by this module
        manual_cw = self.env['loyalty.card'].create(
            {'program_id': self.prog_sedan_full.id, 'partner_id': self.partner.id, 'points': 8})
        self.assertFalse(manual_cw.cw_period_id)

        with freeze_time('2026-01-10'):
            order = self._quotation()
            self._apply_code_backend(order, other_card.code, other.reward_ids)
            line = self._reward_lines(order, other_card)
            # core: floor(8 / 2) = 4 claims -> qty 4 * 3 = 12, cost 8
            self.assertEqual((len(line), line.product_uom_qty, line.points_cost), (1, 12, 8))
            order._update_programs_and_rewards()
            line = self._reward_lines(order, other_card)
            self.assertEqual((line.product_uom_qty, line.points_cost), (12, 8))
            order.action_confirm()
        self.assertEqual(other_card.points, 0)

        with freeze_time('2026-01-10'):
            order2 = self._quotation()
            self._apply_code_backend(order2, manual_cw.code, self.prog_sedan_full.reward_ids)
            line = self._reward_lines(order2, manual_cw)
            self.assertEqual((line.product_uom_qty, line.points_cost), (8, 8))
            order2.action_confirm()
        self.assertEqual(manual_cw.points, 0)

    def test_21_cart_hides_claim_box_for_applied_cw_coupon(self):
        """Website cart 'Claim' box (_get_claimable_and_showable_rewards): a CW card already applied
        to the order is not offered again; non-CW coupons and the backend keep core behaviour."""
        period, card = self._cw_card(8)
        cw_reward = self.prog_sedan_full.reward_ids
        free = self.env['product.product'].create({'name': 'Fake Free Cloth', 'type': 'consu', 'list_price': 5})
        other = self.env['loyalty.program'].create({
            'name': 'Fake Other Coupons Two Rewards',
            'program_type': 'coupons',
            'trigger': 'with_code',
            'applies_on': 'current',
            'company_id': self.company.id,
            'rule_ids': [(5, 0, 0)],
            'reward_ids': [(5, 0, 0),
                           (0, 0, {'reward_type': 'discount', 'discount': 10, 'discount_mode': 'percent',
                                   'discount_applicability': 'order', 'required_points': 1}),
                           (0, 0, {'reward_type': 'product', 'reward_product_id': free.id,
                                   'reward_product_qty': 1, 'required_points': 1})],
        })
        other_discount = other.reward_ids.filtered(lambda r: r.reward_type == 'discount')
        other_product = other.reward_ids - other_discount
        other_card = self.env['loyalty.card'].create(
            {'program_id': other.id, 'partner_id': self.partner.id, 'points': 8})

        with freeze_time('2026-01-10'):
            order = self._quotation()
            website = self.env.ref('website.default_website', raise_if_not_found=False)
            if website:
                order.website_id = website
            # before applying: the customer's own CW card is offered in the cart
            showable = order._get_claimable_and_showable_rewards()
            self.assertIn(card, showable)
            self.assertEqual(showable[card], cw_reward)
            self.assertIn(other_card, showable)

            # customer types the code in the cart (same calls as website_sale_loyalty)
            self.assertNotIn('error', order._try_apply_code(card.code))
            order._apply_program_reward(cw_reward, card)
            order._update_programs_and_rewards()
            self.assertEqual(self._reward_lines(order, card).product_uom_qty, 1)
            self.assertEqual(order._get_real_points_for_coupon(card), 7)

            showable = order._get_claimable_and_showable_rewards()
            self.assertNotIn(card, showable, 'applied CW coupon must not show the Claim box again')

            # non-CW coupon with points left after one reward: still offered (core)
            self.assertNotIn('error', order._try_apply_code(other_card.code))
            order._apply_program_reward(other_discount, other_card)
            order._update_programs_and_rewards()
            self.assertEqual(order._get_real_points_for_coupon(other_card), 7)
            showable = order._get_claimable_and_showable_rewards()
            self.assertIn(other_card, showable, 'non-CW coupon keeps the core Claim box')
            self.assertIn(other_product, showable[other_card])
            self.assertNotIn(card, showable)

            # backend (Promotions button) still lists the applied CW card: second wash on purpose
            claimable = order._get_claimable_rewards()
            self.assertIn(card, claimable)
            self.assertEqual(claimable[card], cw_reward)
            order._apply_program_reward(cw_reward, card)
            order._update_programs_and_rewards()
            lines = self._reward_lines(order, card)
            self.assertEqual(lines.mapped('product_uom_qty'), [1, 1])
            self.assertEqual(sum(lines.mapped('points_cost')), 2)
            self.assertNotIn(card, order._get_claimable_and_showable_rewards())
            order.action_confirm()
        self.assertEqual(card.points, 6)
        self.assertEqual(period.services_left, 6)
