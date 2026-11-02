# -*- coding: utf-8 -*-
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestDiscussChannelPrefix(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Dummy values, same shape as enterprise whatsapp/tests/common.py.
        cls.account = cls.env['whatsapp.account'].create({
            'name': 'Prefix test account',
            'account_uid': 'prefix-test-account',
            'app_secret': 'prefix-test-secret',
            'app_uid': 'prefix-test-app',
            'phone_uid': '000111222',
            'token': 'prefix-test-token',
            'channel_prefix': 'KSA',
        })

    def _new_channel(self, name):
        return self.env['discuss.channel'].create({
            'name': name,
            'channel_type': 'whatsapp',
            'wa_account_id': self.account.id,
            'whatsapp_number': '+966583851020',
        })

    def test_new_channel_gets_prefix(self):
        channel = self._new_channel('+966583851020 · Ghamer Agency')
        self.assertEqual(channel.name, '[KSA] +966583851020 · Ghamer Agency')

    def test_prefix_not_duplicated(self):
        channel = self._new_channel('[KSA] X')
        self.assertEqual(channel.name, '[KSA] X')
        channel.write({'name': '[KSA] Y'})
        self.assertEqual(channel.name, '[KSA] Y')
        channel.write({'name': 'Z'})
        self.assertEqual(channel.name, '[KSA] Z')

    def test_saving_prefix_renames_existing(self):
        channel = self._new_channel('X')
        self.account.channel_prefix = 'SA'
        self.assertEqual(channel.name, '[SA] X')
        self.assertEqual(self.account._wayakit_apply_channel_prefix(), 0)
