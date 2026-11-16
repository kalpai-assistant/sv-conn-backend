"""SVC-418: the default SP price backfill, frozen.

The migration (``471bb682ac1b``) and ``scripts/dry_run_default_sp_prices.py``
both read this module, so the dry-run counts exactly what the migration inserts.
It is named for the ticket and frozen like ``UPDATE_SERVICE_DETAILS_SVC_340``:
don't edit it for later work, because a re-run of the migration would change.

A default price is one ``sp_service_pricing`` row per ACTIVE variant of every
ACTIVE service an SP subscribes to (``provider_services``), at the variant's
``base_price`` (today's platform price) and the service's ``price_unit``. There
is no SP filter: subscribing already needs KYC VERIFIED, and the ranking sheet
(SVC-409) checks eligibility. This is a one-off baseline for launch. Nothing
writes a price at runtime: a service an SP enables later shows "Set your price"
(SVC-412).

**How the downgrade finds its own rows.** A backfilled row's id is derived from
its key, ``md5('svc-418:' || sp_id || ':' || service_variant_id)::uuid``,
instead of a random one. The app gives every other row a random ``uuid4``, so a
price an SP sets later never matches. An SP's edit bumps ``updated_at`` (the
ORM's ``onupdate``, in a later transaction), so the downgrade only removes rows
whose id matches and whose ``updated_at`` still equals ``created_at``. A raw-SQL
``UPDATE`` that leaves ``updated_at`` alone would look unedited.
"""

from sqlalchemy import text
from sqlalchemy.engine import Connection

ID_NAMESPACE = "svc-418"


def _default_id(sp_id: str, variant_id: str) -> str:
    return f"md5('{ID_NAMESPACE}:' || {sp_id}::text || ':' || {variant_id}::text)::uuid"


# Every (SP, active variant of an active subscribed service): the rows a
# default price is due for. ``services.is_active`` is nullable; NULL is not on.
_CANDIDATES = (
    "FROM provider_services ps "
    "JOIN services s ON s.id = ps.service_id AND s.is_active IS TRUE "
    "JOIN service_variants v ON v.service_id = s.id AND v.is_active IS TRUE"
)

# created_at = updated_at = the migration's transaction time. A row that is
# already there (an SP's own price, or this backfill's row kept by a downgrade
# because it was edited) is left exactly as it is.
INSERT_DEFAULT_PRICES = (
    "INSERT INTO sp_service_pricing (id, sp_id, service_id, service_variant_id, "
    "price_amount, price_unit, is_active, created_at, updated_at) "
    f"SELECT {_default_id('ps.provider_id', 'v.id')}, ps.provider_id, s.id, v.id, "
    "v.base_price, s.price_unit, true, now(), now() "
    f"{_CANDIDATES} "
    "ON CONFLICT (sp_id, service_variant_id) DO NOTHING"
)

DELETE_DEFAULT_PRICES = (
    "DELETE FROM sp_service_pricing p "
    f"WHERE p.id = {_default_id('p.sp_id', 'p.service_variant_id')} "
    "AND p.updated_at = p.created_at"
)


def backfill_default_prices(conn: Connection) -> int:
    """Insert the default prices; returns how many rows went in."""
    return conn.execute(text(INSERT_DEFAULT_PRICES)).rowcount


def remove_default_prices(conn: Connection) -> int:
    """Delete the backfill's rows nobody has edited; returns how many."""
    return conn.execute(text(DELETE_DEFAULT_PRICES)).rowcount


def default_price_counts(conn: Connection) -> dict[str, int]:
    """What ``backfill_default_prices`` would do on this database. Read-only."""
    row = (
        conn.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM provider_services) AS subscriptions, "
                "(SELECT count(*) FROM provider_services ps JOIN services s "
                " ON s.id = ps.service_id AND s.is_active IS TRUE) AS active_subscriptions, "
                "(SELECT count(DISTINCT ps.provider_id) FROM provider_services ps JOIN services s "
                " ON s.id = ps.service_id AND s.is_active IS TRUE) AS sps, "
                f"(SELECT count(*) {_CANDIDATES}) AS candidates, "
                f"(SELECT count(*) {_CANDIDATES} WHERE EXISTS (SELECT 1 FROM sp_service_pricing p "
                " WHERE p.sp_id = ps.provider_id AND p.service_variant_id = v.id)) "
                "AS already_present"
            )
        )
        .mappings()
        .one()
    )
    counts = dict(row)
    counts["to_insert"] = counts["candidates"] - counts["already_present"]
    return counts
