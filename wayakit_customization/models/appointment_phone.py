# -*- coding: utf-8 -*-
"""Pure phone rules for the public appointment form.

No Odoo imports on purpose (same trick as whatsapp_channel_name.py), so the
rules can be checked with plain python. The template and the controller both
read from here: the HTML pattern only guides the customer, the server check is
the real guard because a POST can skip the browser.
"""

import re

# KSA bookings (Car Wash Care, HOME Deep Cleaning Care, Al Khozama) are the
# types published on a website of this company. Mexico types (no website)
# keep the generic rule.
KSA_COMPANY_ID = 1

MIN_PHONE_DIGITS = 8

# The KSA service team calls back on local mobiles only: 05 + 8 digits. The
# +966 / 966 forms were rejected on purpose. [0-9], not \d: Python's \d also
# matches Arabic-Indic digits, which the browser pattern (ASCII \d) refuses.
_KSA_MOBILE = re.compile(r'05[0-9]{8}')

# HTML pattern attributes are implicitly anchored (^(?:...)$). The generic one
# counts digits, not characters: "1234 567" must fail in the browser, not
# after submit, where the customer loses the slot and every answer.
KSA_PHONE_PATTERN = r'\s*0\s*5(\s*\d){8}\s*'
GENERIC_PHONE_PATTERN = r'\s*\+?(\s*\d){8,20}\s*'


def phone_is_valid(phone, is_ksa):
    """True if `phone` passes the booking rule of a KSA / non-KSA type.
    KSA: 05 followed by 8 digits, spaces allowed. Other: at least 8 digits."""
    if is_ksa:
        return bool(_KSA_MOBILE.fullmatch(re.sub(r'\s', '', phone or '')))
    return len(re.sub(r'\D', '', phone or '')) >= MIN_PHONE_DIGITS


def phone_input_attrs(is_ksa):
    """HTML attributes of the form's phone input (fed to QWeb t-att)"""
    if is_ksa:
        return {
            'pattern': KSA_PHONE_PATTERN,
            'title': "Please enter a Saudi mobile number starting with 05, e.g. 05X XXX XXXX",
            'placeholder': "05X XXX XXXX",
            'inputmode': "numeric",
        }
    return {
        'pattern': GENERIC_PHONE_PATTERN,
        'title': "Please enter a valid phone number (at least 8 digits)",
        'placeholder': "Phone number",
    }
