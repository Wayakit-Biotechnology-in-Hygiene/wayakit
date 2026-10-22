"""Pure Salla->Odoo mapping helpers.

No Odoo imports on purpose so the parsing / signature / token rules are testable
standalone (same pattern as trendyol_connector):
    python salla_connector/tests/test_mapping.py

Every Salla field name below is taken from docs.salla.dev and is UNVERIFIED against a
real payload. The Trendyol connector got three of those wrong (shipmentPackageId,
lineId, lineUnitPrice), so each reader is tolerant and the controller logs the
structure of the first real webhooks. Fix the names here, once, when a payload
disagrees.
"""
import hashlib
import hmac
import json
import math
import re
from urllib.parse import urlsplit
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone

AUTHORIZE_EVENT = "app.store.authorize"
# Both spellings appear in Salla's docs depending on the page; either one means the
# tokens we hold are dead.
REVOKE_EVENTS = {"app.uninstalled", "app.store.revoke"}

# Keys whose VALUE must never reach a log line. Matched as a substring of the
# lowercased key so access_token / refresh_token / client_secret are all caught.
_SENSITIVE_KEY_PARTS = ("token", "secret", "password", "authorization", "api_key")
# Customer PII (confidential under Wayakit policy), masked the same way. Substrings
# catch customer_email / shipping_address / mobile_code. "name"/"city"/"location" are
# exact matches only: as substrings they would mask half the payload. That also masks
# a product's "name" — acceptable, the log is for checking field NAMES, not values.
_PII_KEY_PARTS = ("first_name", "last_name", "full_name", "email", "mobile", "phone",
                  "address", "street", "postal", "zip", "latitude", "longitude",
                  "national", "district", "block", "building",
                  # F2b: Saudi national address parts of Salla's ship_to (docs, unverified)
                  "additional_number", "geo", "coordinate", "short_code")
# ip / user agent identify the customer's device (source_details on real orders). Exact
# keys only: "ip" as a substring would mask "shipping", "description", "zip"...
# lat/lng/lon too: as substrings they would hit "translation", "template", "long_desc".
_PII_KEYS = {"name", "city", "location", "ip", "user-agent", "user_agent", "useragent",
             "lat", "lng", "lon"}
# Free text the customer types (order notes, gift messages) routinely carries a phone
# number or an address: masked as a substring so customer_note / notes are caught too.
_FREE_TEXT_KEY_PARTS = ("note", "comment", "message", "gift")
# Payment/attachment data: bank transfer receipts, IBANs, avatar and signed file URLs
# (a signed URL is a credential to the file it points at).
_PAYMENT_KEY_PARTS = ("receipt", "avatar", "iban", "account", "url")

# OAuth credentials (client_secret, refresh token, Bearer) are only ever sent to these.
# A backend pointed anywhere else would hand them to whoever runs that host.
# Exact hosts, no wildcard: a *.salla.dev rule would trust any subdomain Salla (or a
# dangling DNS record) ever exposes.
_SALLA_HOSTS = {"api.salla.dev", "accounts.salla.sa"}
# Whole netloc, after lowercasing: plain host chars and an optional :443. Anything else
# (backslash, %, @, unicode) is how URL parsers disagree on what the host is.
_NETLOC_RE = re.compile(r"[a-z0-9.-]+(:443)?")

# Below this an "expires" number cannot be an epoch timestamp (10**9 s = 2001-09-09)
# and above it cannot be a lifetime (31 years). Salla's access token lives 14 days.
_EPOCH_SECONDS_MIN = 10 ** 9
_EPOCH_MS_MIN = 10 ** 12

_HEX = set("0123456789abcdef")


def _utcnow():
    """Naive UTC, the format Odoo Datetime fields store."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def text(value):
    """str(value).strip(), but "" for None/bool/containers instead of "None"/"True"
    — the Trendyol "None" package id collapsed every order onto one dedup key."""
    if value is None or isinstance(value, (bool, dict, list, tuple, set)):
        return ""
    return str(value).strip()


# ------------------------------------------------------------------ webhook trust
def verify_signature(raw_body, secret, signature):
    """True when `signature` is the hex HMAC-SHA256 of `raw_body` keyed with `secret`.

    Salla signs the RAW request body, so this must run on the bytes as received,
    never on re-serialized JSON. Tolerant to str/bytes inputs, surrounding whitespace,
    upper-case hex and a "sha256=" prefix; constant-time compare.

    An EMPTY body (b"") is still verified — the HMAC of an empty body is as
    unforgeable as any other, and the controller answers it 200 "ok:0". A missing
    body (None), secret or signature is always False.
    """
    if raw_body is None or not secret or not signature:
        return False
    try:
        if isinstance(raw_body, str):
            raw_body = raw_body.encode("utf-8")
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        if isinstance(signature, bytes):
            signature = signature.decode("ascii")
    except (UnicodeError, AttributeError):
        return False
    if not isinstance(raw_body, bytes) or not isinstance(secret, bytes) \
            or not isinstance(signature, str):
        return False
    sig = signature.strip().lower()
    if sig.startswith("sha256="):
        sig = sig[len("sha256="):].strip()
    # compare_digest raises TypeError on non-ASCII str; a non-hex header is just wrong.
    if not sig or not set(sig) <= _HEX:
        return False
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def event_name(payload):
    """Lowercased `event` of a webhook payload, "" when absent."""
    if not isinstance(payload, dict):
        return ""
    return text(payload.get("event")).lower()


def merchant_id(payload):
    """The Salla store (merchant) id a webhook belongs to, as a string, "" if absent.

    Docs show `merchant` as a bare number; accept {"id": ...} too, and fall back to
    data.merchant in case the envelope differs."""
    if not isinstance(payload, dict):
        return ""
    for holder in (payload, payload.get("data") if isinstance(payload.get("data"), dict) else {}):
        value = holder.get("merchant")
        if isinstance(value, dict):
            value = value.get("id")
        found = text(value)
        if found:
            return found
    return ""


def pick_backend(candidates, merchant, event=""):
    """Choose which backend a signed webhook belongs to, or None.

    `candidates` = [(key, store_id), ...] for every active backend whose secret
    verified the signature. Several can match: the webhook secret belongs to the Salla
    APP, so the demo store and the real store share it.

    1. The backend bound to that exact store wins, for every event.
    2. Revoke/uninstall events stop there — never a fallback, and never without a
       merchant: an uninstall on the demo store must not wipe the real store's tokens.
    3. `app.store.authorize` may bind a backend not bound to any store yet (first
       install; the first such candidate wins) — and stops there. It never lands on an
       already-bound backend without an exact match: if merchant_id() misreads the real
       payload, the demo store's authorize must not overwrite prod's tokens.
    4. Any other event with NO merchant in the payload goes to the single matching
       backend if there is exactly one.
    A store we do not know never reaches another store's backend.
    """
    merchant = text(merchant)
    event = text(event).lower()
    candidates = list(candidates or [])
    if merchant:
        for key, store in candidates:
            if text(store) == merchant:
                return key
    if event in REVOKE_EVENTS:
        return None
    if event == AUTHORIZE_EVENT:
        for key, store in candidates:
            if not text(store):
                return key
        return None
    if not merchant and len(candidates) == 1:
        return candidates[0][0]
    return None


def is_allowed_salla_url(url):
    """True only for an https URL on exactly api.salla.dev or accounts.salla.sa,
    default port, no user-info, plain host characters. Guards base_url/token_url: whatever host is there
    receives the client secret, the single-use refresh token and the Bearer token."""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return False
    if parts.scheme.lower() != "https" or parts.username or parts.password:
        return False
    if port not in (None, 443):
        return False
    if not _NETLOC_RE.fullmatch(parts.netloc.lower()):
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    return host in _SALLA_HOSTS


# ------------------------------------------------------------------ OAuth tokens
def expires_at(value, now=None):
    """Turn an OAuth "expires" number into a naive UTC datetime, or None.

    Salla's authorize webhook documents `expires` as an epoch timestamp, while the
    standard token endpoint answers `expires_in` = seconds from now. Rather than trust
    either name, decide by magnitude: >= 10**12 epoch ms, >= 10**9 epoch seconds,
    anything smaller positive = seconds from `now`. A non-numeric string is read as an
    ISO date ("2026-10-29 12:00:00", optional "T" and "Z"/offset; naive = UTC).
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _parse_iso(value)
    # float("nan") / "inf" parse fine and then blow up in datetime: junk -> None,
    # because a webhook must never 500 on an odd expiry value.
    if not math.isfinite(number) or number <= 0:
        return None
    if number >= _EPOCH_MS_MIN:
        number /= 1000.0
    try:
        if number >= _EPOCH_SECONDS_MIN:
            return datetime.fromtimestamp(number, timezone.utc).replace(tzinfo=None)
        return (now or _utcnow()) + timedelta(seconds=number)
    except (OverflowError, ValueError, OSError):
        return None


def _parse_iso(value):
    """ "2026-10-29 12:00:00" / "2026-10-29T12:00:00Z" / "...+03:00" -> naive UTC.
    A naive string is taken as UTC. Anything unparseable -> None."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw[-1:] in ("Z", "z"):
        raw = raw[:-1] + "+00:00"   # fromisoformat only learned "Z" in Python 3.11
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    return when


def parse_tokens(data, now=None):
    """{access_token, refresh_token, expires_at, scope} from a token-bearing dict
    (authorize webhook `data` or the token endpoint response), or None without an
    access token."""
    if not isinstance(data, dict):
        return None
    access = text(data.get("access_token"))
    if not access:
        return None
    raw_exp = data.get("expires")
    if raw_exp is None:
        raw_exp = data.get("expires_in")
    return {
        "access_token": access,
        "refresh_token": text(data.get("refresh_token")),
        "expires_at": expires_at(raw_exp, now),
        "scope": text(data.get("scope")),
    }


def parse_authorize(payload, now=None):
    """Parse an `app.store.authorize` webhook (OAuth Easy Mode).

    Documented shape (unverified):
        {"event": "app.store.authorize", "merchant": 1234, "created_at": "...",
         "data": {"access_token", "refresh_token", "expires", "scope", "token_type"}}
    Tokens are read from `data`, or from the top level if Salla ever flattens it.
    Returns parse_tokens(...) + "store_id", or None when no access token is found.
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    tokens = parse_tokens(data if isinstance(data, dict) else payload, now)
    if tokens is None and isinstance(data, dict):
        tokens = parse_tokens(payload, now)
    if tokens is None:
        return None
    tokens["store_id"] = merchant_id(payload)
    return tokens


def token_needs_refresh(expires, now, margin):
    """True when the access token expires within `margin` of `now`.
    An unknown expiry counts as "needs refresh": guessing it is still valid is how a
    refresh token silently lapses after its one month."""
    if not expires:
        return True
    return expires - margin <= now


# ------------------------------------------------------------------ HTTP helpers
def _header(headers, name):
    """Case-insensitive header lookup that also works on a plain dict."""
    if not headers:
        return None
    if name in headers:
        return headers[name]
    lname = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lname:
            return value
    return None


def retry_delay(status, headers, attempt, now=None, cap=30):
    """Seconds to sleep before retrying, or None when the response must not be retried.

    - status None (network error) or >= 500 -> exponential backoff 2**attempt.
    - 429 -> Retry-After (seconds, or an HTTP-date); else X-RateLimit-Reset, which Salla
      documents as an epoch timestamp (accept a bare seconds count too, by magnitude);
      else exponential backoff. Always capped at `cap` and never negative.
    - anything else -> None.
    `now` = epoch seconds, injectable for tests.
    """
    backoff = float(min(2 ** attempt, cap))
    if status is None or status >= 500:
        return backoff
    if status != 429:
        return None
    if now is None:
        now = datetime.now(timezone.utc).timestamp()
    retry_after = text(_header(headers, "Retry-After"))
    # isascii(): str.isdigit() also accepts "²" and other Unicode digits int() rejects.
    if retry_after.isascii() and retry_after.isdigit():
        return float(min(int(retry_after), cap))
    if retry_after:
        # RFC 9110 also allows an HTTP-date here; a garbage value falls through.
        try:
            when = parsedate_to_datetime(retry_after)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return float(min(max(when.timestamp() - now, 0.0), cap))
        except (TypeError, ValueError, IndexError, OverflowError, AttributeError):
            pass
    reset = text(_header(headers, "X-RateLimit-Reset"))
    try:
        reset = float(reset)
    except ValueError:
        return backoff
    if not math.isfinite(reset):   # "nan"/"inf" parse, then time.sleep() chokes on them
        return backoff
    if reset >= _EPOCH_SECONDS_MIN:
        reset -= now
    return float(min(max(reset, 0.0), cap))


def current_page(pagination):
    """The page number a Salla `pagination` block says it is, or None."""
    if not isinstance(pagination, dict):
        return None
    for key in ("currentPage", "current_page"):
        try:
            return int(pagination.get(key))
        except (TypeError, ValueError):
            continue
    return None


def next_page(pagination):
    """Next page number from a Salla `pagination` block, or None on the last page.

    Documented keys are camelCase (currentPage/totalPages); snake_case accepted too.
    When totalPages is missing, a `links.next` still means there is one more page.
    Callers keep their own hard page cap: a paginator that never says "last" must not
    loop forever."""
    if not isinstance(pagination, dict):
        return None

    def _int(*keys):
        for key in keys:
            try:
                return int(pagination.get(key))
            except (TypeError, ValueError):
                continue
        return None

    current = _int("currentPage", "current_page")
    total = _int("totalPages", "total_pages")
    if current is None:
        return None
    if total is not None:
        return current + 1 if current < total else None
    links = pagination.get("links")
    if isinstance(links, dict) and links.get("next"):
        return current + 1
    return None


# ------------------------------------------------------------------ products
def product_skus(product):
    """Every SKU a Salla product carries: its own `sku` plus each variant's.

    Variants are expected under `skus` (product detail) and accepted under `variants`.
    Stripped, empties dropped, de-duplicated preserving order."""
    if not isinstance(product, dict):
        return []
    codes = [product.get("sku")]
    for key in ("skus", "variants"):
        for variant in product.get(key) or []:
            if isinstance(variant, dict):
                codes.append(variant.get("sku"))
    out = []
    for code in codes:
        code = text(code)
        if code and code not in out:
            out.append(code)
    return out


def needs_variant_fetch(product):
    """True when the product declares options but the list payload carried no variant
    list, so the variant SKUs have to be read from the product detail."""
    if not isinstance(product, dict) or not product.get("options"):
        return False
    return not any(isinstance(product.get(key), list) for key in ("skus", "variants"))


# ------------------------------------------------------------------ safe logging
def _sensitive(key):
    key = str(key).lower()
    return (key in _PII_KEYS
            # client_ip / remote_ip / ip_addr, without the "ip" substring trap (shipping)
            or key.endswith(("_ip", "-ip")) or key.startswith(("ip_", "ip-"))
            or any(part in key for part in _SENSITIVE_KEY_PARTS)
            or any(part in key for part in _PII_KEY_PARTS)
            or any(part in key for part in _FREE_TEXT_KEY_PARTS)
            or any(part in key for part in _PAYMENT_KEY_PARTS))


def redact(payload):
    """Deep copy of `payload` with every credential AND customer-PII key's value
    replaced by "***" (keys kept, so the structure stays readable). For logging only."""
    if isinstance(payload, dict):
        return {k: ("***" if _sensitive(k) else redact(v)) for k, v in payload.items()}
    if isinstance(payload, list):
        return [redact(v) for v in payload]
    return payload


def payload_keys(payload, prefix="", depth=4):
    """Sorted dotted key paths of a payload — its STRUCTURE, no values at all.
    Used to log the authorize webhook, whose values are the tokens. Lists show as
    "key[]" with the keys of their first dict element."""
    out = set()
    if depth <= 0:
        return []
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = "%s%s" % (prefix, key)
            out.add(path)
            out.update(payload_keys(value, path + ".", depth - 1))
    elif isinstance(payload, list):
        first = next((v for v in payload if isinstance(v, dict)), None)
        if first is not None:
            base = prefix[:-1] + "[]." if prefix.endswith(".") else prefix
            out.update(payload_keys(first, base, depth - 1))
    return sorted(out)


# ================================================================== F2a: orders
# EVERY field name in this section comes from docs.salla.dev and is UNVERIFIED against a
# real payload: validate against the first real order (log_payloads on) and fix the
# names here, once. Each reader takes a list of candidate paths, first hit wins.

# order.cancelled / order.refunded (F3): queued like the others; the queue re-reads the
# order and _salla_apply_status acts on its status, so the event body is never trusted.
ORDER_EVENTS = {"order.created", "order.updated", "order.status.updated",
                "order.cancelled", "order.refunded"}
STATUS_EVENT = "order.status.updated"
# Events whose `data` may be something other than the order (a status change, a refund):
# their own `id` is then not the order id, so the order reference is read first.
ORDER_REF_FIRST_EVENTS = {STATUS_EVENT, "order.cancelled", "order.refunded"}

# Status slugs (status.slug). Validate against a real payload: a store can rename
# statuses ("customized"), the slug is what should stay stable.
# Not imported: nothing has been paid (payment_pending, request_quote = the customer
# asked for a price quote) or the order is dead.
NOT_IMPORTABLE_STATUSES = {"payment_pending", "request_quote", "canceled", "cancelled",
                           "restored", "restoring", "refunded", "deleted"}
# Imported AND confirmed: the order is paid and must be prepared.
# closed: Salla's final state after completed (demo store status list, 2026-10-20).
CONFIRMABLE_STATUSES = {"in_progress", "processing", "completed", "closed",
                        "shipped", "delivering", "delivered", "ready_for_pickup"}
# "Under review" (Pending review): Salla holds it for the merchant to accept. Since the
# 2026-10-18 meeting the store only takes Apple Pay, Tabby, Tamara and cards, all paid
# upfront, so an order here is already paid and is confirmed. The one exception is a bank
# transfer, whose receipt waits in this status unchecked: should one slip through (the
# method switched back on), it stays a quotation.
REVIEW_STATUSES = {"under_review"}
# Payment method slugs meaning bank transfer. VALIDATE AGAINST A REAL PAYLOAD: both the
# field (payment_method) and the slugs are taken from the docs ("bank").
BANK_TRANSFER_PAYMENT_METHODS = {"bank", "bank_transfer", "banktransfer", "بنك"}
# The guard is allow-by-default, so a label (payment_method.name, maybe Arabic: "تحويل
# بنكي", "حوالة بنكية") must also trip it: a value containing one of these is a bank
# transfer. Not a bare "bank": "bank_card" / mada is a prepaid card, not a transfer.
BANK_TRANSFER_MARKERS = ("transfer", "wire", "تحويل", "حوالة")
# Payment method slugs meaning cash on delivery (not offered since 2026-10-18; kept so a
# COD order is still recognisable). Same caveat: slugs from the docs.
COD_PAYMENT_METHODS = {"cod", "cash_on_delivery"}
# On an order we already have: handled once (F3, cancel_action): cancelled in Odoo when
# nothing shipped yet, else handed to Ops. restoring/restored = return in progress /
# returned (the items go back to Salla's stock): the same as a cancellation for Odoo.
CANCEL_STATUSES = {"canceled", "cancelled", "restored", "restoring", "refunded"}
# Unknown slug (a custom status, or the field misread): imported as a quotation, never
# confirmed, so a human looks at it. Losing it would be worse.

# payment_pending orders hold the reconcile cursor so the cron still sees them once paid
# (e.g. a Tabby/Tamara checkout finished late) even if the status webhook is lost;
# abandoned ones stop holding after:
PENDING_HOLD_DAYS = 7
# Salla's order dates are Riyadh time when they carry no zone (UTC+3, no DST).
SALLA_UTC_OFFSET = timedelta(hours=3)
DEFAULT_VAT_RATE = 15.0

# Paths for the items endpoint and list filters (unverified, see docs "List Order Items"
# and "List Orders"). The "expanded" order format was removed on 2026-09-01.
ORDER_ITEMS_PATH = "/orders/items"
ORDER_LIST_FROM_PARAM = "from_date"
ORDER_LIST_TO_PARAM = "to_date"


def clean_id(value):
    """An id as a string, "" when absent — including the literal "None"/"null" a
    str(None) somewhere upstream produces (Trendyol's "None" package id collapsed every
    order onto one dedup key)."""
    found = text(value)
    return "" if found.lower() in ("none", "null", "undefined", "nan") else found


def _canon_status(value):
    return text(value).lower().replace("-", "_").replace(" ", "_")


def _dig(obj, path):
    """obj["a"]["b"]... for a dotted path, None as soon as a step is missing."""
    for key in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _first(obj, *paths):
    """Value of the first path that holds something other than None/""."""
    for path in paths:
        value = _dig(obj, path)
        if value is not None and value != "":
            return value
    return None


def money(value):
    """A Salla amount as a float, or None. Accepts 12.5, "12.50", "1,250.00" and the
    {"amount": ..., "currency": ...} object Salla uses for most money fields."""
    if isinstance(value, dict):
        value = value.get("amount")
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.replace(",", "").strip()
        if not value:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _currency(value):
    return text(value.get("currency")).upper() if isinstance(value, dict) else ""


def salla_date(value):
    """Salla date -> naive UTC datetime, or None.

    Shapes seen in the docs: "2026-10-18 15:30:00", ISO with offset/Z, and
    {"date": "2026-10-18 15:30:00.000000", "timezone_type": 3, "timezone": "Asia/Riyadh"}.
    A date without an offset is Riyadh time (UTC+3): Salla is a KSA platform and the
    docs show Asia/Riyadh. Reading it as UTC would shift the cursor window 3 h."""
    zone = ""
    if isinstance(value, dict):
        zone = text(value.get("timezone"))
        value = value.get("date")
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("T", " ", 1)
    if raw[-1:] in ("Z", "z"):
        raw = raw[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        # fromisoformat before 3.11 rejects 6-digit microseconds in some layouts; the
        # seconds are all the cursor needs.
        try:
            when = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if when.tzinfo is not None:
        return when.astimezone(timezone.utc).replace(tzinfo=None)
    if zone.upper() in ("UTC", "Z", "GMT", "+00:00"):
        return when
    return when - SALLA_UTC_OFFSET


def to_salla_date(when):
    """Naive UTC datetime -> the Riyadh calendar date ("YYYY-MM-DD") for list filters."""
    return (when + SALLA_UTC_OFFSET).strftime("%Y-%m-%d")


# ------------------------------------------------------------------ status
def order_status(order):
    """Canonical status slug of an order (or of a status webhook's data), "" if absent.
    status may be {"slug": ...}, a bare string, or nested under order.status."""
    if not isinstance(order, dict):
        return ""
    status = order.get("status")
    if isinstance(status, dict):
        return _canon_status(status.get("slug") or _dig(status, "customized.slug"))
    if status:
        return _canon_status(status)
    return _canon_status(_first(order, "order.status.slug", "status_slug"))


def should_import(status):
    """False for unpaid / dead orders. An unknown or empty status IS imported (as a
    quotation, see should_confirm): never lose a paid order to a status we can't read."""
    return _canon_status(status) not in NOT_IMPORTABLE_STATUSES


def is_cod(payment_method):
    return _canon_status(payment_method) in COD_PAYMENT_METHODS


def is_bank_transfer(payment_method):
    method = _canon_status(payment_method)
    return method in BANK_TRANSFER_PAYMENT_METHODS \
        or any(marker in method for marker in BANK_TRANSFER_MARKERS)


def should_confirm(status, payment_method=""):
    """True only for a status known to mean "paid, prepare it".
    under_review counts too, except with a bank transfer (receipt not verified yet).
    An empty/unknown method under review is confirmed: every method on offer is prepaid."""
    status = _canon_status(status)
    if status in CONFIRMABLE_STATUSES:
        return True
    return status in REVIEW_STATUSES and not is_bank_transfer(payment_method)


def vat_rate(value):
    """(rate in %, suspicious) from Salla's tax.percent.

    15 / "15.00" -> (15.0, False). A fraction (0 < x < 1, e.g. 0.15) -> x * 100,
    flagged. Missing -> (None, False): build_lines then uses DEFAULT_VAT_RATE. Anything
    outside 0..30 % (no Saudi rate is) -> (None, True): the caller logs a WARNING."""
    rate = money(value)
    if rate is None:
        return None, value not in (None, "")
    if 0 < rate < 1:
        return rate * 100.0, True
    if 0 <= rate <= 30:
        return rate, False
    return None, True


def is_cancel_status(status):
    return _canon_status(status) in CANCEL_STATUSES


def webhook_order_id(payload):
    """Salla order id a webhook is about, as a string, "" if absent.

    order.created/updated: data IS the order (data.id). order.status.updated (and the
    cancel/refund events, shape unverified): data may be the status change or the refund
    itself, whose own `id` is NOT the order id, so the order reference (data.order.id /
    data.order_id) is tried first for those events."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return ""
    data = payload["data"]
    paths = ("order.id", "order_id", "id") if event_name(payload) in ORDER_REF_FIRST_EVENTS \
        else ("id", "order.id", "order_id")
    for path in paths:
        found = clean_id(_dig(data, path))
        if found:
            return found
    return ""


# ------------------------------------------------------------------ order
def normalize_order(order):
    """The fields the import needs from an order detail (GET /orders/{id} `data`).

    {"id", "reference", "status", "created_at" (naive UTC), "currency", "total",
     "vat_rate", "shipping", "cod", "discounts": [{"title", "amount"}],
     "customer": {"ref", "name"}}
    Money is as Salla sends it (VAT-inclusive or not is decided by build_lines).
    None for a non-dict or an order without id."""
    if not isinstance(order, dict):
        return None
    oid = clean_id(order.get("id"))
    if not oid:
        return None
    total_raw = _first(order, "amounts.total", "total")
    vat, vat_suspect = vat_rate(_first(order, "amounts.tax.percent", "tax.percent"))
    discounts = []
    for entry in _first(order, "amounts.discounts", "discounts") or []:
        if not isinstance(entry, dict):
            continue
        amount = money(_first(entry, "discount", "amount", "value"))
        if amount:
            discounts.append({
                "title": text(_first(entry, "title", "name", "code")) or "Discount",
                "amount": abs(amount),
            })
    return {
        "id": oid,
        "reference": text(_first(order, "reference_id", "reference")) or oid,
        "status": order_status(order),
        "created_at": salla_date(_first(order, "date", "created_at")),
        "currency": (text(order.get("currency")).upper() or _currency(total_raw)
                     or _currency(_dig(order, "amounts.sub_total"))),
        "total": money(total_raw),
        "vat_rate": vat,
        "vat_suspect": vat_suspect,
        "shipping": money(_first(order, "amounts.shipping_cost", "shipping_cost")) or 0.0,
        "cod": money(_first(order, "amounts.cash_on_delivery", "cash_on_delivery")) or 0.0,
        "discounts": discounts,
        "customer": order_customer(order),
        "payment_method": order_payment_method(order),
    }


def _method_slug(value):
    if isinstance(value, dict):
        value = value.get("slug") or value.get("name")
    return _canon_status(value) if isinstance(value, str) else ""


def order_payment_method(order):
    """Canonical payment method slug, "" if absent. VALIDATE AGAINST A REAL PAYLOAD:
    docs show a bare string ("cod", "bank", "credit_card"); an object is accepted too.
    Fallbacks: payment.method, then payment_methods[] (docs list it on the order). From
    the list a bank transfer wins over the first entry: should_confirm only needs to
    know whether one is involved, and missing it would confirm an unverified order."""
    if not isinstance(order, dict):
        return ""
    method = _method_slug(order.get("payment_method")) or _method_slug(_dig(order, "payment.method"))
    if method:
        return method
    listed = order.get("payment_methods")
    listed = [m for m in (_method_slug(v) for v in listed) if m] if isinstance(listed, list) else []
    return next((m for m in listed if is_bank_transfer(m)), listed[0] if listed else "")


def order_customer(order):
    """{"ref": Salla customer id, "name": display name} — F2a uses nothing else.
    The name is PII: never log it."""
    cust = order.get("customer") if isinstance(order, dict) else None
    if not isinstance(cust, dict):
        return {"ref": "", "name": ""}
    name = " ".join(p for p in (text(cust.get("first_name")), text(cust.get("last_name"))) if p)
    name = " ".join((name or text(cust.get("full_name")) or text(cust.get("name"))).split())
    return {"ref": clean_id(cust.get("id")), "name": name}


def normalize_items(items):
    """Order Items endpoint `data` -> (lines, problems).

    lines: [{"line_id", "sku", "name", "quantity", "unit_net", "discount"}]
      unit_net = amounts.price_without_tax, read as the PER-UNIT price before VAT (the
      Trendyol lineUnitPrice trap: validate with quantity > 1). discount = the line's
      total_discount (a line total, not per unit), 0.0 when absent.
    problems: human-readable reasons the order cannot be built (no price, bad quantity).
    An empty SKU is NOT a problem here: it is reported by missing_skus() like any SKU
    Odoo does not know."""
    lines, problems = [], []
    for idx, item in enumerate(items if isinstance(items, list) else []):
        if not isinstance(item, dict):
            continue
        ref = clean_id(item.get("id")) or "#%s" % (idx + 1)
        qty = money(item.get("quantity"))
        if qty is None or qty <= 0:
            problems.append("item %s: quantity %r" % (ref, item.get("quantity")))
            continue
        unit = money(_first(item, "amounts.price_without_tax", "price_without_tax"))
        if unit is None:
            # Never import at price 0 (Trendyol S02056): hold the order instead.
            problems.append("item %s: no price_without_tax" % ref)
            continue
        lines.append({
            "line_id": clean_id(item.get("id")),
            "sku": text(_first(item, "sku", "product.sku", "variant.sku")),
            "name": text(item.get("name")),
            "quantity": qty,
            "unit_net": unit,
            "discount": abs(money(_first(item, "amounts.total_discount", "total_discount")) or 0.0),
        })
    return lines, problems


def missing_skus(lines, known_skus):
    """SKUs of `lines` not in `known_skus` (sorted, unique; "" shown as "(empty SKU)").
    Any of them means the WHOLE order is skipped — never half an order."""
    known = set(known_skus or ())
    out = {line["sku"] or "(empty SKU)" for line in lines if not line["sku"] or line["sku"] not in known}
    return sorted(out)


# ------------------------------------------------------------------ amounts
# Decimal precision "Product Price" on the production DB (checked 2026-10-18): unit
# prices are stored with 7 decimals, the currency (SAR) rounds to 2. The backend passes
# the live value; this default is only for callers without an Odoo env.
PRICE_DIGITS = 7


def _round(value, digits):
    """Round half away from zero, like Odoo's float_round (Python's round is banker's
    rounding on binary floats: round(2.675, 2) == 2.67)."""
    factor = 10 ** digits
    sign = -1 if value < 0 else 1
    return sign * math.floor(abs(value) * factor + 0.5 + 1e-9) / factor


def _r2(value):
    return _round(value, 2)


def expected_total(lines, vat_rate, price_digits=PRICE_DIGITS):
    """VAT-inclusive total Odoo will compute for `lines` [{"price_unit", "quantity"}]:
    unit price stored with `price_digits`, line subtotal rounded to the currency, tax
    rounded per line (company 1 uses round_per_line), every line at vat_rate.
    Only a prediction for build_lines; the real check is the order's amount_total."""
    total = 0.0
    for line in lines:
        subtotal = _r2(_round(line["price_unit"], price_digits) * line["quantity"])
        total += subtotal + _r2(subtotal * vat_rate / 100.0)
    return _r2(total)


def total_tolerance(lines):
    """How far Odoo's total may drift from Salla's from rounding alone.

    With 7-digit unit prices Odoo's own error is the per-line rounding of subtotal and
    tax: under 0.01 per line. What remains is Salla handing us price_without_tax already
    rounded to 2 decimals (8.69 for 9.99 gross): up to half a halala per unit before
    VAT, 0.00575 with it, hence 0.006 per unit. Capped at 0.50 SAR: past that it is a
    real error (wrong product tax, misread field), never rounding."""
    units = sum(abs(line["quantity"]) for line in lines)
    return min(0.5, _r2(0.006 * units + 0.01 * len(lines)))


def build_lines(order, items, price_digits=PRICE_DIGITS):
    """Plan the Odoo lines for a normalized order + items. Pure: no product ids.

    Returns {"products": [{"sku", "name", "quantity", "price_unit", "line_id"}],
             "extras": [{"kind": "shipping"|"cod"|"discount", "name", "price_unit"}],
             "extras_basis": "gross" | "net" | "none" | "unreconciled",
             "expected_total", "salla_total", "tolerance", "vat_rate", "cod_not_charged"}
    price_unit is always NET (before VAT), rounded to `price_digits` like Odoo stores
    it: the KSA sales tax is price-excluded, so Odoo adds 15 % on top and the total must
    come back to Salla's (9.99 gross -> 8.6869565 net; x 3 -> 26.06 + 3.91 = 29.97).

    Product lines: price_without_tax per unit, discount 0 — item discounts become the
    discount line instead, so the coupon is visible as its own line.
    Discount: the order's amounts.discounts, or (when that list is empty) the sum of the
    items' total_discount — never both, they describe the same money.
    Shipping / COD / discount: Salla does not say whether these amounts include VAT, so
    both readings are tried against Salla's own total and the one that reconciles wins
    ("gross" = VAT-inclusive, divided by 1 + VAT). Neither -> "unreconciled", gross is
    assumed and the caller's total check flags the order.
    COD not charged: Salla can send amounts.cash_on_delivery and still leave it out of
    the total (real order 2026-10-18: sub_total 427.76 x 1.15 = total 491.92, COD 10
    nowhere), while a dashboard order did include it. So the same two readings are also
    tried WITHOUT the COD line; the closest reading wins, a reading that charges COD wins
    a tie. "cod_not_charged" = the COD amount dropped that way (0.0 otherwise), for the
    caller's chatter note. With nothing else left, extras_basis is "none"."""
    vat = order.get("vat_rate")
    vat = DEFAULT_VAT_RATE if vat is None else vat
    products = [{
        "sku": l["sku"], "name": l["name"], "quantity": l["quantity"],
        "price_unit": _round(l["unit_net"], price_digits), "line_id": l["line_id"],
    } for l in items]

    raw_extras = []
    if order.get("shipping"):
        raw_extras.append(("shipping", "Shipping", order["shipping"]))
    if order.get("cod"):
        raw_extras.append(("cod", "Cash on Delivery fee", order["cod"]))
    discounts = order.get("discounts") or []
    if not discounts:
        item_disc = _r2(sum(l["discount"] for l in items))
        if item_disc:
            discounts = [{"title": "Salla offer", "amount": item_disc}]
    for disc in discounts:
        raw_extras.append(("discount", disc["title"], -abs(disc["amount"])))

    def plan(basis, with_cod=True):
        div = (1 + vat / 100.0) if basis == "gross" else 1.0
        return [{"kind": k, "name": n, "price_unit": _round(amount / div, price_digits),
                 "quantity": 1}
                for k, n, amount in raw_extras if with_cod or k != "cod"]

    # Preference order = tie order: charging the COD the payload names is the default.
    readings = [("gross", True), ("net", True)]
    if order.get("cod"):
        readings += [("gross", False), ("net", False)]
    candidates = {r: plan(*r) for r in readings}
    salla_total = order.get("total")
    tol = total_tolerance(products + candidates[("gross", True)])
    basis, chosen = ("none" if not raw_extras else "unreconciled"), ("gross", True)
    if raw_extras and salla_total is not None:
        # _r2: float noise must not break the tie order between equal readings.
        diffs = {r: _r2(abs(expected_total(products + candidates[r], vat, price_digits)
                            - salla_total))
                 for r in readings}
        best = min(readings, key=lambda r: diffs[r])   # min keeps the first of a tie
        if diffs[best] <= tol:
            chosen = best
            basis = best[0]
    extras = candidates[chosen]
    cod_dropped = 0.0 if chosen[1] else order["cod"]
    if not extras and basis != "unreconciled":
        basis = "none"
    for extra in extras:
        extra.pop("quantity")
    return {
        "products": products,
        "extras": extras,
        "extras_basis": basis,
        "expected_total": expected_total(products + [dict(e, quantity=1) for e in extras], vat,
                                         price_digits),
        "salla_total": salla_total,
        "tolerance": tol,
        "vat_rate": vat,
        "cod_not_charged": cod_dropped,
    }


def fmt_money(value):
    """An amount for a chatter note or log line: 2 decimals ("370.09", not the float
    370.09000000000003), half away from zero like the currency; "?" when unknown."""
    if value is None or isinstance(value, bool):
        return "?"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "?"
    if not math.isfinite(number):
        return "?"
    return "%.2f" % (_r2(number) + 0.0)   # + 0.0: -0.0 prints "0.00", not "-0.00"


def total_check(odoo_total, salla_total, tolerance):
    """Compare Odoo's amount_total with Salla's total.

    -> ("ok", 0.0) exact; ("adjust", diff) within the rounding tolerance, diff =
    salla - odoo, to be absorbed by a rounding line; ("mismatch", diff) beyond it, or
    ("unknown", None) when Salla's total could not be read."""
    if salla_total is None or odoo_total is None:
        return ("unknown", None)
    diff = _r2(salla_total - odoo_total)
    if diff == 0:
        return ("ok", 0.0)
    if abs(diff) <= tolerance:
        return ("adjust", diff)
    return ("mismatch", diff)


def _line_total(net, vat_rate):
    """VAT-inclusive total of a one-unit line, Odoo per-line rounding."""
    return _r2(net + _r2(net * vat_rate / 100.0))


def adjustment_nets(diff, vat_rate):
    """Net prices of the one-unit rounding line(s) whose VAT-inclusive totals add up to
    exactly `diff` (Odoo per-line tax rounding). [] when diff is 0 or unreachable.

    e.g. Salla sends price_without_tax 8.69 for 9.99 gross: x 3 -> Odoo 29.98, Salla
    29.97, diff -0.01 -> [-0.01] (the tax on 0.01 rounds to 0). Searched around diff / (1 + VAT), not just
    divided: some totals (0.04 at 15 %) no single 2-decimal price reaches — 0.03 lands
    on 0.03 and 0.04 on 0.05 — and then two lines are used."""
    diff = _r2(diff or 0.0)
    if not diff:
        return []
    cents = int(round(diff / (1 + vat_rate / 100.0) * 100))
    span = range(cents - 3, cents + 4)
    for c in sorted(span, key=lambda c: abs(c - cents)):
        if c and _line_total(c / 100.0, vat_rate) == diff:
            return [c / 100.0]
    for a in sorted(span, key=lambda c: abs(c - cents)):
        if not a:
            continue
        rest = _r2(diff - _line_total(a / 100.0, vat_rate))
        b_cents = int(round(rest / (1 + vat_rate / 100.0) * 100))
        for b in (b_cents, b_cents - 1, b_cents + 1):
            if b and _line_total(b / 100.0, vat_rate) == rest:
                return [a / 100.0, b / 100.0]
    return []


# ------------------------------------------------------------------ cursor
def next_cursor(window_end, holds):
    """Where the reconcile cursor goes after a run.

    `holds` = creation dates (naive UTC, or None when unknown) of the orders that must be
    seen again: missing SKU, fetch/processing error, recent payment_pending. With none,
    the cursor advances to window_end; otherwise it stays at the oldest hold, so the next
    window still contains it. An unknown date returns None = do not move the cursor."""
    holds = list(holds or [])
    if not holds:
        return window_end
    if any(h is None for h in holds):
        return None
    return min(min(holds), window_end)


def holds_cursor(status, created_at, now, pending_days=PENDING_HOLD_DAYS):
    """True when a not-importable order must still hold the cursor: payment_pending and
    younger than `pending_days` (it may be paid soon). Cancelled ones never hold."""
    if _canon_status(status) != "payment_pending":
        return False
    return created_at is None or created_at >= now - timedelta(days=pending_days)


# ================================================================== F3: status Odoo -> Salla
# POST /orders/{id}/status {"slug", "send_status_sms"} VALIDATED on staging 2026-10-21
# (order 765844026 = S02378): 201 from under_review, no shipment needed.
SHIPPED_STATUS = "shipped"
# Salla is already there or past it: pushing "shipped" would move the order BACK.
SHIPPED_OR_LATER_STATUSES = {"shipped", "delivering", "delivered", "completed", "closed"}
# Retry delays (minutes) after the 1st, 2nd and 3rd failed push; the 4th failure gives
# up and alerts Ops once. Short first: most failures are a blip (token refresh, 5xx).
PUSH_RETRY_MINUTES = (5, 30, 120)
PUSH_MAX_ATTEMPTS = len(PUSH_RETRY_MINUTES) + 1
_PICKING_FINAL_STATES = {"done", "cancel"}


def ship_push_decision(order_state, salla_status, pushed_status, out_picking_states):
    """Whether Odoo must tell Salla the order is "shipped". -> "push" | "wait" | "skip"

    `out_picking_states` = states of the order's OUTGOING pickings, returns excluded (the
    caller filters). "wait": a delivery is still open (backorder) or none was done — the
    last validation pushes. "skip": nothing to send, ever: the order is not a confirmed
    sale, "shipped" was already sent (remembered, not compared with the previous status:
    webhooks arrive repeated and out of order), or Salla is already at/after shipped or
    cancelled (a push would move it back or revive it)."""
    states = [text(s) for s in (out_picking_states or [])]
    if text(order_state) != "sale":
        return "skip"
    if not states or any(s not in _PICKING_FINAL_STATES for s in states) \
            or "done" not in states:
        return "wait"
    if _canon_status(pushed_status) == SHIPPED_STATUS:
        return "skip"
    status = _canon_status(salla_status)
    if status in SHIPPED_OR_LATER_STATUSES or status in CANCEL_STATUSES \
            or status in NOT_IMPORTABLE_STATUSES:
        return "skip"
    return "push"


def status_payload(slug, send_sms):
    """Body of POST /orders/{id}/status. send_status_sms: Salla texts the customer (the
    customer messages come from Salla, not Odoo)."""
    return {"slug": _canon_status(slug), "send_status_sms": bool(send_sms)}


def is_permanent_error(http_status):
    """True for an answer that retrying cannot change (bad path/slug, 404, 422 invalid
    transition, 401/403 scope after _request's own refresh). None (network, 5xx/429 after
    _request's retries) is transient."""
    try:
        code = int(http_status)
    except (TypeError, ValueError):
        return False
    return 400 <= code < 500 and code not in (408, 425, 429)


def push_next_try(attempts, now, permanent=False):
    """When to retry a failed push after `attempts` failures (>= 1), or None = give up
    and alert Ops. A permanent error gives up at once: waiting hours to report a 422
    only delays the person who has to fix it by hand."""
    if permanent or attempts is None or attempts < 1 or attempts >= PUSH_MAX_ATTEMPTS:
        return None
    return now + timedelta(minutes=PUSH_RETRY_MINUTES[int(attempts) - 1])


def cancel_action(order_state, out_picking_states, has_posted_invoice, already_handled=False,
                  auto_credit_note=False):
    """What Odoo does when Salla says an order we have is cancelled/refunded/returned.
    -> "none" | "cancel" | "credit_note" | "alert_shipped" | "alert_invoiced"

    "none": handled before (repeated / reordered webhook) or already cancelled in Odoo.
    "alert_shipped": an outgoing picking is done, the goods left: nothing changes in
    Odoo, Ops handles the return. Nothing shipped and a posted invoice not yet credited
    (`has_posted_invoice`): "credit_note" (F5: full credit note, then cancel the order
    and its open deliveries) when `auto_credit_note` (the backend auto-invoices), else
    "alert_invoiced" (a human does it). "cancel": cancel the order and its open
    deliveries automatically."""
    if already_handled or text(order_state) == "cancel":
        return "none"
    if any(text(s) == "done" for s in (out_picking_states or [])):
        return "alert_shipped"
    if has_posted_invoice:
        return "credit_note" if auto_credit_note else "alert_invoiced"
    return "cancel"


# ------------------------------------------------------------------ notification
def stock_rows(lines):
    """[(name, ordered, available, storable)] -> ([{"name", "ordered", "available",
    "short"}], any_short). Only storable products can be short; a service never is."""
    rows, any_short = [], False
    for name, ordered, available, storable in lines:
        short = bool(storable) and ordered - (available or 0.0) > 1e-9
        any_short = any_short or short
        rows.append({"name": name, "ordered": ordered, "available": available or 0.0,
                     "short": short})
    return rows, any_short


# ================================================================== F2b: real customer
# customer.{id, full_name, first_name, last_name, mobile, mobile_code, email, city,
# country, country_code} are VALIDATED names (real order detail, 2026-10-18), but the
# values were masked in that log: the FORMAT of mobile / mobile_code is NOT known, hence
# every shape below.
# Shipping address: key PATHS validated on a real webhook body (staging 2026-10-18,
# values masked): data.shipments[].ship_to = the buyer's address, data.shipments[].ship_from
# = the merchant's origin (never used). GET /orders/{id} is the "light" format since
# 2026-09-01: no shipments, no shipping, receiver null, and /shipments needs a scope the
# Partners Portal does not offer. So the address only exists in the webhook body: the
# webhook keeps it on the queue row (webhook_ship_to) and the import merges it back
# (with_ship_to) before buyer() reads it.

# Arabic-Indic and Extended Arabic-Indic digits: an Arabic keyboard types these, and
# Python's \d / int() would accept them while Odoo's and WhatsApp's validators may not.
_DIGITS_TO_ASCII = {**{0x0660 + i: str(i) for i in range(10)},
                    **{0x06F0 + i: str(i) for i in range(10)}}
# Separators people type inside a phone number. Anything else (letters, "ext") = unusable.
_PHONE_SEPARATORS_RE = re.compile(r"[\s\-./()‎‏‪-‮]")
KSA_DIAL_CODE = "966"
# KSA mobile in E.164: +966 5X XXX XXXX. Only mobiles are written on a partner: the
# order confirmation sends WhatsApp to that number, and a landline or a foreign number
# Salla let through is exactly what errored inside action_confirm on Trendyol (§11).
_KSA_MOBILE_RE = re.compile(r"9665[0-9]{8}", re.ASCII)
_SHORT_CODE_RE = re.compile(r"[A-Z]{4}[0-9]{4}", re.ASCII)
_EMAIL_RE = re.compile(r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;.]+")


def ascii_digits(value):
    """text(value) with Arabic-Indic digits turned into 0-9."""
    return text(value).translate(_DIGITS_TO_ASCII)


def e164_ksa(number, code=""):
    """A Saudi mobile as E.164 ("+9665XXXXXXXX"), or "" when it is not one.

    `number`/`code` = customer.mobile / customer.mobile_code as Salla sends them (format
    unverified: "+966" or "966" or "00966" code, number with or without it). Accepts
    "0501234567", "501234567", "+966 50 123 4567", "00966501234567", "966501234567",
    "+966 0 50...", Arabic-Indic digits. A foreign or landline number, letters, or a
    wrong length -> "" (never written: see _KSA_MOBILE_RE)."""
    raw = _PHONE_SEPARATORS_RE.sub("", ascii_digits(number))
    cc = _PHONE_SEPARATORS_RE.sub("", ascii_digits(code)).lstrip("+")
    if cc.startswith("00"):
        cc = cc[2:]
    if not raw:
        return ""
    # The code only matters for a number without its own +/00 prefix: an ISO code
    # ("SA") in mobile_code must not drop a number that is already international.
    if not cc.isdigit():
        cc = ""
    if raw.startswith("+"):
        intl = raw[1:]
    elif raw.startswith("00"):
        intl = raw[2:]
    elif cc:
        # The number may already carry the code ("966501234567" + "+966").
        if raw.startswith(cc) and len(raw) > len(cc) + 8:
            intl = raw
        else:
            intl = cc + (raw[1:] if raw.startswith("0") else raw)
    elif raw.startswith(KSA_DIAL_CODE) and len(raw) == 12:
        intl = raw
    elif raw.startswith("0") and len(raw) == 10:
        intl = KSA_DIAL_CODE + raw[1:]
    elif len(raw) == 9:
        intl = KSA_DIAL_CODE + raw
    else:
        return ""
    if not intl.isdigit():
        return ""
    # "+966 0 50..." : the national trunk 0 typed after the country code.
    if intl.startswith(KSA_DIAL_CODE + "0"):
        intl = KSA_DIAL_CODE + intl[4:]
    return "+" + intl if _KSA_MOBILE_RE.fullmatch(intl) else ""


def clean_email(value):
    """Trimmed, lowercased email, or "" when it does not look like one address."""
    email = text(value).lower()
    return email if _EMAIL_RE.fullmatch(email) else ""


def national_short_code(value):
    """(code, state) for a Saudi National Address short code ("RRRD2929").

    Uppercased, every space/dash removed, Arabic-Indic digits converted, then it must be
    exactly 4 ASCII letters + 4 ASCII digits (wayakit_customization's constraint on
    x_national_short_code raises ValidationError otherwise, which would kill the order).
    state: "ok" | "missing" (nothing sent) | "invalid" (sent, unusable: code is "")."""
    raw = text(value)
    if not raw:
        return "", "missing"
    code = re.sub(r"[\s\-]", "", ascii_digits(raw)).upper()
    if len(code) == 8 and _SHORT_CODE_RE.fullmatch(code):
        return code, "ok"
    return "", "invalid"


def _label(value):
    """A place name that may come as a string or as {"name": ...} (city, district)."""
    if isinstance(value, dict):
        value = value.get("name")
    return " ".join(text(value).split())


def _join(*parts, sep=" "):
    return sep.join(p for p in (" ".join(text(x).split()) for x in parts) if p)


# A ship_to of these types is a place of the merchant (pickup branch, warehouse), never
# the buyer's door. The real value on a shipped order is unknown (masked in the log):
# matched as substrings of the lowercased type, anything else is accepted.
_NOT_BUYER_TYPE_PARTS = ("branch", "merchant", "store", "warehouse", "pickup")
# The only ship_to keys the webhook keeps on the queue row: what shipping_address reads.
# No email/phone/coordinates (customer.* already carries the contact data), nothing else
# of the body. Values are flattened to strings and capped: a queue row is not an archive.
SHIP_TO_KEYS = ("type", "name", "city", "district", "address_line", "address_line_two",
                "street_number", "building_number", "block", "subblock", "postal_code",
                "short_address")
_SHIP_TO_MAX_LEN = 256
# order.status.updated carries a status change, not the order: no address in it.
ADDRESS_EVENTS = {"order.created", "order.updated"}


def delivery_method(order):
    """Canonical delivery_method: "shipping", "pickup" or whatever Salla sends; ""
    when absent."""
    return _canon_status(order.get("delivery_method")) if isinstance(order, dict) else ""


def _ship_to(order):
    """The buyer's ship_to dict from order.shipments[], or None. First entry whose
    ship_to is a non-empty dict of a non-merchant type; ship_from is never looked at."""
    shipments = order.get("shipments")
    if isinstance(shipments, dict):
        shipments = [shipments]
    if not isinstance(shipments, list):
        return None
    for shipment in shipments:
        found = shipment.get("ship_to") if isinstance(shipment, dict) else None
        if not isinstance(found, dict) or not found:
            continue
        kind = text(found.get("type")).lower()
        if any(part in kind for part in _NOT_BUYER_TYPE_PARTS):
            continue
        return found
    return None


def _contains(haystack, needle):
    """`needle` appears in `haystack` as whole words: a plain substring test would find
    block "2" inside building "1234" and drop it."""
    needle = _addr_key(needle)
    return bool(needle) and re.search(r"(?<!\w)%s(?!\w)" % re.escape(needle),
                                      _addr_key(haystack)) is not None


def shipping_address(order):
    """The delivery address of an order, {} when there is none (pickup).

    {"name", "street", "street2", "city", "zip", "short_code", "short_code_state",
     "short_code_raw"}. Real source: shipments[].ship_to (see the section header); the
    older docs-based containers stay as fallbacks. A pickup order -> {} whatever it
    carries (its ship_to, if any, is the branch). Saudi-style lines:
      street  = address_line, else "building_number street_number"; the building number
                is prefixed when address_line lacks it
      street2 = address_line_two, district, block, subblock (", "-joined, parts already
                in street skipped: address_line often repeats the district)
    additional_number is not used: the short code already pins the building.
    The country is ignored on purpose (always SA, see buyer()).
    short_code_raw is PII: for the order's chatter only, never a log line."""
    if not isinstance(order, dict) or delivery_method(order) == "pickup":
        return {}
    addr = _ship_to(order)
    if addr is None:
        for path in ("shipping.address", "shipping.ship_to", "ship_to", "shipping_address",
                     "shipping.receiver.address", "receiver.address", "receiver"):
            found = _dig(order, path)
            if isinstance(found, dict) and found:
                addr = found
                break
    if addr is None:
        return {}
    building = _join(addr.get("building_number"))
    street = _join(addr.get("address_line"))
    if street and building and not _contains(street, building):
        street = _join(building, street)
    street = street or _join(building, addr.get("street_number"), addr.get("street"))
    extra = []
    for part in (_join(addr.get("address_line_two")), _label(addr.get("district")),
                 _join(addr.get("block")), _join(addr.get("subblock"))):
        if part and not _contains(street, part) and part not in extra:
            extra.append(part)
    raw_code = _first(addr, "short_address", "short_code", "national_short_code",
                      "national_address")
    if isinstance(raw_code, dict):
        # Shape unknown: an object may hold the code; if not, it is "sent but unusable".
        raw_code = text(_first(raw_code, "code", "short_address", "short_code", "value")) \
            or ("?" if raw_code else "")
    code, state = national_short_code(text(raw_code))
    out = {
        "name": _join(addr.get("name")) or _join(addr.get("first_name"), addr.get("last_name")),
        "street": street,
        "street2": ", ".join(extra),
        "city": _label(addr.get("city")),
        "zip": ascii_digits(_first(addr, "postal_code", "zip_code", "zip")),
        "short_code": code,
        "short_code_state": state,
        "short_code_raw": raw_code,
    }
    if not (out["street"] or out["city"] or out["zip"] or code):
        return {}
    return out


def webhook_ship_to(payload):
    """The buyer's address of an order.created / order.updated webhook, reduced to
    SHIP_TO_KEYS (string values), or {} (other event, pickup, no usable address).

    Takes the WHOLE webhook body. The result is what the queue row stores (as JSON):
    nothing else of the body survives (no customer, amounts, items, coordinates)."""
    if not isinstance(payload, dict) or event_name(payload) not in ADDRESS_EVENTS:
        return {}
    data = payload.get("data")
    if not isinstance(data, dict) or delivery_method(data) == "pickup":
        return {}
    found = _ship_to(data)
    if not found:
        return {}
    kept = {}
    for key in SHIP_TO_KEYS:
        value = found.get(key)
        value = _label(value) if isinstance(value, dict) else \
            ("" if isinstance(value, (list, bool)) else " ".join(text(value).split()))
        if value:
            kept[key] = value[:_SHIP_TO_MAX_LEN]
    # Stored only if shipping_address can use it: a name alone is not an address.
    return kept if shipping_address({"shipments": [{"ship_to": kept}]}) else {}


def dump_ship_to(ship_to):
    """JSON text for the queue row, or None (stored as NULL) when there is nothing."""
    return json.dumps(ship_to, ensure_ascii=False, sort_keys=True) if ship_to else None


def load_ship_to(value):
    """Queue row JSON -> SHIP_TO_KEYS dict; {} for empty, corrupt or non-dict JSON.
    Never raises: a bad row must cost the address, not the order."""
    if not value or not isinstance(value, (str, bytes)):
        return {}
    try:
        data = json.loads(value)
    except (ValueError, TypeError, RecursionError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: text(data[k])[:_SHIP_TO_MAX_LEN] for k in SHIP_TO_KEYS
            if k in data and not isinstance(data[k], (dict, list)) and text(data[k])}


def with_ship_to(order, ship_to):
    """`order` (API detail) with the webhook's address merged in, as a NEW dict.

    Only when the detail itself has no shipments[].ship_to (the light API format never
    has one; if Salla brings it back, the API's wins). A pickup order is left alone."""
    if not isinstance(order, dict):
        return order
    if not ship_to or delivery_method(order) == "pickup" or _ship_to(order) is not None:
        return order
    merged = dict(order)
    merged["shipments"] = [{"ship_to": dict(ship_to)}]
    return merged


def buyer(order):
    """Everything F2b writes on the customer, from an order detail.

    {"ref", "name", "phone" (E.164 KSA mobile or ""), "phone_state" ("ok" | "missing" |
     "invalid"), "email" ("" if unusable), "city" (customer.city, a fallback when there
    is no shipping address), "address" (shipping_address(order), {} for pickup),
    "address_missing" (True: delivery_method "shipping" and no address at all)}.
    Pass the detail through with_ship_to() first: the API detail has no address.
    The country is NOT read: the partner is always SA (fiscal position, VAT must keep
    matching Salla), and a real test customer carried country_code "MX".
    All values are PII: never log them."""
    customer = order.get("customer") if isinstance(order, dict) else None
    cust = customer if isinstance(customer, dict) else {}
    base = order_customer(order)
    raw_phones = [(cust.get("mobile"), cust.get("mobile_code")), (cust.get("phone"), "")]
    phone, given = "", False
    for number, code in raw_phones:
        if text(number):
            given = True
            phone = e164_ksa(number, code)
            if phone:
                break
    address = shipping_address(order)
    return {
        "ref": base["ref"],
        "name": base["name"],
        "phone": phone,
        "phone_state": "ok" if phone else ("invalid" if given else "missing"),
        "email": clean_email(cust.get("email")),
        "city": _label(cust.get("city")),
        "address": address,
        # Shipped order but no address anywhere (webhook lost or without it): the
        # caller alerts so Ops adds it by hand. Pickup / unknown method: nothing to say.
        "address_missing": not address and delivery_method(order) == "shipping",
    }


def fill_empty(current, incoming):
    """The subset of `incoming` to write on an existing record: only keys whose current
    value is empty/False and whose incoming value is not. Ops data always wins."""
    return {k: v for k, v in (incoming or {}).items()
            if v not in (None, "", False) and not (current or {}).get(k)}


def _addr_key(value):
    return " ".join(text(value).casefold().split())


def same_address(current, incoming):
    """True when `current` (a partner's street/city) already is `incoming`'s address:
    same street and same city, case- and space-insensitive. An empty street is never
    "the same" (nothing to compare)."""
    a, b = current or {}, incoming or {}
    if not _addr_key(a.get("street")) or not _addr_key(b.get("street")):
        return False
    return (_addr_key(a.get("street")) == _addr_key(b.get("street"))
            and _addr_key(a.get("city")) == _addr_key(b.get("city")))


def address_fillable(current, incoming):
    """True when `incoming` (a shipping address) may be written on the contact itself:
    the contact has no street, street2 or zip, and its city is empty or the same one
    (a pickup order fills only the city; the first shipped order must still land on
    the contact, not on a delivery child), or it already is that address."""
    a = current or {}
    if same_address(a, incoming):
        return True
    if any(_addr_key(a.get(k)) for k in ("street", "street2", "zip")):
        return False
    city = _addr_key(a.get("city"))
    return not city or city == _addr_key((incoming or {}).get("city"))


# Partner types that are an ADDRESS of someone, not a customer: never matched as buyer.
_ADDRESS_TYPES = {"delivery", "invoice", "other", "private"}


def pick_partner(candidates, ref=""):
    """Choose the contact a phone/email match resolves to. -> (id or None, eligible)

    `candidates` = [{"id", "salla_customer_id", "active", "type", "parent_id",
    "internal"}]. Not eligible: a contact already linked to ANOTHER Salla customer id
    (it is somebody else who shares the phone/email; the buyer gets their own contact
    instead), address-type rows (delivery/invoice/other children), and "internal" ones
    (an employee's user partner or a company's own partner: an employee buying on Salla,
    or a buyer sharing the office phone, must not turn Wayakit's own contacts into
    customers and receive their WhatsApp). Among the rest, deterministic:
    linked to this same ref first, then not yet linked, active first, top-level contact
    (no parent) first, lowest id. `eligible` > 1 means ambiguous: the caller notes it."""
    ref = clean_id(ref)
    pool = []
    for c in candidates or []:
        linked = clean_id(c.get("salla_customer_id"))
        if linked and linked != ref:
            continue
        if text(c.get("type")) in _ADDRESS_TYPES or c.get("internal"):
            continue
        pool.append(c)
    if not pool:
        return None, 0
    pool.sort(key=lambda c: (not (ref and clean_id(c.get("salla_customer_id")) == ref),
                             not c.get("active", True), bool(c.get("parent_id")), c["id"]))
    return pool[0]["id"], len(pool)


# ================================================================== F5: invoices, payments
# Proposal C (team meeting 2026-10-22): Odoo is the only ZATCA issuer. It invoices the
# confirmed Salla order, registers the payment in the journal of the provider that
# collected it, and uploads the ZATCA-accepted PDF to Salla (POST /orders/invoices,
# validated on the demo store 2026-10-22). Payment method slugs are from the docs and
# NOT yet seen on a real paid order: unknown -> no payment + alert, never a guess.
PAYMENT_JOURNAL_KEYS = ("salla_payments", "tabby", "tamara")
# Salla Payments (mada, Visa/Mastercard, Apple Pay): settled by Salla every 48 h.
SALLA_PAYMENTS_METHODS = {"mada", "credit_card", "creditcard", "card", "bank_card",
                          "debit_card", "visa", "mastercard", "master_card", "apple_pay",
                          "applepay"}
# Tabby/Tamara settle on their own accounts. Matched as a substring: the docs show
# tabby_installment / tamara_installment, and a pay-later variant must not fall
# through to "unknown".
_BNPL_MARKERS = (("tabby", "tabby"), ("tamara", "tamara"))
TAX_INVOICE = "tax_invoice"
CREDIT_NOTE = "credit_note"
# GET /orders/invoices `slug`: 1 = tax invoice, 2 = credit note (demo store).
_INVOICE_SLUGS = {"1": TAX_INVOICE, "2": CREDIT_NOTE}
# Salla refuses an invoice while the order waits for review (422 "the order status does
# not allow creating an invoice now", demo 2026-10-22): moved to in_progress first.
# Only these: every other status is either already past it (never moved back) or not
# paid (payment_pending must never be marked as being prepared).
PRE_UPLOAD_STATUSES = {"under_review"}
IN_PROGRESS_STATUS = "in_progress"
INVOICE_PDF_ROUTE = "/sl/invoice"
_FILENAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")


def parse_method_overrides(value):
    """Backend "Payment Method Mapping" text -> ({slug: journal key}, [bad entries]).

    One "slug = key" per line (commas work too), key in PAYMENT_JOURNAL_KEYS, e.g.
    "stc_pay = salla_payments". Lets Accounting map a slug seen on a real order without a
    code change. Bad entries are returned, not raised: the caller reports them."""
    mapping_, bad = {}, []
    for raw in re.split(r"[\n,;]+", text(value)):
        entry = raw.strip()
        if not entry:
            continue
        slug, sep, key = entry.partition("=")
        if not sep:
            slug, sep, key = entry.partition(":")
        slug, key = _canon_status(slug.strip()), _canon_status(key.strip())
        if not sep or not slug or key not in PAYMENT_JOURNAL_KEYS:
            bad.append(entry[:60])
            continue
        mapping_[slug] = key
    return mapping_, bad


def payment_journal_key(method, overrides=None):
    """Salla payment method slug -> "salla_payments" | "tabby" | "tamara" | "".

    "" = unknown, empty, cash on delivery or a bank transfer: no payment is registered,
    Accounting is alerted. COD and transfers stay "" even if the backend mapping names
    them: their money is not collected by a provider when the order is confirmed. Then
    the backend override, then the known slugs."""
    slug = _canon_status(method)
    if not slug or is_cod(slug) or is_bank_transfer(slug):
        return ""
    override = (overrides or {}).get(slug)
    if override in PAYMENT_JOURNAL_KEYS:
        return override
    if slug in SALLA_PAYMENTS_METHODS:
        return "salla_payments"
    for marker, key in _BNPL_MARKERS:
        if marker in slug:
            return key
    return ""


def should_auto_invoice(order_state, total_mismatch, invoice_status, has_invoice,
                        auto_invoice=True, failed_before=False):
    """Whether Odoo invoices a Salla order by itself: a confirmed sale ("sale"), its
    total equal to Salla's, something to invoice, no invoice yet (draft included: a
    draft left for Accounting is theirs), and no earlier automatic attempt that
    failed (Accounting got the alert; retrying every 30 min would only repeat it)."""
    return bool(auto_invoice) and text(order_state) == "sale" and not total_mismatch \
        and text(invoice_status) == "to invoice" and not has_invoice and not failed_before


def upload_kind(move_type):
    """account.move type -> Salla invoice type, "" for anything Salla does not take."""
    return {"out_invoice": TAX_INVOICE, "out_refund": CREDIT_NOTE}.get(text(move_type), "")


def upload_decision(move_type, move_state, edi_state, require_zatca, salla_invoice_id="",
                    edi_error=False, origin_state="done", cancelled_before_upload=False):
    """Whether a Salla order's invoice / credit note goes to Salla now.
    -> "upload" | "wait" | "skip" | "no_zatca" | "zatca_error" | "origin_failed"

    "skip": never (not a customer invoice/credit note, not posted, or ALREADY uploaded:
    Salla accepts the same order's tax invoice twice, so the stored id is the guard).
    A tax invoice never uploaded whose order Salla cancelled and that a credit note
    already reverses (`cancelled_before_upload`) is skipped too: the customer never got
    it, and Salla may refuse a cancelled order's invoice; its credit note then skips as
    well (origin "skipped"). ZATCA has both either way.
    With `require_zatca`: edi_state "sent" (account_edi: every web-service document
    accepted) is required; "to_send" waits, or "zatca_error" when a document is
    blocked in error (`edi_error`); no EDI at all (edi_state empty: a journal without
    ZATCA) is "no_zatca", it would wait forever otherwise. Credit notes follow their
    invoice: `origin_state` = upload state of the invoice it credits ("done" uploads,
    "pending" waits, "failed" -> "origin_failed": waits too but the caller alerts once,
    anything else = no uploaded invoice -> "skip")."""
    kind = upload_kind(move_type)
    if not kind or text(move_state) != "posted" or clean_id(salla_invoice_id):
        return "skip"
    if kind == TAX_INVOICE and cancelled_before_upload:
        return "skip"
    if require_zatca:
        edi = text(edi_state)
        if edi in ("cancelled", "to_cancel"):
            return "skip"
        if edi == "to_send":
            return "zatca_error" if edi_error else "wait"
        if edi != "sent":
            return "no_zatca"
    if kind == CREDIT_NOTE:
        origin = text(origin_state)
        if origin == "pending":
            return "wait"
        if origin == "failed":
            return "origin_failed"
        if origin != "done":
            return "skip"
    return "upload"


def status_before_upload(kind, salla_status):
    """The status Salla must be set to before a tax invoice upload, "" for none. Only
    forward from a pre-processing status; credit notes never move the order."""
    if kind == TAX_INVOICE and _canon_status(salla_status) in PRE_UPLOAD_STATUSES:
        return IN_PROGRESS_STATUS
    return ""


def pdf_filename(name):
    """Move name -> a URL-safe file name ending in .pdf ("INV/2026/00012" ->
    "INV_2026_00012.pdf"). Salla checks the URL looks like a PDF."""
    base = _FILENAME_UNSAFE_RE.sub("_", text(name)).strip("_")[:80]
    return "%s.pdf" % (base or "invoice")


def public_base_url(webhook_url, fallback=""):
    """https://host[:port] Salla can reach: the webhook URL's origin (on odoo.sh
    web.base.url often names another build, webhook_url is kept right by hand), else
    `fallback` (web.base.url). "" when neither is an https URL with a host."""
    for candidate in (webhook_url, fallback):
        try:
            parts = urlsplit(text(candidate))
        except ValueError:
            continue
        if parts.scheme == "https" and parts.hostname and not parts.username \
                and not parts.password:
            return "https://%s" % parts.netloc.lower()
    return ""


def invoice_pdf_url(base_url, move_id, token, name):
    """Public PDF URL of a move (controller route INVOICE_PDF_ROUTE). "" without a base,
    an id or a token."""
    if not base_url or not move_id or not text(token):
        return ""
    return "%s%s/%s/%s/%s" % (base_url.rstrip("/"), INVOICE_PDF_ROUTE, int(move_id),
                              text(token), pdf_filename(name))


def invoice_upload_payload(order_id, kind, url):
    """Body of POST /orders/invoices. order_id as an int (the docs type it so)."""
    oid = clean_id(order_id)
    return {"order_id": int(oid) if oid.isdigit() else oid, "type": kind,
            "data_type": "url", "url": url}


def uploaded_invoice_id(response):
    """Salla invoice id from the POST answer {"data": {"id": ...}}, "" if absent."""
    data = response.get("data") if isinstance(response, dict) else None
    return clean_id(data.get("id")) if isinstance(data, dict) else ""


def salla_invoice_kind(entry):
    """TAX_INVOICE / CREDIT_NOTE of a GET /orders/invoices entry, "" if unreadable."""
    if not isinstance(entry, dict):
        return ""
    kind = _INVOICE_SLUGS.get(text(entry.get("slug")))
    if kind:
        return kind
    label = _canon_status(entry.get("type"))
    if "credit" in label:
        return CREDIT_NOTE
    if "tax" in label or label == "invoice":
        return TAX_INVOICE
    return ""


def adopt_uploaded_invoice(salla_invoices, kind, known_ids):
    """Before a POST: an invoice of `kind` Salla already holds that came from an upload
    Odoo never recorded (the POST succeeded, the commit did not). Returns its id, or ""
    = upload. The caller only asks for a move that already attempted a POST (marker
    committed before it): a move that never posted cannot have been lost, and must not
    take another move's (or a hand-uploaded) document. Uploaded documents carry no invoice_number (demo 2026-10-22); Salla's own
    invoices (store in "Salla" invoicing mode) do, and are never adopted. Ids already on
    another Odoo move (`known_ids`) are skipped. Highest id = newest."""
    known = {clean_id(k) for k in (known_ids or []) if clean_id(k)}
    found = []
    for entry in salla_invoices or []:
        if salla_invoice_kind(entry) != kind or clean_id(entry.get("invoice_number")):
            continue
        sid = clean_id(entry.get("id"))
        if sid and sid not in known:
            found.append(sid)
    if not found:
        return ""
    return max(found, key=lambda s: (s.isdigit(), int(s) if s.isdigit() else 0, s))
