import json
import logging

from odoo import http, SUPERUSER_ID
from odoo.http import content_disposition, request
from odoo.tools import consteq

from ..models import mapping

_logger = logging.getLogger(__name__)


class SallaWebhookController(http.Controller):

    # type="http" (not "json"): Salla POSTs plain JSON, not JSON-RPC.
    # "/sl/", mirroring Trendyol's "/ty/": marketplaces have rejected URLs carrying their
    # own name before, and a Cloudflare Skip rule for this exact path must exist on the
    # prod domain before the URL is configured in the Salla app.
    # Answer fast: Salla waits ~30 s and retries 3 times, so nothing slow belongs here.
    @http.route("/sl/webhook", type="http", auth="public",
                methods=["POST"], csrf=False)
    def salla_webhook(self, **kwargs):
        # The signature covers the RAW bytes; never verify re-serialized JSON.
        raw = request.httprequest.get_data() or b""
        signature = request.httprequest.headers.get("X-Salla-Signature")
        if not signature:
            return request.make_response("unauthorized", status=401)

        # The only sudo() before authentication, read-only: webhook_secret is a
        # system-only field and the caller is the public user.
        candidates = request.env["salla.backend"].sudo().search(
            [("active", "=", True), ("webhook_secret", "!=", False)])
        matched = candidates.filtered(
            lambda b: mapping.verify_signature(raw, b.webhook_secret, signature))
        if not matched:
            return request.make_response("unauthorized", status=401)

        if not raw.strip():
            return request.make_response("ok:0", status=200)
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            # Signed but unparseable: log the shape we got, never the bytes — a
            # malformed authorize body would still carry the tokens.
            _logger.warning("Salla webhook: unparseable body (%s bytes)", len(raw))
            return request.make_response("bad json", status=400)
        if not isinstance(payload, dict):
            _logger.warning("Salla webhook: body is a %s, not an object", type(payload).__name__)
            return request.make_response("bad json", status=400)

        event = mapping.event_name(payload)
        merchant = mapping.merchant_id(payload)
        backend = matched._pick_for_event(merchant, event)
        # Default: key structure only. From F2 on these bodies are customer PII
        # (confidential), and authorize bodies are credentials. The full body (credentials
        # and PII masked) only when an admin turned log_payloads on to check field names
        # against the first real payloads — and never for authorize.
        if backend and backend.log_payloads and event != mapping.AUTHORIZE_EVENT:
            _logger.info("Salla webhook %s (store %s): %s", event, merchant or "?",
                         json.dumps(mapping.redact(payload)))
        else:
            _logger.info("Salla webhook %s (store %s), payload keys: %s", event,
                         merchant or "?", mapping.payload_keys(payload))
        if not backend:
            # 200, not an error: the signature is good, so retrying would change nothing.
            if event == mapping.AUTHORIZE_EVENT:
                # Tokens just issued and dropped: the app has to be reinstalled unless the
                # binding is fixed. Keys show whether merchant_id() misread the payload.
                _logger.warning("Salla webhook %s for store %s NOT routed: no unbound "
                                "backend and no backend bound to that store. Payload keys: %s",
                                event, merchant or "?", mapping.payload_keys(payload))
                return request.make_response("ok:0", status=200)
            _logger.warning("Salla webhook %s for store %s matches no backend allowed to "
                            "take it; ignored.", event, merchant or "?")
            return request.make_response("ok:0", status=200)
        try:
            # with_user, not sudo(): OdooBot is the honest author for an inbound call
            # (same reasoning as the Trendyol controller).
            status = backend.with_user(SUPERUSER_ID)._process_webhook(payload)
            return request.make_response("ok:%s" % status, status=200)
        except Exception:
            # 500 so Salla retries. Event + backend only, never the body: the traceback
            # never carries locals, and the key structure was logged above.
            _logger.exception("Salla webhook %s processing failed for backend %s",
                              event, backend.name)
            return request.make_response("error", status=500)

    # F5: Salla downloads the invoice PDF from here when Odoo uploads it (POST
    # /orders/invoices with data_type=url) and keeps its own copy on its CDN, so this only
    # has to answer at upload time. "/sl/", never the marketplace name; on the prod domain
    # it needs the same Cloudflare Skip rule as /sl/webhook. Token = the move's portal
    # access token, created at posting (never here: a GET must not write). Every refusal
    # is the same 404, and nothing here logs the token.
    @http.route("/sl/invoice/<int:move_id>/<string:token>/<string:filename>", type="http",
                auth="public", methods=["GET"], csrf=False)
    def salla_invoice_pdf(self, move_id, token, filename, **kwargs):
        move = request.env["account.move"].sudo().browse(move_id).exists()
        if not move or not move.access_token or not token \
                or not consteq(move.access_token, token) \
                or move.state != "posted" \
                or move.move_type not in ("out_invoice", "out_refund") \
                or not move.salla_sale_order_id.salla_order_id:
            return request.not_found()
        try:
            pdf = move.with_user(SUPERUSER_ID)._salla_invoice_pdf()
        except Exception:
            _logger.exception("Salla invoice PDF: rendering failed for move %s", move_id)
            return request.make_response("error", status=500)
        return request.make_response(pdf, headers=[
            ("Content-Type", "application/pdf"),
            ("Content-Length", len(pdf)),
            ("Content-Disposition", content_disposition(mapping.pdf_filename(move.name),
                                                        disposition_type="inline")),
            ("X-Robots-Tag", "noindex"),
            ("Cache-Control", "private, no-store"),
        ])
