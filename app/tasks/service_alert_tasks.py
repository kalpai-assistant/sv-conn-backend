"""
The "Notify me" sweep (SVC-484): every 2 minutes, push the customers whose
service alerts have come due. The rules live in ``service_alert_service``.

**Per sweep**, outside quiet hours (22:00–08:00 IST, when nothing is read or
sent):

1. One SELECT of the due alerts: open, not expired, the customer active and
   holding a push token, the service active, and the kind's rule true at the
   alert's own point (``coming_soon``: served; ``no_pros_online``: listable
   with an SP online for 10 continuous minutes). ``FOR UPDATE SKIP LOCKED``,
   so an overlapping sweep skips these rows. At most ``BATCH_ALERTS`` worth of
   alerts, cut on whole customers, so nobody's alerts split over two pushes.
2. One push per customer, however many alerts came due.
3. One UPDATE stamping ``notified_at`` on the alerts whose push went out,
   then commit. Each fires once.

That's 2 statements whatever the number of customers, alerts or SPs, plus
one per dead token (``_clear_dead_fcm_token``, after the commit). An alert
whose customer has no token isn't selected: it waits for a token, or
expiry. An alert whose push failed stays open for the next sweep.

**After the commit**, the from-prices cache key at each notified alert's point
is deleted, so the tile the push sends the customer to isn't 10 minutes stale
(14952 #7). A Redis error there is logged, never raised.

Due alerts found during quiet hours aren't queued. A later sweep reads them
again, so at 08:00 they go out only if the service is still available.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone

import redis
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session, aliased

from app.config import get_settings
from app.models.service import Service
from app.models.service_alert import KIND_COMING_SOON, KIND_NO_PROS_ONLINE, ServiceAlert
from app.models.user import User
from app.services.from_price_service import cache_key
from app.services.service_alert_service import is_open, listable_at, served_at
from app.tasks.celery_app import celery_app
from app.tasks.notification_tasks import _DEAD_TOKEN_ERRORS, _clear_dead_fcm_token
from app.utils.fcm_client import send_push_notification
from app.utils.geo import point_lat_lng

settings = get_settings()
logger = logging.getLogger(__name__)

# IST has no DST, so a fixed offset (as app/analytics/service.py) and no tzdata.
_IST = timezone(timedelta(hours=5, minutes=30))
QUIET_FROM_HOUR = 22  # inclusive
QUIET_UNTIL_HOUR = 8  # exclusive: 08:00 sends

# Upper bound on alerts read per sweep. The rest wait 2 minutes.
BATCH_ALERTS = 500

PUSH_TYPE = "service_alert"
PUSH_EVENT = "service_available"


def in_quiet_hours(now: datetime) -> bool:
    hour = now.astimezone(_IST).hour
    return hour >= QUIET_FROM_HOUR or hour < QUIET_UNTIL_HOUR


def push_copy(service_names: list[str]) -> tuple[str, str]:
    """(title, body) for one customer's due alerts, in English: the customer
    app is English-only (14952 #3)."""
    if len(service_names) == 1:
        return "Now available near you", f"{service_names[0]} is now available in your area"
    return (
        "Now available near you",
        f"{len(service_names)} services you asked about are now available",
    )


def due_alerts_stmt(now: datetime, limit: int):
    """The due alerts with their customer and service name, grouped by customer.
    The customer and the alert's service are aliased, so the rule EXISTS
    clauses keep their own ``users`` / ``services`` rows (the SP's)."""
    customer = aliased(User)
    service = aliased(Service)
    due = or_(
        and_(
            ServiceAlert.kind == KIND_COMING_SOON,
            served_at(ServiceAlert.service_id, ServiceAlert.location),
        ),
        and_(
            ServiceAlert.kind == KIND_NO_PROS_ONLINE,
            listable_at(ServiceAlert.service_id, ServiceAlert.location, steady_now=now),
        ),
    )
    return (
        select(ServiceAlert, customer, service.name.label("service_name"))
        .join(customer, customer.id == ServiceAlert.user_id)
        .join(service, service.id == ServiceAlert.service_id)
        .where(
            *is_open(now),
            customer.is_active.is_(True),
            customer.fcm_token.is_not(None),
            service.is_active.is_(True),
            due,
        )
        .order_by(ServiceAlert.user_id, ServiceAlert.created_at, ServiceAlert.id)
        .limit(limit + 1)
        .with_for_update(of=ServiceAlert, skip_locked=True)
    )


def _whole_customers(rows: list, limit: int) -> list:
    """At most ``limit`` rows, never splitting a customer. ``rows`` holds up to
    ``limit + 1``; with more than ``limit``, the customer of the extra row
    may have alerts on both sides of the cut, so all of theirs wait. If that
    leaves nothing (one customer above the limit), they get the first ``limit``."""
    if len(rows) <= limit:
        return rows
    straddling = rows[limit].ServiceAlert.user_id
    kept = [row for row in rows[:limit] if row.ServiceAlert.user_id != straddling]
    return kept or rows[:limit]


@dataclass
class SweepResult:
    notified: list[uuid.UUID] = field(default_factory=list)
    dead_tokens: list[tuple[User, Exception]] = field(default_factory=list)
    cache_keys: list[str] = field(default_factory=list)


def sweep_due_alerts(db: Session, now: datetime, *, limit: int = BATCH_ALERTS) -> SweepResult:
    """Steps 1–3 of the module docstring, without the commit (the caller's)."""
    result = SweepResult()
    if in_quiet_hours(now):
        return result

    rows = _whole_customers(db.execute(due_alerts_stmt(now, limit)).all(), limit)
    by_customer: dict[uuid.UUID, list] = {}
    for row in rows:
        by_customer.setdefault(row.ServiceAlert.user_id, []).append(row)

    # The push goes out while the rows are locked and before notified_at is
    # written. If the UPDATE or commit then fails, the next sweep pushes
    # again: a rare duplicate beats a lost alert, which is the other order's
    # failure (stamped, then the push never sent).
    keys: set[str] = set()
    for customer_rows in by_customer.values():
        customer = customer_rows[0][1]
        names = [row.service_name for row in customer_rows]
        title, body = push_copy(names)
        first = customer_rows[0].ServiceAlert
        try:
            send_push_notification(
                customer.fcm_token,
                title=title,
                body=body,
                data={
                    "type": PUSH_TYPE,
                    "event": PUSH_EVENT,
                    "service_id": str(first.service_id),
                    "service_name": names[0],
                    "count": str(len(names)),
                },
            )
        except _DEAD_TOKEN_ERRORS as dead:
            result.dead_tokens.append((customer, dead))
            continue
        except Exception:
            # One customer's failed push never stops the rest; theirs stay open.
            logger.exception("service_alert.push_failed user_id=%s", customer.id)
            continue
        for row in customer_rows:
            result.notified.append(row.ServiceAlert.id)
            lat, lng = point_lat_lng(row.ServiceAlert.location)
            keys.add(cache_key(lat, lng))

    if result.notified:
        db.execute(
            update(ServiceAlert)
            .where(ServiceAlert.id.in_(result.notified))
            .values(notified_at=now)
            .execution_options(synchronize_session=False)
        )
    result.cache_keys = sorted(keys)
    return result


def forget_cached_tiles(keys: list[str]) -> None:
    """Delete the from-prices cache at the notified points. Best-effort."""
    if not keys:
        return
    client = redis.Redis.from_url(settings.REDIS_URL)
    try:
        client.delete(*keys)
    except Exception as exc:
        logger.warning("service_alert.cache_clear_failed keys=%d error=%r", len(keys), exc)
    finally:
        try:
            client.close()
        except Exception:
            pass


@celery_app.task(name="app.tasks.service_alert_tasks.notify_service_alerts")
def notify_service_alerts():
    """Celery Beat, every 120 s."""
    from app.database import SyncSessionLocal

    now = datetime.now(UTC)
    if in_quiet_hours(now):
        return
    with SyncSessionLocal() as db:
        result = sweep_due_alerts(db, now)
        db.commit()
        # After the commit, so the notified_at stamps are durable first; each
        # clear commits on its own. Those customers' alerts stay open.
        for customer, error in result.dead_tokens:
            _clear_dead_fcm_token(db, customer, error)
    forget_cached_tiles(result.cache_keys)
    if result.notified or result.dead_tokens:
        logger.info(
            "service_alert.sweep notified=%d dead_tokens=%d",
            len(result.notified),
            len(result.dead_tokens),
        )
