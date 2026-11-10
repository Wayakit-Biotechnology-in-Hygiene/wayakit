import logging

from odoo import fields, models

from .mapping import hide_marker_ref
from .salla_backend import WRITE_LOCK_TIMEOUT

_logger = logging.getLogger(__name__)


class SallaProductHide(models.Model):
    """Salla products the product upload must leave HIDDEN, until it has seen them hidden.

    Salla publishes a product by itself once a variant gets stock, and ignores
    `status: hidden` in the create body (spike 2026-11-10). The upload puts such a
    product back to hidden at the end, but a worker killed in between (limit_time_real),
    or two failed hide calls, would leave it published, and the next run reads it as
    "not hidden before" and never touches its status. A row here survives that: the
    next run counts the product as hidden before it. Only for HIDE_MARKER_MAX_AGE
    (24 h) after it was last marked: past that, Ops may have published the product on
    purpose, and the run only reports the marker (mapping.hide_marker_stale).

    Rows are written and deleted on their OWN cursor, committed at once (same reason as
    the token rule: they must survive a rollback or a kill of the upload's transaction),
    never through the ORM in the upload's transaction. Only the upload writes them;
    the ACL is for reading and for an admin clearing a stale one."""
    _name = "salla.product.hide"
    _description = "Salla Product Kept Hidden (upload)"
    _order = "id"

    backend_id = fields.Many2one("salla.backend", required=True, ondelete="cascade",
                                 index=True, readonly=True)
    # mapping.hide_marker_ref: "product:<Salla id>", or "sku:<parent SKU>" before a create.
    ref = fields.Char(required=True, readonly=True)
    handle = fields.Char(readonly=True)

    _sql_constraints = [
        ("backend_ref_uniq", "unique(backend_id, ref)",
         "A Salla product is marked once per backend."),
    ]

    def _refs(self, backend):
        """Marker refs of `backend`, read in the caller's transaction (no lock)."""
        return set(self._marked(backend))

    def _marked(self, backend):
        """{ref: when it was last marked (write_date, naive UTC)} of `backend`, read in
        the caller's transaction (no lock). The date says whether a leftover is still
        the next run's to recover (mapping.hide_marker_stale)."""
        self.env.cr.execute("SELECT ref, write_date FROM salla_product_hide "
                            "WHERE backend_id = %s", (backend.id,))
        return dict(self.env.cr.fetchall())

    def _mark(self, backend, handle, product_id="", sku=""):
        """Commit a marker on a fresh cursor. Raises on failure: the caller must not
        write to a product it cannot protect. The FK check takes a key-share lock on
        the backend row; lock_timeout bounds a wait on a token refresh holding it."""
        ref = hide_marker_ref(product_id=product_id, sku=sku)
        if not ref:
            return ""
        with self.env.registry.cursor() as cr:
            cr.execute("SET LOCAL lock_timeout = %s", (WRITE_LOCK_TIMEOUT,))
            cr.execute(
                "INSERT INTO salla_product_hide (backend_id, ref, handle, create_uid, "
                "write_uid, create_date, write_date) VALUES (%s, %s, %s, %s, %s, "
                "now() at time zone 'UTC', now() at time zone 'UTC') "
                # Marked again: the date restarts, so an old marker of a product this
                # run writes to again counts as fresh if this run dies too.
                "ON CONFLICT (backend_id, ref) DO UPDATE SET write_date = "
                "EXCLUDED.write_date, write_uid = EXCLUDED.write_uid, "
                "handle = COALESCE(EXCLUDED.handle, salla_product_hide.handle)",
                (backend.id, ref, handle or None, self.env.uid, self.env.uid))
        return ref

    def _clear(self, backend, refs):
        """Delete markers on a fresh cursor. -> False on failure (logged): a marker left
        behind only makes the next run check that product's status once more."""
        refs = [r for r in refs or () if r]
        if not refs:
            return True
        try:
            with self.env.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = %s", (WRITE_LOCK_TIMEOUT,))
                cr.execute("DELETE FROM salla_product_hide WHERE backend_id = %s "
                           "AND ref = ANY(%s)", (backend.id, refs))
        except Exception:
            _logger.warning("Salla upload: hidden marker(s) %s not cleared; the next run "
                            "checks them again", refs, exc_info=True)
            return False
        return True
