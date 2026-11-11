"""Materialized views behind the admin analytics dashboards (SVC-432).

One copy of the SQL, used two ways:

- the SVC-432 migration imports ``VIEWS_V1`` and creates them on deployed DBs;
- ``register_create_all_hook`` (called from ``app.models``) hangs the same
  statements off ``Base.metadata``'s ``after_create``, so the test suite's
  ``create_all`` builds them too.

``VIEWS_V1`` is frozen: the migration that created it imports it, so editing it
would rewrite history. To change a view, add ``VIEWS_V2``, point
``CURRENT_VIEWS`` at it, and add a migration that drops the V1 views and creates
the V2 ones (``drop_statements`` / ``create_statements``).

Rules every view follows:

- no ``now()`` (except ``mv_analytics_meta.refreshed_at``): a view is a pure
  function of the tables, and the service applies time windows against
  ``as_of``. That keeps the math testable with a fixed clock;
- a unique index, so ``REFRESH MATERIALIZED VIEW CONCURRENTLY`` works;
- created WITH DATA: a concurrent refresh refuses a never-populated view;
- IST bucketing: ``(ts AT TIME ZONE 'Asia/Kolkata')::date`` for days and
  ``date_trunc('week', …)`` (ISO, Monday) for weeks.

``bookings.status``, ``users.role``, ``*.kyc_status`` and
``bookings.payment_status`` are SQLAlchemy name-enums, so their DB labels are the
upper-case member names (``'COMPLETED'``). ``booking_status_logs.to_status`` is a
varchar holding the lower-case value (``'accepted'``).
"""

from dataclasses import dataclass

from sqlalchemy import DDL, event


@dataclass(frozen=True)
class MatView:
    name: str
    select: str
    unique_cols: tuple[str, ...]


_IST_DAY = "(b.created_at AT TIME ZONE 'Asia/Kolkata')::date"
_IST_WEEK = "date_trunc('week', b.created_at AT TIME ZONE 'Asia/Kolkata')::date"

VIEWS_V1: tuple[MatView, ...] = (
    MatView(
        "mv_analytics_meta",
        # now() is the refresh transaction's start, so every view refreshed in
        # the same transaction shares this as_of.
        """
        SELECT 1 AS id,
               now() AS refreshed_at,
               (SELECT min(created_at) FROM bookings) AS first_booking_at
        """,
        ("id",),
    ),
    MatView(
        "mv_bookings_daily",
        f"""
        SELECT {_IST_DAY} AS day,
               lower(b.status::text) AS status,
               count(*) AS bookings,
               coalesce(sum(b.service_amount) FILTER (WHERE b.status = 'COMPLETED'), 0) AS gmv,
               coalesce(sum(b.platform_commission) FILTER (WHERE b.status = 'COMPLETED'), 0)
                   AS commission,
               count(*) FILTER (
                   WHERE b.payment_status = 'REFUNDED'
                      OR EXISTS (
                          SELECT 1 FROM booking_disputes d
                          WHERE d.booking_id = b.id
                            AND d.refund_amount IS NOT NULL
                            AND d.refund_status IS DISTINCT FROM 'FAILED'
                      )
               ) AS refunded
        FROM bookings b
        GROUP BY 1, 2
        """,
        ("day", "status"),
    ),
    MatView(
        "mv_consumer_rebook",
        # One row per consumer with a completed booking. anchor_at is the
        # created_at of their first completed booking; next_booking_at is the
        # first other booking (any status) created at or after it.
        """
        WITH firsts AS (
            SELECT DISTINCT ON (b.consumer_id)
                   b.consumer_id, b.id AS booking_id, b.created_at AS anchor_at
            FROM bookings b
            WHERE b.status = 'COMPLETED'
            ORDER BY b.consumer_id, b.created_at, b.id
        )
        SELECT f.consumer_id,
               date_trunc('week', u.created_at AT TIME ZONE 'Asia/Kolkata')::date AS signup_week,
               f.anchor_at,
               (SELECT min(b2.created_at) FROM bookings b2
                WHERE b2.consumer_id = f.consumer_id
                  AND b2.id <> f.booking_id
                  AND b2.created_at >= f.anchor_at) AS next_booking_at
        FROM firsts f
        JOIN users u ON u.id = f.consumer_id
        WHERE u.role = 'CONSUMER'
        """,
        ("consumer_id",),
    ),
    MatView(
        "mv_supply_activity_daily",
        # Distinct (kind, entity, IST day) with at least one booking of any
        # status. 'sp' = currently KYC-verified provider (bookings.provider_id);
        # 'pro' = currently active professional (bookings.professional_id).
        f"""
        SELECT 'sp'::text AS kind, b.provider_id AS entity_id, {_IST_DAY} AS day
        FROM bookings b
        JOIN provider_profiles p ON p.user_id = b.provider_id
        WHERE p.kyc_status = 'VERIFIED'
        UNION
        SELECT 'pro'::text, b.professional_id, {_IST_DAY}
        FROM bookings b
        JOIN professionals pr ON pr.id = b.professional_id
        WHERE pr.is_active
        """,
        ("kind", "entity_id", "day"),
    ),
    MatView(
        "mv_sp_lifecycle",
        # Every currently verified SP. booked_dN = at least one booking created
        # in the 30 days ending N days after kyc_reviewed_at.
        """
        SELECT p.user_id AS provider_id,
               p.kyc_reviewed_at,
               u.created_at AS signup_at,
               (SELECT max(b.created_at) FROM bookings b WHERE b.provider_id = p.user_id)
                   AS last_booking_at,
               EXISTS (SELECT 1 FROM bookings b WHERE b.provider_id = p.user_id
                       AND b.created_at >= p.kyc_reviewed_at
                       AND b.created_at < p.kyc_reviewed_at + interval '30 days') AS booked_d30,
               EXISTS (SELECT 1 FROM bookings b WHERE b.provider_id = p.user_id
                       AND b.created_at >= p.kyc_reviewed_at + interval '30 days'
                       AND b.created_at < p.kyc_reviewed_at + interval '60 days') AS booked_d60,
               EXISTS (SELECT 1 FROM bookings b WHERE b.provider_id = p.user_id
                       AND b.created_at >= p.kyc_reviewed_at + interval '60 days'
                       AND b.created_at < p.kyc_reviewed_at + interval '90 days') AS booked_d90
        FROM provider_profiles p
        JOIN users u ON u.id = p.user_id
        WHERE p.kyc_status = 'VERIFIED'
        """,
        ("provider_id",),
    ),
    MatView(
        "mv_supply_gap_weekly",
        # pincode x service x IST week. A booking with several items counts once
        # per service. unmet = EXPIRED and never accepted (no accept log).
        f"""
        SELECT {_IST_WEEK} AS week_start,
               coalesce(nullif(a.pincode, ''), 'Unknown') AS pincode,
               bi.service_id,
               s.name AS service_name,
               count(DISTINCT b.id) AS demand,
               count(DISTINCT b.id) FILTER (
                   WHERE b.status = 'EXPIRED'
                     AND NOT EXISTS (
                         SELECT 1 FROM booking_status_logs l
                         WHERE l.booking_id = b.id AND l.to_status = 'accepted'
                     )
               ) AS unmet
        FROM bookings b
        JOIN booking_items bi ON bi.booking_id = b.id
        JOIN services s ON s.id = bi.service_id
        LEFT JOIN addresses a ON a.id = b.consumer_address_id
        GROUP BY 1, 2, 3, 4
        """,
        ("week_start", "pincode", "service_id"),
    ),
    MatView(
        "mv_price_by_category",
        # All-time unit_price of items on COMPLETED bookings, by the service's
        # own category.
        """
        SELECT c.id AS category_id,
               c.name AS category_name,
               count(*) AS items,
               min(bi.unit_price) AS min_price,
               percentile_cont(0.25) WITHIN GROUP (ORDER BY bi.unit_price) AS p25,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY bi.unit_price) AS median,
               percentile_cont(0.75) WITHIN GROUP (ORDER BY bi.unit_price) AS p75,
               max(bi.unit_price) AS max_price
        FROM booking_items bi
        JOIN bookings b ON b.id = bi.booking_id
        JOIN services s ON s.id = bi.service_id
        JOIN categories c ON c.id = s.category_id
        WHERE b.status = 'COMPLETED'
        GROUP BY c.id, c.name
        """,
        ("category_id",),
    ),
    MatView(
        "mv_time_to_accept_weekly",
        # First accept log minus created_at; a reopened booking (two accepts)
        # uses the first. Never-accepted bookings are left out.
        f"""
        WITH first_accept AS (
            SELECT l.booking_id, min(l.created_at) AS accepted_at
            FROM booking_status_logs l
            WHERE l.to_status = 'accepted'
            GROUP BY l.booking_id
        )
        SELECT {_IST_WEEK} AS week_start,
               count(*) AS accepted,
               percentile_cont(0.5) WITHIN GROUP (
                   ORDER BY extract(epoch FROM fa.accepted_at - b.created_at)) AS p50_seconds,
               percentile_cont(0.9) WITHIN GROUP (
                   ORDER BY extract(epoch FROM fa.accepted_at - b.created_at)) AS p90_seconds
        FROM bookings b
        JOIN first_accept fa ON fa.booking_id = b.id
        GROUP BY 1
        """,
        ("week_start",),
    ),
)

CURRENT_VIEWS = VIEWS_V1


def create_statements(views: tuple[MatView, ...], if_not_exists: bool = False) -> list[str]:
    """CREATE ... WITH DATA + its unique index, one statement per string (asyncpg
    runs one statement per execute)."""
    ine = "IF NOT EXISTS " if if_not_exists else ""
    out: list[str] = []
    for v in views:
        out.append(f"CREATE MATERIALIZED VIEW {ine}{v.name} AS {v.select.strip()} WITH DATA")
        out.append(
            f"CREATE UNIQUE INDEX {ine}uix_{v.name} ON {v.name} ({', '.join(v.unique_cols)})"
        )
    return out


def drop_statements(views: tuple[MatView, ...]) -> list[str]:
    return [f"DROP MATERIALIZED VIEW IF EXISTS {v.name}" for v in reversed(views)]


def refresh_statements(views: tuple[MatView, ...] = CURRENT_VIEWS) -> list[str]:
    return [f"REFRESH MATERIALIZED VIEW CONCURRENTLY {v.name}" for v in views]


def register_create_all_hook(metadata) -> None:
    """Make ``metadata.create_all`` build the views after the tables.

    IF NOT EXISTS keeps it idempotent across the suite's per-test create_all. It
    also means a long-lived test DB keeps whatever definition it first got: after
    a VIEWS_V2 lands, drop the analytics matviews in sc_test (or use a fresh DB).
    """
    for stmt in create_statements(CURRENT_VIEWS, if_not_exists=True):
        event.listen(metadata, "after_create", DDL(stmt))
