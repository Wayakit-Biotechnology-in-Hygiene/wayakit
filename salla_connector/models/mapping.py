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
                  "national", "district", "block", "building")
# ip / user agent identify the customer's device (source_details on real orders). Exact
# keys only: "ip" as a substring would mask "shipping", "description", "zip"...
_PII_KEYS = {"name", "city", "location", "ip", "user-agent", "user_agent", "useragent"}
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

ORDER_EVENTS = {"order.created", "order.updated", "order.status.updated"}
STATUS_EVENT = "order.status.updated"

# Status slugs (status.slug). Validate against a real payload: a store can rename
# statuses ("customized"), the slug is what should stay stable.
# Not imported: nothing has been paid (payment_pending) or the order is dead.
NOT_IMPORTABLE_STATUSES = {"payment_pending", "canceled", "cancelled", "restored",
                           "restoring", "refunded", "deleted"}
# Imported AND confirmed: the order is paid (or COD accepted) and must be prepared.
CONFIRMABLE_STATUSES = {"in_progress", "processing", "completed",
                        "shipped", "delivering", "delivered", "ready_for_pickup"}
# "Under review": Salla holds it for the merchant to accept. For a prepaid method it is
# not proof of payment (bank transfer receipts sit here), so it is confirmed only for
# cash on delivery, where nothing is paid upfront anyway. Lead's call, Mafer may change it.
REVIEW_STATUSES = {"under_review"}
# Payment method slugs meaning cash on delivery. VALIDATE AGAINST A REAL PAYLOAD: both the
# field (payment_method) and the slug are taken from the docs.
COD_PAYMENT_METHODS = {"cod", "cash_on_delivery"}
# On an order we already have: posted once as a "needs manual handling" notice, never
# automated (same call as Trendyol Phase 3).
CANCEL_STATUSES = {"canceled", "cancelled", "restored", "restoring", "refunded"}
# Unknown slug (a custom status, or the field misread): imported as a quotation, never
# confirmed, so a human looks at it. Losing it would be worse.

# payment_pending orders hold the reconcile cursor so the cron still sees them once paid
# (bank transfer) even if the status webhook is lost; abandoned ones stop holding after:
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


def should_confirm(status, payment_method=""):
    """True only for a status known to mean "paid (or COD accepted), prepare it".
    under_review counts only with a cash-on-delivery payment method."""
    status = _canon_status(status)
    if status in CONFIRMABLE_STATUSES:
        return True
    return status in REVIEW_STATUSES and is_cod(payment_method)


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

    order.created/updated: data IS the order (data.id). order.status.updated: data may
    be the status change itself, whose own `id` is NOT the order id, so the order
    reference (data.order.id / data.order_id) is tried first for that event."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return ""
    data = payload["data"]
    paths = ("order.id", "order_id", "id") if event_name(payload) == STATUS_EVENT \
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


def order_payment_method(order):
    """Canonical payment method slug, "" if absent. VALIDATE AGAINST A REAL PAYLOAD:
    docs show a bare string ("cod", "bank", "credit_card"); an object is accepted too."""
    value = order.get("payment_method") if isinstance(order, dict) else None
    if isinstance(value, dict):
        value = value.get("slug") or value.get("name")
    if not value and isinstance(order, dict):
        value = _dig(order, "payment.method")
    return _canon_status(value)


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
