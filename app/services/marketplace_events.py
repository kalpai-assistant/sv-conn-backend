"""The only writer of ``marketplace_events`` (SVC-419). ADMIN.md lists the events.

- ``record_server_event``: our own events, added to the caller's transaction so
  an event can't describe a change that was rolled back. Their ids were
  inserted or checked earlier in that transaction, so the FKs can't fail.
- ``record_client_event``: one event from the app. ``INSERT ... ON CONFLICT
  (event_id) DO NOTHING`` in a savepoint: a retry stores nothing, and an id the
  DB doesn't know (an FK violation) is a 422 that rolls back only the savepoint.
- ``money``: every amount in a payload, as an exact 2-decimal string.
"""

import logging
import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import status
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AppException
from app.models.marketplace_event import MarketplaceEvent
from app.utils.money import to_decimal

logger = logging.getLogger(__name__)

ANALYTICS_UNKNOWN_REFERENCE = "ANALYTICS_UNKNOWN_REFERENCE"


def money(amount: Decimal | int | float | str) -> str:
    """An amount as stored in a payload: 2 decimals, half up ("2400.00")."""
    return str(to_decimal(amount))


async def record_server_event(
    db: AsyncSession,
    name: str,
    *,
    payload: dict,
    user_id: uuid.UUID | None = None,
    sp_id: uuid.UUID | None = None,
    booking_id: uuid.UUID | None = None,
    service_id: uuid.UUID | None = None,
    service_variant_id: uuid.UUID | None = None,
) -> None:
    """Add one server event to the caller's transaction; the caller commits."""
    db.add(
        MarketplaceEvent(
            event_id=uuid.uuid4(),
            name=name,
            occurred_at=None,
            user_id=user_id,
            sp_id=sp_id,
            booking_id=booking_id,
            service_id=service_id,
            service_variant_id=service_variant_id,
            payload=payload,
        )
    )
    await db.flush()


async def record_client_event(
    db: AsyncSession,
    *,
    event_id: uuid.UUID,
    name: str,
    occurred_at: datetime | None,
    user_id: uuid.UUID,
    payload: dict,
    sp_id: uuid.UUID | None = None,
    service_id: uuid.UUID | None = None,
    service_variant_id: uuid.UUID | None = None,
) -> bool:
    """Store one event from the app. False when ``event_id`` was already stored
    (by anyone); that row is left as it was. 422 ``ANALYTICS_UNKNOWN_REFERENCE``
    with nothing stored when an id names no row."""
    stmt = (
        pg_insert(MarketplaceEvent)
        .values(
            id=uuid.uuid4(),
            event_id=event_id,
            name=name,
            occurred_at=occurred_at,
            user_id=user_id,
            sp_id=sp_id,
            service_id=service_id,
            service_variant_id=service_variant_id,
            payload=payload,
        )
        .on_conflict_do_nothing(index_elements=[MarketplaceEvent.event_id])
        .returning(MarketplaceEvent.id)
    )
    try:
        async with db.begin_nested():
            stored = (await db.execute(stmt)).scalar_one_or_none() is not None
    except IntegrityError:
        raise AppException(
            "This event names a service, option or provider that doesn't exist.",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code=ANALYTICS_UNKNOWN_REFERENCE,
        ) from None
    logger.info(
        "analytics.event name=%s event_id=%s user_id=%s stored=%s",
        name,
        event_id,
        user_id,
        stored,
    )
    return stored


async def forget_user(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Account deletion (SVC-367): the user's events stay, pointing at nobody.
    ``sp_id`` is kept, like ``bookings.provider_id``: the tombstoned row it
    names holds no personal data."""
    await db.execute(
        update(MarketplaceEvent)
        .where(MarketplaceEvent.user_id == user_id)
        .values(user_id=None)
        .execution_options(synchronize_session=False)
    )
