"""
"Notify me" on a service tile that shows no price (SVC-484, Jira 14950 / 14952).

**Kinds**, picked by the server from the point's state at the tap:

- ``coming_soon``: nobody serves the point (the tile's ``served`` is false).
  Fires as soon as someone does.
- ``no_pros_online``: served, but nobody listable (``served`` true, no
  ``from_price``). Fires when the service is listable there with "online"
  replaced by "online for 10 continuous minutes", so a Pro flicking the switch
  doesn't send a push.
- Already listable (the tile shows a price): refused, 409
  ``SERVICE_ALREADY_AVAILABLE``.

**The rules are the tile's.** ``served_at`` and ``listable_at`` read the same
priced join as ``from_prices_stmt`` (``from_price_service.serving_prices``)
and pick their stages by name from dispatch's one stage list, so an alert
can't fire for a point whose tile would still say "Coming soon". Both are
session-less EXISTS clauses. ``service_id`` and ``point`` may be values (the
PUT) or ``service_alerts`` columns (the sweep, ``service_alert_tasks``).

**Lifetime.** One open alert per (user, service). A second tap moves it to
the new point and restarts the 45 days. It fires once (``notified_at``),
the customer can cancel it (``cancelled_at``), and an expired one just
stops: nothing is written, and every reader filters ``expires_at > now``.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, and_, exists, literal, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import ServiceAlreadyAvailable, ServiceNotFound
from app.models.provider_profile import ProviderProfile
from app.models.service import Service
from app.models.service_alert import (
    KIND_COMING_SOON,
    KIND_NO_PROS_ONLINE,
    OPEN_PREDICATE,
    ServiceAlert,
)
from app.models.sp_service_pricing import SpServicePricing
from app.services.from_price_service import serving_prices
from app.services.provider_matching import listable_only_stages, stage_conditions
from app.utils.geo import build_point_wkt

ALERT_LIFETIME = timedelta(days=45)
# Vipul's dampener for "No pros online right now" (14950 #6).
STEADY_ONLINE = timedelta(minutes=10)
# The stage the steady rule replaces, by its name in ``_eligibility_stages``.
ONLINE_STAGE = "online"


def _priced_by_serving_sps(service_id, point, *conditions) -> Select:
    return (
        serving_prices(select(literal(1)), point)
        .where(SpServicePricing.service_id == service_id, *conditions)
        # Only the alert row is the outer query's; every other table here is
        # this subquery's own, even where the outer query names it too.
        .correlate(ServiceAlert)
    )


def served_at(service_id, point):
    """Some SP serves ``service_id`` at ``point`` with an active price, online
    or not: the tile's ``served``."""
    return exists(_priced_by_serving_sps(service_id, point))


def listable_stage_names(*, steady: bool) -> tuple[str, ...]:
    """``listable_only_stages``, without "online" when ``steady``. A rename of
    that stage raises here rather than dropping the online rule."""
    names = listable_only_stages()
    if not steady:
        return names
    if ONLINE_STAGE not in names:
        raise KeyError(ONLINE_STAGE)
    return tuple(name for name in names if name != ONLINE_STAGE)


def online_steadily(now: datetime) -> list:
    """Online, and switched on at least ``STEADY_ONLINE`` ago. A NULL
    ``online_since`` (not switched on since SVC-484) never passes."""
    return [
        ProviderProfile.is_online.is_(True),
        ProviderProfile.online_since <= now - STEADY_ONLINE,
    ]


def listable_at(service_id, point, *, steady_now: datetime | None = None):
    """An SP the "pick your SP" sheet could list at ``point`` has an active
    price for ``service_id``: the tile shows a price. With ``steady_now``, the
    "online" stage is replaced by ``online_steadily(steady_now)``."""
    steady = steady_now is not None
    conditions = stage_conditions(point, listable_stage_names(steady=steady))
    if steady:
        conditions += online_steadily(steady_now)
    return exists(_priced_by_serving_sps(service_id, point, *conditions))


def is_open(now: datetime) -> list:
    """Not notified, not cancelled, not expired."""
    return [
        ServiceAlert.notified_at.is_(None),
        ServiceAlert.cancelled_at.is_(None),
        ServiceAlert.expires_at > now,
    ]


@dataclass
class OpenAlert:
    service_id: uuid.UUID
    service_name: str
    kind: str
    created_at: datetime
    expires_at: datetime


class ServiceAlertService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_open(self, user_id: uuid.UUID) -> list[OpenAlert]:
        rows = await self.db.execute(
            select(
                ServiceAlert.service_id,
                Service.name,
                ServiceAlert.kind,
                ServiceAlert.created_at,
                ServiceAlert.expires_at,
            )
            .join(Service, Service.id == ServiceAlert.service_id)
            .where(ServiceAlert.user_id == user_id, *is_open(datetime.now(UTC)))
            .order_by(ServiceAlert.created_at.desc(), ServiceAlert.id)
        )
        return [OpenAlert(*row) for row in rows]

    async def kind_at(self, service_id: uuid.UUID, point_wkt: str) -> str:
        """The alert kind for the tile's state at this point now, or 409."""
        row = (
            await self.db.execute(
                select(
                    served_at(service_id, point_wkt).label("served"),
                    listable_at(service_id, point_wkt).label("listable"),
                )
            )
        ).one()
        if row.listable:
            raise ServiceAlreadyAvailable(service_id)
        return KIND_NO_PROS_ONLINE if row.served else KIND_COMING_SOON

    async def upsert(
        self, user_id: uuid.UUID, service_id: uuid.UUID, lat: float, lng: float
    ) -> OpenAlert:
        """Create the user's open alert for this service, or move it here. The
        partial unique index arbitrates, so two taps at once leave one row."""
        service = await self.db.get(Service, service_id)
        if service is None or not service.is_active:
            raise ServiceNotFound(service_id)
        point = build_point_wkt(lat, lng)
        kind = await self.kind_at(service_id, point)
        now = datetime.now(UTC)
        fresh = {
            "location": point,
            "kind": kind,
            "created_at": now,
            "expires_at": now + ALERT_LIFETIME,
        }
        stmt = (
            pg_insert(ServiceAlert)
            .values(id=uuid.uuid4(), user_id=user_id, service_id=service_id, **fresh)
            .on_conflict_do_update(
                index_elements=[ServiceAlert.user_id, ServiceAlert.service_id],
                index_where=text(OPEN_PREDICATE),
                set_=fresh,
            )
            .returning(ServiceAlert.created_at, ServiceAlert.expires_at)
        )
        row = (await self.db.execute(stmt)).one()
        return OpenAlert(service_id, service.name, kind, row.created_at, row.expires_at)

    async def cancel(self, user_id: uuid.UUID, service_id: uuid.UUID) -> None:
        """Cancel the open alert, if there is one. Idempotent."""
        await self.db.execute(
            update(ServiceAlert)
            .where(
                ServiceAlert.user_id == user_id,
                ServiceAlert.service_id == service_id,
                and_(ServiceAlert.notified_at.is_(None), ServiceAlert.cancelled_at.is_(None)),
            )
            .values(cancelled_at=datetime.now(UTC))
        )
