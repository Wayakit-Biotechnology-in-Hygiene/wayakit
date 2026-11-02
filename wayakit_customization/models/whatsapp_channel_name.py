# -*- coding: utf-8 -*-
"""Pure helper that builds the name of a WhatsApp discuss channel.

No Odoo imports on purpose: same trick as models/review_text.py and
trendyol_connector/models/mapping.py, so tests/test_whatsapp_channel_name.py
runs with plain python.

Background: enterprise commit 8fe680ab ("whatsapp_identifiers: support for
business-scoped user IDs", July 2026) made the channel name default to the
WhatsApp profile push name, leaving the phone number as a mere fallback. Sales
KSA copies that number to follow up once the 24h WhatsApp window closes, so we
put it back in front.
"""

import re

SEPARATOR = " · "  # middle dot; parentheses are taken by Odoo's record_name

# "[KSA] " put in front by wayakit_whatsapp_prefix. Peeled off before looking for
# the number: otherwise the number no longer leads, gets prepended again, the
# prefix module re-prefixes, and both grow by one per deploy. Kept as narrow as
# the prefixes that module accepts (1-6 letters/digits), so a push name such as
# "[Company] Bob" still gets the number in front.
_LEADING_TAG = re.compile(r"^(\[[A-Za-z0-9]{1,6}\])\s*")


def _digits(text):
    return "".join(char for char in (text or "") if char.isdigit())


def _leading_number_end(label, wanted):
    """Index just past the number at the start of ``label``, or 0 if none.

    Walks the digits instead of comparing whole strings so every shape the
    history contains lands on the same cut: '+966583851020', '966583851020' and
    '+966 58 385 1020' all end at the same place, and whatever follows -- a
    ' · Name' we added or a ' (S02125)' Odoo added -- is preserved verbatim.
    """
    found = ""
    for index, char in enumerate(label):
        if char.isdigit():
            found += char
            if found == wanted:
                return index + 1
        elif char not in "+ -":
            break
    return 0


def channel_name(number, current_name):
    """Return ``"<number> · <label>"``, number first.

    Number first and not last because the Discuss sidebar truncates long names
    ("Nimo Abuhaimed (S021..."); at the front the number is always readable.

    Idempotent on the number already present in the name, NOT on "the name
    changed" -- same lesson as trendyol_pushed_status / trendyol_alerted_status
    in CLAUDE.md. It runs on every module upgrade, so a non-idempotent version
    would stack one number per deploy onto every channel.

    :param str number: display-formatted number, e.g. '+966583851020'. Falsy or
      digit-less (the BSUID-only case) means "leave the native name alone".
    :param str current_name: the name Odoo built.
    """
    label = (current_name or "").strip()
    wanted = _digits(number)
    if not wanted:
        return label
    tag = _LEADING_TAG.match(label)
    if tag:
        rest = channel_name(number, label[tag.end():])
        return "%s %s" % (tag.group(1), rest) if rest else tag.group(1)
    cut = _leading_number_end(label, wanted)
    if cut:
        return number + label[cut:]     # already leads: just normalise the number
    return "%s%s%s" % (number, SEPARATOR, label) if label else number
