"""Standalone self-check for the pure mapping logic (no Odoo needed).

Run:  python3 salla_connector/tests/test_mapping.py

All secrets/tokens below are obviously fake test values.
"""
import ast
import hashlib
import hmac
import inspect
import io
import json
import math
import os
import re
import sys
import zipfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "models"))
import mapping  # noqa: E402

SECRET = "fake-webhook-secret-for-tests"
BODY = b'{"event":"order.created","merchant":1000000001,"data":{"id":42}}'
NOW = datetime(2026, 10, 15, 12, 0, 0)          # naive UTC, like Odoo stores it
NOW_TS = 1792065600.0                            # == NOW as epoch seconds


def _sign(body, secret=SECRET):
    if isinstance(body, str):
        body = body.encode("utf-8")
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


# --- verify_signature(raw_body, secret, signature) ---------------------------

def test_signature_valid():
    assert mapping.verify_signature(BODY, SECRET, _sign(BODY))


def test_signature_wrong_secret():
    assert not mapping.verify_signature(BODY, SECRET, _sign(BODY, "another-fake-secret"))


def test_signature_tampered_one_byte():
    tampered = BODY.replace(b'"id":42', b'"id":43')
    assert len(tampered) == len(BODY)
    assert not mapping.verify_signature(tampered, SECRET, _sign(BODY))
    # and one flipped hex char in the signature itself
    sig = _sign(BODY)
    flipped = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    assert not mapping.verify_signature(BODY, SECRET, flipped)


def test_signature_missing_or_empty_header():
    assert not mapping.verify_signature(BODY, SECRET, None)
    assert not mapping.verify_signature(BODY, SECRET, "")
    assert not mapping.verify_signature(BODY, SECRET, "   ")
    assert not mapping.verify_signature(BODY, SECRET, "sha256=")


def test_signature_empty_secret():
    # An unconfigured backend must never accept anything, not even a body
    # "signed" with the empty key (which anyone can compute).
    assert not mapping.verify_signature(BODY, "", _sign(BODY, ""))
    assert not mapping.verify_signature(BODY, None, _sign(BODY, ""))


def test_signature_case_whitespace_prefix():
    sig = _sign(BODY)
    assert mapping.verify_signature(BODY, SECRET, sig.upper())
    assert mapping.verify_signature(BODY, SECRET, "  %s\n" % sig)
    assert mapping.verify_signature(BODY, SECRET, "sha256=" + sig)
    assert mapping.verify_signature(BODY, SECRET, sig.encode("ascii"))   # header as bytes


def test_signature_bytes_vs_str_body():
    assert mapping.verify_signature(BODY.decode("utf-8"), SECRET, _sign(BODY))
    # non-ASCII body: str must be hashed as UTF-8, same as the raw bytes
    body = '{"name":"منظف"}'
    assert mapping.verify_signature(body, SECRET, _sign(body.encode("utf-8")))
    assert mapping.verify_signature(body.encode("utf-8"), SECRET, _sign(body))


def test_signature_is_over_raw_bytes():
    """Same JSON document, different bytes -> must FAIL. Proves nobody parses
    and re-serializes the body before hashing."""
    sig = _sign(BODY)
    doc = json.loads(BODY)
    respaced = json.dumps(doc).encode("utf-8")                       # ", " and ": "
    reordered = json.dumps(dict(reversed(list(doc.items()))),
                           separators=(",", ":")).encode("utf-8")
    assert json.loads(respaced) == json.loads(reordered) == doc
    assert respaced != BODY and reordered != BODY
    assert not mapping.verify_signature(respaced, SECRET, sig)
    assert not mapping.verify_signature(reordered, SECRET, sig)
    assert not mapping.verify_signature(BODY + b"\n", SECRET, sig)
    # same document, \\u escape vs raw UTF-8 encoding
    escaped = json.dumps({"n": "م"}, ensure_ascii=True).encode("utf-8")
    raw_utf8 = json.dumps({"n": "م"}, ensure_ascii=False).encode("utf-8")
    assert json.loads(escaped) == json.loads(raw_utf8) and escaped != raw_utf8
    assert not mapping.verify_signature(raw_utf8, SECRET, _sign(escaped))


def test_signature_empty_body():
    # signed empty body is valid (controller answers 200), unsigned is not
    assert mapping.verify_signature(b"", SECRET, _sign(b""))
    assert mapping.verify_signature("", SECRET, _sign(b""))
    assert not mapping.verify_signature(b"", SECRET, None)
    assert not mapping.verify_signature(b"", SECRET, _sign(b"x"))
    assert not mapping.verify_signature(None, SECRET, _sign(b""))   # missing body


def test_signature_garbage_header():
    for bad in ("not-hex-at-all", _sign(BODY)[:-1], _sign(BODY) + "0",
                "sha256=" + "z" * 64, "م" * 64, "0" * 64, 12345, ["x"]):
        assert not mapping.verify_signature(BODY, SECRET, bad), bad


def test_signature_uses_compare_digest():
    """Timing-safe comparison: digests must not be compared with ==."""
    src = inspect.getsource(mapping.verify_signature)
    assert "compare_digest" in src
    assert "== sig" not in src and "sig ==" not in src and "== expected" not in src


# --- authorize / tokens ------------------------------------------------------

AUTHORIZE = {
    "event": "app.store.authorize",
    "merchant": 1000000001,
    "created_at": "2026-10-15 12:00:00",
    "data": {
        "access_token": "FAKE_ACCESS_TOKEN",
        "refresh_token": "FAKE_REFRESH_TOKEN",
        "expires": 1700000000,
        "scope": "settings.read offline_access",
        "token_type": "bearer",
    },
}


def test_parse_authorize_full():
    t = mapping.parse_authorize(AUTHORIZE, now=NOW)
    assert t["access_token"] == "FAKE_ACCESS_TOKEN"
    assert t["refresh_token"] == "FAKE_REFRESH_TOKEN"
    assert t["expires_at"] == datetime(2023, 11, 14, 22, 13, 20)   # epoch s, naive UTC
    assert t["scope"] == "settings.read offline_access"
    assert t["store_id"] == "1000000001"                             # str, not int


def test_parse_authorize_missing_refresh_token():
    payload = dict(AUTHORIZE, data=dict(AUTHORIZE["data"]))
    del payload["data"]["refresh_token"]
    t = mapping.parse_authorize(payload, now=NOW)
    assert t["access_token"] == "FAKE_ACCESS_TOKEN"
    assert t["refresh_token"] == ""          # never the string "None"
    payload["data"]["refresh_token"] = None
    assert mapping.parse_authorize(payload, now=NOW)["refresh_token"] == ""


def test_parse_authorize_missing_data():
    assert mapping.parse_authorize({"event": "app.store.authorize", "merchant": 1}) is None
    assert mapping.parse_authorize(dict(AUTHORIZE, data={})) is None
    assert mapping.parse_authorize(dict(AUTHORIZE, data=None)) is None
    no_access = dict(AUTHORIZE, data={"refresh_token": "FAKE_REFRESH_TOKEN"})
    assert mapping.parse_authorize(no_access) is None
    blank_access = dict(AUTHORIZE, data=dict(AUTHORIZE["data"], access_token="   "))
    assert mapping.parse_authorize(blank_access) is None


def test_parse_authorize_flat_and_nested_merchant():
    flat = {"event": "app.store.authorize", "merchant": {"id": 77},
            "access_token": "FAKE_ACCESS_TOKEN", "expires_in": 3600}
    t = mapping.parse_authorize(flat, now=NOW)
    assert t["access_token"] == "FAKE_ACCESS_TOKEN"
    assert t["store_id"] == "77"
    assert t["expires_at"] == NOW + timedelta(hours=1)
    # merchant only inside data
    inner = {"data": dict(AUTHORIZE["data"], merchant=55)}
    assert mapping.parse_authorize(inner, now=NOW)["store_id"] == "55"
    # no merchant anywhere -> "" (pick_backend then decides), never "None"
    t = mapping.parse_authorize({"data": dict(AUTHORIZE["data"])}, now=NOW)
    assert t["store_id"] == ""


def test_parse_authorize_garbage():
    for junk in (None, "junk", 42, [], [AUTHORIZE], {"data": "x"}, {"data": [1, 2]}, True):
        assert mapping.parse_authorize(junk) is None, junk


def test_expires_epoch_vs_lifetime():
    # epoch seconds
    assert mapping.expires_at(1700000000, NOW) == datetime(2023, 11, 14, 22, 13, 20)
    # epoch milliseconds
    assert mapping.expires_at(1700000000000, NOW) == datetime(2023, 11, 14, 22, 13, 20)
    # lifetime in seconds (expires_in): Salla's 14 days
    assert mapping.expires_at(1209600, NOW) == NOW + timedelta(days=14)
    assert mapping.expires_at("1209600", NOW) == NOW + timedelta(days=14)
    # expires wins over expires_in when both are present
    t = mapping.parse_tokens({"access_token": "FAKE_ACCESS_TOKEN",
                              "expires": 1700000000, "expires_in": 60}, NOW)
    assert t["expires_at"] == datetime(2023, 11, 14, 22, 13, 20)


def test_expires_iso_strings():
    want = datetime(2026, 10, 29, 12, 0, 0)
    assert mapping.expires_at("2026-10-29 12:00:00", NOW) == want      # naive = UTC
    assert mapping.expires_at("2026-10-29T12:00:00", NOW) == want
    assert mapping.expires_at("2026-10-29T12:00:00Z", NOW) == want
    assert mapping.expires_at("2026-10-29T12:00:00z", NOW) == want
    assert mapping.expires_at(" 2026-10-29 12:00:00 ", NOW) == want
    # +03:00 (KSA) -> converted to naive UTC, not just stripped
    assert mapping.expires_at("2026-10-29T15:00:00+03:00", NOW) == want
    assert mapping.expires_at("2026-10-29 15:00:00+03:00", NOW) == want
    assert mapping.expires_at("2026-10-29T06:00:00-06:00", NOW) == want  # Mexico, crosses no day
    assert mapping.expires_at("2026-10-30T02:00:00+14:00", NOW) == want  # crosses the day
    res = mapping.expires_at("2026-10-29T15:00:00+03:00", NOW)
    assert res.tzinfo is None                                           # Odoo Datetime: naive
    for junk in ("2026-13-45 12:00:00", "2026-10-29 25:00:00", "29/10/2026", "tomorrow", "Z"):
        assert mapping.expires_at(junk, NOW) is None, junk


def test_expires_magnitude_boundaries():
    # 10**9 - 1 is still a LIFETIME in seconds from now
    assert mapping.expires_at(10 ** 9 - 1, NOW) == NOW + timedelta(seconds=10 ** 9 - 1)
    # 10**9 is the first EPOCH second: 2001-09-09 01:46:40 UTC
    assert mapping.expires_at(10 ** 9, NOW) == datetime(2001, 9, 9, 1, 46, 40)
    # 10**12 is the first EPOCH millisecond: the same instant
    assert mapping.expires_at(10 ** 12, NOW) == datetime(2001, 9, 9, 1, 46, 40)
    assert mapping.expires_at(str(10 ** 12), NOW) == datetime(2001, 9, 9, 1, 46, 40)
    # just under 10**12 is epoch seconds in year ~33658: out of datetime range -> None, no crash
    assert mapping.expires_at(10 ** 12 - 1, NOW) is None


def test_expires_garbage_is_none():
    for junk in (None, True, False, 0, -5, "", "abc", {}, []):
        assert mapping.expires_at(junk, NOW) is None, junk


def test_expires_non_finite_or_huge_does_not_crash():
    """A garbage `expires` must yield None (-> treated as needs-refresh), never raise
    inside the webhook: an exception there loses the authorize event and its tokens."""
    for junk in ("nan", float("nan"), "inf", float("inf"), 10 ** 20):
        try:
            value = mapping.expires_at(junk, NOW)
        except Exception as exc:  # noqa: BLE001
            raise AssertionError("expires_at(%r) raised %s: %s"
                                 % (junk, type(exc).__name__, exc))
        assert value is None, (junk, value)


def test_token_needs_refresh_boundaries():
    margin = timedelta(days=3)
    exp = NOW + margin
    assert mapping.token_needs_refresh(exp, NOW, margin)                       # exactly at margin
    assert not mapping.token_needs_refresh(exp + timedelta(seconds=1), NOW, margin)
    assert mapping.token_needs_refresh(exp - timedelta(seconds=1), NOW, margin)
    assert mapping.token_needs_refresh(NOW - timedelta(days=1), NOW, margin)   # past
    assert mapping.token_needs_refresh(NOW, NOW, timedelta(0))                 # expires now
    assert mapping.token_needs_refresh(None, NOW, margin)                      # unknown
    assert mapping.token_needs_refresh(False, NOW, margin)                     # Odoo empty field
    assert not mapping.token_needs_refresh(NOW + timedelta(days=14), NOW, margin)


# --- backend routing / events ------------------------------------------------

def test_event_and_merchant():
    assert mapping.event_name({"event": " App.Store.Authorize "}) == mapping.AUTHORIZE_EVENT
    assert mapping.event_name({}) == ""
    assert mapping.event_name("junk") == ""
    assert mapping.merchant_id({"merchant": 0}) == "0"
    assert mapping.merchant_id({"merchant": None}) == ""
    assert mapping.merchant_id({"merchant": True}) == ""      # never "True"
    assert mapping.merchant_id([]) == ""


def test_pick_backend():
    AUTH, ORDER = mapping.AUTHORIZE_EVENT, "order.created"
    # (1) exact store match wins, for every event, over an unbound backend
    for ev in (AUTH, ORDER, "", "app.uninstalled", "app.store.revoke"):
        assert mapping.pick_backend([(1, ""), (2, "55")], "55", ev) == 2, ev
    # (3) only authorize may take the first unbound backend (first install)
    assert mapping.pick_backend([(1, "99"), (2, False), (3, "")], "55", AUTH) == 2
    assert mapping.pick_backend([(1, "99"), (2, False)], "55") is None          # no event
    assert mapping.pick_backend([(1, "99"), (2, False)], "55", ORDER) is None
    assert mapping.pick_backend([(1, "99"), (2, False)], "55", "  APP.STORE.AUTHORIZE ") == 2
    # unknown store with every backend bound -> None (never overwrite another store)
    for ev in (AUTH, ORDER, ""):
        assert mapping.pick_backend([(1, "99"), (2, "98")], "55", ev) is None, ev
    # (4) non-revoke event with no merchant -> the single backend, only if exactly one
    assert mapping.pick_backend([(1, "99")], "", ORDER) == 1
    assert mapping.pick_backend([(1, "99")], None) == 1
    assert mapping.pick_backend([(1, "99"), (2, "98")], "", ORDER) is None
    assert mapping.pick_backend([(1, "")], "", AUTH) == 1        # first install, no merchant
    # authorize never lands on a BOUND backend without an exact store match: a misread
    # merchant must not let the demo store overwrite prod's tokens
    assert mapping.pick_backend([(1, "99")], "", AUTH) is None
    assert mapping.pick_backend([(1, "99")], None, AUTH) is None
    assert mapping.pick_backend([(1, "99"), (2, "98")], "", AUTH) is None
    assert mapping.pick_backend([(1, "99")], "55", AUTH) is None
    assert mapping.pick_backend([(1, "99"), (2, "")], "", AUTH) == 2   # only the unbound one
    assert mapping.pick_backend([], "55", AUTH) is None
    assert mapping.pick_backend(None, "55", AUTH) is None


def test_pick_backend_revoke():
    """(2) An uninstall on the demo store must never wipe another backend's tokens."""
    for ev in sorted(mapping.REVOKE_EVENTS):
        # foreign store, an unbound backend available -> still None
        assert mapping.pick_backend([(1, "99"), (2, "")], "55", ev) is None, ev
        assert mapping.pick_backend([(2, "")], "55", ev) is None, ev
        # no merchant at all -> None, even with a single candidate
        assert mapping.pick_backend([(1, "99")], "", ev) is None, ev
        assert mapping.pick_backend([(1, "")], None, ev) is None, ev
        # exact match -> that one
        assert mapping.pick_backend([(1, "99"), (2, "55"), (3, "")], "55", ev) == 2, ev
        assert mapping.pick_backend([(1, "99")], 99, ev.upper()) == 1, ev   # int merchant, case


def test_is_allowed_salla_url():
    """Exact hosts only: api.salla.dev and accounts.salla.sa. Whatever host passes
    receives the client secret, the single-use refresh token and the Bearer token."""
    for ok in ("https://api.salla.dev/admin/v2", "https://accounts.salla.sa/oauth2/token",
               "https://API.Salla.Dev/admin/v2", "HTTPS://ACCOUNTS.SALLA.SA/oauth2/token",
               "https://api.salla.dev:443/admin/v2", "  https://api.salla.dev/admin/v2  ",
               "https://api.salla.dev./admin/v2",          # trailing dot = same FQDN
               "https://accounts.salla.sa./oauth2/token"):
        assert mapping.is_allowed_salla_url(ok), ok
    for bad in (
            # no wildcard any more, no bare domain
            "https://salla.dev/", "https://x.salla.dev/", "https://store.salla.dev/admin/v2",
            "https://salla.sa/", "https://x.accounts.salla.sa/", "https://evilsalla.dev/",
            # scheme / port
            "http://api.salla.dev/admin/v2", "http://accounts.salla.sa/oauth2/token",
            "ftp://api.salla.dev/", "https://api.salla.dev:8443/admin/v2",
            "https://api.salla.dev:80/", "https://api.salla.dev:abc/",
            # host confusion
            "https://salla.dev.evil.com/", "https://api.salla.dev.evil.com/",
            "https://accounts.salla.sa.evil.com/", "https://evil.com/.salla.dev",
            "https://evil.com/api.salla.dev", "https://evil.com/?h=api.salla.dev",
            "https://evil.com#api.salla.dev",
            "https://evil.com\\@accounts.salla.sa/oauth2/token",   # backslash + @
            "https://accounts.salla.sa\\@evil.com/oauth2/token",   # and its reverse
            "https://api.salla.dev\\evil.com/", "https://api.salla.dev%2eevil.com/",
            "https://\u0430pi.salla.dev/admin/v2",                  # Cyrillic a
            "https://api.s\u0430lla.dev/admin/v2",
            "https://accounts.salla.s\u0430/oauth2/token",
            "https://api\u3002salla\u3002dev/",                    # ideographic full stop
            # user-info
            "https://user:pw@api.salla.dev/", "https://user@accounts.salla.sa/",
            "https://api.salla.dev@evil.com/", "https://:@api.salla.dev/",
            # empty / garbage
            "//api.salla.dev/admin", "api.salla.dev/admin/v2", "https://", "https://[::1]/",
            "", "   ", None, 42, b"https://api.salla.dev", ["https://api.salla.dev"],
            "not a url at all"):
        assert not mapping.is_allowed_salla_url(bad), bad


# --- retry_delay(status, headers, attempt, now=None, cap=30) -----------------

def test_retry_delay_retry_after_numeric():
    assert mapping.retry_delay(429, {"Retry-After": "7"}, 0, now=NOW_TS) == 7.0
    assert mapping.retry_delay(429, {"retry-after": "7"}, 0, now=NOW_TS) == 7.0   # case
    assert mapping.retry_delay(429, {"Retry-After": "0"}, 0, now=NOW_TS) == 0.0


def test_retry_delay_cap():
    assert mapping.retry_delay(429, {"Retry-After": "3600"}, 0, now=NOW_TS) == 30.0
    assert mapping.retry_delay(429, {"Retry-After": "9" * 30}, 0, now=NOW_TS) == 30.0
    assert mapping.retry_delay(500, {}, 10, now=NOW_TS) == 30.0
    assert mapping.retry_delay(429, {"X-RateLimit-Reset": str(NOW_TS + 600)}, 0, now=NOW_TS) == 30.0


def test_retry_delay_http_date():
    hdr = {"Retry-After": "Thu, 15 Oct 2026 12:00:10 GMT"}
    assert mapping.retry_delay(429, hdr, 0, now=NOW_TS) == 10.0
    past = {"Retry-After": "Thu, 15 Oct 2026 11:00:00 GMT"}
    assert mapping.retry_delay(429, past, 0, now=NOW_TS) == 0.0          # never negative
    far = {"Retry-After": "Fri, 16 Oct 2026 12:00:00 GMT"}
    assert mapping.retry_delay(429, far, 0, now=NOW_TS) == 30.0


def test_retry_delay_rate_limit_reset():
    # epoch timestamp
    assert mapping.retry_delay(429, {"X-RateLimit-Reset": str(int(NOW_TS) + 12)}, 0, now=NOW_TS) == 12.0
    # bare seconds count
    assert mapping.retry_delay(429, {"X-RateLimit-Reset": "5"}, 0, now=NOW_TS) == 5.0
    # reset already passed
    assert mapping.retry_delay(429, {"X-RateLimit-Reset": str(int(NOW_TS) - 50)}, 0, now=NOW_TS) == 0.0
    # Retry-After takes precedence
    both = {"Retry-After": "3", "X-RateLimit-Reset": str(int(NOW_TS) + 20)}
    assert mapping.retry_delay(429, both, 0, now=NOW_TS) == 3.0


def test_retry_delay_missing_header():
    assert mapping.retry_delay(429, {}, 0, now=NOW_TS) == 1.0          # 2**0
    assert mapping.retry_delay(429, None, 2, now=NOW_TS) == 4.0
    assert mapping.retry_delay(None, None, 1, now=NOW_TS) == 2.0       # network error
    assert mapping.retry_delay(503, {}, 3, now=NOW_TS) == 8.0


def test_retry_delay_not_retryable():
    for status in (200, 400, 401, 403, 404, 422):
        assert mapping.retry_delay(status, {"Retry-After": "5"}, 0, now=NOW_TS) is None, status


def test_retry_delay_garbage_header():
    """Whatever the header says, the result is a finite float in [0, cap]: it goes
    straight into time.sleep(), which raises on NaN and blocks on huge values."""
    for value in ("soon", "-5", "1.5", "", "  ", "²", "nan", "inf", "Thu, 99 Foo"):
        for name in ("Retry-After", "X-RateLimit-Reset"):
            try:
                d = mapping.retry_delay(429, {name: value}, 1, now=NOW_TS)
            except Exception as exc:  # noqa: BLE001
                raise AssertionError("retry_delay(429, {%r: %r}) raised %s: %s"
                                     % (name, value, type(exc).__name__, exc))
            assert isinstance(d, float) and math.isfinite(d) and 0.0 <= d <= 30.0, \
                (name, value, d)


# --- next_page(pagination) ---------------------------------------------------

def _walk(pages):
    """Simulate the caller's loop against a fake server; returns pages fetched.
    Hard cap so a broken next_page fails the test instead of hanging it."""
    fetched, page = [], 1
    while page is not None:
        assert len(fetched) < 50, "pagination did not terminate"
        fetched.append(page)
        page = mapping.next_page(pages(page))
    return fetched


def test_next_page_basic():
    assert mapping.next_page({"currentPage": 1, "totalPages": 3}) == 2
    assert mapping.next_page({"current_page": "2", "total_pages": "3"}) == 3
    assert mapping.next_page({"currentPage": 3, "totalPages": 3}) is None     # last page
    assert mapping.next_page({"currentPage": 1, "totalPages": 1}) is None     # single page
    assert mapping.next_page({"currentPage": 5, "totalPages": 3}) is None     # past the end
    assert mapping.next_page({"currentPage": 1, "totalPages": 0}) is None     # empty result


def test_next_page_walk_terminates():
    assert _walk(lambda p: {"currentPage": p, "totalPages": 4}) == [1, 2, 3, 4]
    assert _walk(lambda p: {"currentPage": p, "totalPages": 1}) == [1]
    # links.next only, server stops sending it on page 3
    links = lambda p: {"currentPage": p, "links": {"next": "u"} if p < 3 else {}}  # noqa: E731
    assert _walk(links) == [1, 2, 3]
    # server keeps echoing page 1 with totalPages 1 -> stop, do not refetch forever
    assert _walk(lambda p: {"currentPage": 1, "totalPages": 1}) == [1]


def test_current_page():
    assert mapping.current_page({"currentPage": 3, "totalPages": 5}) == 3
    assert mapping.current_page({"current_page": "2"}) == 2
    assert mapping.current_page({"currentPage": "x", "current_page": 4}) == 4   # fallback key
    for junk in (None, {}, "junk", [], 3, {"totalPages": 5}, {"currentPage": None},
                 {"currentPage": "abc"}, {"currentPage": [1]}, {"currentPage": {}}):
        assert mapping.current_page(junk) is None, junk


def test_next_page_missing_or_garbage():
    for junk in (None, {}, "junk", [], 3, {"totalPages": 5}, {"currentPage": "x", "totalPages": 5},
                 {"currentPage": None, "links": {"next": "u"}},
                 {"currentPage": 1}, {"currentPage": 1, "links": {"next": ""}},
                 {"currentPage": 1, "links": "u"}):
        assert mapping.next_page(junk) is None, junk


# --- product_skus(product) ---------------------------------------------------

def test_product_skus_with_variants():
    prod = {"sku": "FP-TEST-00001",
            "skus": [{"sku": "FP-TEST-00002"}, {"sku": "FP-TEST-00003"}]}
    assert mapping.product_skus(prod) == ["FP-TEST-00001", "FP-TEST-00002", "FP-TEST-00003"]
    alt = {"sku": "", "variants": [{"sku": "FP-TEST-00004"}]}
    assert mapping.product_skus(alt) == ["FP-TEST-00004"]
    both = {"skus": [{"sku": "A"}], "variants": [{"sku": "B"}]}
    assert mapping.product_skus(both) == ["A", "B"]


def test_product_skus_dedup_and_whitespace():
    prod = {"sku": " FP-TEST-00001 ",
            "skus": [{"sku": "FP-TEST-00001"}, {"sku": "\tFP-TEST-00002\n"},
                     {"sku": "FP-TEST-00002"}]}
    assert mapping.product_skus(prod) == ["FP-TEST-00001", "FP-TEST-00002"]


def test_product_skus_empty_and_none():
    assert mapping.product_skus({"sku": None, "skus": [{"sku": ""}, {"sku": "  "}, {}]}) == []
    assert mapping.product_skus({"sku": "A", "skus": None}) == ["A"]
    assert mapping.product_skus({"sku": "A", "skus": ["junk", 3, None]}) == ["A"]
    assert mapping.product_skus({"sku": 12345}) == ["12345"]          # numeric SKU
    assert mapping.product_skus({"sku": True}) == []                  # never "True"
    for junk in (None, "junk", [], 3):
        assert mapping.product_skus(junk) == [], junk


def test_needs_variant_fetch():
    assert mapping.needs_variant_fetch({"options": [{"name": "size"}]})
    assert not mapping.needs_variant_fetch({"options": [{"name": "size"}], "skus": []})
    assert not mapping.needs_variant_fetch({"options": []})
    assert not mapping.needs_variant_fetch({"sku": "A"})
    assert not mapping.needs_variant_fetch(None)


# --- safe logging -------------------------------------------------------------

def test_redact_hides_tokens():
    out = mapping.redact(AUTHORIZE)
    assert out["data"]["access_token"] == "***"
    assert out["data"]["refresh_token"] == "***"
    assert out["data"]["scope"] == "settings.read offline_access"
    assert out["merchant"] == 1000000001
    assert "FAKE_ACCESS_TOKEN" not in json.dumps(out)
    assert "FAKE_REFRESH_TOKEN" not in json.dumps(out)
    nested = {"list": [{"Client_Secret": "FAKE_SECRET", "Authorization": "Bearer FAKE"}]}
    assert "FAKE" not in json.dumps(mapping.redact(nested))
    assert AUTHORIZE["data"]["access_token"] == "FAKE_ACCESS_TOKEN"   # input untouched


def test_redact_masks_pii():
    order = {
        "id": 900001, "status": "under_review", "total": {"amount": 230, "currency": "SAR"},
        "reference_id": 12345, "sku": "FP-TEST-00001", "quantity": 4,
        "customer": {"id": 77, "first_name": "Fake", "last_name": "Buyer",
                     "full_name": "Fake Buyer", "mobile": "500000000", "mobile_code": "+966",
                     "email": "fake.buyer@example.invalid", "phone": "0110000000",
                     "city": "FakeCity", "name": "Fake Buyer"},
        "shipping": {"address": {"street_number": "1", "street": "Fake St",
                                 "national_address": "ABCD1234", "postal_code": "00000",
                                 "location": "24.0,46.0", "latitude": 24.0, "longitude": 46.0,
                                 "district": "Fake District", "building_number": "9"},
                     "shipping_address": "Fake address line"},
        "items": [{"id": 1, "name": "Fake Product", "sku": "FP-TEST-00002", "quantity": 2,
                   "receiver": {"customer_email": "x@example.invalid", "zip": "00000"}}],
    }
    out = mapping.redact(order)
    dump = json.dumps(out, ensure_ascii=False)
    for leaked in ("Fake", "fake.buyer", "500000000", "0110000000", "ABCD1234",
                   "x@example.invalid", "24.0,46.0"):
        assert leaked not in dump, leaked
    c = out["customer"]
    for key in ("first_name", "last_name", "full_name", "mobile", "mobile_code", "email",
                "phone", "city", "name"):
        assert c[key] == "***", key
    assert out["shipping"]["address"] == "***"              # whole sub-dict masked by key
    assert out["shipping"]["shipping_address"] == "***"
    assert out["items"][0]["name"] == "***"                 # exact "name", inside a list
    assert out["items"][0]["receiver"] == {"customer_email": "***", "zip": "***"}
    # unrelated keys untouched, including inside nested dicts and lists
    assert out["id"] == 900001 and out["status"] == "under_review"
    assert out["total"] == {"amount": 230, "currency": "SAR"}
    assert out["reference_id"] == 12345 and out["sku"] == "FP-TEST-00001" and out["quantity"] == 4
    assert c["id"] == 77
    assert out["items"][0]["id"] == 1 and out["items"][0]["sku"] == "FP-TEST-00002"
    assert out["items"][0]["quantity"] == 2
    # "name"/"city"/"location" are EXACT matches only: these keys stay readable
    nested = mapping.redact({"store_name": "S", "city_id": 5, "location_id": 6, "status_name": "ok"})
    assert nested == {"store_name": "S", "city_id": 5, "location_id": 6, "status_name": "ok"}
    # list of dicts at the top level, scalars pass through
    assert mapping.redact([{"email": "e"}, {"id": 2}, 3, None]) == [{"email": "***"}, {"id": 2}, 3, None]
    assert mapping.redact("plain") == "plain"
    assert order["customer"]["email"] == "fake.buyer@example.invalid"   # input untouched


def test_payload_keys_has_no_values():
    keys = mapping.payload_keys(AUTHORIZE)
    assert "data.access_token" in keys and "merchant" in keys
    assert not any("FAKE" in k for k in keys)
    assert mapping.payload_keys({"items": [{"sku": "A"}]}) == ["items", "items[].sku"]
    assert mapping.payload_keys("junk") == []


# ================================================================== F2a: orders
# Fake orders only: ids 9000001xx, SKUs FP-TEST-*, customer "Fake Buyer". Shapes follow
# docs.salla.dev and are unverified, exactly like mapping.py. Never a quantity of 1 or
# a zero discount where the rule under test is per-unit or a discount.

def _amt(value):
    return {"amount": value, "currency": "SAR"}


def _item(item_id, sku, qty, unit_net, discount=0.0, **extra):
    item = {"id": item_id, "name": "Fake Product %s" % item_id, "sku": sku,
            "quantity": qty,
            "amounts": {"price_without_tax": _amt(unit_net),
                        # traps: line total and VAT-inclusive unit price, never to be used
                        "total": _amt(round(unit_net * qty * 1.15, 2)),
                        "total_discount": _amt(discount)},
            "price": round(unit_net * 1.15, 2)}
    item.update(extra)
    return item


def _order(total, shipping=None, cod=None, discounts=None, status="under_review", **extra):
    amounts = {"sub_total": _amt(0), "tax": {"percent": "15.00", "amount": _amt(0)},
               "total": _amt(total)}
    if shipping is not None:
        amounts["shipping_cost"] = _amt(shipping)
    if cod is not None:
        amounts["cash_on_delivery"] = _amt(cod)
    if discounts is not None:
        amounts["discounts"] = discounts
    order = {"id": 900000101, "reference_id": 70000101,
             "status": {"id": 1, "name": "Fake status", "slug": status},
             "date": {"date": "2026-10-18 15:30:00.000000", "timezone_type": 3,
                      "timezone": "Asia/Riyadh"},
             "currency": "SAR", "amounts": amounts,
             "customer": {"id": 7700001, "first_name": "Fake", "last_name": "Buyer",
                          "mobile": "500000000", "email": "fake.buyer@example.invalid"}}
    order.update(extra)
    return order


def _plan(order, items):
    lines, problems = mapping.normalize_items(items)
    assert problems == [], problems
    return mapping.build_lines(mapping.normalize_order(order), lines)


def _all_lines(plan):
    return plan["products"] + [dict(e, quantity=1) for e in plan["extras"]]


def _close(a, b):
    return abs(a - b) < 1e-9


# --- money / dates ------------------------------------------------------------

def test_money():
    assert mapping.money(12.5) == 12.5
    assert mapping.money("12.50") == 12.5
    assert mapping.money(" 1,250.00 ") == 1250.0
    assert mapping.money({"amount": "69.00", "currency": "SAR"}) == 69.0
    assert mapping.money(0) == 0.0 and mapping.money("0") == 0.0
    assert mapping.money(-22) == -22.0
    for junk in (None, True, False, "", "   ", "abc", "nan", "inf", float("nan"),
                 {}, {"amount": None}, [], [12]):
        assert mapping.money(junk) is None, junk


def test_salla_date_offsets():
    want = datetime(2026, 10, 18, 12, 30, 0)                              # naive UTC
    assert mapping.salla_date("2026-10-18 15:30:00") == want              # no zone = Riyadh
    assert mapping.salla_date("2026-10-18T15:30:00") == want
    assert mapping.salla_date("2026-10-18T15:30:00+03:00") == want
    assert mapping.salla_date("2026-10-18 15:30:00+03:00") == want
    assert mapping.salla_date("2026-10-18T12:30:00Z") == want
    assert mapping.salla_date("2026-10-18T06:30:00-06:00") == want
    assert mapping.salla_date({"date": "2026-10-18 15:30:00.000000", "timezone_type": 3,
                               "timezone": "Asia/Riyadh"}) == want
    assert mapping.salla_date({"date": "2026-10-18 12:30:00.000000", "timezone": "UTC"}) == want
    # crosses midnight: 01:00 Riyadh on the 19th is the 18th in UTC
    assert mapping.salla_date("2026-10-19 01:00:00") == datetime(2026, 10, 18, 22, 0, 0)
    assert mapping.salla_date("2026-10-18 15:30:00").tzinfo is None
    for junk in (None, "", "  ", "yesterday", "18/10/2026", "2026-13-01 00:00:00",
                 {"date": None}, {}, 42, True, []):
        assert mapping.salla_date(junk) is None, junk


def test_to_salla_date_window_overlap():
    """The list filter is a Riyadh CALENDAR date. Whatever the cursor (UTC), the day sent
    as from_date must never start after an order created at or after the cursor, or that
    order falls out of the window for good."""
    assert mapping.to_salla_date(datetime(2026, 10, 18, 20, 59, 59)) == "2026-10-18"
    assert mapping.to_salla_date(datetime(2026, 10, 18, 21, 0, 0)) == "2026-10-19"
    start = datetime(2026, 10, 18, 0, 0, 0)
    for minutes in range(0, 48 * 60, 7):
        cursor = start + timedelta(minutes=minutes)
        for lag in (0, 1, 59, 180, 600):
            created_utc = cursor + timedelta(minutes=lag)
            riyadh_day = (created_utc + timedelta(hours=3)).strftime("%Y-%m-%d")
            assert mapping.to_salla_date(cursor) <= riyadh_day, (cursor, created_utc)
    # round trip: a Riyadh string read by salla_date lands back on the same Riyadh day
    for raw in ("2026-10-18 00:00:01", "2026-10-18 02:59:59", "2026-10-18 23:59:59"):
        assert mapping.to_salla_date(mapping.salla_date(raw)) == raw[:10], raw


# --- status ---------------------------------------------------------------------

def test_order_status_shapes():
    assert mapping.order_status({"status": {"slug": "Under_Review"}}) == "under_review"
    assert mapping.order_status({"status": {"slug": "payment-pending"}}) == "payment_pending"
    assert mapping.order_status({"status": {"customized": {"slug": "in_progress"}}}) == "in_progress"
    assert mapping.order_status({"status": "Completed"}) == "completed"
    assert mapping.order_status({"order": {"status": {"slug": "shipped"}}}) == "shipped"
    for junk in (None, "junk", [], {}, {"status": None}, {"status": {}}):
        assert mapping.order_status(junk) == "", junk


def test_status_filter():
    for st in ("payment_pending", "Payment-Pending", "request_quote", "canceled",
               "cancelled", "refunded", "restored", "restoring", "deleted"):
        assert not mapping.should_import(st), st
        assert not mapping.should_confirm(st), st
    for st in ("in_progress", "processing", "completed", "closed", "shipped",
               "delivering", "delivered", "ready_for_pickup", "In Progress"):
        assert mapping.should_import(st), st
        assert mapping.should_confirm(st), st
        assert mapping.should_confirm(st, "bank"), st          # payment method irrelevant
    # under_review: imported, confirmed (only electronic, fully paid methods since
    # 2026-10-18) unless the payment method is a bank transfer
    for st in ("under_review", "Under Review", "under-review"):
        assert mapping.should_import(st), st
        assert mapping.should_confirm(st), st
        assert mapping.should_confirm(st, "credit_card"), st
        assert not mapping.should_confirm(st, "bank"), st
        assert not mapping.should_confirm(st, "bank_transfer"), st
    # unknown / unreadable: imported (never lose a paid order), never auto-confirmed
    for st in ("", None, "fake_custom_status"):
        assert mapping.should_import(st), st
        assert not mapping.should_confirm(st), st
    for st in ("canceled", "cancelled", "refunded", "restored", "restoring"):
        assert mapping.is_cancel_status(st), st
    for st in ("payment_pending", "under_review", "completed", ""):
        assert not mapping.is_cancel_status(st), st


# --- webhook order id: dedup key for repeated / out-of-order deliveries ----------------

def test_webhook_order_id():
    created = {"event": "order.created", "merchant": 1000000001, "data": {"id": 900000101}}
    updated = {"event": "order.updated", "merchant": 1000000001,
               "data": {"id": "900000101", "status": {"slug": "completed"}}}
    status_nested = {"event": "order.status.updated", "merchant": 1000000001,
                     "data": {"id": 555, "order": {"id": 900000101}, "status": "completed"}}
    status_flat = {"event": "Order.Status.Updated", "data": {"id": 555, "order_id": 900000101}}
    status_bare = {"event": "order.status.updated", "data": {"id": 900000101}}
    keys = {mapping.webhook_order_id(p)
            for p in (created, updated, status_nested, status_flat, status_bare)}
    # every delivery about the same order, in any order and any number of times, maps to
    # ONE key: the one the UNIQUE salla_order_id is checked against
    assert keys == {"900000101"}, keys
    assert mapping.ORDER_EVENTS == {"order.created", "order.updated", "order.status.updated",
                                    "order.cancelled", "order.refunded"}


def test_webhook_order_id_empty_rejected():
    for junk in (None, "junk", [], {}, {"event": "order.created"},
                 {"event": "order.created", "data": None},
                 {"event": "order.created", "data": []},
                 {"event": "order.created", "data": {}},
                 {"event": "order.created", "data": {"id": None}},
                 {"event": "order.created", "data": {"id": ""}},
                 {"event": "order.created", "data": {"id": "   "}},
                 {"event": "order.created", "data": {"id": True}},
                 {"event": "order.created", "data": {"id": {}}},
                 {"event": "order.status.updated", "data": {"order": {}}}):
        assert mapping.webhook_order_id(junk) == "", junk


def test_order_id_literal_none_rejected():
    """Trendyol's "None" package id collapsed every order onto one dedup key. A literal
    "None"/"null" coming back as an id must be rejected like a missing one."""
    for bad in ("None", "null", "none", "NULL"):
        payload = {"event": "order.created", "data": {"id": bad}}
        assert mapping.webhook_order_id(payload) == "", bad
        assert mapping.normalize_order({"id": bad}) is None, bad


def test_clean_id():
    assert mapping.clean_id(900000101) == "900000101"
    assert mapping.clean_id(" 900000101 ") == "900000101"
    assert mapping.clean_id(0) == "0"                       # a real (if odd) id, not empty
    for junk in (None, "", "  ", True, False, {}, [], "None", "none", "NULL", "null",
                 "undefined", "NaN"):
        assert mapping.clean_id(junk) == "", junk


def test_literal_none_ids_fall_through():
    """A "None" id is treated as absent everywhere, so the next candidate path is used
    instead of collapsing every order onto the key "None"."""
    created = {"event": "order.created", "data": {"id": "None", "order": {"id": 900000101}}}
    assert mapping.webhook_order_id(created) == "900000101"
    status = {"event": "order.status.updated", "data": {"order": {"id": "null"}, "id": 900000101}}
    assert mapping.webhook_order_id(status) == "900000101"
    assert mapping.order_customer({"customer": {"id": "None", "first_name": "Fake"}}) == \
        {"ref": "", "name": "Fake"}                       # -> shared guest contact, not "None"
    lines, problems = mapping.normalize_items([
        {"id": "None", "sku": "FP-TEST-00001", "quantity": 3,
         "amounts": {"price_without_tax": 20}},
        {"id": "null", "sku": "FP-TEST-00001", "quantity": 0}])
    assert lines[0]["line_id"] == ""
    assert problems and "#2" in problems[0], problems


# --- normalize_order -------------------------------------------------------------

def test_normalize_order_full():
    o = mapping.normalize_order(_order(
        101.2, shipping=25.0, cod=10.0,
        discounts=[{"title": "TEST20", "code": "TEST20", "discount": "25.30"}]))
    assert o["id"] == "900000101" and o["reference"] == "70000101"
    assert o["status"] == "under_review"
    assert o["created_at"] == datetime(2026, 10, 18, 12, 30, 0)
    assert o["currency"] == "SAR"
    assert o["total"] == 101.2 and o["vat_rate"] == 15.0
    assert o["shipping"] == 25.0 and o["cod"] == 10.0
    assert o["discounts"] == [{"title": "TEST20", "amount": 25.3}]
    assert o["customer"] == {"ref": "7700001", "name": "Fake Buyer"}
    # PII beyond the name never leaves normalize_order in F2a
    assert "500000000" not in json.dumps(o, default=str)
    assert "fake.buyer" not in json.dumps(o, default=str)


def test_normalize_order_missing_none_and_numeric_strings():
    o = mapping.normalize_order({"id": "900000102"})
    assert o["id"] == "900000102" and o["reference"] == "900000102"     # falls back to id
    assert o["status"] == "" and o["created_at"] is None and o["currency"] == ""
    assert o["total"] is None and o["vat_rate"] is None
    assert o["shipping"] == 0.0 and o["cod"] == 0.0 and o["discounts"] == []
    assert o["customer"] == {"ref": "", "name": ""}
    o = mapping.normalize_order({
        "id": 900000103, "reference_id": None, "status": None, "date": None,
        "customer": None,
        "amounts": {"total": {"amount": "1,150.00", "currency": "sar"},
                    "tax": {"percent": "15"}, "shipping_cost": None,
                    "cash_on_delivery": {"amount": None},
                    "discounts": [None, "junk", {"discount": "0"}, {"discount": "-12.5"},
                                  {"amount": "7.50"}]}})
    assert o["reference"] == "900000103"
    assert o["total"] == 1150.0 and o["currency"] == "SAR"              # from the total object
    assert o["vat_rate"] == 15.0
    assert o["shipping"] == 0.0 and o["cod"] == 0.0
    # zero discounts dropped, signs normalised, a nameless one still gets a title
    assert o["discounts"] == [{"title": "Discount", "amount": 12.5},
                              {"title": "Discount", "amount": 7.5}]
    # flat (non-"amounts") fallbacks
    o = mapping.normalize_order({"id": 900000104, "total": "69", "shipping_cost": "25",
                                 "cash_on_delivery": 10, "created_at": "2026-10-18T12:30:00Z",
                                 "tax": {"percent": 15}})
    assert (o["total"], o["shipping"], o["cod"], o["vat_rate"]) == (69.0, 25.0, 10.0, 15.0)
    assert o["created_at"] == datetime(2026, 10, 18, 12, 30, 0)
    # a negative VAT percent is junk, not a rate
    assert mapping.normalize_order({"id": 1, "amounts": {"tax": {"percent": -15}}})["vat_rate"] is None


def test_normalize_order_garbage():
    for junk in (None, "junk", 42, [], {}, {"id": None}, {"id": ""}, {"id": True},
                 {"id": {}}, {"reference_id": 70000101}):
        assert mapping.normalize_order(junk) is None, junk


def test_order_customer():
    assert mapping.order_customer({"customer": {"id": 7, "first_name": " Fake ",
                                                "last_name": " Buyer "}}) == \
        {"ref": "7", "name": "Fake Buyer"}
    assert mapping.order_customer({"customer": {"id": 7, "full_name": "Fake   Buyer"}})["name"] == "Fake Buyer"
    assert mapping.order_customer({"customer": {"id": 7, "name": "Fake"}})["name"] == "Fake"
    assert mapping.order_customer({"customer": {"first_name": None, "last_name": None}}) == \
        {"ref": "", "name": ""}
    for junk in (None, {}, {"customer": None}, {"customer": "Fake"}, []):
        assert mapping.order_customer(junk) == {"ref": "", "name": ""}, junk


# --- normalize_items ---------------------------------------------------------------

def test_items_unit_price_not_line_total():
    """Case 1 of the brief. 3 units at 20.00 net: the line must carry 20.00, never the
    line total (60 net / 69 gross) and never the VAT-inclusive unit (23.00)."""
    lines, problems = mapping.normalize_items([_item(5001, "FP-TEST-00001", 3, 20.0)])
    assert problems == []
    line = lines[0]
    assert line["quantity"] == 3 and line["unit_net"] == 20.0
    assert line["sku"] == "FP-TEST-00001" and line["line_id"] == "5001"
    assert line["discount"] == 0.0
    plan = _plan(_order(69.0), [_item(5001, "FP-TEST-00001", 3, 20.0)])
    p = plan["products"][0]
    assert p["price_unit"] == 20.0 and p["quantity"] == 3
    assert plan["extras"] == [] and plan["extras_basis"] == "none"
    assert plan["expected_total"] == 69.0 == plan["salla_total"]
    assert mapping.total_check(plan["expected_total"], plan["salla_total"], plan["tolerance"]) == ("ok", 0.0)


def test_items_numeric_strings_and_fallback_paths():
    items = [
        {"id": 5002, "sku": " FP-TEST-00002 ", "quantity": "4",
         "amounts": {"price_without_tax": {"amount": "12.50"}, "total_discount": None}},
        {"id": 5003, "product": {"sku": "FP-TEST-00003"}, "quantity": 3.0,
         "price_without_tax": "8.69"},
        {"id": 5004, "variant": {"sku": "FP-TEST-00004"}, "quantity": 5,
         "amounts": {"price_without_tax": 10, "total_discount": {"amount": "-6.00"}}},
    ]
    lines, problems = mapping.normalize_items(items)
    assert problems == []
    assert [(l["sku"], l["quantity"], l["unit_net"], l["discount"]) for l in lines] == [
        ("FP-TEST-00002", 4.0, 12.5, 0.0),
        ("FP-TEST-00003", 3.0, 8.69, 0.0),
        ("FP-TEST-00004", 5.0, 10.0, 6.0)]


def test_items_problems_never_price_zero():
    items = [
        {"id": 6001, "sku": "FP-TEST-00001", "quantity": 3},                     # no price
        {"id": 6002, "sku": "FP-TEST-00001", "quantity": 0,
         "amounts": {"price_without_tax": 20}},
        {"id": 6003, "sku": "FP-TEST-00001", "quantity": None,
         "amounts": {"price_without_tax": 20}},
        {"id": 6004, "sku": "FP-TEST-00001", "quantity": "-3",
         "amounts": {"price_without_tax": 20}},
        {"id": 6005, "sku": "FP-TEST-00001", "quantity": 3,
         "amounts": {"price_without_tax": {"amount": None}}},
        {"sku": "FP-TEST-00001", "quantity": "abc", "amounts": {"price_without_tax": 20}},
    ]
    lines, problems = mapping.normalize_items(items)
    assert lines == []
    assert len(problems) == 6, problems
    assert "6001" in problems[0] and "price" in problems[0]
    assert "#6" in problems[5]                                 # no id -> position
    # a valid price of 0.00 is a real price (free gift), not a missing one
    lines, problems = mapping.normalize_items(
        [{"id": 6006, "sku": "FP-TEST-00001", "quantity": 3, "amounts": {"price_without_tax": 0}}])
    assert problems == [] and lines[0]["unit_net"] == 0.0
    for junk in (None, "junk", {}, 3):
        assert mapping.normalize_items(junk) == ([], []), junk
    assert mapping.normalize_items([None, "x", 3]) == ([], [])


def test_missing_skus_whole_order():
    lines, _ = mapping.normalize_items([
        _item(5001, "FP-TEST-00001", 3, 20.0),
        _item(5002, "FP-TEST-99999", 4, 12.5),
        _item(5003, "", 3, 5.0),
        _item(5004, "FP-TEST-99999", 2, 12.5)])
    assert mapping.missing_skus(lines, {"FP-TEST-00001"}) == ["(empty SKU)", "FP-TEST-99999"]
    # one unknown SKU among known ones is enough to hold the whole order
    assert mapping.missing_skus(lines[:2], ["FP-TEST-00001"]) == ["FP-TEST-99999"]
    assert mapping.missing_skus(lines[:1], ["FP-TEST-00001"]) == []
    assert mapping.missing_skus(lines[:1], None) == ["FP-TEST-00001"]
    assert mapping.missing_skus([], set()) == []
    # match is exact on the stripped code (Odoo default_code), not case-folded
    assert mapping.missing_skus(lines[:1], {"fp-test-00001"}) == ["FP-TEST-00001"]


# --- build_lines: discount / shipping / COD, total == Salla ---------------------------

def test_coupon_20_percent_gross_discount():
    """Case 2. 3 x 20.00 + 4 x 12.50 = 110.00 net, 126.50 with VAT. Coupon 20 % sent as a
    VAT-inclusive 25.30 -> Salla total 101.20."""
    items = [_item(5001, "FP-TEST-00001", 3, 20.0), _item(5002, "FP-TEST-00002", 4, 12.5)]
    plan = _plan(_order(101.2, discounts=[{"title": "TEST20", "discount": "25.30"}]), items)
    assert [(p["price_unit"], p["quantity"]) for p in plan["products"]] == [(20.0, 3), (12.5, 4)]
    assert plan["extras"] == [{"kind": "discount", "name": "TEST20", "price_unit": -22.0}]
    assert plan["extras_basis"] == "gross"
    assert plan["expected_total"] == 101.2
    assert mapping.total_check(plan["expected_total"], 101.2, plan["tolerance"]) == ("ok", 0.0)


def test_coupon_20_percent_net_discount():
    """Same coupon if Salla sends the discount net of VAT (22.00): the net reading must win
    and the total must still be 101.20."""
    items = [_item(5001, "FP-TEST-00001", 3, 20.0), _item(5002, "FP-TEST-00002", 4, 12.5)]
    plan = _plan(_order(101.2, discounts=[{"title": "TEST20", "discount": 22}]), items)
    assert plan["extras"] == [{"kind": "discount", "name": "TEST20", "price_unit": -22.0}]
    assert plan["extras_basis"] == "net"
    assert plan["expected_total"] == 101.2


def test_item_discount_becomes_discount_line_not_both():
    items = [_item(5001, "FP-TEST-00001", 3, 20.0, discount=6.9),     # 10 % offer, gross
             _item(5002, "FP-TEST-00002", 4, 12.5, discount=5.75)]
    # no order-level discounts -> the items' total_discount becomes ONE offer line
    plan = _plan(_order(126.5 - 12.65), items)
    assert len(plan["extras"]) == 1
    e = plan["extras"][0]
    assert e["kind"] == "discount" and e["name"] == "Salla offer" and e["price_unit"] == -11.0
    assert plan["products"][0]["price_unit"] == 20.0                   # not pre-discounted
    assert plan["expected_total"] == 113.85
    # order-level discounts present -> item discounts are the same money, not added twice
    plan = _plan(_order(113.85, discounts=[{"title": "OFFER10", "discount": 12.65}]), items)
    assert [e["name"] for e in plan["extras"]] == ["OFFER10"]
    assert plan["expected_total"] == 113.85


def test_paid_shipping():
    """Case 3a. 3 x 20.00 net + shipping 25.00 VAT-inclusive -> 94.00. The net shipping
    price keeps Odoo's 7 "Product Price" digits (21.7391304), not 2."""
    items = [_item(5001, "FP-TEST-00001", 3, 20.0)]
    plan = _plan(_order(94.0, shipping=25.0), items)
    assert plan["extras"] == [{"kind": "shipping", "name": "Shipping", "price_unit": 21.7391304}]
    assert plan["extras_basis"] == "gross"
    assert plan["expected_total"] == 94.0
    # the same fee sent net of VAT -> 69 + 28.75 = 97.75, net reading
    plan = _plan(_order(97.75, shipping=25.0), items)
    assert plan["extras"][0]["price_unit"] == 25.0 and plan["extras_basis"] == "net"
    assert plan["expected_total"] == 97.75


def test_free_shipping():
    """Case 3b. Free shipping: no shipping charge out of nowhere, total unchanged."""
    items = [_item(5001, "FP-TEST-00001", 3, 20.0)]
    for shipping in (0, "0.00", None, {"amount": 0}):
        order = _order(69.0)
        order["amounts"]["shipping_cost"] = shipping
        plan = _plan(order, items)
        assert not [e for e in plan["extras"] if e["kind"] == "shipping"], shipping
        assert plan["expected_total"] == 69.0, shipping
    # free shipping together with a coupon: only the discount line
    plan = _plan(_order(55.2, shipping=0, discounts=[{"title": "TEST20", "discount": 13.8}]), items)
    assert [e["kind"] for e in plan["extras"]] == ["discount"]
    assert plan["expected_total"] == 55.2


def test_cod_line_and_rounding_adjust():
    """Case 4. 4 x 12.50 net + shipping 25.00 + COD 10.00 (both VAT-inclusive) = 92.50.
    Even at 7 digits the COD fee (8.6956522) gives subtotal 8.70 and, with the tax
    rounded per line on that subtotal, VAT 1.31: the COD line totals 10.01, so Odoo
    lands on 92.51 and a -0.01 rounding line brings it back to 92.50."""
    items = [_item(5002, "FP-TEST-00002", 4, 12.5)]
    plan = _plan(_order(92.5, shipping=25.0, cod=10.0), items)
    kinds = {e["kind"]: e["price_unit"] for e in plan["extras"]}
    assert kinds == {"shipping": 21.7391304, "cod": 8.6956522}
    assert [e["name"] for e in plan["extras"] if e["kind"] == "cod"] == ["Cash on Delivery fee"]
    assert plan["extras_basis"] == "gross"
    assert plan["expected_total"] == 92.51
    status, diff = mapping.total_check(plan["expected_total"], 92.5, plan["tolerance"])
    assert (status, diff) == ("adjust", -0.01)
    adj = mapping.adjustment_nets(diff, plan["vat_rate"])
    assert adj == [-0.01]
    fixed = _all_lines(plan) + [{"price_unit": a, "quantity": 1} for a in adj]
    assert mapping.expected_total(fixed, 15.0) == 92.5


def test_cod_with_coupon_all_extras():
    """Coupon + shipping + COD on a multi-unit order, all three extras at once."""
    items = [_item(5001, "FP-TEST-00001", 3, 20.0), _item(5002, "FP-TEST-00002", 4, 12.5)]
    # goods 126.50 gross - coupon 25.30 + shipping 23.00 + COD 11.50 = 135.70
    plan = _plan(_order(135.7, shipping=23.0, cod=11.5,
                        discounts=[{"title": "TEST20", "discount": 25.3}]), items)
    kinds = sorted((e["kind"], e["price_unit"]) for e in plan["extras"])
    assert kinds == [("cod", 10.0), ("discount", -22.0), ("shipping", 20.0)]
    assert plan["expected_total"] == 135.7
    assert mapping.total_check(plan["expected_total"], 135.7, plan["tolerance"]) == ("ok", 0.0)


def test_vat_applied_on_extra_lines():
    """Discount, shipping and COD lines carry the 15 % too: their VAT-inclusive totals
    must add up to what Salla charged for them."""
    assert mapping.expected_total([{"price_unit": -22.0, "quantity": 1}], 15.0) == -25.3
    assert mapping.expected_total([{"price_unit": 20.0, "quantity": 1}], 15.0) == 23.0
    assert mapping.expected_total([{"price_unit": 10.0, "quantity": 1}], 15.0) == 11.5
    assert mapping.expected_total([{"price_unit": 20.0, "quantity": 3}], 0.0) == 60.0
    # tax rounded PER LINE (Odoo round_per_line): 3 lines of 0.03 net -> 0 VAT each
    three = [{"price_unit": 0.03, "quantity": 1}] * 3
    assert mapping.expected_total(three, 15.0) == 0.09


def test_rounding_3_x_9_99_incl_vat():
    """Salla hands price_without_tax ALREADY rounded to 2 decimals (8.69 for a 9.99 shelf
    price incl. VAT). Odoo: 3 x 8.69 = 26.07 + 3.91 VAT = 29.98; Salla charged 29.97.
    One halala, rounding only: adjust. (With the exact 8.6869565 there is nothing to
    adjust, see test_price_digits_7_rounding.)"""
    items = [_item(5005, "FP-TEST-00005", 3, 8.69)]
    plan = _plan(_order(29.97), items)
    assert plan["products"][0]["price_unit"] == 8.69
    assert plan["expected_total"] == 29.98
    status, diff = mapping.total_check(plan["expected_total"], 29.97, plan["tolerance"])
    assert (status, diff) == ("adjust", -0.01)
    adj = mapping.adjustment_nets(diff, 15.0)
    assert adj == [-0.01]
    assert mapping.expected_total(plan["products"] + [{"price_unit": a, "quantity": 1}
                                                      for a in adj], 15.0) == 29.97
    # 12 units: Odoo 104.28 + 15.64 = 119.92 vs Salla 119.88 -> still rounding, adjust
    plan = _plan(_order(119.88), [_item(5005, "FP-TEST-00005", 12, 8.69)])
    status, diff = mapping.total_check(plan["expected_total"], 119.88, plan["tolerance"])
    assert status == "adjust" and _close(diff, 119.88 - plan["expected_total"]), (status, diff)
    adj = mapping.adjustment_nets(diff, 15.0)
    assert adj, diff
    assert mapping.expected_total(plan["products"] + [{"price_unit": a, "quantity": 1}
                                                      for a in adj], 15.0) == 119.88


def test_adjustment_nets_reach_diff():
    """Every diff inside the 1 SAR tolerance must be reachable, with one line or two:
    0.04 at 15 % has no single 2-decimal net price (0.03 -> 0.03, 0.04 -> 0.05)."""
    for cents in range(-100, 101):
        diff = cents / 100.0
        nets = mapping.adjustment_nets(diff, 15.0)
        if not cents:
            assert nets == [], diff
            continue
        assert 1 <= len(nets) <= 2, (diff, nets)
        got = mapping.expected_total([{"price_unit": n, "quantity": 1} for n in nets], 15.0)
        assert got == diff, (diff, nets, got)
        # every net is a real 2-decimal price, never 0
        assert all(n and _close(round(n, 2), n) for n in nets), (diff, nets)
    assert len(mapping.adjustment_nets(0.04, 15.0)) == 2
    assert mapping.adjustment_nets(0, 15.0) == []
    assert mapping.adjustment_nets(None, 15.0) == []
    assert mapping.adjustment_nets(0.001, 15.0) == []                 # rounds to 0


def test_total_check_mismatch_and_unknown():
    items = [_item(5001, "FP-TEST-00001", 3, 20.0)]
    # Salla says 80, Odoo would say 69: a real error, not rounding
    plan = _plan(_order(80.0), items)
    assert mapping.total_check(plan["expected_total"], 80.0, plan["tolerance"]) == ("mismatch", 11.0)
    assert mapping.total_check(69.0, 68.0, 0.5) == ("mismatch", -1.0)
    assert mapping.total_check(69.0, 69.0, 0.04) == ("ok", 0.0)
    assert mapping.total_check(69.0, 69.04, 0.04) == ("adjust", 0.04)
    assert mapping.total_check(69.0, 69.05, 0.04) == ("mismatch", 0.05)
    assert mapping.total_check(69.0, None, 0.04) == ("unknown", None)
    assert mapping.total_check(None, 69.0, 0.04) == ("unknown", None)


def test_total_tolerance_bounds():
    # 0.006 per unit + 0.01 per line, rounded to 2, capped at 0.50 SAR
    assert mapping.total_tolerance([{"quantity": 3}]) == 0.03       # 0.018 + 0.01
    assert mapping.total_tolerance([{"quantity": 1}]) == 0.02
    assert mapping.total_tolerance([{"quantity": 3}, {"quantity": 4}]) == 0.06   # 0.042 + 0.02
    assert mapping.total_tolerance([{"quantity": 500}]) == 0.5      # capped
    assert mapping.total_tolerance([{"quantity": -3}]) == 0.03      # abs(), refunds
    assert mapping.total_tolerance([]) == 0.0


def test_unreconciled_extras_flagged():
    """Shipping that fits neither reading of Salla's total: gross assumed, flagged, and
    the total check says mismatch so the caller warns instead of guessing."""
    items = [_item(5001, "FP-TEST-00001", 3, 20.0)]
    plan = _plan(_order(120.0, shipping=25.0), items)
    assert plan["extras_basis"] == "unreconciled"
    assert plan["extras"][0]["price_unit"] == 21.7391304             # gross assumed
    assert mapping.total_check(plan["expected_total"], 120.0, plan["tolerance"])[0] == "mismatch"
    # no Salla total at all
    order = _order(0, shipping=25.0)
    del order["amounts"]["total"]
    plan = _plan(order, items)
    assert plan["salla_total"] is None and plan["extras_basis"] == "unreconciled"
    assert mapping.total_check(plan["expected_total"], None, plan["tolerance"]) == ("unknown", None)


def test_build_lines_default_vat_when_missing():
    items = [_item(5001, "FP-TEST-00001", 3, 20.0)]
    order = _order(94.0, shipping=25.0)
    del order["amounts"]["tax"]
    plan = _plan(order, items)
    assert plan["vat_rate"] == 15.0 and plan["expected_total"] == 94.0


def test_should_confirm_under_review_vs_bank_transfer():
    """Since the 2026-10-18 meeting Salla takes only Apple Pay, Tabby, Tamara and cards:
    an under_review order is already paid and is confirmed. A stray bank-transfer order
    (receipt not checked yet) stays a quotation."""
    paid = ("card", "credit_card", "Credit Card", "debit_card", "apple_pay", "Apple-Pay",
            "tabby", "TABBY", "tamara", "mada", "cod", "", None, "fake_unknown_method")
    for method in paid:
        assert mapping.should_confirm("under_review", method), method
        assert mapping.should_confirm("Under Review", method), method
    for method in ("bank", "BANK", "bank_transfer", "Bank Transfer", "BANK-TRANSFER",
                   " bank_transfer "):
        assert not mapping.should_confirm("under_review", method), method
        assert not mapping.should_confirm("under-review", method), method
    for method in ("bank", "Bank Transfer", "bank-transfer", "banktransfer", "wire",
                   "Wire", "wire_transfer", "Wire Transfer", "transfer", "\u0628\u0646\u0643",
                   "\u062a\u062d\u0648\u064a\u0644 \u0628\u0646\u0643\u064a",
                   "\u062d\u0648\u0627\u0644\u0629 \u0628\u0646\u0643\u064a\u0629"):
        assert mapping.is_bank_transfer(method), method
        assert not mapping.should_confirm("under_review", method), method
    for method in ("credit_card", "apple_pay", "tabby", "tamara", "mada", "cod", "", None,
                   "bankx", "bank_card", "banking"):
        assert not mapping.is_bank_transfer(method), method
    # COD is no longer offered but stays recognisable
    for method in ("cod", "COD", "cash_on_delivery", "Cash-On-Delivery", "cash on delivery"):
        assert mapping.is_cod(method), method
    for method in ("bank", "credit_card", "", None, "codx", "c.o.d"):
        assert not mapping.is_cod(method), method
    # a bank transfer the merchant approved (in_progress / completed) is confirmed
    for st in ("in_progress", "processing", "completed", "shipped"):
        assert mapping.should_confirm(st, "bank"), st
        assert mapping.should_confirm(st, "bank_transfer"), st
    # no payment method makes an unpaid, dead or unknown status confirmable
    for st in ("payment_pending", "Payment-Pending", "cancelled", "canceled", "refunded",
               "restored", "deleted", "", None, "fake_custom_status"):
        for method in ("credit_card", "apple_pay", "tabby", "tamara", "cod", "bank", ""):
            assert not mapping.should_confirm(st, method), (st, method)
    for st in ("payment_pending", "cancelled", "refunded", "deleted"):
        assert not mapping.should_import(st), st
    assert mapping.should_import("under_review")


def test_order_payment_method():
    assert mapping.order_payment_method({"payment_method": "Cash-On-Delivery"}) == "cash_on_delivery"
    assert mapping.order_payment_method({"payment_method": "bank"}) == "bank"
    assert mapping.order_payment_method({"payment_method": {"slug": "cod", "name": "Fake"}}) == "cod"
    assert mapping.order_payment_method({"payment_method": {"name": "Bank Transfer"}}) == "bank_transfer"
    assert mapping.order_payment_method({"payment": {"method": "COD"}}) == "cod"
    assert mapping.order_payment_method({"payment_method": "", "payment": {"method": "cod"}}) == "cod"
    for junk in (None, "junk", [], {}, {"payment_method": None}, {"payment_method": {}},
                 {"payment": "cod"}):
        assert mapping.order_payment_method(junk) == "", junk
    # normalize_order carries it, end to end into should_confirm
    o = mapping.normalize_order(_order(69.0, payment_method="cod"))
    assert o["payment_method"] == "cod"
    assert mapping.should_confirm(o["status"], o["payment_method"])
    o = mapping.normalize_order(_order(69.0, payment_method={"slug": "tabby"}))
    assert o["status"] == "under_review" and o["payment_method"] == "tabby"
    assert mapping.should_confirm(o["status"], o["payment_method"])
    o = mapping.normalize_order(_order(69.0))                # method missing: paid
    assert mapping.should_confirm(o["status"], o["payment_method"])
    o = mapping.normalize_order(_order(69.0, payment_method="bank"))
    assert not mapping.should_confirm(o["status"], o["payment_method"])
    o = mapping.normalize_order(_order(69.0, payment_method={"name": "Bank Transfer"}))
    assert o["payment_method"] == "bank_transfer"
    assert not mapping.should_confirm(o["status"], o["payment_method"])
    assert mapping.normalize_order({"id": 1})["payment_method"] == ""


def test_order_payment_method_list_fallback():
    """payment_methods[] fallback: a bank transfer anywhere in the list wins (missing it
    would confirm an unverified order), else the first readable entry."""
    pm = mapping.order_payment_method
    assert pm({"payment_methods": ["credit_card", {"slug": "bank"}]}) == "bank"
    assert pm({"payment_methods": [{"name": "Tabby"}]}) == "tabby"
    assert pm({"payment_methods": ["apple_pay", "tamara"]}) == "apple_pay"
    assert pm({"payment_methods": [None, "", {}, 5, "Mada"]}) == "mada"
    assert pm({"payment_methods": ["credit_card"]}) == "credit_card"
    assert pm({"payment_methods": [{"slug": "tabby"}]}) == "tabby"
    for junk in ([None, 5], [None, {}, 5, ""], [], "credit_card", "bank", {"slug": "bank"},
                 None):
        assert pm({"payment_methods": junk}) == "", junk
    for junk in (5, 5.0, True, ["bank"]):          # non-str primary field is ignored
        assert pm({"payment_method": junk}) == "", junk
    # a non-empty payment_method wins over the list (even a bank transfer in the list)
    assert pm({"payment_method": "credit_card", "payment_methods": ["bank"]}) == "credit_card"
    assert pm({"payment_method": "", "payment_methods": [{"name": "Bank Transfer"}]}) \
        == "bank_transfer"
    assert pm({"payment": {"method": "tabby"}, "payment_methods": ["bank"]}) == "tabby"
    # end to end: under_review + list holding a transfer stays a quotation
    o = mapping.normalize_order(_order(69.0, payment_methods=["credit_card", "bank"]))
    assert o["status"] == "under_review" and o["payment_method"] == "bank"
    assert not mapping.should_confirm(o["status"], o["payment_method"])
    o = mapping.normalize_order(_order(69.0, payment_methods=["bank"]))
    assert o["payment_method"] == "bank"
    assert not mapping.should_confirm(o["status"], o["payment_method"])
    o = mapping.normalize_order(_order(69.0, payment_methods=[{"slug": "tamara"}]))
    assert mapping.should_confirm(o["status"], o["payment_method"])


def test_vat_rate():
    assert mapping.vat_rate(15) == (15.0, False)
    assert mapping.vat_rate("15.00") == (15.0, False)
    assert mapping.vat_rate({"amount": "15"}) == (15.0, False)
    assert mapping.vat_rate(0) == (0.0, False)                        # exempt, not junk
    assert mapping.vat_rate(30) == (30.0, False)                      # upper bound kept
    rate, suspect = mapping.vat_rate(0.15)                            # a fraction
    assert _close(rate, 15.0) and suspect
    for missing in (None, ""):
        assert mapping.vat_rate(missing) == (None, False), missing
    for junk in (45, 30.01, -1, "abc", "nan", "inf", True):
        assert mapping.vat_rate(junk) == (None, True), junk


def test_normalize_order_vat_suspect_and_default():
    o = mapping.normalize_order(_order(69.0))
    assert o["vat_rate"] == 15.0 and o["vat_suspect"] is False
    order = _order(69.0)
    order["amounts"]["tax"]["percent"] = 0.15
    o = mapping.normalize_order(order)
    assert _close(o["vat_rate"], 15.0) and o["vat_suspect"] is True
    order["amounts"]["tax"]["percent"] = 45
    o = mapping.normalize_order(order)
    assert o["vat_rate"] is None and o["vat_suspect"] is True
    # a junk rate falls back to 15 % in build_lines: the total still reconciles
    plan = mapping.build_lines(o, mapping.normalize_items([_item(5001, "FP-TEST-00001", 3, 20.0)])[0])
    assert plan["vat_rate"] == 15.0 and plan["expected_total"] == 69.0


def test_price_digits_7_rounding():
    """Odoo stores unit prices with 7 decimals (prod "Product Price" precision). A 9.99
    shelf price with VAT is 8.6869565 net: 3 x -> 26.06 + 3.91 = 29.97 exactly, no
    rounding line. With only 2 digits the same order would need one."""
    items = [_item(5005, "FP-TEST-00005", 3, 9.99 / 1.15)]
    plan = _plan(_order(29.97), items)
    assert plan["products"][0]["price_unit"] == 8.6869565
    assert plan["expected_total"] == 29.97
    assert mapping.total_check(plan["expected_total"], 29.97, plan["tolerance"]) == ("ok", 0.0)
    # the price_digits knob: 2 digits reproduces the old 29.98
    lines, _ = mapping.normalize_items(items)
    plan2 = mapping.build_lines(mapping.normalize_order(_order(29.97)), lines, price_digits=2)
    assert plan2["products"][0]["price_unit"] == 8.69 and plan2["expected_total"] == 29.98
    assert mapping.expected_total([{"price_unit": 8.6869565, "quantity": 3}], 15.0) == 29.97
    assert mapping.expected_total([{"price_unit": 8.6869565, "quantity": 3}], 15.0,
                                  price_digits=2) == 29.98


def test_extras_at_7_digits_hundred_net():
    """1 x 100.00 net (115.00) + shipping 25 incl. VAT = 140.00 exactly. Adding a COD fee
    of 10 incl. VAT: its line is 8.70 + 1.31 = 10.01 under per-line rounding of the
    rounded subtotal, so the plan says 150.01 and the total check asks for -0.01.
    (Quantity 1 is fine here: the rule under test is the extras' rounding, not a
    per-unit price; the product line is checked with qty 3 elsewhere.)"""
    items = [_item(5001, "FP-TEST-00001", 1, 100.0)]
    plan = _plan(_order(140.0, shipping=25.0), items)
    assert plan["expected_total"] == 140.0 and plan["extras_basis"] == "gross"
    assert mapping.total_check(plan["expected_total"], 140.0, plan["tolerance"]) == ("ok", 0.0)
    plan = _plan(_order(150.0, shipping=25.0, cod=10.0), items)
    assert plan["extras_basis"] == "gross"
    assert plan["expected_total"] == 150.01
    assert mapping.total_check(plan["expected_total"], 150.0, plan["tolerance"]) == ("adjust", -0.01)
    # same thing with 3 units of 100/3 net: still 140.00 with shipping
    plan = _plan(_order(140.0, shipping=25.0), [_item(5001, "FP-TEST-00001", 3, 100.0 / 3)])
    assert plan["expected_total"] == 140.0


def test_rounding_adjust_salla_net_two_decimals():
    """A rounding line is still needed when Salla hands price_without_tax already
    rounded to 2 decimals: 8.69 x 3 -> 29.98 vs 29.97 (covered in
    test_rounding_3_x_9_99_incl_vat), and x 12 -> 119.92 vs 119.88, -0.04: two lines."""
    plan = _plan(_order(119.88), [_item(5005, "FP-TEST-00005", 12, 8.69)])
    assert plan["expected_total"] == 119.92 and plan["tolerance"] == 0.08
    status, diff = mapping.total_check(plan["expected_total"], 119.88, plan["tolerance"])
    assert (status, diff) == ("adjust", -0.04)
    nets = mapping.adjustment_nets(diff, 15.0)
    assert len(nets) == 2
    assert mapping.expected_total(plan["products"] + [{"price_unit": n, "quantity": 1}
                                                      for n in nets], 15.0) == 119.88


def test_offsetting_shipping_and_coupon_tie():
    """Shipping +23 and coupon -23 cancel out: both readings give the same 69.00 total,
    so Salla's total cannot tell gross from net. Gross wins the tie (documented): the
    TOTAL is right either way, the split between the lines (20/-20 vs 23/-23 net) is
    not verifiable from the total and must be checked against the Salla VAT breakdown
    on staging."""
    items = [_item(5001, "FP-TEST-00001", 3, 20.0)]
    plan = _plan(_order(69.0, shipping=23.0,
                        discounts=[{"title": "TEST20", "discount": 23}]), items)
    assert plan["extras_basis"] == "gross"
    assert sorted((e["kind"], e["price_unit"]) for e in plan["extras"]) == \
        [("discount", -20.0), ("shipping", 20.0)]
    assert plan["expected_total"] == 69.0
    assert mapping.total_check(plan["expected_total"], 69.0, plan["tolerance"]) == ("ok", 0.0)


def test_large_order_real_fee_not_absorbed():
    """The tolerance grows with the units but is capped at 0.50 SAR. On a 100-unit order
    (tolerance 0.50) a real 0.70 fee the mapping missed is a mismatch, not rounding.
    Documented bound: a real amount <= 0.50 on an order of >= ~82 units WOULD be absorbed
    as a rounding line (the total still equals what the customer paid)."""
    items = [_item(5001, "FP-TEST-00001", 100, 20.0)]
    plan = _plan(_order(2300.70), items)
    assert plan["tolerance"] == 0.5
    assert mapping.total_check(plan["expected_total"], 2300.70, plan["tolerance"]) == ("mismatch", 0.7)
    assert mapping.total_check(plan["expected_total"], 2300.90, plan["tolerance"]) == ("mismatch", 0.9)
    assert mapping.total_check(plan["expected_total"], 2300.51, plan["tolerance"]) == ("mismatch", 0.51)
    assert mapping.total_check(plan["expected_total"], 2300.50, plan["tolerance"]) == ("adjust", 0.5)
    # on a 3-unit order even 0.04 is not rounding
    small = _plan(_order(69.04), [_item(5001, "FP-TEST-00001", 3, 20.0)])
    assert small["tolerance"] == 0.03
    assert mapping.total_check(small["expected_total"], 69.04, small["tolerance"])[0] == "mismatch"
    assert mapping.total_check(small["expected_total"], 69.03, small["tolerance"])[0] == "adjust"


def test_redact_payment_data():
    order = _order(69.0, receipt={"file": "fake-receipt.pdf"}, bank_receipt_url="https://x.invalid/s?sig=FAKE",
                   iban="SA0000000000000000000000", account_number="0000000000",
                   avatar="https://x.invalid/a.png", checkout_url="https://x.invalid/c")
    order["customer"]["avatar"] = "https://x.invalid/b.png"
    out = mapping.redact(order)
    for key in ("receipt", "bank_receipt_url", "iban", "account_number", "avatar", "checkout_url"):
        assert out[key] == "***", key
    assert out["customer"]["avatar"] == "***"
    dump = json.dumps(out)
    assert "FAKE" not in dump and "SA0000" not in dump and "x.invalid" not in dump
    assert out["amounts"]["total"] == {"amount": 69.0, "currency": "SAR"}
    assert out["status"]["slug"] == "under_review"


# --- cursor ------------------------------------------------------------------------

def test_next_cursor_held_by_missing_sku():
    end = datetime(2026, 10, 18, 12, 0, 0)
    a, b = datetime(2026, 10, 18, 9, 0, 0), datetime(2026, 10, 18, 10, 30, 0)
    assert mapping.next_cursor(end, []) == end
    assert mapping.next_cursor(end, None) == end
    assert mapping.next_cursor(end, [b]) == b                          # held at the order
    assert mapping.next_cursor(end, [b, a]) == a                       # oldest hold wins
    assert mapping.next_cursor(end, [end + timedelta(hours=1)]) == end  # never past the window
    assert mapping.next_cursor(end, [a, None]) is None                 # unknown date: do not move
    # the held window still contains the skipped order on the next run (inclusive start)
    held = mapping.next_cursor(end, [b])
    assert held <= b


def test_holds_cursor_payment_pending():
    now = datetime(2026, 10, 18, 12, 0, 0)
    assert mapping.holds_cursor("payment_pending", now - timedelta(days=2), now)
    assert mapping.holds_cursor("Payment-Pending", now - timedelta(days=7), now)    # boundary
    assert not mapping.holds_cursor("payment_pending", now - timedelta(days=7, seconds=1), now)
    assert mapping.holds_cursor("payment_pending", None, now)
    for st in ("cancelled", "canceled", "refunded", "under_review", ""):
        assert not mapping.holds_cursor(st, now - timedelta(days=1), now), st


# --- notice ------------------------------------------------------------------------

def test_stock_rows_short_lines():
    rows, short = mapping.stock_rows([
        ("Fake Product A", 3.0, 1.0, True),     # short: must be manufactured
        ("Fake Product B", 4.0, 10.0, True),
        ("Shipping", 1.0, 0.0, False),          # service: never short
        ("Fake Product C", 3.0, None, True)])   # unknown stock counts as 0
    assert short
    assert [r["short"] for r in rows] == [True, False, False, True]
    assert rows[3]["available"] == 0.0
    rows, short = mapping.stock_rows([("Fake Product B", 3.0, 3.0, True)])
    assert not short and rows[0]["short"] is False                     # exactly enough
    assert mapping.stock_rows([]) == ([], False)


# --- logging of order payloads ----------------------------------------------------

def test_redact_order_notes():
    order = _order(69.0, notes="call me on 0500000000", customer_note="Fake St 1",
                   gift_message="from Fake", comment="x", note="y")
    out = mapping.redact(order)
    for key in ("notes", "customer_note", "gift_message", "comment", "note"):
        assert out[key] == "***", key
    dump = json.dumps(out)
    assert "0500000000" not in dump and "500000000" not in dump and "Fake St" not in dump
    assert "fake.buyer" not in dump
    # money and status stay readable for checking field names
    assert out["amounts"]["total"] == {"amount": 69.0, "currency": "SAR"}
    assert out["status"]["slug"] == "under_review"
    assert out["id"] == 900000101 and out["reference_id"] == 70000101


# --- F2a fix round 1 (staging findings 2026-10-18) ---------------------------------

def test_redact_ip_and_user_agent():
    """Real payload: source_details.ip and source_details["user-agent"] were logged in
    clear. "ip" is an EXACT key match: as a substring it would mask shipping,
    shipping_cost, description, recipient... (all contain "ip")."""
    order = _order(491.92, shipping=25.0, cod=10.0,
                   source="api",
                   source_details={"type": "api", "value": None, "device": "desktop",
                                   "user-agent": "FakeAgent/1.0 (test)",
                                   "ip": "203.0.113.7"},           # TEST-NET-3, fake
                   description="fake order description",
                   tracking=[{"IP": "198.51.100.9", "user_agent": "FakeAgent/2.0",
                              "User-Agent": "FakeAgent/3.0", "step": 2}])
    out = mapping.redact(order)
    sd = out["source_details"]
    assert sd["ip"] == "***" and sd["user-agent"] == "***"
    assert out["tracking"][0]["IP"] == "***"                         # case-insensitive
    assert out["tracking"][0]["user_agent"] == "***"
    assert out["tracking"][0]["User-Agent"] == "***"
    dump = json.dumps(out)
    for leaked in ("203.0.113.7", "198.51.100.9", "FakeAgent"):
        assert leaked not in dump, leaked
    # neighbours of the exact "ip" key stay readable
    assert sd["type"] == "api" and sd["device"] == "desktop" and sd["value"] is None
    assert out["tracking"][0]["step"] == 2
    assert out["source"] == "api"
    assert out["description"] == "fake order description"
    assert out["amounts"]["shipping_cost"] == {"amount": 25.0, "currency": "SAR"}
    assert out["amounts"]["cash_on_delivery"] == {"amount": 10.0, "currency": "SAR"}
    keep = {"shipping": {"company": "Fake Courier", "shipping_cost": 25,
                         "ship_to_type": "pickup"},
            "shipping_company": "Fake Courier", "shipment_id": 5,
            "description": "d", "recipient_count": 1, "membership_tier": "gold",
            "zip_code_id": None, "ipsum": "x", "skip": True}
    redacted = mapping.redact(keep)
    redacted.pop("zip_code_id")       # "zip" is a PII substring by design (postal code)
    keep.pop("zip_code_id")
    assert redacted == keep, redacted
    # ip as a prefix/suffix word: client_ip, remote_ip, ip_addr, ip-address, X-Real-IP
    ipish = {"client_ip": "192.0.2.1", "remote_ip": "192.0.2.2", "ip_addr": "192.0.2.3",
             "ip-address": "192.0.2.4", "X-Real-IP": "192.0.2.5", "Customer_IP": "192.0.2.6"}
    assert set(mapping.redact(ipish).values()) == {"***"}, mapping.redact(ipish)
    assert "192.0.2" not in json.dumps(mapping.redact({"src": [ipish]}))
    # ...but not every word that merely starts/ends with the letters "ip"
    letters = {"ipsum": 1, "skip": 2, "tip": 3, "recipient": 4, "ship_to": 5, "ipv": 6}
    assert mapping.redact(letters) == letters, mapping.redact(letters)
    # previously masked keys still masked (no regression from the new rule)
    prev = mapping.redact({"receipt": "r", "shipping_address": "a", "email": "e",
                           "access_token": "FAKE"})
    assert set(prev.values()) == {"***"}, prev
    assert order["source_details"]["ip"] == "203.0.113.7"             # input untouched


def _real_cod_items():
    # 3 x 100.00 + 4 x 31.94 = 427.76 net, the sub_total of the real order 504361123
    # (fake SKUs/ids). x 1.15 per line: 345.00 + 146.92 = 491.92.
    return [_item(6001, "FP-TEST-00001", 3, 100.0), _item(6002, "FP-TEST-00002", 4, 31.94)]


def test_cod_not_charged_dropped():
    """Staging finding: amounts.cash_on_delivery 10 but total 491.92 = sub_total x 1.15,
    COD nowhere. Before the fix a COD line (8.6956522 net -> 10.01) made Odoo 501.93 and
    the order 'unreconciled'. Now the COD line is dropped and reported."""
    order = _order(491.92, shipping=0, cod=10.0, discounts=[])
    order["amounts"]["sub_total"] = _amt(427.76)
    order["amounts"]["tax"] = {"percent": "15.00", "amount": _amt(64.16)}
    plan = _plan(order, _real_cod_items())
    assert [p["quantity"] for p in plan["products"]] == [3, 4]
    assert [p["price_unit"] for p in plan["products"]] == [100.0, 31.94]   # per unit
    assert [e for e in plan["extras"] if e["kind"] == "cod"] == []
    assert plan["extras"] == []
    assert plan["cod_not_charged"] == 10.0
    assert plan["extras_basis"] == "none"
    assert plan["expected_total"] == 491.92
    assert mapping.total_check(plan["expected_total"], 491.92, plan["tolerance"]) == ("ok", 0.0)


def test_cod_charged_same_items_keeps_line():
    """Same goods, but Salla DOES charge the 10.00 COD (total 501.92): the COD line stays,
    nothing is reported dropped, the 0.01 per-line rounding is absorbed as before."""
    plan = _plan(_order(501.92, cod=10.0), _real_cod_items())
    assert [(e["kind"], e["price_unit"]) for e in plan["extras"]] == [("cod", 8.6956522)]
    assert plan["cod_not_charged"] == 0.0
    assert plan["extras_basis"] == "gross"
    assert plan["expected_total"] == 501.93
    assert mapping.total_check(plan["expected_total"], 501.92, plan["tolerance"]) == ("adjust", -0.01)


def test_cod_charged_vat_zero_dashboard_order():
    """Manual dashboard order with VAT 0: 3 x 106.94 = 320.82 + COD 1.00 = 321.82.
    With VAT 0 gross == net, so only the 'with COD' readings reconcile: COD kept."""
    order = _order(321.82, cod=1.0)
    order["amounts"]["tax"] = {"percent": "0.00", "amount": _amt(0)}
    norm = mapping.normalize_order(order)
    assert norm["vat_rate"] == 0.0
    lines, problems = mapping.normalize_items([_item(6003, "FP-TEST-00003", 3, 106.94)])
    assert problems == []
    plan = mapping.build_lines(norm, lines)
    assert [(e["kind"], e["price_unit"]) for e in plan["extras"]] == [("cod", 1.0)]
    assert plan["cod_not_charged"] == 0.0
    assert plan["expected_total"] == 321.82
    # and the same order without the COD in Salla's total drops it
    order2 = _order(320.82, cod=1.0)
    order2["amounts"]["tax"] = {"percent": "0.00", "amount": _amt(0)}
    plan2 = mapping.build_lines(mapping.normalize_order(order2), lines)
    assert plan2["extras"] == [] and plan2["cod_not_charged"] == 1.0
    assert plan2["expected_total"] == 320.82


def test_cod_not_charged_with_coupon_and_shipping():
    """COD dropped while shipping and a non-zero coupon are still charged (gross)."""
    items = [_item(5001, "FP-TEST-00001", 3, 20.0), _item(5002, "FP-TEST-00002", 4, 12.5)]
    # goods 126.50 gross - coupon 25.30 + shipping 23.00 = 124.20; COD 11.50 not charged
    plan = _plan(_order(124.2, shipping=23.0, cod=11.5,
                        discounts=[{"title": "TEST20", "discount": 25.3}]), items)
    kinds = sorted((e["kind"], e["price_unit"]) for e in plan["extras"])
    assert kinds == [("discount", -22.0), ("shipping", 20.0)]
    assert plan["cod_not_charged"] == 11.5
    assert plan["extras_basis"] == "gross"
    assert mapping.total_check(plan["expected_total"], 124.2, plan["tolerance"]) == ("ok", 0.0)
    # the same order with the COD charged still takes all three extras
    plan = _plan(_order(135.7, shipping=23.0, cod=11.5,
                        discounts=[{"title": "TEST20", "discount": 25.3}]), items)
    assert sorted(e["kind"] for e in plan["extras"]) == ["cod", "discount", "shipping"]
    assert plan["cod_not_charged"] == 0.0


def test_cod_not_charged_net_shipping():
    """Shipping read as NET (20 net -> 23.00 with VAT) and COD not charged."""
    plan = _plan(_order(514.92, shipping=20.0, cod=10.0), _real_cod_items())
    assert [(e["kind"], e["price_unit"]) for e in plan["extras"]] == [("shipping", 20.0)]
    assert plan["extras_basis"] == "net"
    assert plan["cod_not_charged"] == 10.0
    assert plan["expected_total"] == 514.92


def test_cod_tiny_fee_charged_wins_tie_or_closest():
    """A 0.01 COD fee: both readings sit within the rounding tolerance; the closest wins
    and, on an exact tie, the reading that CHARGES the COD (the payload's default)."""
    items = [_item(6004, "FP-TEST-00004", 3, 10.0)]          # 34.50 gross
    plan = _plan(_order(34.51, cod=0.01), items)              # COD charged: exact
    assert [e["kind"] for e in plan["extras"]] == ["cod"]
    assert plan["cod_not_charged"] == 0.0
    plan = _plan(_order(34.5, cod=0.01), items)               # gross 0.01 -> net 0.0087 -> 0.01 line
    # dropped reading is exact (34.50), charged one is 34.51: closest = dropped
    assert plan["extras"] == [] and plan["cod_not_charged"] == 0.01


def test_no_cod_field_reports_zero():
    plan = _plan(_order(103.5), [_item(6005, "FP-TEST-00005", 3, 30.0)])
    assert plan["cod_not_charged"] == 0.0 and plan["extras_basis"] == "none"
    assert plan["expected_total"] == 103.5


def test_fmt_money():
    assert mapping.fmt_money(370.09000000000003) == "370.09"
    assert mapping.fmt_money(321.82) == "321.82"
    assert mapping.fmt_money(10) == "10.00"
    assert mapping.fmt_money("12.5") == "12.50"
    assert mapping.fmt_money(2.675) == "2.68"                 # half away from zero
    assert mapping.fmt_money(-2.675) == "-2.68"
    assert mapping.fmt_money(-0.001) == "0.00"                 # never "-0.00"
    assert mapping.fmt_money(-0.01) == "-0.01"
    assert mapping.fmt_money(0) == "0.00"
    for bad in (None, True, False, "abc", "", float("nan"), float("inf"), [], {}):
        assert mapping.fmt_money(bad) == "?", bad


# --- F2b: real customer and national address -------------------------------------
# Fake numbers only: +966 50 123 4567 is a placeholder, every name/email is fake and
# the email domain is the reserved ".invalid" TLD.

E164 = "+966501234567"


def test_e164_ksa_formats_same_customer():
    """The ticket case: the same phone written with/without spaces, with +966 / 0 /
    00966 / nothing, must collapse to ONE E.164 value (= one customer)."""
    for number in ("0501234567", "501234567", "+966 50 123 4567", "00966501234567",
                   "966501234567", "+966501234567", "+966 0 50 123 4567",
                   "(050) 123-4567", "050-123-4567", "050.123.4567",
                   " 0501234567\n", "966 50 123 4567"):
        assert mapping.e164_ksa(number) == E164, number


def test_e164_ksa_mobile_code_split():
    """mobile_code + mobile as Salla may send them (format unverified: both shapes)."""
    for number, code in (("501234567", "+966"), ("501234567", "966"),
                         ("501234567", "00966"), ("501234567", " +966 "),
                         ("0501234567", "+966"),          # trunk 0 kept by the store
                         ("966501234567", "+966"),        # number already has the code
                         ("+966501234567", "966"), ("00966501234567", "+966"),
                         ("966 50 123 4567", "+966"), ("050-123-4567", "00966"),
                         ("501234567", 966),              # code as a number
                         ("501234567", ""), ("501234567", None)):
        assert mapping.e164_ksa(number, code) == E164, (number, code)


def test_e164_ksa_unicode_digits_and_marks():
    arabic_indic = "٠٥٠١٢٣٤٥٦٧"  # ٠٥٠١٢٣٤٥٦٧
    assert mapping.e164_ksa(arabic_indic) == E164
    extended = "".join(chr(0x06F0 + int(d)) for d in "0501234567")          # ۰۵۰...
    assert mapping.e164_ksa(extended) == E164
    # Direction marks pasted from an RTL screen are separators, not garbage.
    assert mapping.e164_ksa("‎+966501234567‏") == E164
    # Fullwidth / superscript digits pass str.isdigit() but are NOT 0-9: rejected,
    # never written (WhatsApp / Odoo phone validation would choke on them).
    fullwidth = "".join(chr(0xFF10 + int(d)) for d in "501234567")
    assert mapping.e164_ksa(fullwidth) == ""
    assert mapping.e164_ksa("50123456²") == ""
    # The result is always plain ASCII.
    assert mapping.e164_ksa(arabic_indic).isascii()


def test_e164_ksa_unusable_is_empty():
    """Anything that is not a KSA mobile -> "" (never written; confirmation WhatsApp
    would error inside action_confirm, Trendyol §11)."""
    for number, code in (("0112345678", ""),          # Riyadh landline
                         ("+966112345678", ""), ("966112345678", ""),
                         ("5512345678", "+52"), ("+525512345678", ""),   # Mexico
                         ("501234567", "+52"),         # KSA-looking number, foreign code
                         ("50123456", ""), ("05012345678", ""),          # 8 / 11 digits
                         ("5012345678", ""), ("+9665012345678", ""),     # one digit too many
                         ("0096650123456", ""), ("9665012345", "966"),
                         ("05O1234567", ""), ("abc", ""),               # letter O
                         ("+966 50 123 4567 ext 1", ""),
                         ("+", ""), ("00", ""), ("0", ""), ("   ", ""), ("", ""),
                         (None, None), (501234567.0, ""), (True, ""), ({}, ""),
                         ([], "+966")):
        assert mapping.e164_ksa(number, code) == "", (number, code)
    # int 501234567 is a plausible JSON number: accepted like the string.
    assert mapping.e164_ksa(501234567) == E164


def test_clean_email():
    assert mapping.clean_email("  Fake.Buyer@Example.Invalid ") == "fake.buyer@example.invalid"
    assert mapping.clean_email("FAKE@EXAMPLE.INVALID") == mapping.clean_email("fake@example.invalid")
    assert mapping.clean_email("a@b.c") == "a@b.c"
    for bad in ("a b@x.invalid", "x@y", "@b.invalid", "a@@b.invalid", "a@b.invalid.",
                "x@y.invalid,z@w.invalid", "<a@b.invalid>", "x@y.invalid; z@w.invalid",
                "", "   ", None, True, 42, {}, []):
        assert mapping.clean_email(bad) == "", bad


def test_national_short_code_ok_and_normalized():
    for raw in ("RRRD2929", "rrrd2929", "RRRD 2929", " rr-rd 2929 ", "RRRD\t2929\n",
                "rRrD-29 29"):
        assert mapping.national_short_code(raw) == ("RRRD2929", "ok"), raw
    assert mapping.national_short_code("ABCD١٢٣٤") == ("ABCD1234", "ok")
    code, state = mapping.national_short_code("abcd 1234")
    assert state == "ok" and code.isascii() and len(code) == 8


def test_national_short_code_invalid_never_written():
    """Every value that would make wayakit_customization's x_national_short_code
    constraint raise ValidationError (and kill the order) -> ("", "invalid")."""
    for raw in ("RRR29299", "ABCD12345", "ABC1234", "ABCDE123", "1234ABCD", "RRRD292",
                "ÄBCD1234",                                 # non-ASCII letter
                "ابجد2929",                  # Arabic letters
                "RRRD" + "".join(chr(0xFF10 + int(d)) for d in "2929"),  # fullwidth digits
                "RRRD_2929", "RRRD.2929", "RRRD/2929", "RRRD2929X", 12345678, 2929.0):
        assert mapping.national_short_code(raw) == ("", "invalid"), raw
    for empty in ("", "   ", None, False, {}, []):
        assert mapping.national_short_code(empty) == ("", "missing"), empty
    # Whatever comes out "ok" satisfies the Odoo constraint ^[A-Za-z]{4}\d{4}$ with
    # ASCII only, and is already upper case (no onchange needed).
    import re
    for raw in ("rrrd2929", "ABCD١٢٣٤", " ab-cd 12 34 "):
        code, state = mapping.national_short_code(raw)
        assert state == "ok" and re.fullmatch(r"[A-Z]{4}[0-9]{4}", code, re.ASCII), raw


# A shipping order as the Salla docs describe it (never seen for real: fake values).
def _ship_order(container="shipping.address", **over):
    addr = {"name": "Fake Receiver", "building_number": "1234", "street_number": "Fake St",
            "district": "Fake District", "block": "B7", "city": {"name": "FakeCity"},
            "postal_code": "12345", "short_address": "abcd 1234",
            "additional_number": "6789", "country": "SA",
            "geo_coordinates": {"lat": "1.0", "lng": "2.0"}}
    addr.update(over)
    order = {"id": 900001, "customer": {"id": 7700001}}
    node = order
    keys = container.split(".")
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = addr
    return order


def test_shipping_address_docs_shape():
    a = mapping.shipping_address(_ship_order())
    assert a == {"name": "Fake Receiver", "street": "1234 Fake St",
                 "street2": "Fake District, B7", "city": "FakeCity", "zip": "12345",
                 "short_code": "ABCD1234", "short_code_state": "ok",
                 "short_code_raw": "abcd 1234"}, a
    # No country key: the partner is always SA, the payload country is ignored.
    assert "country" not in a and "country_code" not in a


def test_shipping_address_every_container():
    for path in ("shipping.address", "shipping.ship_to", "ship_to", "shipping_address",
                 "shipping.receiver.address", "receiver.address", "receiver"):
        a = mapping.shipping_address(_ship_order(path))
        assert a["street"] == "1234 Fake St" and a["short_code"] == "ABCD1234", path
    # First DICT wins: a string where the first path points is skipped, not used.
    o = {"shipping": {"address": "a plain string"},
         "ship_to": {"city": "FakeCity", "postal_code": "12345"}}
    assert mapping.shipping_address(o)["city"] == "FakeCity"


def test_shipping_address_fields_variants():
    a = mapping.shipping_address(_ship_order(address_line="  Fake   Line 1 ",
                                             city="FakeCity", district={"name": "D1"},
                                             block="", postal_code=12345,
                                             short_address="RRR29299"))
    # address_line wins, spaces collapsed; the building number (1234 from the fixture)
    # is prefixed because address_line does not carry it (fix round 1, Saudi style).
    assert a["street"] == "1234 Fake Line 1", a["street"]
    assert a["street2"] == "D1"                  # empty block: no dangling ", "
    assert a["city"] == "FakeCity" and a["zip"] == "12345"   # numeric postal code
    assert a["short_code"] == "" and a["short_code_state"] == "invalid"
    assert a["short_code_raw"] == "RRR29299"     # kept for the chatter note only
    # Arabic-Indic postal code -> ASCII digits.
    a = mapping.shipping_address(_ship_order(postal_code="١٢٣٤٥"))
    assert a["zip"] == "12345"
    # zip_code / zip fallbacks; short_code / national_short_code fallbacks.
    o = {"ship_to": {"city": "C", "zip_code": "11111", "national_short_code": "wxyz9999"}}
    a = mapping.shipping_address(o)
    assert a["zip"] == "11111" and a["short_code"] == "WXYZ9999"
    o = {"ship_to": {"city": "C", "short_address": "", "short_code": "wxyz9999"}}
    assert mapping.shipping_address(o)["short_code"] == "WXYZ9999"
    # Missing short code with a real address: "missing", not "invalid" (no note).
    a = mapping.shipping_address({"ship_to": {"street": "Fake St", "city": "C"}})
    assert a["short_code"] == "" and a["short_code_state"] == "missing"
    assert a["street"] == "Fake St"
    # Only a short code: still an address (the code alone is worth saving).
    a = mapping.shipping_address({"ship_to": {"national_address": "abcd1234"}})
    assert a["short_code"] == "ABCD1234" and a["street"] == ""


def test_shipping_address_pickup_is_empty():
    """F2a orders were all pickup (receiver null): no address, nothing written."""
    for order in ({}, {"delivery_method": "pickup", "receiver": None},
                  {"shipping": None, "ship_to": None}, {"shipping": {"address": {}}},
                  {"receiver": {"name": "Fake Receiver", "phone": "0501234567",
                                "email": "r@example.invalid"}},     # contact, no address
                  {"ship_to": {"geo_coordinates": {"lat": "1", "lng": "2"},
                               "country": "SA"}},
                  None, [], "x", 42):
        assert mapping.shipping_address(order) == {}, order


def _buyer_order(**cust):
    c = {"id": 7700001, "first_name": "Fake", "last_name": "Buyer", "mobile": "501234567",
         "mobile_code": "+966", "email": " Fake@Example.Invalid", "city": "FakeCity",
         "country": "Mexico", "country_code": "MX"}
    c.update(cust)
    return {"id": 900001, "customer": c}


def test_buyer_full():
    order = _buyer_order()
    order["shipping"] = _ship_order()["shipping"]
    b = mapping.buyer(order)
    assert b == {"ref": "7700001", "name": "Fake Buyer", "phone": E164, "phone_state": "ok",
                 "email": "fake@example.invalid", "city": "FakeCity",
                 "address": mapping.shipping_address(order), "address_missing": False}, b
    assert b["address"]["short_code"] == "ABCD1234"
    # The payload country (a real test customer said "MX") is NOT carried at all:
    # the partner is always SA.
    assert not any("country" in k for k in b)
    assert "MX" not in repr(b) and "Mexico" not in repr(b)


def test_buyer_same_customer_any_phone_format():
    phones = {mapping.buyer(_buyer_order(mobile=n, mobile_code=c))["phone"]
              for n, c in (("501234567", "+966"), ("0501234567", ""),
                           ("+966 50 123 4567", None), ("00966501234567", "+966"),
                           ("966501234567", "966"))}
    assert phones == {E164}, phones


def test_buyer_phone_states():
    b = mapping.buyer(_buyer_order(mobile="0112345678", mobile_code=""))
    assert b["phone"] == "" and b["phone_state"] == "invalid"     # landline: not written
    b = mapping.buyer(_buyer_order(mobile="", mobile_code="+966"))
    assert b["phone"] == "" and b["phone_state"] == "missing"     # code alone is nothing
    b = mapping.buyer(_buyer_order(mobile=None, mobile_code=None))
    assert b["phone_state"] == "missing"
    # mobile unusable, customer.phone usable -> the usable one, "ok".
    b = mapping.buyer(_buyer_order(mobile="0112345678", mobile_code="", phone="0501234567"))
    assert b["phone"] == E164 and b["phone_state"] == "ok"
    # mobile usable wins over phone.
    b = mapping.buyer(_buyer_order(phone="0559999999"))
    assert b["phone"] == E164
    b = mapping.buyer(_buyer_order(mobile="", phone="garbage"))
    assert b["phone"] == "" and b["phone_state"] == "invalid"


def test_buyer_email_and_name_fallbacks():
    b = mapping.buyer(_buyer_order(email="not-an-email"))
    assert b["email"] == ""
    b = mapping.buyer(_buyer_order(first_name="", last_name="", full_name="  Fake   Full "))
    assert b["name"] == "Fake Full"
    b = mapping.buyer(_buyer_order(city={"name": "FakeCity2"}))
    assert b["city"] == "FakeCity2"


def test_buyer_garbage_and_guest():
    empty = {"ref": "", "name": "", "phone": "", "phone_state": "missing", "email": "",
             "city": "", "address": {}, "address_missing": False}
    for order in ({}, None, [], {"customer": None}, {"customer": "x"}, {"customer": []}):
        assert mapping.buyer(order) == empty, order
    # No customer id (guest order) -> ref "": the backend must route it to the shared
    # guest contact and never match it by phone/email.
    b = mapping.buyer({"customer": {"id": None, "mobile": "0501234567"}})
    assert b["ref"] == "" and b["phone"] == E164
    b = mapping.buyer({"customer": {"id": "None", "mobile": "0501234567"}})
    assert b["ref"] == ""


def test_fill_empty_never_overwrites_ops_data():
    ops = {"name": "Ops Typed Name", "email": "ops@example.invalid", "phone": "+966551112222",
           "street": "Ops Street 1", "city": "Ops City", "zip": "11111",
           "x_national_short_code": "OPST1111", "mobile": False, "salla_customer_id": False}
    incoming = {"name": "Fake Buyer", "email": "fake@example.invalid", "phone": E164,
                "street": "1234 Fake St", "city": "FakeCity", "zip": "12345",
                "x_national_short_code": "ABCD1234", "mobile": E164,
                "salla_customer_id": "7700001"}
    assert mapping.fill_empty(ops, incoming) == {"mobile": E164,
                                                 "salla_customer_id": "7700001"}
    # Empty incoming values never blank anything, even on an empty field.
    assert mapping.fill_empty({"phone": False, "email": ""},
                              {"phone": "", "email": None, "city": False}) == {}
    assert mapping.fill_empty({"email": "ops@example.invalid", "phone": False},
                              {"email": "a@b.c", "phone": E164, "city": ""}) == {"phone": E164}
    # A brand-new contact: everything non-empty goes in.
    assert mapping.fill_empty({}, {"phone": E164, "city": ""}) == {"phone": E164}
    assert mapping.fill_empty(None, None) == {}
    # The salla_customer_id of a contact found by phone/email is written only if empty.
    assert mapping.fill_empty({"salla_customer_id": "5500001"},
                              {"salla_customer_id": "7700001"}) == {}


def test_same_address():
    cur = {"street": " Fake  St 1 ", "city": "fakecity"}
    assert mapping.same_address(cur, {"street": "FAKE ST 1", "city": "FakeCity "})
    assert not mapping.same_address(cur, {"street": "Fake St 2", "city": "FakeCity"})
    assert not mapping.same_address(cur, {"street": "Fake St 1", "city": "OtherCity"})
    # Empty street on either side is never "the same" (nothing to compare).
    assert not mapping.same_address({"street": "", "city": "C"}, {"street": "", "city": "C"})
    assert not mapping.same_address({"street": False, "city": "C"}, {"street": "x", "city": "C"})
    assert not mapping.same_address(None, None)
    # Both cities empty, same street -> same.
    assert mapping.same_address({"street": "a", "city": ""}, {"street": "A", "city": None})


def test_pick_partner_rules():
    cands = [{"id": 5, "salla_customer_id": "99"},                 # another Salla buyer
             {"id": 9, "salla_customer_id": "", "active": False},  # archived
             {"id": 7, "parent_id": 3},                            # child contact
             {"id": 8, "type": "delivery"},                        # an address, not a buyer
             {"id": 12}]
    assert mapping.pick_partner(cands, "1") == (12, 3)
    assert mapping.pick_partner([], "1") == (None, 0)
    assert mapping.pick_partner(None) == (None, 0)
    # Only linked to someone else / only addresses -> nobody, the buyer gets a new contact.
    assert mapping.pick_partner([{"id": 5, "salla_customer_id": "99"}], "") == (None, 0)
    assert mapping.pick_partner([{"id": 4, "type": t} for t in
                                 ("delivery", "invoice", "other", "private")]) == (None, 0)
    # Linked to THIS ref first, even with a higher id.
    assert mapping.pick_partner([{"id": 2}, {"id": 50, "salla_customer_id": "1"}], "1")[0] == 50
    assert mapping.pick_partner([{"id": 2}, {"id": 50, "salla_customer_id": " 1 "}], 1)[0] == 50
    # Active beats archived even when the archived one has the lower id.
    assert mapping.pick_partner([{"id": 3, "active": False}, {"id": 40}])[0] == 40
    # Top-level contact beats a company's child contact.
    assert mapping.pick_partner([{"id": 3, "parent_id": 1}, {"id": 40, "parent_id": False}])[0] == 40
    # Falsy/None/"None" salla ids count as not linked; type "contact" / None are buyers.
    assert mapping.pick_partner([{"id": 6, "salla_customer_id": False, "type": "contact"},
                                 {"id": 4, "salla_customer_id": "None", "type": None}]) == (4, 2)
    # Single eligible = not ambiguous.
    assert mapping.pick_partner([{"id": 6}, {"id": 4, "type": "delivery"}]) == (6, 1)


def test_pick_partner_deterministic_any_order():
    import itertools
    cands = [{"id": 31}, {"id": 17, "active": False}, {"id": 22, "parent_id": 9},
             {"id": 45}, {"id": 12, "type": "invoice"}, {"id": 8, "salla_customer_id": "x"}]
    results = {mapping.pick_partner(list(p), "7700001")
               for p in itertools.permutations(cands)}
    assert results == {(31, 4)}, results


def test_redact_shipping_address_pii():
    addr = {"short_address": "ABCD1234", "short_code": "ABCD1234",
            "national_short_code": "ABCD1234", "building_number": "1234",
            "additional_number": "6789", "postal_code": "12345", "street_number": "Fake St",
            "street": "Fake St", "district": "Fake District", "block": "B7",
            "address_line": "Fake Line", "city": "FakeCity", "name": "Fake Receiver",
            "phone": "0501234567", "mobile": "501234567", "mobile_code": "+966",
            "email": "r@example.invalid",
            "geo_coordinates": {"lat": "24.1", "lng": "46.1"},
            "lat": "24.1", "lng": "46.1", "lon": "46.1",
            "latitude": "24.1", "longitude": "46.1"}
    for path in ("ship_to", "shipping", "receiver", "shipping.ship_to"):
        order = {}
        node = order
        keys = path.split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = dict(addr)
        out = mapping.redact(order)
        node = out
        for k in keys:
            node = node[k]
        if node == "***":        # e.g. a whole "shipping_address" container: fine
            continue
        leaked = {k: v for k, v in node.items() if v != "***"}
        assert leaked == {}, (path, sorted(leaked))
    # Nested under "address": the whole container is masked.
    assert mapping.redact({"shipping": {"address": dict(addr)}})["shipping"]["address"] == "***"
    # Not-PII keys that merely contain lat/lng/lon/geo-ish letters stay readable.
    out = mapping.redact({"translation": "t", "template": "t", "long_description": "d",
                          "latest": "l", "flat_rate": 5, "longest": 1, "country": "SA",
                          "shipping_cost": {"amount": 25}})
    assert out == {"translation": "t", "template": "t", "long_description": "d",
                   "latest": "l", "flat_rate": 5, "longest": 1, "country": "SA",
                   "shipping_cost": {"amount": 25}}, out
    # No address VALUE ever survives anywhere in the dump.
    dump = json.dumps(mapping.redact(_ship_order("ship_to")))
    for value in ("ABCD", "1234", "6789", "12345", "Fake St", "Fake District", "24.1",
                  "FakeCity", "Fake Receiver"):
        assert value not in dump, value


def test_e164_ksa_non_digit_mobile_code_ignored():
    """If Salla's mobile_code turns out to be an ISO code ("SA"), the number must not
    be dropped because of it; a foreign DIGIT code still rejects a bare number."""
    for number, code in (("+966501234567", "SA"), ("501234567", "SA"),
                         ("0501234567", "KSA"), ("00966501234567", "sa")):
        assert mapping.e164_ksa(number, code) == E164, (number, code)
    assert mapping.e164_ksa("0112345678", "SA") == ""          # still a landline
    assert mapping.e164_ksa("+525512345678", "SA") == ""       # still foreign
    assert mapping.e164_ksa("501234567", "+52") == ""


def test_shipping_address_short_code_object():
    a = mapping.shipping_address({"ship_to": {"city": "C",
                                              "short_address": {"code": "abcd1234"}}})
    assert a["short_code"] == "ABCD1234" and a["short_code_state"] == "ok", a
    a = mapping.shipping_address({"ship_to": {"city": "C", "short_address": {"foo": 1}}})
    assert a["short_code"] == "" and a["short_code_state"] == "invalid", a
    # An unreadable object is never echoed into the chatter as a dict repr.
    assert "foo" not in a["short_code_raw"]
    a = mapping.shipping_address({"ship_to": {"city": "C",
                                              "short_address": {"code": "RRR29299"}}})
    assert a["short_code"] == "" and a["short_code_state"] == "invalid"


def test_pick_partner_never_internal():
    """An employee's own contact (res.users partner) or a company partner sharing the
    buyer's phone/email is never the Salla customer: a new contact is created instead."""
    staff = {"id": 3, "salla_customer_id": "", "active": True, "type": "contact",
             "internal": True}
    buyer_c = {"id": 9, "salla_customer_id": "", "active": True, "type": "contact"}
    assert mapping.pick_partner([staff, buyer_c], "1") == (9, 1)
    assert mapping.pick_partner([buyer_c, staff], "1") == (9, 1)
    assert mapping.pick_partner([staff], "1") == (None, 0)
    # Internal wins over "linked to this same ref" (a bad link from before the fix).
    assert mapping.pick_partner([dict(staff, salla_customer_id="1")], "1") == (None, 0)
    # Archived internal user's partner: still excluded.
    assert mapping.pick_partner([dict(staff, active=False)], "") == (None, 0)
    # Missing / False / None "internal" = a normal contact.
    assert mapping.pick_partner([{"id": 4, "internal": False}, {"id": 6, "internal": None},
                                 {"id": 2}]) == (2, 3)


def test_address_fillable():
    """Address on the contact itself vs a delivery child. A pickup order leaves only
    the city on the contact: the first shipped order in that city must still land on
    the contact. Any typed street/street2/zip, or another city, is Ops data: kept, and
    the order ships to a delivery child instead."""
    a = {"street": "1 Fake St", "city": "Riyadh"}
    for cur in ({}, None, {"city": "riyadh "}, {"city": "Riyadh"},
                {"street": False, "street2": False, "city": False, "zip": False},
                {"street": "   "},                              # blank = empty
                {"street": "1 fake st", "city": "RIYADH"}):     # already that address
        assert mapping.address_fillable(cur, a), cur
    for cur in ({"city": "Jeddah"}, {"zip": "12345"}, {"street2": "Fake District"},
                {"street": "2 Other St", "city": "Riyadh"},
                {"street": "1 Fake St", "city": "Jeddah"},
                {"zip": "12345", "city": "Riyadh"}):
        assert not mapping.address_fillable(cur, a), cur
    # A contact with a city but an incoming address without one: not "the same city".
    assert not mapping.address_fillable({"city": "Riyadh"}, {"street": "1 Fake St", "city": ""})
    assert mapping.address_fillable(None, None)


# --- F2b fix round 1: the address only comes in the webhook body ------------
# Real key paths from the staging webhook of 2026-10-18 (values here are FAKE).
def _real_ship_to(**over):
    st = {"type": "address", "name": "Fake Receiver", "email": "r@example.invalid",
          "phone": "+966500000009", "country": "Saudi Arabia", "country_id": 1,
          "country_code": "SA", "region": {"id": 1, "name": "Fake Region", "code": "FR"},
          "city": "FakeCity", "city_id": 101, "district": "Fake District",
          "address_line": "Fake Line 1", "address_line_two": "Fake Line 2",
          "street_number": "Fake St", "block": "B7", "subblock": "S3",
          "short_address": "abcd 1234", "building_number": "1234",
          "additional_number": "6789", "postal_code": "12345",
          "latitude": "24.0000001", "longitude": "46.0000001", "branch_id": None}
    st.update(over)
    return st


def _real_ship_from():
    return {"type": "branch", "name": "Fake Warehouse", "email": "wh@example.invalid",
            "phone": "+966500000008", "country": "Saudi Arabia", "country_code": "SA",
            "city": "OriginCity", "district": "Origin District",
            "address_line": "Origin Line", "street_number": "Origin St",
            "short_address": "wxyz9999", "building_number": "9999",
            "postal_code": "99999", "latitude": "21.0", "longitude": "39.0",
            "branch_id": 555}


def _real_webhook_body(ship_to=None, ship_from=True, delivery="shipping", **data_over):
    shipment = {}
    if ship_to is not None:
        shipment["ship_to"] = ship_to
    if ship_from:
        shipment["ship_from"] = _real_ship_from()
    data = {"id": 900002, "reference_id": 70000002, "delivery_method": delivery,
            "customer": {"id": 7700002, "full_name": "Fake Buyer", "first_name": "Fake",
                         "last_name": "Buyer", "mobile": "500000001",
                         "mobile_code": "+966", "email": "fake.buyer@example.invalid",
                         "city": "CustomerCity", "country": "Mexico",
                         "country_code": "MX", "location": "Fake Location"},
            "amounts": {"sub_total": {"amount": 30, "currency": "SAR"},
                        "total": {"amount": 34.5, "currency": "SAR"}},
            "items": [{"sku": "FP-ACC-00004", "quantity": 3}],
            "shipping": {"id": 1, "company": "Fake Courier",
                         "receiver": {"name": "Fake Receiver", "email": "r@example.invalid",
                                      "phone": "+966500000009"},
                         "shipper": {"name": "Fake Warehouse"},
                         "pickup_address": {"city": "OriginCity"},
                         "address": "***", "branch_id": 555},
            "shipments": [shipment] if shipment else []}
    data.update(data_over)
    return {"event": "order.created", "merchant": 1000000001, "data": data}

# Every value of the fixture that is NOT part of the buyer's street address: none of
# these may reach the queue row (webhook_ship_to) or the Odoo address.
_NOT_ADDRESS_VALUES = ("r@example.invalid", "+966500000009", "24.0000001", "46.0000001",
                       "Fake Region", "6789", "fake.buyer@example.invalid", "500000001",
                       "CustomerCity", "Fake Location", "Mexico", "34.5", "FP-ACC-00004",
                       "Fake Courier")
_SHIP_FROM_VALUES = ("Fake Warehouse", "wh@example.invalid", "+966500000008", "OriginCity",
                     "Origin District", "Origin Line", "Origin St", "wxyz9999", "WXYZ9999",
                     "9999", "99999", "21.0", "39.0")


def test_shipping_address_real_webhook_shape():
    """The staging finding: the address lives at data.shipments[0].ship_to."""
    data = _real_webhook_body(_real_ship_to())["data"]
    a = mapping.shipping_address(data)
    assert a == {"name": "Fake Receiver", "street": "1234 Fake Line 1",
                 "street2": "Fake Line 2, Fake District, B7, S3", "city": "FakeCity",
                 "zip": "12345", "short_code": "ABCD1234", "short_code_state": "ok",
                 "short_code_raw": "abcd 1234"}, a
    # The real shape is read FIRST: an old docs-path container never shadows it.
    data["ship_to"] = {"city": "DocsCity", "postal_code": "00000"}
    assert mapping.shipping_address(data)["city"] == "FakeCity"
    # Nothing of ship_from, the customer, the coordinates or the region leaks in.
    dump = json.dumps(a, ensure_ascii=False)
    for value in _SHIP_FROM_VALUES + _NOT_ADDRESS_VALUES:
        assert value not in dump, value
    # buyer() on the merged detail: the address, and no "address missing" alert.
    b = mapping.buyer(data)
    assert b["address"] == mapping.shipping_address(data) and b["address_missing"] is False


def test_shipping_address_ship_from_never_used():
    # Only ship_from (merchant origin): no address at all, never Wayakit's warehouse.
    data = _real_webhook_body(None)["data"]
    assert data["shipments"] == [{"ship_from": _real_ship_from()}]
    assert mapping.shipping_address(data) == {}
    assert mapping.webhook_ship_to(_real_webhook_body(None)) == {}
    b = mapping.buyer(data)
    assert b["address"] == {} and b["address_missing"] is True       # shipping, no address
    # ship_to shaped like the merchant's (type branch/warehouse/...) is skipped too.
    for kind in ("branch", "Branch", "merchant", "store", "warehouse", "pickup_point"):
        body = _real_webhook_body(_real_ship_to(type=kind))
        assert mapping.shipping_address(body["data"]) == {}, kind
        assert mapping.webhook_ship_to(body) == {}, kind
    # A branch ship_to first, the buyer's second: the buyer's wins, origin never read.
    data = _real_webhook_body(_real_ship_to())["data"]
    data["shipments"].insert(0, {"ship_to": _real_ship_from()})
    a = mapping.shipping_address(data)
    assert a["city"] == "FakeCity" and a["short_code"] == "ABCD1234", a
    # shipments given as a single dict instead of a list still works.
    data = _real_webhook_body(_real_ship_to())["data"]
    data["shipments"] = data["shipments"][0]
    assert mapping.shipping_address(data)["city"] == "FakeCity"


def test_shipping_address_real_pickup_is_empty():
    # A pickup order: {} whatever ship_to carries (on pickup it is the branch).
    for method in ("pickup", "Pickup", " PICKUP "):
        data = _real_webhook_body(_real_ship_to(), delivery=method)["data"]
        assert mapping.shipping_address(data) == {}, method
        assert mapping.webhook_ship_to(_real_webhook_body(_real_ship_to(),
                                                          delivery=method)) == {}
        assert mapping.buyer(data)["address_missing"] is False      # pickup: no alert
    # Empty / garbage shipments lists: no address, no crash.
    for shipments in ([], [None], [{}], [{"ship_to": None}], [{"ship_to": {}}],
                      [{"ship_to": "a string"}], [{"ship_to": []}], "x", None, 42):
        data = {"delivery_method": "shipping", "shipments": shipments}
        assert mapping.shipping_address(data) == {}, shipments
        assert mapping.buyer(dict(data, customer={"id": 1}))["address_missing"] is True
        assert mapping.webhook_ship_to({"event": "order.created", "data": data}) == {}
    # No delivery_method at all (unknown): no address and NO alert (nothing to say).
    assert mapping.buyer({"customer": {"id": 1}})["address_missing"] is False


def test_shipping_address_real_short_code_states():
    for raw, code, state in (("abcd 1234", "ABCD1234", "ok"), ("RRRD-2929", "RRRD2929", "ok"),
                             ("ABCD١٢٣٤", "ABCD1234", "ok"), ("RRR29299", "", "invalid"),
                             ("ABCD12345", "", "invalid"), ("ABC1234", "", "invalid"),
                             ("", "", "missing"), (None, "", "missing")):
        a = mapping.shipping_address(_real_webhook_body(_real_ship_to(short_address=raw))["data"])
        assert (a["short_code"], a["short_code_state"]) == (code, state), (raw, a)
        # The same through the queue row (webhook -> JSON -> merge -> read).
        st = mapping.load_ship_to(mapping.dump_ship_to(
            mapping.webhook_ship_to(_real_webhook_body(_real_ship_to(short_address=raw)))))
        a2 = mapping.shipping_address(mapping.with_ship_to({"delivery_method": "shipping"}, st))
        assert (a2["short_code"], a2["short_code_state"]) == (code, state), (raw, a2)


def test_shipping_address_real_street_lines():
    def addr(**kw):
        st = {"type": "address", "city": "C"}
        st.update(kw)
        return mapping.shipping_address({"delivery_method": "shipping",
                                         "shipments": [{"ship_to": st}]})
    # address_line already holds the building number: not repeated.
    assert addr(address_line="1234 Fake Line", building_number="1234")["street"] == "1234 Fake Line"
    # No address_line: building number + street number.
    assert addr(building_number="1234", street_number="Fake St")["street"] == "1234 Fake St"
    # address_line that repeats the district: the district is not repeated in street2.
    a = addr(address_line="Fake Line, Fake District", district="Fake District", block="B7")
    assert a["street2"] == "B7", a
    # Duplicated parts in street2 appear once; empty parts leave no dangling ", ".
    a = addr(address_line="Fake Line", address_line_two="B7", district="", block="B7",
             subblock=" ")
    assert a["street2"] == "B7", a
    # district as {"name": ...} (like city) is read.
    assert addr(address_line="L", district={"name": "D1"})["street2"] == "D1"
    # "Already contained" is by WHOLE word, not substring: building 123 is not inside
    # "...Rd 1234", block "2" is not inside building "1234" (both would be lost silently).
    assert addr(address_line="Fake Rd 1234", building_number="123")["street"] == "123 Fake Rd 1234"
    a = addr(building_number="1234", street_number="Fake St", block="2", subblock="34")
    assert a["street"] == "1234 Fake St" and a["street2"] == "2, 34", a
    assert addr(address_line="Fake Olaya St", district="Ola")["street2"] == "Ola"
    # Reviewer cases: digits inside a longer number are not "contained".
    assert addr(address_line="1234 Fake Rd", block="2")["street2"] == "2"
    assert addr(address_line="4123 Fake St", building_number="12")["street"] == "12 4123 Fake St"
    a = addr(address_line="1234 Fake St, Al Fake", building_number="1234", district="al fake")
    assert a["street"] == "1234 Fake St, Al Fake" and a["street2"] == "", a
    # Arabic text: the word boundary is Unicode-aware. The district word inside an
    # Arabic address_line is not repeated; a district that is only part of a word is kept.
    a = addr(address_line="شارع الملك فهد، حي العليا", district="العليا")
    assert a["street2"] == "", a
    a = addr(address_line="شارع العليا", district="علي")
    assert a["street2"] == "علي", a


def test_webhook_ship_to_keeps_only_address_keys():
    body = _real_webhook_body(_real_ship_to())
    st = mapping.webhook_ship_to(body)
    assert set(st) <= set(mapping.SHIP_TO_KEYS), sorted(set(st) - set(mapping.SHIP_TO_KEYS))
    assert st == {"type": "address", "name": "Fake Receiver", "city": "FakeCity",
                  "district": "Fake District", "address_line": "Fake Line 1",
                  "address_line_two": "Fake Line 2", "street_number": "Fake St",
                  "building_number": "1234", "block": "B7", "subblock": "S3",
                  "postal_code": "12345", "short_address": "abcd 1234"}, st
    for forbidden in ("email", "phone", "latitude", "longitude", "region", "country",
                      "country_code", "additional_number", "branch_id", "city_id",
                      "customer", "amounts", "items", "ship_from", "shipping"):
        assert forbidden not in st, forbidden
    stored = mapping.dump_ship_to(st)
    assert isinstance(stored, str)
    for value in _SHIP_FROM_VALUES + _NOT_ADDRESS_VALUES:
        assert value not in stored, value
    # Every value is a flat string (no nested dict/list ends up in the row).
    assert all(isinstance(v, str) for v in st.values())
    # Numbers and {"name": ...} are flattened; spaces collapsed; long values capped.
    st = mapping.webhook_ship_to(_real_webhook_body(_real_ship_to(
        postal_code=12345, building_number=1234, city={"id": 9, "name": " Fake  City "},
        address_line="x" * 5000, block=["B7"], subblock=True)))
    assert st["postal_code"] == "12345" and st["building_number"] == "1234"
    assert st["city"] == "Fake City"
    assert 0 < len(st["address_line"]) <= 256
    assert "block" not in st and "subblock" not in st      # list / bool dropped
    # The stored address gives the same Odoo address as the full webhook body.
    merged = mapping.with_ship_to({"id": 900002, "delivery_method": "shipping"},
                                  mapping.load_ship_to(mapping.dump_ship_to(
                                      mapping.webhook_ship_to(body))))
    assert mapping.shipping_address(merged) == mapping.shipping_address(body["data"])


def test_webhook_ship_to_empty_cases():
    good = _real_ship_to()
    for body in (None, [], "x", {}, {"event": "order.created"},
                 {"event": "order.created", "data": None},
                 {"event": "order.created", "data": "x"},
                 _real_webhook_body(good, delivery="pickup"),
                 _real_webhook_body(None),                               # ship_from only
                 _real_webhook_body({"type": "address", "name": "Fake Receiver"}),  # name only
                 _real_webhook_body({"type": "address", "latitude": "24.1",
                                     "longitude": "46.1", "country": "SA"})):
        assert mapping.webhook_ship_to(body) == {}, body
        assert mapping.dump_ship_to(mapping.webhook_ship_to(body)) is None
    # Only order.created / order.updated carry the order; any other event -> {}.
    for event, kept in (("order.created", True), ("order.updated", True),
                        ("order.status.updated", False), ("order.deleted", False),
                        ("app.store.authorize", False), ("", False), (None, False)):
        body = _real_webhook_body(good)
        body["event"] = event
        assert bool(mapping.webhook_ship_to(body)) is kept, event


def test_load_ship_to_never_raises():
    for value in (None, "", b"", "{", "[]", "null", "42", '"str"', 5, 4.2, [], {},
                  "[" * 100000, "{" * 100000, '{"city": {"a": 1}}', "\x00\xff",
                  b"\xff\xfe"):
        out = mapping.load_ship_to(value)
        assert out == {}, (repr(value)[:30], out)
    # Unknown keys dropped, containers dropped, values capped.
    out = mapping.load_ship_to(json.dumps({"city": "C", "email": "e@example.invalid",
                                           "phone": "+966500000009", "evil": "x",
                                           "district": {"name": "D"}, "block": ["B"],
                                           "address_line": "y" * 999}))
    assert out == {"city": "C", "address_line": "y" * 256}, out
    assert mapping.load_ship_to(b'{"city": "C"}') == {"city": "C"}


def test_with_ship_to_merge_rules():
    st = mapping.webhook_ship_to(_real_webhook_body(_real_ship_to()))
    # Light API detail: no shipments -> the webhook's address is merged, as a NEW dict.
    detail = {"id": 900002, "delivery_method": "shipping", "customer": {"id": 7700002}}
    before = json.dumps(detail, sort_keys=True)
    merged = mapping.with_ship_to(detail, st)
    assert merged is not detail and json.dumps(detail, sort_keys=True) == before
    assert mapping.shipping_address(merged)["city"] == "FakeCity"
    assert mapping.buyer(merged)["address_missing"] is False
    # The detail as the API sends it, without the merge: alert (shipping, no address).
    assert mapping.buyer(detail)["address_missing"] is True
    # API brought its own ship_to back: the API's wins, the stored one is ignored.
    api = dict(detail, shipments=[{"ship_to": {"type": "address", "city": "ApiCity",
                                               "postal_code": "54321"}}])
    assert mapping.shipping_address(mapping.with_ship_to(api, st))["city"] == "ApiCity"
    # Pickup detail: untouched even with a stored address.
    pick = dict(detail, delivery_method="pickup")
    assert mapping.with_ship_to(pick, st) is pick
    assert mapping.shipping_address(mapping.with_ship_to(pick, st)) == {}
    # Nothing stored: unchanged (and buyer() then alerts for a shipping order).
    for empty in ({}, None):
        assert mapping.with_ship_to(detail, empty) is detail
    # Garbage detail is returned as is, never raises.
    for garbage in (None, [], "x"):
        assert mapping.with_ship_to(garbage, st) == garbage
    # A branch-type ship_to in the detail does not block the buyer's stored one.
    branchy = dict(detail, shipments=[{"ship_to": _real_ship_from(),
                                       "ship_from": _real_ship_from()}])
    a = mapping.shipping_address(mapping.with_ship_to(branchy, st))
    assert a["city"] == "FakeCity", a


def test_redact_real_webhook_body():
    """log_payloads dump of the real body shape: no address/contact value in clear."""
    body = _real_webhook_body(_real_ship_to())
    out = mapping.redact(body)
    dump = json.dumps(out, ensure_ascii=False)
    for value in ("Fake Receiver", "r@example.invalid", "+966500000009", "Fake Region",
                  "FakeCity", "Fake District", "Fake Line 1", "Fake Line 2", "Fake St",
                  "B7", "S3", "abcd 1234", "1234", "6789", "12345", "24.0000001",
                  "46.0000001", "Fake Buyer", "fake.buyer@example.invalid", "500000001",
                  "CustomerCity", "Fake Location") + _SHIP_FROM_VALUES:
        assert value not in dump, value
    st = out["data"]["shipments"][0]["ship_to"]
    for key in ("name", "email", "phone", "city", "district", "address_line",
                "address_line_two", "street_number", "block", "subblock", "short_address",
                "building_number", "additional_number", "postal_code", "latitude",
                "longitude"):
        assert st[key] == "***", key
    assert st["region"] == "***" or st["region"]["name"] == "***"
    # Field names stay readable (that is what the dump is for).
    assert st["type"] == "address" and out["data"]["delivery_method"] == "shipping"
    assert out["data"]["amounts"]["total"]["amount"] == 34.5


# ================================================================== F3: status Odoo -> Salla
# Pure decisions only. Picking states are Odoo's stock.picking.state values; the caller
# passes the OUTGOING pickings only (returns filtered out). Fake order ids 9000003xx.

_AHEAD = ("shipped", "delivering", "delivered", "completed", "closed")
_CANCELS = ("canceled", "cancelled", "restored", "restoring", "refunded")
_BEFORE = ("under_review", "in_progress", "processing", "ready_for_pickup", "", None,
           "fake_custom_status")


def test_order_cancelled_is_queued():
    for ev in ("order.created", "order.updated", "order.status.updated",
               "order.cancelled", "order.refunded"):
        assert ev in mapping.ORDER_EVENTS, ev
    # a cancel webhook never feeds the address reader (its body is not trusted)
    assert "order.cancelled" not in mapping.ADDRESS_EVENTS
    assert "order.refunded" not in mapping.ADDRESS_EVENTS
    # the dedup key of a cancel webhook is the same order id as the create
    for body in ({"event": "order.cancelled", "merchant": 1000000001, "data": {"id": 900000301}},
                 {"event": "order.cancelled", "data": {"id": "900000301",
                                                       "status": {"slug": "canceled"}}},
                 {"event": "order.refunded", "data": {"id": 900000301}}):
        assert mapping.webhook_order_id(body) == "900000301", body
    assert mapping.event_name({"event": "order.cancelled"}) in mapping.ORDER_EVENTS


def test_cancel_refund_webhook_order_ref_first():
    """order.cancelled / order.refunded (shape unverified): data may be the refund or the
    status change, whose own id is NOT the order id -> the order reference wins."""
    assert mapping.ORDER_REF_FIRST_EVENTS == {"order.status.updated", "order.cancelled",
                                              "order.refunded"}
    for ev in ("order.cancelled", "order.refunded", "order.status.updated"):
        for data in ({"id": 555, "order_id": 900000301},
                     {"id": 555, "order": {"id": 900000301}},
                     {"id": 555, "order": {"id": "900000301"}, "order_id": 777},
                     {"id": 900000301},                       # bare order id: still read
                     {"id": 555, "order_id": None, "order": {"id": 900000301}},
                     {"id": 555, "order_id": "None", "order": {"id": 900000301}}):
            got = mapping.webhook_order_id({"event": ev, "merchant": 1000000001, "data": data})
            assert got == "900000301", (ev, data, got)
    # order.created / updated: data IS the order, its own id wins over any nested ref
    for ev in ("order.created", "order.updated"):
        got = mapping.webhook_order_id({"event": ev, "data": {"id": 900000301,
                                                               "order_id": 555}})
        assert got == "900000301", (ev, got)
    # nothing usable -> "" (never "None", never the refund id via a literal None ref)
    for data in ({}, {"order": None}, {"order_id": ""}, {"order": {"id": None}}):
        assert mapping.webhook_order_id({"event": "order.refunded", "data": data}) == "", data


def test_ship_push_single_delivery_done():
    for st in _BEFORE:
        assert mapping.ship_push_decision("sale", st, "", ["done"]) == "push", st
        assert mapping.ship_push_decision("sale", st, None, ["done"]) == "push", st
        assert mapping.ship_push_decision("sale", st, False, ["done"]) == "push", st
    # a cancelled extra delivery next to a done one still pushes
    assert mapping.ship_push_decision("sale", "in_progress", "", ["cancel", "done"]) == "push"
    assert mapping.ship_push_decision("sale", "in_progress", "", ["done", "done", "cancel"]) == "push"


def test_ship_push_backorder_waits_until_last():
    # 3 x P1 split: 2 shipped (done), 1 in a backorder -> wait for every open state
    for open_state in ("draft", "waiting", "confirmed", "assigned"):
        assert mapping.ship_push_decision("sale", "in_progress", "", ["done", open_state]) \
            == "wait", open_state
        assert mapping.ship_push_decision("sale", "in_progress", "", [open_state, "done"]) \
            == "wait", open_state
        assert mapping.ship_push_decision("sale", "in_progress", "", [open_state]) == "wait"
    # then the backorder is validated -> push (once)
    assert mapping.ship_push_decision("sale", "in_progress", "", ["done", "done"]) == "push"
    assert mapping.ship_push_decision("sale", "in_progress", "shipped", ["done", "done"]) == "skip"


def test_ship_push_nothing_done_waits():
    # no delivery yet, or every delivery cancelled: nothing has shipped, never "shipped"
    for states in ([], None, ["cancel"], ["cancel", "cancel"]):
        assert mapping.ship_push_decision("sale", "in_progress", "", states) == "wait", states


def test_ship_push_already_pushed_is_remembered():
    # remembered by what WE sent, not by the previous Salla status: even if a stale /
    # out-of-order webhook put salla_status back to in_progress, never push twice
    for st in _BEFORE:
        assert mapping.ship_push_decision("sale", st, "shipped", ["done"]) == "skip", st
        assert mapping.ship_push_decision("sale", st, " Shipped ", ["done"]) == "skip", st
    # an unrelated pushed value does not count as "already shipped"
    assert mapping.ship_push_decision("sale", "in_progress", "in_progress", ["done"]) == "push"


def test_ship_push_salla_already_ahead_or_cancelled():
    for st in _AHEAD + _CANCELS:
        for variant in (st, st.upper(), " %s " % st):
            assert mapping.ship_push_decision("sale", variant, "", ["done"]) == "skip", variant
    # never revive an order Salla does not consider paid/alive
    for st in ("payment_pending", "request_quote", "deleted"):
        assert mapping.ship_push_decision("sale", st, "", ["done"]) == "skip", st
    # "under review" spelled with a space / dash is still before shipped
    for st in ("Under Review", "under-review", "In Progress"):
        assert mapping.ship_push_decision("sale", st, "", ["done"]) == "push", st
    assert mapping.SHIPPED_STATUS == "shipped"
    assert set(_AHEAD) == mapping.SHIPPED_OR_LATER_STATUSES


def test_ship_push_only_confirmed_orders():
    for state in ("draft", "sent", "cancel", "", None, "done"):
        assert mapping.ship_push_decision(state, "in_progress", "", ["done"]) == "skip", state


def test_ship_push_garbage_never_raises():
    for states in ("done", ["DONE"], [None], [""], [{"state": "done"}], (), set()):
        out = mapping.ship_push_decision("sale", "in_progress", "", states)
        assert out in ("push", "wait", "skip"), (states, out)
    # a bare string is not a list of states: it must not count as ["d","o","n","e"] -> push
    assert mapping.ship_push_decision("sale", "in_progress", "", "done") == "wait"
    # malformed entries are never read as "done"
    assert mapping.ship_push_decision("sale", "in_progress", "", [{"state": "done"}]) == "wait"
    assert mapping.ship_push_decision("sale", "in_progress", "", [None, "done"]) == "wait"


def test_status_payload():
    assert mapping.status_payload("shipped", True) == {"slug": "shipped", "send_status_sms": True}
    assert mapping.status_payload(" Shipped ", False) == {"slug": "shipped",
                                                           "send_status_sms": False}
    # Odoo Boolean fields come back as False/True; None (unset) -> False, never "None"
    assert mapping.status_payload("shipped", None)["send_status_sms"] is False
    assert mapping.status_payload("shipped", 1)["send_status_sms"] is True
    body = json.dumps(mapping.status_payload("shipped", True))
    assert body == '{"slug": "shipped", "send_status_sms": true}', body


def test_is_permanent_error():
    for code in (400, 401, 403, 404, 405, 409, 422, "422", " 404 "):
        assert mapping.is_permanent_error(code), code
    for code in (408, 425, 429, 500, 502, 503, 504, None, "", "abc", 0, 200, 201, 302):
        assert not mapping.is_permanent_error(code), code


def test_push_next_try_bounded():
    now = NOW
    assert mapping.PUSH_MAX_ATTEMPTS == len(mapping.PUSH_RETRY_MINUTES) + 1
    assert mapping.push_next_try(1, now) == now + timedelta(minutes=mapping.PUSH_RETRY_MINUTES[0])
    assert mapping.push_next_try(2, now) == now + timedelta(minutes=mapping.PUSH_RETRY_MINUTES[1])
    last = mapping.PUSH_MAX_ATTEMPTS - 1
    assert mapping.push_next_try(last, now) == \
        now + timedelta(minutes=mapping.PUSH_RETRY_MINUTES[-1])
    # delays grow, and the walk ends (None = give up + alert once)
    tries, attempts = [], 1
    while True:
        nxt = mapping.push_next_try(attempts, now)
        if nxt is None:
            break
        tries.append(nxt)
        attempts += 1
        assert attempts < 50, "push retries never end"
    assert tries == sorted(tries) and len(tries) == len(mapping.PUSH_RETRY_MINUTES)
    assert attempts == mapping.PUSH_MAX_ATTEMPTS
    for attempts in (mapping.PUSH_MAX_ATTEMPTS, mapping.PUSH_MAX_ATTEMPTS + 5, 0, -1, None):
        assert mapping.push_next_try(attempts, now) is None, attempts
    # permanent error (422 invalid transition): give up at once, even on the 1st failure
    assert mapping.push_next_try(1, now, permanent=True) is None


def test_cancel_action_not_shipped_cancels():
    for states in ([], None, ["assigned"], ["confirmed", "waiting"], ["draft"], ["cancel"],
                   ["assigned", "cancel"]):
        for order_state in ("sale", "draft", "sent"):
            assert mapping.cancel_action(order_state, states, False) == "cancel", \
                (order_state, states)


def test_cancel_action_shipped_alerts_only():
    # one delivery done is enough (partial shipment + open backorder): goods left
    for states in (["done"], ["done", "assigned"], ["assigned", "done"], ["cancel", "done"]):
        assert mapping.cancel_action("sale", states, False) == "alert_shipped", states
        # shipped wins over invoiced: the return is what Ops must handle
        assert mapping.cancel_action("sale", states, True) == "alert_shipped", states


def test_cancel_action_posted_invoice_alerts():
    for states in ([], ["assigned"], ["cancel"]):
        assert mapping.cancel_action("sale", states, True) == "alert_invoiced", states


def test_cancel_action_idempotent():
    # repeated / out-of-order webhook: handled before -> nothing, whatever the pickings
    for states in ([], ["done"], ["assigned"]):
        for inv in (False, True):
            assert mapping.cancel_action("sale", states, inv, already_handled=True) == "none"
            # already cancelled in Odoo (by hand or by the first webhook)
            assert mapping.cancel_action("cancel", states, inv) == "none"
    # after the auto-cancel, a replay sees state "cancel" + cancelled pickings -> none
    assert mapping.cancel_action("cancel", ["cancel"], False) == "none"


# --- F5: invoices (proposal C), payments, credit notes -----------------------

_FAKE_TOKEN = "fake-access-token-0000-test"   # obviously fake account.move access token


def test_payment_journal_key_salla_payments():
    for slug in ("mada", "MADA", " mada ", "credit_card", "Credit Card", "credit-card",
                 "apple_pay", "Apple Pay", "apple-pay", "applepay", "visa", "mastercard",
                 "bank_card", "debit_card"):
        assert mapping.payment_journal_key(slug) == "salla_payments", slug
    assert set(mapping.PAYMENT_JOURNAL_KEYS) == {"salla_payments", "tabby", "tamara"}


def test_payment_journal_key_bnpl():
    for slug in ("tabby", "tabby_installment", "Tabby Installment", "TABBY-INSTALLMENT",
                 "tabby_pay_later", "tabby_card"):
        # "tabby_card" contains "card": it must still be Tabby, never Salla Payments
        assert mapping.payment_journal_key(slug) == "tabby", slug
    for slug in ("tamara", "tamara_installment", "Tamara-Pay-Later", " TAMARA "):
        assert mapping.payment_journal_key(slug) == "tamara", slug


def test_payment_journal_key_unknown_no_payment():
    # unknown / empty / COD / bank transfer -> "" (no payment + alert, never a guess)
    for slug in ("", None, False, 0, [], {}, "stc_pay", "myfatoorah", "paypal",
                 "cod", "cash_on_delivery", "bank", "bank_transfer", "wire",
                 "تحويل بنكي", "None", "card_tabby_transfer"):
        assert mapping.payment_journal_key(slug) == "", slug
    # "bank_card" is a card, not a transfer (F2a rule kept)
    assert mapping.payment_journal_key("bank_card") == "salla_payments"


def test_payment_journal_key_overrides():
    over, bad = mapping.parse_method_overrides("stc_pay = salla_payments\nX:tabby, bad")
    assert over == {"stc_pay": "salla_payments", "x": "tabby"}, over
    assert bad == ["bad"], bad
    assert mapping.payment_journal_key("stc_pay", over) == "salla_payments"
    assert mapping.payment_journal_key("STC Pay", {"stc_pay": "tamara"}) == "tamara"
    # the override wins over the built-in list (Accounting decided, e.g. a real slug)
    assert mapping.payment_journal_key("mada", {"mada": "tabby"}) == "tabby"
    # ...but never for COD or a bank transfer (unverified money): still no payment
    for slug, key in (("bank_transfer", "salla_payments"), ("cod", "tabby"),
                      ("bank", "tamara"), ("cash_on_delivery", "salla_payments"),
                      ("wire_transfer", "salla_payments")):
        assert mapping.payment_journal_key(slug, {slug: key}) == "", slug
    over, _ = mapping.parse_method_overrides("bank_transfer = salla_payments\ncod = tabby")
    assert mapping.payment_journal_key("Bank Transfer", over) == ""
    assert mapping.payment_journal_key("COD", over) == ""
    # an override to something that is not a journal key is ignored
    assert mapping.payment_journal_key("mada", {"mada": "cash"}) == "salla_payments"
    assert mapping.payment_journal_key("stc_pay", {"stc_pay": "cash"}) == ""
    # no / garbage overrides never raise
    for o in (None, {}, {"": "tabby"}):
        assert mapping.payment_journal_key("tabby_installment", o) == "tabby"


def test_parse_method_overrides_tolerant():
    for empty in (None, "", "   ", "\n\n", ",;,"):
        assert mapping.parse_method_overrides(empty) == ({}, []), empty
    over, bad = mapping.parse_method_overrides(
        "Apple-Pay = Salla Payments; tabby_new : TABBY\n a=b=c \n=tabby\nfoo: cash\nnosep")
    assert over == {"apple_pay": "salla_payments", "tabby_new": "tabby"}, over
    assert bad == ["a=b=c", "=tabby", "foo: cash", "nosep"], bad
    _, bad = mapping.parse_method_overrides("x" * 200)
    assert bad and all(len(b) <= 60 for b in bad), bad
    # every key the parser accepts is a journal key
    over, _ = mapping.parse_method_overrides("a=tamara\nb=salla_payments\nc=tabby")
    assert set(over.values()) <= set(mapping.PAYMENT_JOURNAL_KEYS)


def test_should_auto_invoice():
    ok = dict(order_state="sale", total_mismatch=False, invoice_status="to invoice",
              has_invoice=False)
    assert mapping.should_auto_invoice(**ok) is True
    assert mapping.should_auto_invoice(**ok, auto_invoice=True, failed_before=False) is True
    # each guard alone blocks it
    for state in ("draft", "sent", "cancel", "", None):
        assert not mapping.should_auto_invoice(**dict(ok, order_state=state)), state
    assert not mapping.should_auto_invoice(**dict(ok, total_mismatch=True))
    for st in ("invoiced", "no", "upselling", "", None, "To Invoice"):
        assert not mapping.should_auto_invoice(**dict(ok, invoice_status=st)), st
    assert not mapping.should_auto_invoice(**dict(ok, has_invoice=True))   # draft too
    assert not mapping.should_auto_invoice(**ok, auto_invoice=False)
    assert not mapping.should_auto_invoice(**ok, failed_before=True)


def test_upload_kind():
    assert mapping.upload_kind("out_invoice") == mapping.TAX_INVOICE == "tax_invoice"
    assert mapping.upload_kind("out_refund") == mapping.CREDIT_NOTE == "credit_note"
    for t in ("in_invoice", "in_refund", "entry", "out_receipt", "", None, False):
        assert mapping.upload_kind(t) == "", t


def test_upload_decision_invoice_zatca_required():
    d = mapping.upload_decision
    assert d("out_invoice", "posted", "sent", True) == "upload"
    assert d("out_invoice", "posted", "to_send", True) == "wait"
    assert d("out_invoice", "posted", "to_send", True, edi_error=True) == "zatca_error"
    for edi in (False, None, "", "nonsense"):
        assert d("out_invoice", "posted", edi, True) == "no_zatca", edi
    for edi in ("cancelled", "to_cancel"):
        assert d("out_invoice", "posted", edi, True) == "skip", edi


def test_upload_decision_zatca_off_staging():
    # staging: "Require ZATCA acceptance" off -> EDI state ignored
    for edi in ("sent", "to_send", False, None, ""):
        assert mapping.upload_decision("out_invoice", "posted", edi, False) == "upload", edi
        assert mapping.upload_decision("out_invoice", "posted", edi, False,
                                       edi_error=True) == "upload", edi


def test_upload_decision_never_twice_never_draft():
    d = mapping.upload_decision
    for req in (True, False):
        # already uploaded: never again (Salla accepts a 2nd tax invoice for one order)
        for sid in ("1644645772", 1644645772, " 1644645772 "):
            assert d("out_invoice", "posted", "sent", req, salla_invoice_id=sid) == "skip"
            assert d("out_refund", "posted", "sent", req, salla_invoice_id=sid) == "skip"
        # empty / literal None id = not uploaded yet
        for sid in ("", None, False, "None", "null"):
            assert d("out_invoice", "posted", "sent", req, salla_invoice_id=sid) == "upload"
        for state in ("draft", "cancel", "", None):
            assert d("out_invoice", state, "sent", req) == "skip", state
            assert d("out_refund", state, "sent", req) == "skip", state
        for mt in ("in_invoice", "in_refund", "entry", ""):
            assert d(mt, "posted", "sent", req) == "skip", mt


def test_upload_decision_credit_note_follows_invoice():
    d = mapping.upload_decision
    for req in (True, False):
        assert d("out_refund", "posted", "sent", req, origin_state="done") == "upload"
        assert d("out_refund", "posted", "sent", req, origin_state="pending") == "wait"
        # invoice upload gave up: never a silent wait, the caller alerts Accounting once
        assert d("out_refund", "posted", "sent", req, origin_state="failed") == "origin_failed"
        # credit note of an invoice that was never a Salla upload -> skip
        for origin in ("", None, False, "skipped", "nonsense"):
            assert d("out_refund", "posted", "sent", req, origin_state=origin) == "skip"
    # ZATCA still gates the credit note itself first
    assert d("out_refund", "posted", "to_send", True, origin_state="done") == "wait"
    assert d("out_refund", "posted", False, True, origin_state="done") == "no_zatca"
    # origin_state is ignored for tax invoices
    assert d("out_invoice", "posted", "sent", True, origin_state="") == "upload"
    assert d("out_invoice", "posted", "sent", True, origin_state="failed") == "upload"


def test_upload_decision_cancelled_before_upload():
    d = mapping.upload_decision
    # order cancelled in Salla before its invoice went up: the invoice is never uploaded
    # (it is credited at once), whatever ZATCA says; checked before any ZATCA state
    for req in (True, False):
        for edi in ("sent", "to_send", False, None, "cancelled"):
            assert d("out_invoice", "posted", edi, req, cancelled_before_upload=True) \
                == "skip", (req, edi)
            assert d("out_invoice", "posted", edi, req, edi_error=True,
                     cancelled_before_upload=True) == "skip", (req, edi)
    # no effect on credit notes: the credit note still follows its invoice
    assert d("out_refund", "posted", "sent", True, origin_state="done",
             cancelled_before_upload=True) == "upload"
    assert d("out_refund", "posted", "sent", True, origin_state="skipped",
             cancelled_before_upload=True) == "skip"
    # default off: unchanged
    assert d("out_invoice", "posted", "sent", True) == "upload"
    # the full set of answers callers must handle
    seen = set()
    for mt in ("out_invoice", "out_refund", "entry"):
        for st in ("posted", "draft"):
            for edi in ("sent", "to_send", False, "cancelled"):
                for req in (True, False):
                    for err in (True, False):
                        for origin in ("done", "pending", "failed", "skipped"):
                            for cbu in (True, False):
                                seen.add(d(mt, st, edi, req, edi_error=err,
                                           origin_state=origin,
                                           cancelled_before_upload=cbu))
    assert seen == {"upload", "wait", "skip", "no_zatca", "zatca_error",
                    "origin_failed"}, seen


def test_status_before_upload_never_backwards():
    for st in ("under_review", "Under Review", "under-review", " UNDER_REVIEW "):
        assert mapping.status_before_upload("tax_invoice", st) == "in_progress", st
        assert mapping.status_before_upload("credit_note", st) == "", st
    assert mapping.IN_PROGRESS_STATUS == "in_progress"
    for st in _AHEAD + _CANCELS + ("in_progress", "processing", "payment_pending",
                                    "request_quote", "deleted", "", None):
        assert mapping.status_before_upload("tax_invoice", st) == "", st
    assert mapping.status_before_upload("", "under_review") == ""


def test_pdf_filename_safe():
    assert mapping.pdf_filename("INV/2026/00012") == "INV_2026_00012.pdf"
    assert mapping.pdf_filename("RINV/2026/00003") == "RINV_2026_00003.pdf"
    for name in ("", None, False, "/", "///", "فاتورة"):
        assert mapping.pdf_filename(name) == "invoice.pdf", name
    for name in ("../../etc/passwd", "a b?c#d%2F..", "INV/2026/00012.pdf",
                 "x" * 300, "inv\\..\\x", "INV/2026/0001\n2"):
        out = mapping.pdf_filename(name)
        assert out.endswith(".pdf") and out.count(".") == 1, (name, out)
        assert "/" not in out and "\\" not in out and ".." not in out, (name, out)
        assert len(out) <= 84 and not out.startswith("_"), (name, out)
    assert mapping.pdf_filename("../../etc/passwd") == "etc_passwd.pdf"


def test_public_base_url():
    f = mapping.public_base_url
    assert f("https://ksasystem.wayakit.com/sl/webhook") == "https://ksasystem.wayakit.com"
    assert f("https://Fake-Maintest-123.dev.odoo.com/sl/webhook?x=1") == \
        "https://fake-maintest-123.dev.odoo.com"
    assert f("https://example.test:8443/sl/webhook") == "https://example.test:8443"
    # webhook unusable -> fallback (web.base.url)
    for bad in ("", None, "http://example.test/sl/webhook", "ftp://example.test",
                "https://", "https://user:pw@example.test/sl/webhook", "https://[bad/sl",
                "/sl/webhook", "example.test"):
        assert f(bad, "https://fallback.example.test") == "https://fallback.example.test", bad
        assert f(bad) == "", bad
        assert f(bad, "http://fallback.example.test") == "", bad
    # a good webhook URL wins over the fallback
    assert f("https://a.example.test/sl/webhook", "https://b.example.test") == \
        "https://a.example.test"


def test_invoice_pdf_url():
    url = mapping.invoice_pdf_url("https://fake.example.test/", 42, _FAKE_TOKEN,
                                  "INV/2026/00012")
    assert url == "https://fake.example.test/sl/invoice/42/%s/INV_2026_00012.pdf" \
        % _FAKE_TOKEN, url
    assert mapping.INVOICE_PDF_ROUTE == "/sl/invoice"
    # path must not name the marketplace (repo rule 6) and must end in .pdf (Salla checks)
    assert "salla" not in url.lower() and url.endswith(".pdf")
    assert mapping.invoice_pdf_url("https://h.test", "42", _FAKE_TOKEN, "") == \
        "https://h.test/sl/invoice/42/%s/invoice.pdf" % _FAKE_TOKEN
    for base, mid, tok in (("", 42, _FAKE_TOKEN), (None, 42, _FAKE_TOKEN),
                           ("https://h.test", 0, _FAKE_TOKEN), ("https://h.test", None,
                                                                 _FAKE_TOKEN),
                           ("https://h.test", 42, ""), ("https://h.test", 42, None),
                           ("https://h.test", 42, False)):
        assert mapping.invoice_pdf_url(base, mid, tok, "INV/1") == "", (base, mid, tok)


def test_invoice_upload_payload():
    url = "https://fake.example.test/sl/invoice/42/%s/INV_2026_00012.pdf" % _FAKE_TOKEN
    for oid in (504361123, "504361123", " 504361123 "):
        body = mapping.invoice_upload_payload(oid, "tax_invoice", url)
        assert body == {"order_id": 504361123, "type": "tax_invoice", "data_type": "url",
                        "url": url}, body
    body = mapping.invoice_upload_payload("504361123", "credit_note", url)
    assert json.dumps(body) == ('{"order_id": 504361123, "type": "credit_note", '
                                '"data_type": "url", "url": "%s"}' % url)


def test_uploaded_invoice_id():
    real = {"status": 200, "success": True,
            "data": {"id": 1644645772, "invoice_number": None, "uuid": "fake-uuid",
                     "type": "tax_invoice", "slug": 1, "date": "2026-10-22",
                     "url": "https://cdn.example.test/fake.pdf"}}
    assert mapping.uploaded_invoice_id(real) == "1644645772"
    for resp in (None, [], "", {}, {"data": None}, {"data": []}, {"data": {}},
                 {"data": {"id": None}}, {"data": {"id": "None"}}, {"id": 5},
                 {"data": [{"id": 5}]}):
        assert mapping.uploaded_invoice_id(resp) == "", resp


def test_salla_invoice_kind():
    k = mapping.salla_invoice_kind
    assert k({"slug": 1}) == k({"slug": "1"}) == "tax_invoice"
    assert k({"slug": 2}) == k({"slug": " 2 "}) == "credit_note"
    assert k({"type": "Credit Note"}) == k({"type": "credit_note"}) == "credit_note"
    assert k({"type": "tax_invoice"}) == k({"type": "Invoice"}) == "tax_invoice"
    # slug wins over a contradicting label
    assert k({"slug": 2, "type": "tax_invoice"}) == "credit_note"
    for e in (None, [], "1", {"slug": 3}, {"type": "فاتورة"}, {}, {"slug": True}):
        assert k(e) == "", e


def test_adopt_uploaded_invoice():
    # Pure pick only. The CALLER must adopt only for a move that already attempted a
    # POST (the POST succeeded, the commit did not): otherwise two credit notes of one
    # order could adopt each other's Salla id. That guard is checked on staging (R).
    listing = [
        {"id": 4, "invoice_number": 4, "slug": 1},          # Salla's own (store in Salla mode)
        {"id": 5, "invoice_number": "5", "slug": 1},
        {"id": 1644645772, "invoice_number": None, "slug": 1},
        {"id": 1644645990, "invoice_number": "", "slug": 1},
        {"id": 1644646100, "invoice_number": None, "slug": 2},
        None, "garbage", {"slug": 1}, {"id": None, "slug": 1},
    ]
    a = mapping.adopt_uploaded_invoice
    assert a(listing, "tax_invoice", []) == "1644645990"          # newest of ours
    assert a(listing, "tax_invoice", ["1644645990"]) == "1644645772"
    assert a(listing, "tax_invoice", [1644645990, "1644645772"]) == ""
    assert a(listing, "credit_note", None) == "1644646100"
    assert a(listing, "credit_note", ["1644646100"]) == ""
    # Salla's own numbered invoices are never adopted
    assert a(listing[:2], "tax_invoice", []) == ""
    for lst in (None, [], [None], "x"):
        assert a(lst, "tax_invoice", []) == "", lst
    # numeric ordering, not string ordering
    assert a([{"id": "99", "slug": 1}, {"id": "100", "slug": 1}], "tax_invoice", []) == "100"


def test_invoice_total_check_tolerance_zero():
    # F5 posts the invoice / registers the payment only when amount_total == Salla's
    # total (total_check with tolerance 0). 3 x 9.99 incl. VAT with a 20 % coupon: float
    # noise must not leave a correct invoice in draft, a 0.01 cent gap must.
    net_unit = round(9.99 / 1.15, 7)
    odoo_total = 3 * net_unit * 1.15 * 0.8            # 23.975999999... in float
    assert mapping.total_check(odoo_total, 23.98, 0) == ("ok", 0.0)
    assert mapping.total_check(29.969999999999995, 29.97, 0) == ("ok", 0.0)
    for odoo, salla in ((23.97, 23.98), (23.99, 23.98), (0.0, 23.98)):
        verdict, diff = mapping.total_check(odoo, salla, 0)
        assert verdict == "mismatch" and diff != 0, (odoo, salla, verdict)
    # Salla total unknown -> never "ok" (left in draft + alert)
    assert mapping.total_check(23.98, None, 0)[0] == "unknown"


def test_cancel_action_credit_note_when_auto_invoicing():
    # F5: posted invoice + nothing shipped -> full credit note (when the backend invoices)
    for states in ([], None, ["assigned"], ["confirmed", "waiting"], ["cancel"],
                   ["assigned", "cancel"]):
        assert mapping.cancel_action("sale", states, True, auto_credit_note=True) \
            == "credit_note", states
        # no posted invoice -> plain cancel, as in F3
        assert mapping.cancel_action("sale", states, False, auto_credit_note=True) \
            == "cancel", states
    # shipped still wins: Ops handles the return, Accounting the credit note by hand
    for states in (["done"], ["done", "assigned"], ["cancel", "done"]):
        assert mapping.cancel_action("sale", states, True, auto_credit_note=True) \
            == "alert_shipped", states
    # repeated webhook after the credit note + cancel: nothing again
    for inv in (False, True):
        assert mapping.cancel_action("cancel", ["cancel"], inv, auto_credit_note=True) == "none"
        assert mapping.cancel_action("sale", [], inv, already_handled=True,
                                     auto_credit_note=True) == "none"
    # already fully credited (caller passes has_posted_invoice=False) -> cancel only
    assert mapping.cancel_action("sale", ["assigned"], False, auto_credit_note=True) == "cancel"


# ============================================================================ F8
# Bulk product upload (spec claude-docs/14-product-upload.md §3-§4). Every handle, SKU,
# barcode, category id and piece of copy below is obviously fake. No ingredient names,
# no costs.

# --- tiny .xlsx files built with zipfile only (no openpyxl locally) -------------------

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
_XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'


def _xml_text(value):
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _xlsx(sheets, shared=(), targets=None, absolute=False):
    """A minimal but complete .xlsx. sheets: [(name, <sheetData> inner XML)] in
    workbook order; targets: file number of each sheet (default 1, 2, ...), so a test
    can store the first sheet in sheet2.xml as Excel does after reordering sheets;
    absolute: relationship targets as "/xl/worksheets/..." (openpyxl style) instead of
    "worksheets/..." (Excel / xlsxwriter style); shared: raw <si> XML strings."""
    targets = targets or list(range(1, len(sheets) + 1))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        overrides = ['<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
                     'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>']
        overrides += ['<Override PartName="/xl/worksheets/sheet%d.xml" ContentType='
                      '"application/vnd.openxmlformats-officedocument.spreadsheetml.'
                      'worksheet+xml"/>' % n for n in targets]
        if shared:
            overrides.append('<Override PartName="/xl/sharedStrings.xml" ContentType='
                             '"application/vnd.openxmlformats-officedocument.'
                             'spreadsheetml.sharedStrings+xml"/>')
        z.writestr("[Content_Types].xml", _XML_HEAD +
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-'
                   'types"><Default Extension="rels" ContentType="application/vnd.'
                   'openxmlformats-package.relationships+xml"/><Default Extension="xml" '
                   'ContentType="application/xml"/>%s</Types>' % "".join(overrides))
        z.writestr("_rels/.rels", _XML_HEAD +
                   '<Relationships xmlns="%s"><Relationship Id="rId1" Type="%s/'
                   'officeDocument" Target="xl/workbook.xml"/></Relationships>'
                   % (_NS_PKG, _NS_REL))
        z.writestr("xl/workbook.xml", _XML_HEAD +
                   '<workbook xmlns="%s" xmlns:r="%s"><sheets>%s</sheets></workbook>' % (
                       _NS_MAIN, _NS_REL, "".join(
                           '<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (
                               name, i + 1, targets[i] + 10)
                           for i, (name, _) in enumerate(sheets))))
        prefix = "/xl/worksheets/" if absolute else "worksheets/"
        rels = ['<Relationship Id="rId%d" Type="%s/worksheet" Target="%ssheet%d.xml"/>'
                % (n + 10, _NS_REL, prefix, n) for n in targets]
        if shared:
            rels.append('<Relationship Id="rId1" Type="%s/sharedStrings" Target="%s"/>' % (
                _NS_REL, "/xl/sharedStrings.xml" if absolute else "sharedStrings.xml"))
        z.writestr("xl/_rels/workbook.xml.rels", _XML_HEAD +
                   '<Relationships xmlns="%s">%s</Relationships>' % (_NS_PKG, "".join(rels)))
        for n, (_, data) in zip(targets, sheets):
            z.writestr("xl/worksheets/sheet%d.xml" % n, _XML_HEAD +
                       '<worksheet xmlns="%s" xmlns:r="%s"><sheetData>%s</sheetData>'
                       '</worksheet>' % (_NS_MAIN, _NS_REL, data))
        if shared:
            z.writestr("xl/sharedStrings.xml", _XML_HEAD +
                       '<sst xmlns="%s" count="%d" uniqueCount="%d">%s</sst>' % (
                           _NS_MAIN, len(shared), len(shared), "".join(shared)))
    return buf.getvalue()


def _c_s(ref, idx):          # shared string cell
    return '<c r="%s" t="s"><v>%d</v></c>' % (ref, idx)


def _c_in(ref, value):       # inline string cell
    return '<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (
        ref, _xml_text(value))


def _c_n(ref, raw):          # number cell, raw = the text Excel stores in <v>
    return '<c r="%s"><v>%s</v></c>' % (ref, raw)


def _xrow(n, *cells):
    return '<row r="%d">%s</row>' % (n, "".join(cells))


def _si(value):
    return '<si><t xml:space="preserve">%s</t></si>' % _xml_text(value)


def _si_runs(*parts):        # rich text: one <r> run per part, each with formatting
    return "<si>%s</si>" % "".join(
        '<r><rPr><b/><sz val="11"/></rPr><t xml:space="preserve">%s</t></r>' % _xml_text(p)
        for p in parts)


def _simple_sheet(sku="FAKE-XL-001", handle="fake-xl"):
    return (_xrow(1, _c_in("A1", "handle"), _c_in("B1", "sku"), _c_in("C1", "name_ar")) +
            _xrow(2, _c_in("A2", handle), _c_in("B2", sku), _c_in("C2", "اسم تجريبي")))


def _by_sku(rows):
    return {r.get("sku"): r for r in rows if r.get("sku")}


def _csv_bytes():
    return ("handle,sku,name_ar,description_ar,price\n"
            "fake-csv,FAKE-CSV-001,منظف تجريبي,وصف تجريبي,25\n").encode("utf-8")


# --- parse_upload_rows fixtures ---------------------------------------------------------

def _r(n, **values):
    row = {"_row": n}
    row.update(values)
    return row


def _simple_rows(**over):
    """One simple product, row 2. Multi-unit stock and a non-zero discount on purpose."""
    row = _r(2, handle="fake-glass-cleaner", sku="FAKE-GC-001",
             name_ar="منظف زجاج تجريبي", name_en="Fake Glass Cleaner",
             description_ar="وصف تجريبي للمنتج", description_en="Fake product description",
             price="25", sale_price="20", stock="12", weight="0.5", weight_unit="kg",
             barcode="000123456789")
    row.update(over)
    return [row]


# (size ar, size en, scent ar, scent en, price, stock)
_FC_VARIANTS = [("500 مل", "500 ml", "لافندر", "Lavender", "30", "4"),
                ("500 مل", "500 ml", "ليمون", "Lemon", "32", "0"),
                ("1 لتر", "1 L", "لافندر", "Lavender", "45", "12"),
                ("1 لتر", "1 L", "ليمون", "Lemon", "47", "3")]
_FC = "fake-floor-cleaner"


def _options_rows(handle=_FC, start=2, variants=None, sku="FAKE-FC"):
    """Size x Scent product: 4 rows, product-level values on the first row only."""
    rows = []
    for i, (s_ar, s_en, c_ar, c_en, price, stock) in enumerate(variants or _FC_VARIANTS):
        row = _r(start + i, handle=handle, sku="%s-%03d" % (sku, i + 1),
                 option1_value_ar=s_ar, option1_value_en=s_en,
                 option2_value_ar=c_ar, option2_value_en=c_en, price=price, stock=stock)
        if i == 0:
            row.update(name_ar="منظف أرضيات تجريبي", name_en="Fake Floor Cleaner",
                       description_ar="سطر أول\nسطر ثان", description_en="Fake floor copy",
                       option1_name_ar="الحجم", option1_name_en="Size",
                       option2_name_ar="الرائحة", option2_name_en="Scent",
                       category_ids="101;202", image_1="fake-fc-1.jpg",
                       image_2="fake-fc-2.webp", image_alt="Fake floor cleaner bottle")
        rows.append(row)
    return rows


def _parse(rows, **kw):
    products, errors, warnings = mapping.parse_upload_rows(rows, **kw)
    products = list(products)
    return ({p["handle"]: p for p in products}, list(errors), list(warnings), products)


def _all_errors(products, errors):
    out = list(errors)
    for p in products:
        out += list(p.get("errors") or [])
    return out


def _blocked(by_handle, handle):
    """Blocked = not offered as a product, or carrying errors (skipped in apply)."""
    return handle not in by_handle or bool(by_handle[handle].get("errors"))


def _err_at(msgs, rows=(), handle=None):
    """Some message starts with "Row N: " for one of `rows`, or with "<handle>: "."""
    prefixes = tuple("Row %d: " % n for n in rows) + ((handle + ": ",) if handle else ())
    return any(isinstance(m, str) and m.startswith(prefixes) for m in msgs)


def _leaves(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _leaves(v, "%s.%s" % (path, k))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _leaves(v, "%s[%d]" % (path, i))
    else:
        yield path, obj


def _all_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _all_keys(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _all_keys(v)


def _assert_clean_body(body, forbidden=()):
    """No "" / None / [] / {} anywhere, never cost_price, never status sale, none of
    the `forbidden` texts (e.g. EN copy in an Arabic body)."""
    assert isinstance(body, dict), body

    def _check(obj, path):
        if isinstance(obj, dict):
            for k, v in obj.items():
                assert v is not None and not (isinstance(v, (str, list, dict)) and not v), \
                    ("empty value sent", path + "." + str(k), body)
                _check(v, path + "." + str(k))
        elif isinstance(obj, (list, tuple)):
            for i, v in enumerate(obj):
                _check(v, "%s[%d]" % (path, i))
        elif isinstance(obj, str):
            assert obj.strip(), ("blank string sent", path, body)
            for text in forbidden:
                assert text not in obj, ("forbidden text", text, path)
    _check(body, "")
    assert "cost_price" not in set(_all_keys(body)), body
    assert body.get("status") in (None, "hidden"), body


def _p(rows, handle, **kw):
    by, errors, _, products = _parse(rows, **kw)
    assert not _all_errors(products, errors), _all_errors(products, errors)
    return by[handle]


_DETAIL_OPTIONS = [
    {"id": 11, "name": "الحجم", "values": [{"id": 111, "name": "500 مل"},
                                           {"id": 112, "name": "1 لتر"}]},
    {"id": 22, "name": "الرائحة", "values": [{"id": 221, "name": "لافندر"},
                                             {"id": 222, "name": "ليمون"}]},
]


def _pair_map(pairs):
    out = {}
    for sheet_v, salla_v in pairs:
        out[sheet_v["sku"]] = str(salla_v.get("id") if isinstance(salla_v, dict) else salla_v)
    return out


def _row_numbers(missing):
    return sorted(m if isinstance(m, int) else (m.get("row") if isinstance(m, dict) else m)
                  for m in missing)


# --- constants -----------------------------------------------------------------------------

def test_upload_columns_and_handle_re():
    cols = list(mapping.UPLOAD_COLUMNS)
    assert len(cols) == len(set(cols)), "duplicate column names"
    expected = ["handle", "sku", "name_ar", "name_en", "subtitle_ar", "subtitle_en",
                "promotion_title_ar", "promotion_title_en", "description_ar",
                "description_en", "seo_title_ar", "seo_title_en", "seo_description_ar",
                "seo_description_en", "category_ids", "product_type", "option1_name_ar",
                "option1_name_en", "option2_name_ar", "option2_name_en", "option1_value_ar",
                "option1_value_en", "option2_value_ar", "option2_value_en", "price",
                "sale_price", "stock", "weight", "weight_unit", "barcode", "image_alt",
                "salla_product_id"] + ["image_%d" % i for i in range(1, 11)]
    for col in expected:
        assert col in cols, col
    for col in ("image_11", "cost_price", "_row", "option3_name_ar"):
        assert col not in cols, col
    rx = re.compile(mapping.UPLOAD_HANDLE_RE)
    for ok in ("ab", "fake-1", "1-fake", "fake--cleaner", "a" + "b" * 60):
        assert rx.fullmatch(ok), ok
    for bad in ("", "a", "-fake", "Fake-cleaner", "fake cleaner", "fake_cleaner",
                "fake/cleaner", "fake.cleaner", "منظف", "a" + "b" * 61, "fake-١"):
        assert not rx.fullmatch(bad), bad


# --- read_csv_rows ---------------------------------------------------------------------

def test_read_csv_rows_bom_arabic_multiline():
    # "CSV UTF-8" from Excel: BOM, CRLF, a quoted multi-line cell. _row is the sheet row
    # (record number + 1), not the physical line number of the file.
    data = ("﻿Handle, SKU ,Name_AR,PRICE\r\n"
            "fake-a, FAKE-A-001 ,منظف تجريبي ,25\r\n"
            'fake-b,FAKE-B-001,"سطر 1\r\nسطر 2",30\r\n'
            "fake-c,FAKE-C-001,اسم تجريبي,9.5\r\n").encode("utf-8")
    rows = mapping.read_csv_rows(data)
    assert len(rows) == 3, rows
    for row in rows:
        for key, value in row.items():
            assert isinstance(key, str) and "﻿" not in key, key
            if key != "_row":
                assert isinstance(value, str), (key, value)
    a, b, c = rows
    assert a["handle"] == "fake-a" and a["sku"] == "FAKE-A-001", a
    assert a["name_ar"] == "منظف تجريبي" and a["price"] == "25", a
    assert b["name_ar"].replace("\r\n", "\n") == "سطر 1\nسطر 2", b
    assert c["price"] == "9.5", c
    assert [r["_row"] for r in rows] == [2, 3, 4], [r["_row"] for r in rows]


def test_read_csv_rows_utf8_without_bom_and_cp1256_fallback():
    text_ = "handle,sku,name_ar,description_ar\nfake-a,FAKE-A-001,منظف تجريبي,وصف عربي\n"
    rows = mapping.read_csv_rows(text_.encode("utf-8"))
    assert rows[0]["name_ar"] == "منظف تجريبي" and rows[0]["_row"] == 2, rows
    legacy = text_.encode("cp1256")      # "CSV" saved by an Arabic Windows Excel
    try:
        legacy.decode("utf-8")
        raise AssertionError("fixture must not be valid UTF-8")
    except UnicodeDecodeError:
        pass
    rows = mapping.read_csv_rows(legacy)
    assert len(rows) == 1, rows
    assert rows[0]["name_ar"] == "منظف تجريبي", rows
    assert rows[0]["description_ar"] == "وصف عربي", rows


def test_read_csv_rows_ragged_rows_never_crash():
    data = (b"handle,sku,name_ar\n"
            b"fake-a,FAKE-A-001\n"                       # one cell short
            b"fake-b,FAKE-B-001,x,extra-cell\n")         # one cell too many
    rows = mapping.read_csv_rows(data)
    assert len(rows) == 2, rows
    assert rows[0].get("name_ar") in ("", None), rows[0]
    assert rows[1]["name_ar"] == "x", rows[1]
    for row in rows:
        assert all(isinstance(k, str) for k in row), row      # no None key from DictReader


# --- read_xlsx_rows --------------------------------------------------------------------

def _products_sheet_fixture():
    shared = [_si("Handle"), _si(" SKU "), _si("Name_AR"), _si("FAKE-XL-001"),
              _si_runs("منظف ", "تجريبي"), _si("Description_AR"), _si("Barcode")]
    data = (
        _xrow(1, _c_s("A1", 0), _c_s("B1", 1), _c_s("C1", 2), _c_in("D1", "Price"),
              _c_in("E1", "Stock"), _c_s("F1", 6), _c_s("AB1", 5)) +
        _xrow(2, _c_in("A2", "fake-xl"), _c_s("B2", 3), _c_s("C2", 4), _c_n("D2", "12.0"),
              _c_n("E2", "7"), _c_in("F2", "000123456789"), _c_in("AB2", "وصف تجريبي")) +
        # row 3 absent (empty in Excel); E4 absent; F4 styled but empty; AB4 a formula
        _xrow(4, _c_in("A4", "  fake-xl-two  "), _c_in("B4", "FAKE-XL-002"),
              _c_in("C4", "اسم"), _c_n("D4", "19.95"), '<c r="F4" s="1"/>',
              '<c r="AB4" t="str"><f>CONCAT("a","b")</f><v>ab</v></c>') +
        _xrow(5, _c_in("A5", "fake-xl-three"), _c_in("B5", "FAKE-XL-003"),
              _c_in("C5", "x"), _c_n("D5", "1.5E-2"), _c_n("E5", "0"),
              _c_n("F5", "6281234567890"), '<c r="AB5" t="b"><v>1</v></c>'))
    wrong = _xrow(1, _c_in("A1", "handle"), _c_in("B1", "sku")) + \
        _xrow(2, _c_in("A2", "wrong-sheet"), _c_in("B2", "FAKE-WRONG-SHEET"))
    return shared, data, wrong


def test_read_xlsx_rows_cells():
    shared, data, wrong = _products_sheet_fixture()
    rows = mapping.read_xlsx_rows(_xlsx([("Instructions", wrong), ("Products", data)],
                                        shared=shared))
    got = _by_sku(rows)
    assert set(got) == {"FAKE-XL-001", "FAKE-XL-002", "FAKE-XL-003"}, list(got)
    a, b, c = got["FAKE-XL-001"], got["FAKE-XL-002"], got["FAKE-XL-003"]
    # header normalized; shared string; rich-text runs joined with their spaces
    assert a["handle"] == "fake-xl" and a["name_ar"] == "منظف تجريبي", a
    assert a["price"] == "12" and a["stock"] == "7", a          # integral float -> "12"
    assert a["barcode"] == "000123456789", a                    # text cell keeps zeros
    assert a["description_ar"] == "وصف تجريبي", a               # column AB (past Z)
    assert a["_row"] == 2, a
    # stripped value; empty cells ""; formula string; real Excel row number
    assert b["handle"] == "fake-xl-two", b
    assert b["price"] == "19.95", b
    assert b.get("stock") == "" and b.get("barcode") == "", b
    assert b["description_ar"] == "ab", b
    assert b["_row"] == 4, b
    assert c["price"] == "0.015" and c["stock"] == "0", c
    assert c["barcode"] == "6281234567890", c                   # not 6.28e+12, not ".0"
    assert c["_row"] == 5, c
    for row in rows:
        assert row.get("sku") != "SKU" and "FAKE-WRONG-SHEET" not in row.values(), row


def test_read_xlsx_rows_sheet_choice():
    shared, data, wrong = _products_sheet_fixture()
    want = {"FAKE-XL-001", "FAKE-XL-002", "FAKE-XL-003"}
    # "Products" is chosen over the first sheet, wherever its XML part lives
    for targets in ([1, 2], [2, 1]):
        for absolute in (False, True):
            raw = _xlsx([("Instructions", wrong), ("Products", data)], shared=shared,
                        targets=targets, absolute=absolute)
            got = set(_by_sku(mapping.read_xlsx_rows(raw)))
            assert got == want, (targets, absolute, got)
    # no "Products" sheet -> the FIRST sheet in workbook order (stored as sheet2.xml)
    raw = _xlsx([("Data", _simple_sheet("FAKE-FIRST-001")),
                 ("Other", _simple_sheet("FAKE-SECOND-001"))], targets=[2, 1])
    assert set(_by_sku(mapping.read_xlsx_rows(raw))) == {"FAKE-FIRST-001"}
    raw = _xlsx([("Sheet1", _simple_sheet("FAKE-ONLY-001"))])
    rows = mapping.read_xlsx_rows(raw)
    assert set(_by_sku(rows)) == {"FAKE-ONLY-001"} and rows[0]["name_ar"] == "اسم تجريبي"


def test_read_xlsx_rows_not_xlsx_raises_value_error():
    images_zip = io.BytesIO()
    with zipfile.ZipFile(images_zip, "w") as z:
        z.writestr("fake-1.jpg", b"\xff\xd8\xff\xe0 fake jpeg bytes")
    good = _xlsx([("Products", _simple_sheet())])
    for bad in (b"", _csv_bytes(), b"PK\x03\x04 not really a zip", images_zip.getvalue(),
                good[:200], b"\x89PNG\r\n\x1a\n fake"):
        try:
            mapping.read_xlsx_rows(bad)
        except ValueError as e:
            assert str(e).strip(), "ValueError without a readable message"
            continue
        except Exception as e:      # BadZipFile, KeyError, ParseError... must be wrapped
            raise AssertionError("%r raised %s, not ValueError" % (bad[:20], type(e).__name__))
        raise AssertionError("no error for %r" % bad[:20])


def test_read_upload_rows_dispatch():
    xlsx = _xlsx([("Products", _simple_sheet("FAKE-XL-001"))])
    for name in ("products.xlsx", "PRODUCTS.XLSX", "upload", "upload.csv"):
        # extension or the PK magic: a zip is never decoded as CSV text
        rows = mapping.read_upload_rows(xlsx, name)
        assert set(_by_sku(rows)) == {"FAKE-XL-001"}, (name, rows)
    for name in ("products.csv", "PRODUCTS.CSV"):
        rows = mapping.read_upload_rows(_csv_bytes(), name)
        assert set(_by_sku(rows)) == {"FAKE-CSV-001"}, (name, rows)
        assert rows[0]["name_ar"] == "منظف تجريبي" and rows[0]["_row"] == 2


# --- parse_upload_rows -----------------------------------------------------------------

def test_parse_simple_product():
    by, errors, warnings, products = _parse(_simple_rows(
        category_ids=" 101 ; 202 ;", salla_product_id="987654321",
        image_1="fake-gc-1.jpg", image_2="fake-gc-2.webp", image_alt="Fake bottle",
        subtitle_ar="عنوان فرعي", seo_title_en="Fake SEO title"))
    assert not _all_errors(products, errors), _all_errors(products, errors)
    assert len(products) == 1
    p = by["fake-glass-cleaner"]
    assert p["row"] == 2 and p["parent_sku"] == "FAKE-GC-001", p
    assert p["product_type"] == "product", p                     # default
    assert p["name"] == {"ar": "منظف زجاج تجريبي", "en": "Fake Glass Cleaner"}, p["name"]
    assert p["description"]["ar"] == mapping.text_to_html("وصف تجريبي للمنتج")
    assert p["description"]["en"] == mapping.text_to_html("Fake product description")
    assert p["subtitle"]["ar"] == "عنوان فرعي" and not p["subtitle"]["en"]
    assert p["seo_title"]["en"] == "Fake SEO title" and not p["seo_title"]["ar"]
    assert p["categories"] == [101, 202], p["categories"]
    assert str(p["salla_product_id"]) == "987654321"
    assert p["options"] == [] and p["images"] == ["fake-gc-1.jpg", "fake-gc-2.webp"]
    assert p["image_alt"] == "Fake bottle"
    assert not p.get("errors")
    assert len(p["variants"]) == 1
    v = p["variants"][0]
    assert v["row"] == 2 and v["sku"] == "FAKE-GC-001" and v["values"] == [], v
    assert v["price"] == 25 and v["sale_price"] == 20 and v["stock"] == 12, v
    for key in ("price", "sale_price", "stock", "weight"):
        assert not isinstance(v[key], str), (key, v[key])         # parsed numbers
    assert v["weight"] == 0.5 and v["weight_unit"] == "kg", v
    assert v["barcode"] == "000123456789", v                      # leading zeros kept


def test_parse_stock_blank_vs_zero_and_defaults():
    p = _p(_simple_rows(stock="", sale_price="", weight="", weight_unit="", product_type=""),
           "fake-glass-cleaner")
    v = p["variants"][0]
    assert v["stock"] is None, v           # blank = not sent; never 0 (would zero stock)
    assert not v["sale_price"] and not v["weight"], v
    assert v["weight_unit"] == "kg", v
    assert p["product_type"] == "product"
    v = _p(_simple_rows(stock="0", weight="500", weight_unit="g"),
           "fake-glass-cleaner")["variants"][0]
    assert v["stock"] == 0 and v["stock"] is not None, v
    assert v["weight"] == 500 and v["weight_unit"] == "g", v
    assert _p(_simple_rows(product_type="group_products"),
              "fake-glass-cleaner")["product_type"] == "group_products"


def test_parse_options_size_x_scent():
    by, errors, warnings, products = _parse(_options_rows())
    assert not _all_errors(products, errors), _all_errors(products, errors)
    p = by[_FC]
    assert p["row"] == 2 and p["parent_sku"] == "FAKE-FLOOR-CLEANER", p   # handle.upper()
    assert p["options"] == [{"ar": "الحجم", "en": "Size"},
                            {"ar": "الرائحة", "en": "Scent"}], p["options"]
    assert [v["sku"] for v in p["variants"]] == ["FAKE-FC-001", "FAKE-FC-002",
                                                 "FAKE-FC-003", "FAKE-FC-004"]
    assert [v["row"] for v in p["variants"]] == [2, 3, 4, 5]
    assert [v["price"] for v in p["variants"]] == [30, 32, 45, 47]
    assert [v["stock"] for v in p["variants"]] == [4, 0, 12, 3]
    assert p["variants"][0]["values"] == [{"ar": "500 مل", "en": "500 ml"},
                                          {"ar": "لافندر", "en": "Lavender"}]
    assert p["variants"][3]["values"] == [{"ar": "1 لتر", "en": "1 L"},
                                          {"ar": "ليمون", "en": "Lemon"}]
    # product-level values come from the first row; later rows leave them empty
    assert p["name"] == {"ar": "منظف أرضيات تجريبي", "en": "Fake Floor Cleaner"}
    assert p["description"]["ar"] == mapping.text_to_html("سطر أول\nسطر ثان")
    assert p["categories"] == [101, 202]
    assert p["images"] == ["fake-fc-1.jpg", "fake-fc-2.webp"]
    assert p["image_alt"] == "Fake floor cleaner bottle"
    # ...or repeat them identically: still fine
    rows = _options_rows()
    for row in rows[1:]:
        row.update(name_ar="منظف أرضيات تجريبي", category_ids="101;202",
                   option1_name_ar="الحجم", option2_name_ar="الرائحة",
                   image_1="fake-fc-1.jpg")
    by, errors, _, products = _parse(rows)
    assert not _all_errors(products, errors), _all_errors(products, errors)
    assert len(by[_FC]["variants"]) == 4


def test_parse_one_option_product():
    rows = [_r(2, handle="fake-one-opt", sku="FAKE-OO-001", name_ar="منتج", name_en="Item",
               description_ar="وصف", description_en="Copy", price="10", stock="5",
               option1_name_ar="الحجم", option1_name_en="Size",
               option1_value_ar="صغير", option1_value_en="Small"),
            _r(3, handle="fake-one-opt", sku="FAKE-OO-002", price="15", stock="2",
               option1_value_ar="كبير", option1_value_en="Large")]
    p = _p(rows, "fake-one-opt")
    assert p["parent_sku"] == "FAKE-ONE-OPT", p
    assert p["options"] == [{"ar": "الحجم", "en": "Size"}]
    assert [v["values"] for v in p["variants"]] == [[{"ar": "صغير", "en": "Small"}],
                                                    [{"ar": "كبير", "en": "Large"}]]


def test_parse_handle_order_and_blank_rows_skipped():
    rows = _options_rows() + [_r(6, handle="", sku=""), _r(7)] + \
        [dict(_simple_rows()[0], _row=8)]
    by, errors, warnings, products = _parse(rows)
    assert not _all_errors(products, errors), _all_errors(products, errors)
    assert [p["handle"] for p in products] == [_FC, "fake-glass-cleaner"]
    assert by["fake-glass-cleaner"]["row"] == 8
    assert not any(isinstance(m, str) and m.startswith(("Row 6: ", "Row 7: "))
                   for m in warnings), warnings


def test_parse_required_values():
    for field in ("name_ar", "description_ar", "price", "sku"):
        by, errors, _, products = _parse(_simple_rows(**{field: ""}))
        errs = _all_errors(products, errors)
        assert _blocked(by, "fake-glass-cleaner"), field
        assert _err_at(errs, rows=(2,), handle="fake-glass-cleaner"), (field, errs)
    # a row with a SKU but no handle: error on that row, the other product unaffected
    rows = _simple_rows() + [_r(3, handle="", sku="FAKE-ORPHAN-001", name_ar="منتج",
                                description_ar="وصف", price="5")]
    by, errors, _, products = _parse(rows)
    assert _err_at(_all_errors(products, errors), rows=(3,)), _all_errors(products, errors)
    assert not _blocked(by, "fake-glass-cleaner")
    # a variant row without SKU blocks its product
    rows = _options_rows()
    rows[1]["sku"] = ""
    by, errors, _, products = _parse(rows)
    assert _blocked(by, _FC) and _err_at(_all_errors(products, errors), rows=(3,))


def test_parse_en_missing_is_only_a_warning():
    by, errors, warnings, products = _parse(_simple_rows(name_en="", description_en=""))
    assert not _all_errors(products, errors), _all_errors(products, errors)
    p = by["fake-glass-cleaner"]
    assert not _blocked(by, "fake-glass-cleaner") and not p["name"]["en"]
    assert _err_at(warnings, rows=(2,), handle="fake-glass-cleaner"), warnings


def test_parse_unknown_columns_warned_once():
    rows = _options_rows()
    for row in rows:
        row["colour_notes"] = "fake"
    by, errors, warnings, products = _parse(rows)
    assert not _all_errors(products, errors), _all_errors(products, errors)
    assert sum("colour_notes" in m for m in warnings) == 1, warnings
    assert not any("_row" in m for m in warnings + errors), warnings


def test_parse_duplicate_sku():
    # across handles: reported on the row where it repeats
    rows = _simple_rows() + _options_rows(start=3)
    rows[1]["sku"] = "FAKE-GC-001"
    by, errors, _, products = _parse(rows)
    errs = _all_errors(products, errors)
    assert _err_at(errs, rows=(3,)), errs
    assert _blocked(by, _FC), errs
    # inside one handle
    rows = _options_rows()
    rows[1]["sku"] = rows[0]["sku"]
    by, errors, _, products = _parse(rows)
    assert _blocked(by, _FC) and _err_at(_all_errors(products, errors), rows=(3,))


def test_parse_bad_handle():
    for handle in ("Fake Cleaner", "-fake", "a", "fake_cleaner", "fake/cleaner", "منظف",
                   "x" * 62):
        by, errors, _, products = _parse(_simple_rows(handle=handle))
        errs = _all_errors(products, errors)
        assert _blocked(by, handle), handle
        assert _err_at(errs, rows=(2,), handle=handle), (handle, errs)


def test_parse_non_numeric_and_out_of_range_numbers():
    cases = [("price", "abc"), ("price", "inf"), ("price", "nan"), ("price", "0"),
             ("price", "-5"), ("price", "25 SAR"), ("stock", "abc"), ("stock", "2.5"),
             ("stock", "-1"), ("weight", "abc"), ("weight", "0"), ("weight", "-0.5"),
             ("weight", "inf"), ("sale_price", "abc")]
    for field, value in cases:
        rows = _options_rows()
        rows[1][field] = value                                   # sheet row 3
        by, errors, _, products = _parse(rows)
        errs = _all_errors(products, errors)
        assert _blocked(by, _FC), (field, value)
        assert _err_at(errs, rows=(3,)), (field, value, errs)


def test_parse_sale_price_rules():
    # numbers, not strings: "99" < "100" and "10" > "9"
    p = _p(_simple_rows(price="100", sale_price="99"), "fake-glass-cleaner")
    assert p["variants"][0]["sale_price"] == 99
    for price, sale in (("9", "10"), ("25", "25"), ("25", "25.00"), ("25", "30")):
        by, errors, _, products = _parse(_simple_rows(price=price, sale_price=sale))
        assert _blocked(by, "fake-glass-cleaner"), (price, sale)
        assert _err_at(_all_errors(products, errors), rows=(2,)), (price, sale)
    # checked per variant, against that variant's own price
    rows = _options_rows()
    rows[0]["sale_price"] = "28"           # 30 -> 28 fine
    rows[1]["sale_price"] = "35"           # 32 -> 35 wrong (would pass against max 47)
    by, errors, _, products = _parse(rows)
    errs = _all_errors(products, errors)
    assert _blocked(by, _FC) and _err_at(errs, rows=(3,)), errs
    assert not _err_at(errs, rows=(2,)), errs


def test_parse_option_name_value_pairing():
    every = (2, 3, 4, 5)
    # values without an option name
    rows = _options_rows()
    rows[0].update(option2_name_ar="", option2_name_en="")
    by, errors, _, products = _parse(rows)
    assert _blocked(by, _FC) and _err_at(_all_errors(products, errors), every, _FC)
    # option name set, one row without its value
    rows = _options_rows()
    rows[2]["option2_value_ar"] = ""
    rows[2]["option2_value_en"] = ""
    by, errors, _, products = _parse(rows)
    assert _blocked(by, _FC) and _err_at(_all_errors(products, errors), (4,))
    # EN option name only: AR is required when values are used
    rows = _options_rows()
    rows[0]["option1_name_ar"] = ""
    by, errors, _, products = _parse(rows)
    assert _blocked(by, _FC) and _err_at(_all_errors(products, errors), every, _FC)
    # EN value only: AR is required when the option name is set
    rows = _options_rows()
    rows[1]["option1_value_ar"] = ""
    by, errors, _, products = _parse(rows)
    assert _blocked(by, _FC) and _err_at(_all_errors(products, errors), (3,))


def test_parse_duplicate_option_combination():
    rows = _options_rows()
    rows[1].update(option1_value_ar="500 مل", option2_value_ar="لافندر")   # = row 2
    by, errors, _, products = _parse(rows)
    assert _blocked(by, _FC) and _err_at(_all_errors(products, errors), (2, 3), _FC)


def test_parse_duplicate_option_combination_normalized():
    """Hardening beyond the literal spec: two rows whose AR values differ only by
    spaces / Arabic-Indic digits would both match ONE Salla variant (match_variants
    uses norm_label), so the second silently overwrites the first."""
    rows = _options_rows()
    rows[1].update(option1_value_ar="٥٠٠  مل", option2_value_ar="لافندر ")
    by, errors, _, products = _parse(rows)
    assert _blocked(by, _FC) and _err_at(_all_errors(products, errors), (2, 3), _FC)


def test_parse_max_options_and_images():
    by, errors, _, products = _parse(_options_rows(), max_options=1)
    assert _blocked(by, _FC) and _err_at(_all_errors(products, errors), (2, 3, 4, 5), _FC)
    ten = {"image_%d" % i: "fake-gc-%d.jpg" % i for i in range(1, 11)}
    p = _p(_simple_rows(**ten), "fake-glass-cleaner")
    assert p["images"] == ["fake-gc-%d.jpg" % i for i in range(1, 11)]
    four = {"image_%d" % i: "fake-gc-%d.jpg" % i for i in range(1, 5)}
    by, errors, _, products = _parse(_simple_rows(**four), max_images=3)
    assert _blocked(by, "fake-glass-cleaner")
    assert _err_at(_all_errors(products, errors), (2,), "fake-glass-cleaner")


def test_parse_mixed_product_level_values():
    for field, value in (("name_ar", "اسم آخر"), ("description_en", "Other fake copy"),
                         ("image_1", "fake-other.jpg"), ("category_ids", "303"),
                         ("option1_name_en", "Volume"), ("salla_product_id", "111"),
                         ("product_type", "group_products")):
        rows = _options_rows()
        rows[1][field] = value
        by, errors, _, products = _parse(rows)
        errs = _all_errors(products, errors)
        assert _blocked(by, _FC), (field, errs)
        assert _err_at(errs, (3,), _FC), (field, errs)


def test_parse_simple_product_with_two_rows():
    rows = _simple_rows() + [_r(3, handle="fake-glass-cleaner", sku="FAKE-GC-002",
                                price="30", stock="5")]
    by, errors, _, products = _parse(rows)
    errs = _all_errors(products, errors)
    assert _blocked(by, "fake-glass-cleaner") and _err_at(errs, (2, 3), "fake-glass-cleaner")


def test_parse_bad_product_type_and_categories_flagged():
    for over in ({"product_type": "bundle"}, {"category_ids": "abc"},
                 {"category_ids": "101;x"}, {"category_ids": "1.5"}):
        by, errors, warnings, products = _parse(_simple_rows(**over))
        msgs = _all_errors(products, errors) + warnings
        assert _err_at(msgs, (2,), "fake-glass-cleaner"), (over, msgs)


def test_upload_headers_case_and_spaces_insensitive():
    """Spec §3: header names are case/spaces-insensitive. A header typed "Name AR" must
    not silently become an unknown column (the product would then miss its AR name)."""
    data = ("Handle,SKU,Name AR,Description AR,Price,Option1 Name AR\n"
            "fake-hdr,FAKE-HDR-001,منظف تجريبي,وصف,25,\n").encode("utf-8")
    p = _p(mapping.read_csv_rows(data), "fake-hdr")
    assert p["name"]["ar"] == "منظف تجريبي" and p["variants"][0]["price"] == 25


def test_upload_csv_end_to_end():
    header = ",".join(mapping.UPLOAD_COLUMNS)
    def line(**values):
        return ",".join('"%s"' % values.get(c, "").replace('"', '""')
                        for c in mapping.UPLOAD_COLUMNS)
    body = [line(**row) for row in [
        {k: v for k, v in r.items() if k != "_row"} for r in _options_rows()]]
    data = ("﻿" + header + "\r\n" + "\r\n".join(body) + "\r\n").encode("utf-8")
    by, errors, warnings, products = _parse(mapping.read_upload_rows(data, "fake.csv"))
    assert not _all_errors(products, errors), _all_errors(products, errors)
    assert not any("unknown" in m.lower() for m in warnings), warnings
    p = by[_FC]
    assert len(p["variants"]) == 4 and p["variants"][3]["stock"] == 3
    assert p["variants"][1]["stock"] == 0 and p["categories"] == [101, 202]


# --- text_to_html ------------------------------------------------------------------------

def test_text_to_html():
    f = mapping.text_to_html
    assert f("") == "" and f(None) == ""
    one = f("Fake cleaner")
    assert "Fake cleaner" in one and one.count("<p>") == 1, one
    esc = f('Fake "A" & B < C > D')
    assert "&amp;" in esc and "&lt;" in esc and "&gt;" in esc and "< C" not in esc, esc
    assert "<script>" not in f("x <script>alert(1)</script>")
    two = f("Para one\n\nPara two")
    assert two.count("<p>") == 2 and "<br" not in two, two
    many = f("Para one\n\n\n\nPara two")
    assert many.count("<p>") == 2 and "<p></p>" not in many, many
    br = f("Line one\nLine two")
    assert br.count("<p>") == 1 and "<br" in br, br
    crlf = f("Line one\r\n\r\nLine two\r\nLine three")
    assert crlf.count("<p>") == 2 and "<br" in crlf and "\r" not in crlf, crlf
    lst = f("Intro\n- first point\n- second point\n\nOutro")
    assert "<ul>" in lst and "</ul>" in lst and lst.count("<li>") == 2, lst
    assert "<li>first point</li>" in lst and "- first" not in lst, lst
    assert "Intro" in lst and "Outro" in lst, lst
    bullet = f("• نقطة أولى\n• نقطة ثانية")
    assert bullet.count("<li>") == 2 and "•" not in bullet and "نقطة أولى" in bullet, bullet
    html_in = "<p>Already <b>HTML</b></p>"
    assert f(html_in) == html_in
    assert "منظف تجريبي" in f("منظف تجريبي")


# --- flagged_claims ----------------------------------------------------------------------

_CLAIMS = ["baby-safe", "baby safe", "SASO", "salmonella", "microbiome", "won't scratch",
           "non-corrosive", "no allergies", "safe if licked", "lick", "آمن للرضع"]


def test_flagged_claims_ar_en_case_insensitive():
    p = _p(_simple_rows(
        description_en="Gentle formula.\nBaby-Safe and won't scratch glass.",
        subtitle_ar="آمن للرضع", seo_description_en="Tested against SALMONELLA"),
        "fake-glass-cleaner")
    found = mapping.flagged_claims(p, _CLAIMS)
    # "won't" must be found although the description is already HTML (' may be escaped)
    assert sorted(x.lower() for x in found) == sorted(
        ["baby-safe", "won't scratch", "salmonella", "آمن للرضع"]), found
    assert len(found) == len(set(found)), found
    clean = _p(_simple_rows(), "fake-glass-cleaner")
    assert mapping.flagged_claims(clean, _CLAIMS) == []
    assert mapping.flagged_claims(clean, ["", "   "]) == []    # blank lines never match
    assert mapping.flagged_claims(clean, []) == []


def test_flagged_claims_every_text_field():
    for field in ("name_ar", "name_en", "subtitle_ar", "subtitle_en", "promotion_title_ar",
                  "promotion_title_en", "description_ar", "description_en", "seo_title_ar",
                  "seo_title_en", "seo_description_ar", "seo_description_en"):
        p = _p(_simple_rows(**{field: "Fake copy SaSo approved"}), "fake-glass-cleaner")
        found = mapping.flagged_claims(p, ["saso", "microbiome"])
        assert [x.lower() for x in found] == ["saso"], (field, found)


def test_flagged_claims_options_and_alt():
    """Option values and the image alt text are uploaded too: claims there count."""
    rows = _options_rows()
    rows[0]["image_alt"] = "Baby safe bottle"
    p = _p(rows, _FC)
    assert [x.lower() for x in mapping.flagged_claims(p, ["baby safe"])] == ["baby safe"]
    rows = _options_rows()
    for row in rows:
        if row["option2_value_ar"] == "لافندر":
            row["option2_value_en"] = "Non-Corrosive Lavender"
    p = _p(rows, _FC)
    assert [x.lower() for x in mapping.flagged_claims(p, ["non-corrosive"])] == \
        ["non-corrosive"]


def test_flagged_claims_typographic_apostrophe():
    """Hardening beyond the literal spec: copy pasted from Word/Notion uses the curly
    apostrophe; "won’t scratch" must still hit the blocked phrase "won't scratch"."""
    p = _p(_simple_rows(description_en="It won’t scratch glass"), "fake-glass-cleaner")
    assert [x.lower() for x in mapping.flagged_claims(p, ["won't scratch"])] == \
        ["won't scratch"]


# --- product_payload / translation_payload ------------------------------------------------

_EN_TEXTS = ("Fake Glass Cleaner", "Fake product description", "Fake Floor Cleaner",
             "Fake floor copy", "Size", "Scent", "Lavender", "Lemon")


def test_product_payload_create_simple():
    p = _p(_simple_rows(subtitle_ar="عنوان فرعي", promotion_title_ar="عرض تجريبي",
                        seo_title_ar="عنوان سيو", seo_description_ar="وصف سيو",
                        category_ids="101;202", weight="500", weight_unit="g",
                        image_1="fake-gc-1.jpg"), "fake-glass-cleaner")
    body = mapping.product_payload(p, True)
    _assert_clean_body(body, _EN_TEXTS)
    assert body["name"] == "منظف زجاج تجريبي"
    assert body["price"] == 25 and body["sale_price"] == 20, body
    assert body["quantity"] == 12, body                       # multi-unit stock
    assert body["status"] == "hidden" and body["sku"] == "FAKE-GC-001", body
    assert body["product_type"] == "product"
    assert body["description"] == p["description"]["ar"]
    assert body["subtitle"] == "عنوان فرعي" and body["promotion_title"] == "عرض تجريبي"
    assert body["metadata_title"] == "عنوان سيو" and body["metadata_description"] == "وصف سيو"
    assert body["categories"] == [101, 202] and body["require_shipping"] is True, body
    assert body["weight"] == 500 and body["weight_type"] == "g", body
    for key in ("barcode", "options", "images", "cost_price"):
        assert key not in body, key


def test_product_payload_create_options():
    rows = _options_rows()
    for row, price in zip(rows, ("100", "25", "9.5", "47")):   # min is numeric, not text
        row["price"] = price
    p = _p(rows, _FC)
    body = mapping.product_payload(p, True)
    _assert_clean_body(body, _EN_TEXTS)
    assert body["price"] == 9.5, body
    assert body["sku"] == "FAKE-FLOOR-CLEANER" and body["status"] == "hidden", body
    assert body["options"] == [
        {"name": "الحجم", "display_type": "text",
         "values": [{"name": "500 مل"}, {"name": "1 لتر"}]},
        {"name": "الرائحة", "display_type": "text",
         "values": [{"name": "لافندر"}, {"name": "ليمون"}]}], body["options"]
    for key in ("quantity", "sale_price", "images", "barcode"):
        assert key not in body, key


def test_product_payload_update_never_status_options_images():
    p = _p(_simple_rows(category_ids="101"), "fake-glass-cleaner")
    upd = mapping.product_payload(p, False)
    _assert_clean_body(upd, _EN_TEXTS)
    for key in ("status", "options", "images"):
        assert key not in upd, key
    assert upd["name"] == "منظف زجاج تجريبي" and upd["price"] == 25, upd
    assert upd["description"] == p["description"]["ar"] and upd["categories"] == [101]
    upd = mapping.product_payload(_p(_options_rows(), _FC), False)
    _assert_clean_body(upd, _EN_TEXTS)
    for key in ("status", "options", "images", "price"):
        assert key not in upd, key
    assert upd["name"] == "منظف أرضيات تجريبي"


def test_product_payload_empty_optionals_omitted():
    p = _p(_simple_rows(stock="", sale_price="", weight=""), "fake-glass-cleaner")
    for create in (True, False):
        body = mapping.product_payload(p, create)
        _assert_clean_body(body, _EN_TEXTS)
        # an empty categories list on update would wipe categories set by hand in Salla
        for key in ("subtitle", "promotion_title", "metadata_title", "metadata_description",
                    "categories", "quantity", "sale_price", "weight", "weight_type"):
            assert key not in body, (create, key, body)
    body = mapping.product_payload(_p(_simple_rows(stock="0"), "fake-glass-cleaner"), True)
    assert body["quantity"] == 0, body                         # 0 is a value, not empty


# --- create_weight: Salla requires a weight on POST /products (422 "fields.weight" on the
# demo store, 2026-11-10) ------------------------------------------------------------------

def _weighted_options_rows(*weights):
    """_options_rows with (weight, weight_unit) per row; None = both cells empty."""
    rows = _options_rows()
    for row, wu in zip(rows, weights):
        if wu is not None:
            row["weight"], row["weight_unit"] = wu
    return rows


def test_create_weight_simple_product():
    p = _p(_simple_rows(), "fake-glass-cleaner")                 # 0.5 kg, stock 12, sale 20
    assert mapping.create_weight(p) == (0.5, "kg"), mapping.create_weight(p)
    body = mapping.product_payload(p, True)
    _assert_clean_body(body, _EN_TEXTS)
    assert body["weight"] == 0.5 and body["weight_type"] == "kg", body
    assert body["quantity"] == 12 and body["sale_price"] == 20, body   # unchanged
    upd = mapping.product_payload(p, False)                     # simple: update still sends it
    assert upd["weight"] == 0.5 and upd["weight_type"] == "kg", upd
    # g unit, given in upper case in the sheet: lower-cased on create and update
    p = _p(_simple_rows(weight="750", weight_unit="G"), "fake-glass-cleaner")
    assert mapping.create_weight(p) == (750, "g"), mapping.create_weight(p)
    for create in (True, False):
        body = mapping.product_payload(p, create)
        assert body["weight"] == 750 and body["weight_type"] == "g", (create, body)
    # unit cell empty -> kg
    p = _p(_simple_rows(weight="2", weight_unit=""), "fake-glass-cleaner")
    assert mapping.create_weight(p) == (2, "kg"), mapping.create_weight(p)
    assert mapping.product_payload(p, True)["weight_type"] == "kg"


def test_create_weight_options_first_weighted_row():
    # every row weighted: the first row's weight goes on the create
    p = _p(_weighted_options_rows(("0.5", "kg"), ("0.6", "kg"), ("1.1", "kg"), ("1.2", "kg")),
           _FC)
    assert mapping.create_weight(p) == (0.5, "kg"), mapping.create_weight(p)
    body = mapping.product_payload(p, True)
    _assert_clean_body(body, _EN_TEXTS)
    assert body["weight"] == 0.5 and body["weight_type"] == "kg", body
    assert body["price"] == 30 and len(body["options"]) == 2, body   # rest of the create body
    for key in ("quantity", "sale_price", "images", "barcode"):
        assert key not in body, key
    # first row has no weight, second is 600 g, third 1.1 kg: the second row wins (sheet
    # order, not the lowest, not the first kg one)
    p = _p(_weighted_options_rows(None, ("600", "g"), ("1.1", "kg"), None), _FC)
    assert mapping.create_weight(p) == (600, "g"), mapping.create_weight(p)
    body = mapping.product_payload(p, True)
    _assert_clean_body(body, _EN_TEXTS)
    assert body["weight"] == 600 and body["weight_type"] == "g", body
    # only the last row weighted
    p = _p(_weighted_options_rows(None, None, None, ("1.2", "KG")), _FC)
    assert mapping.create_weight(p) == (1.2, "kg"), mapping.create_weight(p)
    assert mapping.product_payload(p, True)["weight"] == 1.2


def test_create_weight_none_without_any_weight():
    p = _p(_options_rows(), _FC)                                # 4 rows, no weight anywhere
    assert mapping.create_weight(p) is None, mapping.create_weight(p)
    body = mapping.product_payload(p, True)
    _assert_clean_body(body, _EN_TEXTS)
    assert "weight" not in body and "weight_type" not in body, body
    p = _p(_simple_rows(weight="", weight_unit="g"), "fake-glass-cleaner")   # unit alone
    assert mapping.create_weight(p) is None, mapping.create_weight(p)
    for create in (True, False):
        body = mapping.product_payload(p, create)
        assert "weight" not in body and "weight_type" not in body, (create, body)
    # raw dicts (no parser): no variants / missing key / explicit None
    for product in ({}, {"variants": []}, {"variants": None},
                    {"variants": [{"sku": "FAKE-1"}, {"sku": "FAKE-2", "weight": None}]}):
        assert mapping.create_weight(product) is None, product
    # unit missing or None on a raw dict -> kg; upper case -> lower case
    assert mapping.create_weight({"variants": [{"weight": 3}]}) == (3, "kg")
    assert mapping.create_weight({"variants": [{"weight": 3, "weight_unit": None}]}) == (3, "kg")
    assert mapping.create_weight({"variants": [{"weight": 3, "weight_unit": "G"}]}) == (3, "g")



def test_create_weight_rejects_non_positive_and_non_numbers():
    # Second line of defence behind the parser: only a real number above 0 counts.
    for bad in (0, -1, 0.0, True, "0.5", None):
        p = {"options": [], "variants": [{"sku": "FAKE-W-1", "weight": bad, "weight_unit": "kg"}]}
        assert mapping.create_weight(p) is None, bad
    p = {"options": [], "variants": [{"sku": "FAKE-W-1", "weight": 750, "weight_unit": ""}]}
    assert mapping.create_weight(p) == (750, "kg"), mapping.create_weight(p)
    p = {"options": [{"ar": "x", "en": "x"}], "variants": [
        {"sku": "FAKE-W-1", "weight": 0, "weight_unit": "kg"},
        {"sku": "FAKE-W-2", "weight": 2, "weight_unit": "G"}]}
    assert mapping.create_weight(p) == (2, "g"), mapping.create_weight(p)

def test_product_payload_update_options_never_weight():
    # a product with options: its weight lives on the variants (variant_payload), so the
    # product PUT never carries one, even when every row is weighted
    p = _p(_weighted_options_rows(("600", "g"), ("0.6", "kg"), ("1.1", "kg"), ("1.2", "kg")),
           _FC)
    upd = mapping.product_payload(p, False)
    _assert_clean_body(upd, _EN_TEXTS)
    for key in ("weight", "weight_type", "status", "options", "price", "quantity"):
        assert key not in upd, (key, upd)
    # ... while each variant PUT still sends its own row's weight (multi-unit stock rows)
    by_sku = {v["sku"]: v for v in p["variants"]}
    vp = mapping.variant_payload(by_sku["FAKE-FC-001"])
    assert vp.get("weight") == 600 and vp.get("weight_type") == "g", vp
    vp = mapping.variant_payload(by_sku["FAKE-FC-003"])
    assert vp.get("weight") == 1.1 and vp.get("weight_type") == "kg", vp


def test_translation_payload():
    rows = _options_rows()
    rows[0].update(subtitle_en="Fake subtitle", promotion_title_en="Fake promo",
                   seo_title_en="Fake SEO", seo_description_en="Fake SEO copy",
                   subtitle_ar="عنوان فرعي", promotion_title_ar="عرض تجريبي",
                   seo_title_ar="عنوان سيو", seo_description_ar="وصف سيو")
    p = _p(rows, _FC)
    tp = mapping.translation_payload(p, "en")
    assert tp == {"name": "Fake Floor Cleaner", "description": p["description"]["en"],
                  "subtitle": "Fake subtitle", "promotion_title": "Fake promo",
                  "metadata_title": "Fake SEO", "metadata_description": "Fake SEO copy"}, tp
    assert mapping.translation_payload(p) == tp                # lang defaults to "en"
    p = _p(_simple_rows(name_en=""), "fake-glass-cleaner")
    tp = mapping.translation_payload(p, "en")
    assert "name" not in tp and tp["description"] == p["description"]["en"], tp
    _assert_clean_body(tp, ("منظف", "وصف"))
    p = _p(_simple_rows(name_en="", description_en=""), "fake-glass-cleaner")
    assert mapping.translation_payload(p, "en") == {}


_WIZARD_SRC = os.path.join(os.path.dirname(__file__), "..", "wizard", "salla_product_upload.py")
# _request("PUT", "/products/%s" ...): method + path literal of every API call in the wizard
_REQUEST_RE = re.compile(r'_request\(\s*"([A-Z]+)",\s*"([^"]*)"')


def _wizard_requests():
    with open(_WIZARD_SRC, encoding="utf-8") as fh:
        src = fh.read()
    return src, _REQUEST_RE.findall(src)


def _wizard_tree():
    with open(_WIZARD_SRC, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _enclosing_functions(tree):
    """{id(node): FunctionDef} for every node inside a function of the wizard."""
    owner = {}
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for node in ast.walk(fn):
                owner[id(node)] = fn          # inner defs walked later override: fine
    return owner


def _is_option_path_call(node):
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
        and node.func.attr == "_option_path"


def _assigned_from_option_path(fn, name):
    return any(isinstance(n, ast.Assign) and _is_option_path_call(n.value)
               and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)
               for n in ast.walk(fn))


def _str_constants(node):
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def test_option_en_writes_lang_ar_translations_only():
    # Spike run 4 (2026-11-10, English enabled): option / value English goes ONLY through
    # PUT /products/options[/values]/{id} with lang "ar" + {"name": <Salla's Arabic>,
    # "translations": {"en": {...}}}. A lang "en" PUT there wrote OVER the Arabic. The
    # wizard imports odoo, so it is read with ast (the path is a variable: a regex on
    # literal paths cannot see these calls).
    tree = _wizard_tree()
    owner = _enclosing_functions(tree)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "_request"]
    assert len(calls) >= 8, "too few _request calls found: ast walk out of date"
    option_calls = []
    for call in calls:
        assert call.args and isinstance(call.args[0], ast.Constant), ast.unparse(call)
        method = call.args[0].value
        assert method in ("GET", "POST", "PUT"), ast.unparse(call)       # never DELETE
        path = call.args[1]
        lang = {k.arg: k.value for k in call.keywords}.get("lang")
        lang = lang.value if isinstance(lang, ast.Constant) else lang
        fn = owner.get(id(call))
        is_option = _is_option_path_call(path) or (
            isinstance(path, ast.Name) and fn is not None
            and _assigned_from_option_path(fn, path.id))
        # no literal options path anywhere: every one goes through _option_path
        assert not any("option" in c for c in _str_constants(path)), ast.unparse(call)
        if not is_option:
            continue
        option_calls.append(call)
        assert method == "PUT", ast.unparse(call)
        assert lang == "ar", ("option write without lang ar", ast.unparse(call))
        payload = {k.arg: k.value for k in call.keywords}.get("payload")
        assert payload is not None, ast.unparse(call)
        if isinstance(payload, ast.Dict):        # the Arabic restore: name only
            keys = {k.value for k in payload.keys}
            assert keys == {"name"}, ("option restore body", keys)
        else:                                    # the write: body built by the mapping
            assert isinstance(payload, ast.Name), ast.unparse(call)
            assert "option_translation_payloads" in ast.unparse(fn), fn.name
    # the translations write + the Arabic restore
    assert len(option_calls) >= 2, [ast.unparse(c) for c in option_calls]
    # every _option_path use is the path of one of those calls, or assigned to a name
    # that is (path = self._option_path(...); _request("PUT", path, lang="ar", ...))
    for use in (n for n in ast.walk(tree) if _is_option_path_call(n)):
        direct = any(use is c.args[1] for c in option_calls)
        fn = owner.get(id(use))
        targets = [t.id for a in ast.walk(fn) if isinstance(a, ast.Assign) and a.value is use
                   for t in a.targets if isinstance(t, ast.Name)] if fn else []
        via_name = targets and all(
            any(isinstance(c.args[1], ast.Name) and c.args[1].id == t and owner.get(id(c)) is fn
                for c in option_calls) for t in targets)
        assert direct or via_name, ("_option_path used outside a lang ar PUT",
                                    ast.unparse(use))
    # _option_path only builds the two endpoints of the spike
    option_path = next(n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef) and n.name == "_option_path")
    paths = {c for c in _str_constants(option_path) if "/" in c}
    assert paths == {"/products/options/%s", "/products/options/values/%s"}, paths
    # the option English code never names values / price / quantity / status
    for name in ("_apply_option_english", "_option_english_kept", "_option_path"):
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
        for c in _str_constants(fn):
            assert c not in ("values", "price", "quantity", "sale_price", "status"), (name, c)
    # every status write is "hidden", never "sale"
    src, _ = _wizard_requests()
    statuses = re.findall(r'payload=\{\s*"status":\s*"([^"]*)"', src)
    assert statuses and set(statuses) == {"hidden"}, statuses
    assert '"status": "sale"' not in src and "'status': 'sale'" not in src


def _fc_detail(**over):
    """Salla detail (lang ar, ?with=translations) of the fake floor cleaner, no English."""
    detail = {"id": 555, "options": [
        {"id": 11, "name": "الحجم", "values": [{"id": 111, "name": "500 مل"},
                                               {"id": 112, "name": "1 لتر"}]},
        {"id": 22, "name": "الرائحة", "values": [{"id": 221, "name": "لافندر"},
                                                 {"id": 222, "name": "ليمون"}]}]}
    detail.update(over)
    return detail


def _assert_option_bodies(writes):
    """Every body: exactly name + translations.en.<kind key>; never values / price /
    quantity / lang / an English name in "name"."""
    for kind, salla_id, body in writes:
        assert kind in ("option", "value"), kind
        assert isinstance(salla_id, str) and salla_id, salla_id
        assert set(body) == {"name", "translations"}, body
        assert set(body["translations"]) == {"en"}, body
        assert set(body["translations"]["en"]) == {mapping.OPTION_EN_KEYS[kind]}, body
        en = body["translations"]["en"][mapping.OPTION_EN_KEYS[kind]]
        assert mapping.norm_label(body["name"]) != mapping.norm_label(en), body
        keys = set(_all_keys(body))
        for bad in ("values", "price", "sale_price", "quantity", "stock", "lang", "status",
                    "sku", "skus"):
            assert bad not in keys, (bad, body)
        assert "name" not in body["translations"]["en"], body     # silently ignored by Salla
        _assert_clean_body(body)


def test_option_en_keys_from_spike_run4():
    assert mapping.OPTION_EN_KEYS == {"option": "option_name", "value": "option_details_name"}


def test_option_translation_payloads_full_product():
    p = _p(_options_rows(), _FC)
    writes, skipped, unchanged = mapping.option_translation_payloads(p, _fc_detail())
    assert skipped == [] and unchanged == 0, (skipped, unchanged)
    assert writes == [
        ("option", "11", {"name": "الحجم", "translations": {"en": {"option_name": "Size"}}}),
        ("value", "111", {"name": "500 مل",
                          "translations": {"en": {"option_details_name": "500 ml"}}}),
        ("value", "112", {"name": "1 لتر",
                          "translations": {"en": {"option_details_name": "1 L"}}}),
        ("option", "22", {"name": "الرائحة", "translations": {"en": {"option_name": "Scent"}}}),
        ("value", "221", {"name": "لافندر",
                          "translations": {"en": {"option_details_name": "Lavender"}}}),
        ("value", "222", {"name": "ليمون",
                          "translations": {"en": {"option_details_name": "Lemon"}}}),
    ], writes
    _assert_option_bodies(writes)
    # four rows, two values per option: each value written once
    assert len([w for w in writes if w[0] == "value"]) == 4


def test_option_translation_payloads_arabic_from_salla_not_sheet():
    # Salla spells the Arabic differently (Arabic-Indic digits, a dict name, extra
    # spaces): matched by norm_label, but the body carries SALLA's text, so the write
    # cannot change the Arabic.
    p = _p(_options_rows(), _FC)
    detail = _fc_detail(options=[
        {"id": 11, "name": {"ar": "الحجم", "en": "Old size"},
         "values": [{"id": 111, "name": "٥٠٠ مل"}, {"id": 112, "name": "1  لتر"}]},
        {"id": 22, "name": "الرائحة", "values": [{"id": 221, "name": {"ar": "لافندر"}},
                                                 {"id": 222, "name": "ليمون"}]}])
    writes, skipped, unchanged = mapping.option_translation_payloads(p, detail)
    assert skipped == [] and unchanged == 0, skipped
    by_id = {i: body["name"] for kind, i, body in writes}
    assert by_id["111"] == "٥٠٠ مل", by_id          # not the sheet's "500 مل"
    assert by_id["112"] == mapping.text("1  لتر"), by_id
    assert by_id["11"] == "الحجم", by_id             # .ar of a dict name, never .en
    assert by_id["221"] == "لافندر", by_id
    assert all(not isinstance(n, dict) for n in by_id.values()), by_id
    _assert_option_bodies(writes)


def test_option_translation_payloads_empty_en_skipped():
    rows = _options_rows()
    rows[0]["option1_name_en"] = ""                       # option name EN empty
    for row in rows:
        if row["option2_value_ar"] == "ليمون":
            row["option2_value_en"] = ""                  # value EN empty on every row
    p = _p(rows, _FC)
    writes, skipped, unchanged = mapping.option_translation_payloads(p, _fc_detail())
    ids = [i for kind, i, body in writes]
    assert ids == ["111", "112", "22", "221"], ids        # option 11 and value 222 not sent
    assert "option1 الحجم: option1_name_en is empty" in skipped, skipped
    assert "option2 value ليمون: option2_value_en is empty on every row" in skipped, skipped
    assert len(skipped) == 2 and unchanged == 0, skipped
    _assert_option_bodies(writes)


def test_option_translation_payloads_en_inherited_from_other_rows():
    # 500 مل has its English on row 2 only (rows 3 empty): taken from row 2.
    rows = _options_rows()
    rows[1]["option1_value_en"] = ""
    rows[3]["option2_value_en"] = "  "                    # blank, not a conflict
    p = _p(rows, _FC)
    writes, skipped, _ = mapping.option_translation_payloads(p, _fc_detail())
    en = {i: b["translations"]["en"].get("option_details_name") for k, i, b in writes}
    assert en["111"] == "500 ml" and en["222"] == "Lemon", en
    assert skipped == [], skipped


def test_option_translation_payloads_conflicting_en_skipped():
    rows = _options_rows()
    rows[2]["option2_value_en"] = "Lavande"               # row 4: لافندر -> other EN
    p = _p(rows, _FC)
    writes, skipped, _ = mapping.option_translation_payloads(p, _fc_detail())
    assert "221" not in [i for k, i, b in writes], writes
    assert skipped == ["option2 value لافندر: different English on rows 2, 4 "
                       "(Lavender, Lavande)"], skipped
    # only case / spacing differs -> the same English, not a conflict
    rows = _options_rows()
    rows[2]["option2_value_en"] = "  LAVENDER "
    p = _p(rows, _FC)
    writes, skipped, _ = mapping.option_translation_payloads(p, _fc_detail())
    assert skipped == [], skipped
    assert ("value", "221", {"name": "لافندر", "translations": {
        "en": {"option_details_name": "Lavender"}}}) in writes, writes


def test_option_translation_payloads_en_same_as_arabic_skipped():
    rows = _options_rows()
    rows[0]["option2_name_en"] = "الرائحة"
    for row in rows:
        if row["option1_value_ar"] == "500 مل":
            row["option1_value_en"] = "٥٠٠ مل"            # same as the Arabic (norm_label)
    p = _p(rows, _FC)
    writes, skipped, _ = mapping.option_translation_payloads(p, _fc_detail())
    ids = [i for k, i, b in writes]
    assert "22" not in ids and "111" not in ids, ids
    assert "option2 الرائحة: the English name is the same as the Arabic" in skipped, skipped
    assert "option1 value 500 مل: the English name is the same as the Arabic" in skipped, \
        skipped
    _assert_option_bodies(writes)


def test_option_translation_payloads_missing_salla_option_or_value():
    p = _p(_options_rows(), _FC)
    # option 2 not in Salla: none of its names sent, one reason; option 1 still sent
    detail = _fc_detail()
    detail["options"] = detail["options"][:1]
    writes, skipped, _ = mapping.option_translation_payloads(p, detail)
    assert [i for k, i, b in writes] == ["11", "111", "112"], writes
    assert skipped == ["option2 الرائحة: not in the Salla product (its English names are "
                       "not sent)"], skipped
    # one value missing in the Salla option
    detail = _fc_detail()
    detail["options"][0]["values"] = detail["options"][0]["values"][:1]
    writes, skipped, _ = mapping.option_translation_payloads(p, detail)
    assert "112" not in [i for k, i, b in writes], writes
    assert skipped == ["option1 value 1 لتر: not in the Salla option"], skipped


def test_option_translation_payloads_ambiguous_match_skipped():
    p = _p(_options_rows(), _FC)
    # two Salla values with the same Arabic in one option
    detail = _fc_detail()
    detail["options"][1]["values"].append({"id": 229, "name": " لافندر "})
    writes, skipped, _ = mapping.option_translation_payloads(p, detail)
    ids = [i for k, i, b in writes]
    assert "221" not in ids and "229" not in ids, ids
    assert skipped == ["option2 value لافندر: matches 2 Salla values"], skipped
    # two Salla options with the same Arabic name
    detail = _fc_detail()
    detail["options"].append({"id": 33, "name": "الحجم", "values": []})
    writes, skipped, _ = mapping.option_translation_payloads(p, detail)
    assert [i for k, i, b in writes] == ["22", "221", "222"], writes
    assert skipped == ["option1 الحجم: matches 2 Salla options (its English names are "
                       "not sent)"], skipped


def test_option_translation_payloads_no_id_or_no_arabic_skipped():
    p = _p(_options_rows(), _FC)
    detail = _fc_detail()
    detail["options"][0]["values"][0]["id"] = None
    detail["options"][1]["id"] = "None"                   # str(None) upstream
    writes, skipped, _ = mapping.option_translation_payloads(p, detail)
    ids = [i for k, i, b in writes]
    assert ids == ["11", "112", "221", "222"], ids
    assert "option1 value 500 مل: the Salla value has no id or Arabic name" in skipped
    assert "option2 الرائحة: the Salla option has no id or Arabic name" in skipped
    assert all(i not in ("None", "") for i in ids)


def test_option_translation_payloads_already_in_salla_nothing():
    p = _p(_options_rows(), _FC)
    detail = _fc_detail()
    en = {"11": "Size", "111": "500 ml", "112": "1 l", "22": "SCENT", "221": "Lavender",
          "222": " Lemon "}
    for option in detail["options"]:
        option["translations"] = {"en": {"option_name": en[str(option["id"])]},
                                  "ar": {"option_name": option["name"]}}
        for value in option["values"]:
            value["translations"] = {"en": {"option_details_name": en[str(value["id"])]}}
    writes, skipped, unchanged = mapping.option_translation_payloads(p, detail)
    assert writes == [] and skipped == [] and unchanged == 6, (writes, skipped, unchanged)
    # one different English -> only that one written, the rest unchanged
    detail["options"][1]["values"][1]["translations"]["en"]["option_details_name"] = "Lime"
    writes, skipped, unchanged = mapping.option_translation_payloads(p, detail)
    assert [(k, i) for k, i, b in writes] == [("value", "222")] and unchanged == 5, writes
    # English under the WRONG key ("name" is ignored by Salla) is not "already set"
    detail = _fc_detail()
    detail["options"][0]["translations"] = {"en": {"name": "Size"}}
    detail["options"][0]["values"][0]["translations"] = {"en": {"option_name": "500 ml"}}
    writes, _, unchanged = mapping.option_translation_payloads(p, detail)
    assert unchanged == 0 and len(writes) == 6, (writes, unchanged)
    # garbage translations do not crash
    detail = _fc_detail()
    detail["options"][0]["translations"] = ["en"]
    detail["options"][1]["translations"] = {"en": "Scent"}
    writes, _, unchanged = mapping.option_translation_payloads(p, detail)
    assert unchanged == 0 and len(writes) == 6, writes


def test_option_translation_payloads_two_options_share_a_value_name():
    # "عادي" in both options with different English: each goes to ITS option's value id
    p = _overlap_product()
    writes, skipped, unchanged = mapping.option_translation_payloads(p, _OVERLAP_DETAIL)
    assert skipped == [] and unchanged == 0, skipped
    values = {i: b["translations"]["en"]["option_details_name"]
              for k, i, b in writes if k == "value"}
    assert values == {"113": "Regular", "221": "Lavender", "223": "Unscented"}, values
    assert [(k, i) for k, i, b in writes] == [
        ("option", "11"), ("value", "113"), ("option", "22"), ("value", "221"),
        ("value", "223")], writes
    _assert_option_bodies(writes)


def test_option_translation_payloads_no_detail_or_no_options():
    p = _p(_options_rows(), _FC)
    for detail in (None, {}, {"options": None}, {"options": "x"}, [], "garbage"):
        writes, skipped, unchanged = mapping.option_translation_payloads(p, detail)
        assert writes == [] and unchanged == 0, (detail, writes)
        assert skipped == [
            "option1 الحجم: not in the Salla product (its English names are not sent)",
            "option2 الرائحة: not in the Salla product (its English names are not sent)"], \
            (detail, skipped)
    # a simple product has no option English at all
    simple = _p(_simple_rows(), "fake-glass-cleaner")
    assert mapping.option_translation_payloads(simple, _fc_detail()) == ([], [], 0)
    assert mapping.option_english_names(simple) == ([], [])
    assert mapping.option_english_names(None) == ([], [])


def test_option_english_names_order_and_dedup():
    names, skipped = mapping.option_english_names(_p(_options_rows(), _FC))
    assert skipped == []
    assert names == [("option", 0, "الحجم", "Size"), ("value", 0, "500 مل", "500 ml"),
                     ("value", 0, "1 لتر", "1 L"), ("option", 1, "الرائحة", "Scent"),
                     ("value", 1, "لافندر", "Lavender"), ("value", 1, "ليمون", "Lemon")], names


def test_detail_option_name():
    d = _fc_detail()
    assert mapping.detail_option_name(d, "option", "11") == "الحجم"
    assert mapping.detail_option_name(d, "option", 22) == "الرائحة"      # int vs str id
    assert mapping.detail_option_name(d, "value", "222") == "ليمون"
    assert mapping.detail_option_name(d, "value", "11") is None         # option id as value
    assert mapping.detail_option_name(d, "option", "111") is None       # value id as option
    assert mapping.detail_option_name(d, "value", "999") is None
    for empty in (None, {}, {"options": None}, "garbage"):
        assert mapping.detail_option_name(empty, "option", "11") is None
    en = {"options": [{"id": 11, "name": {"ar": "الحجم", "en": "Size"},
                       "values": [{"id": 111, "name": {"ar": "500 مل", "en": "500 ml"}}]}]}
    assert mapping.detail_option_name(en, "option", "11", lang="en") == "Size"
    assert mapping.detail_option_name(en, "value", "111", lang="en") == "500 ml"
    assert mapping.detail_option_name(en, "value", "111") == "500 مل"   # default lang ar
    # a lang en read that answers a plain string: that string
    assert mapping.detail_option_name(_fc_detail(), "option", "11", lang="en") == "الحجم"


def _overlap_product():
    # the same AR value "عادي" in both options, with different EN names
    variants = [("عادي", "Regular", "لافندر", "Lavender", "30", "4"),
                ("عادي", "Regular", "عادي", "Unscented", "31", "6")]
    return _p(_options_rows(handle="fake-overlap", variants=variants, sku="FAKE-OV"),
              "fake-overlap")


_OVERLAP_DETAIL = {"id": 556, "options": [
    {"id": 11, "name": "الحجم", "values": [{"id": 113, "name": "عادي"}]},
    {"id": 22, "name": "الرائحة", "values": [{"id": 221, "name": "لافندر"},
                                             {"id": 223, "name": "عادي"}]}],
    "skus": [{"id": 9101, "sku": "", "related_option_values": [113, 221]},
             {"id": 9102, "sku": "", "related_option_values": [223, 113]}]}


# --- norm_label / match_variants -----------------------------------------------------------

def test_norm_label():
    n = mapping.norm_label
    assert n("  Size  ") == n("SIZE") == n("size") == "size"
    assert n("٥٠٠ مل") == n("500 مل") == "500 مل"              # Arabic-Indic digits
    assert n("۵۰۰ مل") == "500 مل"                             # Extended (Persian) digits
    assert n("500 ml") == n("500   ml") == n(" 500 ml\t") == "500 ml"
    assert n("Straße") == n("STRASSE")                          # casefold, not lower
    for empty in (None, "", "   ", " "):
        assert n(empty) == "", empty


def test_match_variants_set_match():
    p = _p(_options_rows(), _FC)
    detail = {"id": 555, "options": [
        {"id": 11, "name": "الحجم ", "values": [{"id": 111, "name": "٥٠٠ مل"},
                                                {"id": 112, "name": "1 لتر"}]},
        {"id": 22, "name": "الرائحة", "values": [{"id": 221, "name": "لافندر "},
                                                 {"id": 222, "name": "ليمون"}]}],
        "skus": [{"id": 9005, "sku": "", "related_option_values": [111]},   # partial
                 {"id": 9006, "sku": "", "related_option_values": [111, 221, 999]},
                 {"id": 9001, "sku": "", "related_option_values": [111, 221]},
                 {"id": 9002, "sku": "", "related_option_values": [222, 111]},  # order
                 {"id": 9003, "sku": "FAKE-OLD", "related_option_values": [112, 221]},
                 {"id": 9004, "sku": "", "related_option_values": [112, 222]}]}
    pairs, missing, extra = mapping.match_variants(p, detail)
    assert _pair_map(pairs) == {"FAKE-FC-001": "9001", "FAKE-FC-002": "9002",
                                "FAKE-FC-003": "9003", "FAKE-FC-004": "9004"}, pairs
    assert _row_numbers(missing) == [], missing
    assert sorted(str(x) for x in extra) == ["9005", "9006"], extra
    # Salla lacks one combination: that sheet row is missing, the rest still pair
    detail["skus"] = [s for s in detail["skus"] if s["id"] not in (9004, 9005, 9006)]
    pairs, missing, extra = mapping.match_variants(p, detail)
    assert set(_pair_map(pairs)) == {"FAKE-FC-001", "FAKE-FC-002", "FAKE-FC-003"}
    assert _row_numbers(missing) == [5], missing
    assert list(extra) == [], extra


def test_match_variants_same_value_in_two_options():
    pairs, missing, extra = mapping.match_variants(_overlap_product(), _OVERLAP_DETAIL)
    assert _pair_map(pairs) == {"FAKE-OV-001": "9101", "FAKE-OV-002": "9102"}, pairs
    assert not list(missing) and not list(extra)


def test_match_variants_id_types():
    """Salla ids may come as int in options[] and as str in related_option_values (or
    the reverse): a type mismatch must not turn every variant into "missing"."""
    p = _p(_options_rows(), _FC)
    detail = {"options": _DETAIL_OPTIONS,
              "skus": [{"id": 9001, "related_option_values": ["111", "221"]},
                       {"id": 9002, "related_option_values": ["111", "222"]},
                       {"id": "9003", "related_option_values": [112, 221]},
                       {"id": 9004, "related_option_values": ["112", 222]}]}
    pairs, missing, extra = mapping.match_variants(p, detail)
    assert len(pairs) == 4 and not list(missing) and not list(extra), (pairs, missing, extra)


def test_match_variants_simple_product():
    p = _p(_simple_rows(), "fake-glass-cleaner")
    for detail in ({"id": 1, "options": [], "skus": []},
                   {"id": 1, "skus": [{"id": 77, "sku": "FAKE-GC-001",
                                       "related_option_values": []}]}):
        pairs, missing, extra = mapping.match_variants(p, detail)
        assert list(pairs) == [] and list(missing) == [], (pairs, missing)


# --- variant_payload -------------------------------------------------------------------------

# Spec §4 lists sku, price, sale_price, stock_quantity, weight, barcode. weight_type is
# accepted ONLY next to weight: 500 g sent without its unit would read as 500 kg.
_VARIANT_KEYS = {"sku", "price", "sale_price", "stock_quantity", "weight", "weight_type",
                 "barcode"}


def _gc_variant(**over):
    return _p(_simple_rows(**over), "fake-glass-cleaner")["variants"][0]


def test_variant_payload_full():
    body = mapping.variant_payload(_gc_variant())
    assert set(body) <= _VARIANT_KEYS, body
    assert body["sku"] == "FAKE-GC-001" and body["price"] == 25 and body["sale_price"] == 20
    assert body["stock_quantity"] == 12 and body["weight"] == 0.5, body
    assert body["barcode"] == "000123456789", body
    assert body.get("weight_type", "kg") == "kg", body
    _assert_clean_body(body)
    body = mapping.variant_payload(_gc_variant(stock="", sale_price="", weight="",
                                               barcode=""))
    assert set(body) == {"sku", "price"}, body                 # blanks never sent
    body = mapping.variant_payload(_gc_variant(stock="0"))
    assert body["stock_quantity"] == 0, body
    body = mapping.variant_payload(_gc_variant(weight="500", weight_unit="g"))
    assert (body["weight"], body.get("weight_type")) in ((500, "g"), (0.5, "kg")), body


def test_variant_payload_diff_against_current():
    v = _gc_variant()
    current = {"id": 9001, "sku": "FAKE-GC-001",
               "price": {"amount": "25.00", "currency": "SAR"},
               "sale_price": {"amount": 20, "currency": "SAR"},
               "stock_quantity": 12, "weight": 0.5, "barcode": "000123456789",
               "cost_price": {"amount": 1, "currency": "SAR"}}
    assert mapping.variant_payload(v, current) == {}
    cur = dict(current, price=25)                              # plain number
    assert mapping.variant_payload(v, cur) == {}
    cur = dict(current, price={"amount": "24.99", "currency": "SAR"})
    assert mapping.variant_payload(v, cur) == {"price": 25}, mapping.variant_payload(v, cur)
    cur = dict(current, stock_quantity=3, barcode="123456789")   # zeros lost in Salla
    out = mapping.variant_payload(v, cur)
    assert out == {"stock_quantity": 12, "barcode": "000123456789"}, out
    cur = dict(current, sku="")                                # fresh Salla variant
    assert mapping.variant_payload(v, cur) == {"sku": "FAKE-GC-001"}
    # a weight change never travels without its unit
    out = mapping.variant_payload(v, dict(current, weight=0.4))
    assert out.get("weight") == 0.5 and out.get("weight_type", "kg") == "kg", out
    assert set(out) <= {"weight", "weight_type"}, out
    for cur in (current, dict(current, weight=0.4), dict(current, price=1)):
        out = mapping.variant_payload(v, cur)
        assert ("weight_type" in out) <= ("weight" in out), out
    # a blank sheet value is "not sent", never a diff (no clearing of Salla's value)
    v2 = _gc_variant(stock="", sale_price="")
    assert mapping.variant_payload(v2, dict(current, stock_quantity=99)) == {}
    for cur in (current, dict(current, price=1), None):
        assert "cost_price" not in mapping.variant_payload(v, cur)


# --- images_to_attach / translation_kept / detail helpers ---------------------------------

def test_images_to_attach():
    f = mapping.images_to_attach
    imgs = ["fake-1.jpg", "fake-2.jpg", "fake-3.webp"]
    zip_names = ["fake-1.jpg", "fake-2.jpg", "fake-3.webp"]
    assert f(imgs, 0, zip_names) == ([(1, "fake-1.jpg"), (2, "fake-2.jpg"),
                                      (3, "fake-3.webp")], None)
    assert f(imgs, 1, zip_names) == ([(2, "fake-2.jpg"), (3, "fake-3.webp")], None)
    assert f(imgs, 3, zip_names) == ([], None)
    assert f(imgs, 7, zip_names) == ([], None)
    # stops at the FIRST missing image (keeps the display order), never skips over it
    todo, reason = f(imgs, 0, ["fake-1.jpg", "fake-3.webp"])
    assert todo == [(1, "fake-1.jpg")], todo
    assert reason == "fake-2.jpg not in the ZIP", reason
    todo, reason = f(imgs, 0, ["fake-1.jpg", "fake-2.jpg"])
    assert todo == [(1, "fake-1.jpg"), (2, "fake-2.jpg")] and \
        reason == "fake-3.webp not in the ZIP", (todo, reason)
    # case-insensitive basename (ZIPs made on a Mac carry a folder)
    todo, reason = f(["FAKE-1.JPG", "fake-2.jpg"], 0,
                     ["Wave1/fake-1.jpg", "Wave1/FAKE-2.JPG", "__MACOSX/Wave1/._fake-2.jpg"])
    assert [(s, n.lower()) for s, n in todo] == [(1, "fake-1.jpg"), (2, "fake-2.jpg")] \
        and reason is None, (todo, reason)
    # never more than 10 in total
    many = ["fake-%d.jpg" % i for i in range(1, 13)]
    todo, reason = f(many, 0, many)
    assert [s for s, _ in todo] == list(range(1, 11)), todo
    todo, reason = f(many, 8, many)
    assert [s for s, _ in todo] == [9, 10], todo
    assert f(many, 10, many)[0] == []
    assert f([], 0, zip_names) == ([], None)


def test_translation_kept():
    k = mapping.translation_kept
    assert k("منظف زجاج تجريبي", "منظف زجاج تجريبي") is True
    assert k("منظف زجاج تجريبي", "Fake Glass Cleaner") is False   # EN wrote over AR
    assert k("منظف زجاج تجريبي", "") is False
    assert k("منظف زجاج تجريبي", None) is False


def test_english_kept():
    k = mapping.english_kept
    assert k("Cleaner", {"name": "Cleaner"}) is True
    assert k("Cleaner", {"name": {"ar": "منظف", "en": " cleaner "}}) is True
    assert k("Fake  Glass Cleaner", {"name": {"ar": "منظف", "en": "fake glass cleaner"}}) is True
    # spike 2026-11-10: the EN read still showed the Arabic string name
    assert k("Cleaner", {"name": "منظف"}) is False
    assert k("Cleaner", {"name": {"ar": "منظف", "en": "Other"}}) is False
    assert k("Cleaner", {"name": {"ar": "منظف"}}) is False
    assert k("Cleaner", {"name": {"ar": "منظف", "en": ""}}) is False
    assert k("Cleaner", {"name": None}) is False
    assert k("Cleaner", {}) is False
    assert k("Cleaner", None) is False
    assert k("Cleaner", "garbage") is False
    # an empty / blank name_en is never "kept", even against an empty name
    for empty in ("", "   ", None):
        assert k(empty, {"name": ""}) is False, empty
        assert k(empty, {"name": {"ar": "منظف", "en": ""}}) is False, empty
        assert k(empty, None) is False, empty


def test_translation_payload_ar_rewrite_texts_only():
    # The wizard re-writes the Arabic texts with translation_payload(product, "ar") when
    # English was not kept: texts only, never stock, price, weight, status or options.
    rows = _simple_rows(name_en="Fake Glass Cleaner", subtitle_ar="عنوان فرعي")
    p = _p(rows, "fake-glass-cleaner")
    tp = mapping.translation_payload(p, "ar")
    assert tp.get("name") == p["name"]["ar"], tp
    assert tp.get("description") == p["description"]["ar"], tp
    for key in ("quantity", "price", "sale_price", "cost_price", "weight", "weight_type",
                "status", "options", "images", "sku", "product_type", "categories"):
        assert key not in tp, (key, tp)
    for value in tp.values():
        assert "Fake Glass Cleaner" not in str(value), tp           # no EN in the AR body
    tp = mapping.translation_payload(_p(_options_rows(), _FC), "ar")
    assert "options" not in tp and "quantity" not in tp and "price" not in tp, tp


# --- image_size (header parser; Pillow on odoo.sh has no WebP) ----------------------------

# Real 64x64 lossy WebP given by the lead (spike 2026-11-10). NOTE: only the first 95 of
# the 286 bytes its RIFF header declares: the header is intact, the bitstream is cut. Good
# for the header parser (it never reads past the size); not a decodable image.
_WEBP_64_GIVEN_B64 = ("UklGRhYBAABXRUJQVlA4IAoBAACwCACdASpAAEAAPm0ylEakIqIhKBgMgA2JaQAIDgCF"
                      "//+H1iK37ZcbM8vyWwMN2Bwy4YJLXWYnYhw/YIz4qmXAAP7+HQf0vDAAAAA=")
# Complete 64x64 lossy WebP (110 bytes; Pillow 10.2 + dwebp decode it), same as the spike.
_WEBP_64_FULL_B64 = ("UklGRmYAAABXRUJQVlA4IFoAAADQBACdASpAAEAAPrFInUmnJCKhMegA4BYJQBpGIAaADeAA"
                     "P7BD0SV+jyHoTKG4AAD+4Ll+q8QZmA/ys1sxthqyRlG2r//9zfv7fOA6l6N7S38Vw7NxDQAA"
                     "AAA=")
# Real Pillow 10.2 files, 37x23 (not square: a swapped width/height shows).
_REAL_37x23_B64 = {
    "webp-lossless": "UklGRh4AAABXRUJQVlA4TBEAAAAvJIAFAAdQ5CrUo/+BiOh/AAA=",
    "webp-alpha-vp8x": ("UklGRmwAAABXRUJQVlA4WAoAAAAQAAAAJAAAFgAAQUxQSAoAAAABB1DAiAhERP8DVlA4"
                        "IDwAAABQAwCdASolABcAPtFmrVEoJaQipWgBABoJZwDKpoAANa6r6AD+Ex/jebzWtb//"
                        "FodaHWhtjoTKuJ+uAAA="),
    "png": ("iVBORw0KGgoAAAANSUhEUgAAACUAAAAXCAIAAAADThHyAAAAKElEQVR4nGPkOiHHQEfARE/LRu0b"
            "tW/UvlH7Ru0btW/UvlH7Ru0jCABfIgEe+UCBwwAAAABJRU5ErkJggg=="),
    "jpeg-progressive-exif": (
        "/9j/4AAQSkZJRgABAQAAAQABAAD/4QASRXhpZgAATU0AKgAAAAgAAP/bAEMAGxIUFxQRGxcWFx4cGyAo"
        "QisoJSUoUTo9MEJgVWVkX1VdW2p4mYFqcZBzW12FtYaQnqOrratngLzJuqbHmairpP/bAEMBHB4eKCMo"
        "TisrTqRuXW6kpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpP/C"
        "ABEIABcAJQMBIgACEQEDEQH/xAAVAAEBAAAAAAAAAAAAAAAAAAAAAf/EABUBAQEAAAAAAAAAAAAAAAAA"
        "AAAE/9oADAMBAAIQAxAAAAGCeMAAAAD/xAAUEAEAAAAAAAAAAAAAAAAAAAAw/9oACAEBAAEFAn//xAAU"
        "EQEAAAAAAAAAAAAAAAAAAAAg/9oACAEDAQE/AV//xAAUEQEAAAAAAAAAAAAAAAAAAAAg/9oACAECAQE/"
        "AV//xAAUEAEAAAAAAAAAAAAAAAAAAAAw/9oACAEBAAY/An//xAAUEAEAAAAAAAAAAAAAAAAAAAAw/9oA"
        "CAEBAAE/IX//2gAMAwEAAgADAAAAEP8A/wD/AP8A/wD/xAAUEQEAAAAAAAAAAAAAAAAAAAAg/9oACAED"
        "AQE/EF//xAAUEQEAAAAAAAAAAAAAAAAAAAAg/9oACAECAQE/EF//xAAUEAEAAAAAAAAAAAAAAAAAAAAw"
        "/9oACAEBAAE/EH//2Q=="),
}


def _b64(value):
    import base64
    return base64.b64decode(value)


def _png(width, height, chunk=b"IHDR"):
    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"
    return (b"\x89PNG\r\n\x1a\n" + len(ihdr).to_bytes(4, "big") + chunk + ihdr
            + b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00IEND\xaeB`\x82")


def _jpeg_segment(marker, payload):
    return bytes([0xFF, marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


def _jpeg_sof(width, height, marker=0xC0):
    # precision 8, height, width, 3 components (id, sampling, quant table)
    payload = (b"\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big")
               + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01")
    return _jpeg_segment(marker, payload)


_JFIF_APP0 = _jpeg_segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
_DQT = _jpeg_segment(0xDB, b"\x00" + bytes(range(1, 65)))
_DHT = _jpeg_segment(0xC4, b"\x00" + b"\x00" * 16)       # C4 = DHT, not a frame
_SOS = _jpeg_segment(0xDA, b"\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00") + b"\x12\x34"


def _jpeg(width, height, marker=0xC0, exif_bytes=40, before_sof=b""):
    app1 = _jpeg_segment(0xE1, b"Exif\x00\x00" + b"MM\x00*" + b"\x00" * exif_bytes)
    return (b"\xff\xd8" + _JFIF_APP0 + app1 + _DQT + before_sof
            + _jpeg_sof(width, height, marker) + _DHT + _SOS + b"\xff\xd9")


def _riff_webp(chunk, body):
    payload = b"WEBP" + chunk + len(body).to_bytes(4, "little") + body
    return b"RIFF" + len(payload).to_bytes(4, "little") + payload


def _webp_vp8(width, height, start=b"\x9d\x01\x2a", scale=0):
    w = (width & 0x3FFF) | (scale << 14)
    h = (height & 0x3FFF) | (scale << 14)
    return _riff_webp(b"VP8 ", b"\x30\x01\x00" + start + w.to_bytes(2, "little")
                      + h.to_bytes(2, "little") + b"\x00" * 8)


def _webp_vp8l(width, height, signature=0x2F):
    bits = (width - 1) | ((height - 1) << 14) | (1 << 28)     # alpha_is_used, version 0
    return _riff_webp(b"VP8L", bytes([signature]) + bits.to_bytes(4, "little") + b"\x00" * 4)


def _webp_vp8x(width, height):
    body = (b"\x10\x00\x00\x00" + (width - 1).to_bytes(3, "little")
            + (height - 1).to_bytes(3, "little"))
    return _riff_webp(b"VP8X", body)


def test_image_size_real_webp_64():
    f = mapping.image_size
    given = _b64(_WEBP_64_GIVEN_B64)
    assert given[:4] == b"RIFF" and given[12:16] == b"VP8 "
    assert f(given) == (64, 64), f(given)
    full = _b64(_WEBP_64_FULL_B64)
    assert int.from_bytes(full[4:8], "little") + 8 == len(full)      # complete file
    assert f(full) == (64, 64), f(full)
    assert f(bytearray(full)) == (64, 64)


def test_image_size_real_files_not_square():
    for kind, b64 in _REAL_37x23_B64.items():
        assert mapping.image_size(_b64(b64)) == (37, 23), (kind, mapping.image_size(_b64(b64)))


def test_image_size_png():
    f = mapping.image_size
    assert f(_png(1000, 1000)) == (1000, 1000)
    assert f(_png(300, 200)) == (300, 200)                # width first, height second
    assert f(_png(1, 70000)) == (1, 70000)
    assert f(_png(300, 200)[:24]) == (300, 200)           # header only is enough
    for bad in (_png(300, 200)[:23], _png(300, 200)[:16], _png(300, 200, chunk=b"IDAT"),
                _png(0, 200), _png(300, 0), b"\x89PNG\r\n\x1a\n"):
        assert f(bad) is None, bad


def test_image_size_jpeg_baseline_and_progressive():
    f = mapping.image_size
    assert f(_jpeg(1000, 1000)) == (1000, 1000)
    assert f(_jpeg(300, 200)) == (300, 200)               # SOF stores height before width
    assert f(_jpeg(300, 200, marker=0xC2)) == (300, 200)  # progressive
    for marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE,
                   0xCF):
        assert f(_jpeg(640, 480, marker=marker)) == (640, 480), hex(marker)
    # DHT (C4), JPG (C8), DAC (CC) before the frame are skipped, never read as a size
    junk = _jpeg_segment(0xC8, b"\x00\x05\x00\x07\x00") + _jpeg_segment(0xCC, b"\x01\x02\x03")
    assert f(_jpeg(640, 480, before_sof=_DHT + junk)) == (640, 480)
    # 0xFF fill bytes before a marker, and markers without a length (RSTn / TEM)
    assert f(_jpeg(640, 480, before_sof=b"\xff\xff\xff")) == (640, 480)
    assert f(_jpeg(640, 480, before_sof=b"\xff\xd0\xff\x01")) == (640, 480)


def test_image_size_jpeg_big_exif_and_64kb_header():
    # The wizard reads 64 KB first and the whole entry only for a JPEG without a SOF in
    # them: the parser must answer None (not a wrong size) on the cut header.
    f = mapping.image_size
    big = _jpeg(1000, 1000, exif_bytes=40 * 1024)
    assert len(big) < 65536 and f(big[:65536]) == (1000, 1000)
    app1s = b"".join(_jpeg_segment(0xE1, b"\x00" * 60000) for _ in range(2))
    late = _jpeg(1000, 1000, before_sof=app1s)            # SOF ~120 KB in
    assert len(late) > 65536
    assert f(late[:65536]) is None
    assert f(late) == (1000, 1000)


def test_image_size_jpeg_none_cases():
    f = mapping.image_size
    good = _jpeg(640, 480)
    sof_at = good.index(b"\xff\xc0")
    # cut inside the APP1, right before the SOF, inside the SOF length, before width
    for cut in (4, 20, sof_at, sof_at + 1, sof_at + 3, sof_at + 7, sof_at + 8):
        assert f(good[:cut]) is None, cut
    assert f(good[:sof_at + 9]) == (640, 480)             # FFC0 + len + prec + h + w
    # SOS or EOI before any SOF
    assert f(b"\xff\xd8" + _JFIF_APP0 + _SOS + _jpeg_sof(640, 480)) is None
    assert f(b"\xff\xd8" + _JFIF_APP0 + b"\xff\xd9" + _jpeg_sof(640, 480)) is None
    # garbage where a marker should be, a length below 2, zero sizes
    assert f(b"\xff\xd8" + _JFIF_APP0 + b"\x00\x01" + _jpeg_sof(640, 480)) is None
    assert f(b"\xff\xd8\xff\xe0\x00\x01" + _jpeg_sof(640, 480)) is None
    assert f(_jpeg(0, 480)) is None and f(_jpeg(640, 0)) is None
    assert f(b"\xff\xd8") is None and f(b"\xff\xd8\xff") is None


def test_image_size_webp_vp8_lossy():
    f = mapping.image_size
    assert f(_webp_vp8(1000, 1000)) == (1000, 1000)
    assert f(_webp_vp8(300, 200)) == (300, 200)
    assert f(_webp_vp8(16383, 1)) == (16383, 1)
    assert f(_webp_vp8(300, 200, scale=3)) == (300, 200)  # top 2 bits = scaling, masked
    assert f(_webp_vp8(300, 200)[:30]) == (300, 200)      # header only
    for bad in (_webp_vp8(300, 200)[:29], _webp_vp8(300, 200, start=b"\x00\x00\x00"),
                _webp_vp8(0, 200), _webp_vp8(300, 0)):
        assert f(bad) is None, bad


def test_image_size_webp_vp8l_lossless():
    f = mapping.image_size
    assert f(_webp_vp8l(1000, 1000)) == (1000, 1000)
    assert f(_webp_vp8l(300, 200)) == (300, 200)
    assert f(_webp_vp8l(1, 1)) == (1, 1)
    assert f(_webp_vp8l(16384, 16384)) == (16384, 16384)  # 14 bits of size - 1
    assert f(_webp_vp8l(300, 200)[:25]) == (300, 200)
    for bad in (_webp_vp8l(300, 200)[:24], _webp_vp8l(300, 200, signature=0x2E)):
        assert f(bad) is None, bad


def test_image_size_webp_vp8x_extended():
    f = mapping.image_size
    assert f(_webp_vp8x(1000, 1000)) == (1000, 1000)
    assert f(_webp_vp8x(300, 200)) == (300, 200)
    assert f(_webp_vp8x(1, 1)) == (1, 1)
    assert f(_webp_vp8x(1 << 24, 70000)) == (1 << 24, 70000)   # 24 bits of size - 1
    assert f(_webp_vp8x(300, 200)[:30]) == (300, 200)
    assert f(_webp_vp8x(300, 200)[:29]) is None


def test_image_size_unknown_or_garbage_is_none():
    f = mapping.image_size
    gif = b"GIF89a" + (64).to_bytes(2, "little") * 2 + b"\x00" * 20
    for bad in (gif, b"", None, "not bytes", 12345, b"\x00" * 64, b"RIFF",
                b"RIFF\x10\x00\x00\x00WAVEfmt " + b"\x00" * 20,          # RIFF, not WEBP
                _riff_webp(b"ALPH", b"\x00" * 16),                       # unknown chunk
                b"BM" + b"\x00" * 60, b"%PDF-1.4" + b"\x00" * 40,
                b"\x89PNG", b"RIFF\x00\x00\x00\x00WEBP", b"RIFF\x00\x00\x00\x00WEBPVP8 "):
        assert f(bad) is None, bad


def test_detail_helpers():
    detail = {"id": 555, "status": "hidden",
              "images": [{"id": 1, "url": "https://cdn.example.test/fake-1.jpg"},
                         {"id": 2, "url": "https://cdn.example.test/fake-2.jpg"}]}
    assert len(mapping.detail_images(detail)) == 2
    assert mapping.detail_status(detail) == "hidden"
    assert str(mapping.detail_id(detail)) == "555"
    for empty in (None, {}, {"images": None}, "garbage"):
        assert list(mapping.detail_images(empty)) == [], empty
    assert mapping.detail_status(None) in ("", None)
    assert not mapping.detail_id(None)


# --- F8 review round 4: English overwrite guard -----------------------------------------

def test_translation_skip_reason():
    r = mapping.translation_skip_reason
    # nothing to send -> nothing to skip
    p = _p(_simple_rows(name_en="", description_en=""), "fake-glass-cleaner")
    assert mapping.translation_payload(p, "en") == {}
    assert r(p) == "" and r(p, "en") == ""
    # EN description (or any EN text) without name_en: the overwrite would go unseen
    p = _p(_simple_rows(name_en=""), "fake-glass-cleaner")
    assert r(p) == "name_en is empty", r(p)
    p = _p(_simple_rows(name_en="", description_en="", subtitle_en="Fake subtitle",
                        subtitle_ar="عنوان فرعي"), "fake-glass-cleaner")
    assert r(p) == "name_en is empty", r(p)
    # an EN text whose AR side is blank is never sent (translation_dropped): alone, it
    # leaves nothing to send, so nothing to skip either
    p = _p(_simple_rows(name_en="", description_en="", subtitle_en="Fake subtitle"),
           "fake-glass-cleaner")
    assert mapping.translation_payload(p, "en") == {} and r(p) == "", r(p)
    # same name on both sides once normalized (case, inner/outer spaces)
    p = _p(_simple_rows(name_ar="WAYAKIT Gel", name_en=" wayakit  gel"), "fake-glass-cleaner")
    assert r(p) == "name_en is the same as name_ar", r(p)
    p = _p(_simple_rows(name_ar="FAKE Gel ٥٠٠", name_en="fake gel 500"), "fake-glass-cleaner")
    assert r(p) == "name_en is the same as name_ar", r(p)     # Arabic-Indic digits too
    # a real translation -> sent
    p = _p(_simple_rows(), "fake-glass-cleaner")
    assert r(p) == "", r(p)
    p = _p(_simple_rows(name_ar="WAYAKIT Gel", name_en="WAYAKIT Gel 2"), "fake-glass-cleaner")
    assert r(p) == ""
    p = _p(_options_rows(), _FC)
    assert r(p) == ""
    # tolerant of a hand-built product
    assert r({}) == ""
    assert r({"name": None, "description": {"ar": "<p>وصف</p>", "en": "<p>Fake</p>"}}) \
        == "name_en is empty"
    assert r({"name": None, "description": {"en": "<p>Fake</p>"}}) == ""    # AR blank: not sent


# --- F8 review round 4: weight units ------------------------------------------------------

def test_weight_kg():
    w = mapping.weight_kg
    assert abs(w("600", "G") - 0.6) < 1e-9, w("600", "G")
    assert abs(w(600, "g") - 0.6) < 1e-9
    assert w(1, "") == 1.0 and w(1, None) == 1.0 and w("1.5", "KG") == 1.5
    assert w({"amount": "2", "currency": "x"}, "kg") == 2.0           # through money()
    assert abs(w("1,250", "g") - 1.25) < 1e-9
    assert w(1, "lb") is None and w(1, "oz") is None
    for bad in (None, "", "abc", "inf", True, [1]):
        assert w(bad, "kg") is None, bad


def test_same_weight():
    s = mapping.same_weight
    assert s(600, "g", 0.6, "kg") is True
    assert s(600, "g", 600, "") is True                       # no Salla unit: raw numbers
    assert s(600, "g", "600.00", None) is True
    assert s(0.6, "kg", 600, "g") is True
    assert s(0.5, "kg", "500", "G") is True                   # unit case-insensitive
    assert s(0.5, "kg", 0.5, " kg ") is True
    assert s(600, "g", 0.6004, "kg") is True                  # within half a gram
    assert s(600, "g", 0.601, "kg") is False                  # one gram off
    assert s(600, "g", 0.6, "") is False                      # no unit: 600 != 0.6
    assert s(1, "kg", 2.2, "lb") is False                     # unknown unit never same
    assert s(1, "kg", None, "kg") is False
    assert s(1, "kg", "abc", "kg") is False
    assert s(1, "lb", 1, "kg") is False
    assert s(0.5, "kg", 0.5, "g") is False                    # 500 g vs 0.5 g
    # no unit and nothing on either side: same as _same_number (both unset)
    assert s(None, "kg", None, "") is True
    assert s(1, "kg", None, "") is False


def test_variant_payload_weight_units():
    v = _gc_variant(weight="600", weight_unit="g")       # stock 12, sale 20 < price 25
    base = {"id": 9001, "sku": "FAKE-GC-001", "price": {"amount": "25.00", "currency": "SAR"},
            "sale_price": {"amount": 20, "currency": "SAR"}, "stock_quantity": 12,
            "barcode": "000123456789"}
    vp = mapping.variant_payload
    # Salla echoes 600 g as 0.6 kg: no change, no endless resend
    assert vp(v, dict(base, weight=0.6, weight_type="kg")) == {}
    assert vp(v, dict(base, weight="0.60", weight_type="KG")) == {}
    assert vp(v, dict(base, weight=600, weight_type="g")) == {}
    assert vp(v, dict(base, weight=600)) == {}                 # no unit: raw numbers
    # a real change: weight and the SHEET unit together
    out = vp(v, dict(base, weight=0.5, weight_type="kg"))
    assert out == {"weight": 600, "weight_type": "g"}, out
    out = vp(v, dict(base, weight=0.6))                       # no unit, 600 != 0.6
    assert out == {"weight": 600, "weight_type": "g"}, out
    out = vp(v, dict(base, weight=1.3, weight_type="lb"))     # unknown unit
    assert out == {"weight": 600, "weight_type": "g"}, out
    # weight_type alone differing is not a change (0.5 kg sheet, Salla 0.5 with g/none)
    v2 = _gc_variant()                                        # 0.5 kg
    assert vp(v2, dict(base, weight=0.5)) == {}
    assert vp(v2, dict(base, weight=500, weight_type="g")) == {}
    out = vp(v2, dict(base, weight=0.5, weight_type="g"))      # 0.5 g is not 0.5 kg
    assert out == {"weight": 0.5, "weight_type": "kg"}, out
    # other diffs still reported next to an equal weight; weight never leaks in
    out = vp(v, dict(base, weight=0.6, weight_type="kg", stock_quantity=3,
                     sale_price={"amount": 19, "currency": "SAR"}))
    assert out == {"stock_quantity": 12, "sale_price": 20}, out
    # a blank sheet weight is never sent, whatever Salla holds
    v3 = _gc_variant(weight="", weight_unit="")
    assert vp(v3, dict(base, weight=7, weight_type="lb")) == {}
    for cur in (dict(base, weight=0.6, weight_type="kg"), dict(base, weight=0.4),
                dict(base, weight=0.6, weight_type="kg", price=1)):
        out = vp(v, cur)
        assert ("weight" in out) == ("weight_type" in out), out


def _gc_detail(**over):
    detail = {"id": 555, "price": {"amount": "25.00", "currency": "SAR"},
              "sale_price": {"amount": 20, "currency": "SAR"}, "quantity": 12,
              "weight": 0.6, "weight_type": "kg", "options": [], "skus": []}
    detail.update(over)
    return detail


def test_simple_changes_weight_units():
    sc = mapping.simple_changes
    p = _p(_simple_rows(weight="600", weight_unit="g"), "fake-glass-cleaner")
    assert sc(p, _gc_detail()) == []
    assert sc(p, _gc_detail(weight=600, weight_type="g")) == []
    assert sc(p, _gc_detail(weight=600, weight_type=None)) == []
    out = sc(p, _gc_detail(weight=0.5))
    assert out == [("weight", "600 g", "0.5 kg")], out
    out = sc(p, _gc_detail(weight=0.6, weight_type=""))
    assert out == [("weight", "600 g", 0.6)], out
    out = sc(p, _gc_detail(weight=1.3, weight_type="lb"))
    assert out == [("weight", "600 g", "1.3 lb")], out
    out = sc(p, _gc_detail(weight=None, weight_type="kg"))      # Salla has no weight
    assert out == [("weight", "600 g", "")], out
    # no separate weight_unit entry, ever
    for d in (_gc_detail(), _gc_detail(weight=600, weight_type="g"), _gc_detail(weight=1)):
        assert all(c[0] != "weight_unit" for c in sc(p, d)), sc(p, d)
    # Salla number shown as a person reads it: 2.0 -> 2
    p_kg = _p(_simple_rows(weight="1", weight_unit="kg"), "fake-glass-cleaner")
    out = sc(p_kg, _gc_detail(weight=2.0, weight_type="kg"))
    assert out == [("weight", "1 kg", "2 kg")], out
    assert sc(p_kg, _gc_detail(weight=1000, weight_type="g")) == []
    # multi-unit stock and a non-zero discount: the other changes next to the weight
    out = sc(p, _gc_detail(quantity=3, sale_price={"amount": "18.00", "currency": "SAR"},
                           price={"amount": "24.00", "currency": "SAR"}, weight=0.5))
    assert out == [("price", 25, 24), ("sale_price", 20, 18), ("stock", 12, 3),
                   ("weight", "600 g", "0.5 kg")], out
    # blank sheet weight is not sent -> not a change
    p_none = _p(_simple_rows(weight="", weight_unit=""), "fake-glass-cleaner")
    assert sc(p_none, _gc_detail(weight=9, weight_type="lb")) == []
    # products with options keep their values on the variants
    assert sc(_p(_options_rows(), _FC), _gc_detail(weight=9)) == []


def test_shown():
    sh = mapping._shown
    assert sh(12.0) == 12 and isinstance(sh(12.0), int)
    assert sh("12.50") == 12.5 and sh({"amount": "7.00", "currency": "SAR"}) == 7
    for bad in (None, "", "abc", True):
        assert sh(bad) == "", bad


# --- F8 review round 4: parent SKU against the file's SKUs -------------------------------

def test_parse_parent_sku_collides_with_other_product_sku():
    # options product fake-floor-cleaner -> parent SKU FAKE-FLOOR-CLEANER; a simple
    # product further down owns that SKU in lower case (a Salla search may ignore case)
    other = _simple_rows(sku="fake-floor-cleaner")
    other[0]["_row"] = 6
    rows = _options_rows() + other
    by, errors, _, products = _parse(rows)
    fc, gc = by[_FC], by["fake-glass-cleaner"]
    assert fc["parent_sku"] == "FAKE-FLOOR-CLEANER", fc["parent_sku"]
    want_fc = ("Row 2: the Salla parent SKU of fake-floor-cleaner is its handle in capitals "
               "(FAKE-FLOOR-CLEANER), which is also the SKU of row 6: rename the handle or "
               "that SKU")
    want_gc = ("Row 6: SKU equals the parent SKU of handle fake-floor-cleaner (its handle "
               "in capitals): rename the handle or this SKU")
    assert want_fc in fc["errors"], fc["errors"]
    assert want_gc in gc["errors"], gc["errors"]
    assert want_fc in errors and want_gc in errors, errors
    assert errors.count(want_fc) == 1 and errors.count(want_gc) == 1, errors
    # the simple product listed FIRST: same result
    other[0]["_row"] = 2
    rows = other + _options_rows(start=3)
    by, errors, _, products = _parse(rows)
    assert _blocked(by, _FC) and _blocked(by, "fake-glass-cleaner")
    assert _err_at(by[_FC]["errors"], (3,)) and _err_at(by["fake-glass-cleaner"]["errors"], (2,))
    assert any("row 2" in m for m in by[_FC]["errors"]), by[_FC]["errors"]


def test_parse_parent_sku_collides_with_own_variant_sku():
    rows = _options_rows()
    rows[2]["sku"] = "Fake-Floor-Cleaner"                   # row 4, its own variant
    by, errors, _, products = _parse(rows)
    fc = by[_FC]
    want = ("Row 2: the Salla parent SKU of fake-floor-cleaner is its handle in capitals "
            "(FAKE-FLOOR-CLEANER), which is also the SKU of row 4: rename the handle or "
            "that SKU")
    assert want in fc["errors"] and want in errors, (fc["errors"], errors)
    # one error, not a second "SKU equals" on the same product
    assert not any("SKU equals the parent SKU" in m for m in fc["errors"]), fc["errors"]
    assert len([m for m in errors if "parent SKU" in m]) == 1, errors


def test_parse_parent_sku_no_false_positive():
    # a simple product's parent SKU is its own variant SKU: never a collision with itself
    by, errors, _, products = _parse(_simple_rows(sku="FAKE-GLASS-CLEANER"))
    assert not _all_errors(products, errors), _all_errors(products, errors)
    # a simple product whose HANDLE in capitals is another product's SKU: no rule
    rows = _simple_rows() + [_r(3, handle="fake-two", sku="FAKE-GLASS-CLEANER",
                                name_ar="اسم تجريبي", description_ar="وصف تجريبي",
                                price="10", stock="2")]
    by, errors, _, products = _parse(rows)
    assert not _all_errors(products, errors), _all_errors(products, errors)
    # options product next to an unrelated SKU that only CONTAINS the handle
    rows = _options_rows() + _simple_rows(sku="FAKE-FLOOR-CLEANER-2")
    rows[-1]["_row"] = 6
    by, errors, _, products = _parse(rows)
    assert not _all_errors(products, errors), _all_errors(products, errors)


# --- F8 review round 4: the same image twice on one handle -------------------------------

def test_parse_duplicate_image_names():
    by, errors, _, products = _parse(_simple_rows(image_1="fake-a.jpg", image_2="fake-b.jpg",
                                                  image_3="dir/FAKE-A.JPG"))
    want = "Row 2: image_3 repeats image_1 (dir/FAKE-A.JPG)"
    assert want in by["fake-glass-cleaner"]["errors"] and want in errors, errors
    by, errors, _, products = _parse(_simple_rows(image_2="fake-a.jpg",
                                                  image_5="C:\\photos\\Fake-A.jpg"))
    want = "Row 2: image_5 repeats image_2 (C:\\photos\\Fake-A.jpg)"
    assert want in errors, errors
    # exact same name, and a third copy reported against the FIRST one
    by, errors, _, products = _parse(_simple_rows(image_1="fake-a.jpg", image_2="fake-a.jpg",
                                                  image_4="fake-a.jpg"))
    assert "Row 2: image_2 repeats image_1 (fake-a.jpg)" in errors, errors
    assert "Row 2: image_4 repeats image_1 (fake-a.jpg)" in errors, errors
    # on a product with options too (images live on its first row)
    rows = _options_rows()
    rows[0]["image_2"] = "FAKE-FC-1.jpg"
    by, errors, _, products = _parse(rows)
    assert "Row 2: image_2 repeats image_1 (FAKE-FC-1.jpg)" in by[_FC]["errors"], errors
    # not duplicates: other extension, other name, same file on two different handles
    p = _p(_simple_rows(image_1="fake-a.jpg", image_2="fake-a.jpeg", image_3="fake-a2.jpg",
                        image_4="dir/fake-b.jpg"), "fake-glass-cleaner")
    assert p["images"] == ["fake-a.jpg", "fake-a.jpeg", "fake-a2.jpg", "dir/fake-b.jpg"]
    rows = _simple_rows(image_1="fake-shared.jpg") + [
        _r(3, handle="fake-two", sku="FAKE-TWO-001", name_ar="اسم تجريبي",
           description_ar="وصف تجريبي", price="10", stock="2", image_1="fake-shared.jpg")]
    by, errors, _, products = _parse(rows)
    assert not _all_errors(products, errors), _all_errors(products, errors)


# --- F8 review round 4: unreadable CSV / encrypted xlsx -> ValueError -------------------

def test_read_csv_rows_unclosed_quote_value_error():
    # an unclosed quote swallows the rest of the file into one field over the csv
    # module's limit (131072): a readable ValueError, never a raw csv.Error
    big = "x" * 140000
    data = ("handle,sku,name_ar,description_ar,price\n"
            'fake-csv,FAKE-CSV-001,"منظف تجريبي,وصف,25\n' + big + "\nmore,rows\n"
            ).encode("utf-8")
    for call in (lambda: mapping.read_csv_rows(data),
                 lambda: mapping.read_upload_rows(data, "products.csv")):
        try:
            call()
        except ValueError as e:
            msg = str(e)
            assert msg.startswith("The CSV file cannot be read near line "), msg
            assert "quote that is never closed" in msg and ".xlsx" in msg, msg
            assert "line 2 " in msg or re.search(r"near line \d+ \(", msg), msg
            continue
        except Exception as e:
            raise AssertionError("raised %s, not ValueError: %s" % (type(e).__name__, e))
        raise AssertionError("no error for an unclosed quote over the field limit")
    # the test never moved the global limit
    import csv
    assert csv.field_size_limit() == 131072, csv.field_size_limit()
    # a long but closed cell under the limit still reads
    ok = ('handle,sku,description_ar\nfake-csv,FAKE-CSV-001,"%s"\n' % ("y" * 100000)
          ).encode("utf-8")
    rows = mapping.read_csv_rows(ok)
    assert len(rows) == 1 and len(rows[0]["description_ar"]) == 100000


def _patch_zip(raw, flag_bits=0, method=None, only=None):
    """Rewrite the local AND central headers of a ZIP: OR `flag_bits` into the
    general-purpose flag, set the compression `method`. `only`: entry names to touch
    (default all). Offsets come from the central directory, not a byte search."""
    import struct
    data = bytearray(raw)
    eocd = data.rfind(b"PK\x05\x06")
    count = struct.unpack_from("<H", data, eocd + 10)[0]
    pos = struct.unpack_from("<I", data, eocd + 16)[0]
    for _ in range(count):
        assert data[pos:pos + 4] == b"PK\x01\x02", "central header expected"
        n_len, e_len, c_len = struct.unpack_from("<HHH", data, pos + 28)
        local = struct.unpack_from("<I", data, pos + 42)[0]
        name = bytes(data[pos + 46:pos + 46 + n_len]).decode("utf-8")
        assert data[local:local + 4] == b"PK\x03\x04", "local header expected"
        if only is None or name in only:
            for flag_at in (pos + 8, local + 6):
                value = struct.unpack_from("<H", data, flag_at)[0]
                struct.pack_into("<H", data, flag_at, value | flag_bits)
            if method is not None:
                for method_at in (pos + 10, local + 8):
                    struct.pack_into("<H", data, method_at, method)
        pos += 46 + n_len + e_len + c_len
    return bytes(data)


def test_read_xlsx_rows_encrypted_entry_value_error():
    good = _xlsx([("Products", _simple_sheet())])
    assert set(_by_sku(mapping.read_xlsx_rows(good))) == {"FAKE-XL-001"}
    assert set(_by_sku(mapping.read_xlsx_rows(_patch_zip(good)))) == {"FAKE-XL-001"}
    cases = [(_patch_zip(good, flag_bits=1), "xl/workbook.xml"),
             (_patch_zip(good, flag_bits=1, only={"xl/worksheets/sheet1.xml"}),
              "xl/worksheets/sheet1.xml")]
    for raw, part in cases:
        for call in (lambda: mapping.read_xlsx_rows(raw),
                     lambda: mapping.read_upload_rows(raw, "products.xlsx"),
                     lambda: mapping.read_upload_rows(raw, "upload.csv")):
            try:
                call()
            except ValueError as e:
                want = ("The .xlsx file is encrypted (%s): save it without a password."
                        % part)
                assert str(e) == want, (str(e), want)
                continue
            except Exception as e:      # RuntimeError must be wrapped
                raise AssertionError("raised %s, not ValueError: %s" % (
                    type(e).__name__, e))
            raise AssertionError("no error for an encrypted entry (%s)" % part)


def test_read_xlsx_rows_unsupported_compression_is_damaged():
    # NotImplementedError is a RuntimeError subclass: still "damaged", not "encrypted"
    assert issubclass(NotImplementedError, RuntimeError)
    good = _xlsx([("Products", _simple_sheet())])
    raw = _patch_zip(good, method=99, only={"xl/worksheets/sheet1.xml"})
    try:
        mapping.read_xlsx_rows(raw)
    except ValueError as e:
        assert str(e) == "The .xlsx file is damaged (xl/worksheets/sheet1.xml).", str(e)
    except Exception as e:
        raise AssertionError("raised %s, not ValueError: %s" % (type(e).__name__, e))
    else:
        raise AssertionError("no error for an unsupported compression method")


# --- F8 spike follow-up: EN text without AR, hidden markers, stock publishes ---------------

def test_translation_payload_drops_en_without_ar():
    # Review S2: the Arabic re-write / restore sends translation_payload(product, "ar");
    # an EN key it does not carry (AR blank) could stay over the Arabic in Salla, so it is
    # never sent in English, and translation_dropped names its column.
    rows = _simple_rows(subtitle_en="Fake subtitle", promotion_title_en="Fake promo",
                        seo_title_en="Fake SEO", seo_description_en="Fake SEO copy",
                        seo_title_ar="عنوان سيو")
    p = _p(rows, "fake-glass-cleaner")
    tp = mapping.translation_payload(p, "en")
    assert tp == {"name": "Fake Glass Cleaner", "description": p["description"]["en"],
                  "metadata_title": "Fake SEO"}, tp
    assert "subtitle" not in tp and "promotion_title" not in tp, tp
    assert "metadata_description" not in tp, tp
    assert mapping.translation_dropped(p) == ["subtitle_en", "promotion_title_en",
                                              "seo_description_en"], \
        mapping.translation_dropped(p)
    assert mapping.translation_dropped(p, "en") == mapping.translation_dropped(p)
    # every EN key has its AR counterpart in the re-write body
    ar = mapping.translation_payload(p, "ar")
    assert set(tp) <= set(ar), (tp, ar)
    # AR side: never filtered, nothing dropped
    assert mapping.translation_dropped(p, "ar") == []
    assert ar["name"] == p["name"]["ar"] and ar["metadata_title"] == "عنوان سيو", ar
    # AR blank, EN blank -> neither sent nor dropped
    p = _p(_simple_rows(), "fake-glass-cleaner")
    assert mapping.translation_dropped(p) == []
    assert set(mapping.translation_payload(p, "en")) == {"name", "description"}
    # options product: subtitle_en alone on the first row
    rows = _options_rows()
    rows[0].update(subtitle_en="Fake subtitle")
    p = _p(rows, _FC)
    assert "subtitle" not in mapping.translation_payload(p, "en")
    assert mapping.translation_dropped(p) == ["subtitle_en"]
    # tolerant of a hand-built product
    assert mapping.translation_dropped({}) == []
    assert mapping.translation_payload({"name": None, "subtitle": {"en": "x"}}) == {}
    assert mapping.translation_dropped({"name": None, "subtitle": {"en": "x"}}) == \
        ["subtitle_en"]


def test_hide_marker_ref():
    ref = mapping.hide_marker_ref
    assert ref(product_id=555) == "product:555"
    assert ref(product_id="555") == "product:555"
    assert ref(product_id=" 555 ") == "product:555"
    assert ref(product_id=555, sku="FAKE-FC") == "product:555"         # id wins
    assert ref(sku="fake-floor-cleaner") == "sku:FAKE-FLOOR-CLEANER"    # upper case
    assert ref(sku=" FAKE-GC-001 ") == "sku:FAKE-GC-001"
    for pid in (None, "", "None", "null"):
        assert ref(product_id=pid) == "", pid
        assert ref(product_id=pid, sku="FAKE-1") == "sku:FAKE-1", pid
    assert ref() == "" and ref(sku=None) == ""


def test_hide_marker_left():
    left = mapping.hide_marker_left
    markers = {"product:555", "sku:FAKE-FLOOR-CLEANER"}
    assert left(markers, 555, "OTHER") is True
    assert left(markers, "555", "") is True
    assert left(markers, 777, "fake-floor-cleaner") is True             # SKU, any case
    assert left(markers, "", "FAKE-FLOOR-CLEANER") is True              # id unreadable
    assert left(markers, 777, "OTHER") is False
    assert left(markers, "", "") is False
    assert left(set(), 555, "FAKE-FLOOR-CLEANER") is False
    assert left(None, 555, "X") is False
    assert left(["product:555"], 555, None) is True                    # any iterable
    # the empty ref of a missing id / SKU never matches a stray "" in the markers
    assert left({""}, "", "") is False and left({""}, None, None) is False
    # a marker of product 55 is not one of product 555
    assert left({"product:55"}, 555, "") is False


def test_hide_marker_stale():
    # A marker older than HIDE_MARKER_MAX_AGE (24 h) is only reported: Ops may have
    # published the product on purpose since. Any recent marker of the product wins.
    stale = mapping.hide_marker_stale
    now = datetime(2026, 11, 10, 12, 0)
    h = lambda hours: now - timedelta(hours=hours)  # noqa: E731
    assert mapping.HIDE_MARKER_MAX_AGE == timedelta(hours=24)
    assert stale({}, "1", "", now) is False                             # no marker
    assert stale(None, "1", "a", now) is False
    assert stale({"product:1": h(1)}, "1", "", now) is False            # 1 h: act on it
    assert stale({"product:1": h(25)}, "1", "", now) is True            # 25 h: report only
    assert stale({"product:1": h(24)}, "1", "", now) is True            # exactly 24 h
    assert stale({"product:1": now - timedelta(hours=24) + timedelta(seconds=1)},
                 "1", "", now) is False                                 # 1 s under
    assert stale({"product:1": None}, "1", "", now) is True             # undated = old
    # one recent marker (SKU, any case) is enough to keep acting on the product
    assert stale({"product:1": h(25), "sku:A": h(2)}, "1", "a", now) is False
    assert stale({"product:1": h(2), "sku:A": h(25)}, "1", "a", now) is False
    assert stale({"product:1": h(25), "sku:A": None}, "1", "a", now) is True
    # id unreadable: matched by SKU alone
    assert stale({"sku:A": h(72)}, "", "a", now) is True
    # another product's marker is not this one's
    assert stale({"product:2": h(72)}, "1", "", now) is False
    assert stale({"product:11": h(72)}, "1", "", now) is False
    # explicit max_age
    assert stale({"product:1": h(2)}, "1", "", now, max_age=timedelta(hours=1)) is True


def test_writes_stock():
    # Review S3: the dry run warns when Apply would write the stock of an existing
    # product that is neither hidden nor sale (Salla then publishes it by itself).
    ws = mapping.writes_stock
    p = _p(_simple_rows(), "fake-glass-cleaner")                          # stock 12
    assert ws(p, _gc_detail(quantity=12)) is False                        # same stock
    assert ws(p, _gc_detail(quantity=5)) is True
    assert ws(p, _gc_detail(quantity=None)) is True                       # unknown
    assert ws(_p(_simple_rows(stock=""), "fake-glass-cleaner"),
              _gc_detail(quantity=5)) is False                            # blank: not sent
    # price change only: no stock write
    assert ws(_p(_simple_rows(price="30"), "fake-glass-cleaner"),
              _gc_detail(quantity=12)) is False
    # options: per matched variant
    p = _p(_options_rows(), _FC)
    detail = _fc_detail_matching(p)
    assert ws(p, detail) is False, detail                                 # all the same
    detail["skus"][0]["stock_quantity"] = 99
    assert ws(p, detail) is True
    # no matched variant -> nothing written
    assert ws(p, {"id": 1, "options": [], "skus": []}) is False
    assert ws({"options": [], "variants": []}, {}) is False


def _fc_detail_matching(p):
    """A Salla detail whose variants match _options_rows() value for value."""
    opt_ids = {}
    options = []
    for index, option in enumerate(p["options"]):
        values = []
        for variant in p["variants"]:
            name = variant["values"][index]["ar"]
            if name not in opt_ids:
                opt_ids[name] = str(100 + len(opt_ids))
                values.append({"id": opt_ids[name], "name": name})
        options.append({"id": str(10 + index), "name": option["ar"], "values": values})
    skus = []
    for i, variant in enumerate(p["variants"]):
        skus.append({"id": str(900 + i), "sku": variant["sku"], "price": variant["price"],
                     "sale_price": variant.get("sale_price"),
                     "stock_quantity": variant["stock"], "barcode": variant.get("barcode"),
                     "weight": variant.get("weight"), "weight_type": variant.get("weight_unit"),
                     "related_option_values": [opt_ids[v["ar"]] for v in variant["values"]]})
    return {"id": 555, "options": options, "skus": skus}


def test_wizard_restore_and_hide_static():
    # Read as text (the wizard imports odoo). Review N3: the Arabic restore sends the
    # texts only; product_payload(create=False) is the update PUT alone.
    src, calls = _wizard_requests()
    assert src.count("product_payload(product, create=False)") == 1, \
        src.count("product_payload(product, create=False)")
    assert src.count('translation_payload(product, "ar")') >= 2
    # Review S1: the verify read of _ensure_hidden is caught on its own, before the POST
    body = src[src.index("def _ensure_hidden"):src.index("# ---", src.index(
        "def _ensure_hidden"))]
    verify, post = body.index("if verify_first:"), body.index('"/products/%s/status"')
    first_try = body.index("try:", verify)
    first_except = body.index("except Exception", first_try)
    assert first_except < post, "the verify GET must have its own except before the POST"
    # Review B1: markers committed on their own cursor, never through the ORM
    hide_src = open(os.path.join(os.path.dirname(__file__), "..", "models",
                                 "salla_product_hide.py"), encoding="utf-8").read()
    assert hide_src.count("self.env.registry.cursor()") == 2, "mark + clear on own cursor"
    # marked again -> the date restarts (hide_marker_stale reads write_date)
    assert "ON CONFLICT (backend_id, ref) DO UPDATE" in hide_src
    assert "DO NOTHING" not in hide_src
    assert ".create(" not in hide_src and ".unlink(" not in hide_src
    # the marker is committed before the create POST and before the first write
    apply_src = src[src.index("def _apply_product"):src.index("def _status_before")]
    assert apply_src.index("_hold_hidden(") < apply_src.index('"PUT", "/products/%s"')
    create_src = src[src.index("def _create_product"):src.index("def _lookup_after_create")]
    assert create_src.index("_mark(") < create_src.index('"POST", "/products"')


if __name__ == "__main__":
    for fn in [test_signature_valid, test_signature_wrong_secret,
               test_signature_tampered_one_byte, test_signature_missing_or_empty_header,
               test_signature_empty_secret, test_signature_case_whitespace_prefix,
               test_signature_bytes_vs_str_body, test_signature_is_over_raw_bytes,
               test_signature_empty_body, test_signature_garbage_header,
               test_signature_uses_compare_digest,
               test_parse_authorize_full, test_parse_authorize_missing_refresh_token,
               test_parse_authorize_missing_data, test_parse_authorize_flat_and_nested_merchant,
               test_parse_authorize_garbage, test_expires_epoch_vs_lifetime,
               test_expires_iso_strings, test_expires_magnitude_boundaries,
               test_expires_garbage_is_none, test_expires_non_finite_or_huge_does_not_crash,
               test_token_needs_refresh_boundaries,
               test_event_and_merchant, test_pick_backend, test_pick_backend_revoke,
               test_is_allowed_salla_url,
               test_retry_delay_retry_after_numeric, test_retry_delay_cap,
               test_retry_delay_http_date, test_retry_delay_rate_limit_reset,
               test_retry_delay_missing_header, test_retry_delay_not_retryable,
               test_retry_delay_garbage_header,
               test_current_page, test_next_page_basic, test_next_page_walk_terminates,
               test_next_page_missing_or_garbage,
               test_product_skus_with_variants, test_product_skus_dedup_and_whitespace,
               test_product_skus_empty_and_none, test_needs_variant_fetch,
               test_redact_hides_tokens, test_redact_masks_pii,
               test_payload_keys_has_no_values,
               # F2a: orders
               test_money, test_salla_date_offsets, test_to_salla_date_window_overlap,
               test_order_status_shapes, test_status_filter,
               test_webhook_order_id, test_webhook_order_id_empty_rejected,
               test_order_id_literal_none_rejected,
               test_clean_id, test_literal_none_ids_fall_through,
               test_normalize_order_full, test_normalize_order_missing_none_and_numeric_strings,
               test_normalize_order_garbage, test_order_customer,
               test_items_unit_price_not_line_total, test_items_numeric_strings_and_fallback_paths,
               test_items_problems_never_price_zero, test_missing_skus_whole_order,
               test_coupon_20_percent_gross_discount, test_coupon_20_percent_net_discount,
               test_item_discount_becomes_discount_line_not_both,
               test_paid_shipping, test_free_shipping,
               test_cod_line_and_rounding_adjust, test_cod_with_coupon_all_extras,
               test_vat_applied_on_extra_lines, test_rounding_3_x_9_99_incl_vat,
               test_adjustment_nets_reach_diff, test_total_check_mismatch_and_unknown,
               test_total_tolerance_bounds, test_unreconciled_extras_flagged,
               test_build_lines_default_vat_when_missing,
               test_next_cursor_held_by_missing_sku, test_holds_cursor_payment_pending,
               test_stock_rows_short_lines, test_redact_order_notes,
               # F2a round 2
               test_should_confirm_under_review_vs_bank_transfer, test_order_payment_method,
               test_order_payment_method_list_fallback,
               test_vat_rate, test_normalize_order_vat_suspect_and_default,
               test_price_digits_7_rounding, test_extras_at_7_digits_hundred_net,
               test_rounding_adjust_salla_net_two_decimals,
               test_offsetting_shipping_and_coupon_tie, test_large_order_real_fee_not_absorbed,
               test_redact_payment_data,
               # F2a fix round 1
               test_redact_ip_and_user_agent,
               test_cod_not_charged_dropped, test_cod_charged_same_items_keeps_line,
               test_cod_charged_vat_zero_dashboard_order,
               test_cod_not_charged_with_coupon_and_shipping,
               test_cod_not_charged_net_shipping,
               test_cod_tiny_fee_charged_wins_tie_or_closest,
               test_no_cod_field_reports_zero, test_fmt_money,
               # F2b: real customer and national address
               test_e164_ksa_formats_same_customer, test_e164_ksa_mobile_code_split,
               test_e164_ksa_unicode_digits_and_marks, test_e164_ksa_unusable_is_empty,
               test_e164_ksa_non_digit_mobile_code_ignored, test_clean_email,
               test_national_short_code_ok_and_normalized,
               test_national_short_code_invalid_never_written,
               test_shipping_address_docs_shape, test_shipping_address_every_container,
               test_shipping_address_fields_variants, test_shipping_address_pickup_is_empty,
               test_shipping_address_short_code_object,
               test_buyer_full, test_buyer_same_customer_any_phone_format,
               test_buyer_phone_states, test_buyer_email_and_name_fallbacks,
               test_buyer_garbage_and_guest, test_fill_empty_never_overwrites_ops_data,
               test_same_address, test_address_fillable, test_pick_partner_rules,
               test_pick_partner_deterministic_any_order,
               test_pick_partner_never_internal,
               test_redact_shipping_address_pii,
               # F2b fix round 1: address only in the webhook body
               test_shipping_address_real_webhook_shape,
               test_shipping_address_ship_from_never_used,
               test_shipping_address_real_pickup_is_empty,
               test_shipping_address_real_short_code_states,
               test_shipping_address_real_street_lines,
               test_webhook_ship_to_keeps_only_address_keys,
               test_webhook_ship_to_empty_cases, test_load_ship_to_never_raises,
               test_with_ship_to_merge_rules, test_redact_real_webhook_body,
               # F3: status Odoo -> Salla and cancellations
               test_order_cancelled_is_queued, test_cancel_refund_webhook_order_ref_first,
               test_ship_push_single_delivery_done, test_ship_push_backorder_waits_until_last,
               test_ship_push_nothing_done_waits, test_ship_push_already_pushed_is_remembered,
               test_ship_push_salla_already_ahead_or_cancelled,
               test_ship_push_only_confirmed_orders, test_ship_push_garbage_never_raises,
               test_status_payload, test_is_permanent_error, test_push_next_try_bounded,
               test_cancel_action_not_shipped_cancels, test_cancel_action_shipped_alerts_only,
               test_cancel_action_posted_invoice_alerts, test_cancel_action_idempotent,
               # F5: invoices, payments, credit notes
               test_payment_journal_key_salla_payments, test_payment_journal_key_bnpl,
               test_payment_journal_key_unknown_no_payment,
               test_payment_journal_key_overrides, test_parse_method_overrides_tolerant,
               test_should_auto_invoice, test_upload_kind,
               test_upload_decision_invoice_zatca_required,
               test_upload_decision_zatca_off_staging,
               test_upload_decision_never_twice_never_draft,
               test_upload_decision_credit_note_follows_invoice,
               test_upload_decision_cancelled_before_upload,
               test_status_before_upload_never_backwards, test_pdf_filename_safe,
               test_public_base_url, test_invoice_pdf_url, test_invoice_upload_payload,
               test_uploaded_invoice_id, test_salla_invoice_kind, test_adopt_uploaded_invoice,
               test_invoice_total_check_tolerance_zero,
               test_cancel_action_credit_note_when_auto_invoicing,
               # F8: product upload
               test_upload_columns_and_handle_re, test_read_csv_rows_bom_arabic_multiline,
               test_read_csv_rows_utf8_without_bom_and_cp1256_fallback,
               test_read_csv_rows_ragged_rows_never_crash, test_read_xlsx_rows_cells,
               test_read_xlsx_rows_sheet_choice,
               test_read_xlsx_rows_not_xlsx_raises_value_error,
               test_read_upload_rows_dispatch, test_parse_simple_product,
               test_parse_stock_blank_vs_zero_and_defaults,
               test_parse_options_size_x_scent, test_parse_one_option_product,
               test_parse_handle_order_and_blank_rows_skipped, test_parse_required_values,
               test_parse_en_missing_is_only_a_warning,
               test_parse_unknown_columns_warned_once, test_parse_duplicate_sku,
               test_parse_bad_handle, test_parse_non_numeric_and_out_of_range_numbers,
               test_parse_sale_price_rules, test_parse_option_name_value_pairing,
               test_parse_duplicate_option_combination,
               test_parse_duplicate_option_combination_normalized,
               test_parse_max_options_and_images, test_parse_mixed_product_level_values,
               test_parse_simple_product_with_two_rows,
               test_parse_bad_product_type_and_categories_flagged,
               test_upload_headers_case_and_spaces_insensitive, test_upload_csv_end_to_end,
               test_text_to_html, test_flagged_claims_ar_en_case_insensitive,
               test_flagged_claims_every_text_field, test_flagged_claims_options_and_alt,
               test_flagged_claims_typographic_apostrophe,
               test_product_payload_create_simple, test_product_payload_create_options,
               test_product_payload_update_never_status_options_images,
               test_product_payload_empty_optionals_omitted, test_translation_payload,
               test_option_en_writes_lang_ar_translations_only, test_norm_label,
               test_match_variants_set_match,
               test_match_variants_same_value_in_two_options, test_match_variants_id_types,
               test_match_variants_simple_product, test_variant_payload_full,
               test_variant_payload_diff_against_current, test_images_to_attach,
               test_translation_kept, test_english_kept,
               test_translation_payload_ar_rewrite_texts_only,
               test_image_size_real_webp_64, test_image_size_real_files_not_square,
               test_image_size_png, test_image_size_jpeg_baseline_and_progressive,
               test_image_size_jpeg_big_exif_and_64kb_header, test_image_size_jpeg_none_cases,
               test_image_size_webp_vp8_lossy, test_image_size_webp_vp8l_lossless,
               test_image_size_webp_vp8x_extended, test_image_size_unknown_or_garbage_is_none,
               test_detail_helpers,
               # F8 review round 4
               test_translation_skip_reason, test_weight_kg, test_same_weight,
               test_variant_payload_weight_units, test_simple_changes_weight_units,
               test_shown, test_parse_parent_sku_collides_with_other_product_sku,
               test_parse_parent_sku_collides_with_own_variant_sku,
               test_parse_parent_sku_no_false_positive, test_parse_duplicate_image_names,
               test_read_csv_rows_unclosed_quote_value_error,
               test_read_xlsx_rows_encrypted_entry_value_error,
               test_read_xlsx_rows_unsupported_compression_is_damaged,
               # F8 weight required on create (demo 422, 2026-11-10)
               test_create_weight_simple_product,
               test_create_weight_options_first_weighted_row,
               test_create_weight_none_without_any_weight,
               test_create_weight_rejects_non_positive_and_non_numbers,
               test_product_payload_update_options_never_weight,
               # F8 spike follow-up (review B1/S2/S3)
               test_translation_payload_drops_en_without_ar, test_hide_marker_ref,
               test_hide_marker_left, test_hide_marker_stale, test_writes_stock,
               test_wizard_restore_and_hide_static,
               # F8 option / value English (spike run 4, lang ar + translations.en)
               test_option_en_keys_from_spike_run4,
               test_option_translation_payloads_full_product,
               test_option_translation_payloads_arabic_from_salla_not_sheet,
               test_option_translation_payloads_empty_en_skipped,
               test_option_translation_payloads_en_inherited_from_other_rows,
               test_option_translation_payloads_conflicting_en_skipped,
               test_option_translation_payloads_en_same_as_arabic_skipped,
               test_option_translation_payloads_missing_salla_option_or_value,
               test_option_translation_payloads_ambiguous_match_skipped,
               test_option_translation_payloads_no_id_or_no_arabic_skipped,
               test_option_translation_payloads_already_in_salla_nothing,
               test_option_translation_payloads_two_options_share_a_value_name,
               test_option_translation_payloads_no_detail_or_no_options,
               test_option_english_names_order_and_dedup, test_detail_option_name]:
        fn()
        print(fn.__name__, "OK")
    print("all passed")
