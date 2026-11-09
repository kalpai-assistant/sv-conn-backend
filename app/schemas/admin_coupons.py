"""SVC-429: the admin coupon surface (/admin/coupons/*).

Codes go out uppercased for display and are stored lowercased, which is what the
consumer lookups match on. Money goes out as JSON numbers, never Decimal strings.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, StrictBool, field_validator

from app.models.booking import BookingStatus
from app.models.coupon import DiscountType

CouponStatus = Literal["active", "inactive", "expired", "exhausted", "scheduled"]


def _lower_discount_type(value: Any) -> Any:
    # The ticket spells them PERCENT / FLAT; the enum values are lowercase.
    return value.strip().lower() if isinstance(value, str) else value


class AdminCouponRow(BaseModel):
    """One coupon. ``user_id`` set = a personal (referral) coupon, owned by
    ``owner_name`` / ``owner_phone``. ``usage_count`` counts redemption rows, a
    cancelled booking's included, which is the count the limits use."""

    id: uuid.UUID
    code: str
    discount_type: DiscountType
    discount_value: float
    max_discount: float | None
    min_order_amount: float | None
    valid_from: datetime
    valid_until: datetime
    usage_limit: int | None
    usage_limit_per_user: int
    is_active: bool
    user_id: uuid.UUID | None
    owner_name: str | None
    owner_phone: str | None
    usage_count: int
    status: CouponStatus
    created_at: datetime


class AdminCouponListResponse(BaseModel):
    items: list[AdminCouponRow]
    total: int
    page: int
    per_page: int


class AdminCouponRedemptionRow(BaseModel):
    booking_id: uuid.UUID
    booking_status: BookingStatus
    redeemed_at: datetime
    discount_applied: float
    customer_id: uuid.UUID
    customer_name: str | None


class AdminCouponDetail(AdminCouponRow):
    """The row plus its usage: the total discount given and the latest 20
    redemptions, newest first."""

    total_discount: float
    recent_redemptions: list[AdminCouponRedemptionRow]


class AdminCouponCreate(BaseModel):
    """A new global coupon. ``code`` is trimmed and uppercased, then must match
    ``^[A-Z0-9]{3,20}$``. ``valid_from`` defaults to now. A datetime with no
    offset is read as UTC."""

    model_config = ConfigDict(extra="forbid")

    code: str
    discount_type: DiscountType
    discount_value: Decimal
    max_discount: Decimal | None = None
    min_order_amount: Decimal | None = None
    valid_from: datetime | None = None
    valid_until: datetime
    usage_limit: int | None = None
    usage_limit_per_user: int = 1
    is_active: StrictBool = True

    _discount_type = field_validator("discount_type", mode="before")(_lower_discount_type)


class AdminCouponUpdate(BaseModel):
    """Any subset of the editable fields. ``code``, ``user_id`` and ``is_active``
    aren't editable here (deactivate / activate change the last). ``null`` clears
    ``max_discount``, ``min_order_amount`` or ``usage_limit`` and is refused on
    the others."""

    model_config = ConfigDict(extra="forbid")

    discount_type: DiscountType | None = None
    discount_value: Decimal | None = None
    max_discount: Decimal | None = None
    min_order_amount: Decimal | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    usage_limit: int | None = None
    usage_limit_per_user: int | None = None

    _discount_type = field_validator("discount_type", mode="before")(_lower_discount_type)
