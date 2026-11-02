# -*- coding: utf-8 -*-
"""Pure helper that puts "[PREFIX] " in front of a WhatsApp channel name.

No Odoo imports on purpose (same trick as
wayakit_customization/models/whatsapp_channel_name.py), so
tests/test_channel_prefix.py runs with plain python.
"""
import re


# Must stay within what wayakit_customization's channel_name() treats as a
# leading tag (whatsapp_channel_name._LEADING_TAG); a wider prefix, e.g. one
# with "]", would be split by it and both modules would grow names per deploy.
VALID_PREFIX = re.compile(r"^[A-Za-z0-9]{1,6}$")


def is_valid_prefix(prefix):
    """Empty (no tag) or 1-6 letters/digits, brackets/spaces around allowed."""
    prefix = _clean(prefix)
    return not prefix or bool(VALID_PREFIX.match(prefix))


def _clean(prefix):
    """'KSA' out of 'KSA', ' [KSA] ' or '[KSA]'; '' for nothing."""
    return (prefix or "").strip().strip("[]").strip()


def _strip(label, prefix):
    """Drop every leading "[prefix]" -- also heals an already doubled one."""
    prefix = _clean(prefix)
    if not prefix:
        return label
    pattern = re.compile(r"^\[%s\]\s*" % re.escape(prefix))
    while True:
        stripped = pattern.sub("", label, count=1)
        if stripped == label:
            return label
        label = stripped


def prefixed_name(prefix, name, old_prefix=None):
    """Return ``"[PREFIX] <name>"``.

    Idempotent on the tag already present, NOT on "the name changed": it runs on
    every create/rename and from the retroactive action, so a version that only
    compared names would stack one tag per pass.

    :param str prefix: the account's channel_prefix; empty means "no tag", and
      the tag of ``old_prefix`` (if any) is removed.
    :param str name: current channel name.
    :param str old_prefix: previous prefix of the account, removed first so that
      changing KSA -> SA replaces the tag instead of adding a second one.
    """
    label = (name or "").strip()
    for tag in (old_prefix, prefix):
        label = _strip(label, tag)
    prefix = _clean(prefix)
    if not prefix:
        return label
    return "[%s] %s" % (prefix, label) if label else "[%s]" % prefix
