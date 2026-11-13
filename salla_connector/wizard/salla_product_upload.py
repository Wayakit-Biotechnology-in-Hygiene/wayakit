import base64
import html
import io
import logging
import time
import zipfile
from urllib.parse import quote

from odoo import fields, models, tools, _
from odoo.exceptions import AccessError, UserError

from ..models import mapping
from ..models.salla_backend import SallaApiError

_logger = logging.getLogger(__name__)

# Stop STARTING products after this: an HTTP worker gets killed past its time limit and
# the whole report would be lost. Re-runs are idempotent (upsert by SKU, images counted
# on Salla first), so "run again" simply continues. Capped at half the server's
# limit_time_real (Odoo's default is 120 s): one slow product (images, retries) must
# still fit in what is left.
TIME_BUDGET = 240
TIME_BUDGET_MIN = 30
# Excel's per-cell limit: a longer cell would be cut, and the result sheet is meant to
# be uploaded again.
XLSX_CELL_MAX = 32767
# Salla's image guidance: square, 1200 px, at most 300 KB. Only warnings: Salla resizes.
IMAGE_MAX_BYTES = 300 * 1024
IMAGE_SIDE = 1200
# Zip-bomb guard per entry; a product photo is never anywhere near this.
ZIP_ENTRY_MAX_BYTES = 20 * 1024 * 1024
# Bytes read per image for its size: PNG and WebP keep it in the first 30 bytes, a JPEG
# after its EXIF / ICC segments (read whole, up to ZIP_ENTRY_MAX_BYTES, when not here).
IMAGE_HEADER_BYTES = 64 * 1024
# Keyword-search pages read per SKU (60 items each). Past this the lookup is
# inconclusive and the product is not created.
KEYWORD_MAX_PAGES = 5
# SAL-92: claims on hold until validated. Editable per run in the wizard.
DEFAULT_BLOCKED_PHRASES = (
    "baby-safe", "baby safe", "SASO", "salmonella", "microbiome", "won't scratch",
    "non-corrosive", "no allergies", "safe if licked", "lick",
)
RESULT_COLUMNS = ("salla_product_id", "salla_variant_id", "upload_action", "upload_status",
                  "upload_message")
IMAGE_MIMETYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                   "webp": "image/webp", "gif": "image/gif"}
STATUS_RANK = {"ok": 0, "warning": 1, "error": 2}
TEMPLATE_NAME = "salla_products_template.xlsx"
# Instructions sheet of the template: (column, level, required, notes).
TEMPLATE_HELP = (
    ("handle", "product", "yes", "Groups rows into one Salla product: lowercase letters, "
     "digits and dashes. Product-level columns are read from the first row of each "
     "handle; leave them empty on its later rows."),
    ("sku", "variant", "yes", "Odoo internal reference (default_code). Unique in the file."),
    ("name_ar / name_en", "product", "AR yes", "Product name."),
    ("subtitle_ar / subtitle_en", "product", "no", "Salla subtitle."),
    ("promotion_title_ar / promotion_title_en", "product", "no", "Salla promotion title."),
    ("description_ar / description_en", "product", "AR yes", "Plain text: a blank line "
     "starts a paragraph, lines starting with '- ' become a bullet list. A value starting "
     "with '<' is sent as HTML."),
    ("seo_title_ar/en, seo_description_ar/en", "product", "no", "SEO title / description."),
    ("category_ids", "product", "no", "Salla category ids separated by ';' (numbers)."),
    ("product_type", "product", "no", "product (default) or group_products."),
    ("option1_name_ar/en, option2_name_ar/en", "product", "AR yes if values are used",
     "Option names, e.g. Size, Scent. At most 2 options. The _en names are sent as "
     "Salla's English translation (the Arabic is never changed); an _en name left "
     "empty, or the same as the Arabic, has to be entered by hand in Salla."),
    ("option1_value_ar/en, option2_value_ar/en", "variant", "AR yes when the option name "
     "is set", "One row per combination. The _en value is sent as Salla's English "
     "translation of that value: give one Arabic value the same English on every row "
     "(empty rows take it from the others). Salla creates EVERY combination of the option "
     "values (e.g. 2 sizes x 3 scents = 6 variants); a combination without a row gets the "
     "lowest price and default stock: give it a row, or disable it or set it out of stock "
     "in Salla before publishing. The uploader never adds, removes or renames options or "
     "values of an existing product (only their English translation is sent): a new "
     "value must be added in Salla by hand."),
    ("price", "variant", "yes", "SAR, above 0, as the store expects it (VAT setting)."),
    ("sale_price", "variant", "no", "Below price."),
    ("stock", "variant", "no", "Whole number. Blank = not sent."),
    ("weight / weight_unit", "variant", "yes to create", "Above 0; unit kg (default) or g. "
     "Salla refuses to create a product without a weight (422): a NEW product needs a "
     "weight on at least one row (with options, the first row with a weight is sent on "
     "the create; give every row its own). Not required to update an existing product."),
    ("barcode", "variant", "no", "Keep this column formatted as Text so leading zeros "
     "survive."),
    ("image_1 ... image_10", "product", "no", "File names inside the images ZIP, in display "
     "order (1 = main). Square, 1200 px, up to 300 KB."),
    ("image_alt", "product", "no", "One alt text for all the images."),
    ("variant_image", "variant", "no", "Photo of this row's option1 value (e.g. the pack "
     "shot of the size): the file name of one of image_1 ... image_10. The same file on "
     "every row of one option1 value; either every row has one or none. Salla then shows "
     "option1 as image swatches (the value name under each) and switches the main photo "
     "when the customer picks one. Not for a product without options."),
    ("salla_product_id", "product", "no", "Filled in the result sheet; when set it is used "
     "first to find the product."),
    ("", "", "", ""),
    ("Never in this sheet", "", "", "Ingredients and safety data, SASO data, cost prices, "
     "or claims on hold (see the blocked phrases of the wizard)."),
    ("Products", "", "", "Created HIDDEN, matched by SKU on every run; nothing is ever "
     "deleted or published by the uploader."),
)


def _data(response):
    """`data` of a Salla answer as a dict, {} otherwise."""
    data = response.get("data") if isinstance(response, dict) else None
    return data if isinstance(data, dict) else {}


def _basename(name):
    return (name or "").replace("\\", "/").rsplit("/", 1)[-1].lower()


def _error_text(exc):
    return exc.args[0] if isinstance(exc, UserError) and exc.args else str(exc)


def _pillow_webp():
    """True when this server's Pillow can open and write WebP. features.check("webp")
    is not enough: it only tests the C library, while Odoo's image tools stop Pillow
    from loading its format plugins (Image.preinit(); Image._initialized = 2), so WebP
    is not registered in a worker until its plugin is imported (spike 2026-11-10 on
    odoo.sh: img.save(buf, "WEBP") -> KeyError 'WEBP')."""
    try:
        Image = _webp_image()
        # SAVE, not only OPEN: Pillow registers the opener even without libwebp.
        return "WEBP" in Image.OPEN and "WEBP" in Image.SAVE
    except Exception:
        return False


def _webp_image():
    """PIL.Image with the WebP plugin registered. The import registers WebP for the
    rest of the worker's life, not only for this wizard: other image code running in
    that worker can then decode a WebP too, beyond Odoo's minimal preloaded set.
    Accepted: a format Pillow ships, and only reached by the upload's WebP paths."""
    from PIL import Image, WebPImagePlugin  # noqa: F401  (registers WEBP)
    return Image


def _read_entry(archive, info):
    """Bytes of a ZIP entry, read with a cap: the declared size is the archive's word,
    not a guarantee. ValueError past ZIP_ENTRY_MAX_BYTES."""
    with archive.open(info) as stream:
        content = stream.read(ZIP_ENTRY_MAX_BYTES + 1)
    if len(content) > ZIP_ENTRY_MAX_BYTES:
        raise ValueError("over %s MB" % (ZIP_ENTRY_MAX_BYTES // (1024 * 1024)))
    return content


class SallaProductUpload(models.TransientModel):
    _name = "salla.product.upload"
    _description = "Salla Product Upload"

    def _default_backend(self):
        # With two backends (demo + real store) Apply would hit whichever comes first:
        # the user has to pick.
        backends = self.env["salla.backend"].search([], limit=2)
        return backends if len(backends) == 1 else False

    # Not required at field level: Download Template saves the wizard and needs no
    # backend. action_run checks it.
    backend_id = fields.Many2one(
        "salla.backend", string="Salla Backend", ondelete="cascade",
        default=_default_backend)
    file = fields.Binary(string="Master Sheet",
                         help="One row per SKU, sheet \"Products\" (.xlsx) or a .csv. "
                              "Download the template for the columns.")
    file_name = fields.Char()
    images_zip = fields.Binary(string="Images ZIP",
                               help="The files named in image_1 ... image_10.")
    images_zip_name = fields.Char()
    mode = fields.Selection(
        [("dry_run", "Dry Run"), ("apply", "Apply")], required=True, default="dry_run",
        help="Dry Run only reads Salla and reports what Apply would do. Apply writes to "
             "Salla and needs an administrator.")
    step = fields.Selection(
        [("all", "Everything"), ("images", "Images Only")], required=True, default="all",
        help="Images Only: attach missing images to products already in Salla; nothing "
             "else is created or changed.")
    handles = fields.Char(
        string="Only Handles",
        help="Optional, comma-separated: run only these handles of the sheet.")
    blocked_phrases = fields.Text(
        default=lambda self: "\n".join(DEFAULT_BLOCKED_PHRASES),
        help="One per line. A product whose copy contains one is blocked in Apply and "
             "flagged in Dry Run. Claims on hold must not reach the store.")
    convert_webp = fields.Boolean(
        string="Convert WebP to JPEG",
        help="Re-encode WebP images as JPEG (quality 90) before uploading them. Needs "
             "Pillow with WebP support; the run refuses to start without it.")
    result_html = fields.Html(string="Result", readonly=True)
    result_file = fields.Binary(string="Result Sheet", readonly=True)
    result_file_name = fields.Char()
    template_file = fields.Binary(readonly=True)

    # ------------------------------------------------------------------ buttons
    def action_download_template(self):
        self.ensure_one()
        self.template_file = base64.b64encode(self._template_xlsx())
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/%s/%s/template_file/%s?download=true" % (
                self._name, self.id, TEMPLATE_NAME),
            "target": "self",
        }

    def action_run(self):
        self.ensure_one()
        self._check_run_access()
        started = time.monotonic()
        if not self.backend_id:
            raise UserError(_("Pick the Salla backend (store) to run against first."))
        if not self.file:
            raise UserError(_("Upload the master sheet (.xlsx or .csv) first."))
        if self.convert_webp and not _pillow_webp():
            # Refused before any read, dry-run included: Apply would fail on the first
            # WebP of each product and stop its images there.
            raise UserError(_("This server cannot convert WebP (Pillow without WebP): "
                              "convert the images to JPEG before zipping, or leave the "
                              "toggle off."))
        try:
            rows = mapping.read_upload_rows(base64.b64decode(self.file), self.file_name or "")
        except ValueError as e:
            raise UserError(str(e))
        products, errors, warnings = mapping.parse_upload_rows(rows)
        selected = self._select(products, warnings)
        archive, entries = self._open_zip(warnings)
        try:
            per_product = self._distribute(products, warnings)
            self._check_odoo_skus(selected, per_product, warnings)
            self._check_images(selected, archive, entries, per_product, warnings)
            self._check_claims(selected, per_product)
            results, left = self._run_products(selected, per_product, archive, entries,
                                               started)
        finally:
            if archive:
                archive.close()
        if not left and self.handles and len(selected) < len(products):
            # Only Handles may be left over from a time-budget stop: without this, an
            # Apply right after a finished dry-run continuation silently skips the rest.
            warnings.insert(0, _(
                "Only Handles is set: %(skipped)s of %(total)s product(s) of the sheet were "
                "not run. Clear Only Handles to run the whole sheet.") % {
                "skipped": len(products) - len(selected), "total": len(products)})
        # Before the HTML: an over-long cell adds a warning there.
        result_file = self._result_xlsx(rows, products, results, errors, warnings)
        values = {}
        if left:
            # The only way on: a plain re-run starts again at the first handle and stops
            # at the same place (a dry-run would never reach the rest).
            values["handles"] = ", ".join(left)
        self.write(dict(values, **{
            "result_html": self._result_html(products, selected, results, errors, warnings,
                                             left),
            "result_file": base64.b64encode(result_file),
            "result_file_name": "salla_upload_%s_%s.xlsx" % (
                self.mode, fields.Datetime.now().strftime("%Y%m%d_%H%M")),
        }))
        return {
            "type": "ir.actions.act_window",
            "name": _("Upload Products to Salla"),
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def _check_run_access(self):
        # The menu and button are hidden for others, but any method is callable over RPC.
        user = self.env.user
        if not (user.has_group("sales_team.group_sale_manager")
                or user.has_group("base.group_system")):
            raise AccessError(_("Only a sales manager can run the Salla product upload."))
        if self.mode == "apply" and not user.has_group("base.group_system"):
            raise AccessError(_("Only an administrator can apply a product upload to "
                                "Salla; run a Dry Run instead."))

    # ------------------------------------------------------------------ validation
    def _select(self, products, warnings):
        wanted = [h.strip() for h in (self.handles or "").split(",") if h.strip()]
        if not wanted:
            return list(products)
        known = {p["handle"] for p in products}
        warnings += ["%s: handle not in the sheet" % h for h in wanted if h not in known]
        return [p for p in products if p["handle"] in wanted]

    def _open_zip(self, warnings):
        """(ZipFile, {lower-case basename: ZipInfo}). Folders, macOS metadata and hidden
        files are skipped; on a duplicate basename the first entry wins."""
        if not self.images_zip:
            return None, {}
        try:
            archive = zipfile.ZipFile(io.BytesIO(base64.b64decode(self.images_zip)))
        except (zipfile.BadZipFile, ValueError):
            raise UserError(_("The images file is not a valid ZIP."))
        entries = {}
        for info in archive.infolist():
            path = info.filename.replace("\\", "/")
            key = _basename(path)
            if info.is_dir() or not key or key.startswith(".") or path.startswith("__MACOSX/"):
                continue
            if info.file_size > ZIP_ENTRY_MAX_BYTES:
                warnings.append("ZIP: %s is over %s MB, ignored" % (
                    path, ZIP_ENTRY_MAX_BYTES // (1024 * 1024)))
            elif key in entries:
                warnings.append("ZIP: %s appears twice, %s is used" % (
                    key, entries[key].filename))
            else:
                entries[key] = info
        return archive, entries

    def _distribute(self, products, warnings):
        """{handle: [message]} for the parse warnings that belong to one product ("Row N"
        of one of its rows, or "<handle>: "); they are removed from `warnings`, which
        keeps the file-level ones only."""
        by_row, out = {}, {p["handle"]: [] for p in products}
        for product in products:
            by_row[product["row"]] = product["handle"]
            for variant in product["variants"]:
                by_row[variant["row"]] = product["handle"]
        rest = []
        for message in warnings:
            handle = None
            if message.startswith("Row "):
                number = message[4:].split(":", 1)[0]
                handle = by_row.get(int(number)) if number.isdigit() else None
            else:
                prefix = message.split(": ", 1)[0]
                handle = prefix if prefix in out else None
            if handle:
                out[handle].append(message)
            else:
                rest.append(message)
        warnings[:] = rest
        return out

    def _check_odoo_skus(self, products, per_product, warnings):
        backend = self.backend_id
        skus = [v["sku"] for p in products for v in p["variants"] if v["sku"]]
        # Same match as the order import: active_test=False, the backend's company.
        try:
            found = self.env["sale.order"].with_company(backend.company_id) \
                ._salla_match_products(backend, skus)
        except AccessError:
            # A sales manager without the backend's company (e.g. Mexico only): the check
            # is advisory, it must not block a dry-run.
            warnings.append(_("SKUs not checked against Odoo: your user has no access to "
                              "the company %s") % backend.company_id.sudo().name)
            return
        for product in products:
            for variant in product["variants"]:
                match = found.get(variant["sku"]) if variant["sku"] else None
                if variant["sku"] and not match:
                    per_product[product["handle"]].append(
                        "Row %s: SKU %s not in Odoo (%s)" % (
                            variant["row"], variant["sku"], backend.company_id.name))
                elif match and not match.active:
                    per_product[product["handle"]].append(
                        "Row %s: SKU %s is archived in Odoo" % (variant["row"], variant["sku"]))

    def _check_images(self, products, archive, entries, per_product, warnings):
        if not any(p["images"] for p in products):
            return
        if not archive:
            warnings.append("No images ZIP: images are not uploaded")
            return
        # Per ZIP entry: an image named by several products is read once (a JPEG without
        # its size in the first 64 KB is decompressed whole).
        sizes = {}
        for product in products:
            messages = per_product[product["handle"]]
            for name in product["images"]:
                info = entries.get(_basename(name))
                if not info:
                    messages.append("%s: %s not in the ZIP" % (product["handle"], name))
                    continue
                if info.file_size > IMAGE_MAX_BYTES:
                    messages.append("%s: %s is %s KB (over %s KB)" % (
                        product["handle"], name, info.file_size // 1024,
                        IMAGE_MAX_BYTES // 1024))
                if info.filename not in sizes:
                    try:
                        sizes[info.filename] = (True, self._image_size(archive, info))
                    except Exception:
                        sizes[info.filename] = (False, None)
                readable, size = sizes[info.filename]
                if not readable:
                    messages.append("%s: %s is not a readable image" % (product["handle"], name))
                    continue
                if not size:
                    messages.append("%s: %s size not checked (format not readable on this "
                                    "server)" % (product["handle"], name))
                    continue
                width, height = size
                if width != height:
                    messages.append("%s: %s is not square (%sx%s)" % (
                        product["handle"], name, width, height))
                elif width != IMAGE_SIDE:
                    messages.append("%s: %s is %s px, not %s px" % (
                        product["handle"], name, width, IMAGE_SIDE))

    @staticmethod
    def _image_size(archive, info):
        """(width, height) of a ZIP entry, None when unknown. The header parser goes
        first: Pillow may not open a WebP at all (no libwebp on the server; see
        _pillow_webp, which also registers the plugin when it can). Pillow only reads
        what the parser does not know (GIF...); an exception means the file is not an
        image."""
        with archive.open(info) as stream:
            head = stream.read(IMAGE_HEADER_BYTES)
        size = mapping.image_size(head)
        if not size and head[:2] == b"\xff\xd8" and len(head) == IMAGE_HEADER_BYTES:
            # A JPEG whose SOF sits after big EXIF / ICC segments.
            size = mapping.image_size(_read_entry(archive, info))
        if size:
            return size
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP" and not _pillow_webp():
            # A WebP the parser cannot size: not proof it is broken.
            return None
        try:
            from PIL import Image
        except ImportError:
            return None
        # Straight from the ZIP stream: Pillow reads only the header for .size.
        with archive.open(info) as stream, Image.open(stream) as img:
            return img.size

    def _check_claims(self, products, per_product):
        phrases = [p.strip() for p in (self.blocked_phrases or "").splitlines() if p.strip()]
        for product in products:
            found = mapping.flagged_claims(product, phrases)
            if not found:
                continue
            message = "%s: blocked claims in the copy: %s" % (
                product["handle"], ", ".join(found))
            if self.mode == "apply":
                product["errors"].append(message)
            else:
                per_product[product["handle"]].append(message + " (blocks Apply)")

    # ------------------------------------------------------------------ run
    def _run_products(self, products, per_product, archive, entries, started):
        """-> ({handle: result}, [handles left by the time budget])."""
        # en_supported: False once Salla lost or misplaced an EN write: no more EN
        # writes in this run. item_skus: {Salla id: SKUs} of the keyword-search items,
        # read once per run. hide_refs: "must stay hidden" markers (salla.product.hide)
        # left by earlier runs, plus the ones this run commits. hide_dates: when each
        # earlier one was last marked (a stale one is only reported). hide_marked: the
        # ones this run committed (re-marking an old one restarts its date).
        # option_en: False once an English option / value write failed its check (no
        # more of them in this run); option_verified: the kinds ("option", "value")
        # whose first write of the run was read back fine. deadline: option / value
        # English writes stop there (a product with 2 options and 8 values is 10 PUTs).
        marked = self.env["salla.product.hide"]._marked(self.backend_id)
        budget = self._time_budget()
        state = {"en_supported": True, "claimed": {},
                 "archive": archive, "entries": entries, "item_skus": {},
                 "hide_refs": set(marked), "hide_dates": marked, "hide_marked": set(),
                 "option_en": True, "option_verified": set(),
                 "deadline": started + budget}
        results = {}
        for index, product in enumerate(products):
            if time.monotonic() - started > budget:
                left = [rest["handle"] for rest in products[index:]]
                for rest in products[index:]:
                    results[rest["handle"]] = self._new_result(
                        rest, "not run", "warning",
                        ["time budget reached: listed in Only Handles, click Run again"])
                return results, left
            res = self._new_result(product, "", "ok", per_product.get(product["handle"]))
            results[product["handle"]] = res
            self._run_product(product, res, state)
        return results, []

    @staticmethod
    def _time_budget():
        limit = tools.config.get("limit_time_real") or 0
        if limit <= 0:
            return TIME_BUDGET
        return max(TIME_BUDGET_MIN, min(TIME_BUDGET, limit / 2))

    def _new_result(self, product, action, status, messages=None):
        # wrote: a write call to the product was STARTED (its outcome may be unknown).
        # leftover: a marker from an earlier run that did not finish on it.
        # image_ids: {sort: Salla image id} of this run's attaches (photo per size).
        res = {"handle": product["handle"], "salla_id": product["salla_product_id"],
               "action": action, "status": status, "messages": list(messages or []),
               "variant_ids": {}, "variants_set": 0, "images": 0, "image_ids": {},
               "photos_linked": 0, "photo_puts": False, "wrote": False,
               "leftover": False}
        if res["messages"] and status == "ok":
            res["status"] = "warning"
        return res

    @staticmethod
    def _flag(res, status, message):
        res["messages"].append(message)
        if STATUS_RANK[status] > STATUS_RANK.get(res["status"], 0):
            res["status"] = status

    def _run_product(self, product, res, state):
        if product["errors"]:
            res["action"] = "skipped"
            res["status"] = "error"
            res["messages"] = product["errors"] + res["messages"]
            return
        try:
            detail = self._lookup(product, state)
            if detail:
                pid = mapping.detail_id(detail)
                other = state["claimed"].setdefault(pid, product["handle"])
                if other != product["handle"]:
                    raise UserError(_("Salla product #%s is already the product of handle "
                                      "%s in this sheet") % (pid, other))
            if self.mode == "apply":
                with self.env.cr.savepoint():
                    self._apply_product(product, detail, res, state)
            else:
                self._dry_run_product(product, detail, res, state)
        except Exception as e:
            if not isinstance(e, UserError):
                _logger.exception("Salla upload: %s failed", product["handle"])
            res["action"] = res["action"] or "failed"
            self._flag(res, "error", _error_text(e))
        _logger.info("Salla upload %s %s: Salla #%s, %s, %s; %s variant(s) set, %s image(s), "
                     "%s size photo(s) linked", self.mode, product["handle"],
                     res["salla_id"] or "-", res["action"] or "-", res["status"],
                     res["variants_set"], res["images"], res["photos_linked"])

    # --- Salla reads ---
    def _get_product(self, product_id, lang=None, params=None):
        """GET /products/{id} -> detail dict, None on a 404."""
        try:
            response = self.backend_id._request(
                "GET", "/products/%s" % quote(str(product_id), safe=""), lang=lang,
                params=params)
        except SallaApiError as e:
            if e.http_status == 404:
                return None
            raise
        detail = _data(response)
        self.backend_id._log_payload("product", product_id, detail)
        return detail or None

    def _id_by_sku(self, sku):
        """Salla product id of GET /products/sku/{sku}, "" on a 404 or for a variant
        answer without its parent id (mapping.sku_answer_product_id)."""
        try:
            response = self.backend_id._request("GET", "/products/sku/%s" % quote(sku, safe=""))
        except SallaApiError as e:
            if e.http_status == 404:
                return ""
            raise
        return mapping.sku_answer_product_id(_data(response))

    def _ids_by_keyword(self, sku, state):
        """Ids of the products a keyword search returns whose SKUs (own or variants')
        really contain `sku`: the search is fuzzy, and Wave 1 SKUs share a prefix, so
        the real product can sit past the first page. Every page is read (up to
        KEYWORD_MAX_PAGES); no match on an incomplete listing -> UserError, never "not
        found" (that would create a second copy)."""
        items, complete = self.backend_id._list_pages("/products", {"keyword": sku},
                                                      KEYWORD_MAX_PAGES)
        try:
            return mapping.keyword_match_ids(
                items, sku, complete, lambda item: self._item_skus(item, state))
        except ValueError as e:
            raise UserError(str(e))

    def _item_skus(self, item, state):
        """SKUs of a listed Salla product and its variants, cached per Salla id for the
        run: shared-prefix SKUs return the same items for every search, and each may cost
        a GET. Unlike backend._product_skus, a failed variant read raises: a lookup that
        missed the variants would answer "not in Salla" and create a second copy."""
        pid = mapping.detail_id(item)
        cache = state["item_skus"]
        if pid and pid in cache:
            return cache[pid]
        skus = mapping.product_skus(item)
        if pid and mapping.needs_variant_fetch(item):
            skus += [s for s in mapping.product_skus(self._get_product(pid))
                     if s not in skus]
        if pid:
            cache[pid] = skus
        return skus

    def _lookup(self, product, state):
        """The Salla product of a sheet product (detail dict), None = not in Salla.
        Order: salla_product_id, parent SKU, each variant SKU, keyword search. Two
        different Salla products for one handle -> UserError (never guess).
        Read in Arabic: variant matching compares its option names with the sheet's
        Arabic ones."""
        if product["salla_product_id"]:
            detail = self._get_product(product["salla_product_id"], lang="ar")
            if not detail:
                raise UserError(_("salla_product_id %s not found in Salla: clear it to "
                                  "create the product again") % product["salla_product_id"])
            self._check_same_product(product, detail)
            return detail
        skus = [product["parent_sku"]] + [v["sku"] for v in product["variants"]
                                          if v["sku"] and v["sku"] != product["parent_sku"]]
        ids = []
        for sku in skus:
            pid = self._id_by_sku(sku)
            if pid and pid not in ids:
                ids.append(pid)
        if not ids:
            for sku in skus:
                ids += [pid for pid in self._ids_by_keyword(sku, state) if pid not in ids]
                if ids:
                    # One search per SKU is costly; the product it found is checked
                    # against the whole handle (structure_problem) before any write.
                    break
        if len(ids) > 1:
            raise UserError(_("the SKUs of this handle belong to different Salla products "
                              "(%s): fix it in Salla or set salla_product_id") % ", ".join(
                "#%s" % i for i in ids))
        if not ids:
            return None
        detail = self._get_product(ids[0], lang="ar")
        if not detail:
            raise UserError(_("Salla product #%s found by SKU but not readable") % ids[0])
        return detail

    def _check_same_product(self, product, detail):
        """A typed or pasted salla_product_id must point at this handle's product, or
        Apply would PUT its copy and price onto another one. A Salla product without any
        SKU (made by hand) cannot be checked and is accepted."""
        salla_skus = set(mapping.product_skus(detail))
        sheet_skus = {product["parent_sku"]} | {v["sku"] for v in product["variants"]
                                                if v["sku"]}
        if salla_skus and not salla_skus & sheet_skus:
            raise UserError(_("salla_product_id %s is a Salla product with other SKUs (%s): "
                              "fix or clear salla_product_id") % (
                product["salla_product_id"], ", ".join(sorted(salla_skus)[:5])))

    # --- dry run: reads only, never a write call ---
    def _dry_run_product(self, product, detail, res, state):
        if detail:
            pid = res["salla_id"] = mapping.detail_id(detail)
            res["action"] = "would attach images" if self.step == "images" \
                else "would update #%s" % pid
            problem = mapping.structure_problem(product, detail)
            if problem:
                # Apply refuses it before any write: say so now.
                res["action"] = "would be refused"
                self._flag(res, "error", self._structure_message(pid, problem))
                return
            status, leftover, stale = self._status_before(product, detail, state)
            if stale and status != "hidden":
                self._flag(res, "warning", self._stale_marker_message(status, dry_run=True))
            if leftover:
                self._flag(res, "warning", self._leftover_message(status, dry_run=True))
            elif status == "hidden":
                res["messages"].append(_(
                    "hidden in Salla: kept hidden (Salla publishes a product by itself "
                    "when a variant gets stock; Apply puts it back to hidden)"))
            elif self.step != "images" and status != "sale" \
                    and mapping.writes_stock(product, detail):
                # Apply never writes this product's status: say it before, not after.
                self._flag(res, "warning", _(
                    "Salla status %s: writing the stock makes Salla publish it (sale) by "
                    "itself; hide it in Salla first if it must stay hidden") % (
                    status or _("not read")))
            if self.step != "images":
                self._plan_variants(product, detail, res)
                self._plan_simple(product, detail, res)
            existing = len(mapping.detail_images(detail))
        else:
            if self.step == "images":
                res["action"] = "not in Salla"
                self._flag(res, "error", _("not in Salla yet: run step Everything first"))
                return
            if not mapping.create_weight(product):
                res["action"] = "would not create"
                self._flag(res, "error", self._no_weight_message())
                return
            res["action"] = "would create"
            res["messages"].append(_("would create it hidden with %s variant(s)") % len(
                product["variants"]))
            unlisted = mapping.unlisted_combinations(product)
            if unlisted:
                self._flag(res, "warning", _(
                    "Salla would also create %s option combination(s) that have no row "
                    "(lowest price, default stock): add their rows, or disable them or set "
                    "them out of stock in Salla before publishing") % unlisted)
            existing = 0
        if self.step != "images":
            self._plan_option_english(product, detail, res)
            skip = mapping.translation_skip_reason(product)
            if skip:
                self._flag(res, "warning", self._english_skipped_message(skip))
            elif mapping.translation_dropped(product):
                self._flag(res, "warning", self._english_dropped_message(product))
        todo, stop = mapping.images_to_attach(product["images"], existing, state["entries"])
        if todo:
            res["messages"].append(_("would attach image(s) %s") % ", ".join(
                "%s=%s" % (sort, name) for sort, name in todo))
        if stop and state["archive"]:
            self._flag(res, "warning", _("images stop: %s") % stop)
        # Without an archive nothing is attached: only images already in Salla link.
        self._plan_option_images(product, detail, res,
                                 [name for sort, name in todo] if state["archive"] else [])

    def _plan_option_images(self, product, detail, res, pending):
        """Dry run: the size photos (variant_image) Apply would link. `pending` = the
        files Apply would attach first (their Salla id is known only then)."""
        links = product["option_images"]
        if not links or not product["options"]:
            return
        if not detail:
            attach = {_basename(name) for name in pending}
            missing = [name for ar, name in links if _basename(name) not in attach]
            if missing:
                self._flag(res, "warning", self._photos_skipped_message(
                    [_("%s would not be attached") % name for name in missing],
                    dry_run=True))
                return
            res["messages"].append(_("would show %s as image swatches") % product[
                "options"][0]["ar"])
            res["messages"].append(_("would link size photos: %s") % self._links_text(
                (ar, name) for ar, name in links))
            return
        ids, reason = mapping.gallery_image_ids(product["images"],
                                                mapping.detail_images(detail), {})
        option_write, writes, linked, problems = mapping.option_image_plan(
            product, detail, ids, pending)
        if problems:
            self._flag(res, "warning", self._photos_skipped_message(
                problems + ([reason] if reason else []), dry_run=True))
            return
        if option_write:
            res["messages"].append(_("would show %s as image swatches") % option_write[1][
                "name"])
        if writes:
            res["messages"].append(_("would link size photos: %s") % self._links_text(
                (ar, name) for value_id, ar, name, body in writes))
        if linked:
            res["messages"].append(_("size photos already linked: %s") % self._links_text(
                linked))

    def _plan_variants(self, product, detail, res):
        if not product["options"]:
            return
        pairs, missing, extra = mapping.match_variants(product, detail)
        for variant, current in pairs:
            res["variant_ids"][variant["sku"]] = mapping.clean_id(current.get("id"))
        changes = sum(1 for variant, current in pairs if mapping.variant_payload(variant, current))
        res["messages"].append(_("would set %s of %s variant(s)") % (changes, len(pairs)))
        self._report_variant_gaps(res, missing, extra)

    def _plan_option_english(self, product, detail, res):
        """Dry run: the English option / value names Apply would send. An existing
        product is read again with ?with=translations (a read) so names Salla already
        has in English are not counted."""
        if not product["options"]:
            return
        if not detail:
            names, skipped = mapping.option_english_names(product)
            todo, unchanged = len(names), 0
        else:
            try:
                current = self._get_product(mapping.detail_id(detail), lang="ar",
                                            params={"with": "translations"}) or detail
            except Exception as e:
                if not isinstance(e, UserError):
                    _logger.exception("Salla upload: translations read of %s failed",
                                      mapping.detail_id(detail))
                current = detail          # current English unknown: counted as to do
            writes, skipped, unchanged = mapping.option_translation_payloads(
                product, current)
            todo = len(writes)
        if todo or not skipped:
            res["messages"].append(_("would set English option/value names: %s") % todo
                                   + (_(" (%s already set)") % unchanged if unchanged
                                      else ""))
        for reason in skipped:
            self._flag(res, "warning", self._option_names_by_hand_message(reason))

    def _plan_simple(self, product, detail, res):
        # Apply PUTs these on the product itself; Ops keeps the stock by hand (F4), so an
        # overwrite has to be visible before Apply.
        changes = mapping.simple_changes(product, detail)
        if changes:
            res["messages"].append(_("would set %s") % ", ".join(
                "%s %s (Salla: %s)" % (label, new, current if current != "" else "?")
                for label, new, current in changes))

    @staticmethod
    def _no_weight_message():
        return _("Salla requires a weight to create a product: fill weight (and "
                 "weight_unit) in the sheet")

    @staticmethod
    def _structure_message(pid, problem):
        return _("Salla product #%s has a different structure (options/variants) than "
                 "this handle: %s. Nothing written: fix it in Salla or set "
                 "salla_product_id") % (pid, problem)

    def _report_variant_gaps(self, res, missing, extra):
        if missing:
            self._flag(res, "error", _(
                "rows %s have no matching Salla variant: add the option value in Salla by "
                "hand; the uploader never edits options") % ", ".join(map(str, missing)))
        if extra:
            self._flag(res, "warning", _(
                "Salla variants not in the sheet: %s. Before publishing, disable them or "
                "set them out of stock in Salla (Salla creates every option combination, "
                "with the lowest price and default stock)") % ", ".join(
                "#%s" % i for i in extra))

    # --- apply ---
    def _apply_product(self, product, detail, res, state):
        backend = self.backend_id
        created = False
        # Read before any write: Salla publishes a product by itself once a variant has
        # stock (spike 2026-11-10: a hidden product became "sale" after a variant PUT
        # with stock). Only a product hidden before the run is put back, and one an
        # earlier run left marked (salla.product.hide) less than 24 h ago counts as
        # hidden before. An older marker is only reported: Ops may have published it.
        before = found = ""
        stale = False
        if detail:
            found, res["leftover"], stale = self._status_before(product, detail, state)
            before = "hidden" if res["leftover"] else found
        if not detail:
            if self.step == "images":
                res["action"] = "not in Salla"
                raise UserError(_("not in Salla yet: run step Everything first"))
            detail = self._create_product(product, res, state)
            created = True
        else:
            # Before ANY write: a SKU lookup can land on an old listing of another shape,
            # and its copy, prices and images must not be overwritten.
            problem = mapping.structure_problem(product, detail)
            if problem:
                res["action"] = "refused"
                raise UserError(self._structure_message(mapping.detail_id(detail), problem))
            res["action"] = "images" if self.step == "images" else ""
            if res["leftover"]:
                # After the structure check: a refused product is not touched (its
                # marker stays for the next run).
                self._flag(res, "warning", self._leftover_message(found))
            elif stale and found != "hidden":
                self._flag(res, "warning", self._stale_marker_message(found))
        pid = res["salla_id"] = mapping.detail_id(detail)
        state["claimed"].setdefault(pid, product["handle"])
        try:
            if created or before == "hidden":
                # Committed before the first write (right after the create for a new
                # one): a worker killed from here on (limit_time_real, the time budget
                # only bounds when a product STARTS) leaves the marker, and the next run
                # puts the product back to hidden instead of reading it as "not hidden".
                self._hold_hidden(product, pid, state)
            if created or res["leftover"]:
                # At once, before variants, English and images. Salla ignores `status:
                # hidden` in the create body (spike: the create answered "out"); a
                # leftover may have been published by the run that did not finish.
                self._ensure_hidden(pid, res, verify_first=res["leftover"])
            if self.step == "all" and not created:
                # Inside the try: the PUT of a product without options carries its stock,
                # and an unknown outcome may have flipped the status too.
                # Set first so a failing PUT is reported as such, not as "updated".
                res["action"] = "update failed"
                res["wrote"] = True
                # Arabic stated, not left to Salla's default language (unverified).
                backend._request("PUT", "/products/%s" % pid, lang="ar",
                                 payload=mapping.product_payload(product, create=False))
                res["action"] = "updated"
            if self.step == "all":
                if product["options"] and not (mapping.detail_skus(detail)
                                               and detail.get("options")):
                    detail = self._get_product(pid, lang="ar") or detail
                if not self._apply_variants(product, detail, res):
                    # Only reachable for a product created here (existing ones were
                    # checked above): English and images wait for the fix in Salla.
                    return
                # Right after the stock writes, not only at the end: the product is not
                # left published while English and images are written.
                self._keep_hidden(product, pid, before, created, res, state)
                self._apply_english(product, detail, res, state)
            self._apply_images(product, detail, res, state)
            # After the gallery: the ids of this run's images come from the attaches.
            # So after the English step too: an English name a link PUT drops is put
            # back by _recheck_option_english.
            self._apply_option_images(product, detail, res, state)
        finally:
            # Again at the end, even when a later step failed: images may flip it too.
            self._keep_hidden(product, pid, before, created, res, state, final=True)

    def _status_before(self, product, detail, state):
        """(Salla status of `detail`, leftover, stale): leftover = an earlier run left a
        "must stay hidden" marker on it (by Salla id, or by parent SKU when it died
        around the create) less than HIDE_MARKER_MAX_AGE ago; stale = it has markers,
        all older: its status is not put back (only reported)."""
        status = mapping.detail_status(detail)
        pid, sku = mapping.detail_id(detail), product["parent_sku"]
        if not mapping.hide_marker_left(state["hide_refs"], pid, sku):
            return status, False, False
        if mapping.hide_marker_stale(state["hide_dates"], pid, sku, fields.Datetime.now()):
            return status, False, True
        return status, True, False

    @staticmethod
    def _leftover_message(status, dry_run=False):
        if dry_run:
            return _("a previous upload did not finish on this product (Salla status now: "
                     "%s): Apply puts it back to hidden. If it was published on purpose, "
                     "delete its row in Sales > Configuration > Salla > Products Kept "
                     "Hidden before Apply") % (status or "?")
        return _("a previous upload did not finish on this product (Salla status at the "
                 "start of this run: %s): kept hidden. If it was published on purpose, "
                 "delete its row in Sales > Configuration > Salla > Products Kept Hidden "
                 "and publish it again") % (status or "?")

    @staticmethod
    def _stale_marker_message(status, dry_run=False):
        verb = _("Apply does not touch it") if dry_run else _("not touched")
        return _("a previous upload left this product marked \"must stay hidden\" more "
                 "than 24 h ago; Salla status %(status)s: %(verb)s (it may have been "
                 "published on purpose). If it must be hidden, hide it in Salla; either "
                 "way delete its row in Sales > Configuration > Salla > Products Kept "
                 "Hidden") % {"status": status or "?", "verb": verb}

    def _hold_hidden(self, product, pid, state):
        """Commit the "must stay hidden" marker of `pid` before writing to it. Raises
        UserError when it cannot be committed: an unprotected write is not made."""
        ref = mapping.hide_marker_ref(product_id=pid)
        if not ref or ref in state["hide_marked"]:
            # Not "in hide_refs": an earlier run's marker is marked again so its date
            # restarts (a stale one would not protect this run's writes).
            return
        try:
            self.env["salla.product.hide"]._mark(self.backend_id, product["handle"],
                                                 product_id=pid)
        except Exception as e:
            _logger.exception("Salla upload: hidden marker of product %s not saved", pid)
            raise UserError(_("could not record that #%s must stay hidden (%s): nothing "
                              "more written to it; re-run") % (pid, _error_text(e)))
        state["hide_refs"].add(ref)
        state["hide_marked"].add(ref)

    def _release_hidden(self, product, pid, state):
        """Drop the markers of this product once it was seen hidden."""
        refs = {mapping.hide_marker_ref(product_id=pid),
                mapping.hide_marker_ref(sku=product["parent_sku"])} & state["hide_refs"]
        if refs and self.env["salla.product.hide"]._clear(self.backend_id, refs):
            state["hide_refs"] -= refs
            state["hide_marked"] -= refs

    def _keep_hidden(self, product, pid, before, created, res, state, final=False):
        """Status after this run's writes. Created here, hidden before the run, or a
        leftover: verified hidden (a GET; the POST only when Salla flipped it), and at
        the end, once seen hidden, its marker is cleared. Any other existing product:
        its status is never written; at the end a change Salla made by itself (e.g.
        out -> sale) is only reported. Never POSTs any status but hidden."""
        if not (created or before == "hidden"):
            if not (final and res["wrote"]):
                return                     # nothing was written to it
            if before:
                self._report_status_change(pid, before, res)
            else:
                self._flag(res, "warning", _(
                    "status before the run not read: check in Salla that it is not "
                    "published by mistake (Salla publishes a product when its stock is "
                    "written)"))
            return
        if created or res["leftover"] or res["wrote"]:
            changed_after = None
            if not (created or res["leftover"]):
                changed_after = _("image upload") if self.step == "images" \
                    else _("stock update")
                if res["photo_puts"]:
                    # The spike saw no flip from the link PUTs; not ruled out either.
                    changed_after = _("%s or size photo link") % changed_after
            hidden = self._ensure_hidden(pid, res, verify_first=True,
                                         changed_after=changed_after)
        else:
            hidden = True                  # hidden when read, nothing written since
        if hidden and final:
            self._release_hidden(product, pid, state)

    def _report_status_change(self, pid, before, res):
        try:
            now = mapping.detail_status(self._get_product(pid))
        except Exception as e:
            # Runs in a `finally`: it must never replace the error being reported.
            if not isinstance(e, UserError):
                _logger.exception("Salla upload: status read of product %s failed", pid)
            res["messages"].append(_("status after the upload not read (%s)") % _error_text(
                e))
            return
        if now and now != before:
            res["messages"].append(_(
                "Salla changed the status from %(before)s to %(now)s during the upload "
                "(not touched: the product was not hidden before)") % {
                "before": before, "now": now})

    def _create_product(self, product, res, state):
        """POST /products. An unknown outcome (timeout, 5xx) is checked with a lookup:
        found = adopted, never posted twice."""
        if not mapping.create_weight(product):
            # Refused before the POST: Salla answers 422 anyway, and an existing product
            # (update path) never gets here, so only new ones need a weight.
            res["action"] = "not created"
            raise UserError(self._no_weight_message())
        # By parent SKU, before the POST: the id is not known yet, and a create with an
        # unknown outcome (or a kill right after it) leaves a product at "out" that the
        # next run finds by SKU; the marker makes that run hide it.
        sku_ref = mapping.hide_marker_ref(sku=product["parent_sku"])
        if sku_ref not in state["hide_marked"]:
            try:
                self.env["salla.product.hide"]._mark(self.backend_id, product["handle"],
                                                     sku=product["parent_sku"])
            except Exception as e:
                _logger.exception("Salla upload: hidden marker of %s not saved",
                                  product["handle"])
                res["action"] = "not created"
                raise UserError(_("not created: could not record that it must stay hidden "
                                  "(%s); re-run") % _error_text(e))
            state["hide_refs"].add(sku_ref)
            state["hide_marked"].add(sku_ref)
        try:
            response = self.backend_id._request(
                "POST", "/products", payload=mapping.product_payload(product, create=True),
                idempotent=False, lang="ar")
        except SallaApiError as e:
            if e.http_status is not None:
                if mapping.is_permanent_error(e.http_status) and self.env[
                        "salla.product.hide"]._clear(self.backend_id, [sku_ref]):
                    # Refused (422...): nothing was created, the marker has no product.
                    state["hide_refs"].discard(sku_ref)
                    state["hide_marked"].discard(sku_ref)
                raise
            detail = self._lookup_after_create(product, state, _(
                "unknown outcome of the create (%s)") % _error_text(e))
            res["action"] = "created (adopted after an unknown outcome)"
            return detail
        detail = _data(response)
        if not mapping.detail_id(detail):
            # Created, but the id is unknown: find it by SKU so it can be hidden now.
            detail = self._lookup_after_create(product, state, _(
                "Salla answered the create without a product id"))
            res["action"] = "created (id read back by SKU)"
            return detail
        res["action"] = "created"
        return detail

    def _lookup_after_create(self, product, state, what):
        """The product a create may have made, found by SKU. Not found, or a lookup that
        fails itself -> UserError that still says `what`: the product may exist in Salla,
        unverified as hidden, and the user has to check it."""
        try:
            detail = self._lookup(product, state)
        except Exception as e:
            if not isinstance(e, UserError):
                _logger.exception("Salla upload: lookup after the create of %s failed",
                                  product["handle"])
            problem = _("the lookup after it failed (%s)") % _error_text(e)
        else:
            if detail:
                return detail
            problem = _("not found by SKU yet")
        raise UserError(_(
            "%s, %s: check the Salla product list for a copy of this product, hide it by "
            "hand if it is there and set salla_product_id, then re-run") % (what, problem))

    def _apply_variants(self, product, detail, res):
        """-> False when sheet rows have no Salla variant (nothing more is written on
        that product)."""
        if not product["options"]:
            return True
        pairs, missing, extra = mapping.match_variants(product, detail)
        for variant, current in pairs:
            vid = res["variant_ids"][variant["sku"]] = mapping.clean_id(current.get("id"))
            if not vid:
                self._flag(res, "error", _("Row %s: the Salla variant has no readable id, "
                                           "not updated") % variant["row"])
                continue
            body = mapping.variant_payload(variant, current)
            if not body:
                continue
            res["wrote"] = True
            try:
                self.backend_id._request("PUT", "/products/variants/%s" % vid, payload=body)
            except UserError as e:
                # Also the plain UserError of exhausted retries: one failed call costs
                # this variant, not the rest of the product.
                self._flag(res, "error", _("Row %s: variant #%s not updated: %s") % (
                    variant["row"], vid, _error_text(e)))
                continue
            res["variants_set"] += 1
        self._report_variant_gaps(res, missing, extra)
        return not missing

    def _apply_english(self, product, detail, res, state):
        """EN product texts, then the EN option / value names."""
        self._apply_text_english(product, detail, res, state)
        self._apply_option_english(product, detail, res, state)

    def _apply_text_english(self, product, detail, res, state):
        """EN product texts: PUT /products/{id} with lang "en".
        After the EN PUT the product is read back twice. In Arabic: if the EN write
        replaced the Arabic, it is put back. In English: the demo (English disabled)
        answered the EN PUT with a 200, kept the Arabic, and still showed the Arabic
        name with lang en, so a 200 proves nothing. Either failure stops EN writes for the rest of the run. The
        read-backs compare names only, so EN texts are sent only with a name_en that
        differs from name_ar (mapping.translation_skip_reason), and only the ones whose
        Arabic side is set (mapping.translation_payload): the Arabic restore / re-write
        must cover every key the EN PUT carried."""
        backend, pid = self.backend_id, mapping.detail_id(detail)
        body = mapping.translation_payload(product)
        skip = mapping.translation_skip_reason(product)
        if skip:
            self._flag(res, "warning", self._english_skipped_message(skip))
            return
        if mapping.translation_dropped(product):
            self._flag(res, "warning", self._english_dropped_message(product))
        if not body:
            return
        if not state["en_supported"]:
            self._flag(res, "warning", _("English must be entered by hand in Salla"))
            return
        # "English not written" only when the EN PUT itself failed; once one went
        # through, any later failure is about the Arabic and is reported as such.
        res["wrote"] = True
        try:
            backend._request("PUT", "/products/%s" % pid, payload=body, lang="en")
        except UserError as e:
            self._flag(res, "warning", _("English not written: %s") % _error_text(e))
            return
        if not self._arabic_kept(
                res, state, pid, _("the Arabic name/description"),
                product["name"]["ar"], lambda after: mapping.detail_name(after, "ar"),
                # Texts only: the price, stock and weight of a product without
                # options are not re-sent.
                lambda: backend._request(
                    "PUT", "/products/%s" % pid, lang="ar",
                    payload=mapping.translation_payload(product, "ar"))):
            return
        try:
            shown = self._get_product(pid, lang="en")
        except Exception as e:
            if not isinstance(e, UserError):
                _logger.exception("Salla upload: English read-back of %s failed", pid)
            # Not proof that English is lost: the next products keep trying.
            self._flag(res, "warning", _("English written but not verified (%s): check "
                                         "it in Salla") % _error_text(e))
            return
        if mapping.english_kept(product["name"]["en"], shown):
            return
        state["en_supported"] = False
        self._flag(res, "warning", _(
            "Salla did not keep the English (check that English is enabled in the "
            "store's languages); enter it by hand (no more English writes in this run)"))
        # The Arabic NAME was read back intact, the description was not: where Salla
        # put the EN texts is unknown, so the Arabic texts are written again. Texts
        # only: the stock and price of a product without options are not re-sent.
        try:
            backend._request("PUT", "/products/%s" % pid, lang="ar",
                             payload=mapping.translation_payload(product, "ar"))
        except Exception as e:
            if not isinstance(e, UserError):
                _logger.exception("Salla upload: Arabic re-write of %s failed", pid)
            self._flag(res, "error", _(
                "the Arabic texts could not be written again after the English write "
                "(%s): check the Arabic description of #%s in Salla") % (
                _error_text(e), pid))

    def _apply_option_english(self, product, detail, res, state):
        """EN option / value names (spike run 4, 2026-11-10, English enabled): PUT
        /products/options[/values]/{id} with lang "ar" and the CURRENT Arabic name plus
        translations.en (mapping.option_translation_payloads). A lang "en" PUT wrote
        over the Arabic in earlier spikes, so the first option and the first value of
        the run are read back in Arabic (unchanged, else restored and no more option
        English in this run) and in English (shown, else no more option English).
        Once both kinds passed, later writes only check the HTTP result, like the
        product texts. Values / price / quantity are never sent."""
        if not product["options"]:
            return
        if not state["en_supported"] or not state["option_en"]:
            self._flag(res, "warning", self._option_names_by_hand_message(
                _("English option names stopped earlier in this run") if state["en_supported"]
                else _("Salla did not keep the English texts in this run (check that English "
                       "is enabled in the store's languages)")))
            return
        pid = mapping.detail_id(detail)
        try:
            # Fresh, after the variants step: current Arabic names and English.
            current = self._get_product(pid, lang="ar", params={"with": "translations"})
        except Exception as e:
            if not isinstance(e, UserError):
                _logger.exception("Salla upload: translations read of %s failed", pid)
            current = None
            error = _error_text(e)
        else:
            error = _("product not found")
        if not current:
            self._flag(res, "warning", self._option_names_by_hand_message(
                _("product not read (%s); re-run") % error))
            return
        writes, skipped, unchanged = mapping.option_translation_payloads(product, current)
        for reason in skipped:
            self._flag(res, "warning", self._option_names_by_hand_message(reason))
        done = 0
        for index, (kind, item_id, body) in enumerate(writes):
            if not state["option_en"]:
                break
            if time.monotonic() > state["deadline"]:
                self._flag(res, "warning", _(
                    "time budget reached: %s English option/value name(s) not written; "
                    "run this handle again") % (len(writes) - index))
                break
            path = self._option_path(kind, item_id)
            res["wrote"] = True
            try:
                self.backend_id._request("PUT", path, lang="ar", payload=body)
            except UserError as e:
                self._flag(res, "warning", _("English %(kind)s name %(name)s not written: "
                                             "%(error)s") % {
                    "kind": kind, "name": body["name"], "error": _error_text(e)})
                continue
            if kind not in state["option_verified"] \
                    and not self._option_english_kept(pid, kind, item_id, body, res, state):
                continue
            done += 1
        if done:
            res["messages"].append(_("English option/value names set: %s") % done)
        if unchanged:
            res["messages"].append(_("English option/value names already set: %s") % unchanged)

    @staticmethod
    def _option_path(kind, item_id):
        if kind == "option":
            return "/products/options/%s" % quote(item_id, safe="")
        return "/products/options/values/%s" % quote(item_id, safe="")

    def _option_english_kept(self, pid, kind, item_id, body, res, state):
        """First write of `kind` in the run: read back in Arabic and in English. True
        = both fine (kind verified for the run). On a failure option English stops
        for the run; an Arabic that moved is put back (lang ar, name only)."""
        ar = body["name"]
        en = body["translations"]["en"][mapping.OPTION_EN_KEYS[kind]]
        try:
            after = mapping.detail_option_name(self._get_product(pid, lang="ar"), kind,
                                               item_id)
        except Exception as e:
            state["option_en"] = False
            if not isinstance(e, UserError):
                _logger.exception("Salla upload: Arabic option read-back of %s failed", pid)
            self._flag(res, "error", _(
                "English %(kind)s name written but its Arabic not verified (%(error)s): "
                "check %(kind)s %(name)s of #%(pid)s in Salla (no more English option "
                "names in this run)") % {"kind": kind, "error": _error_text(e),
                                          "name": ar, "pid": pid})
            return False
        if after is None:
            # The item is not in the answer (404, unexpected shape): no proof the Arabic
            # moved, so no restore PUT; just stop and ask for a look.
            state["option_en"] = False
            self._flag(res, "error", _(
                "English %(kind)s name written but its Arabic not verified (not in Salla's "
                "answer): check %(kind)s %(name)s of #%(pid)s in Salla (no more English "
                "option names in this run)") % {"kind": kind, "name": ar, "pid": pid})
            return False
        if not mapping.translation_kept(ar, after):
            state["option_en"] = False
            try:
                self.backend_id._request("PUT", self._option_path(kind, item_id), lang="ar",
                                         payload={"name": ar})
            except Exception as e:
                if not isinstance(e, UserError):
                    _logger.exception("Salla upload: Arabic option restore of %s failed", pid)
                self._flag(res, "error", _(
                    "Salla wrote the English over the Arabic %(kind)s name and restoring "
                    "it failed (%(error)s): set it back to %(name)s on #%(pid)s in Salla "
                    "by hand (no more English option names in this run)") % {
                    "kind": kind, "error": _error_text(e), "name": ar, "pid": pid})
                return False
            self._flag(res, "warning", _(
                "Salla wrote the English over the Arabic %(kind)s name: Arabic restored; "
                "enter the English option names by hand (no more English option names in "
                "this run)") % {"kind": kind})
            return False
        try:
            shown = mapping.detail_option_name(self._get_product(pid, lang="en"), kind,
                                               item_id, lang="en")
        except Exception as e:
            if not isinstance(e, UserError):
                _logger.exception("Salla upload: English option read-back of %s failed", pid)
            # Not proof that English is lost: the next write of this kind is checked.
            self._flag(res, "warning", _("English %(kind)s name written but not verified "
                                         "(%(error)s): check it in Salla") % {
                "kind": kind, "error": _error_text(e)})
            return False
        if mapping.norm_label(shown) != mapping.norm_label(en):
            state["option_en"] = False
            self._flag(res, "warning", _(
                "Salla did not keep the English option names; enter them by hand (no "
                "more English option names in this run)"))
            return False
        state["option_verified"].add(kind)
        return True

    @staticmethod
    def _option_names_by_hand_message(reason):
        return _("English option/value names must be entered by hand in Salla: %s") % reason

    @staticmethod
    def _english_dropped_message(product):
        return _("English texts not sent for %s: their Arabic column is empty, and the "
                 "Arabic could not be put back if Salla wrote them over it; fill the _ar "
                 "column or enter them by hand in Salla") % ", ".join(
            mapping.translation_dropped(product))

    @staticmethod
    def _english_skipped_message(reason):
        return _("English texts not sent (%s): English needs a name_en different from "
                 "name_ar, or Salla could write it over the Arabic unseen; enter it by "
                 "hand in Salla or fix name_en") % reason

    def _arabic_kept(self, res, state, pid, what, before, read_after, restore):
        """After a successful EN write: True when the Arabic read back is still
        `before`. Otherwise EN writes stop for the rest of the run FIRST (so a failing
        restore or read can never let the next products, published ones included, get
        the same write), then the Arabic is put back with `restore()`."""
        try:
            after = read_after(self._get_product(pid, lang="ar"))
        except Exception as e:
            state["en_supported"] = False
            if not isinstance(e, UserError):
                _logger.exception("Salla upload: Arabic read-back of %s failed", pid)
            self._flag(res, "error", _(
                "English written but Arabic not verified (%s): check %s of #%s in Salla "
                "(no more English writes in this run)") % (_error_text(e), what, pid))
            return False
        if mapping.translation_kept(before, after):
            return True
        state["en_supported"] = False
        try:
            restore()
        except Exception as e:
            if not isinstance(e, UserError):
                _logger.exception("Salla upload: Arabic restore of %s failed", pid)
            self._flag(res, "error", _(
                "Salla wrote the English over the Arabic and restoring it failed (%s): fix "
                "%s of #%s in Salla by hand (no more English writes in this run)") % (
                _error_text(e), what, pid))
            return False
        self._flag(res, "warning", _(
            "Salla wrote the English over the Arabic: Arabic restored; English must be "
            "entered by hand (no more English writes in this run)"))
        return False

    def _apply_images(self, product, detail, res, state):
        archive, entries = state["archive"], state["entries"]
        if not product["images"] or not archive:
            return
        pid = mapping.detail_id(detail)
        todo, stop = mapping.images_to_attach(
            product["images"], len(mapping.detail_images(detail)), entries)
        for sort, name in todo:
            info = entries[_basename(name)]
            filename = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
            try:
                content = _read_entry(archive, info)
                if self.convert_webp and filename.lower().endswith(".webp"):
                    content, filename = self._webp_to_jpeg(content, filename)
            except Exception as e:
                # Corrupt entry or image: the next ones would land on the wrong sort.
                self._flag(res, "error", _("image %s not readable (%s): images stopped for "
                                           "this product") % (name, _error_text(e)))
                return
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            data = {"sort": str(sort), "main": "1" if sort == 1 else "0"}
            if product["image_alt"]:
                data["alt"] = product["image_alt"]
            # Before the POST: an unknown outcome may still have stored the image (and
            # flipped the status), so the final status check must run.
            res["wrote"] = True
            try:
                response = self.backend_id._request(
                    "POST", "/products/%s/images" % pid, idempotent=False, data=data,
                    files={"photo": (filename, content,
                                     IMAGE_MIMETYPES.get(ext, "application/octet-stream"))})
            except UserError as e:
                # http_status None (or a plain UserError of exhausted retries): Salla may
                # have stored it.
                if getattr(e, "http_status", None) is None:
                    self._flag(res, "warning", _(
                        "image %s: unknown outcome, images stopped for this product; re-run "
                        "(Salla's images are counted first)") % name)
                else:
                    self._flag(res, "error", _("image %s not attached: %s") % (
                        name, _error_text(e)))
                return
            res["images"] += 1
            # Spike 2026-11-13: the answer's `id` is the gallery id a size links to. An
            # answer without one leaves that image unlinkable (never guessed by sort).
            image_id = mapping.clean_id(_data(response).get("id"))
            if image_id:
                res["image_ids"][sort] = image_id
        if stop:
            self._flag(res, "warning", _("images stop: %s") % stop)

    def _apply_option_images(self, product, detail, res, state):
        """Photo per size (variant_image; spike 2026-11-13, demo): option1 shown as
        image swatches, each of its values linked to its gallery photo. PUT
        /products/options/{id} {name, display_type: "image"}, then PUT
        /products/options/values/{id} {name, display_value: "<image id>"}, both lang
        ar with Salla's current Arabic name (mapping.option_image_plan): never
        "values", price or quantity. Idempotent: what is already set is not sent
        again. Never raises: a failure is this product's error line, and the links
        already set stay (a re-run finishes them)."""
        if not product["option_images"] or not product["options"]:
            return
        pid = mapping.detail_id(detail)
        try:
            self._link_option_images(product, detail, pid, res, state)
        except Exception as e:
            if not isinstance(e, UserError):
                _logger.exception("Salla upload: size photos of %s failed", pid)
            self._flag(res, "error", _("size photos not linked (%s): re-run this handle") %
                       _error_text(e))

    def _link_option_images(self, product, detail, pid, res, state):
        # Plain read: the shape the spike read display_type / display_value / images
        # from. `detail` is the gallery BEFORE this run's attaches (images_to_attach
        # counted it): its images are the first ones of the sheet, by sort.
        current = self._get_product(pid, lang="ar")
        if not current:
            raise UserError(_("product #%s not found") % pid)
        ids, reason = mapping.gallery_image_ids(
            product["images"], mapping.detail_images(detail), res["image_ids"])
        option_write, writes, linked, problems = mapping.option_image_plan(
            product, current, ids)
        if problems:
            self._flag(res, "warning", self._photos_skipped_message(
                problems + ([reason] if reason else [])))
            return
        if linked:
            res["messages"].append(_("size photos already linked: %s") % self._links_text(
                linked))
        if not option_write and not writes:
            return
        # Checked once, before the first PUT and never between them (review B1): an
        # option switched to image swatches whose values then wait for the next run
        # would show empty swatches, maybe on a published product. The few value PUTs
        # fit in the slack the budget keeps (limit_time_real / 2).
        if time.monotonic() > state["deadline"]:
            self._flag(res, "warning", _("time budget reached: size photos not linked; "
                                         "run this handle again"))
            return
        # The English Salla has now, to put back what a link PUT drops (not tested on
        # the demo). Unread = not linked: the English must not be lost unseen.
        try:
            before = self._get_product(pid, lang="ar", params={"with": "translations"})
        except Exception as e:
            if not isinstance(e, UserError):
                _logger.exception("Salla upload: translations read of %s failed", pid)
            before, error = None, _error_text(e)
        else:
            error = _("product not found")
        if not before:
            self._flag(res, "warning", self._photos_skipped_message(
                [_("English names not read before the link (%s); re-run") % error]))
            return
        # written: every PUT that may have reached Salla, added BEFORE the call and
        # dropped only on a real 4xx (review S1): an unknown outcome may have dropped
        # the English too. links: (value id, image id, label) set in this run.
        written, done, links, refused, switched = [], [], [], 0, False
        option_id = option_write[0] if option_write else ""
        # The option the post-restore check expects as image swatches ("" = none).
        check_option = option_id or self._option1_id(product, current)
        res["wrote"] = res["photo_puts"] = True
        try:
            if option_write:
                body = option_write[1]
                written.append(("option", option_id))
                path = self._option_path("option", option_id)
                try:
                    self.backend_id._request("PUT", path, lang="ar", payload=body)
                except UserError as e:
                    if self._refused(e):
                        written.remove(("option", option_id))
                    # A value photo on a text option shows nothing: stop here.
                    self._flag(res, "error", _(
                        "size photos not linked: %(option)s not set to image swatches "
                        "(%(error)s); re-run this handle") % {
                        "option": body["name"], "error": _error_text(e)})
                    return
                switched = True
            for value_id, ar, name, body in writes:
                written.append(("value", value_id))
                path = self._option_path("value", value_id)
                try:
                    self.backend_id._request("PUT", path, lang="ar", payload=body)
                except UserError as e:
                    if self._refused(e):
                        written.remove(("value", value_id))
                        refused += 1
                    self._flag(res, "error", _("size photo %(name)s not linked to %(value)s: "
                                               "%(error)s; re-run this handle") % {
                        "name": name, "value": ar, "error": _error_text(e)})
                    continue
                done.append((ar, name))
                links.append((value_id, body["display_value"], ar))
            if option_write and writes and refused == len(writes) and not linked:
                # Switched in this run and every value refused for sure: put the option
                # back so the storefront does not show swatches without photos.
                check_option = self._revert_option_display(
                    option_write, mapping.option_display_type(current, option_id) or "text",
                    res)
        finally:
            res["photos_linked"] += len(done)
            if done:
                res["messages"].append(_("size photos linked: %s") % self._links_text(done))
            if switched and check_option:
                res["messages"].append(_("%s shown as image swatches") % option_write[1][
                    "name"])
            self._recheck_option_english(pid, before, written, check_option, links, res,
                                         state)

    @staticmethod
    def _option1_id(product, detail):
        """Salla id of the sheet's option1 in `detail`, "" when not matched once."""
        key = mapping.norm_label(product["options"][0]["ar"])
        hits = [o for o in (detail.get("options") or []) if isinstance(o, dict)
                and mapping.norm_label(mapping.detail_option_name(
                    detail, "option", o.get("id"))) == key]
        return mapping.clean_id(hits[0].get("id")) if len(hits) == 1 else ""

    @staticmethod
    def _refused(error):
        """Salla answered and refused (4xx): the write did not happen. Anything else
        (no answer, 5xx after the retries) may have reached Salla."""
        status = getattr(error, "http_status", None)
        return status is not None and 400 <= status < 500

    def _revert_option_display(self, option_write, display_type, res):
        """Put option1 back to `display_type` after every value photo was refused.
        -> "" (no link left to check) when done, the option id when it stays image."""
        option_id, body = option_write
        revert = {"name": body["name"], "display_type": display_type}
        path = self._option_path("option", option_id)
        try:
            self.backend_id._request("PUT", path, lang="ar", payload=revert)
        except UserError as e:
            self._flag(res, "error", _(
                "every size photo was refused and %(option)s could not be put back to "
                "%(type)s (%(error)s): set its option type back by hand in Salla, or fix "
                "and re-run") % {"option": body["name"], "type": display_type,
                                 "error": _error_text(e)})
            return option_id
        self._flag(res, "error", _(
            "every size photo was refused: %(option)s put back to %(type)s; fix and "
            "re-run") % {"option": body["name"], "type": display_type})
        return ""

    def _recheck_option_english(self, pid, before, written, option_id, links, res, state):
        """After the link PUTs, the names Salla had before (mapping.option_english_lost):
        an English lost or changed is put back through the v17.0.5.0.2 path (lang ar,
        the Arabic BEFORE + translations.en), an Arabic the link moved is put back
        ({"name": <before>}, as _option_english_kept does). Then one verify read: the
        Arabic (moved again -> put back, error, no more English option names), the
        English, and the links themselves (option_id still image swatches, `links`
        still linked): the name PUTs carry neither field and might drop them, as the
        link PUT might drop the English (review S2). English is not put back when this
        run stopped English writes: then only reported."""
        if not written:
            return
        try:
            after = self._get_product(pid, lang="ar", params={"with": "translations"})
        except Exception as e:
            if not isinstance(e, UserError):
                _logger.exception("Salla upload: translations read of %s failed", pid)
            after, error = None, _error_text(e)
        else:
            error = _("product not found")
        if not after:
            self._flag(res, "warning", _(
                "English option/value names not re-checked after the size photo link "
                "(%s): check them in Salla") % error)
            return
        restores, unknown, arabic = mapping.option_english_lost(before, after, written)
        for kind, item_id, english in unknown:
            self._flag(res, "warning", _(
                "English %(kind)s name %(en)s not re-checked after the size photo link "
                "(#%(id)s not in Salla's answer): check it in Salla") % {
                "kind": kind, "en": english, "id": item_id})
        lost = ", ".join(body["translations"]["en"][mapping.OPTION_EN_KEYS[kind]]
                         for kind, item_id, body in restores)
        if restores and not (state["en_supported"] and state["option_en"]):
            self._flag(res, "warning", _(
                "Salla dropped the English name(s) %s on the size photo link; English "
                "writes are stopped in this run: enter them by hand in Salla") % lost)
            # Their restore would also have put the Arabic back: an Arabic the link
            # moved still is, name only (review round 2).
            for kind, item_id, body in restores:
                now = mapping.detail_option_name(after, kind, item_id)
                if now is not None and not mapping.translation_kept(body["name"], now):
                    arabic.append((kind, item_id, body["name"], now))
            restores = []
        put_back = []
        for kind, item_id, old, new in arabic:
            path = self._option_path(kind, item_id)
            try:
                self.backend_id._request("PUT", path, lang="ar", payload={"name": old})
            except UserError as e:
                self._flag(res, "error", _(
                    "Salla changed the Arabic %(kind)s name %(old)s to %(new)s on the size "
                    "photo link and putting it back failed (%(error)s): set it back to "
                    "%(old)s on #%(pid)s in Salla by hand") % {
                    "kind": kind, "old": old, "new": new, "error": _error_text(e),
                    "pid": pid})
                continue
            self._flag(res, "warning", _(
                "Salla changed the Arabic %(kind)s name %(old)s to %(new)s on the size "
                "photo link: put back") % {"kind": kind, "old": old, "new": new})
            put_back.append((kind, item_id, {"name": old}))
        for kind, item_id, body in restores:
            path = self._option_path(kind, item_id)
            try:
                self.backend_id._request("PUT", path, lang="ar", payload=body)
            except UserError as e:
                self._flag(res, "warning", _(
                    "English %(kind)s name %(en)s lost on the size photo link and not put "
                    "back (%(error)s): enter it by hand in Salla") % {
                    "kind": kind, "error": _error_text(e),
                    "en": body["translations"]["en"][mapping.OPTION_EN_KEYS[kind]]})
                continue
            put_back.append((kind, item_id, body))
        if not put_back:
            return
        try:
            check = self._get_product(pid, lang="ar", params={"with": "translations"})
            # Plain read for the links: the shape the spike read them from.
            shown = self._get_product(pid, lang="ar")
        except Exception as e:
            if not isinstance(e, UserError):
                _logger.exception("Salla upload: read-back of %s failed", pid)
            check = shown = None
        if not check or not shown:
            self._flag(res, "warning", _(
                "names put back after the size photo link but not verified: check the "
                "option names and size photos of #%s in Salla") % pid)
            return
        for kind, item_id, body in put_back:
            now = mapping.detail_option_name(check, kind, item_id)
            # None = not in the answer: reported below as not kept, never "changed".
            if now is None or mapping.translation_kept(body["name"], now):
                continue
            state["option_en"] = False
            try:
                self.backend_id._request("PUT", self._option_path(kind, item_id), lang="ar",
                                         payload={"name": body["name"]})
            except UserError as e:
                self._flag(res, "error", _(
                    "the Arabic %(kind)s name changed when its English was put back and "
                    "restoring it failed (%(error)s): set it back to %(name)s on #%(pid)s "
                    "in Salla by hand (no more English option names in this run)") % {
                    "kind": kind, "error": _error_text(e), "name": body["name"],
                    "pid": pid})
                continue
            self._flag(res, "error", _(
                "the Arabic %(kind)s name changed when its English was put back: Arabic "
                "%(name)s restored; check #%(pid)s in Salla and enter the English by hand "
                "(no more English option names in this run)") % {
                "kind": kind, "name": body["name"], "pid": pid})
        english = [(kind, item_id) for kind, item_id, body in put_back if "translations" in body]
        still, gone, moved = mapping.option_english_lost(before, check, english)
        missing = [body["translations"]["en"][mapping.OPTION_EN_KEYS[kind]]
                   for kind, item_id, body in still] + [en for kind, item_id, en in gone]
        if missing:
            self._flag(res, "warning", _(
                "Salla did not keep the English name(s) %s put back after the size photo "
                "link: enter them by hand in Salla") % ", ".join(missing))
        undone = mapping.option_image_lost(shown, option_id, links)
        if undone:
            self._flag(res, "error", _(
                "Salla undid the size photo link when the names were put back (%s): link "
                "the photos by hand in Salla and report it") % "; ".join(undone))
            return
        if english and not missing:
            # A finding worth seeing (the demo never showed it), not a problem.
            res["messages"].append(_(
                "Salla dropped the English name(s) %s on the size photo link: put back")
                % lost)

    @staticmethod
    def _links_text(links):
        return ", ".join("%s -> %s" % (ar, name) for ar, name in links)

    @staticmethod
    def _photos_skipped_message(reasons, dry_run=False):
        if dry_run:
            return _("size photos would not be linked: %s") % "; ".join(reasons)
        return _("size photos not linked (nothing changed for them): %s") % "; ".join(
            reasons)

    @staticmethod
    def _webp_to_jpeg(content, filename):
        try:
            Image = _webp_image()
        except ImportError:
            raise UserError(_("Pillow is not installed: WebP images cannot be converted"))
        with Image.open(io.BytesIO(content)) as img:
            if img.mode in ("RGBA", "LA", "P"):
                # JPEG has no transparency: flatten on white, not on black.
                img = img.convert("RGBA")
                flat = Image.new("RGB", img.size, (255, 255, 255))
                flat.paste(img, mask=img.getchannel("A"))
            else:
                flat = img.convert("RGB")
            out = io.BytesIO()
            flat.save(out, "JPEG", quality=90)
        return out.getvalue(), filename.rsplit(".", 1)[0] + ".jpg"

    def _ensure_hidden(self, pid, res, verify_first=False, changed_after=None):
        """Hidden, and verified -> True when Salla showed it hidden. Only for a product
        created in this run, or an existing one hidden before it (or left marked by an
        earlier run): _keep_hidden. Any other existing product's status is never touched
        (it may be published on purpose). verify_first: read the status first and POST
        only when it is not hidden; a failed read still POSTs (hiding a product of these
        kinds is always safe). changed_after: existing hidden product only, what this
        run wrote ("stock update"): a status Salla flipped by itself is put back AND
        reported as a warning (it was visible for a moment)."""
        found = ""
        if verify_first:
            try:
                found = mapping.detail_status(self._get_product(pid))
            except Exception as e:
                if not isinstance(e, UserError):
                    _logger.exception("Salla upload: status read of product %s failed", pid)
            if found == "hidden":
                return True
        try:
            self.backend_id._request("POST", "/products/%s/status" % pid,
                                     payload={"status": "hidden"})
            status = mapping.detail_status(self._get_product(pid))
        except Exception as e:
            # Also runs in a `finally`: it must never replace the error being reported.
            if not isinstance(e, UserError):
                _logger.exception("Salla upload: hiding product %s failed", pid)
            message = _("status not confirmed hidden (%s): check it in Salla") % _error_text(e)
        else:
            if status == "hidden":
                if changed_after and found:
                    # found "" = the read failed: not known to have changed.
                    message = _("Salla changed the status to %(status)s after the "
                                "%(step)s: put back to hidden") % {
                        "status": found, "step": changed_after}
                    if message not in res["messages"]:
                        self._flag(res, "warning", message)
                return True
            message = _("Salla shows status %r, not hidden: hide it in Salla by hand") % (
                status or "?")
        # The early call and the final re-check can fail the same way: say it once.
        if message not in res["messages"]:
            self._flag(res, "error", message)
        return False

    # ------------------------------------------------------------------ output
    def _result_html(self, products, selected, results, errors, warnings, left):
        esc = html.escape
        own = {m for p in products for m in p["errors"]}
        file_errors = [e for e in errors if e not in own]
        counts = {}
        for res in results.values():
            counts[res["status"]] = counts.get(res["status"], 0) + 1
        mode = dict(self._fields["mode"].selection).get(self.mode)
        parts = ["<p><b>%s</b>: %s of %s product(s) in the sheet. %s</p>" % (
            esc(mode), len(selected), len(products),
            esc(", ".join("%s %s" % (n, s) for s, n in sorted(counts.items())) or "-"))]
        if left:
            parts.append("<p><b>%s</b><br/>%s</p>" % (
                esc(_("%s product(s) left (time budget): the Only Handles field now lists "
                      "them, click Run again.") % len(left)),
                esc(", ".join(left))))
        for title, items in ((_("Errors"), file_errors), (_("Warnings"), warnings)):
            if items:
                parts.append("<p><b>%s</b></p><ul>%s</ul>" % (
                    esc(title), "".join("<li>%s</li>" % esc(i) for i in items)))
        rows = []
        for product in selected:
            res = results.get(product["handle"])
            if not res:
                continue
            rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                esc(res["handle"]), esc(res["salla_id"] or ""), esc(res["action"]),
                esc(res["status"]), "<br/>".join(esc(m) for m in res["messages"])))
        if rows:
            parts.append(
                "<table class='table table-sm'><thead><tr><th>Handle</th><th>Salla ID</th>"
                "<th>Action</th><th>Status</th><th>Details</th></tr></thead><tbody>%s"
                "</tbody></table>" % "".join(rows))
        return "".join(parts)

    def _result_xlsx(self, rows, products, results, errors, warnings):
        """The input rows with the result columns, every cell as text (barcodes keep their
        leading zeros, and a copy cell starting with "=" never becomes a formula).
        Values are written whole: this sheet is uploaded again. A value over Excel's
        cell limit cannot be: its row is marked error and a warning added."""
        import xlsxwriter
        headers = []
        for row in rows:
            headers += [k for k in row if k != "_row" and k not in headers]
        headers += [c for c in RESULT_COLUMNS if c not in headers]
        owner = {}
        for product in products:
            for variant in product["variants"]:
                owner[variant["row"]] = product
        buf = io.BytesIO()
        book = xlsxwriter.Workbook(buf, {"in_memory": True, "strings_to_numbers": False,
                                         "strings_to_formulas": False, "strings_to_urls": False})
        sheet = book.add_worksheet("Products")
        bold = book.add_format({"bold": True})
        for col, name in enumerate(headers):
            sheet.write_string(0, col, name, bold)
        for line, row in enumerate(rows, start=1):
            product = owner.get(row.get("_row"))
            res = results.get(product["handle"]) if product else None
            values = dict(row)
            prefix = "Row %s: " % row.get("_row")
            if res:
                first = row.get("_row") == product["row"]
                messages = [m for m in res["messages"] if m.startswith(prefix)
                            or (first and not m.startswith("Row "))]
                values.update({
                    "salla_product_id": res["salla_id"] or values.get("salla_product_id", ""),
                    "salla_variant_id": res["variant_ids"].get(row.get("sku"), ""),
                    "upload_action": res["action"], "upload_status": res["status"],
                    "upload_message": "; ".join(messages)})
            elif product:
                values.update({"upload_action": "not selected", "upload_status": "",
                               "upload_message": ""})
            else:
                messages = [e for e in errors if e.startswith(prefix)]
                values.update({"upload_action": "ignored",
                               "upload_status": "error" if messages else "",
                               "upload_message": "; ".join(messages)
                               or _("no handle and no SKU")})
            cells = [mapping.text(values.get(name)) for name in headers]
            too_long = [name for name, cell in zip(headers, cells) if len(cell) > XLSX_CELL_MAX]
            if too_long:
                message = _("cell too long (%s), do not re-upload this sheet") % ", ".join(
                    too_long)
                warnings.append("Row %s: %s over %s characters: the result sheet cuts "
                                "it, do not re-upload that sheet" % (
                                    row.get("_row"), ", ".join(too_long), XLSX_CELL_MAX))
                cells[headers.index("upload_status")] = "error"
                cells[headers.index("upload_message")] = "; ".join(
                    m for m in (message, cells[headers.index("upload_message")]) if m
                )[:XLSX_CELL_MAX]
            for col, cell in enumerate(cells):
                sheet.write_string(line, col, cell[:XLSX_CELL_MAX])
        sheet.freeze_panes(1, 0)
        book.close()
        return buf.getvalue()

    def _template_xlsx(self):
        import xlsxwriter
        buf = io.BytesIO()
        book = xlsxwriter.Workbook(buf, {"in_memory": True})
        as_text = book.add_format({"num_format": "@"})
        bold = book.add_format({"bold": True, "num_format": "@"})
        sheet = book.add_worksheet("Products")
        columns = mapping.UPLOAD_COLUMNS
        sheet.set_column(0, len(columns) - 1, 18, as_text)
        for col, name in enumerate(columns):
            sheet.write_string(0, col, name, bold)
        # Fake examples only: one product without options, one with Size x Scent (two
        # rows; product-level columns only on its first row).
        examples = [
            {"handle": "example-simple", "sku": "EXAMPLE-001",
             "name_ar": "منتج تجريبي", "name_en": "Example product",
             "description_ar": "وصف قصير للمنتج.\n- النقطة الأولى\n- النقطة الثانية",
             "description_en": "Short product description.\n- First point\n- Second point",
             "price": "25", "stock": "100", "weight": "0.5", "weight_unit": "kg",
             "image_1": "example-simple-1.jpg", "image_alt": "Example product"},
            {"handle": "example-options", "sku": "EXAMPLE-002-500",
             "name_ar": "منتج تجريبي بخيارات", "name_en": "Example product with options",
             "description_ar": "وصف قصير للمنتج.", "description_en": "Short product description.",
             "option1_name_ar": "الحجم", "option1_name_en": "Size",
             "option2_name_ar": "الرائحة", "option2_name_en": "Scent",
             "option1_value_ar": "500 مل", "option1_value_en": "500 ml",
             "option2_value_ar": "منعش", "option2_value_en": "Fresh",
             "price": "30", "sale_price": "27", "stock": "50", "weight": "600",
             "weight_unit": "g", "image_1": "example-options-1.jpg",
             "image_2": "example-options-500ml.jpg", "image_3": "example-options-1l.jpg",
             "variant_image": "example-options-500ml.jpg"},
            {"handle": "example-options", "sku": "EXAMPLE-002-1000",
             "option1_value_ar": "1 لتر", "option1_value_en": "1 L",
             "option2_value_ar": "منعش", "option2_value_en": "Fresh",
             "price": "50", "stock": "50", "weight": "1.1", "weight_unit": "kg",
             "variant_image": "example-options-1l.jpg"},
        ]
        for line, example in enumerate(examples, start=1):
            for col, name in enumerate(columns):
                if example.get(name):
                    sheet.write_string(line, col, example[name])
        sheet.freeze_panes(1, 2)
        help_sheet = book.add_worksheet("Instructions")
        help_sheet.set_column(0, 0, 38)
        help_sheet.set_column(1, 2, 14)
        help_sheet.set_column(3, 3, 90)
        wrap = book.add_format({"text_wrap": True, "valign": "top"})
        for col, title in enumerate(("Column", "Level", "Required", "Notes")):
            help_sheet.write_string(0, col, title, bold)
        for line, cells in enumerate(TEMPLATE_HELP, start=1):
            for col, value in enumerate(cells):
                help_sheet.write_string(line, col, value, wrap)
        book.close()
        return buf.getvalue()
