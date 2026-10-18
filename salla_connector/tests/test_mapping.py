"""Standalone self-check for the pure mapping logic (no Odoo needed).

Run:  python3 salla_connector/tests/test_mapping.py

All secrets/tokens below are obviously fake test values.
"""
import hashlib
import hmac
import inspect
import json
import math
import os
import sys
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
    for st in ("payment_pending", "Payment-Pending", "canceled", "cancelled", "refunded",
               "restored", "restoring", "deleted"):
        assert not mapping.should_import(st), st
        assert not mapping.should_confirm(st), st
    for st in ("in_progress", "processing", "completed", "shipped",
               "delivering", "delivered", "ready_for_pickup", "In Progress"):
        assert mapping.should_import(st), st
        assert mapping.should_confirm(st), st
        assert mapping.should_confirm(st, "bank"), st          # payment method irrelevant
    # under_review: imported, confirmed ONLY for cash on delivery
    for st in ("under_review", "Under Review", "under-review"):
        assert mapping.should_import(st), st
        assert not mapping.should_confirm(st), st
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
    assert mapping.ORDER_EVENTS == {"order.created", "order.updated", "order.status.updated"}


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


def test_should_confirm_cod_vs_bank_transfer():
    """under_review means "waiting for the merchant": for COD that IS the accepted order
    (confirm, prepare it); for a bank transfer it means the receipt is not checked yet
    (quotation, no delivery)."""
    for method in ("cod", "COD", "cash_on_delivery", "Cash-On-Delivery", "cash on delivery"):
        assert mapping.is_cod(method), method
        assert mapping.should_confirm("under_review", method), method
    for method in ("bank", "bank_transfer", "credit_card", "mada", "", None, "codx", "c.o.d"):
        assert not mapping.is_cod(method), method
        assert not mapping.should_confirm("under_review", method), method
    # COD never makes a dead or unpaid status confirmable
    for st in ("payment_pending", "canceled", "refunded", "", "fake_custom_status"):
        assert not mapping.should_confirm(st, "cod"), st


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
    o = mapping.normalize_order(_order(69.0, payment_method="bank"))
    assert not mapping.should_confirm(o["status"], o["payment_method"])
    assert mapping.normalize_order({"id": 1})["payment_method"] == ""


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
               test_should_confirm_cod_vs_bank_transfer, test_order_payment_method,
               test_vat_rate, test_normalize_order_vat_suspect_and_default,
               test_price_digits_7_rounding, test_extras_at_7_digits_hundred_net,
               test_rounding_adjust_salla_net_two_decimals,
               test_offsetting_shipping_and_coupon_tie, test_large_order_real_fee_not_absorbed,
               test_redact_payment_data]:
        fn()
        print(fn.__name__, "OK")
    print("all passed")
