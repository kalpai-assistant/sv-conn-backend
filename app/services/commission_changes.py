"""Scheduled commission changes (SVC-486, epic SVC-406). Model and table notes
are in ``app/models/commission_change.py``.

The admin side (schedule, cancel, the PATCH paper trail) is in
``AdminVariantService``. This module has the rules both it and booking need:

* **When a change takes effect.** ``effective_from`` is a date, and the change
  takes effect at 00:00 IST that day. ``today_ist`` is the one clock every
  check reads. Tests pin it by patching this module's ``datetime``.
* **Applying.** ``apply_due_commission_changes`` is the only place a scheduled
  change becomes live. It sets the variant's amounts with
  ``commission.split_at_pct`` on the variant's price at that moment, so a price
  edit between scheduling and applying is fine. It stores the amounts before
  and after on the change row and marks it ``applied`` in the same transaction,
  so a change can never apply twice. It writes no admin audit row: no admin
  acted, and the schedule was audited.
  - Called by the ``apply-commission-changes`` beat entry every 60 s.
  - Called by ``create_booking`` for the booked services before it reads them,
    so a booking made at or after 00:00 IST on the date is split at the new
    percent even before the sweep runs.
  - Called by every admin variant write and commission-change write, right
    after the service lock, so the admin never edits on top of a change that is
    due but not yet applied.
* **Bookings are judged by when they are created.** Existing ``booking_items``
  keep the split they were made with.

Locks: services ``FOR UPDATE``, then variants, then the change rows, each in id
order. The admin variant editor takes service → variant, the same prefix, so
the two can't deadlock. When nothing is due, which is almost every booking, the
only cost is one unlocked indexed read.
"""

import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import call_after_commit
from app.models.commission_change import (
    STATUS_APPLIED,
    STATUS_SCHEDULED,
    CommissionChange,
)
from app.models.service import Service
from app.models.service_variant import ServiceVariant
from app.services.commission import split_at_pct
from app.services.provider_service import _IST
from app.utils.money import to_decimal

NOTICE_DAYS = 14


def today_ist() -> date:
    """Today's date in IST: a change dated today or earlier is in force."""
    return datetime.now(_IST).date()


def earliest_increase_date() -> date:
    """The first date an increase can take effect: 14 days' notice, counting
    from today (IST)."""
    return today_ist() + timedelta(days=NOTICE_DAYS)


def format_pct(pct: Decimal | float | str) -> str:
    """A percent as the notices show it: "20" for 20.00, "30.04" for 30.04,
    "12.5" for 12.50. No % sign, which the copy adds."""
    text = f"{to_decimal(pct):.2f}"
    return text.rstrip("0").rstrip(".")


def format_notice_date(value: date) -> str:
    """A date as the notices show it, "06 Dec 2026" (the SMS template's form).
    English month names in every language, like the brand terms."""
    return value.strftime("%d %b %Y")


def apply_change(change: CommissionChange, variant: ServiceVariant) -> None:
    """Make ``change`` live on ``variant``. The caller holds the variant's lock
    and has checked the change is due and still scheduled."""
    change.old_platform_commission = to_decimal(variant.platform_commission)
    change.old_provider_payout = to_decimal(variant.provider_payout)
    commission, payout = split_at_pct(variant.base_price, change.new_pct)
    variant.platform_commission = commission
    variant.provider_payout = payout
    change.new_platform_commission = commission
    change.new_provider_payout = payout
    change.status = STATUS_APPLIED
    change.applied_at = datetime.now(UTC)


async def apply_due_commission_changes(
    db: AsyncSession, *, service_ids: Iterable[uuid.UUID] | None = None
) -> list[uuid.UUID]:
    """Apply every scheduled change whose date has come (on ``service_ids``'
    variants, or everywhere when None). Returns the ids it applied. Never
    commits: the caller's transaction does, together with whatever else it does.

    Keyed by service, not by variant, because ``create_booking`` can fall back to
    a service's first active variant, which it reads from the service.
    """
    today = today_ist()
    stmt = (
        select(CommissionChange.id, CommissionChange.service_variant_id, ServiceVariant.service_id)
        .join(ServiceVariant, ServiceVariant.id == CommissionChange.service_variant_id)
        .where(
            CommissionChange.status == STATUS_SCHEDULED,
            CommissionChange.effective_from <= today,
        )
    )
    if service_ids is not None:
        service_ids = list(service_ids)
        if not service_ids:
            return []
        stmt = stmt.where(ServiceVariant.service_id.in_(service_ids))
    due = (await db.execute(stmt)).all()
    if not due:
        return []

    # The admin editor's order: services, then variants. Then the change rows,
    # whose status is re-read under the lock. That re-read is what keeps a
    # change from applying twice when the sweep and a booking race.
    await db.execute(
        select(Service.id)
        .where(Service.id.in_({row.service_id for row in due}))
        .order_by(Service.id)
        .with_for_update()
    )
    variants = {
        v.id: v
        for v in await db.scalars(
            select(ServiceVariant)
            .where(ServiceVariant.id.in_({row.service_variant_id for row in due}))
            .order_by(ServiceVariant.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    changes = await db.scalars(
        select(CommissionChange)
        .where(CommissionChange.id.in_([row.id for row in due]))
        .order_by(CommissionChange.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    applied: list[uuid.UUID] = []
    for change in changes:
        if change.status != STATUS_SCHEDULED or change.effective_from > today:
            continue
        apply_change(change, variants[change.service_variant_id])
        applied.append(change.id)
    await db.flush()
    return applied


async def due_service_ids(db: AsyncSession) -> list[uuid.UUID]:
    """The services with a scheduled change whose date has come, in id order.
    No locks: the sweep then applies each one in its own transaction."""
    rows = await db.scalars(
        select(ServiceVariant.service_id)
        .join(CommissionChange, CommissionChange.service_variant_id == ServiceVariant.id)
        .where(
            CommissionChange.status == STATUS_SCHEDULED,
            CommissionChange.effective_from <= today_ist(),
        )
        .distinct()
        .order_by(ServiceVariant.service_id)
    )
    return list(rows)


async def scheduled_changes(
    db: AsyncSession, variant_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, CommissionChange]:
    """Variant id → its scheduled change, for the variants that have one."""
    variant_ids = list(variant_ids)
    if not variant_ids:
        return {}
    rows = await db.scalars(
        select(CommissionChange).where(
            CommissionChange.service_variant_id.in_(variant_ids),
            CommissionChange.status == STATUS_SCHEDULED,
        )
    )
    return {row.service_variant_id: row for row in rows}


def enqueue_notices(db: AsyncSession, change_id: uuid.UUID, kind: str) -> None:
    """Send the change's notices once the transaction commits (push, email and,
    for a scheduled one, SMS, to every SP with an active price on the variant)."""
    change_id_str = str(change_id)

    def _send() -> None:
        from app.tasks.commission_tasks import send_commission_change_notices

        send_commission_change_notices.delay(change_id=change_id_str, kind=kind)

    call_after_commit(db, _send)
