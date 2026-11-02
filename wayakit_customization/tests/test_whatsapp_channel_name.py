# -*- coding: utf-8 -*-
"""Standalone self-check for channel_name (no Odoo needed).

Run:  python wayakit_customization/tests/test_whatsapp_channel_name.py

Guards the rule that is easy to break and annoying to undo: the rename must be
idempotent. It runs on every module upgrade, so a non-idempotent version would
stack one number per deploy onto ~387 production channels.
"""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'models'))

from whatsapp_channel_name import channel_name  # noqa: E402

NUMBER = '+966583851020'


def test_number_and_push_name():
    assert channel_name(NUMBER, 'Ghamer Agency') == '+966583851020 · Ghamer Agency'


def test_number_without_push_name():
    # No dangling separator when there is nothing to append.
    assert channel_name(NUMBER, '') == NUMBER
    assert channel_name(NUMBER, None) == NUMBER


def test_keeps_record_name_suffix():
    assert channel_name(NUMBER, 'Nimo Abuhaimed (S02125)') == \
        '+966583851020 · Nimo Abuhaimed (S02125)'


def test_idempotent():
    once = channel_name(NUMBER, 'Ghamer Agency')
    assert channel_name(NUMBER, once) == once
    assert channel_name(NUMBER, channel_name(NUMBER, once)) == once


def test_idempotent_with_record_name_suffix():
    once = channel_name(NUMBER, 'Nimo Abuhaimed (S02125)')
    assert channel_name(NUMBER, once) == once


def test_legacy_name_is_the_number():
    # Pre-July channels: the name already WAS the number, with or without '+'.
    assert channel_name(NUMBER, NUMBER) == NUMBER
    assert channel_name(NUMBER, '966583851020') == NUMBER
    assert channel_name(NUMBER, '+966583851020 (S02125)') == '+966583851020 (S02125)'


def test_spaced_number_is_normalised_not_duplicated():
    # We cannot read res.partner._format_wa_phone, so the native name may well
    # arrive as INTERNATIONAL format. It must be rewritten, never prepended to.
    assert channel_name(NUMBER, '+966 58 385 1020') == NUMBER
    assert channel_name(NUMBER, '+966 58 385 1020 \u00b7 Ghamer Agency') == \
        '+966583851020 \u00b7 Ghamer Agency'
    assert channel_name(NUMBER, '+966 58 385 1020 (S02125)') == '+966583851020 (S02125)'


def test_different_number_in_name_does_not_match():
    # Someone hand-renamed a channel putting another number in front: the real
    # number still goes first. Accepted edge case, see CLAUDE-customizations.md.
    assert channel_name(NUMBER, '+966111111111 \u00b7 X').startswith(NUMBER + ' \u00b7 ')


def test_no_number_leaves_native_name_alone():
    # BSUID-only identifiers: Meta did not give us a number, so do not touch.
    assert channel_name('', 'Ghamer Agency') == 'Ghamer Agency'
    assert channel_name(None, 'Ghamer Agency') == 'Ghamer Agency'
    assert channel_name('no-digits-here', 'Ghamer Agency') == 'Ghamer Agency'


def test_no_number_and_no_name():
    assert channel_name('', '') == ''


def test_strips_whitespace():
    assert channel_name(NUMBER, '  Ghamer Agency  ') == '+966583851020 · Ghamer Agency'


def test_leading_tag_is_kept_in_front():
    # wayakit_whatsapp_prefix puts "[KSA] " before the number.
    once = '[KSA] +966583851020 · Ghamer Agency (S02125)'
    assert channel_name(NUMBER, once) == once
    assert channel_name(NUMBER, '[KSA] Ghamer Agency') == '[KSA] +966583851020 · Ghamer Agency'
    assert channel_name(NUMBER, '[KSA] 966583851020') == '[KSA] +966583851020'
    # Bracketed text that is not a short tag stays behind the number.
    assert channel_name(NUMBER, '[Ghamer Agency] Bob') == \
        '+966583851020 · [Ghamer Agency] Bob'


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn()
            print('ok  %s' % name)
    print('all green')
