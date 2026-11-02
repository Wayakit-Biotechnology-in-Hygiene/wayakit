# -*- coding: utf-8 -*-
"""Standalone self-check for prefixed_name (no Odoo needed).

Run:  python3 wayakit_whatsapp_prefix/tests/test_channel_prefix.py

Also replays the interaction with wayakit_customization's channel_name(), which
rewrites every WhatsApp channel name on each upgrade of that module: the two
together must settle after one pass, never grow.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ADDONS = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ADDONS, 'wayakit_whatsapp_prefix', 'models'))
sys.path.insert(0, os.path.join(ADDONS, 'wayakit_customization', 'models'))

from channel_prefix import is_valid_prefix, prefixed_name  # noqa: E402
from whatsapp_channel_name import channel_name  # noqa: E402

NUMBER = '+966583851020'
NATIVE = '+966583851020 · Ghamer Agency (S02125)'


def test_new_channel_gets_prefix():
    assert prefixed_name('KSA', NATIVE) == '[KSA] ' + NATIVE
    assert prefixed_name('MX', '+529935180743 · mafer') == '[MX] +529935180743 · mafer'


def test_prefix_not_duplicated():
    once = prefixed_name('KSA', NATIVE)
    assert prefixed_name('KSA', once) == once
    assert prefixed_name('KSA', prefixed_name('KSA', once)) == once


def test_doubled_prefix_is_healed():
    assert prefixed_name('KSA', '[KSA] [KSA]  X') == '[KSA] X'


def test_prefix_typed_with_brackets_or_spaces():
    assert prefixed_name('[KSA]', 'X') == '[KSA] X'
    assert prefixed_name(' KSA ', '[KSA]X') == '[KSA] X'


def test_prefix_change_replaces_tag():
    assert prefixed_name('SA', '[KSA] X', old_prefix='KSA') == '[SA] X'


def test_empty_prefix_removes_old_tag():
    assert prefixed_name('', '[KSA] X', old_prefix='KSA') == 'X'
    assert prefixed_name(None, 'X') == 'X'


def test_other_bracket_text_is_kept():
    # A tag that is not ours (hand-typed) is not eaten.
    assert prefixed_name('KSA', '[VIP] X') == '[KSA] [VIP] X'


def test_empty_name():
    assert prefixed_name('KSA', '') == '[KSA]'


def test_settles_with_number_rename_both_orders():
    # Upgrade of wayakit_customization (number backfill) after the prefix...
    prefixed = prefixed_name('KSA', NATIVE)
    assert channel_name(NUMBER, prefixed) == prefixed
    # ...and the prefix write after the number backfill: N passes == 1 pass.
    name = 'Ghamer Agency'
    for _ in range(3):
        name = prefixed_name('KSA', channel_name(NUMBER, name))
    assert name == '[KSA] +966583851020 · Ghamer Agency'


def test_prefix_validation():
    for ok in ('KSA', 'MX', '[KSA]', ' mx ', '', None):
        assert is_valid_prefix(ok), ok
    # "]" would be split by channel_name()'s tag regex -> growth per deploy.
    for bad in ('K]SA', 'K SA', 'TOOLONG', 'K-SA'):
        assert not is_valid_prefix(bad), bad


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn()
            print('ok  %s' % name)
    print('all green')
