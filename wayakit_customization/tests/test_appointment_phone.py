# -*- coding: utf-8 -*-
"""Standalone self-check for the appointment phone rule (no Odoo needed).

Run:  python3 wayakit_customization/tests/test_appointment_phone.py
 or:  pytest wayakit_customization/tests/test_appointment_phone.py

KSA appointment types (Car Wash Care, HOME Deep Cleaning Care, Al Khozama)
only accept a local Saudi mobile: 05 + 8 digits, spaces allowed. The browser
pattern and the server check must agree, otherwise a customer either gets
past the form and bounced by the controller (lost booking) or the server
accepts what the form was meant to refuse. All numbers below are fake.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'models'))

from appointment_phone import (  # noqa: E402
    GENERIC_PHONE_PATTERN,
    KSA_PHONE_PATTERN,
    phone_input_attrs,
    phone_is_valid,
)

KSA_PASS = [
    '0512345678',
    '05 1234 5678',
    '051 234 5678',
    ' 0512345678 ',
    '05X XXX XXXX'.replace('X', '1'),   # placeholder shape, filled in
    '0 5 1 2 3 4 5 6 7 8',
    '\t0512345678\t',
]

KSA_FAIL = [
    '+966512345678',
    '966512345678',
    '+966 51 234 5678',
    '00966512345678',
    '051234567',        # 9 digits
    '05123456789',      # 11 digits
    '0612345678',       # not a mobile prefix
    '5123456789',       # missing leading 0
    '512345678',        # international without country code
    '05-1234-5678',
    '(05) 1234 5678',
    '05.1234.5678',
    '٠٥١٢٣٤٥٦٧٨',  # Arabic-Indic
    '۰۵۱۲۳۴۵۶۷۸',  # Extended (Persian)
    '05١٢٣٤٥٦٧٨',            # mixed
    '０５１２３４５６７８',  # fullwidth
    '0512345678x',
    'abc',
    '   ',
    '',
    None,
]


# ---- server rule, KSA ------------------------------------------------------

def test_ksa_accepts_local_mobile():
    for s in KSA_PASS:
        assert phone_is_valid(s, True), repr(s)


def test_ksa_rejects_everything_else():
    for s in KSA_FAIL:
        assert not phone_is_valid(s, True), repr(s)


def test_ksa_rejects_966_forms_that_old_rule_accepted():
    # These had >= 8 digits, so the old generic rule let them through.
    for s in ('+966512345678', '966512345678'):
        assert phone_is_valid(s, False), repr(s)
        assert not phone_is_valid(s, True), repr(s)


def test_ksa_empty_and_none():
    assert phone_is_valid('', True) is False
    assert phone_is_valid(None, True) is False


def test_returns_real_bool():
    # The controller only does `if not ...`, but keep it a clean bool.
    assert phone_is_valid('0512345678', True) is True
    assert phone_is_valid('12345678', False) is True
    assert phone_is_valid('1234567', False) is False


# ---- server rule, non-KSA (Mexico) -----------------------------------------

def test_generic_accepts_international_and_8_digits():
    for s in ('+52 55 1234 5678', '12345678', '0512345678', '+966512345678'):
        assert phone_is_valid(s, False), repr(s)


def test_generic_rejects_short_and_none():
    for s in ('1234567', '', None, '+52 123 4'):
        assert not phone_is_valid(s, False), repr(s)


# ---- input attributes ------------------------------------------------------

def test_ksa_input_attrs():
    attrs = phone_input_attrs(True)
    assert attrs['pattern'] == KSA_PHONE_PATTERN
    assert attrs['placeholder'] == '05X XXX XXXX'
    assert '05' in attrs['title']
    assert all(isinstance(v, str) for v in attrs.values())


def test_generic_input_attrs_have_no_05():
    attrs = phone_input_attrs(False)
    assert attrs['pattern'] == GENERIC_PHONE_PATTERN
    for key, value in attrs.items():
        assert '05' not in value, (key, value)
    # Must override the native e.g. +1(605)... placeholder too, so the key
    # has to be present (QWeb t-att wins over the static attribute).
    assert 'placeholder' in attrs


def test_attrs_are_fresh_dicts():
    # t-att must not be able to mutate a shared module-level dict.
    a = phone_input_attrs(True)
    a['pattern'] = 'tampered'
    assert phone_input_attrs(True)['pattern'] == KSA_PHONE_PATTERN


def test_patterns_compile_as_browser_would():
    # HTML wraps pattern as ^(?:pattern)$ (with the v flag in current
    # browsers); at least make sure it compiles anchored in Python.
    for p in (KSA_PHONE_PATTERN, GENERIC_PHONE_PATTERN):
        re.compile('^(?:%s)$' % p, re.ASCII)


# ---- browser vs server agreement -------------------------------------------

def _browser_accepts(s):
    # HTML pattern: implicitly anchored, ASCII \d. An empty value is caught
    # by required="1" instead of the pattern, so treat it as refused.
    if not s:
        return False
    return re.fullmatch(KSA_PHONE_PATTERN, s, re.ASCII) is not None


def test_browser_and_server_agree_ksa():
    for s in KSA_PASS + KSA_FAIL:
        assert _browser_accepts(s) == phone_is_valid(s, True), \
            '%r: browser=%s server=%s' % (s, _browser_accepts(s), phone_is_valid(s, True))


GENERIC_SAMPLES = [
    '+52 55 1234 5678', '12345678', '1234567', '+966512345678',
    '0512345678', '05-1234-5678',
    # Space-padded short numbers: 8+ characters but fewer than 8 digits.
    # required="1" does not stop whitespace, so these reach the pattern.
    '        ',          # 8 spaces
    '1234 567',          # 7 digits + 1 space
    '+1234 567',
    '1 2 3 4 5 6 7',     # 7 digits, 13 chars
    '1 2 3 4',           # 7 chars: pattern already refuses, kept as control
]


def test_browser_and_server_agree_generic():
    # The generic pattern may be stricter than the server (only digits,
    # spaces and +), which is fine; but anything the browser lets through
    # must pass the server, or a Mexico customer fills the form and gets
    # bounced to failed-phone.
    leaks = [s for s in GENERIC_SAMPLES
             if re.fullmatch(GENERIC_PHONE_PATTERN, s, re.ASCII)
             and not phone_is_valid(s, False)]
    assert not leaks, 'browser accepts, server rejects: %r' % leaks


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn()
            print('ok  %s' % name)
    print('all green')
