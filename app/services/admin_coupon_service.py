"""SVC-429: admin reads and audited writes of coupons.

The consumer rules (``CouponService``: validate, list_available, apply) are not
touched. This module only creates, edits and switches coupons, and reports how
they've been used.

* Codes are stored lowercased, as the consumer lookups expect, and shown
  uppercased. The code never changes after create: it's the URL key.
* Usage is the count of ``coupon_redemptions`` rows, the count the limits use.
  A redemption is never deleted, so a cancelled booking still counts.
* A coupon is never deleted. Deactivate is the kill switch and activate undoes it,
  since the code is UNIQUE and a mistaken deactivate would otherwise burn it.
* Every save that changes something writes one ``admin_actions`` row. A save that
  changes nothing writes none.

A refused value is a 422 ``COUPON_INVALID`` with ``details.field`` naming the
field, as SVC-428's ``PRICING_INVALID`` does.
"""

import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import status
from sqlalchemy import Select, and_, case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import AppException
from app.models.booking import Booking
from app.models.coupon import Coupon, CouponRedemption, DiscountType
from app.models.user import User
from app.schemas.admin_coupons import (
    AdminCouponCreate,
    AdminCouponDetail,
    AdminCouponListResponse,
    AdminCouponRedemptionRow,
    AdminCouponRow,
    AdminCouponUpdate,
    CouponStatus,
)
from app.services.admin_audit import AuditActor, audit_admin_action, snapshot
from app.utils.money import to_decimal

CODE_PATTERN = re.compile(r"^[A-Z0-9]{3,20}$")
EDITABLE_FIELDS = (
    "discount_type",
    "discount_value",
    "max_discount",
    "min_order_amount",
    "valid_from",
    "valid_until",
    "usage_limit",
    "usage_limit_per_user",
)
# Only these may be null: no cap, no minimum, no global limit.
_NULLABLE_FIELDS = frozenset({"max_discount", "min_order_amount", "usage_limit"})
_MONEY_FIELDS = ("discount_value", "max_discount", "min_order_amount")
_HUNDRED = Decimal("100")
# numeric(12,2) holds up to 9,999,999,999.99.
_MONEY_LIMIT = Decimal("10000000000")
_INT_MAX = 2_147_483_647
RECENT_REDEMPTIONS = 20


class AdminCouponInvalid(AppException):
    """Not ``app.exceptions.CouponInvalid``, which is the consumer's 400."""

    def __init__(self, field: str, message: str):
        super().__init__(
            message,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="COUPON_INVALID",
            details={"field": field},
        )


class CouponNotFound(AppException):
    def __init__(self):
        super().__init__(
            "Coupon not found", status_code=status.HTTP_404_NOT_FOUND, code="COUPON_NOT_FOUND"
        )


class CouponCodeTaken(AppException):
    def __init__(self, code: str):
        super().__init__(
            f"A coupon with code {code} already exists",
            status_code=status.HTTP_409_CONFLICT,
            code="COUPON_CODE_TAKEN",
            details={"field": "code"},
        )


# -- the one definition of usage and status ---------------------------------


def _usage_count():
    """Redemptions of the outer query's coupon: the count validate() limits on."""
    return (
        select(func.count(CouponRedemption.id))
        .where(CouponRedemption.coupon_id == Coupon.id)
        .correlate(Coupon)
        .scalar_subquery()
    )


def _status(now: datetime, uses):
    """A coupon's status. The first match wins, so every coupon has exactly one.

    ``exhausted`` checks only the global ``usage_limit``: the per-user limit
    depends on who is asking, so it isn't a status of the coupon.
    """
    return case(
        (Coupon.is_active.is_(False), "inactive"),
        (Coupon.valid_until <= now, "expired"),
        (and_(Coupon.usage_limit.is_not(None), uses >= Coupon.usage_limit), "exhausted"),
        (Coupon.valid_from > now, "scheduled"),
        else_="active",
    )


def _rows_query(now: datetime) -> Select:
    uses = _usage_count()
    return (
        select(
            Coupon,
            uses.label("usage_count"),
            _status(now, uses).label("status"),
            User.name,
            User.phone,
        )
        .outerjoin(User, User.id == Coupon.user_id)
        .order_by(Coupon.created_at.desc(), Coupon.id.desc())
    )


def _money(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _row(coupon: Coupon, usage_count: int, status_: str, name, phone) -> AdminCouponRow:
    return AdminCouponRow(
        id=coupon.id,
        code=coupon.code.upper(),
        discount_type=coupon.discount_type,
        discount_value=float(coupon.discount_value),
        max_discount=_money(coupon.max_discount),
        min_order_amount=_money(coupon.min_order_amount),
        valid_from=coupon.valid_from,
        valid_until=coupon.valid_until,
        usage_limit=coupon.usage_limit,
        usage_limit_per_user=coupon.usage_limit_per_user,
        is_active=coupon.is_active,
        user_id=coupon.user_id,
        owner_name=name if coupon.user_id is not None else None,
        owner_phone=phone if coupon.user_id is not None else None,
        usage_count=usage_count,
        status=status_,
        created_at=coupon.created_at,
    )


# -- validation ---------------------------------------------------------------


def _as_utc(moment: datetime) -> datetime:
    """A datetime with no offset means UTC."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _check_money(field: str, value: Decimal) -> None:
    if not value.is_finite():
        raise AdminCouponInvalid(field, f"{field} must be a number")
    if value != value.quantize(Decimal("0.01")):
        raise AdminCouponInvalid(field, f"{field} takes at most 2 decimal places")
    if value >= _MONEY_LIMIT:
        raise AdminCouponInvalid(field, f"{field} is too large")


def _normalise_code(raw: str) -> str:
    code = raw.strip().upper()
    if not CODE_PATTERN.fullmatch(code):
        raise AdminCouponInvalid("code", "Code must be 3 to 20 letters or digits")
    return code


def _validate(
    values: dict[str, Any],
    sent: dict[str, Any],
    *,
    now: datetime,
    creating: bool,
    usage: int,
) -> dict[str, Any]:
    """The row to save, from ``values`` (the row as it would be saved) and
    ``sent`` (what the body set), or a 422. Rules, in order:

    - discount_value > 0, and at most 100 for a percent coupon;
    - max_discount only for a percent coupon, and > 0 when set. A flat coupon
      saves it as null, which validate() ignores for flat anyway;
    - min_order_amount >= 0 or null;
    - valid_until after valid_from, and (on create only) in the future;
    - usage_limit > 0 or null, and not below the current usage when sent;
    - usage_limit_per_user >= 1.
    """
    for field, value in sent.items():
        if value is None and field not in _NULLABLE_FIELDS:
            raise AdminCouponInvalid(field, f"{field} can't be empty")

    out = dict(values)
    for field in _MONEY_FIELDS:
        if out[field] is not None:
            _check_money(field, out[field])
            out[field] = to_decimal(out[field])
    for field in ("valid_from", "valid_until"):
        out[field] = _as_utc(out[field])

    percent = out["discount_type"] is DiscountType.PERCENT
    if out["discount_value"] <= 0:
        raise AdminCouponInvalid("discount_value", "discount_value must be more than 0")
    if percent and out["discount_value"] > _HUNDRED:
        raise AdminCouponInvalid("discount_value", "A percent discount can be at most 100")

    if not percent:
        if sent.get("max_discount") is not None:
            raise AdminCouponInvalid(
                "max_discount", "max_discount applies only to a percent coupon"
            )
        out["max_discount"] = None
    elif out["max_discount"] is not None and out["max_discount"] <= 0:
        raise AdminCouponInvalid("max_discount", "max_discount must be more than 0, or empty")

    if out["min_order_amount"] is not None and out["min_order_amount"] < 0:
        raise AdminCouponInvalid("min_order_amount", "min_order_amount must be 0 or more")

    if out["valid_until"] <= out["valid_from"]:
        raise AdminCouponInvalid("valid_until", "valid_until must be after valid_from")
    if creating and out["valid_until"] <= now:
        raise AdminCouponInvalid("valid_until", "valid_until must be in the future")

    limit = out["usage_limit"]
    if limit is not None:
        if not 0 < limit <= _INT_MAX:
            raise AdminCouponInvalid("usage_limit", "usage_limit must be more than 0, or empty")
        if "usage_limit" in sent and limit < usage:
            raise AdminCouponInvalid(
                "usage_limit",
                f"usage_limit can't be below the {usage} uses so far; deactivate the "
                "coupon to stop it",
            )
    if not 1 <= out["usage_limit_per_user"] <= _INT_MAX:
        raise AdminCouponInvalid("usage_limit_per_user", "usage_limit_per_user must be 1 or more")
    return out


# -- the service ----------------------------------------------------------------


class AdminCouponService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_coupons(
        self,
        *,
        status_filter: CouponStatus | None,
        q: str | None,
        personal: bool | None,
        page: int,
        per_page: int,
    ) -> AdminCouponListResponse:
        """Newest first. ``q`` matches any part of the code, in any case."""
        now = datetime.now(UTC)
        where = []
        if status_filter is not None:
            where.append(_status(now, _usage_count()) == status_filter)
        if q is not None and q.strip():
            where.append(func.lower(Coupon.code).contains(q.strip().lower(), autoescape=True))
        if personal is not None:
            where.append(Coupon.user_id.is_not(None) if personal else Coupon.user_id.is_(None))

        total = await self.db.scalar(select(func.count()).select_from(Coupon).where(*where))
        rows = await self.db.execute(
            _rows_query(now).where(*where).offset((page - 1) * per_page).limit(per_page)
        )
        return AdminCouponListResponse(
            items=[_row(*r) for r in rows.all()], total=total, page=page, per_page=per_page
        )

    async def detail(self, code: str) -> AdminCouponDetail:
        coupon = await self._load(code)
        row = await self._row_for(coupon.id)
        total = await self.db.scalar(
            select(func.coalesce(func.sum(CouponRedemption.discount_applied), 0)).where(
                CouponRedemption.coupon_id == coupon.id
            )
        )
        recent = await self.db.execute(
            select(CouponRedemption, Booking.status, User.name)
            .join(Booking, Booking.id == CouponRedemption.booking_id)
            .join(User, User.id == CouponRedemption.user_id)
            .where(CouponRedemption.coupon_id == coupon.id)
            .order_by(CouponRedemption.redeemed_at.desc(), CouponRedemption.id.desc())
            .limit(RECENT_REDEMPTIONS)
        )
        return AdminCouponDetail(
            **row.model_dump(),
            total_discount=float(total),
            recent_redemptions=[
                AdminCouponRedemptionRow(
                    booking_id=r.booking_id,
                    booking_status=booking_status,
                    redeemed_at=r.redeemed_at,
                    discount_applied=float(r.discount_applied),
                    customer_id=r.user_id,
                    customer_name=name,
                )
                for r, booking_status, name in recent.all()
            ],
        )

    async def create(self, actor: AuditActor, body: AdminCouponCreate) -> AdminCouponRow:
        code = _normalise_code(body.code)
        if await self._code_exists(code):
            raise CouponCodeTaken(code)

        now = datetime.now(UTC)
        sent = {f: getattr(body, f) for f in body.model_fields_set if f in EDITABLE_FIELDS}
        values = {f: getattr(body, f) for f in EDITABLE_FIELDS}
        if values["valid_from"] is None:
            sent.pop("valid_from", None)
            values["valid_from"] = now
        values = _validate(values, sent, now=now, creating=True, usage=0)

        coupon = Coupon(code=code.lower(), user_id=None, is_active=body.is_active, **values)
        try:
            # A savepoint, so a racing create of the same code is a 409 and the
            # rest of the transaction survives.
            async with self.db.begin_nested():
                self.db.add(coupon)
                await self.db.flush()
        except IntegrityError:
            raise CouponCodeTaken(code) from None

        await self.db.refresh(coupon)
        await audit_admin_action(
            self.db,
            actor,
            action="coupon.create",
            target_type="coupon",
            target_id=coupon.id,
            after=snapshot("coupon", coupon),
        )
        return await self._row_for(coupon.id)

    async def update(self, actor: AuditActor, code: str, body: AdminCouponUpdate) -> AdminCouponRow:
        coupon = await self._load(code, lock=True)
        sent = {f: getattr(body, f) for f in body.model_fields_set}
        if not sent:
            raise AdminCouponInvalid("body", "Send at least one field to change")

        old = {f: getattr(coupon, f) for f in EDITABLE_FIELDS}
        usage = await self.db.scalar(
            select(func.count(CouponRedemption.id)).where(CouponRedemption.coupon_id == coupon.id)
        )
        new = _validate({**old, **sent}, sent, now=datetime.now(UTC), creating=False, usage=usage)

        changed = [f for f in EDITABLE_FIELDS if new[f] != old[f]]
        if changed:
            for field in changed:
                setattr(coupon, field, new[field])
            await self.db.flush()
            await audit_admin_action(
                self.db,
                actor,
                action="coupon.update",
                target_type="coupon",
                target_id=coupon.id,
                before={f: old[f] for f in changed},
                after={f: new[f] for f in changed},
            )
        return await self._row_for(coupon.id)

    async def set_active(self, actor: AuditActor, code: str, active: bool) -> AdminCouponRow:
        """Deactivate or activate. Already in that state: 200, no audit row."""
        coupon = await self._load(code, lock=True)
        if coupon.is_active != active:
            coupon.is_active = active
            await self.db.flush()
            await audit_admin_action(
                self.db,
                actor,
                action="coupon.activate" if active else "coupon.deactivate",
                target_type="coupon",
                target_id=coupon.id,
                before={"is_active": not active},
                after={"is_active": active},
            )
        return await self._row_for(coupon.id)

    # -- helpers ----------------------------------------------------------------

    async def _code_exists(self, code: str) -> bool:
        found = await self.db.scalar(
            select(Coupon.id).where(func.lower(Coupon.code) == code.lower())
        )
        return found is not None

    async def _load(self, code: str, *, lock: bool = False) -> Coupon:
        """By code, in any case."""
        stmt = select(Coupon).where(func.lower(Coupon.code) == code.strip().lower())
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        coupon = await self.db.scalar(stmt)
        if coupon is None:
            raise CouponNotFound()
        return coupon

    async def _row_for(self, coupon_id: uuid.UUID) -> AdminCouponRow:
        result = await self.db.execute(_rows_query(datetime.now(UTC)).where(Coupon.id == coupon_id))
        return _row(*result.one())
