import importlib.util
import json
import os
import re
from contextlib import contextmanager
from datetime import date
from unittest.mock import patch

from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tools import format_date

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAN_CODES = ('FP-CWS-00103', 'FP-CWS-00104', 'FP-CWS-00203', 'FP-CWS-00204', 'FP-CWS-00105', 'FP-CWS-00205')
KSA_COMPANY_ID = 1
# Internal section of Dalal's proposal: must never reach the public page.
INTERNAL_WORDS = ('Indicative profitability', 'gross margin', 'Est. gross margin')


def _load_module_file(relpath):
    path = os.path.join(MODULE_DIR, relpath)
    spec = importlib.util.spec_from_file_location('cw_' + re.sub(r'\W', '_', relpath), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CarwashPageCommon:

    @classmethod
    def _setup_common(cls):
        cls.ksa = cls.env['res.company'].browse(KSA_COMPANY_ID)
        cls.website = cls.env.ref('website.default_website')

    @classmethod
    def _plan(cls, code, published=True, website=False, sale_ok=True):
        return cls.env['product.template'].create({
            'sale_ok': sale_ok,
            'name': 'Fake CW Plan %s' % code,
            'default_code': code,
            'type': 'service',
            'list_price': 100,
            'company_id': KSA_COMPANY_ID,
            'is_published': published,
            'website_id': website and website.id,
        })

    @classmethod
    def _appointment(cls, name='Car Wash Subscription', published=True, website=False):
        return cls.env['appointment.type'].create({
            'name': name,
            'appointment_tz': 'Asia/Riyadh',
            'is_published': published,
            'website_id': website and website.id,
        })


@tagged('post_install', '-at_install')
class TestCarwashPage(CarwashPageCommon, HttpCase):
    """Deliverable 1: /car-wash-subscriptions on the KSA website."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_common()
        cls.Website = cls.env['website']

    def test_01_page_bound_to_ksa_website(self):
        self.assertEqual(self.website.company_id, self.ksa, 'precondition: default website is KSA (company 1)')
        self.assertEqual(self.Website._cw_ksa_website(), self.website,
                         'test DB fallback: lowest-id website of company 1')
        page = self.env.ref('wayakit_carwash_subscription.page_cw_subscriptions_page')
        self.assertEqual(page.url, '/car-wash-subscriptions')
        self.assertEqual(page.website_id, self.website, 'page must be website-specific (KSA only)')
        self.assertTrue(page.is_published)
        # binding is re-run on every upgrade: must be idempotent
        self.Website._cw_bind_page()
        self.Website._cw_bind_page()
        self.assertEqual(page.website_id, self.website)
        self.assertEqual(self.env['website.page'].search_count([('url', '=', '/car-wash-subscriptions')]), 1)

    def test_02_page_renders_for_public_user(self):
        res = self.url_open('/car-wash-subscriptions')
        self.assertEqual(res.status_code, 200)
        html = res.text
        self.assertIn('WAYAKIT', html)
        self.assertIn('id="book"', html, 'anchor used by the e-mail button')
        for price in ('180', '220', '260', '300', '435', '570'):
            self.assertIn(price, html, 'plan price incl. VAT %s missing' % price)
        for word in INTERNAL_WORDS:
            self.assertNotIn(word.lower(), html.lower(), 'internal profitability data on the public page')
        self.assertNotIn('Wayakit Car Wash', html, 'brand must be WAYAKIT in visible text')

    def test_03_subscribe_buttons_follow_products(self):
        other = self.Website.create({'name': 'Fake Other Website', 'company_id': self.ksa.id})
        ok = self._plan('FP-CWS-00103')
        hidden = self._plan('FP-CWS-00205', published=False)
        elsewhere = self._plan('FP-CWS-00105', website=other)
        not_sellable = self._plan('FP-CWS-00204', sale_ok=False)
        # 00104 / 00203 deliberately missing

        urls = self.website._cw_plan_urls()
        self.assertEqual(set(urls), set(PLAN_CODES), 'one key per plan code')
        self.assertEqual(urls['FP-CWS-00103'], ok.website_url)
        self.assertTrue(ok.website_url)
        self.assertEqual(urls['FP-CWS-00205'], '', 'unpublished product -> no button')
        self.assertEqual(urls['FP-CWS-00105'], '', 'product published on another website -> no button')
        self.assertEqual(urls['FP-CWS-00204'], '', 'published but not "Can be Sold" -> no button')
        for code in ('FP-CWS-00104', 'FP-CWS-00203'):
            self.assertEqual(urls[code], '', 'missing product %s -> no button' % code)

        html = self.url_open('/car-wash-subscriptions').text
        self.assertIn('href="%s"' % ok.website_url, html)
        self.assertNotIn(hidden.website_url, html)
        self.assertNotIn(elsewhere.website_url, html)
        self.assertNotIn(not_sellable.website_url, html)

        # publish the hidden one: its button appears without any code change
        hidden.is_published = True
        self.assertEqual(self.website._cw_plan_urls()['FP-CWS-00205'], hidden.website_url)
        self.assertIn('href="%s"' % hidden.website_url, self.url_open('/car-wash-subscriptions').text)

    def test_04_booking_link_named_appointment_or_fallback(self):
        self.assertEqual(self.website._cw_booking_url(), '/appointment/1', 'no appointment yet -> fallback')
        self.assertIn('/appointment/1', self.url_open('/car-wash-subscriptions').text)

        # look-alikes must not be picked
        self._appointment('Car Wash Care')
        self._appointment('Car Wash Subscription (old)')
        self._appointment(published=False)
        other = self.Website.create({'name': 'Fake Other Website', 'company_id': self.ksa.id})
        self._appointment(website=other)
        self.assertEqual(self.website._cw_booking_url(), '/appointment/1')

        appt = self._appointment()
        self.assertTrue(appt.website_url)
        self.assertNotEqual(appt.website_url, '/appointment/1')
        self.assertEqual(self.website._cw_booking_url(), appt.website_url)
        html = self.url_open('/car-wash-subscriptions').text
        self.assertIn(appt.website_url, html)

        # unpublishing the real one degrades gracefully
        appt.is_published = False
        self.assertEqual(self.website._cw_booking_url(), '/appointment/1')
        self.assertEqual(self.url_open('/car-wash-subscriptions').status_code, 200)

    def test_05_page_not_served_on_another_website(self):
        self.Website.create({'name': 'Fake MX Website', 'company_id': self.ksa.id, 'domain': 'localhost'})
        port = self.base_url().rsplit(':', 1)[1]
        res = self.url_open('http://localhost:%s/car-wash-subscriptions' % port)
        self.assertEqual(res.status_code, 404, 'KSA-only page leaked to another website')
        self.assertEqual(self.url_open('/car-wash-subscriptions').status_code, 200)


    def _make_page_generic(self):
        page = self.env.ref('wayakit_carwash_subscription.page_cw_subscriptions_page')
        page.view_id.with_context(no_cow=True).website_id = False
        page.invalidate_recordset()
        self.assertFalse(page.website_id)
        return page

    def test_06_bind_page_by_website_name(self):
        """No id guessing: bound website wins, else exact name 'KSA Website', else (only with
        a single website) the lowest id; ambiguous -> page unpublished, never generic."""
        page = self.env.ref('wayakit_carwash_subscription.page_cw_subscriptions_page')
        self.assertEqual(self.Website.search_count([]), 1, 'precondition: cw_test has one website')

        # fresh DB, one website: bound to it
        self._make_page_generic()
        self.assertEqual(self.Website._cw_ksa_website(), self.website)
        self.assertTrue(self.Website._cw_bind_page())
        self.assertEqual(page.website_id, self.website)

        # several websites, none named 'KSA Website': nothing guessed, page unpublished
        self._make_page_generic()
        page.is_published = True
        mx = self.Website.create({'name': 'Fake B2B MX Website'})
        self.assertFalse(self.Website._cw_ksa_website())
        self.assertFalse(self.Website._cw_bind_page())
        self.assertFalse(page.is_published, 'ambiguous website -> page must not stay published')
        self.assertFalse(page.website_id)

        # the exact name wins (not 'KSA Website 2', not the lowest id)
        self._make_page_generic()
        page.is_published = True
        self.Website.create({'name': 'KSA Website 2'})
        ksa = self.Website.create({'name': 'KSA Website'})
        self.assertEqual(self.Website._cw_ksa_website(), ksa)
        self.assertTrue(self.Website._cw_bind_page())
        self.assertEqual(page.website_id, ksa)
        self.assertEqual(page.view_id.website_id, ksa)
        self.assertTrue(page.is_published)

        # once bound, renaming the website or adding a namesake does not move the page
        ksa.name = 'KSA Website (renamed)'
        self.Website.create({'name': 'KSA Website'})
        self.assertEqual(self.Website._cw_ksa_website(), ksa)
        self.Website._cw_bind_page()
        self.assertEqual(page.website_id, ksa)
        self.assertNotEqual(page.website_id, mx)

        # two websites with the exact name and a generic page: ambiguous -> unpublished
        self._make_page_generic()
        page.is_published = True
        ksa.name = 'KSA Website'
        self.assertFalse(self.Website._cw_ksa_website())
        self.assertFalse(self.Website._cw_bind_page())
        self.assertFalse(page.is_published)

class CouponMailCommon(CarwashPageCommon):
    """Programs, cards and e-mail rendering shared by the coupon e-mail test classes."""

    @classmethod
    def _setup_mail_common(cls):
        cls._setup_common()
        cls.env = cls.env(context=dict(cls.env.context, allowed_company_ids=[KSA_COMPANY_ID]))
        # The template is noupdate: on a DB already at this version, -u keeps whatever body was
        # loaded first. Reload it from the XML (as the 1.2.1 upgrade does) so the tests always
        # check the template in the code. Rolled back with the test class.
        _load_module_file('migrations/17.0.1.2.1/post-migrate.py').migrate(cls.env.cr, '17.0.1.2.0')
        cls.template = cls.env.ref('wayakit_carwash_subscription.mail_template_cw_coupon')
        cls.template.invalidate_recordset()
        cls.generic = cls.env.ref('loyalty.mail_template_loyalty_card')
        cls.partner = cls.env['res.partner'].create({'name': 'Fake CW Mail Customer', 'email': 'cw.mail@example.com'})
        cls.service = cls.env['product.product'].create(
            {'name': 'Fake Sedan Full Wash Service', 'type': 'service', 'list_price': 0})

    def _program(self, name, template=None, company=None, comm=True):
        vals = {
            'name': name,
            'program_type': 'coupons',
            'trigger': 'with_code',
            'applies_on': 'current',
            'company_id': (company or self.ksa).id,
            'rule_ids': [(5, 0, 0)],
            'reward_ids': [(5, 0, 0), (0, 0, {
                'reward_type': 'product', 'reward_product_id': self.service.id,
                'reward_product_qty': 1, 'required_points': 1})],
            'communication_plan_ids': [(5, 0, 0)],
        }
        if comm:
            vals['communication_plan_ids'].append(
                (0, 0, {'trigger': 'create', 'mail_template_id': (template or self.generic).id}))
        return self.env['loyalty.program'].create(vals)

    @staticmethod
    def _create_lines(program):
        return program.communication_plan_ids.filtered(lambda c: c.trigger == 'create')

    def _card(self, program, kind='included', points=4, expiry=date(2026, 1, 31)):
        return self.env['loyalty.card'].with_context(loyalty_no_mail=True).create({
            'program_id': program.id,
            'partner_id': self.partner.id,
            'points': points,
            'expiration_date': expiry,
            'cw_kind': kind,
        })

    def _body(self, card):
        return str(self.template._render_field('body_html', card.ids)[card.id])

    def _text(self, card):
        """Visible text of the e-mail (tags stripped, whitespace collapsed)."""
        return ' '.join(re.sub(r'<[^>]+>', ' ', self._body(card)).split())


@tagged('post_install', '-at_install')
class TestCarwashCouponEmail(CouponMailCommon, TransactionCase):
    """Deliverable 2: coupon e-mail with 'Book your wash' + switch of the CW Sub programs."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_mail_common()

    def test_10_email_renders_code_points_expiry_and_button(self):
        self.assertEqual(self.template.model, 'loyalty.card')
        program = self._program('CW Sub – Sedan Full Wash', template=self.template)
        card = self._card(program, points=4)
        body = self._body(card)

        text = self._text(card)
        self.assertIn('Dear Fake,', text, 'greeting uses the first name')
        self.assertIn(self.service.name, body)
        self.assertIn(card.code, body)
        self.assertIn('4 washes', text, 'points shown as an integer number of washes')
        self.assertNotIn('4.0', text)
        self.assertIn('Your car washes for this month', text)
        self.assertIn('Included in your monthly plan', text)
        self.assertNotIn('transferable', text.lower(), 'included washes are personal, not transferable')
        self.assertNotIn('Deep cleaning must be booked in advance', text)
        # unambiguous day-month-year (M/D/Y misread in KSA as D/M/Y)
        self.assertEqual(card.expiration_date, date(2026, 1, 31))
        self.assertIn('Valid until', text)
        self.assertIn('31 January 2026', text)
        for ambiguous in ('01/31/2026', '1/31/2026', format_date(self.env, card.expiration_date, lang_code='en_US')):
            self.assertNotIn(ambiguous, body, 'ambiguous M/D/Y expiry date')
        # no expiry -> no "Valid until" line, e-mail still renders
        card.expiration_date = False
        self.assertNotIn('Valid until', self._text(card))
        card.expiration_date = date(2026, 1, 31)
        self.assertIn('WAYAKIT', body)
        self.assertNotIn('barcode', body.lower(), 'no barcode in the CW e-mail (local rendering gotcha)')

        url = card._cw_booking_url()
        self.assertEqual(url, self.website.get_base_url() + '/car-wash-subscriptions#book')
        self.assertIn(url, body, '"Book your wash" button must point to the page anchor')
        self.assertIn('Book your wash', body)
        subject = self.template._render_field('subject', card.ids)[card.id]
        self.assertTrue(subject)

    def test_11_button_uses_website_domain_not_prod(self):
        self.website.domain = 'https://cw-fake-staging.example.com'
        program = self._program('CW Sub – SUV Full Wash', template=self.template)
        card = self._card(program)
        url = card._cw_booking_url()
        self.assertEqual(url, 'https://cw-fake-staging.example.com/car-wash-subscriptions#book')
        self.assertNotIn('ksasystem.wayakit.com', self._body(card))

    def test_12_reward_kinds_say_transferable(self):
        program = self._program('CW Sub – Sedan Exterior', template=self.template)
        expected = {
            'monthly_reward': ('Your free exterior wash this month', 'Monthly reward · transferable'),
            'streak_reward': ('3 months in a row: a free exterior wash on us', 'Loyalty reward · transferable'),
        }
        for kind, (title, kind_line) in expected.items():
            text = self._text(self._card(program, kind=kind, points=1))
            self.assertIn(title, text, kind)
            self.assertIn(kind_line, text, '%s coupon must say it is transferable' % kind)
            self.assertIn('1 wash', text)
            self.assertNotIn('1 washes', text, 'singular for one wash')
        included = self._text(self._card(program, kind='included', points=4))
        self.assertIn('code', included.lower())

    def test_12b_deep_clean_needs_booking_note(self):
        for code, shown in (('FP-CWS-00102', True), ('FP-CWS-00202', True), ('FP-CWS-00101', False)):
            service = self.env['product.product'].create(
                {'name': 'Fake Service %s' % code, 'default_code': code, 'type': 'service'})
            program = self._program('CW Sub – Fake %s' % code, template=self.template)
            program.reward_ids.reward_product_id = service
            text = self._text(self._card(program, points=1))
            self.assertEqual('Deep cleaning must be booked in advance.' in text, shown, code)

    def test_12c_greeting_falls_back(self):
        program = self._program('CW Sub – Sedan Full Wash', template=self.template)
        card = self._card(program)
        card.partner_id = False
        self.assertIn('Valued Customer', self._text(card))

    def test_13_switch_programs_to_cw_template(self):
        Program = self.env['loyalty.program']
        Program._cw_use_coupon_template()  # normalise anything already in the DB
        mx = self.env['res.company'].create({'name': 'Fake MX Company'})
        generic_cw = self._program('CW Sub – Sedan Full Wash')
        no_line = self._program('CW Sub – SUV Exterior', comm=False)
        already = self._program('CW Sub – Sedan Deep Clean', template=self.template)
        custom_tmpl = self.generic.copy({'name': 'Fake custom coupon template'})
        custom = self._program('CW Sub – SUV Deep Clean', template=custom_tmpl)
        other = self._program('Fake Review Coupon')
        other_company = self._program('CW Sub – Sedan Exterior', company=mx)

        self.assertEqual(Program._cw_use_coupon_template(), 2, 'generic -> CW, and missing line added')
        for prog in (generic_cw, no_line, already):
            lines = self._create_lines(prog)
            self.assertEqual(len(lines), 1, prog.name)
            self.assertEqual(lines.mail_template_id, self.template, prog.name)
        self.assertEqual(self._create_lines(custom).mail_template_id, custom_tmpl, 'custom template left alone')
        self.assertEqual(self._create_lines(other).mail_template_id, self.generic, 'non-CW program untouched')
        self.assertEqual(self._create_lines(other_company).mail_template_id, self.generic,
                         'CW-named program of another company untouched')

        self.assertEqual(Program._cw_use_coupon_template(), 0, 'idempotent')
        self.assertEqual(len(self._create_lines(generic_cw)), 1)

        # end to end: a coupon created on a switched program sends the CW e-mail
        card = self.env['loyalty.card'].create({
            'program_id': generic_cw.id, 'partner_id': self.partner.id, 'points': 4, 'cw_kind': 'included'})
        mail = self.env['mail.mail'].search([('model', '=', 'loyalty.card'), ('res_id', '=', card.id)])
        self.assertEqual(len(mail), 1)
        self.assertIn('/car-wash-subscriptions#book', mail.body_html)

    def test_14_post_migrate_script(self):
        path = os.path.join(MODULE_DIR, 'migrations', '17.0.1.2.0', 'post-migrate.py')
        self.assertTrue(os.path.isfile(path), path)
        spec = importlib.util.spec_from_file_location('cw_post_migrate_17_0_1_2_0', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.env['loyalty.program']._cw_use_coupon_template()
        prog = self._program('CW Sub – SUV Full Wash')
        module.migrate(self.env.cr, '17.0.1.1.2')
        prog.invalidate_recordset()
        prog.communication_plan_ids.invalidate_recordset()
        self.assertEqual(self._create_lines(prog).mail_template_id, self.template)
        module.migrate(self.env.cr, '17.0.1.1.2')
        self.assertEqual(len(self._create_lines(prog)), 1)

    def test_15_setup_script_uses_shared_helper(self):
        with open(os.path.join(MODULE_DIR, 'scripts', 'setup_coupon_programs.py')) as f:
            src = f.read()
        self.assertTrue('_cw_use_coupon_template' in src or 'mail_template_cw_coupon' in src,
                        'setup script must give new programs the CW template')


# Markers of Odoo's mail_notification_light / mail_notification_layout wrappers.
ODOO_LAYOUT_MARKERS = ('Powered by', 'Your Loyalty Coupon', 'mail_unfollow', '/logo.png?company=')
FAKE_PHONE = '+966 00 000 0001'
FAKE_EMAIL = 'cw-fake-company@example.com'
FAKE_WEBSITE = 'https://cw-fake-company.example.com'
LEGAL_NAME = 'Advanced Solutions for Detergents LLC'


@tagged('post_install', '-at_install')
class TestCarwashCouponEmailNoLayout(CouponMailCommon, TransactionCase):
    """v17.0.1.2.1: Car Wash cards (cw_period_id) e-mail without Odoo's layout; every other
    loyalty card keeps core behaviour. Checked on the outgoing mail.mail body_html."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_mail_common()
        cls.ksa.write({'name': LEGAL_NAME, 'phone': FAKE_PHONE, 'email': FAKE_EMAIL, 'website': FAKE_WEBSITE})
        plan = cls.env['product.product'].create({'name': 'Fake CW Plan Premium Sedan', 'type': 'service'})
        order = cls.env['sale.order'].create({
            'partner_id': cls.partner.id,
            'company_id': KSA_COMPANY_ID,
            'order_line': [(0, 0, {'product_id': plan.id, 'product_uom_qty': 1})],
        })
        invoice = cls.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': cls.partner.id, 'company_id': KSA_COMPANY_ID})
        cls.period = cls.env['carwash.subscription.period'].create({
            'order_id': order.id,
            'sale_line_id': order.order_line.id,
            'invoice_id': invoice.id,
            'partner_id': cls.partner.id,
            'product_id': plan.id,
            'period_start': date(2026, 1, 1),
            'period_end': date(2026, 1, 31),
            'payment_date': date(2026, 1, 1),
        })

    def setUp(self):
        super().setUp()
        # Core coupon template renders a barcode; stub it only where the local env can't draw it.
        try:
            self.env['ir.actions.report'].barcode('QR', 'probe')
        except Exception:
            self.startPatcher(patch.object(type(self.env['ir.actions.report']), 'barcode', return_value=b''))

    # ------------------------------------------------------------------ helpers
    @contextmanager
    def _capture_mails(self):
        """Record every mail.mail created, with its body as created (force_send + auto_delete
        would unlink it before a search could see it)."""
        MailMail = type(self.env['mail.mail'])
        original = MailMail.create
        captured = []

        def create(this, vals_list):
            mails = original(this, vals_list)
            captured.extend({'model': m.model, 'res_id': m.res_id, 'body': str(m.body_html or ''),
                             'state': m.state, 'failure_type': m.failure_type,
                             'recipients': m.recipient_ids, 'email_to': m.email_to}
                            for m in mails)
            return mails

        with patch.object(MailMail, 'create', create):
            yield captured

    @staticmethod
    def _mails(captured, card):
        return [m for m in captured if m['model'] == 'loyalty.card' and m['res_id'] == card.id]

    def _bodies(self, captured, card):
        return [m['body'] for m in self._mails(captured, card)]

    def _vals(self, program, kind='included', points=3, cw=True):
        return {
            'program_id': program.id,
            'partner_id': self.partner.id,
            'points': points,
            'expiration_date': date(2026, 1, 31),
            'cw_period_id': self.period.id if cw else False,
            'cw_kind': kind if cw else False,
        }

    def _create_one(self, program, **kw):
        with self._capture_mails() as captured:
            card = self.env['loyalty.card'].create(self._vals(program, **kw))
        bodies = self._bodies(captured, card)
        self.assertEqual(len(bodies), 1, 'exactly one creation e-mail per card')
        return card, bodies[0]

    def _assert_no_layout(self, body, program):
        for marker in ODOO_LAYOUT_MARKERS:
            self.assertNotIn(marker, body, 'Odoo layout leaked into the CW e-mail: %r' % marker)
        self.assertNotIn('CW Sub', body, 'internal program name visible to the customer')
        self.assertNotIn(program.name, body)
        # dev notes in the template must never reach the customer (the manual Send path keeps them)
        self.assertNotIn('<!--', body, 'HTML comment in the outgoing e-mail')

    def _assert_core_layout(self, body):
        self.assertIn('Powered by', body, 'core layout expected for non-CW cards')
        self.assertIn('Your Loyalty Coupon', body)

    # ------------------------------------------------------------------ tests
    def test_20_cw_creation_email_has_no_odoo_layout(self):
        program = self._program('CW Sub – Sedan Full Wash', template=self.template)
        card, body = self._create_one(program, points=3)
        self._assert_no_layout(body, program)
        self.assertIn(card.code, body)
        self.assertIn(card._cw_booking_url(), body)
        self.assertIn('Book your wash', body)
        self.assertIn('WAYAKIT', body)
        self.assertNotIn('Wayakit Car Wash', body)
        self.assertNotIn('barcode', body.lower())
        # official logo, used as-is, served from the module with an absolute URL
        logo = card._cw_base_url() + '/wayakit_carwash_subscription/static/src/img/WAYAKIT-AR_B-horizontal_white.png'
        self.assertIn('src="%s"' % logo, body)
        self.assertTrue(os.path.isfile(os.path.join(
            MODULE_DIR, 'static', 'src', 'img', 'WAYAKIT-AR_B-horizontal_white.png')))
        self.assertRegex(body, r'<img[^>]*alt="WAYAKIT"')
        self.assertNotIn('wayakit_logo.png', body)

    def test_21_cw_footer_carries_company_contact(self):
        program = self._program('CW Sub – Sedan Full Wash', template=self.template)
        card, body = self._create_one(program)
        self.assertIn(LEGAL_NAME, body)
        # WhatsApp Business link (wa.me), not the company phone as tel:
        self.assertIn('href="https://wa.me/966545754908"', body)
        self.assertIn('+966 54 575 4908', body)
        self.assertNotIn('tel:', body)
        self.assertNotIn(FAKE_PHONE, body)
        self.assertIn(FAKE_EMAIL, body, 'company e-mail missing from the footer')
        self.assertIn('href="mailto:%s"' % FAKE_EMAIL, body)
        self.assertTrue(FAKE_WEBSITE in body or 'href="%s"' % card._cw_base_url() in body
                        or 'href="%s/"' % card._cw_base_url() in body, 'website link missing')
        for hard_coded in ('info@wayakit.com', 'Thuwal'):
            self.assertNotIn(hard_coded, body, 'contact data must come from the company, not hard-coded')

        # the number can be changed with a system parameter, no code change
        self.env['ir.config_parameter'].sudo().set_param(
            'wayakit_carwash_subscription.whatsapp_number', '+966 50 000 0009')
        _card, body_wa = self._create_one(program)
        self.assertIn('href="https://wa.me/966500000009"', body_wa)
        self.assertIn('+966 50 000 0009', body_wa)
        self.env['ir.config_parameter'].sudo().set_param('wayakit_carwash_subscription.whatsapp_number', False)
        for word in ('margin', 'cost'):
            self.assertNotIn(word, re.sub(r'style="[^"]*"', '', body).lower(), 'internal data: %s' % word)

        # contact data follows the company record (not hard-coded)
        self.ksa.write({'name': 'Fake Renamed Company LLC', 'phone': '+966 00 000 0002',
                        'email': 'cw-fake-company-2@example.com'})
        _card, body2 = self._create_one(program)
        self.assertIn('Fake Renamed Company LLC', body2)
        self.assertIn('cw-fake-company-2@example.com', body2)
        self.assertNotIn(FAKE_EMAIL, body2)
        self.assertNotIn(LEGAL_NAME, body2)

        # no phone / no e-mail on the company: no empty tel:/mailto: links, e-mail still renders
        self.ksa.write({'phone': False, 'email': False})
        card3, body3 = self._create_one(program)
        self.assertIn(card3.code, body3)
        self.assertNotIn('tel:', body3)
        self.assertNotIn('mailto:', body3)
        self.assertIn('Message us on WhatsApp', body3)
        self.assertIn('href="https://wa.me/966545754908"', body3)

    def test_22_reward_kinds_have_no_layout(self):
        program = self._program('CW Sub – Sedan Exterior', template=self.template)
        for kind in ('monthly_reward', 'streak_reward'):
            card, body = self._create_one(program, kind=kind, points=1)
            self._assert_no_layout(body, program)
            self.assertIn(card.code, body, kind)
            self.assertIn('transferable', body.lower(), kind)

    def test_23_scope_period_or_cw_template(self):
        """CW = issued by a period OR program whose creation e-mail is the CW template."""
        other = self._program('Fake Review Coupon')
        _card, body = self._create_one(other, cw=False)
        self._assert_core_layout(body)
        # card made by hand (Generate coupons) on a program that sends the CW template: no layout
        cw_program = self._program('CW Sub – SUV Full Wash', template=self.template)
        card, body = self._create_one(cw_program, cw=False)
        self.assertFalse(card.cw_period_id)
        self._assert_no_layout(body, cw_program)
        self.assertIn(card.code, body)
        # keyed on the template, not the name: a "CW Sub" program still on the core template
        cw_named_generic = self._program('CW Sub – SUV Exterior')
        _card, body = self._create_one(cw_named_generic, cw=False)
        self._assert_core_layout(body)

    def test_24_mixed_batch_in_one_create(self):
        cw_program = self._program('CW Sub – Sedan Full Wash', template=self.template)
        other = self._program('Fake Review Coupon')
        with self._capture_mails() as captured:
            cards = self.env['loyalty.card'].create([
                self._vals(other, cw=False),
                self._vals(cw_program, points=3),
                self._vals(other, cw=False),
                self._vals(cw_program, kind='monthly_reward', points=1),
            ])
        for card in cards:
            bodies = self._bodies(captured, card)
            self.assertEqual(len(bodies), 1, card.program_id.name)
            if card.cw_period_id:
                self._assert_no_layout(bodies[0], cw_program)
                self.assertIn(card.code, bodies[0])
            else:
                self._assert_core_layout(bodies[0])

    def test_25_no_mail_contexts_respected(self):
        cw_program = self._program('CW Sub – Sedan Full Wash', template=self.template)
        other = self._program('Fake Review Coupon')
        for ctx in ({'loyalty_no_mail': True}, {'action_no_send_mail': True}):
            with self._capture_mails() as captured:
                cards = self.env['loyalty.card'].with_context(**ctx).create(
                    [self._vals(cw_program), self._vals(other, cw=False)])
                cards.with_context(**ctx)._send_creation_communication()
                cards.with_context(**ctx)._send_creation_communication(force_send=True)
            self.assertFalse(captured, 'e-mail sent despite %s' % ctx)

    def test_26_force_send_and_no_partner(self):
        cw_program = self._program('CW Sub – Sedan Full Wash', template=self.template)
        Card = self.env['loyalty.card'].with_context(loyalty_no_mail=True)
        card = Card.create(self._vals(cw_program)).with_context(loyalty_no_mail=False)
        anonymous = Card.create(dict(self._vals(cw_program), partner_id=False)).with_context(loyalty_no_mail=False)
        MailTemplate = type(self.env['mail.template'])
        original = MailTemplate.send_mail
        calls = []

        def send_mail(this, res_id, *args, **kwargs):
            calls.append((res_id, kwargs))
            return original(this, res_id, *args, **kwargs)

        with patch.object(MailTemplate, 'send_mail', send_mail), self._capture_mails() as captured:
            # sale_loyalty confirms orders with force_send=True
            (card | anonymous)._send_creation_communication(force_send=True)
        self.assertEqual([c[0] for c in calls], [card.id], 'no e-mail for a card without partner')
        self.assertTrue(calls[0][1].get('force_send'), 'force_send not forwarded for CW cards')
        self.assertFalse(calls[0][1].get('email_layout_xmlid'))
        bodies = self._bodies(captured, card)
        self.assertEqual(len(bodies), 1)
        self._assert_no_layout(bodies[0], cw_program)

    def _manual_send(self, card):
        action = card.action_coupon_send()
        self.assertEqual(action['res_model'], 'mail.compose.message')
        with self._capture_mails() as captured:
            composer = self.env['mail.compose.message'].with_context(action['context']).create({})
            composer.action_send_mail()
        return self._bodies(captured, card)

    def test_27_manual_send_cw_card_has_no_layout(self):
        program = self._program('CW Sub – Sedan Full Wash', template=self.template)
        card = self.env['loyalty.card'].with_context(loyalty_no_mail=True).create(self._vals(program))
        bodies = self._manual_send(card)
        self.assertEqual(len(bodies), 1, 'manual "Send" must produce one e-mail')
        self._assert_no_layout(bodies[0], program)
        self.assertIn(card.code, bodies[0])
        self.assertIn(card._cw_booking_url(), bodies[0])

    def test_27b_manual_send_hand_made_card_on_cw_program(self):
        program = self._program('CW Sub – SUV Full Wash', template=self.template)
        card = self.env['loyalty.card'].with_context(loyalty_no_mail=True).create(self._vals(program, cw=False))
        self.assertFalse(card.cw_period_id)
        ctx = card.action_coupon_send()['context']
        self.assertEqual(ctx.get('default_composition_mode'), 'mass_mail')
        self.assertFalse(ctx.get('default_email_layout_xmlid'))
        bodies = self._manual_send(card)
        self.assertEqual(len(bodies), 1)
        self._assert_no_layout(bodies[0], program)
        self.assertIn(card.code, bodies[0])

    def test_28_manual_send_non_cw_card_unchanged(self):
        other = self._program('Fake Review Coupon')
        card = self.env['loyalty.card'].with_context(loyalty_no_mail=True).create(self._vals(other, cw=False))
        ctx = card.action_coupon_send()['context']
        self.assertEqual(ctx.get('default_email_layout_xmlid'), 'mail.mail_notification_light')
        self.assertEqual(ctx.get('default_composition_mode'), 'comment')
        bodies = self._manual_send(card)
        self.assertEqual(len(bodies), 1)
        self.assertIn('Your Loyalty Coupon', bodies[0], 'core manual send keeps its layout')

    def test_28b_manual_send_reaches_blacklisted_address(self):
        """The coupon is paid, not marketing: a newsletter unsubscribe must not cancel it."""
        program = self._program('CW Sub – Sedan Full Wash', template=self.template)
        card = self.env['loyalty.card'].with_context(loyalty_no_mail=True).create(self._vals(program))
        self.env['mail.blacklist'].sudo()._add(self.partner.email)
        action = card.action_coupon_send()
        with self._capture_mails() as captured:
            composer = self.env['mail.compose.message'].with_context(action['context']).create({})
            composer.action_send_mail()
        mails = self._mails(captured, card)
        self.assertEqual(len(mails), 1)
        mail = mails[0]
        self.assertNotEqual(mail['state'], 'cancel',
                            'coupon e-mail cancelled (%s)' % mail['failure_type'])
        self.assertFalse(mail['failure_type'])
        self.assertTrue(self.partner in mail['recipients'] or mail['email_to'] == self.partner.email,
                        'coupon e-mail not addressed to the card holder')

    def test_29_migration_reloads_template_from_xml(self):
        """noupdate template: a plain -u keeps the old body, the 1.2.1 migration must rewrite it."""
        module = _load_module_file('migrations/17.0.1.2.1/post-migrate.py')
        tmpl = self.template
        imd = self.env['ir.model.data'].search([
            ('module', '=', 'wayakit_carwash_subscription'), ('name', '=', 'mail_template_cw_coupon')])
        self.assertTrue(imd.noupdate)
        generic_body = str(self.generic.body_html)
        program = self._program('CW Sub – Sedan Full Wash', template=self.template)
        stale = {'en_US': '<p>FAKE OLD BODY en</p>', 'ar_001': '<p>FAKE OLD BODY ar</p>'}
        self.env.cr.execute(
            "UPDATE mail_template SET body_html = %s::jsonb, subject = %s::jsonb WHERE id = %s",
            [json.dumps(stale), json.dumps({'en_US': 'FAKE OLD SUBJECT'}), tmpl.id])
        tmpl.invalidate_recordset()
        self.assertIn('FAKE OLD BODY', str(tmpl.body_html), 'precondition')

        Program = type(self.env['loyalty.program'])
        with patch.object(Program, '_cw_use_coupon_template', autospec=True) as switch:
            module.migrate(self.env.cr, '17.0.1.2.0')
            tmpl.invalidate_recordset()
            reloaded = str(tmpl.body_html)
            self.assertNotIn('FAKE OLD', reloaded, 'body not reloaded from the XML')
            self.assertIn('Book your wash', reloaded)
            self.assertIn('object._cw_booking_url()', reloaded, 'raw QWeb source expected in body_html')
            self.assertNotIn('FAKE OLD', tmpl.subject)
            module.migrate(self.env.cr, '17.0.1.2.0')  # idempotent
            tmpl.invalidate_recordset()
            self.assertEqual(str(tmpl.body_html), reloaded)
        switch.assert_not_called()

        self.env.cr.execute("SELECT body_html FROM mail_template WHERE id = %s", [tmpl.id])
        stored = self.env.cr.fetchone()[0]
        self.assertNotIn('FAKE OLD', json.dumps(stored), 'stale translation kept: %s' % list(stored))
        imd.invalidate_recordset()
        self.assertTrue(imd.noupdate, 'template must stay noupdate after the migration')
        self.assertEqual(self.env['ir.model.data'].search_count([
            ('module', '=', 'wayakit_carwash_subscription'), ('name', '=', 'mail_template_cw_coupon')]), 1)
        self.assertEqual(str(self.generic.body_html), generic_body, 'core coupon template touched')
        self.assertEqual(self._create_lines(program).mail_template_id, self.template)
        # the reloaded template still renders
        card, body = self._create_one(program)
        self.assertIn(card.code, body)

