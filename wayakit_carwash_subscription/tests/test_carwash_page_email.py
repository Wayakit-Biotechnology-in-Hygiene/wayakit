import importlib.util
import os
import re
from datetime import date

from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tools import format_date

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAN_CODES = ('FP-CWS-00103', 'FP-CWS-00104', 'FP-CWS-00203', 'FP-CWS-00204', 'FP-CWS-00105', 'FP-CWS-00205')
KSA_COMPANY_ID = 1
# Internal section of Dalal's proposal: must never reach the public page.
INTERNAL_WORDS = ('Indicative profitability', 'gross margin', 'Est. gross margin')


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

@tagged('post_install', '-at_install')
class TestCarwashCouponEmail(CarwashPageCommon, TransactionCase):
    """Deliverable 2: coupon e-mail with 'Book your wash' + switch of the CW Sub programs."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_common()
        cls.env = cls.env(context=dict(cls.env.context, allowed_company_ids=[KSA_COMPANY_ID]))
        cls.template = cls.env.ref('wayakit_carwash_subscription.mail_template_cw_coupon')
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
        expiry_shown = {format_date(self.env, card.expiration_date),
                        format_date(self.env, card.expiration_date, lang_code=self.partner.lang or 'en_US'),
                        card.expiration_date.isoformat()}
        self.assertTrue(any(e in body for e in expiry_shown), 'expiry date not shown in the e-mail')
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
